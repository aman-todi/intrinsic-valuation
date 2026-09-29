# Non-secret runtime/deploy config, owned by Terraform, under /<project>/prod/config/<KEY> (plain String).
# On every deploy the instance renders /opt/dcf/.env from ALL parameters under /<project>/prod
# (these plus the SecureStrings put_ssm_params.sh writes at /<project>/prod/<KEY>); the basename is the
# env var name and config/ wins on a clash. The deploy role can read config/* only (INSTANCE_ID etc.).
locals {
  ssm_config = {
    APP_DOMAIN          = local.app_domain
    PUBLIC_API_BASE_URL = local.api_url
    CORS_ORIGINS        = join(",", var.cors_origins)
    ACME_EMAIL          = local.acme_email
    AWS_REGION          = local.region
    S3_BUCKET_NAME      = aws_s3_bucket.artifacts.id
    ECR_REGISTRY        = local.ecr_registry
    INSTANCE_ID         = aws_instance.app.id
    WORKER_CONCURRENCY  = tostring(var.worker_concurrency)
  }
}

resource "aws_ssm_parameter" "config" {
  for_each = local.ssm_config
  name     = "${local.ssm_config_prefix}/${each.key}"
  type     = "String"
  tier     = "Standard"
  # SSM rejects empty values; the deploy script maps "-" back to empty (CORS_ORIGINS before Vercel exists).
  value = each.value != "" ? each.value : "-"
}
