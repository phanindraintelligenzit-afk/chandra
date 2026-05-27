resource "aws_cloudwatch_event_rule" "hourly" {
  name                = "chandra-hourly"
  schedule_expression = "rate(1 hour)"
}

resource "aws_cloudwatch_event_target" "ecs" {
  rule = aws_cloudwatch_event_rule.hourly.name
  arn  = var.cluster_arn

  ecs_target {
    task_definition_arn = var.task_definition

    launch_type = "FARGATE"

    network_configuration {
      subnets          = var.subnet_ids
      security_groups  = [var.security_group_id]
      assign_public_ip = true
    }
  }

  role_arn = null
}
