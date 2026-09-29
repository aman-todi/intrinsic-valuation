# ---------- general ----------
variable "aws_region" {
  description = "AWS region. Keep it close to the Supabase project's region."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Name prefix for every resource (ECR repos, roles, SSM path /<project>/prod)."
  type        = string
  default     = "dcf"
}

# ---------- compute ----------
variable "instance_type" {
  description = "Graviton (arm64) instance type. t4g.small (2 GB + 2 GB swap) is enough for ~20-50 users; t4g.medium (4 GB) is the upgrade path."
  type        = string
  default     = "t4g.small"

  validation {
    condition     = can(regex("^(t4g|m7g|m8g|c7g|c8g|r7g|r8g|m6g|c6g|r6g)\\.", var.instance_type))
    error_message = "Images are built for linux/arm64: pick a Graviton instance type (t4g.*, m7g.*, ...)."
  }
}

variable "cpu_credits" {
  description = "Burstable CPU credit mode: \"unlimited\" (bursts past the baseline, surplus billed at ~$0.04/vCPU-hour) or \"standard\" (throttled to the baseline once credits run out, never billed extra)."
  type        = string
  default     = "unlimited"

  validation {
    condition     = contains(["standard", "unlimited"], var.cpu_credits)
    error_message = "cpu_credits must be \"standard\" or \"unlimited\"."
  }
}

variable "root_volume_size_gb" {
  description = "gp3 root volume size (GiB). Holds the OS, Docker images (~3 GB) and the swapfile."
  type        = number
  default     = 30
}

variable "swap_size_gb" {
  description = "Swapfile created by user_data (GiB). 0 disables swap."
  type        = number
  default     = 2
}

variable "availability_zone" {
  description = "AZ for the instance (default VPC subnet). Empty = the first AZ, in name order, that offers instance_type. Changing it later replaces the instance."
  type        = string
  default     = ""
}

variable "enable_status_check_alarms" {
  description = "CloudWatch alarms that reboot the instance on a failed instance status check and recover it on a failed system status check."
  type        = bool
  default     = true
}

variable "compose_version" {
  description = "Docker Compose plugin release installed by user_data (github.com/docker/compose/releases). Only read at instance creation."
  type        = string
  default     = "v5.5.1"
}

# ---------- app / DNS ----------
variable "domain_name" {
  description = "API hostname, e.g. api.example.com (create a DNS A record to the Elastic IP first). Empty = <eip-with-dashes>.sslip.io, which gives HTTPS with no domain."
  type        = string
  default     = ""
}

variable "acme_email" {
  description = "Contact email for Let's Encrypt (expiry notices). Empty = alert_email."
  type        = string
  default     = ""
}

variable "cors_origins" {
  description = "Browser origins allowed to call the API: the Vercel production URL (and any stable preview/custom domains), exact origins with no trailing slash. Wildcards are not supported."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for o in var.cors_origins : can(regex("^https?://[^/*]+$", o))])
    error_message = "Each CORS origin must be scheme://host[:port] with no path, trailing slash or wildcard."
  }
}

variable "worker_concurrency" {
  description = "SAQ jobs per worker process. Each build can run LibreOffice (~300 MB), so keep 2 on t4g.small; 4 on t4g.medium."
  type        = number
  default     = 2
}

# ---------- storage / registry ----------
variable "s3_bucket_name" {
  description = "Artifact bucket. Empty = <project>-app-artifacts-<account-id>."
  type        = string
  default     = ""
}

variable "artifact_retention_days" {
  description = "Lifecycle expiry for the models/ and runs/ prefixes (SPEC: 35 days = cache TTL + backstop)."
  type        = number
  default     = 35
}

variable "ecr_keep_images" {
  description = "ECR lifecycle: keep this many most recent images per repository."
  type        = number
  default     = 10
}

variable "force_destroy" {
  description = "Allow `terraform destroy` to delete a non-empty artifact bucket and ECR repos with images. Set true (and apply) right before a teardown."
  type        = bool
  default     = false
}

# ---------- GitHub Actions ----------
variable "github_owner" {
  description = "GitHub user/org that owns the repository allowed to deploy."
  type        = string
}

variable "github_repo" {
  description = "Repository name allowed to deploy (without the owner)."
  type        = string
}

variable "github_branch" {
  description = "Only workflows running on this branch can assume the deploy role."
  type        = string
  default     = "main"
}

variable "github_oidc_provider_arn" {
  description = "ARN of an existing token.actions.githubusercontent.com OIDC provider in this account. Empty = create one (an account can only have one)."
  type        = string
  default     = ""
}

# ---------- cost guardrail ----------
variable "alert_email" {
  description = "Email for budget alerts (and the default ACME contact)."
  type        = string
}

variable "monthly_budget_usd" {
  description = "Monthly AWS cost budget. Alerts at 80% and 100% of actual spend and 100% of forecast."
  type        = number
  default     = 30
}
