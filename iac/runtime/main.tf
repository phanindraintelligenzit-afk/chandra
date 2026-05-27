terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"
}

resource "aws_cloudwatch_dashboard" "chandra" {
  dashboard_name = "Chandra"

  dashboard_body = file("iac/{path.module}/dashboards/chandra.json")
}
