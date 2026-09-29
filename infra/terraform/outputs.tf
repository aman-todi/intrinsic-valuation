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
  description = "GitHub secret AWS_DEPLOY_ROLE_ARN."
  value       = aws_iam_role.github_deploy.arn
}

output "ssm_prefix" {
  description = "put_ssm_params.sh writes the secrets here."
  value       = local.ssm_prefix
}

output "cors_origins" {
  value = var.cors_origins
}
