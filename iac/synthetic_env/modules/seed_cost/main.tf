terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["al2023-ami-*-x86_64"]
  }
}

# ============================================================================
# COST-001 — t3.small running with no workload
# ============================================================================
resource "aws_instance" "idle" {
  ami           = data.aws_ami.al2023.id
  instance_type = "t3.small"
  subnet_id     = tolist(data.aws_subnets.default.ids)[0]

  tags = {
    Name              = "${var.prefix}-idle"
    "chandra:seed_id" = "COST-001-idle-ec2"
  }
}

# ============================================================================
# COST-002 — unattached gp3 volume
# ============================================================================
data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_ebs_volume" "orphan" {
  availability_zone = data.aws_availability_zones.available.names[0]
  size              = 8
  type              = "gp3"

  tags = {
    Name              = "${var.prefix}-orphan"
    "chandra:seed_id" = "COST-002-unattached-ebs"
  }
}
