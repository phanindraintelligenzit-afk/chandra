variable "region" {
  description = "AWS region to deploy resources in"
  type        = string
  default     = "us-east-1"
}

variable "alert_email" {
  description = "Email address to send budget alerts to"
  type        = string
}

variable "account_id" {
  description = "AWS Account ID for budget creation"
  type        = string
}