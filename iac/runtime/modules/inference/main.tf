terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
  }
}

# ── Inference GPU Instance ─────────────────────────────────────────────────
resource "aws_instance" "inference" {
  ami                         = var.ami_id
  instance_type               = var.instance_type
  subnet_id                   = var.subnet_id
  vpc_security_group_ids      = [aws_security_group.inference.id]
  associate_public_ip_address = var.assign_public_ip
  key_name                    = var.key_name

  # Root volume
  root_block_device {
    volume_size = var.root_volume_size_gb
    volume_type = "gp3"
    encrypted   = true
  }

  # Additional EBS volume for model storage (models can be 20-80 GB)
  ebs_block_device {
    device_name = "/dev/sdf"
    volume_size = var.model_volume_size_gb
    volume_type = "gp3"
    encrypted   = true
    delete_on_termination = false
  }

  # User-data script runs on first boot to set up the inference server
  user_data_base64 = base64encode(templatefile("${path.module}/user-data.sh", {
    llm_provider      = var.llm_provider
    model_name        = var.model_name
    hf_token          = var.huggingface_token
    api_port          = var.api_port
    gpu_count         = var.gpu_count
    vllm_args         = var.vllm_extra_args
  }))

  # IAM role for the instance
  iam_instance_profile = var.iam_instance_profile_name

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-inference"
  })
}

# ── Security Group ─────────────────────────────────────────────────────────
resource "aws_security_group" "inference" {
  name        = "${var.name_prefix}-inference-sg"
  description = "Security group for Chandra inference server"
  vpc_id      = var.vpc_id

  # API port — only from Chandra's VPC/security-group
  ingress {
    description     = "Inference API from Chandra backend"
    from_port       = var.api_port
    to_port         = var.api_port
    protocol        = "tcp"
    cidr_blocks     = var.allowed_api_cidrs
    security_groups = var.allowed_source_sg_ids
  }

  # SSH (bastion or limited CIDR only)
  ingress {
    description = "SSH from bastion/admins"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = var.allowed_ssh_cidrs
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = var.tags
}

# ── Elastic IP (optional) ──────────────────────────────────────────────────
resource "aws_eip" "inference" {
  count    = var.assign_public_ip ? 1 : 0
  domain   = "vpc"
  instance = aws_instance.inference.id

  tags = merge(var.tags, {
    Name = "${var.name_prefix}-inference-eip"
  })
}