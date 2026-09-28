# shellcheck shell=bash disable=SC2034  # names defined here are used by the sourcing scripts
# Shared helpers + naming for bootstrap_aws.sh / deploy.sh / put_ssm_params.sh (spec §11.3, §14.2).
# Sourced, not executed. Every resource name lives here so the scripts can't drift apart.

INFRA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "${INFRA_DIR}/.." && pwd)"

# ---------- names (change here, nowhere else) ----------
PROJECT="dcf"
CLUSTER_NAME="${PROJECT}-cluster"
API_SERVICE="${PROJECT}-api"
WORKER_SERVICE="${PROJECT}-worker"
REDIS_SERVICE="${PROJECT}-redis"
API_REPO="${PROJECT}-api"
WORKER_REPO="${PROJECT}-worker"
LOG_GROUP="/ecs/${PROJECT}"
LOG_RETENTION_DAYS=30
EXECUTION_ROLE_NAME="${PROJECT}-ecs-execution"
TASK_ROLE_NAME="${PROJECT}-ecs-task"
GITHUB_DEPLOY_ROLE_NAME="${PROJECT}-github-deploy"
ALB_NAME="${PROJECT}-alb"
TARGET_GROUP_NAME="${PROJECT}-api-tg"
ALB_SG_NAME="${PROJECT}-alb-sg"
API_SG_NAME="${PROJECT}-api-sg"
WORKER_SG_NAME="${PROJECT}-worker-sg"
REDIS_SG_NAME="${PROJECT}-redis-sg"
CLOUDMAP_NAMESPACE="${PROJECT}.internal"
CLOUDMAP_REDIS_SERVICE="redis"
CLOUDFRONT_COMMENT="${PROJECT}-api"
SSM_PREFIX="/${PROJECT}/prod"

# Keys from .env.example that are injected as ECS `secrets` (SSM SecureString under ${SSM_PREFIX}/).
# Keep in sync with the `secrets` blocks in infra/ecs/task-def-{api,worker}.json.
# AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY are deliberately absent: ECS tasks use the IAM task role.
SSM_SECRET_KEYS=(
  SUPABASE_ANON_KEY
  SUPABASE_SERVICE_ROLE_KEY
  DATABASE_URL
  DATABASE_POOLER_URL
  ANTHROPIC_API_KEY
  FRED_API_KEY
)

# Fargate is not offered in every AZ (notably use1-az3). Subnets in these AZ *IDs* are skipped.
FARGATE_EXCLUDED_AZ_IDS="${FARGATE_EXCLUDED_AZ_IDS:-use1-az3}"

# ---------- logging ----------
log() { printf '\033[1;34m==>\033[0m %s\n' "$*" >&2; }
ok() { printf '    \033[32mok\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die() {
  printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  local c
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || die "'$c' is required but not on PATH"
  done
}

# ---------- config ----------
# Optional, git-ignored infra/deploy.env (see infra/deploy.env.example). In CI the same variables
# come from GitHub repository variables instead. Values in the file take precedence.
load_config() {
  if [[ -f "${INFRA_DIR}/deploy.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${INFRA_DIR}/deploy.env"
    set +a
  fi
  AWS_REGION="${AWS_REGION:-us-east-1}"
  export AWS_REGION AWS_DEFAULT_REGION="${AWS_REGION}"
  export AWS_PAGER=""
  REGION="${AWS_REGION}"
  export REGION
}

# Resolves ACCOUNT_ID and everything derived from it. Needs working AWS credentials.
load_account() {
  ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)" ||
    die "AWS credentials not working (aws sts get-caller-identity failed)"
  S3_BUCKET_NAME="${S3_BUCKET_NAME:-dcf-app-artifacts-${ACCOUNT_ID}}"
  REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
  EXECUTION_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${EXECUTION_ROLE_NAME}"
  TASK_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${TASK_ROLE_NAME}"
  export ACCOUNT_ID S3_BUCKET_NAME REGISTRY EXECUTION_ROLE_ARN TASK_ROLE_ARN LOG_GROUP SSM_PREFIX \
    CLUSTER_NAME API_REPO WORKER_REPO EXECUTION_ROLE_NAME TASK_ROLE_NAME
}

# ---------- templates ----------
# render_template SRC DEST VAR...  — substitutes only the listed ${VAR}s (envsubst with an explicit
# SHELL-FORMAT, or a python fallback), then refuses output with unresolved ${...} or invalid JSON.
render_template() {
  local src="$1" dest="$2"
  shift 2
  local v fmt=""
  for v in "$@"; do
    [[ -n "${!v+x}" ]] || die "render_template: \$${v} is not set (needed by $(basename "${src}"))"
    export "${v?}"
    fmt+="\${${v}} "
  done
  if command -v envsubst >/dev/null 2>&1; then
    envsubst "${fmt}" <"${src}" >"${dest}"
  else
    python3 - "${src}" "$@" >"${dest}" <<'PY'
import os, re, sys
src, names = sys.argv[1], set(sys.argv[2:])
text = open(src, encoding="utf-8").read()
sys.stdout.write(re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
                        lambda m: os.environ[m.group(1)] if m.group(1) in names else m.group(0), text))
PY
  fi
  local placeholder_re='\$\{[A-Za-z_][A-Za-z0-9_]*\}'
  if grep -Eq "${placeholder_re}" "${dest}"; then
    die "unresolved placeholders in rendered $(basename "${src}"): $(grep -Eo "${placeholder_re}" "${dest}" | sort -u | tr '\n' ' ')"
  fi
  if [[ "${dest}" == *.json ]]; then
    python3 -m json.tool "${dest}" >/dev/null ||
      die "rendered $(basename "${src}") is not valid JSON (a value probably contains a quote or backslash)"
  fi
}

# ---------- network lookups (default VPC, per the no-NAT decision in §11.3) ----------
default_vpc_id() {
  local id
  id="$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)"
  [[ -n "${id}" && "${id}" != "None" ]] ||
    die "no default VPC in ${REGION}. Create one with 'aws ec2 create-default-vpc' and re-run."
  printf '%s' "${id}"
}

# Space-separated default-for-AZ subnet ids in the default VPC, minus FARGATE_EXCLUDED_AZ_IDS, capped
# at SUBNET_COUNT (default 2: the ALB's minimum; every extra AZ is another billed public IPv4 on the
# ALB). ALB and tasks use the same list so api targets are always in an ALB-enabled AZ.
# SUBNET_IDS (comma- or space-separated) overrides. Must return the same list on every run.
default_subnet_ids() {
  if [[ -n "${SUBNET_IDS:-}" ]]; then
    printf '%s' "${SUBNET_IDS//,/ }"
    return
  fi
  local vpc="$1" out="" id az n=0
  while read -r az id; do
    [[ -z "${id}" ]] && continue
    [[ " ${FARGATE_EXCLUDED_AZ_IDS//,/ } " == *" ${az} "* ]] && continue
    ((n < ${SUBNET_COUNT:-2})) || break
    out+="${id} "
    n=$((n + 1))
  done < <(aws ec2 describe-subnets \
    --filters "Name=vpc-id,Values=${vpc}" Name=default-for-az,Values=true \
    --query 'Subnets[].[AvailabilityZoneId,SubnetId]' --output text | sort)
  [[ -n "${out}" ]] || die "no default subnets found in ${vpc}"
  printf '%s' "${out% }"
}

# sg_id NAME VPC_ID -> group id or empty
sg_id() {
  local id
  id="$(aws ec2 describe-security-groups \
    --filters "Name=group-name,Values=$1" "Name=vpc-id,Values=$2" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)"
  [[ "${id}" == "None" ]] && id=""
  printf '%s' "${id}"
}

# awsvpc shorthand for --network-configuration. $1 = space-separated subnets, $2 = sg id
awsvpc_config() {
  local subnets
  subnets="$(tr ' ' ',' <<<"$1")"
  printf 'awsvpcConfiguration={subnets=[%s],securityGroups=[%s],assignPublicIp=ENABLED}' "${subnets}" "$2"
}

cloudmap_namespace_id() {
  local id
  id="$(aws servicediscovery list-namespaces \
    --query "Namespaces[?Name=='${CLOUDMAP_NAMESPACE}'].Id | [0]" --output text 2>/dev/null || true)"
  [[ "${id}" == "None" ]] && id=""
  printf '%s' "${id}"
}

cloudmap_service_arn() {
  local ns="$1" arn
  arn="$(aws servicediscovery list-services \
    --filters "Name=NAMESPACE_ID,Values=${ns},Condition=EQ" \
    --query "Services[?Name=='${CLOUDMAP_REDIS_SERVICE}'].Arn | [0]" --output text 2>/dev/null || true)"
  [[ "${arn}" == "None" ]] && arn=""
  printf '%s' "${arn}"
}

target_group_arn() {
  local arn
  arn="$(aws elbv2 describe-target-groups --names "${TARGET_GROUP_NAME}" \
    --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || true)"
  [[ "${arn}" == "None" ]] && arn=""
  printf '%s' "${arn}"
}

alb_dns_name() {
  local dns
  dns="$(aws elbv2 describe-load-balancers --names "${ALB_NAME}" \
    --query 'LoadBalancers[0].DNSName' --output text 2>/dev/null || true)"
  [[ "${dns}" == "None" ]] && dns=""
  printf '%s' "${dns}"
}

cloudfront_domain() {
  local d
  d="$(aws cloudfront list-distributions \
    --query "DistributionList.Items[?Comment=='${CLOUDFRONT_COMMENT}'].DomainName | [0]" \
    --output text 2>/dev/null || true)"
  [[ "${d}" == "None" ]] && d=""
  printf '%s' "${d}"
}

service_is_active() {
  local s
  s="$(aws ecs describe-services --cluster "${CLUSTER_NAME}" --services "$1" \
    --query "services[?status=='ACTIVE'].serviceName | [0]" --output text 2>/dev/null || true)"
  [[ -n "${s}" && "${s}" != "None" ]]
}
