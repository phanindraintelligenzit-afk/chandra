provider "aws" {
  region = var.region
}

module "cost_anomaly_monitors" {
  source = "./monitors"

  services = [
    "AmazonRelationalDatabaseService",
    "AmazonEC2",
    "AmazonCloudWatch",
    "AmazonBedrock"
  ]

  budget_name_daily  = "Daily-Cost-Limit"
  budget_name_monthly = "Monthly-Cost-Limit"
  budget_daily_limit = "500"
  budget_monthly_limit = "15000"
  budget_email       = var.alert_email
}

module "monitors" {
  source = "./modules/anomaly-detection"

  monitor_names = [
    "AnomalyMonitor-AWS-RDS",
    "AnomalyMonitor-AWS-EC2",
    "AnomalyMonitor-AWS-CloudWatch",
    "AnomalyMonitor-AWS-Bedrock"
  ]

  monitor_dimensions = [
    { key = "SERVICE", value = "AmazonRelationalDatabaseService" },
    { key = "SERVICE", value = "AmazonEC2" },
    { key = "SERVICE", value = "AmazonCloudWatch" },
    { key = "SERVICE", value = "AmazonBedrock" }
  ]
}

module "budgets" {
  source = "./modules/budget-alerts"

  budget_names = [
    module.cost_anomaly_monitors.budget_name_daily,
    module.cost_anomaly_monitors.budget_name_monthly
  ]
  budget_values = [
    module.cost_anomaly_monitors.budget_daily_limit,
    module.cost_anomaly_monitors.budget_monthly_limit
  ]
  budget_periods = ["DAILY", "MONTHLY"]
  subscribers    = [var.alert_email]
}

output "anomaly_monitor_arns" {
  value = module.monitors.monitors_arns
}

output "budget_arns" {
  value = module.budgets.budgets_arns
}