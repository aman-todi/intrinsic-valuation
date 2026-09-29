# Amazon Linux 2023, arm64. The AMI is resolved once; later AMI releases do not replace the instance
# (lifecycle.ignore_changes). Patch in place with `sudo dnf upgrade --releasever=latest` (docs/DEPLOYMENT.md).
data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_instance" "app" {
  ami                         = data.aws_ssm_parameter.al2023_arm64.insecure_value
  instance_type               = var.instance_type
  subnet_id                   = local.subnet_id
  vpc_security_group_ids      = [aws_security_group.app.id]
  iam_instance_profile        = aws_iam_instance_profile.app.name
  associate_public_ip_address = true # needed until the EIP attaches (SSM agent + dnf reach the internet)
  monitoring                  = false

  user_data = templatefile("${path.module}/templates/user_data.sh.tftpl", {
    aws_region      = local.region
    swap_size_gb    = var.swap_size_gb
    compose_version = var.compose_version
  })

  credit_specification {
    cpu_credits = var.cpu_credits
  }

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required" # IMDSv2 only
    # 2 hops so containers on the compose bridge network can use the instance role (boto3 -> IMDS).
    http_put_response_hop_limit = 2
    instance_metadata_tags      = "disabled"
  }

  maintenance_options {
    auto_recovery = "default"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_size_gb
    encrypted             = true
    delete_on_termination = true
    tags                  = { Name = "${local.name}-app-root" }
  }

  tags = {
    Name = "${local.name}-app"
    Role = "${local.name}-app" # the deploy role's ssm:SendCommand is scoped to this instance
  }

  lifecycle {
    # A new AL2023 AMI or a different default-subnet pick must not replace the running box.
    ignore_changes = [ami, subnet_id]
  }
}

# ---------- optional auto-heal ----------
resource "aws_cloudwatch_metric_alarm" "system_status" {
  count               = var.enable_status_check_alarms ? 1 : 0
  alarm_name          = "${local.name}-app-system-status-check"
  alarm_description   = "Recover the instance (same IDs, EIP and volume) when the underlying host fails."
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed_System"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  dimensions          = { InstanceId = aws_instance.app.id }
  alarm_actions       = ["arn:${local.partition}:automate:${local.region}:ec2:recover"]
}

resource "aws_cloudwatch_metric_alarm" "instance_status" {
  count               = var.enable_status_check_alarms ? 1 : 0
  alarm_name          = "${local.name}-app-instance-status-check"
  alarm_description   = "Reboot the instance when the OS stops responding (e.g. out of memory)."
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed_Instance"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  dimensions          = { InstanceId = aws_instance.app.id }
  alarm_actions       = ["arn:${local.partition}:automate:${local.region}:ec2:reboot"]
}
