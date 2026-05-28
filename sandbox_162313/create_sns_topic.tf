provider "aws" {
  region = "us-east-1"
}

resource "aws_sns_topic" "monitoring_alerts" {
  name = "monitoring-alerts"
}

resource "aws_sns_topic_policy" "monitoring_alerts_policy" {
  topic_arn = aws_sns_topic.monitoring_alerts.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowPublishToTopic"
        Effect    = "Allow"
        Principal = {
          AWS = "*"
        }
        Action    = "SNS:Publish"
        Resource  = aws_sns_topic.monitoring_alerts.arn
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = var.aws_account_id
          }
          ArnLike = {
            "aws:SourceArn" = "arn:aws:cloudwatch:*:${var.aws_account_id}:alarm:*"
          }
        }
      }
    ]
  })
}

resource "aws_sns_topic_subscription" "slack_subscription" {
  topic_arn = aws_sns_topic.monitoring_alerts.arn
  protocol  = "application"
  endpoint  = var.slack_webhook_url
}

resource "aws_sns_topic_subscription" "email_subscription" {
  topic_arn = aws_sns_topic.monitoring_alerts.arn
  protocol  = "email"
  endpoint  = var.notification_email
}

output "sns_topic_arn" {
  value = aws_sns_topic.monitoring_alerts.arn
}

variable "aws_account_id" {
  description = "AWS Account ID for policy restriction"
  type        = string
}

variable "slack_webhook_url" {
  description = "Slack webhook URL for alerts"
  type        = string
}

variable "notification_email" {
  description = "Email address for notifications"
  type        = string
}