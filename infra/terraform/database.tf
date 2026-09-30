# ============================================================================
# Amazon RDS for PostgreSQL: single-AZ db.t4g.micro in the default VPC, private (no public IP),
# reachable on 5432 only from the app instance's security group. TLS is forced (rds.force_ssl = 1).
#
# The master password comes from random_password and is written, inside DATABASE_URL, to the SSM
# SecureString /<project>/prod/DATABASE_URL. It is therefore also in Terraform state: keep the state
# bucket private and encrypted (infra/terraform/bootstrap). Rotate with
#   terraform apply -replace=random_password.db   (then redeploy; docs/DEPLOYMENT.md).
# v1 uses the master user for the app; a least-privilege role is a documented follow-up.
# ============================================================================

# Every default subnet of the default VPC (a DB subnet group needs at least two AZs).
data "aws_subnets" "default_all" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

resource "aws_db_subnet_group" "main" {
  name        = "${local.name}-db"
  description = "DCF app Postgres: default VPC subnets"
  subnet_ids  = sort(data.aws_subnets.default_all.ids)
  tags        = { Name = "${local.name}-db" }
}

resource "aws_security_group" "db" {
  name        = "${local.name}-db"
  description = "DCF app Postgres: 5432 from the app instance only"
  vpc_id      = data.aws_vpc.default.id
  tags        = { Name = "${local.name}-db" }
  # No egress rules: Terraform removes AWS's default allow-all egress, and RDS needs none.
}

resource "aws_vpc_security_group_ingress_rule" "db_from_app" {
  security_group_id            = aws_security_group.db.id
  description                  = "Postgres from the app instance"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.app.id
}

locals {
  db_major_version = split(".", var.db_engine_version)[0]
  # Same AZ as the app instance: no cross-AZ data transfer charge, lowest latency.
  db_availability_zone = var.db_availability_zone != "" ? var.db_availability_zone : data.aws_subnet.candidates[local.subnet_id].availability_zone
}

resource "aws_db_parameter_group" "main" {
  name_prefix = "${local.name}-pg${local.db_major_version}-"
  family      = "postgres${local.db_major_version}"
  description = "DCF app Postgres ${local.db_major_version}: TLS required"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  lifecycle {
    create_before_destroy = true # a major-version upgrade swaps in a new family's group
  }
}

# Alphanumeric only: safe in a URL and accepted by RDS (which rejects / @ " and spaces).
resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_db_instance" "main" {
  identifier     = "${local.name}-db"
  engine         = "postgres"
  engine_version = var.db_engine_version # "17" = the region's default 17.x minor; auto minor upgrades on
  instance_class = var.db_instance_class

  db_name  = var.db_name
  username = var.db_username
  password = random_password.db.result
  port     = 5432

  allocated_storage     = var.db_allocated_storage_gb
  max_allocated_storage = var.db_max_allocated_storage_gb # storage autoscaling ceiling
  storage_type          = "gp3"
  storage_encrypted     = true # aws/rds managed key

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  parameter_group_name   = aws_db_parameter_group.main.name
  availability_zone      = local.db_availability_zone
  multi_az               = false
  publicly_accessible    = false
  network_type           = "IPV4"

  backup_retention_period  = var.db_backup_retention_days
  backup_window            = "07:00-07:30"         # UTC (~2-3 am US Eastern)
  maintenance_window       = "sun:08:00-sun:08:30" # UTC, after the backup window
  copy_tags_to_snapshot    = true
  delete_automated_backups = false # keep the automated backups (until they age out) if the DB is deleted

  auto_minor_version_upgrade = true
  # Only takes effect when db_engine_version's major changes (a deliberate edit; docs/DEPLOYMENT.md).
  allow_major_version_upgrade = true
  apply_immediately           = false # modifications wait for the maintenance window (the password does not)

  deletion_protection       = var.db_deletion_protection
  skip_final_snapshot       = false
  final_snapshot_identifier = "${local.name}-db-final"

  # No paid extras: Performance Insights, Enhanced Monitoring and log exports stay off.
  performance_insights_enabled        = false
  monitoring_interval                 = 0
  iam_database_authentication_enabled = false

  tags = { Name = "${local.name}-db" }

  lifecycle {
    # Changing the app instance's subnet pick must never move (= replace) the database.
    ignore_changes = [availability_zone]
  }
}

# The single DATABASE_URL the api/worker (and Alembic) use. SecureString, Terraform-owned (NOT under
# config/, so the GitHub deploy role cannot read it); put_ssm_params.sh never writes this name.
resource "aws_ssm_parameter" "database_url" {
  name        = "${local.ssm_prefix}/DATABASE_URL"
  description = "Postgres connection string for the api/worker (Terraform-managed; rotate via terraform)"
  type        = "SecureString"
  tier        = "Standard"
  value = format(
    "postgresql+psycopg://%s:%s@%s:%d/%s?sslmode=require",
    urlencode(aws_db_instance.main.username),
    urlencode(random_password.db.result),
    aws_db_instance.main.address,
    aws_db_instance.main.port,
    aws_db_instance.main.db_name,
  )
}
