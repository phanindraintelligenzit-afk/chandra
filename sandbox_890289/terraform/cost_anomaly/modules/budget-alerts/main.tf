variable "budget_names" {
  type = list(string)
}

variable "budget_values" {
  type = list(string)
}

variable "budget_periods" {
  type = list(string)
}

variable "subscribers" {
  type = list(string)
}

resource "aws_budgets_budget" "budgets" {
  count = length(var.budget_names)

  budget_name = var.budget_names[count.index]
  budget_type = "COST"
  time_unit   = var.budget_periods[count.index]

  budget_limit {
    amount = var.budget_values[count.index]
    unit   = "USD"
  }

  time_period {
    start = formatdate("YYYY-MM-DD", timestamp())
    end   = formatdate("YYYY-MM-DD", timestamp() + 31536000) # 1 year
  }

  notification {
    comparison_operator = "GREATER_THAN"
    threshold           = 80
    threshold_type      = "PERCENTAGE"
    notification_type   = "ACTUAL"
  }

  notification {
    comparison_operator = "GREATER_THAN"
    threshold           = 100
    threshold_type      = "PERCENTAGE"
    notification_type   = "ACTUAL"
  }

  subscriber {
    address = var.subscribers[0]
    type    = "EMAIL"
  }

  subscriber {
    address = var.subscribers[0]
    type    = "EMAIL"
  }
}

output "budgets_arns" {
  value = aws_budgets_budget.budgets[*].budget_arn
}