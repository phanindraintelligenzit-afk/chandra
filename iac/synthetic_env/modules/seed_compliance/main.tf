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
# COMP-001 — RDS db.t3.micro with storage_encrypted = false
# ============================================================================
resource "aws_db_subnet_group" "synth" {
  name       = "${var.prefix}-rds"
  subnet_ids = data.aws_subnets.default.ids

  tags = {
    "chandra:seed_id" = "COMP-001-rds-unencrypted"
  }
}

resource "aws_db_instance" "unencrypted" {
  identifier             = "${var.prefix}-unencrypted"
  engine                 = "postgres"
  engine_version         = "16.3"
  instance_class         = "db.t3.micro"
  allocated_storage      = 20
  username               = "chandra"
  password               = "ChandraSynth1234!"
  storage_encrypted      = false
  skip_final_snapshot    = true
  deletion_protection    = false
  publicly_accessible    = false
  db_subnet_group_name   = aws_db_subnet_group.synth.name
  apply_immediately      = true
  multi_az               = false

  tags = {
    "chandra:seed_id" = "COMP-001-rds-unencrypted"
  }
}

# ============================================================================
# COMP-002 — guarantee no compliant CloudTrail exists in this region
#
# We do NOT create one; existing trails (if any) are left alone since
# destroying a real-world trail is destructive. The detector will report
# "no compliant trail" iff the account genuinely has none — which is the
# correct synthetic-env state for an empty burner account.
# ============================================================================
