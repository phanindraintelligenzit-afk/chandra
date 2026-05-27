variable "project_name" {
  type = string
}

variable "environment" {
  type = string
}

variable "retention_days" {
  type    = number
  default = 14
}
