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
# PERF-001 — EC2 tagged Environment=prod outside any ASG
# ============================================================================
resource "aws_instance" "no_asg_prod" {
  ami           = data.aws_ami.al2023.id
  instance_type = "t3.micro"
  subnet_id     = tolist(data.aws_subnets.default.ids)[0]

  tags = {
    Name              = "${var.prefix}-no-asg-prod"
    Environment       = "prod"
    Owner             = "chandra-demo"
    "chandra:seed_id" = "PERF-001-no-asg"
  }
}
