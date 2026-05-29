terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.40"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.11"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project = "chandra"
      Owner   = "chandra-demo"
    }
  }
}

resource "random_pet" "suffix" {
  length = 2
}

locals {
  prefix = "${var.prefix}-${random_pet.suffix.id}"
}

module "seed_security" {
  source = "./modules/seed_security"
  prefix = local.prefix
  region = var.region
}

module "seed_cost" {
  source = "./modules/seed_cost"
  prefix = local.prefix
  region = var.region
}

module "seed_compliance" {
  source = "./modules/seed_compliance"
  prefix = local.prefix
  region = var.region
}

module "seed_performance" {
  source = "./modules/seed_performance"
  prefix = local.prefix
  region = var.region
}

module "seed_reliability" {
  source = "./modules/seed_reliability"
  prefix = local.prefix
  region = var.region
}
###################################
# IAM
###################################

module "iam" {
  source = "./modules/iam"
}

###################################
# SNS
###################################

########################################
# SNS MODULE
########################################

module "sns" {

  source = "./modules/sns"

  project_name = var.project_name

  alert_email = var.alert_email

  slack_workspace_id = var.slack_workspace_id

  slack_channel_id = var.slack_channel_id
}
###################################
# ECS
###################################

module "ecs" {
  source = "./modules/ecs"

  cluster_name       = "chandra-cluster"
  service_name       = "chandra-service"
  image_url          = var.image_url
  execution_role_arn = module.iam.execution_role_arn
  task_role_arn      = module.iam.task_role_arn
  sns_topic_arn      = module.sns.topic_arn
}

###################################
# EventBridge
###################################

module "eventbridge" {
  source = "./modules/eventbridge"

  cluster_arn       = module.ecs.cluster_arn
  task_definition   = module.ecs.task_definition_arn
  subnet_ids        = module.ecs.subnet_ids
  security_group_id = module.ecs.security_group_id
}
# CloudWatch Audit module
###################################

module "cloudwatch_audit" {

  source = "./modules/cloudwatch_audit"

  project_name = var.project_name

  environment = var.environment

  retention_days = 14
}
########################################
# SNS TOPICS
########################################

resource "aws_sns_topic" "critical" {
  name              = "${var.project_name}-escalation-critical"
  kms_master_key_id = "alias/aws/sns"
}

resource "aws_sns_topic" "high" {
  name              = "${var.project_name}-escalation-high"
  kms_master_key_id = "alias/aws/sns"
}

resource "aws_sns_topic" "warning" {
  name              = "${var.project_name}-escalation-warning"
  kms_master_key_id = "alias/aws/sns"
}

########################################
# EMAIL SUBSCRIPTIONS
########################################

resource "aws_sns_topic_subscription" "critical_email" {
  topic_arn = aws_sns_topic.critical.arn
  protocol  = "email"
  endpoint  = var.sns_email
}

resource "aws_sns_topic_subscription" "high_email" {
  topic_arn = aws_sns_topic.high.arn
  protocol  = "email"
  endpoint  = var.sns_email
}

resource "aws_sns_topic_subscription" "warning_email" {
  topic_arn = aws_sns_topic.warning.arn
  protocol  = "email"
  endpoint  = var.sns_email
}
