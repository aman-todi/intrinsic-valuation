#!/usr/bin/env bash
# Runs ON the EC2 host, as root. Installed as /opt/dcf/deploy.sh by every deploy; invoked through SSM
# Run Command by infra/scripts/deploy.sh (CI or laptop), or by hand from an SSM session to roll back:
#
#   sudo /opt/dcf/deploy.sh <image-tag>          # image tag = full git SHA already in ECR
#   sudo SKIP_MIGRATIONS=true /opt/dcf/deploy.sh <older-sha>
#
# Steps: install the shipped compose file / Caddyfile (when run from an extracted bundle) -> render
# /opt/dcf/.env (0600) from every SSM parameter under /dcf/prod -> ECR login -> docker compose pull ->
# `alembic upgrade head` in a one-off api container (abort + restore the previous .env on failure) ->
# docker compose up -d -> caddy reload if the Caddyfile changed -> wait for health -> prune images.
#
# Env: SKIP_MIGRATIONS (default false), HEALTH_TIMEOUT_SECONDS (180), MIGRATION_TIMEOUT_SECONDS (600),
#      DCF_HOME (/opt/dcf), SSM_PREFIX (/dcf/prod), AWS_REGION (default: from instance metadata).
set -euo pipefail

TAG="${1:-}"
DCF_HOME="${DCF_HOME:-/opt/dcf}"
SSM_PREFIX="${SSM_PREFIX:-/dcf/prod}"
SKIP_MIGRATIONS="${SKIP_MIGRATIONS:-false}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-180}"
MIGRATION_TIMEOUT_SECONDS="${MIGRATION_TIMEOUT_SECONDS:-600}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${DCF_HOME}/.env"
COMPOSE=(docker compose --project-directory "${DCF_HOME}" -f "${DCF_HOME}/docker-compose.prod.yml")

log() { printf '[deploy %s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
die() {
  log "ERROR: $*"
  exit 1
}

[[ "${TAG}" =~ ^[A-Za-z0-9._-]{1,128}$ ]] || die "usage: $0 <image-tag> (the git SHA pushed to ECR)"
((EUID == 0)) || die "run as root (sudo)"
[[ -f "${DCF_HOME}/.provisioned" ]] || die "${DCF_HOME}/.provisioned missing: user_data has not finished"
docker compose version >/dev/null 2>&1 || die "docker / the compose plugin is missing"

# One deploy at a time.
exec 9>"${DCF_HOME}/.deploy.lock"
flock -n 9 || die "another deploy is running"

if [[ -z "${AWS_REGION:-}" ]]; then
  imds_token="$(curl -fsS -X PUT http://169.254.169.254/latest/api/token \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')"
  AWS_REGION="$(curl -fsS -H "X-aws-ec2-metadata-token: ${imds_token}" \
    http://169.254.169.254/latest/meta-data/placement/region)"
fi
export AWS_REGION AWS_DEFAULT_REGION="${AWS_REGION}" AWS_PAGER=""
umask 077

log "Deploying ${TAG} (${AWS_REGION})"

# ---------------------------------------------------------------- 1. shipped files
caddy_changed=false
if [[ "${SRC_DIR}" != "${DCF_HOME}" ]]; then
  install -d -m 0755 "${DCF_HOME}/caddy"
  if ! cmp -s "${SRC_DIR}/Caddyfile" "${DCF_HOME}/caddy/Caddyfile"; then
    caddy_changed=true
  fi
  install -m 0644 "${SRC_DIR}/docker-compose.prod.yml" "${DCF_HOME}/docker-compose.prod.yml"
  install -m 0644 "${SRC_DIR}/Caddyfile" "${DCF_HOME}/caddy/Caddyfile"
  install -m 0750 "${SRC_DIR}/deploy_remote.sh" "${DCF_HOME}/deploy.sh"
fi
[[ -f "${DCF_HOME}/docker-compose.prod.yml" ]] || die "no compose file: run a full deploy (infra/scripts/deploy.sh) first"

# ---------------------------------------------------------------- 2. render .env from SSM
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
aws ssm get-parameters-by-path --path "${SSM_PREFIX}" --recursive --with-decryption \
  --query 'Parameters[].[Name,Value]' --output json >"${work}/params.json"

# Basename = env var name; Terraform-owned config/ wins over same-named keys; "-" means empty (SSM
# cannot store ""). Values are single-quoted (literal for docker compose). Only key names are printed.
python3 - "${work}/params.json" "${SSM_PREFIX}" "${TAG}" >"${work}/env" <<'PY'
import json, re, sys

params_file, prefix, tag = sys.argv[1:]
secrets, config = {}, {}
for name, value in json.load(open(params_file, encoding="utf-8")):
    rel = name[len(prefix) + 1:]
    key = rel.rsplit("/", 1)[-1]
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
        print(f"skip parameter {name}: not an env var name", file=sys.stderr)
        continue
    if rel.startswith("config/"):
        config[key] = "" if value == "-" else value
    else:
        secrets[key] = value
env = {**secrets, **config, "IMAGE_TAG": tag}

# Terraform-owned: DATABASE_URL (SecureString), COGNITO_*, APP_DOMAIN, ... (config/*).
# Owner-supplied (put_ssm_params.sh): ANTHROPIC_API_KEY, FRED_API_KEY, SEC_EDGAR_USER_AGENT.
required = [
    "DATABASE_URL", "COGNITO_REGION", "COGNITO_USER_POOL_ID", "COGNITO_APP_CLIENT_ID",
    "ANTHROPIC_API_KEY", "FRED_API_KEY", "SEC_EDGAR_USER_AGENT",
    "APP_DOMAIN", "PUBLIC_API_BASE_URL", "ACME_EMAIL", "AWS_REGION", "S3_BUCKET_NAME", "ECR_REGISTRY",
]
missing = [k for k in required if not env.get(k)]
if env.get("DATABASE_URL") and not env["DATABASE_URL"].startswith("postgresql+psycopg://"):
    print("DATABASE_URL must start with postgresql+psycopg:// (it is written by Terraform)", file=sys.stderr)
    missing.append("DATABASE_URL (malformed)")
bad = [k for k, v in env.items() if "'" in v or "\n" in v]
if missing:
    print(f"missing SSM parameters under {prefix}/: {', '.join(missing)} "
          "(run infra/scripts/put_ssm_params.sh / terraform apply)", file=sys.stderr)
if bad:
    print(f"values containing a single quote or newline are not supported: {', '.join(bad)}", file=sys.stderr)
if missing or bad:
    sys.exit(1)
if not env.get("CORS_ORIGINS"):
    print("WARNING: CORS_ORIGINS is empty - browsers cannot call the API until frontend_origins is set "
          "in terraform.tfvars (then terraform apply + redeploy)", file=sys.stderr)
for k in sorted(env):
    print(f"{k}='{env[k]}'")
PY
envval() { sed -n "s/^$1='\(.*\)'\$/\1/p" "${work}/env"; }
ECR_REGISTRY="$(envval ECR_REGISTRY)"
APP_DOMAIN="$(envval APP_DOMAIN)"
log "Rendered .env ($(wc -l <"${work}/env") keys) for ${APP_DOMAIN}"

# Swap in the new .env; until `up` runs, a failure restores the previous one (running containers were
# never touched, and a reboot keeps using them as they are).
committed=false
if [[ -f "${ENV_FILE}" ]]; then cp -p "${ENV_FILE}" "${ENV_FILE}.prev"; fi
install -m 0600 "${work}/env" "${ENV_FILE}"
restore_env() {
  if [[ "${committed}" != "true" && -f "${ENV_FILE}.prev" ]]; then
    cp -p "${ENV_FILE}.prev" "${ENV_FILE}"
    log "restored the previous .env; running containers unchanged"
  fi
  rm -rf "${work}"
}
trap restore_env EXIT

# ---------------------------------------------------------------- 3. pull
log "ECR login + pull"
aws ecr get-login-password | docker login --username AWS --password-stdin "${ECR_REGISTRY}" >/dev/null
"${COMPOSE[@]}" pull --quiet

# ---------------------------------------------------------------- 4. migrations
if [[ "${SKIP_MIGRATIONS}" == "true" ]]; then
  log "Skipping migrations (SKIP_MIGRATIONS=true)"
else
  log "alembic upgrade head (new api image)"
  if ! timeout "${MIGRATION_TIMEOUT_SECONDS}" "${COMPOSE[@]}" run --rm --no-deps -T api alembic upgrade head; then
    die "migrations failed - nothing was restarted"
  fi
fi

# ---------------------------------------------------------------- 5. roll out
log "docker compose up -d"
committed=true
"${COMPOSE[@]}" up -d --remove-orphans
if [[ "${caddy_changed}" == "true" ]]; then
  log "Caddyfile changed: reloading caddy"
  "${COMPOSE[@]}" exec -T caddy caddy reload --config /etc/caddy/Caddyfile ||
    "${COMPOSE[@]}" restart caddy
fi

# ---------------------------------------------------------------- 6. health
log "Waiting up to ${HEALTH_TIMEOUT_SECONDS}s for the api to be healthy"
deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
while :; do
  api_cid="$("${COMPOSE[@]}" ps -q api)"
  api_health="$(docker inspect -f '{{.State.Health.Status}}' "${api_cid}" 2>/dev/null || echo unknown)"
  running="$("${COMPOSE[@]}" ps --status running --services | sort | tr '\n' ' ')"
  if [[ "${api_health}" == "healthy" && "${running}" == "api caddy redis worker " ]]; then
    break
  fi
  if ((SECONDS >= deadline)); then
    "${COMPOSE[@]}" ps
    "${COMPOSE[@]}" logs --tail 60 api worker caddy
    die "not healthy after ${HEALTH_TIMEOUT_SECONDS}s (api=${api_health}, running: ${running})"
  fi
  sleep 5
done
log "api healthy; services running: ${running}"

# Through Caddy + TLS. Only a warning: on the very first deploy the certificate may still be issuing.
if curl -fsS --max-time 15 --resolve "${APP_DOMAIN}:443:127.0.0.1" "https://${APP_DOMAIN}/api/health" >/dev/null; then
  log "https://${APP_DOMAIN}/api/health OK"
else
  log "WARNING: https://${APP_DOMAIN}/api/health not reachable yet (certificate issuance?). Check: docker compose logs caddy"
fi

# ---------------------------------------------------------------- 7. bookkeeping
if [[ -f "${DCF_HOME}/CURRENT_TAG" ]] && [[ "$(cat "${DCF_HOME}/CURRENT_TAG")" != "${TAG}" ]]; then
  mv "${DCF_HOME}/CURRENT_TAG" "${DCF_HOME}/PREVIOUS_TAG"
fi
echo "${TAG}" >"${DCF_HOME}/CURRENT_TAG"
rm -f "${ENV_FILE}.prev"
docker image prune -af >/dev/null || true
log "Done: ${TAG} is live"
