variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
  default     = "chandra"
}

variable "ami_id" {
  description = "AMI for the inference instance. Use AWS Deep Learning Base AMI (Ubuntu 22.04) for GPU support."
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type for inference."
  type        = string
  default     = "g5.2xlarge"
  # Recommended options (by cost):
  #   g5.2xlarge  (1×A10G, 24GB)  → Qwen 32B 4-bit           ~$0.77/hr
  #   g5.4xlarge  (1×A10G, 24GB)  → Qwen 32B 4-bit + room    ~$1.20/hr
  #   g6.2xlarge  (1×L40S, 48GB)  → Qwen 32B 8-bit           ~$1.50/hr
  #   g5.12xlarge (4×A10G, 96GB)  → Llama 70B 4-bit          ~$5.67/hr
  #   p3.2xlarge  (1×V100, 16GB)  → Smaller models (14-16B)  ~$3.06/hr
  #   g4dn.xlarge (1×T4, 16GB)    → DeepSeek 16B             ~$0.53/hr
}

variable "vpc_id" {
  description = "VPC ID to deploy the inference instance into"
  type        = string
}

variable "subnet_id" {
  description = "Subnet ID for the inference instance"
  type        = string
}

variable "key_name" {
  description = "EC2 key pair name for SSH access"
  type        = string
  default     = null
}

variable "assign_public_ip" {
  description = "Assign a public IP to the inference instance"
  type        = bool
  default     = false
}

variable "root_volume_size_gb" {
  description = "Root EBS volume size in GB"
  type        = number
  default     = 50
}

variable "model_volume_size_gb" {
  description = "Additional EBS volume size in GB for model storage"
  type        = number
  default     = 200
}

variable "api_port" {
  description = "Port for the OpenAI-compatible API server"
  type        = number
  default     = 8000
}

variable "allowed_api_cidrs" {
  description = "CIDR blocks allowed to access the inference API"
  type        = list(string)
  default     = ["10.0.0.0/8"]
}

variable "allowed_source_sg_ids" {
  description = "Security group IDs allowed to access the inference API (in addition to CIDRs)"
  type        = list(string)
  default     = []
}

variable "allowed_ssh_cidrs" {
  description = "CIDR blocks allowed to SSH into the instance"
  type        = list(string)
  default     = ["10.0.0.0/8"]
}

variable "iam_instance_profile_name" {
  description = "IAM instance profile name for the inference instance"
  type        = string
  default     = null
}

variable "gpu_count" {
  description = "Number of GPUs to use (visible devices)"
  type        = number
  default     = 1
}

variable "llm_provider" {
  description = "LLM backend to install: vllm | ollama | llama-cpp"
  type        = string
  default     = "vllm"
}

variable "model_name" {
  description = "HuggingFace model ID or Ollama model name"
  type        = string
  default     = "Qwen/Qwen2.5-32B-Coder-Instruct"
}

variable "huggingface_token" {
  description = "HuggingFace token for gated models (leave empty for public models)"
  type        = string
  default     = ""
  sensitive   = true
}

variable "vllm_extra_args" {
  description = "Extra CLI arguments for vLLM server (e.g. --quantization awq --dtype half)"
  type        = string
  default     = "--quantization awq --dtype half"
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}