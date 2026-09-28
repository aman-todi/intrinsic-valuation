#!/usr/bin/env bash
# One-time (and safely re-runnable) AWS setup for the DCF app (spec §11.3, §14.2, Ticket 15).
#
# Creates, only if missing: ECR repos, S3 bucket (+ public access block, lifecycle), CloudWatch log
# group, ECS cluster, security groups, Cloud Map namespace + redis service, IAM execution/task roles,
# ALB + target group + listener(s), an optional CloudFront HTTPS front door, and the GitHub OIDC
# provider + deploy role. Policies/trust documents are (re)applied on every run so edits under
# infra/iam/ take effect by re-running this script.
#
# It does NOT: create the budget alarm, put secrets into SSM (put_ssm_params.sh), build images or
# create ECS services (deploy.sh does that on its first run).
#
# Usage: infra/scripts/bootstrap_aws.sh            (config from env and/or infra/deploy.env)
# Needs: aws CLI v2 with admin-ish credentials for this account, python3, git.
set -euo pipefail

# shellcheck source=infra/scripts/lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

load_config
require_cmd aws python3
load_account

API_HTTPS_MODE="${API_HTTPS_MODE:-}"
if [[ -z "${API_HTTPS_MODE}" ]]; then
  if [[ -n "${ACM_CERT_ARN:-}" ]]; then API_HTTPS_MODE=acm; else API_HTTPS_MODE=cloudfront; fi
fi
case "${API_HTTPS_MODE}" in
  cloudfront | none) ;;
  acm) [[ -n "${ACM_CERT_ARN:-}" ]] || die "API_HTTPS_MODE=acm needs ACM_CERT_ARN" ;;
  *) die "API_HTTPS_MODE must be cloudfront, acm or none (got '${API_HTTPS_MODE}')" ;;
esac

if [[ -z "${GITHUB_REPO:-}" ]]; then
  GITHUB_REPO="$(git -C "${REPO_ROOT}" remote get-url origin 2>/dev/null |
    sed -E 's#^(https://github\.com/|git@github\.com:|ssh://git@github\.com/)##; s#\.git$##' || true)"
  [[ "${GITHUB_REPO}" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || GITHUB_REPO=""
fi
export GITHUB_REPO CLUSTER_NAME API_REPO WORKER_REPO

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

log "Account ${ACCOUNT_ID}, region ${REGION}, bucket ${S3_BUCKET_NAME}, HTTPS mode ${API_HTTPS_MODE}"

# ---------------------------------------------------------------- service-linked role
log "ECS service-linked role"
if aws iam get-role --role-name AWSServiceRoleForECS >/dev/null 2>&1; then
  ok "exists"
else
  aws iam create-service-linked-role --aws-service-name ecs.amazonaws.com >/dev/null
  ok "created"
fi

# ---------------------------------------------------------------- ECR
log "ECR repositories"
for repo in "${API_REPO}" "${WORKER_REPO}"; do
  if aws ecr describe-repositories --repository-names "${repo}" >/dev/null 2>&1; then
    ok "${repo} exists"
  else
    aws ecr create-repository --repository-name "${repo}" \
      --image-scanning-configuration scanOnPush=true --image-tag-mutability MUTABLE \
      --tags Key=project,Value=dcf >/dev/null
    ok "${repo} created"
  fi
  aws ecr put-lifecycle-policy --repository-name "${repo}" \
    --lifecycle-policy-text "file://${INFRA_DIR}/ecr-lifecycle.json" >/dev/null
done

# ---------------------------------------------------------------- S3
log "S3 bucket ${S3_BUCKET_NAME}"
if head_out="$(aws s3api head-bucket --bucket "${S3_BUCKET_NAME}" 2>&1)"; then
  ok "exists"
elif [[ "${head_out}" == *"404"* || "${head_out}" == *"Not Found"* ]]; then
  if [[ "${REGION}" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "${S3_BUCKET_NAME}" >/dev/null
  else
    aws s3api create-bucket --bucket "${S3_BUCKET_NAME}" \
      --create-bucket-configuration "LocationConstraint=${REGION}" >/dev/null
  fi
  aws s3api wait bucket-exists --bucket "${S3_BUCKET_NAME}"
  ok "created"
else
  die "bucket ${S3_BUCKET_NAME} exists but is not accessible (owned by another account?). Set S3_BUCKET_NAME. (${head_out})"
fi
aws s3api put-public-access-block --bucket "${S3_BUCKET_NAME}" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket "${S3_BUCKET_NAME}" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-bucket-lifecycle-configuration --bucket "${S3_BUCKET_NAME}" \
  --lifecycle-configuration "file://${INFRA_DIR}/s3-lifecycle.json"
ok "public access blocked, SSE-S3, lifecycle (models/ + runs/ expire after 35d)"

# ---------------------------------------------------------------- logs
log "CloudWatch log group ${LOG_GROUP}"
if [[ "$(aws logs describe-log-groups --log-group-name-prefix "${LOG_GROUP}" \
  --query "logGroups[?logGroupName=='${LOG_GROUP}'].logGroupName | [0]" --output text)" == "${LOG_GROUP}" ]]; then
  ok "exists"
else
  aws logs create-log-group --log-group-name "${LOG_GROUP}" --tags project=dcf
  ok "created"
fi
aws logs put-retention-policy --log-group-name "${LOG_GROUP}" --retention-in-days "${LOG_RETENTION_DAYS}"

# ---------------------------------------------------------------- ECS cluster
log "ECS cluster ${CLUSTER_NAME}"
if [[ "$(aws ecs describe-clusters --clusters "${CLUSTER_NAME}" \
  --query "clusters[?status=='ACTIVE'].clusterName | [0]" --output text)" == "${CLUSTER_NAME}" ]]; then
  ok "exists"
else
  aws ecs create-cluster --cluster-name "${CLUSTER_NAME}" \
    --capacity-providers FARGATE FARGATE_SPOT \
    --default-capacity-provider-strategy capacityProvider=FARGATE,weight=1 \
    --settings name=containerInsights,value=disabled \
    --tags key=project,value=dcf >/dev/null
  ok "created (Container Insights off to save cost)"
fi

# ---------------------------------------------------------------- network
log "Default VPC / subnets"
VPC_ID="$(default_vpc_id)"
read -r -a SUBNETS <<<"$(default_subnet_ids "${VPC_ID}")"
((${#SUBNETS[@]} >= 2)) || die "need at least 2 subnets in different AZs for the ALB (got ${#SUBNETS[@]})"
ok "${VPC_ID}: ${SUBNETS[*]}"

ensure_sg() { # name description -> prints id
  local id
  id="$(sg_id "$1" "${VPC_ID}")"
  if [[ -z "${id}" ]]; then
    id="$(aws ec2 create-security-group --group-name "$1" --description "$2" --vpc-id "${VPC_ID}" \
      --tag-specifications "ResourceType=security-group,Tags=[{Key=project,Value=dcf},{Key=Name,Value=$1}]" \
      --query GroupId --output text)"
    ok "$1 created (${id})"
  else
    ok "$1 exists (${id})"
  fi
  printf '%s' "${id}"
}

allow_ingress() { # sg-id aws-ec2-args...  (idempotent)
  local out
  if ! out="$(aws ec2 authorize-security-group-ingress --group-id "$1" "${@:2}" 2>&1)"; then
    [[ "${out}" == *InvalidPermission.Duplicate* ]] || die "authorize ingress on $1 failed: ${out}"
  fi
}

log "Security groups (all egress open; worker has no ingress at all)"
ALB_SG="$(ensure_sg "${ALB_SG_NAME}" "DCF ALB - HTTP(S) from the internet or CloudFront")"
API_SG="$(ensure_sg "${API_SG_NAME}" "DCF api tasks - 8000 from the ALB only")"
WORKER_SG="$(ensure_sg "${WORKER_SG_NAME}" "DCF worker + migration tasks - outbound only")"
REDIS_SG="$(ensure_sg "${REDIS_SG_NAME}" "DCF redis task - 6379 from api/worker only")"

if [[ "${API_HTTPS_MODE}" == "cloudfront" ]]; then
  CF_PREFIX_LIST="$(aws ec2 describe-managed-prefix-lists \
    --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing \
    --query 'PrefixLists[0].PrefixListId' --output text)"
  allow_ingress "${ALB_SG}" --ip-permissions \
    "IpProtocol=tcp,FromPort=80,ToPort=80,PrefixListIds=[{PrefixListId=${CF_PREFIX_LIST},Description=CloudFront}]"
  ok "ALB :80 open to CloudFront origin-facing IPs only"
else
  allow_ingress "${ALB_SG}" --protocol tcp --port 80 --cidr 0.0.0.0/0
  if [[ "${API_HTTPS_MODE}" == "acm" ]]; then
    allow_ingress "${ALB_SG}" --protocol tcp --port 443 --cidr 0.0.0.0/0
  fi
  ok "ALB :80$([[ "${API_HTTPS_MODE}" == acm ]] && echo '/:443') open to 0.0.0.0/0"
fi
allow_ingress "${API_SG}" --ip-permissions \
  "IpProtocol=tcp,FromPort=8000,ToPort=8000,UserIdGroupPairs=[{GroupId=${ALB_SG}}]"
allow_ingress "${REDIS_SG}" --ip-permissions \
  "IpProtocol=tcp,FromPort=6379,ToPort=6379,UserIdGroupPairs=[{GroupId=${API_SG}},{GroupId=${WORKER_SG}}]"
ok "api <- alb:8000, redis <- api/worker:6379"

# ---------------------------------------------------------------- Cloud Map (redis.dcf.internal)
log "Cloud Map namespace ${CLOUDMAP_NAMESPACE}"
NS_ID="$(cloudmap_namespace_id)"
if [[ -z "${NS_ID}" ]]; then
  op="$(aws servicediscovery create-private-dns-namespace --name "${CLOUDMAP_NAMESPACE}" --vpc "${VPC_ID}" \
    --tags Key=project,Value=dcf --query OperationId --output text)"
  for _ in $(seq 1 60); do
    status="$(aws servicediscovery get-operation --operation-id "${op}" --query Operation.Status --output text)"
    [[ "${status}" == "SUCCESS" ]] && break
    [[ "${status}" == "FAIL" ]] && die "Cloud Map namespace creation failed (operation ${op})"
    sleep 5
  done
  NS_ID="$(cloudmap_namespace_id)"
  [[ -n "${NS_ID}" ]] || die "Cloud Map namespace did not appear in time; re-run this script"
  ok "created (${NS_ID})"
else
  ok "exists (${NS_ID})"
fi
if [[ -z "$(cloudmap_service_arn "${NS_ID}")" ]]; then
  aws servicediscovery create-service --name "${CLOUDMAP_REDIS_SERVICE}" --namespace-id "${NS_ID}" \
    --dns-config "NamespaceId=${NS_ID},RoutingPolicy=MULTIVALUE,DnsRecords=[{Type=A,TTL=10}]" \
    --health-check-custom-config FailureThreshold=1 >/dev/null
  ok "service ${CLOUDMAP_REDIS_SERVICE}.${CLOUDMAP_NAMESPACE} created"
else
  ok "service ${CLOUDMAP_REDIS_SERVICE}.${CLOUDMAP_NAMESPACE} exists"
fi

# ---------------------------------------------------------------- IAM (ECS roles)
ensure_role() { # name trust-file description
  if aws iam get-role --role-name "$1" >/dev/null 2>&1; then
    aws iam update-assume-role-policy --role-name "$1" --policy-document "file://$2"
    ok "$1 exists (trust policy refreshed)"
  else
    aws iam create-role --role-name "$1" --assume-role-policy-document "file://$2" \
      --description "$3" --tags Key=project,Value=dcf >/dev/null
    ok "$1 created"
  fi
}

log "IAM roles for ECS tasks"
render_template "${INFRA_DIR}/iam/ecs-tasks-trust.json" "${WORK}/ecs-trust.json" REGION ACCOUNT_ID
render_template "${INFRA_DIR}/iam/execution-role-policy.json" "${WORK}/exec.json" \
  REGION ACCOUNT_ID API_REPO WORKER_REPO LOG_GROUP SSM_PREFIX
render_template "${INFRA_DIR}/iam/task-role-policy.json" "${WORK}/task.json" S3_BUCKET_NAME
ensure_role "${EXECUTION_ROLE_NAME}" "${WORK}/ecs-trust.json" "DCF ECS execution role: ECR pull, logs, SSM ${SSM_PREFIX}/*"
aws iam put-role-policy --role-name "${EXECUTION_ROLE_NAME}" --policy-name dcf-execution \
  --policy-document "file://${WORK}/exec.json"
ensure_role "${TASK_ROLE_NAME}" "${WORK}/ecs-trust.json" "DCF ECS task role: S3 on ${S3_BUCKET_NAME}"
aws iam put-role-policy --role-name "${TASK_ROLE_NAME}" --policy-name dcf-task-s3 \
  --policy-document "file://${WORK}/task.json"
ok "inline policies applied"

# ---------------------------------------------------------------- ALB + target group
log "Target group ${TARGET_GROUP_NAME}"
TG_ARN="$(target_group_arn)"
if [[ -z "${TG_ARN}" ]]; then
  TG_ARN="$(aws elbv2 create-target-group --name "${TARGET_GROUP_NAME}" --protocol HTTP --port 8000 \
    --vpc-id "${VPC_ID}" --target-type ip --health-check-protocol HTTP --health-check-path /api/health \
    --health-check-interval-seconds 15 --health-check-timeout-seconds 5 --healthy-threshold-count 2 \
    --unhealthy-threshold-count 3 --matcher HttpCode=200 --tags Key=project,Value=dcf \
    --query 'TargetGroups[0].TargetGroupArn' --output text)"
  ok "created"
else
  ok "exists"
fi
aws elbv2 modify-target-group-attributes --target-group-arn "${TG_ARN}" \
  --attributes Key=deregistration_delay.timeout_seconds,Value=60 >/dev/null

log "Application Load Balancer ${ALB_NAME}"
ALB_ARN="$(aws elbv2 describe-load-balancers --names "${ALB_NAME}" \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || true)"
if [[ -z "${ALB_ARN}" || "${ALB_ARN}" == "None" ]]; then
  ALB_ARN="$(aws elbv2 create-load-balancer --name "${ALB_NAME}" --type application \
    --scheme internet-facing --ip-address-type ipv4 --subnets "${SUBNETS[@]}" \
    --security-groups "${ALB_SG}" --tags Key=project,Value=dcf \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text)"
  ok "created (takes a few minutes to become active)"
else
  ok "exists"
fi
aws elbv2 modify-load-balancer-attributes --load-balancer-arn "${ALB_ARN}" \
  --attributes Key=idle_timeout.timeout_seconds,Value=120 >/dev/null

ensure_listener() { # port protocol default-action [extra create args...]
  local port="$1" proto="$2" action="$3" arn
  shift 3
  arn="$(aws elbv2 describe-listeners --load-balancer-arn "${ALB_ARN}" \
    --query "Listeners[?Port==\`${port}\`].ListenerArn | [0]" --output text)"
  if [[ -z "${arn}" || "${arn}" == "None" ]]; then
    aws elbv2 create-listener --load-balancer-arn "${ALB_ARN}" --port "${port}" --protocol "${proto}" \
      --default-actions "${action}" "$@" >/dev/null
    ok "listener :${port} created"
  else
    aws elbv2 modify-listener --listener-arn "${arn}" --default-actions "${action}" "$@" >/dev/null
    ok "listener :${port} updated"
  fi
}
FORWARD="Type=forward,TargetGroupArn=${TG_ARN}"
if [[ "${API_HTTPS_MODE}" == "acm" ]]; then
  ensure_listener 443 HTTPS "${FORWARD}" --certificates "CertificateArn=${ACM_CERT_ARN}" \
    --ssl-policy ELBSecurityPolicy-TLS13-1-2-2021-06
  ensure_listener 80 HTTP "Type=redirect,RedirectConfig={Protocol=HTTPS,Port=443,StatusCode=HTTP_301}"
else
  ensure_listener 80 HTTP "${FORWARD}"
fi
ALB_DNS="$(alb_dns_name)"

# ---------------------------------------------------------------- CloudFront (optional HTTPS front door)
API_URL="http://${ALB_DNS}"
if [[ "${API_HTTPS_MODE}" == "cloudfront" ]]; then
  log "CloudFront distribution (${CLOUDFRONT_COMMENT}) in front of the ALB"
  CF_DOMAIN="$(cloudfront_domain)"
  if [[ -z "${CF_DOMAIN}" ]]; then
    CALLER_REFERENCE="dcf-api-$(date +%s)"
    export CALLER_REFERENCE CLOUDFRONT_COMMENT ALB_DNS
    render_template "${INFRA_DIR}/cloudfront-api.json" "${WORK}/cf.json" CALLER_REFERENCE CLOUDFRONT_COMMENT ALB_DNS
    CF_DOMAIN="$(aws cloudfront create-distribution --distribution-config "file://${WORK}/cf.json" \
      --query 'Distribution.DomainName' --output text)"
    ok "created ${CF_DOMAIN} (global rollout takes ~5-15 min)"
  else
    ok "exists ${CF_DOMAIN}"
  fi
  API_URL="https://${CF_DOMAIN}"
elif [[ "${API_HTTPS_MODE}" == "acm" ]]; then
  API_URL="https://<your-api-domain>  (create a DNS CNAME/alias -> ${ALB_DNS})"
fi

# ---------------------------------------------------------------- GitHub OIDC deploy role
DEPLOY_ROLE_ARN=""
if [[ -n "${GITHUB_REPO}" ]]; then
  log "GitHub OIDC provider + deploy role for ${GITHUB_REPO} (main branch only)"
  OIDC_ARN="arn:aws:iam::${ACCOUNT_ID}:oidc-provider/token.actions.githubusercontent.com"
  if aws iam get-open-id-connect-provider --open-id-connect-provider-arn "${OIDC_ARN}" >/dev/null 2>&1; then
    ok "OIDC provider exists"
  else
    # AWS no longer validates this thumbprint for GitHub's IdP, but older CLIs require the argument.
    aws iam create-open-id-connect-provider --url https://token.actions.githubusercontent.com \
      --client-id-list sts.amazonaws.com --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1 >/dev/null
    ok "OIDC provider created"
  fi
  render_template "${INFRA_DIR}/iam/github-oidc-trust.json" "${WORK}/gh-trust.json" ACCOUNT_ID GITHUB_REPO
  render_template "${INFRA_DIR}/iam/github-deploy-policy.json" "${WORK}/gh-policy.json" \
    REGION ACCOUNT_ID API_REPO WORKER_REPO CLUSTER_NAME EXECUTION_ROLE_NAME TASK_ROLE_NAME LOG_GROUP
  ensure_role "${GITHUB_DEPLOY_ROLE_NAME}" "${WORK}/gh-trust.json" "GitHub Actions deploy (${GITHUB_REPO}@main)"
  aws iam put-role-policy --role-name "${GITHUB_DEPLOY_ROLE_NAME}" --policy-name dcf-deploy \
    --policy-document "file://${WORK}/gh-policy.json"
  DEPLOY_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${GITHUB_DEPLOY_ROLE_NAME}"
  ok "${DEPLOY_ROLE_ARN}"
else
  warn "GITHUB_REPO not set and not derivable from 'git remote'; skipped the GitHub OIDC deploy role"
fi

# ---------------------------------------------------------------- summary
cat >&2 <<EOF

$(printf '\033[1;32m')Bootstrap complete.$(printf '\033[0m')
  Bucket           s3://${S3_BUCKET_NAME}
  Cluster          ${CLUSTER_NAME}
  ALB              http://${ALB_DNS}
  API base URL     ${API_URL}
  Redis (internal) redis://${CLOUDMAP_REDIS_SERVICE}.${CLOUDMAP_NAMESPACE}:6379
  Deploy role      ${DEPLOY_ROLE_ARN:-<skipped>}

Still requires the account owner (see docs/DEPLOYMENT.md):
  1. Secrets -> SSM:   infra/scripts/put_ssm_params.sh --env-file .env
  2. GitHub:           secret AWS_ROLE_ARN=${DEPLOY_ROLE_ARN:-<role arn>}; variables DEPLOY_ENABLED=true,
                       AWS_REGION=${REGION}, S3_BUCKET_NAME=${S3_BUCKET_NAME}, SUPABASE_URL, CORS_ORIGINS,
                       SEC_EDGAR_USER_AGENT
  3. First deploy:     infra/scripts/deploy.sh   (or push to main once DEPLOY_ENABLED=true)
  4. Vercel:           NEXT_PUBLIC_API_BASE_URL=${API_URL%% *}
  5. Supabase Auth:    add the Vercel URL to Site URL / Redirect URLs
EOF
if [[ "${API_HTTPS_MODE}" == "acm" ]]; then
  echo "  6. DNS:            point your API hostname at ${ALB_DNS} (CNAME, or Route 53 alias)" >&2
fi
