output "log_group_name" {
  value = aws_cloudwatch_log_group.chandra_actions.name
}

output "log_group_arn" {
  value = aws_cloudwatch_log_group.chandra_actions.arn
}

output "logging_policy_arn" {
  value = aws_iam_policy.cloudwatch_logging.arn
}
