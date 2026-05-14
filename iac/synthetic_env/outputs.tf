output "seeds" {
  description = "Map of detector_id -> resource ARN. Consumed by the eval harness."
  value = merge(
    module.seed_security.seeds,
    module.seed_cost.seeds,
    module.seed_compliance.seeds,
    module.seed_performance.seeds,
    module.seed_reliability.seeds,
  )
}

output "region" {
  value = var.region
}

output "account_id" {
  value = data.aws_caller_identity.current.account_id
}

data "aws_caller_identity" "current" {}
