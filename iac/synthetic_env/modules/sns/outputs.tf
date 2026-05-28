output "topic_arn" {
  value = aws_sns_topic.alerts.arn
}
output "critical_topic_arn" {
  value = aws_sns_topic.critical.arn
}

output "high_topic_arn" {
  value = aws_sns_topic.high.arn
}

output "warning_topic_arn" {
  value = aws_sns_topic.warning.arn
}
