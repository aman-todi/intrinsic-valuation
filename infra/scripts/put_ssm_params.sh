#!/usr/bin/env bash
# Copy the production secrets from a local .env into SSM Parameter Store as SecureString parameters
# under /dcf/prod/<NAME>, which the ECS task definitions reference in their `secrets` blocks.
#
# Values are never printed, never passed on the command line (they go through a 0600 temp JSON file
# via --cli-input-json, so they don't show up in `ps`), and placeholders copied from .env.example
# are refused. Re-running overwrites (new parameter version).
#
# Usage: infra/scripts/put_ssm_params.sh [--env-file PATH] [--migrations-via-pooler] [--dry-run]
#   --env-file PATH           default: <repo>/.env
#   --migrations-via-pooler   store DATABASE_POOLER_URL's value as /dcf/prod/DATABASE_URL too. Use
#                             this when Supabase's direct host (db.<ref>.supabase.co) is IPv6-only
#                             (no IPv4 add-on): Fargate in the default VPC is IPv4-only, so the
#                             one-off `alembic upgrade head` task must use the session pooler.
#   --dry-run                 report which keys would be written; write nothing.
set -euo pipefail

# shellcheck source=infra/scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

ENV_FILE="${REPO_ROOT}/.env"
VIA_POOLER=0
DRY_RUN=0
while (($#)); do
  case "$1" in
    --env-file)
      ENV_FILE="${2:?--env-file needs a path}"
      shift 2
      ;;
    --migrations-via-pooler)
      VIA_POOLER=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h | --help)
      sed -n '2,17p' "$0"
      exit 0
      ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -f "${ENV_FILE}" ]] || die "env file not found: ${ENV_FILE}"

load_config
require_cmd python3
if ((!DRY_RUN)); then
  require_cmd aws
  load_account
fi

OUT="$(mktemp -d)"
chmod 700 "${OUT}"
trap 'rm -rf "${OUT}"' EXIT

# Parse .env (dotenv subset: KEY=VALUE, optional quotes, `export `, trailing ` # comment` on unquoted
# values) and write one put-parameter request per key. Only key NAMES are ever printed.
if ! python3 - "${ENV_FILE}" "${OUT}" "${SSM_PREFIX}" "${VIA_POOLER}" "${SSM_SECRET_KEYS[@]}" <<'PY'
import json, os, re, sys

env_file, out_dir, prefix, via_pooler, *keys = sys.argv[1:]
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

PLACEHOLDER = re.compile(r"your-|:PASSWORD@|sk-ant-your-key|your-project-ref")
bad = []
os.umask(0o077)
for k in keys:
    v = vals.get(k, "")
    if not v:
        bad.append(f"{k}: missing/empty")
        continue
    if PLACEHOLDER.search(v):
        bad.append(f"{k}: still the .env.example placeholder")
        continue
    if k.endswith("_URL") and "DATABASE" in k and not v.startswith("postgresql+psycopg://"):
        bad.append(f"{k}: must start with postgresql+psycopg:// (rewrite Supabase's postgresql:// prefix)")
        continue
    req = {"Name": f"{prefix}/{k}", "Value": v, "Type": "SecureString", "Overwrite": True, "Tier": "Standard"}
    with open(os.path.join(out_dir, f"{k}.json"), "w", encoding="utf-8") as f:
        json.dump(req, f)
for b in bad:
    print(f"    skip {b}", file=sys.stderr)
sys.exit(1 if bad else 0)
PY
then
  die "fix the keys above in ${ENV_FILE} and re-run (nothing was written)"
fi

log "Writing ${#SSM_SECRET_KEYS[@]} SecureString parameters under ${SSM_PREFIX}/ ($(basename "${ENV_FILE}"))"
for k in "${SSM_SECRET_KEYS[@]}"; do
  if ((DRY_RUN)); then
    ok "would put ${SSM_PREFIX}/${k}"
  else
    aws ssm put-parameter --cli-input-json "file://${OUT}/${k}.json" >/dev/null
    ok "put ${SSM_PREFIX}/${k}"
  fi
done
if ((VIA_POOLER)); then
  ok "DATABASE_URL uses the session-pooler value (--migrations-via-pooler)"
fi
log "Done. ECS picks up new values on the next deployment (deploy.sh, or 'aws ecs update-service --force-new-deployment')."
