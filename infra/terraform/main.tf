# DCF valuation app, production: one Graviton EC2 instance running docker compose (caddy, api, worker,
# redis) behind an Elastic IP, a private RDS PostgreSQL instance, and a Cognito user pool with managed
# login. The frontend is on Vercel; artifacts are in S3; images in ECR. Runtime config lives in SSM
# Parameter Store: Terraform writes the non-secret config/* values and the DATABASE_URL SecureString
# (its password is therefore in Terraform state, which is why the state bucket is private + encrypted);
# the owner's third-party API keys are written out-of-band by infra/scripts/put_ssm_params.sh and never
# enter state. See docs/DEPLOYMENT.md.
#
# Files: network.tf (SG, EIP), compute.tf (instance, alarms), database.tf (RDS, DB SG, DATABASE_URL),
# auth.tf (Cognito), storage.tf (S3, ECR), iam.tf (instance role, GitHub OIDC deploy role), ssm.tf
# (non-secret runtime config), budget.tf, outputs.tf.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  partition  = data.aws_partition.current.partition

  name       = var.project
  ssm_prefix = "/${var.project}/prod"
  # Non-secret, Terraform-owned values; the instance renders everything under ssm_prefix into .env.
  ssm_config_prefix = "${local.ssm_prefix}/config"

  bucket_name  = var.s3_bucket_name != "" ? var.s3_bucket_name : "${var.project}-app-artifacts-${local.account_id}"
  ecr_registry = "${local.account_id}.dkr.ecr.${local.region}.amazonaws.com"
  ecr_repos    = ["api", "worker"]

  # sslip.io resolves 1-2-3-4.sslip.io to 1.2.3.4, so Caddy can get a Let's Encrypt cert without a domain.
  app_domain = var.domain_name != "" ? var.domain_name : "${replace(aws_eip.app.public_ip, ".", "-")}.sslip.io"
  api_url    = "https://${local.app_domain}"
  acme_email = var.acme_email != "" ? var.acme_email : var.alert_email
}
