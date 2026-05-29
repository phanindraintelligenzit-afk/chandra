########################################
# EVENTBRIDGE IAM ROLE
########################################

resource "aws_iam_role" "eventbridge_ecs" {

  name = "chandra-eventbridge-ecs-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"

    Statement = [
      {
        Effect = "Allow"

        Principal = {
          Service = "events.amazonaws.com"
        }

        Action = "sts:AssumeRole"
      }
    ]
  })
}

resource "aws_cloudwatch_event_rule" "hourly" {
  name                = "chandra-hourly"
  schedule_expression = "rate(1 hour)"
}

resource "aws_cloudwatch_event_target" "ecs" {
  rule = aws_cloudwatch_event_rule.hourly.name
  arn  = var.cluster_arn
  role_arn = aws_iam_role.eventbridge_ecs.arn
  ecs_target {
    task_definition_arn = var.task_definition

    launch_type = "FARGATE"

    network_configuration {
      subnets          = var.subnet_ids
      security_groups  = [var.security_group_id]
      assign_public_ip = true
    }
  }

}
resource "aws_iam_role_policy" "eventbridge_ecs_policy" {

  role = aws_iam_role.eventbridge_ecs.id

  policy = jsonencode({
    Version = "2012-10-17"

    Statement = [
      {
        Effect = "Allow"

        Action = [
          "ecs:RunTask",
          "iam:PassRole"
        ]

        Resource = "*"
      }
    ]
  })
}
