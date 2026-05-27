variable "prefix" {
  type        = string
  description = "Resource prefix for naming synthetic resources."
  default     = "chandra-test"
}

variable "inject_orphaned_ebs_volume" {
  type        = bool
  description = "Toggles CHANDRA-KRA01-001: 500 GiB unattached gp2 volume."
  default     = true
}

variable "inject_overprovisioned_instance" {
  type        = bool
  description = "Toggles CHANDRA-KRA01-002: Oversized r5.8xlarge instance running an idle stub workload."
  default     = true
}

variable "inject_idle_rds_instance" {
  type        = bool
  description = "Toggles CHANDRA-KRA01-003: Idle multi-AZ db.m5.4xlarge instance with no connections."
  default     = true
}

variable "inject_rightsizing_backlog" {
  type        = bool
  description = "Toggles KRA-01 Backlog Simulation: Enormous unoptimized footprint to populate Cost Explorer recommendations."
  default     = true
}
