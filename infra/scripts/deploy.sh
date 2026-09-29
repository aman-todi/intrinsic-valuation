#!/usr/bin/env bash
# Deploy the API stack to the EC2 host (docs/DEPLOYMENT.md). Used by .github/workflows/deploy.yml
# (with --skip-build, after CI pushed the images) and for manual deploys from a laptop.
#
#   1. build + push dcf-api / dcf-worker for linux/arm64, tagged with the full git SHA
#      (skipped per image when that tag is already in ECR, or entirely with --skip-build)
#   2. upload a bundle (docker-compose.prod.yml, Caddyfile, deploy_remote.sh) to
#      s3://<bucket>/deploy/<sha>.tar.gz
#   3. SSM Run Command on the instance: download the bundle, run deploy_remote.sh <sha>
#      (render .env from SSM, pull, migrate, up -d, health check) and stream back its output
#
# Usage: infra/scripts/deploy.sh [--tag SHA] [--skip-build] [--skip-migrations] [--force-build]
# Env:   AWS_REGION (default: aws configure / us-east-1), PROJECT (dcf), ALLOW_DIRTY (false),
#        DEPLOY_TIMEOUT_SECONDS (1800)
# Needs: aws CLI v2, python3, tar, git; docker with buildx for builds (QEMU/binfmt on x86 hosts;
#        Apple Silicon builds arm64 natively).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PROJECT="${PROJECT:-dcf}"
SSM_CONFIG="/${PROJECT}/prod/config"
DEPLOY_TIMEOUT_SECONDS="${DEPLOY_TIMEOUT_SECONDS:-1800}"
IMAGES=(api worker)

log() { printf '\033[1;34m==>\033[0m %s\n' "$*" >&2; }
die() {
  printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2
  exit 1
}
require_cmd() {
  local c
  for c in "$@"; do command -v "$c" >/dev/null 2>&1 || die "'$c' is required but not on PATH"; done
}

TAG=""
SKIP_BUILD=false
FORCE_BUILD=false
SKIP_MIGRATIONS=false
while (($#)); do
  case "$1" in
    --tag)
      TAG="${2:?--tag needs a value}"
      shift 2
      ;;
    --skip-build)
      SKIP_BUILD=true
      shift
      ;;
    --force-build)
      FORCE_BUILD=true
      shift
      ;;
    --skip-migrations)
      SKIP_MIGRATIONS=true
      shift
      ;;
    -h | --help)
      sed -n '2,17p' "$0"
      exit 0
      ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_cmd aws python3 tar git
export AWS_PAGER=""
AWS_REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || true)}"
AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_REGION AWS_DEFAULT_REGION="${AWS_REGION}"

cd "${REPO_ROOT}"
if [[ -z "${TAG}" ]]; then
  if [[ -n "$(git status --porcelain --untracked-files=no)" && "${ALLOW_DIRTY:-false}" != "true" ]]; then
    die "uncommitted changes: commit them (images are tagged by git SHA) or set ALLOW_DIRTY=true"
  fi
  TAG="$(git rev-parse HEAD)" # full SHA, same as CI's github.sha
fi
[[ "${TAG}" =~ ^[A-Za-z0-9._-]{1,128}$ ]] || die "invalid tag: ${TAG}"

# ---------------------------------------------------------------- config from SSM (Terraform-owned)
cfg="$(aws ssm get-parameters \
  --names "${SSM_CONFIG}/INSTANCE_ID" "${SSM_CONFIG}/S3_BUCKET_NAME" "${SSM_CONFIG}/ECR_REGISTRY" \
  --query 'Parameters[].[Name,Value]' --output text)" ||
  die "cannot read ${SSM_CONFIG}/* (wrong account/region, or terraform apply has not run)"
cfgval() { awk -v n="${SSM_CONFIG}/$1" '$1 == n { print $2 }' <<<"${cfg}"; }
INSTANCE_ID="$(cfgval INSTANCE_ID)"
BUCKET="$(cfgval S3_BUCKET_NAME)"
REGISTRY="$(cfgval ECR_REGISTRY)"
[[ -n "${INSTANCE_ID}" && -n "${BUCKET}" && -n "${REGISTRY}" ]] ||
  die "missing ${SSM_CONFIG}/{INSTANCE_ID,S3_BUCKET_NAME,ECR_REGISTRY}; run terraform apply"

log "Deploying ${TAG} to ${INSTANCE_ID} (${AWS_REGION})"

# ---------------------------------------------------------------- 1. images
image_in_ecr() {
  aws ecr describe-images --repository-name "${PROJECT}-$1" --image-ids "imageTag=${TAG}" >/dev/null 2>&1
}
if [[ "${SKIP_BUILD}" == "true" ]]; then
  for img in "${IMAGES[@]}"; do
    image_in_ecr "${img}" || die "${PROJECT}-${img}:${TAG} is not in ECR (drop --skip-build)"
  done
else
  require_cmd docker
  docker buildx version >/dev/null 2>&1 || die "docker buildx is required"
  aws ecr get-login-password | docker login --username AWS --password-stdin "${REGISTRY}" >/dev/null
  for img in "${IMAGES[@]}"; do
    ref="${REGISTRY}/${PROJECT}-${img}:${TAG}"
    if [[ "${FORCE_BUILD}" != "true" ]] && image_in_ecr "${img}"; then
      log "${ref} already in ECR, not rebuilding"
      continue
    fi
    log "Building ${ref} (linux/arm64)"
    docker buildx build --platform linux/arm64 --provenance=false \
      -f "infra/docker/Dockerfile.${img}" -t "${ref}" --push .
  done
fi

# ---------------------------------------------------------------- 2. bundle
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
mkdir "${work}/bundle"
cp infra/deploy/docker-compose.prod.yml infra/deploy/Caddyfile infra/scripts/deploy_remote.sh "${work}/bundle/"
tar -C "${work}/bundle" -czf "${work}/bundle.tgz" .
bundle_uri="s3://${BUCKET}/deploy/${TAG}.tar.gz"
aws s3 cp --only-show-errors "${work}/bundle.tgz" "${bundle_uri}"
log "Uploaded ${bundle_uri}"

# ---------------------------------------------------------------- 3. run on the instance
# A freshly created instance needs a minute or two before its SSM agent registers.
ping=""
for _ in $(seq 1 30); do
  ping="$(aws ssm describe-instance-information \
    --filters "Key=InstanceIds,Values=${INSTANCE_ID}" \
    --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null || true)"
  [[ "${ping}" == "Online" ]] && break
  log "waiting for the SSM agent on ${INSTANCE_ID} (status: ${ping:-unknown})"
  sleep 10
done
[[ "${ping}" == "Online" ]] || die "SSM agent on ${INSTANCE_ID} is not online (instance stopped? no internet route?)"

python3 - "${AWS_REGION}" "${bundle_uri}" "${TAG}" "${SKIP_MIGRATIONS}" "${DEPLOY_TIMEOUT_SECONDS}" \
  >"${work}/params.json" <<'PY'
import json, shlex, sys

region, bundle, tag, skip_migrations, timeout = sys.argv[1:]
q = shlex.quote
script = [
    "set -eu",
    f"export AWS_REGION={q(region)} AWS_DEFAULT_REGION={q(region)}",
    "cloud-init status --wait >/dev/null 2>&1 || true",  # first boot: wait for user_data
    'd="$(mktemp -d)"',
    "trap 'rm -rf \"$d\"' EXIT",
    f'aws s3 cp --only-show-errors {q(bundle)} "$d/bundle.tgz"',
    'tar -xzf "$d/bundle.tgz" -C "$d"',
    f'SKIP_MIGRATIONS={q(skip_migrations)} bash "$d/deploy_remote.sh" {q(tag)} 2>&1',
]
json.dump({"commands": script, "executionTimeout": [timeout]}, sys.stdout)
PY

command_id="$(aws ssm send-command \
  --instance-ids "${INSTANCE_ID}" \
  --document-name AWS-RunShellScript \
  --comment "${PROJECT} deploy ${TAG:0:12}" \
  --timeout-seconds 600 \
  --parameters "file://${work}/params.json" \
  --query Command.CommandId --output text)"
log "SSM command ${command_id} sent; waiting (up to ${DEPLOY_TIMEOUT_SECONDS}s)"

deadline=$((SECONDS + DEPLOY_TIMEOUT_SECONDS + 120))
status=Pending
while :; do
  sleep 10
  status="$(aws ssm get-command-invocation --command-id "${command_id}" --instance-id "${INSTANCE_ID}" \
    --query Status --output text 2>/dev/null || echo Pending)"
  case "${status}" in
    Pending | InProgress | Delayed) ;;
    *) break ;;
  esac
  ((SECONDS < deadline)) || break
done

aws ssm get-command-invocation --command-id "${command_id}" --instance-id "${INSTANCE_ID}" \
  --query '[StandardOutputContent, StandardErrorContent]' --output text 2>/dev/null || true

[[ "${status}" == "Success" ]] ||
  die "deploy ${status} on ${INSTANCE_ID} (full output: aws ssm get-command-invocation --command-id ${command_id} --instance-id ${INSTANCE_ID})"
log "Deployed ${TAG}"
