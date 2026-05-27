data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

resource "aws_security_group" "ecs" {
  name   = "chandra-ecs-sg"
  vpc_id = data.aws_vpc.default.id

  ingress {
    from_port   = 6001
    to_port     = 6001
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

###################################################

resource "aws_ecs_cluster" "main" {
  name = var.cluster_name
}

###################################################

resource "aws_cloudwatch_log_group" "logs" {
  name = "/ecs/chandra"
}

###################################################

resource "aws_ecs_task_definition" "task" {
  family                   = "chandra"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]

  cpu    = "512"
  memory = "1024"

  execution_role_arn = var.execution_role_arn
  task_role_arn      = var.task_role_arn

  container_definitions = jsonencode([
    {
      name  = "chandra"
      image = var.image_url

      portMappings = [
        {
          containerPort = 6001
          hostPort      = 6001
        }
      ]

      environment = [
        {
          name  = "SNS_TOPIC_ARN"
          value = var.sns_topic_arn
        }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = "/ecs/chandra"
          awslogs-region        = "us-east-1"
          awslogs-stream-prefix = "ecs"
        }
      }
    }
  ])
}

###################################################

resource "aws_ecs_service" "service" {
  name            = var.service_name
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.task.arn

  desired_count = 1
  launch_type   = "FARGATE"

  network_configuration {
    subnets         = data.aws_subnets.default.ids
    security_groups = [aws_security_group.ecs.id]

    assign_public_ip = true
  }
}
