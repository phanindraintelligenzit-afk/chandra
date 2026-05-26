# KRA Definitions and Context

## Cost
**Objective:** Optimize cloud spending without compromising performance or security.

Focuses on:
- Right-sizing instances and reserved capacity
- Identifying idle or redundant resources
- Storage optimization (lifecycle policies, compression)
- Data transfer and egress costs
- Unused services and APIs

Business impact: Cost overruns delay feature delivery and reduce shareholder value.

## Security
**Objective:** Protect against unauthorized access, data breaches, and credential compromise.

Focuses on:
- IAM policies (overly permissive roles, public access)
- Credential hygiene (exposed keys, stale credentials)
- Network exposure (open security groups, public RDS/S3)
- Encryption at rest and in transit
- CloudTrail and access logging

Business impact: Breaches result in customer data loss, regulatory fines, and brand damage.

## Compliance
**Objective:** Meet regulatory and contractual obligations (PCI-DSS, HIPAA, SOC 2, GDPR).

Focuses on:
- Audit logging (CloudTrail, VPC Flow Logs, S3 access logs)
- Data retention and archival policies
- Encryption enforcement (keys, CMK usage)
- Access controls and segregation of duties
- Configuration baselines (CIS benchmarks)

Business impact: Non-compliance triggers audits, contract breaches, and legal liability.

## Performance
**Objective:** Deliver fast, responsive user experiences and reliable application throughput.

Focuses on:
- Database indexing and query optimization
- Caching layers (ElastiCache, CloudFront)
- Auto-scaling configuration (thresholds, cooldown)
- Network latency (CDN placement, AZ distribution)
- Resource contention (throttling, CPU saturation)

Business impact: Slow apps drive user churn and reduce conversion.

## Reliability
**Objective:** Ensure systems run continuously, recover quickly from failures, and scale to demand.

Focuses on:
- Multi-AZ and multi-region redundancy
- Health checks and auto-recovery
- Backup and disaster recovery (RTO/RPO)
- Circuit breakers and graceful degradation
- Load distribution and capacity planning

Business impact: Downtime loses revenue, erodes customer trust, and triggers SLA penalties.

---

## KRA Severity Guidance

Within each KRA, use the following severity matrix:

### Critical
- Account-wide blast radius
- Data loss risk (encrypted backups missing)
- Compliance violation (audit logs disabled)
- Active exploitation risk (public credentials, open RDS)

### High
- Multi-resource impact (overly permissive role used by 10+ services)
- Significant cost/performance degradation
- Compliance gap (encryption not enforced but present)
- Unencrypted sensitive data in transit

### Medium
- Single resource or service impact
- Recoverable data risk (unencrypted EBS attached to an EC2)
- Minor compliance gap (CloudTrail in one region only)
- Moderate performance or cost inefficiency

### Low
- Hygiene or best-practice violation
- No immediate risk
- Easily correctable (tag missing, logging not optimal)

### Info
- Informational, no risk (deprecated API usage, unused reserved capacity)
