terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

#########################################################
# IAM ROLE FOR CHANDRA RUNTIME
#########################################################

resource "aws_iam_role" "chandra_runtime" {

  name = "chandra-runtime-prod"

  permissions_boundary =
    aws_iam_policy.runtime_boundary.arn

  assume_role_policy = jsonencode({
    Version = "2012-10-17"

    Statement = [
      {
        Effect = "Allow"

        Principal = {
          Service = "ecs-tasks.amazonaws.com"
        }

        Action = "sts:AssumeRole"
      }
    ]
  })
}

#########################################################
# AWS MANAGED POLICIES
#########################################################

resource "aws_iam_role_policy_attachment" "readonly_access" {

  role =
    aws_iam_role.chandra_runtime.name

  policy_arn =
    "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

resource "aws_iam_role_policy_attachment" "security_audit" {

  role =
    aws_iam_role.chandra_runtime.name

  policy_arn =
    "arn:aws:iam::aws:policy/SecurityAudit"
}

#########################################################
# BEDROCK POLICY
# ONLY CLAUDE SONNET
#########################################################

resource "aws_iam_policy" "bedrock_policy" {

  name = "chandra-bedrock-sonnet"

  policy = jsonencode({

    Version = "2012-10-17"

    Statement = [

      {
        Effect = "Allow"

        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream"
        ]

        Resource = [

          "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-5-*",

          "arn:aws:bedrock:*::inference-profile/us.anthropic.claude-sonnet-4-5-*"
        ]
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "bedrock_attach" {

  role =
    aws_iam_role.chandra_runtime.name

  policy_arn =
    aws_iam_policy.bedrock_policy.arn
}

#########################################################
# PERMISSIONS BOUNDARY
# BLOCK IAM CHANGES
#########################################################

resource "aws_iam_policy" "runtime_boundary" {

  name = "chandra-runtime-boundary"

  policy = jsonencode({

    Version = "2012-10-17"

    Statement = [

      {
        Effect = "Deny"

        Action = [
          "iam:*"
        ]

        Resource = "*"
      }
    ]
  })
}
