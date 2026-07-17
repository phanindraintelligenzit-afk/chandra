#########################################################
# INFERENCE GPU INSTANCE (local LLM)
#########################################################
# Uncomment and configure to deploy a GPU instance for
# local LLM inference. Set .env vars once deployed:
#
#   LLM_PROVIDER=openai
#   OPENAI_API_BASE=http://<instance-private-ip>:8000/v1
#   LLM_MODEL=Qwen/Qwen2.5-32B-Coder-Instruct
#
# Deployment options (set in terraform.tfvars or at plan time):
#
#   inference_instance_type = "g5.2xlarge"   # 1×A10G, 24GB — Qwen 32B 4-bit
#   inference_llm_provider  = "vllm"          # vllm | ollama | llama-cpp
#   inference_model_name    = "Qwen/Qwen2.5-32B-Coder-Instruct"
#
# Cost: ~$0.77/hr (g5.2xlarge on-demand). Use spot for 60-70% off.
#########################################################

# module "inference" {
#   source = "./modules/inference"
#
#   name_prefix   = "chandra"
#   ami_id        = var.inference_ami_id        # Required: Deep Learning Base AMI (Ubuntu 22.04)
#   instance_type = var.inference_instance_type
#   vpc_id        = var.vpc_id                  # Required
#   subnet_id     = var.inference_subnet_id      # Required
#   key_name      = var.key_name
#
#   # Model settings
#   llm_provider    = var.inference_llm_provider
#   model_name      = var.inference_model_name
#   huggingface_token = var.huggingface_token
#
#   # Security
#   assign_public_ip    = false
#   allowed_api_cidrs   = [var.vpc_cidr]
#   allowed_ssh_cidrs   = var.admin_ssh_cidrs
#   iam_instance_profile_name = aws_iam_instance_profile.inference.name
#
#   tags = var.tags
# }
#
# # IAM role for inference instance (read-only + model pull)
# resource "aws_iam_role" "inference" {
#   name = "chandra-inference-prod"
#
#   assume_role_policy = jsonencode({
#     Version = "2012-10-17"
#     Statement = [{
#       Effect = "Allow"
#       Principal = { Service = "ec2.amazonaws.com" }
#       Action   = "sts:AssumeRole"
#     }]
#   })
#
#   permissions_boundary = aws_iam_policy.runtime_boundary.arn
# }
#
# resource "aws_iam_instance_profile" "inference" {
#   name = "chandra-inference-prod"
#   role = aws_iam_role.inference.name
# }
#
# resource "aws_iam_role_policy_attachment" "inference_readonly" {
#   role       = aws_iam_role.inference.name
#   policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
# }