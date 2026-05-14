variable "region" {
  description = "AWS region where the synthetic env is created."
  type        = string
  default     = "us-east-1"
}

variable "prefix" {
  description = "Naming prefix for synthetic env resources."
  type        = string
  default     = "chandra-synth"
}
