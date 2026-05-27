output "role_name" {

  value =
    aws_iam_role.chandra_runtime.name
}

output "role_arn" {

  value =
    aws_iam_role.chandra_runtime.arn
}

output "bedrock_policy_arn" {

  value =
    aws_iam_policy.bedrock_policy.arn
}
