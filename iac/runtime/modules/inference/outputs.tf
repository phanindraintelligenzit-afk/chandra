output "instance_id" {
  description = "Inference EC2 instance ID"
  value       = aws_instance.inference.id
}

output "private_ip" {
  description = "Private IP of the inference instance"
  value       = aws_instance.inference.private_ip
}

output "public_ip" {
  description = "Public IP of the inference instance (if assigned)"
  value       = try(aws_eip.inference[0].public_ip, null)
}

output "security_group_id" {
  description = "Security group ID for the inference instance"
  value       = aws_security_group.inference.id
}

output "api_url" {
  description = "OpenAI-compatible API endpoint URL"
  value       = "http://${aws_instance.inference.private_ip}:${var.api_port}/v1"
}