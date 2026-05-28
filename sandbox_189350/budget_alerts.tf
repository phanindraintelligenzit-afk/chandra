provider "aws" {
  region = var.aws_region
}

resource "aws_sns_topic" "budget_alerts" {
  name = "budget-alerts-topic"
}

resource "aws_sns_topic_subscription" "email_alert" {
  topic_arn = aws_sns_topic.budget_alerts.arn
  protocol  = "email"
  endpoint  = var.notification_email
}

resource "aws_budgets_budget" "daily_cost" {
  name              = "daily-cost-budget"
  budget_type       = "COST"
  limit_amount      = var.daily_budget_limit
  limit_unit        = "USD"
  time_period_start = "0001-01-01_00:00"

  time_unit = "DAILY"

  notification {
    notification_type    = "ACTUAL"
    comparison_operator  = "GREATER_THAN"
    threshold            = 80
    threshold_type       = "PERCENTAGE"
    subscribers        = [aws_sns_topic.budget_alerts.arn]
  }

  notification {
    notification_type    = "FORECASTED"
    comparison_operator  = "GREATER_THAN"
    threshold            = 90
    threshold_type       = "PERCENTAGE"
    subscribers        = [aws_sns_topic.budget_alerts.arn]
  }
}

resource "aws_budgets_budget" "monthly_cost" {
  name              = "monthly-cost-budget"
  budget_type       = "COST"
  limit_amount      = var.monthly_budget_limit
  limit_unit        = "USD"
  time_period_start = "0001-01-01_00:00"

  time_unit = "MONTHLY"

  notification {
    notification_type    = "ACTUAL"
    comparison_operator  = "GREATER_THAN"
    threshold            = 85
    threshold_type       = "PERCENTAGE"
    subscribers        = [aws_sns_topic.budget_alerts.arn]
  }

  notification {
    notification_type    = "FORECASTED"
    comparison_operator  = "GREATER_THAN"
    threshold            = 95
    threshold_type       = "PERCENTAGE"
    subscribers        = [aws_sns_topic.budget_alerts.arn]
  }
}

variable "aws_region" {
  description = "AWS region to deploy resources"
  type        = string
  default     = "us-east-1"
}

variable "daily_budget_limit" {
  description = "Daily budget limit in USD"
  type        = number
  default     = 50
}

variable "monthly_budget_limit" {
  description = "Monthly budget limit in USD"
  type        = number
  default     = 1500
}

variable "notification_email" {
  description = "Email address to send budget alerts to"
  type        = string
}
