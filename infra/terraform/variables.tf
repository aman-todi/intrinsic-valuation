# ---------- general ----------
variable "aws_region" {
  description = "AWS region for everything (EC2, RDS, Cognito, S3, ECR, SSM)."
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

variable "frontend_origins" {
  description = "The frontend's browser origins (Vercel production URL, stable preview aliases, custom domain): exact scheme://host[:port], no path or trailing slash, no wildcards. Single source of truth for the API's CORS_ORIGINS and the Cognito app client's callback (<origin>/auth/callback) and sign-out (<origin>/) URLs. Cognito requires https except for localhost."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for o in var.frontend_origins : can(regex("^(https://[^/*]+|http://localhost(:[0-9]+)?)$", o))])
    error_message = "Each frontend origin must be https://host[:port] (or http://localhost[:port]) with no path, trailing slash or wildcard."
  }
}

variable "worker_concurrency" {
  description = "SAQ jobs per worker process. Each build can run LibreOffice (~300 MB), so keep 2 on t4g.small; 4 on t4g.medium."
  type        = number
  default     = 2
}

# ---------- auth (Cognito) ----------
variable "cognito_allow_self_signup" {
  description = "false (default) = invite-only: users are created with admin-create-user. true = anyone can sign up on the managed login page."
  type        = bool
  default     = false
}

variable "cognito_email_otp_enabled" {
  description = "Offer passwordless email one-time codes as a first sign-in factor next to the password (choice-based sign-in, Essentials tier). Each OTP sign-in sends an email, which counts toward the 50 emails/day limit of the default Cognito sender."
  type        = bool
  default     = true
}

variable "cognito_localhost_callbacks" {
  description = "Also allow http://localhost:3000/auth/callback and http://localhost:3000/ on the app client, so a local frontend can sign in against the production pool."
  type        = bool
  default     = true
}

variable "cognito_domain_prefix" {
  description = "Managed login domain prefix (<prefix>.auth.<region>.amazoncognito.com); lowercase letters, digits and hyphens, and must not contain aws, amazon or cognito. Empty = <project>-<random hex>. Changing it replaces the domain (update NEXT_PUBLIC_COGNITO_DOMAIN)."
  type        = string
  default     = ""

  validation {
    condition     = var.cognito_domain_prefix == "" || can(regex("^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", var.cognito_domain_prefix))
    error_message = "cognito_domain_prefix must be lowercase letters, digits and hyphens (1-63 chars, no leading/trailing hyphen)."
  }
}

variable "cognito_deletion_protection" {
  description = "Block deleting the user pool (set false and apply before a teardown)."
  type        = bool
  default     = true
}

# ---------- database (RDS PostgreSQL) ----------
variable "db_engine_version" {
  description = "RDS PostgreSQL version. A major version (\"17\") picks the region's default minor and lets auto minor upgrades move it; a full version (\"17.6\") pins it. Major upgrades are manual (docs/DEPLOYMENT.md)."
  type        = string
  default     = "17"
}

variable "db_instance_class" {
  description = "RDS instance class. db.t4g.micro (2 vCPU burstable, 1 GB) is enough for v1; db.t4g.small (2 GB) is the upgrade path."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage_gb" {
  description = "Initial gp3 storage (GiB); 20 is the RDS minimum."
  type        = number
  default     = 20
}

variable "db_max_allocated_storage_gb" {
  description = "Storage autoscaling ceiling (GiB). Storage can grow but never shrink."
  type        = number
  default     = 50
}

variable "db_name" {
  description = "Database created in the instance."
  type        = string
  default     = "dcf"
}

variable "db_username" {
  description = "Master user; v1's app connects as this user."
  type        = string
  default     = "dcf_app"
}

variable "db_backup_retention_days" {
  description = "Automated backup (point-in-time restore) retention in days, 1-35."
  type        = number
  default     = 7
}

variable "db_availability_zone" {
  description = "AZ for the DB instance. Empty = the app instance's AZ (no cross-AZ traffic). Only read at creation."
  type        = string
  default     = ""
}

variable "db_deletion_protection" {
  description = "Block deleting the DB instance (set false and apply before a teardown). A final snapshot is taken on delete regardless."
  type        = bool
  default     = true
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
  default     = 45
}
