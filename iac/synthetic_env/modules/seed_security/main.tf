terraform {
  required_providers {
    aws  = { source = "hashicorp/aws" }
    time = { source = "hashicorp/time" }
  }
}

# ============================================================================
# SEC-001 — public S3 bucket
# ============================================================================
resource "aws_s3_bucket" "leaky" {
  bucket        = "${var.prefix}-leaky"
  force_destroy = true

  tags = {
    "chandra:seed_id" = "SEC-001-public-s3"
  }
}

resource "aws_s3_bucket_ownership_controls" "leaky" {
  bucket = aws_s3_bucket.leaky.id
  rule {
    object_ownership = "BucketOwnerPreferred"
  }
}

resource "aws_s3_bucket_public_access_block" "leaky" {
  bucket                  = aws_s3_bucket.leaky.id
  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_policy" "leaky" {
  depends_on = [aws_s3_bucket_public_access_block.leaky]
  bucket     = aws_s3_bucket.leaky.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "PublicRead"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.leaky.arn}/*"
      }
    ]
  })
}

# ============================================================================
# SEC-002 — security group with 0.0.0.0/0 on SSH attached to a t3.micro
# ============================================================================
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

resource "aws_security_group" "open_ssh" {
  name        = "${var.prefix}-open-ssh"
  description = "Chandra synth: intentionally open SSH"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "World SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    "chandra:seed_id" = "SEC-002-open-sg-ssh"
  }
}

resource "aws_instance" "ssh_box" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = "t3.micro"
  subnet_id              = tolist(data.aws_subnets.default.ids)[0]
  vpc_security_group_ids = [aws_security_group.open_ssh.id]

  tags = {
    Name              = "${var.prefix}-ssh-box"
    "chandra:seed_id" = "SEC-002-open-sg-ssh"
  }
}

# ============================================================================
# SEC-003 — IAM user with access key recorded as 120 days old
#
# Note: AWS does not let us backdate IAM access key CreateDate via the API.
# We capture an "intended creation date" via time_static and expose it as a
# tag + module output. The detector honors CHANDRA_STALE_KEY_DAYS_OVERRIDE=0
# during eval runs so the seed surfaces deterministically.
# ============================================================================
resource "time_static" "stale_key_anchor" {}

resource "aws_iam_user" "stale" {
  name = "${var.prefix}-stale-user"
  tags = {
    "chandra:seed_id"                = "SEC-003-stale-key"
    "chandra:intended_age_days"      = "120"
    "chandra:intended_creation_date" = timeadd(time_static.stale_key_anchor.rfc3339, "-2880h")
  }
}

resource "aws_iam_access_key" "stale" {
  user = aws_iam_user.stale.name
}

# ============================================================================
# SEC-004 — customer-managed policy granting Action:* on Resource:*
# ============================================================================
resource "aws_iam_policy" "wildcard" {
  name        = "${var.prefix}-wildcard"
  description = "Chandra synth: intentionally permissive"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "*"
        Resource = "*"
      }
    ]
  })

  tags = {
    "chandra:seed_id" = "SEC-004-wildcard-iam"
  }
}
