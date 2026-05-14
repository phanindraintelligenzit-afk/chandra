output "seeds" {
  value = {
    "REL-001-rds-single-az" = aws_db_instance.single_az_prod.arn
  }
}
