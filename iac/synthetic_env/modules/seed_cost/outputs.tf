output "seeds" {
  value = {
    "COST-001-idle-ec2"        = aws_instance.idle.arn
    "COST-002-unattached-ebs"  = aws_ebs_volume.orphan.arn
  }
}
