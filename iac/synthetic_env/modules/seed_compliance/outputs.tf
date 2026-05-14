output "seeds" {
  value = {
    "COMP-001-rds-unencrypted" = aws_db_instance.unencrypted.arn
    "COMP-002-no-cloudtrail"   = "arn:aws:cloudtrail::${data.aws_caller_identity.current.account_id}:account"
  }
}

data "aws_caller_identity" "current" {}
