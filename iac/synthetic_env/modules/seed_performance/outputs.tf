output "seeds" {
  value = {
    "PERF-001-no-asg" = aws_instance.no_asg_prod.arn
  }
}
