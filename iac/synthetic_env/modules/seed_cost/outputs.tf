output "active_fixtures" {
  description = "Array of active KRA-01 Cost Anomaly fixture IDs currently deployed."
  value = compact([
    var.inject_orphaned_ebs_volume ? "CHANDRA-KRA01-001" : "",
    var.inject_overprovisioned_instance ? "CHANDRA-KRA01-002" : "",
    var.inject_idle_rds_instance ? "CHANDRA-KRA01-003" : "",
    var.inject_rightsizing_backlog ? "CHANDRA-KRA01-004" : ""
  ])
}
