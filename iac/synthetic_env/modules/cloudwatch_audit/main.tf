########################################
# CloudWatch Log Group
########################################

resource "aws_cloudwatch_log_group" "chandra_actions" {

  name = "/${var.project_name}/${var.environment}/actions"

  retention_in_days = var.retention_days

  tags = {
    Project     = var.project_name
    Environment = var.environment
  }
}

########################################
# IAM Policy for CloudWatch
########################################

resource "aws_iam_policy" "cloudwatch_logging" {

  name = "${var.project_name}-${var.environment}-cw-logging"

  description = "Allow Chandra to write logs"

  policy = jsonencode({
    Version = "2012-10-17"

    Statement = [
      {
        Effect = "Allow"

        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams"
        ]

        Resource = [
          aws_cloudwatch_log_group.chandra_actions.arn,
          "${aws_cloudwatch_log_group.chandra_actions.arn}:*"
        ]
      }
    ]
  })
}
