output "api_url" {
  description = "Public API base URL. Use it as NEXT_PUBLIC_API_BASE_URL in Vercel."
  value       = local.api_url
}

output "api_domain" {
  value = local.app_domain
}

output "elastic_ip" {
  description = "Point a custom domain's A record here, then set domain_name."
  value       = aws_eip.app.public_ip
}

output "instance_id" {
  value = aws_instance.app.id
}

output "ssm_session_command" {
  value = "aws ssm start-session --region ${local.region} --target ${aws_instance.app.id}"
}

output "ecr_repository_urls" {
  value = { for k, r in aws_ecr_repository.app : k => r.repository_url }
}

output "s3_bucket" {
  value = aws_s3_bucket.artifacts.id
}

output "github_deploy_role_arn" {
  description = "GitHub secret AWS_DEPLOY_ROLE_ARN (github_deploy_auth = \"oidc\" only)."
  value       = local.github_oidc ? aws_iam_role.github_deploy[0].arn : null
}

output "github_deploy_user" {
  description = "IAM user whose access key goes into the GitHub secrets AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (github_deploy_auth = \"access_key\" only)."
  value       = local.github_oidc ? null : aws_iam_user.github_deploy[0].name
}

output "ssm_prefix" {
  description = "put_ssm_params.sh writes the owner's secrets here (DATABASE_URL is Terraform's)."
  value       = local.ssm_prefix
}

output "frontend_origins" {
  description = "CORS_ORIGINS of the API and the Cognito callback/sign-out origins."
  value       = var.frontend_origins
}

# ---------- Cognito (Vercel env vars; none of these are secret) ----------
output "cognito_domain_url" {
  description = "NEXT_PUBLIC_COGNITO_DOMAIN (managed login base URL)."
  value       = local.cognito_domain_url
}

output "cognito_client_id" {
  description = "NEXT_PUBLIC_COGNITO_CLIENT_ID (public app client, no secret)."
  value       = aws_cognito_user_pool_client.web.id
}

output "cognito_user_pool_id" {
  description = "NEXT_PUBLIC_COGNITO_USER_POOL_ID; also the --user-pool-id for aws cognito-idp admin-create-user."
  value       = aws_cognito_user_pool.main.id
}

output "cognito_region" {
  description = "NEXT_PUBLIC_COGNITO_REGION."
  value       = local.region
}

output "cognito_callback_urls" {
  value = local.cognito_callback_urls
}

output "vercel_env" {
  description = "Every NEXT_PUBLIC_* value Vercel needs (Production + Preview)."
  value = {
    NEXT_PUBLIC_API_BASE_URL         = local.api_url
    NEXT_PUBLIC_COGNITO_DOMAIN       = local.cognito_domain_url
    NEXT_PUBLIC_COGNITO_CLIENT_ID    = aws_cognito_user_pool_client.web.id
    NEXT_PUBLIC_COGNITO_USER_POOL_ID = aws_cognito_user_pool.main.id
    NEXT_PUBLIC_COGNITO_REGION       = local.region
  }
}

# ---------- RDS ----------
output "rds_endpoint" {
  description = "Postgres hostname (private; reach it through the instance with SSM port forwarding)."
  value       = aws_db_instance.main.address
}

output "rds_instance_id" {
  value = aws_db_instance.main.identifier
}

output "database_url_ssm_parameter" {
  description = "SecureString holding DATABASE_URL (Terraform-managed)."
  value       = aws_ssm_parameter.database_url.name
}

output "db_port_forward_command" {
  description = "Forward localhost:15432 to the database through the app instance (needs the Session Manager plugin)."
  value       = "aws ssm start-session --region ${local.region} --target ${aws_instance.app.id} --document-name AWS-StartPortForwardingSessionToRemoteHost --parameters host=${aws_db_instance.main.address},portNumber=5432,localPortNumber=15432"
}
