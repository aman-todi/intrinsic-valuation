#!/usr/bin/env bash
# Copy the production app settings from a local .env into SSM Parameter Store under /dcf/prod/<NAME>.
# Secrets become SecureString (AWS-managed aws/ssm key); a few non-secret settings become String. On
# every deploy the EC2 host renders /opt/dcf/.env from everything under /dcf/prod (plus the
# Terraform-owned /dcf/prod/config/*), so nothing secret is ever in git, an image or Terraform state.
#
# Values are never printed, never passed on the command line (they go through 0600 temp JSON files
# via --cli-input-json, so they don't show up in `ps`), and .env.example placeholders are refused.
# Re-running overwrites (new parameter version); redeploy afterwards to pick the values up.
#
# Usage: infra/scripts/put_ssm_params.sh [--env-file PATH] [--direct-migrations] [--dry-run]
#   --env-file PATH       default: <repo>/.env
#   --direct-migrations   keep .env's DATABASE_URL (Supabase direct host) for Alembic. By default
#                         /dcf/prod/DATABASE_URL gets the DATABASE_POOLER_URL value: the direct host
#                         db.<ref>.supabase.co is IPv6-only without Supabase's IPv4 add-on, and the
#                         EC2 host in the default VPC is IPv4-only. The session pooler handles DDL.
#   --dry-run             validate and list the keys that would be written; write nothing.
# Env: AWS_REGION (default: aws configure / us-east-1), PROJECT (dcf).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT="${PROJECT:-dcf}"
SSM_PREFIX="/${PROJECT}/prod"

# SecureString. AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY are deliberately absent: the instance role
# provides AWS credentials on the host.
SECRET_KEYS=(SUPABASE_ANON_KEY SUPABASE_SERVICE_ROLE_KEY DATABASE_URL DATABASE_POOLER_URL ANTHROPIC_API_KEY FRED_API_KEY)
# String (non-secret). ANTHROPIC_MODEL falls back to claude-sonnet-5 when absent from .env.
PLAIN_KEYS=(SUPABASE_URL SEC_EDGAR_USER_AGENT ANTHROPIC_MODEL)

log() { printf '\033[1;34m==>\033[0m %s\n' "$*" >&2; }
ok() { printf '    \033[32mok\033[0m %s\n' "$*" >&2; }
die() {
  printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2
  exit 1
}

ENV_FILE="${REPO_ROOT}/.env"
VIA_POOLER=1
DRY_RUN=0
while (($#)); do
  case "$1" in
    --env-file)
      ENV_FILE="${2:?--env-file needs a path}"
      shift 2
      ;;
    --direct-migrations)
      VIA_POOLER=0
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h | --help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -f "${ENV_FILE}" ]] || die "env file not found: ${ENV_FILE}"
command -v python3 >/dev/null || die "python3 is required"
if ((!DRY_RUN)); then
  command -v aws >/dev/null || die "aws CLI v2 is required"
  export AWS_PAGER=""
  AWS_REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || true)}"
  AWS_REGION="${AWS_REGION:-us-east-1}"
  export AWS_REGION AWS_DEFAULT_REGION="${AWS_REGION}"
  account="$(aws sts get-caller-identity --query Account --output text)" || die "AWS credentials not working"
  log "Account ${account}, region ${AWS_REGION}"
fi

OUT="$(mktemp -d)"
chmod 700 "${OUT}"
trap 'rm -rf "${OUT}"' EXIT

# Parse .env (dotenv subset: KEY=VALUE, optional quotes, `export `, trailing ` # comment` on unquoted
# values) and write one put-parameter request per key. Only key NAMES are ever printed.
if ! python3 - "${ENV_FILE}" "${OUT}" "${SSM_PREFIX}" "${VIA_POOLER}" "${#SECRET_KEYS[@]}" \
  "${SECRET_KEYS[@]}" "${PLAIN_KEYS[@]}" <<'PY'; then
import json, os, re, sys

env_file, out_dir, prefix, via_pooler, n_secret, *keys = sys.argv[1:]
secret_keys, plain_keys = keys[: int(n_secret)], keys[int(n_secret):]
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

if via_pooler == "1" and vals.get("DATABASE_POOLER_URL"):
    vals["DATABASE_URL"] = vals["DATABASE_POOLER_URL"]
vals.setdefault("ANTHROPIC_MODEL", "claude-sonnet-5")

PLACEHOLDER = re.compile(r"your-|:PASSWORD@|sk-ant-your-key|your-project-ref|contact@example\.com")
bad = []
os.umask(0o077)
for k in secret_keys + plain_keys:
    v = vals.get(k, "")
    if not v:
        bad.append(f"{k}: missing/empty")
    elif PLACEHOLDER.search(v):
        bad.append(f"{k}: still the .env.example placeholder")
    elif "DATABASE" in k and not v.startswith("postgresql+psycopg://"):
        bad.append(f"{k}: must start with postgresql+psycopg:// (rewrite Supabase's postgresql:// prefix)")
    elif "DATABASE" in k and ":6543/" in v:
        bad.append(f"{k}: that is the transaction pooler (6543); use the session pooler (5432)")
    elif k == "SUPABASE_URL" and not v.startswith("https://"):
        bad.append(f"{k}: must be the https:// project URL")
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
if ((VIA_POOLER)); then
  ok "DATABASE_URL = the session-pooler value (default; --direct-migrations to keep the direct host)"
fi
log "Done. Redeploy (push to main, or infra/scripts/deploy.sh) so the host re-renders /opt/dcf/.env."
