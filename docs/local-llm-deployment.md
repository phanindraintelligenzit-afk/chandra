# Local LLM Inference — Deployment Guide

This guide explains how to deploy a local LLM inference server on AWS EC2
for Chandra's Digital Worker, and switch Chandra from Amazon Bedrock
to the local model.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  AWS Account                                                     │
│                                                                  │
│  ┌──────────────────────┐     OpenAI API       ┌──────────────┐ │
│  │  Chandra Backend     │ ◄─────────────────► │  Inference    │ │
│  │  (ECS / EC2 / FastAPI)│  http://<ip>:8000/v1 │  GPU Instance │ │
│  │                      │                      │  (g5.2xlarge) │ │
│  │  LLM_PROVIDER=openai │                      │  vLLM / Ollama│ │
│  │  OPENAI_API_BASE=... │                      │  Qwen 32B Coder│ │
│  └──────────────────────┘                      └──────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

## Option A: Terraform Deployment (Recommended)

### 1. Prerequisites

- Terraform ≥ 1.5
- AWS credentials with permissions to create EC2, VPC resources
- A Deep Learning AMI ID (Ubuntu 22.04) for your region

### 2. Configure

Edit `iac/runtime/terraform.tfvars` (create if not exists):

```hcl
aws_region               = "us-east-1"
vpc_id                   = "vpc-xxxxxxxx"
vpc_cidr                 = "10.0.0.0/16"
inference_subnet_id      = "subnet-xxxxxxxx"
key_name                 = "your-ssh-key"
inference_ami_id         = "ami-xxxxxxxx"  # Deep Learning Base AMI (Ubuntu 22.04)
inference_instance_type  = "g5.2xlarge"    # 1×A10G, 24GB VRAM
inference_llm_provider   = "vllm"
inference_model_name     = "Qwen/Qwen2.5-32B-Coder-Instruct"
admin_ssh_cidrs          = ["203.0.113.0/32"]  # Your IP
tags                     = { Environment = "production" }
```

### 3. Uncomment the inference module

Edit `iac/runtime/inference.tf` and uncomment the `module "inference" { ... }` block.

### 4. Deploy

```bash
cd iac/runtime
terraform init
terraform plan
terraform apply
```

### 5. Get the API endpoint

```bash
terraform output inference_api_url
# → http://10.0.x.x:8000/v1
```

## Option B: Manual Setup (Quickstart)

### 1. Launch an EC2 GPU instance

| Setting | Value |
|---------|-------|
| AMI | **Deep Learning Base GPU AMI (Ubuntu 22.04)** — search AWS Marketplace |
| Instance type | `g5.2xlarge` (1×A10G, 24GB) — cheapest for Qwen 32B 4-bit |
| Storage | 200 GB gp3 |
| Security group | Inbound: port `8000` from Chandra's VPC CIDR, port `22` from your IP |
| IAM role | `ReadOnlyAccess` (so Chandra can assume the role to call AWS APIs) |

### 2. SSH in and run the setup script

```bash
# Copy the script to the instance
scp -i your-key.pem scripts/setup-inference.sh ubuntu@<instance-ip>:~

# SSH in
ssh -i your-key.pem ubuntu@<instance-ip>

# Make executable and run
chmod +x setup-inference.sh

# For a g5.2xlarge (24GB VRAM) with Qwen 32B 4-bit AWQ:
sudo ./setup-inference.sh \
    --provider vllm \
    --model Qwen/Qwen2.5-32B-Coder-Instruct \
    --port 8000 \
    --quant awq

# For smaller models or less VRAM, try:
# sudo ./setup-inference.sh --provider ollama --model qwen2.5-coder:14b --port 8000
```

### 3. Verify the server is running

```bash
curl http://localhost:8000/v1/models
```

## Switching Chandra to the Local LLM

Once the inference server is running, update Chandra's `.env`:

```bash
# ── LLM Provider ──────────────────────────────────────────────────
LLM_PROVIDER=openai
OPENAI_API_BASE=http://<inference-instance-private-ip>:8000/v1
OPENAI_API_KEY=not-needed-local
LLM_MODEL=Qwen/Qwen2.5-32B-Coder-Instruct
LLM_TEMPERATURE=0.0

# ── Old Bedrock config (no longer needed, kept for reference) ──────
# LLM_PROVIDER=bedrock
# LLM_MODEL=anthropic.claude-sonnet-4-5-20250929-v1:0
```

**Important:** The inference instance is inside your VPC. Use its **private IP**
in `OPENAI_API_BASE` so traffic stays within AWS — no data leaves your VPC.

## Model Recommendations

| Model | VRAM | Quality | Speed | Instance |
|-------|------|---------|-------|----------|
| **Qwen 2.5-32B-Coder-Instruct (4-bit)** | ~18 GB | ★★★★ | ★★★ | g5.2xlarge ($0.77/hr) |
| **Qwen 2.5-32B-Coder-Instruct (8-bit)** | ~32 GB | ★★★★★ | ★★ | g6.2xlarge ($1.50/hr) |
| **DeepSeek-Coder-V2-Lite (16B)** | ~12 GB | ★★★ | ★★★★ | g4dn.xlarge ($0.53/hr) |
| **Llama 3.1-70B (4-bit)** | ~40 GB | ★★★★★ | ★★ | g5.12xlarge ($5.67/hr) |
| **Qwen 2.5-14B (Q4)** | ~9 GB | ★★★ | ★★★★★ | g4dn.xlarge ($0.53/hr) |

## Cost Optimization

- **Use spot instances:** 60-70% cheaper than on-demand
  - `g5.2xlarge` spot: ~$0.23/hr → ~$165/month
  - `g4dn.xlarge` spot: ~$0.16/hr → ~$115/month
- **Stop when not in use:** Save the instance in a stopped state between runs
- **Auto-scaling:** For production, consider using a SageMaker endpoint or
  ECS with GPU tasks instead of a persistent EC2 instance

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `CUDA out of memory` | Model too large for VRAM | Use 4-bit quantization or smaller model |
| `Connection refused` | Server not started | `ssh` into instance, check `journalctl -u vllm` |
| `Model not found` | Wrong model name | Verify model exists on HuggingFace |
| Slow responses | CPU fallback, no GPU | Check `nvidia-smi` — drivers may be missing |
| Empty responses | Context window exceeded | Reduce `--max-model-len` or input size |