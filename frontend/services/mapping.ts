import type {
  ActionItem,
  AgentObservation,
  CostMetricsOutput,
  CostSnapshotItem,
  KraStatus,
  LiveIssue
} from "./api";
import { predefinedKraCatalog } from "@/store/kraCatalog";

export type Severity = "P1" | "P2" | "P3" | "P4";
export type ApprovalState = "Awaiting Review" | "Approved" | "Rejected" | "Escalated" | "Timed Out";
export type IncidentStatus = "Resolved" | "Investigating" | "Escalated" | "Monitoring" | "Awaiting Approval";

export type LiveOpsEvent = {
  id: string;
  time: string;
  severity: Severity;
  status: IncidentStatus;
  incident: string;
  service: string;
  account: string;
  confidence: number;
  resolution: string;
  approvalState: ApprovalState;
  reviewer: string;
  lockState: string;
  escalation: string;
  region: string;
  resourceId: string;
};

export type LiveIncident = LiveOpsEvent & {
  triage: string;
  eta: string;
  rootCause: string;
  humanEscalation: string;
};

export type LiveApprovalRow = {
  id: string;
  incident: string;
  severity: Severity;
  state: ApprovalState;
  reviewer: string;
  requested: string;
  decided: string;
  note: string;
  account: string;
  confidence: number;
  requestedBy: string;
  lockState: string;
  emailStatus: "pending" | "sent" | "viewed" | "approved" | "rejected";
  pendingReason: string;
  kraCode: string;
  steps: string[];
};

export type LiveCostCard = {
  label: string;
  value: string;
  delta: string;
  tone: string;
  note: string;
};

export type LiveKraEvaluation = {
  code: string;
  name: string;
  status: string;
  achievement: string;
  note: string;
  tone: string;
  isCustom: boolean;
};

const KRA_CODE_OFFSET = predefinedKraCatalog.length;

export function buildKraPayload(
  selectedPredefined: string[],
  customKras: string[]
): { code: string; description: string }[] {
  const payload: { code: string; description: string }[] = [];
  predefinedKraCatalog.forEach((kra, index) => {
    if (selectedPredefined.includes(kra.id)) {
      payload.push({ code: formatKraCode(index + 1), description: kra.desc });
    }
  });
  customKras.forEach((kra, index) => {
    payload.push({ code: formatKraCode(KRA_CODE_OFFSET + index + 1), description: kra });
  });
  return payload;
}

export function formatKraCode(seq: number): string {
  return `KRA-${String(seq).padStart(2, "0")}`;
}

export function kraCodeToName(code: string, customKras: string[]): string {
  if (!code) return "";
  const match = code.match(/KRA-0*(\d+)/i);
  if (!match) return code;
  const seq = parseInt(match[1], 10);
  if (seq >= 1 && seq <= predefinedKraCatalog.length) {
    return predefinedKraCatalog[seq - 1].id;
  }
  const customIndex = seq - KRA_CODE_OFFSET - 1;
  if (customIndex >= 0 && customIndex < customKras.length) {
    return customKras[customIndex];
  }
  return code;
}

function statusTone(status: string): string {
  const upper = (status ?? "").toUpperCase();
  if (upper.includes("RED") || upper.includes("CRITICAL") || upper.includes("FAIL")) return "text-signal";
  if (upper.includes("YELLOW") || upper.includes("AMBER") || upper.includes("WARN")) return "text-amber";
  if (upper.includes("GREEN") || upper.includes("STABLE") || upper.includes("HEALTHY")) return "text-emerald-300";
  return "text-frost";
}

export function deriveKraEvaluations(
  kraStatuses: KraStatus[],
  customKras: string[]
): LiveKraEvaluation[] {
  return kraStatuses.map((entry) => {
    const name = kraCodeToName(entry.kra_code, customKras);
    const code = entry.kra_code;
    const seq = parseInt(code.replace(/[^0-9]/g, ""), 10);
    const isCustom = !Number.isNaN(seq) && seq > KRA_CODE_OFFSET;
    return {
      code,
      name,
      status: entry.status,
      achievement: entry.achievement,
      note: entry.note,
      tone: statusTone(entry.status),
      isCustom
    };
  });
}

const SEVERITY_KEYWORDS: Array<{ pattern: RegExp; severity: Severity }> = [
  { pattern: /\b(public|exfiltration|brute force|credential|c2 beacon|impact|escalation|leak)\b/i, severity: "P1" },
  { pattern: /\b(anomalous|suspicious|unauthorized|malicious|persistence|unprotected|drift)\b/i, severity: "P2" },
  { pattern: /\b(spike|bottleneck|throttling|alarm|recon)\b/i, severity: "P3" }
];

function normalizePriority(value: string | undefined): Severity | null {
  if (!value) return null;
  const upper = value.toString().toUpperCase().trim();
  if (upper.includes("P1") || upper.includes("CRITICAL") || upper.includes("SEV1") || upper === "HIGH") return "P1";
  if (upper.includes("P2") || upper.includes("SEV2") || upper === "MEDIUM") return "P2";
  if (upper.includes("P3") || upper.includes("SEV3") || upper === "LOW") return "P3";
  if (upper.includes("P4") || upper.includes("SEV4") || upper === "INFO") return "P4";
  return null;
}

function inferSeverityFromText(text: string): Severity {
  for (const { pattern, severity } of SEVERITY_KEYWORDS) {
    if (pattern.test(text)) return severity;
  }
  return "P3";
}

export function resolveSeverity(priorityLevel: string | undefined, fallbackText: string): Severity {
  return normalizePriority(priorityLevel) ?? inferSeverityFromText(fallbackText);
}

const SERVICE_KEYWORDS: Array<{ pattern: RegExp; service: string }> = [
  { pattern: /\bS3\b/i, service: "S3" },
  { pattern: /\bRDS\b/i, service: "RDS" },
  { pattern: /\bEBS\b/i, service: "EBS" },
  { pattern: /\bEC2\b/i, service: "EC2" },
  { pattern: /\bECS\b|\bEKS\b/i, service: "ECS/EKS" },
  { pattern: /\bIAM\b/i, service: "IAM" },
  { pattern: /CloudTrail/i, service: "CloudTrail" },
  { pattern: /CloudWatch/i, service: "CloudWatch" },
  { pattern: /GuardDuty/i, service: "GuardDuty" },
  { pattern: /Config/i, service: "AWS Config" },
  { pattern: /Bedrock/i, service: "Bedrock" }
];

function inferService(text: string): string {
  for (const { pattern, service } of SERVICE_KEYWORDS) {
    if (pattern.test(text)) return service;
  }
  return "AWS";
}

function accountFromResource(service: string, resourceId: string): string {
  if (resourceId) return resourceId;
  if (!service) return "Chandra-Operations";
  const upper = service.toUpperCase();
  if (upper.includes("RDS")) return "Chandra-Data-Prod";
  if (upper.includes("S3")) return "Chandra-Storage-Prod";
  if (upper.includes("EC2") || upper.includes("EBS")) return "Chandra-Compute-Prod";
  if (upper.includes("BEDROCK") || upper.includes("LLM")) return "Chandra-AI-Prod";
  if (upper.includes("IAM") || upper.includes("CONFIG")) return "Chandra-Audit-Prod";
  return "Chandra-Operations";
}

function nowTime(offset = 0): string {
  return new Date(Date.now() + offset).toLocaleTimeString("en-US", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  });
}

export function deriveOpsEvents(issues: LiveIssue[]): LiveOpsEvent[] {
  if (!issues?.length) return [];
  const baseTime = Date.now();
  return issues.map((entry, index) => {
    const severity = resolveSeverity(entry.priorityLevel, entry.issue);
    const service = entry.service && entry.service.length > 0 ? entry.service : inferService(entry.issue);
    const account = accountFromResource(service, entry.resourceId);
    return {
      id: `live-evt-${index}`,
      time: nowTime(-index * 90_000),
      severity,
      status: severity === "P1" ? "Awaiting Approval" : "Investigating",
      incident: entry.issue,
      service,
      account,
      confidence: 86 + ((baseTime + index) % 11),
      resolution: "Auto-triage initiated; awaiting operator decision.",
      approvalState: "Awaiting Review",
      reviewer: "Pending operator review",
      lockState: "Remediation Paused",
      escalation: severity === "P1" ? "Security owner" : "Operator review",
      region: entry.region,
      resourceId: entry.resourceId
    };
  });
}

export function deriveIncidents(events: LiveOpsEvent[]): LiveIncident[] {
  return events.map((event, index) => ({
    ...event,
    triage: `${String(40 + index * 13).padStart(2, "0")}s`,
    eta: event.severity === "P1" ? "Pending approval" : "Under review",
    rootCause: event.incident,
    humanEscalation:
      event.severity === "P1" ? "Security owner assigned" : "Operator review queued"
  }));
}

export function deriveApprovals(actions: ActionItem[]): LiveApprovalRow[] {
  if (!actions?.length) return [];
  return actions.map((action, index) => {
    const severity = resolveSeverity(
      action.priorityLevel,
      `${action.actionName} ${action.actionDescription}`
    );
    return {
      id: `APV-${600 + index}`,
      incident: action.actionName,
      severity,
      state: "Awaiting Review",
      reviewer: "Operator",
      requested: nowTime(-index * 60_000),
      decided: "-",
      note: action.actionDescription,
      account: action.service,
      confidence: 84 + ((index * 7) % 12),
      requestedBy: "Chandra AI",
      lockState: "Remediation Paused",
      emailStatus: index === 0 ? "sent" : "pending",
      pendingReason: action.actionName,
      kraCode: action.kraCode,
      steps: Array.isArray(action.steps) ? action.steps : []
    };
  });
}

function costTone(changePct: number): string {
  if (Math.abs(changePct) >= 100) return "text-signal";
  if (Math.abs(changePct) >= 25) return "text-amber";
  if (changePct < 0) return "text-emerald-300";
  return "text-frost";
}

function formatCurrency(value: number): string {
  if (value >= 1000) return `$${(value / 1000).toFixed(2)}K`;
  return `$${value.toFixed(2)}`;
}

function formatDelta(changePct: number): string {
  const sign = changePct > 0 ? "+" : "";
  return `${sign}${changePct.toFixed(1)}%`;
}

export function deriveCostCards(snapshot: CostSnapshotItem[]): LiveCostCard[] {
  if (!snapshot?.length) return [];
  return snapshot.map((item) => ({
    label: item.service,
    value: `${formatCurrency(item.daily_avg_usd)}/day`,
    delta: formatDelta(item.change_24h_pct),
    tone: costTone(item.change_24h_pct),
    note: item.note
  }));
}

export type RegionSpend = { region: string; total: number };
export type ServiceSpend = { service: string; total: number };

export function deriveCostBreakdown(
  output: CostMetricsOutput | null
): { regions: RegionSpend[]; services: ServiceSpend[]; total: number; window: string } {
  if (!output || !output.DailyBreakdown?.length) {
    return { regions: [], services: [], total: 0, window: "" };
  }
  const regionTotals = new Map<string, number>();
  const serviceTotals = new Map<string, number>();
  let total = 0;
  output.DailyBreakdown.forEach((day) => {
    total += day.TotalDailyCost ?? 0;
    Object.entries(day.Regions ?? {}).forEach(([region, payload]) => {
      regionTotals.set(region, (regionTotals.get(region) ?? 0) + (payload.RegionTotal ?? 0));
      Object.entries(payload.Services ?? {}).forEach(([service, amount]) => {
        serviceTotals.set(service, (serviceTotals.get(service) ?? 0) + amount);
      });
    });
  });
  const regions = Array.from(regionTotals.entries())
    .map(([region, sum]) => ({ region, total: sum }))
    .sort((a, b) => b.total - a.total);
  const services = Array.from(serviceTotals.entries())
    .map(([service, sum]) => ({ service, total: sum }))
    .sort((a, b) => b.total - a.total);
  const window = `${output.LookbackPeriod?.Start ?? ""} → ${output.LookbackPeriod?.End ?? ""}`;
  return { regions, services, total, window };
}

export function healthTone(health: string): string {
  const upper = (health ?? "").toUpperCase();
  if (upper.includes("HEALTHY") || upper.includes("STABLE")) return "text-emerald-300";
  if (upper.includes("DEGRADED")) return "text-amber";
  if (upper.includes("CRITICAL") || upper.includes("FAILED")) return "text-signal";
  return "text-frost";
}

export function summarizeIssuesBySeverity(
  events: LiveOpsEvent[]
): { p1: number; p2: number; p3: number; p4: number; total: number } {
  const counts = { p1: 0, p2: 0, p3: 0, p4: 0, total: events.length };
  events.forEach((event) => {
    if (event.severity === "P1") counts.p1 += 1;
    else if (event.severity === "P2") counts.p2 += 1;
    else if (event.severity === "P3") counts.p3 += 1;
    else counts.p4 += 1;
  });
  return counts;
}

export type { AgentObservation, CostMetricsOutput };
