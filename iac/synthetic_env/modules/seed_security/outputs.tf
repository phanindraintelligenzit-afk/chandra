output "active_fixtures" {
  description = "Array of active KRA-02 Security & IAM Drift fixture IDs currently deployed."
  value = compact([
    var.inject_sg_open_ssh_world ? "CHANDRA-KRA02-051" : "",
    var.inject_unencrypted_rds ? "CHANDRA-KRA02-052" : "",
    var.inject_cloudtrail_stop_logging ? "CHANDRA-KRA02-053" : "",
    var.inject_ec2_no_ssm_coverage ? "CHANDRA-KRA02-054" : ""
  ])
}
