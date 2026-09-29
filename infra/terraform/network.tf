# Default VPC, public subnet, no NAT. Only 80/443 are open (Caddy); shell access is SSM Session Manager,
# so there is no SSH port and no key pair.

data "aws_vpc" "default" {
  default = true
}

data "aws_ec2_instance_type_offerings" "available" {
  location_type = "availability-zone"
  filter {
    name   = "instance-type"
    values = [var.instance_type]
  }
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
  filter {
    name   = "availability-zone"
    values = var.availability_zone != "" ? [var.availability_zone] : sort(data.aws_ec2_instance_type_offerings.available.locations)
  }
}

data "aws_subnet" "candidates" {
  for_each = toset(data.aws_subnets.default.ids)
  id       = each.value
}

locals {
  # Deterministic pick: the default subnet of the alphabetically first eligible AZ.
  subnets_by_az = { for s in data.aws_subnet.candidates : s.availability_zone => s.id }
  subnet_id     = local.subnets_by_az[sort(keys(local.subnets_by_az))[0]]
}

resource "aws_security_group" "app" {
  name        = "${local.name}-app"
  description = "DCF app: HTTP/HTTPS to Caddy only; no SSH (use SSM Session Manager)"
  vpc_id      = data.aws_vpc.default.id
  tags        = { Name = "${local.name}-app" }
}

locals {
  ingress = {
    http_v4  = { port = 80, proto = "tcp", cidr4 = "0.0.0.0/0", cidr6 = null }
    http_v6  = { port = 80, proto = "tcp", cidr4 = null, cidr6 = "::/0" }
    https_v4 = { port = 443, proto = "tcp", cidr4 = "0.0.0.0/0", cidr6 = null }
    https_v6 = { port = 443, proto = "tcp", cidr4 = null, cidr6 = "::/0" }
    # HTTP/3 (QUIC), served by Caddy by default.
    h3_v4 = { port = 443, proto = "udp", cidr4 = "0.0.0.0/0", cidr6 = null }
    h3_v6 = { port = 443, proto = "udp", cidr4 = null, cidr6 = "::/0" }
  }
}

resource "aws_vpc_security_group_ingress_rule" "app" {
  for_each          = local.ingress
  security_group_id = aws_security_group.app.id
  description       = each.key
  ip_protocol       = each.value.proto
  from_port         = each.value.port
  to_port           = each.value.port
  cidr_ipv4         = each.value.cidr4
  cidr_ipv6         = each.value.cidr6
}

resource "aws_vpc_security_group_egress_rule" "all_v4" {
  security_group_id = aws_security_group.app.id
  description       = "all egress (IPv4)"
  ip_protocol       = "-1"
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "all_v6" {
  security_group_id = aws_security_group.app.id
  description       = "all egress (IPv6)"
  ip_protocol       = "-1"
  cidr_ipv6         = "::/0"
}

# Stable public address: the sslip.io hostname and any custom-domain A record point here.
resource "aws_eip" "app" {
  domain = "vpc"
  tags   = { Name = "${local.name}-app" }
}

resource "aws_eip_association" "app" {
  allocation_id = aws_eip.app.id
  instance_id   = aws_instance.app.id
}
