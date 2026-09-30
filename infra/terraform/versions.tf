terraform {
  required_version = ">= 1.10" # S3 backend native locking (use_lockfile)

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.7"
    }
  }

  # Partial configuration: bucket/key/region/use_lockfile come from backend.hcl
  # (printed by infra/terraform/bootstrap: `terraform output -raw backend_hcl > ../backend.hcl`).
  backend "s3" {}
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      project    = var.project
      managed-by = "terraform"
    }
  }
}
