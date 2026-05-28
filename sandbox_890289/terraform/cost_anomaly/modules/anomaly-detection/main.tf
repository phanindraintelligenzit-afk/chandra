variable "monitor_names" {
  type    = list(string)
  default = []
}

variable "monitor_dimensions" {
  type = list(object({
    key   = string
    value = string
  }))
  default = []
}

resource "aws_cost_anomaly_detection_monitor" "anomaly_monitors" {
  count = length(var.monitor_names)

  monitor_name       = var.monitor_names[count.index]
  monitor_type       = "CUSTOM"
  dimension          = var.monitor_dimensions[count.index]
  threshold          = 1.2
  threshold_type     = "PERCENTAGE"
  frequency          = "DAILY"
}

output "monitors_arns" {
  value = aws_cost_anomaly_detection_monitor.anomaly_monitors[*].monitor_arn
}