#!/usr/bin/env bash
# Build, push and roll out the DCF app to ECS Fargate (spec §11.3, §14.2, §14.7).
#
#   1. build + push dcf-api / dcf-worker images tagged with the git SHA (skipped if already in ECR)
#   2. render infra/ecs/task-def-*.json (envsubst) and register new revisions
#   3. make sure the redis service exists (created once; only redeployed with DEPLOY_REDIS=true)
#   4. run `alembic upgrade head` as a one-off Fargate task from the new api image; abort on failure
#   5. create-or-update the api + worker services and wait until the new revisions are live
#      (a circuit-breaker rollback counts as a failed deploy)
#
# Prereqs: bootstrap_aws.sh has run and put_ssm_params.sh has populated /dcf/prod/*.
# Config (env or infra/deploy.env): SUPABASE_URL, CORS_ORIGINS required; AWS_REGION, S3_BUCKET_NAME,
# SEC_EDGAR_USER_AGENT, ANTHROPIC_MODEL, IMAGE_TAG, SKIP_MIGRATIONS, DEPLOY_REDIS, USE_FARGATE_SPOT,
# REDIS_IMAGE, ALLOW_DIRTY, DEPLOY_TIMEOUT_SECONDS optional.
# Needs: aws CLI v2, docker (buildx/BuildKit), git, python3; envsubst (gettext) preferred.
set -euo pipefail

# shellcheck source=infra/scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

load_config
require_cmd aws docker git python3
load_account

: "${SUPABASE_URL:?set SUPABASE_URL (infra/deploy.env or GitHub variable)}"
: "${CORS_ORIGINS:?set CORS_ORIGINS to the frontend origin(s), e.g. https://your-app.vercel.app}"
SEC_EDGAR_USER_AGENT="${SEC_EDGAR_USER_AGENT:-DCF-Valuation-App aman.todi01@gmail.com}"
ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-claude-sonnet-5}"
REDIS_IMAGE="${REDIS_IMAGE:-public.ecr.aws/docker/library/redis:7-alpine}"
REDIS_URL="redis://${CLOUDMAP_REDIS_SERVICE}.${CLOUDMAP_NAMESPACE}:6379"
DEPLOY_TIMEOUT_SECONDS="${DEPLOY_TIMEOUT_SECONDS:-1500}"
export SUPABASE_URL CORS_ORIGINS SEC_EDGAR_USER_AGENT ANTHROPIC_MODEL REDIS_URL

cd "${REPO_ROOT}"
if [[ -n "$(git status --porcelain --untracked-files=no)" && "${ALLOW_DIRTY:-false}" != "true" ]]; then
  die "working tree has uncommitted changes; commit them (images are tagged by git SHA) or set ALLOW_DIRTY=true"
fi
# Full SHA, same as CI (github.sha), so a local deploy and a CI deploy of one commit share images.
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse HEAD)}"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

log "Deploying ${IMAGE_TAG} to ${CLUSTER_NAME} (${ACCOUNT_ID}/${REGION})"

# ---------------------------------------------------------------- preflight
log "Preflight"
[[ "$(aws ecs describe-clusters --clusters "${CLUSTER_NAME}" \
  --query "clusters[?status=='ACTIVE'].clusterName | [0]" --output text)" == "${CLUSTER_NAME}" ]] ||
  die "cluster ${CLUSTER_NAME} not found; run infra/scripts/bootstrap_aws.sh first"
existing_params=" $(aws ssm describe-parameters \
  --parameter-filters "Key=Name,Option=BeginsWith,Values=${SSM_PREFIX}/" \
  --query 'Parameters[].Name' --output text | tr '\t' ' ') "
missing=()
for k in "${SSM_SECRET_KEYS[@]}"; do
  [[ "${existing_params}" == *" ${SSM_PREFIX}/${k} "* ]] || missing+=("${SSM_PREFIX}/${k}")
done
((${#missing[@]} == 0)) || die "missing SSM parameters: ${missing[*]} (run infra/scripts/put_ssm_params.sh)"
VPC_ID="$(default_vpc_id)"
SUBNETS="$(default_subnet_ids "${VPC_ID}")"
API_SG="$(sg_id "${API_SG_NAME}" "${VPC_ID}")"
WORKER_SG="$(sg_id "${WORKER_SG_NAME}" "${VPC_ID}")"
REDIS_SG="$(sg_id "${REDIS_SG_NAME}" "${VPC_ID}")"
TG_ARN="$(target_group_arn)"
[[ -n "${API_SG}" && -n "${WORKER_SG}" && -n "${REDIS_SG}" && -n "${TG_ARN}" ]] ||
  die "security groups / target group missing; run infra/scripts/bootstrap_aws.sh"
ok "cluster, SSM parameters, network, target group present"

# ---------------------------------------------------------------- images
log "Images"
aws ecr get-login-password | docker login --username AWS --password-stdin "${REGISTRY}" >/dev/null
for svc in api worker; do
  repo="${PROJECT}-${svc}"
  uri="${REGISTRY}/${repo}:${IMAGE_TAG}"
  if aws ecr describe-images --repository-name "${repo}" --image-ids "imageTag=${IMAGE_TAG}" >/dev/null 2>&1; then
    ok "${repo}:${IMAGE_TAG} already in ECR, not rebuilding"
    continue
  fi
  log "docker build ${repo}:${IMAGE_TAG} (linux/amd64)"
  docker build --platform linux/amd64 -f "infra/docker/Dockerfile.${svc}" \
    --label "org.opencontainers.image.revision=${IMAGE_TAG}" -t "${uri}" .
  docker push "${uri}"
  ok "pushed ${uri}"
done

# ---------------------------------------------------------------- task definitions
register_td() { # name image-uri -> prints task definition ARN
  local src="${INFRA_DIR}/ecs/task-def-$1.json" dest="${WORK}/task-def-$1.json"
  IMAGE_URI="$2" render_template "${src}" "${dest}" \
    ACCOUNT_ID REGION IMAGE_URI EXECUTION_ROLE_ARN TASK_ROLE_ARN LOG_GROUP SSM_PREFIX \
    S3_BUCKET_NAME SUPABASE_URL CORS_ORIGINS SEC_EDGAR_USER_AGENT ANTHROPIC_MODEL REDIS_URL
  aws ecs register-task-definition --cli-input-json "file://${dest}" \
    --query 'taskDefinition.taskDefinitionArn' --output text
}

log "Registering task definitions"
API_TD="$(register_td api "${REGISTRY}/${API_REPO}:${IMAGE_TAG}")"
ok "${API_TD}"
WORKER_TD="$(register_td worker "${REGISTRY}/${WORKER_REPO}:${IMAGE_TAG}")"
ok "${WORKER_TD}"

# ---------------------------------------------------------------- services helpers
net_cfg() { printf '%s' "$(awsvpc_config "${SUBNETS}" "$1")"; }

capacity_args() { # spot? -> args for create-service
  if [[ "$1" == "spot" && "${USE_FARGATE_SPOT:-false}" == "true" ]]; then
    printf '%s\n' --capacity-provider-strategy capacityProvider=FARGATE_SPOT,weight=1
  else
    printf '%s\n' --launch-type FARGATE
  fi
}

# upsert_service NAME TD_ARN SG MIN% MAX% spot|ondemand [extra create-service args...]
upsert_service() {
  local name="$1" td="$2" sg="$3" min="$4" max="$5" cap="$6"
  shift 6
  if service_is_active "${name}"; then
    aws ecs update-service --cluster "${CLUSTER_NAME}" --service "${name}" --task-definition "${td}" \
      >/dev/null
    ok "${name}: updated -> ${td##*/}"
  else
    local -a cap_args
    mapfile -t cap_args < <(capacity_args "${cap}")
    aws ecs create-service --cluster "${CLUSTER_NAME}" --service-name "${name}" --task-definition "${td}" \
      --desired-count 1 "${cap_args[@]}" --platform-version LATEST \
      --network-configuration "$(net_cfg "${sg}")" \
      --deployment-configuration "deploymentCircuitBreaker={enable=true,rollback=true},maximumPercent=${max},minimumHealthyPercent=${min}" \
      --propagate-tags SERVICE --tags key=project,value=dcf "$@" >/dev/null
    ok "${name}: created with ${td##*/}"
  fi
}

# wait_rollout NAME TD_ARN: until the PRIMARY deployment runs TD_ARN with rolloutState COMPLETED.
wait_rollout() {
  local name="$1" td="$2" deadline=$((SECONDS + DEPLOY_TIMEOUT_SECONDS)) state primary_td
  while ((SECONDS < deadline)); do
    primary_td="" state=""
    read -r primary_td state < <(aws ecs describe-services --cluster "${CLUSTER_NAME}" --services "${name}" \
      --query "services[0].deployments[?status=='PRIMARY'] | [0].[taskDefinition,rolloutState]" --output text) || true
    if [[ "${primary_td}" != "${td}" ]]; then
      service_events "${name}"
      die "${name}: primary deployment is ${primary_td##*/}, not ${td##*/} (circuit breaker rolled back?)"
    fi
    case "${state}" in
      COMPLETED)
        ok "${name}: ${td##*/} is live"
        return 0
        ;;
      FAILED)
        service_events "${name}"
        die "${name}: deployment FAILED"
        ;;
    esac
    sleep 15
  done
  service_events "${name}"
  die "${name}: not stable after ${DEPLOY_TIMEOUT_SECONDS}s"
}

service_events() {
  warn "recent events for $1:"
  aws ecs describe-services --cluster "${CLUSTER_NAME}" --services "$1" \
    --query 'services[0].events[:8].[createdAt,message]' --output text >&2 || true
}

# ---------------------------------------------------------------- redis (Cloud Map: redis.dcf.internal)
if ! service_is_active "${REDIS_SERVICE}" || [[ "${DEPLOY_REDIS:-false}" == "true" ]]; then
  log "Redis service"
  NS_ID="$(cloudmap_namespace_id)"
  REDIS_REGISTRY="$(cloudmap_service_arn "${NS_ID}")"
  [[ -n "${NS_ID}" && -n "${REDIS_REGISTRY}" ]] || die "Cloud Map ${CLOUDMAP_NAMESPACE}/redis missing; run bootstrap_aws.sh"
  REDIS_TD="$(register_td redis "${REDIS_IMAGE}")"
  upsert_service "${REDIS_SERVICE}" "${REDIS_TD}" "${REDIS_SG}" 0 100 spot \
    --service-registries "registryArn=${REDIS_REGISTRY}"
  wait_rollout "${REDIS_SERVICE}" "${REDIS_TD}"
else
  ok "${REDIS_SERVICE} running; left alone (DEPLOY_REDIS=true to roll it)"
fi

# ---------------------------------------------------------------- migrations (one-off task)
if [[ "${SKIP_MIGRATIONS:-false}" == "true" ]]; then
  warn "SKIP_MIGRATIONS=true: not running alembic"
else
  log "alembic upgrade head (one-off task, api image)"
  TASK_ARN="" FAILURE="" EXIT_CODE="" STOP_REASON=""
  read -r TASK_ARN FAILURE < <(aws ecs run-task --cluster "${CLUSTER_NAME}" --task-definition "${API_TD}" \
    --launch-type FARGATE --count 1 --started-by "deploy-migrate" \
    --network-configuration "$(net_cfg "${WORKER_SG}")" \
    --overrides '{"containerOverrides":[{"name":"api","command":["alembic","upgrade","head"]}]}' \
    --query '[tasks[0].taskArn, failures[0].reason]' --output text) || true
  [[ -n "${TASK_ARN}" && "${TASK_ARN}" != "None" ]] || die "run-task failed: ${FAILURE:-see error above}"
  ok "started ${TASK_ARN##*/}; waiting for it to finish"
  aws ecs wait tasks-stopped --cluster "${CLUSTER_NAME}" --tasks "${TASK_ARN}" ||
    die "migration task ${TASK_ARN##*/} did not stop within the waiter timeout; check ${LOG_GROUP} (api/api/${TASK_ARN##*/})"
  read -r EXIT_CODE STOP_REASON < <(aws ecs describe-tasks --cluster "${CLUSTER_NAME}" --tasks "${TASK_ARN}" \
    --query "tasks[0].[containers[?name=='api'] | [0].exitCode, stoppedReason]" --output text) || true
  aws logs get-log-events --log-group-name "${LOG_GROUP}" --log-stream-name "api/api/${TASK_ARN##*/}" \
    --limit 40 --query 'events[].[message]' --output text 2>/dev/null | sed 's/^/    | /' >&2 || true
  [[ "${EXIT_CODE}" == "0" ]] || die "migration failed (exit ${EXIT_CODE}: ${STOP_REASON}); services NOT updated"
  ok "migrations applied"
fi

# ---------------------------------------------------------------- api + worker
log "Services"
upsert_service "${API_SERVICE}" "${API_TD}" "${API_SG}" 100 200 ondemand \
  --load-balancers "targetGroupArn=${TG_ARN},containerName=api,containerPort=8000" \
  --health-check-grace-period-seconds 60
upsert_service "${WORKER_SERVICE}" "${WORKER_TD}" "${WORKER_SG}" 100 200 spot

log "Waiting for rollout (graceful drain can take a few minutes: stopTimeout=120s)"
wait_rollout "${API_SERVICE}" "${API_TD}"
wait_rollout "${WORKER_SERVICE}" "${WORKER_TD}"

API_URL="http://$(alb_dns_name)"
CF="$(cloudfront_domain)"
if [[ -n "${CF}" ]]; then API_URL="https://${CF}"; fi
log "Deployed ${IMAGE_TAG}. API: ${API_URL}/api/health"
