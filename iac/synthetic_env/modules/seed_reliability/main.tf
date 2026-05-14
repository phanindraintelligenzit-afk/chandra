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

# ============================================================================
# REL-001 — production-tagged RDS without Multi-AZ
# ============================================================================
resource "aws_db_subnet_group" "rel" {
  name       = "${var.prefix}-rel-rds"
  subnet_ids = data.aws_subnets.default.ids
}

resource "aws_db_instance" "single_az_prod" {
  identifier             = "${var.prefix}-single-az-prod"
  engine                 = "postgres"
  engine_version         = "16.3"
  instance_class         = "db.t3.micro"
  allocated_storage      = 20
  username               = "admin"
  password               = "ChandraSynth1234!"
  storage_encrypted      = true
  skip_final_snapshot    = true
  deletion_protection    = false
  publicly_accessible    = false
  db_subnet_group_name   = aws_db_subnet_group.rel.name
  apply_immediately      = true
  multi_az               = false

  tags = {
    Environment       = "prod"
    Owner             = "chandra-demo"
    "chandra:seed_id" = "REL-001-rds-single-az"
  }
}
