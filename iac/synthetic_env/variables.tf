########################################
# Existing variables
########################################

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

########################################
# ECS container image
########################################

variable "image_url" {
  description = "ECR / Docker image for Chandra runtime."
  type        = string
}

########################################
# SNS alerts
########################################

variable "alert_email" {
  description = "Email address for Chandra alerts."
  type        = string
}

########################################
# ECS sizing
########################################

variable "ecs_cpu" {
  description = "Fargate CPU units."
  type        = number
  default     = 512
}

variable "ecs_memory" {
  description = "Fargate memory."
  type        = number
  default     = 1024
}

########################################
# FastAPI port
########################################

variable "container_port" {
  description = "FastAPI container port."
  type        = number
  default     = 6001
}

########################################
# EventBridge schedule
########################################

variable "schedule_expression" {
  description = "How often Chandra runs."
  type        = string
  default     = "rate(1 hour)"
}
variable "aws_region" {
  type = string
}

variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}
########################################
# SNS variables
########################################
variable "sns_email" {
  description = "Email for SNS alerts"
  type        = string
  default     = "dummy.intelligenzit@gmail.com"
}
###############
variable "slack_workspace_id" {
  type = string
}

variable "slack_channel_id" {
  type = string
}
