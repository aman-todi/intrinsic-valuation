#!/usr/bin/env bash
# Copy the owner-supplied production settings from a local .env into SSM Parameter Store under
# /dcf/prod/<NAME>: the third-party API keys as SecureString (AWS-managed aws/ssm key) and two
# non-secret settings as String. On every deploy the EC2 host renders /opt/dcf/.env from everything
# under /dcf/prod, which also holds what Terraform writes:
#   /dcf/prod/DATABASE_URL   SecureString (RDS; generated password)   -- never written by this script
#   /dcf/prod/config/*       String (COGNITO_*, CORS_ORIGINS, APP_DOMAIN, S3_BUCKET_NAME, ...)
#
# Values are never printed, never passed on the command line (they go through 0600 temp JSON files
# via --cli-input-json, so they don't show up in `ps`), and .env.example placeholders are refused.
# Re-running overwrites (new parameter version); redeploy afterwards to pick the values up.
#
# Usage: infra/scripts/put_ssm_params.sh [--env-file PATH] [--dry-run]
#   --env-file PATH   default: <repo>/.env
#   --dry-run         validate and list the keys that would be written; write nothing.
# Env: AWS_REGION (default: aws configure / us-east-2), PROJECT (dcf).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT="${PROJECT:-dcf}"
SSM_PREFIX="/${PROJECT}/prod"

# SecureString. AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY are deliberately absent: the instance role
# provides AWS credentials on the host.
SECRET_KEYS=(ANTHROPIC_API_KEY FRED_API_KEY)
# String (non-secret). ANTHROPIC_MODEL falls back to claude-sonnet-5 when absent from .env.
PLAIN_KEYS=(SEC_EDGAR_USER_AGENT ANTHROPIC_MODEL)
# Owned by Terraform: this script must never write them, even if .env has a (local) value.
TERRAFORM_KEYS=(DATABASE_URL COGNITO_REGION COGNITO_USER_POOL_ID COGNITO_APP_CLIENT_ID CORS_ORIGINS)

log() { printf '\033[1;34m==>\033[0m %s\n' "$*" >&2; }
ok() { printf '    \033[32mok\033[0m %s\n' "$*" >&2; }
die() {
  printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2
  exit 1
}

ENV_FILE="${REPO_ROOT}/.env"
DRY_RUN=0
while (($#)); do
  case "$1" in
    --env-file)
      ENV_FILE="${2:?--env-file needs a path}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h | --help)
      sed -n '2,16p' "$0"
      exit 0
      ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -f "${ENV_FILE}" ]] || die "env file not found: ${ENV_FILE}"
command -v python3 >/dev/null || die "python3 is required"

# Guard against a future edit adding a Terraform-owned key to the write lists.
for k in "${SECRET_KEYS[@]}" "${PLAIN_KEYS[@]}"; do
  for t in "${TERRAFORM_KEYS[@]}"; do
    [[ "${k}" != "${t}" ]] || die "${k} is Terraform-managed and must not be written by this script"
  done
done

if ((!DRY_RUN)); then
  command -v aws >/dev/null || die "aws CLI v2 is required"
  export AWS_PAGER=""
  AWS_REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || true)}"
  AWS_REGION="${AWS_REGION:-us-east-2}"
  export AWS_REGION AWS_DEFAULT_REGION="${AWS_REGION}"
  account="$(aws sts get-caller-identity --query Account --output text)" || die "AWS credentials not working"
  log "Account ${account}, region ${AWS_REGION}"
fi

OUT="$(mktemp -d)"
chmod 700 "${OUT}"
trap 'rm -rf "${OUT}"' EXIT

# Parse .env (dotenv subset: KEY=VALUE, optional quotes, `export `, trailing ` # comment` on unquoted
# values) and write one put-parameter request per key. Only key NAMES are ever printed.
if ! python3 - "${ENV_FILE}" "${OUT}" "${SSM_PREFIX}" "${#SECRET_KEYS[@]}" "${#PLAIN_KEYS[@]}" \
  "${SECRET_KEYS[@]}" "${PLAIN_KEYS[@]}" "${TERRAFORM_KEYS[@]}" <<'PY'; then
import json, os, re, sys

env_file, out_dir, prefix, n_secret, n_plain, *keys = sys.argv[1:]
n_secret, n_plain = int(n_secret), int(n_plain)
secret_keys = keys[:n_secret]
plain_keys = keys[n_secret : n_secret + n_plain]
terraform_keys = keys[n_secret + n_plain :]
vals = {}
with open(env_file, encoding="utf-8") as fh:
    for raw in fh:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if v[:1] in ("'", '"'):
            end = v.find(v[0], 1)
            v = v[1:end] if end != -1 else v[1:]
        else:
            v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
        vals[k] = v

vals.setdefault("ANTHROPIC_MODEL", "claude-sonnet-5")

ignored = [k for k in terraform_keys if vals.get(k)]
if ignored:
    print(f"    note: ignoring {', '.join(ignored)} from the env file (Terraform-managed in production)",
          file=sys.stderr)

PLACEHOLDER = re.compile(r"your-|sk-ant-your-key|contact@example\.com")
bad = []
os.umask(0o077)
for k in secret_keys + plain_keys:
    v = vals.get(k, "")
    if not v:
        bad.append(f"{k}: missing/empty")
    elif PLACEHOLDER.search(v):
        bad.append(f"{k}: still the .env.example placeholder")
    elif "'" in v or "\n" in v:
        bad.append(f"{k}: single quotes/newlines are not supported by the deploy's .env renderer")
    else:
        kind = "SecureString" if k in secret_keys else "String"
        req = {"Name": f"{prefix}/{k}", "Value": v, "Type": kind, "Overwrite": True, "Tier": "Standard"}
        with open(os.path.join(out_dir, f"{k}.json"), "w", encoding="utf-8") as f:
            json.dump(req, f)
for b in bad:
    print(f"    fix {b}", file=sys.stderr)
sys.exit(1 if bad else 0)
PY
  die "fix the keys above in ${ENV_FILE} and re-run (nothing was written)"
fi

log "Writing ${#SECRET_KEYS[@]} SecureString + ${#PLAIN_KEYS[@]} String parameters under ${SSM_PREFIX}/"
for k in "${SECRET_KEYS[@]}" "${PLAIN_KEYS[@]}"; do
  if ((DRY_RUN)); then
    ok "would put ${SSM_PREFIX}/${k}"
  else
    aws ssm put-parameter --cli-input-json "file://${OUT}/${k}.json" >/dev/null
    ok "put ${SSM_PREFIX}/${k}"
  fi
done

log "Done. Redeploy (push to main, or infra/scripts/deploy.sh) so the host re-renders /opt/dcf/.env."
