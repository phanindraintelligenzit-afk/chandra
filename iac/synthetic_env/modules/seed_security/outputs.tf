output "seeds" {
  value = {
    "SEC-001-public-s3"     = aws_s3_bucket.leaky.arn
    "SEC-002-open-sg-ssh"   = aws_security_group.open_ssh.arn
    "SEC-003-stale-key"     = aws_iam_user.stale.arn
    "SEC-004-wildcard-iam"  = aws_iam_policy.wildcard.arn
  }
}
