########################################
# Current AWS account
########################################

data "aws_caller_identity" "current" {}

########################################
# Existing seed outputs
########################################

output "seeds" {
  description = "Map of detector_id -> resource ARN. Consumed by eval harness."

  value = merge(
    module.seed_security.seeds,
    module.seed_cost.seeds,
    module.seed_compliance.seeds,
    module.seed_performance.seeds,
    module.seed_reliability.seeds,
  )
}

output "region" {
  description = "AWS region"

  value = var.region
}

output "account_id" {
  description = "AWS account id"

  value = data.aws_caller_identity.current.account_id
}

########################################
# NEW runtime outputs
########################################

output "prefix" {
  description = "Generated Chandra resource prefix"

  value = local.prefix
}

########################################
# ECS
########################################

output "ecs_cluster_name" {
  description = "Chandra ECS cluster"

  value = module.ecs.cluster_name
}

output "ecs_cluster_arn" {
  description = "Cluster ARN"

  value = module.ecs.cluster_arn
}

output "ecs_task_definition_arn" {
  description = "Task definition"

  value = module.ecs.task_definition_arn
}

output "ecs_security_group_id" {
  description = "Security group"

  value = module.ecs.security_group_id
}

output "ecs_subnet_ids" {
  description = "Subnets"

  value = module.ecs.subnet_ids
}

########################################
# SNS
########################################

output "sns_topic_arn" {
  description = "SNS topic"

  value = module.sns.topic_arn
}

########################################
# IAM
########################################

output "task_role_arn" {
  description = "Task role"

  value = module.iam.task_role_arn
}

output "execution_role_arn" {
  description = "Execution role"

  value = module.iam.execution_role_arn
}
