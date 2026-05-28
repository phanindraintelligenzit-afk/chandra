####################################
# SNS Topics
####################################

resource "aws_sns_topic" "critical" {
  name = "${var.project_name}-escalation-critical"
}

resource "aws_sns_topic" "high" {
  name = "${var.project_name}-escalation-high"
}

resource "aws_sns_topic" "warning" {
  name = "${var.project_name}-escalation-warning"
}
####################################
# Email Subscription
####################################

resource "aws_sns_topic_subscription" "critical_email" {
  topic_arn = aws_sns_topic.critical.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_sns_topic_subscription" "high_email" {
  topic_arn = aws_sns_topic.high.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_sns_topic_subscription" "warning_email" {
  topic_arn = aws_sns_topic.warning.arn
  protocol  = "email"
  endpoint  = var.alert_email
}
########################################
# CHATBOT IAM ROLE
########################################

resource "aws_iam_role" "chatbot_role" {

  name = "chandra-chatbot-role"

  assume_role_policy = jsonencode({

    Version = "2012-10-17"

    Statement = [

      {

        Effect = "Allow"

        Principal = {
          Service = "chatbot.amazonaws.com"
        }

        Action = "sts:AssumeRole"

      }

    ]
  })
}
########################################
# CHATBOT POLICY
########################################

resource "aws_iam_role_policy" "chatbot_policy" {

  role = aws_iam_role.chatbot_role.id

  policy = jsonencode({

    Version = "2012-10-17"

    Statement = [

      {

        Effect = "Allow"

        Action = [

          "sns:Publish",
          "sns:Subscribe",
          "sns:GetTopicAttributes"

        ]

        Resource = "*"

      }

    ]
  })
}
########################################
# AWS CHATBOT SLACK
########################################

resource "aws_chatbot_slack_channel_configuration" "alerts" {

  configuration_name = "chandra-alerts"

  iam_role_arn = aws_iam_role.chatbot_role.arn

  slack_workspace_id = var.slack_workspace_id

  slack_channel_id = var.slack_channel_id

  sns_topic_arns = [

    aws_sns_topic.critical.arn,
    aws_sns_topic.high.arn,
    aws_sns_topic.warning.arn

  ]
}
