"use client";

import { useOnboarding } from "@/store/OnboardingContext";
import { getAvatarById, getAvatarImageSrc, type AgentAvatar } from "@/store/agentProfile";
import { getKraMetric } from "@/store/kraCatalog";
import { fetchAgentObservations, fetchCostMetrics, analyzeActions, fetchBackendLogs, sendCopilotMessage, type CopilotChatMessage, type ActionResult, type BackendLog, type ActionItem, type CostMetricsOutput } from "@/services/api";
import { WorkerActionExecutionCenter, type WorkerActionExecutionCenterHandle } from "./WorkerActionExecutionCenter";
import {
  buildKraPayload,
  deriveApprovals,
  deriveCostBreakdown,
  deriveCostCards,
  deriveIncidents,
  deriveKraEvaluations,
  deriveOpsEvents,
  healthTone,
  kraCodeToName,
  summarizeIssuesBySeverity,
  type LiveApprovalRow,
  type LiveCostCard,
  type LiveIncident,
  type LiveKraEvaluation,
  type LiveOpsEvent
} from "@/services/mapping";
import { AnimatePresence, motion } from "framer-motion";
import { useRouter } from "next/navigation";
import {
  AlertTriangle,
  Bot,
  CheckCircle2,
  ChevronDown,
  CircleDot,
  Command,
  Download,
  FileSpreadsheet,
  FileText,
  Filter,
  MailCheck,
  RadioTower,
  Search,
  Send,
  ShieldCheck,
  Sparkles,
  Terminal,
  X
} from "lucide-react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  PieChart,
  Pie,
  Cell,
  Radar,
  RadarChart,
  PolarGrid,
  PolarAngleAxis,
  PolarRadiusAxis,
  ScatterChart,
  Scatter
} from "recharts";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

type Severity = "P1" | "P2" | "P3" | "P4";
type IncidentStatus = "Resolved" | "Investigating" | "Escalated" | "Monitoring" | "Awaiting Approval";
type ApprovalState = "Awaiting Review" | "Approved" | "Rejected" | "Escalated" | "Timed Out";
type EmailStatus = "pending" | "sent" | "viewed" | "approved" | "rejected";

type OpsEvent = {
  id: string;
  time: string;
  severity: Severity;
  status: IncidentStatus;
  incident: string;
  service: string;
  account: string;
  confidence: number;
  resolution: string;
  approvalState?: ApprovalState;
  reviewer?: string;
  lockState?: string;
  escalation?: string;
};

type Incident = OpsEvent & {
  triage: string;
  eta: string;
  rootCause: string;
  humanEscalation: string;
  approvalState: ApprovalState;
  reviewer: string;
  lockState: string;
  escalation: string;
};

type AuditRow = {
  timestamp: string;
  incidentId: string;
  remediation: string;
  account: string;
  confidence: number;
  reviewer: string;
  compliance: string;
  evidencePack: string;
};

type ApprovalRow = {
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
  emailStatus: EmailStatus;
  pendingReason: string;
  kraCode: string;
  steps: string[];
};

type KraReview = {
  id: string;
  title: string;
  objective: string;
  target: string;
  actual: string;
  confidence: number;
  automation: number;
  impact: string;
  evidence: string;
  remediation: string;
  weight: number;
  score: number;
  contribution: number;
  rating: string;
  color: string;
};

const kraReviewData: KraReview[] = [
  {
    id: "KRA-01",
    title: "Incident Response Quality",
    objective: "Contain P1/P2 events within enterprise MTTR targets.",
    target: "MTTR < 8m",
    actual: "5m 42s",
    confidence: 94,
    automation: 76,
    impact: "Reduced blast radius and stabilized production service availability.",
    evidence: "CHG-4912 / CloudTrail / Incident Stream",
    remediation: "Automated containment with supervisor reviewed lock action.",
    weight: 0.22,
    score: 92,
    contribution: 20.24,
    rating: "High Reliability",
    color: "#ff3b30"
  },
  {
    id: "KRA-02",
    title: "Governance Control",
    objective: "Enforce human approval discipline for high-risk operations.",
    target: "100% high-risk approvals",
    actual: "98%",
    confidence: 96,
    automation: 68,
    impact: "Maintained audit-ready posture while preserving required supervisor oversight.",
    evidence: "GxP control map / Approval queue audit",
    remediation: "Policy enforcement with approval lock state for destructive actions.",
    weight: 0.20,
    score: 91,
    contribution: 18.2,
    rating: "Audit Ready",
    color: "#ffb347"
  },
  {
    id: "KRA-03",
    title: "Access Drift Control",
    objective: "Detect and remediate IAM drift within control windows.",
    target: "Drift auto-remediate 90% in 30m",
    actual: "22m median",
    confidence: 92,
    automation: 64,
    impact: "Minimized privilege escalation risk while preserving identity audit trails.",
    evidence: "IAM Access Analyzer / drift rollback report",
    remediation: "Automated IAM rollback with manual supervisor halt on sensitive changes.",
    weight: 0.19,
    score: 89,
    contribution: 16.91,
    rating: "Governed",
    color: "#8ed9a8"
  },
  {
    id: "KRA-04",
    title: "Cost Anomaly Detection",
    objective: "Reduce idle spend while preserving service continuity.",
    target: "$150K avoided / 30d",
    actual: "$182K prevented",
    confidence: 93,
    automation: 81,
    impact: "Improved cloud economics and reduced unnecessary infrastructure waste.",
    evidence: "Cost telemetry / FinOps playbook",
    remediation: "Rightsizing automation with supervised cost impact review.",
    weight: 0.20,
    score: 93,
    contribution: 18.6,
    rating: "Operationally Efficient",
    color: "#8ed9a8"
  },
  {
    id: "KRA-05",
    title: "Audit Evidence Coverage",
    objective: "Preserve evidence completeness for regulated operations.",
    target: ">95% evidence coverage",
    actual: "95.4%",
    confidence: 95,
    automation: 84,
    impact: "Delivered audit-ready evidence while keeping remediation transparent.",
    evidence: "Immutable evidence packs / SOC2 readiness logs",
    remediation: "Automated evidence capture integrated with every remediation step.",
    weight: 0.19,
    score: 90,
    contribution: 17.1,
    rating: "Trusted",
    color: "#f4efe7"
  }
];

const trendData = [
  { t: "00", score: 83, risk: 24, kra: 76 },
  { t: "04", score: 86, risk: 19, kra: 80 },
  { t: "08", score: 91, risk: 16, kra: 84 },
  { t: "12", score: 89, risk: 21, kra: 86 },
  { t: "16", score: 94, risk: 13, kra: 90 },
  { t: "20", score: 96, risk: 11, kra: 92 },
  { t: "24", score: 95, risk: 12, kra: 93 }
];

function EmptyOperationalState({ label }: { label: string }) {
  return (
    <div className="rounded-2xl border border-white/10 bg-black/25 px-3 py-4 text-center text-[0.68rem] uppercase tracking-[0.16em] text-muted">
      {label}
    </div>
  );
}

type ObservationsSyncState = {
  status: "loading" | "retrying" | "success" | "error";
  attempt: number;
  message: string;
  nextDelayMs: number;
};

function OperationalSkeleton({ label }: { label: string }) {
  return (
    <div className="rounded-2xl border border-white/10 bg-black/25 p-3 telemetry-shimmer">
      <div className="flex items-center gap-2 text-[0.62rem] uppercase tracking-[0.18em] text-amber">
        <span className="h-1.5 w-1.5 rounded-full bg-signal pulse-core" />
        {label}
      </div>
      <div className="mt-3 space-y-2">
        <div className="h-2 rounded-full bg-white/10" />
        <div className="h-2 w-4/5 rounded-full bg-white/8" />
        <div className="h-2 w-2/3 rounded-full bg-white/8" />
      </div>
    </div>
  );
}

function OperationalRetryNotice({ sync }: { sync: ObservationsSyncState }) {
  const label =
    sync.status === "loading"
      ? "RETRIEVING LIVE OBSERVABILITY DATA..."
      : sync.status === "retrying"
      ? "RETRYING SECURITY ANALYSIS..."
      : sync.status === "error"
      ? "WAITING FOR LANGGRAPH RESPONSE..."
      : "LIVE OPERATIONAL INTELLIGENCE ONLINE";
  return (
    <div className="rounded-2xl border border-amber/25 bg-black/30 px-3 py-2 text-[0.62rem] uppercase tracking-[0.16em] text-muted">
      <div className="flex flex-wrap items-center gap-2">
        <span className={`h-1.5 w-1.5 rounded-full ${sync.status === "success" ? "bg-emerald-300" : "bg-signal pulse-core"}`} />
        <span className={sync.status === "success" ? "text-emerald-300" : "text-amber"}>{label}</span>
        {sync.attempt > 0 ? <span>attempt {sync.attempt}</span> : null}
        {sync.nextDelayMs > 0 ? <span>next retry {Math.ceil(sync.nextDelayMs / 1000)}s</span> : null}
      </div>
      {sync.message ? <div className="mt-1 normal-case text-frost/65">{sync.message}</div> : null}
    </div>
  );
}

function nowTime(offset = 0) {
  const d = new Date(Date.now() + offset);
  return d.toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

function ClientTime({ offset = 0 }: { offset?: number }) {
  const [time, setTime] = useState("");
  useEffect(() => {
    setTime(nowTime(offset));
    const t = window.setInterval(() => setTime(nowTime(offset)), 1000);
    return () => window.clearInterval(t);
  }, [offset]);
  return <>{time || "--:--:--"}</>;
}

function Reveal({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <motion.div
      className={className}
      initial={{ opacity: 0, y: 16 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: "-60px" }}
      transition={{ duration: 0.45, ease: [0.16, 1, 0.3, 1] }}
    >
      {children}
    </motion.div>
  );
}

function SectionHead({ label, sub, action }: { label: string; sub?: string; action?: ReactNode }) {
  return (
    <div className="section-head">
      <span className="section-label">{label}</span>
      <div className="ml-auto flex items-center gap-3">
        {sub ? <span className="section-sub">{sub}</span> : null}
        {action}
      </div>
    </div>
  );
}

function SeverityPill({ value }: { value: Severity }) {
  const tone = {
    P1: "border-signal/55 bg-signal/20 text-signal",
    P2: "border-amber/50 bg-amber/15 text-amber",
    P3: "border-white/20 bg-white/[0.05] text-frost",
    P4: "border-emerald-300/35 bg-emerald-300/12 text-emerald-300"
  }[value];
  return <span className={cx("inline-flex w-10 items-center justify-center border px-1.5 py-0.5 text-[0.62rem] font-semibold tracking-wider", tone)}>{value}</span>;
}

function StatusDot({ status }: { status: IncidentStatus }) {
  const tone = status === "Resolved" ? "bg-emerald-300" : status === "Escalated" || status === "Awaiting Approval" ? "bg-signal" : "bg-amber";
  return (
    <span className="inline-flex items-center gap-2 text-[0.7rem]">
      <span className={cx("h-1.5 w-1.5 rounded-full", tone, status !== "Resolved" && "pulse-core")} />
      {status}
    </span>
  );
}

function ApprovalBadge({ state }: { state: ApprovalState }) {
  const tone = {
    "Awaiting Review": "border-amber/45 bg-amber/12 text-amber",
    Approved: "border-emerald-300/40 bg-emerald-300/12 text-emerald-300",
    Rejected: "border-signal/45 bg-signal/15 text-signal",
    Escalated: "border-signal/50 bg-signal/15 text-signal",
    "Timed Out": "border-white/20 bg-white/[0.06] text-frost"
  }[state];
  return <span className={cx("inline-flex items-center border px-2 py-0.5 text-[0.6rem] uppercase tracking-[0.18em]", tone)}>{state}</span>;
}

function EmailStatusPill({ status }: { status: EmailStatus }) {
  const tone = {
    pending: "border-amber/45 bg-amber/12 text-amber",
    sent: "border-emerald-300/40 bg-emerald-300/12 text-emerald-300",
    viewed: "border-emerald-300/40 bg-emerald-300/12 text-emerald-300",
    approved: "border-emerald-300/40 bg-emerald-300/12 text-emerald-300",
    rejected: "border-signal/45 bg-signal/15 text-signal"
  }[status];
  return <span className={cx("inline-flex items-center border px-2 py-0.5 text-[0.55rem] uppercase tracking-[0.18em]", tone)}>{status}</span>;
}

function AgentAvatarMark({ avatar, size = 80 }: { avatar: AgentAvatar; size?: number }) {
  return (
    <div
      className="avatar-portrait shrink-0"
      style={{
        width: size,
        height: size,
        borderColor: "rgba(142,217,168,0.55)",
        boxShadow: "0 0 34px rgba(142,217,168,0.16)"
      }}
    >
      <img src={getAvatarImageSrc(avatar)} alt={avatar.label} draggable={false} />
    </div>
  );
}

function CommandHeader({
  liveObservations,
  liveSummary,
  sync
}: {
  liveObservations: ReturnType<typeof useOnboarding>["observations"];
  liveSummary: ReturnType<typeof summarizeIssuesBySeverity>;
  sync: ObservationsSyncState;
}) {
  const router = useRouter();
  const { agentName, avatarId, permissions, observationsError } = useOnboarding();
  const AGENT = (agentName || "Chandra").toUpperCase();
  const avatar = getAvatarById(avatarId);
  const health = liveObservations?.health ?? "Active";
  const healthClass = healthTone(health);
  const liveOpsForPanel = useMemo(
    () => (liveObservations?.issues ? deriveOpsEvents(liveObservations.issues) : []),
    [liveObservations]
  );

  return (
    <section className="relative px-5 pt-10 pb-6 md:px-10 md:pt-12">
      <div className="mx-auto max-w-[1480px]">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3 text-[0.65rem] uppercase tracking-[0.28em] text-muted">
          <div className="flex items-center gap-3">
            <RadioTower size={14} className="text-signal" />
            <span>{AGENT}</span>
            <span className="text-amber">AWS CLOUD OPERATIONS</span>
            <span className="text-frost/70">PREMIUM OPERATIONAL COMMAND CENTER</span>
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={() => router.push("/onboarding")}
              className="rounded-2xl border border-white/15 bg-white/5 px-3 py-2 text-[0.65rem] uppercase tracking-[0.18em] text-frost transition hover:border-emerald-300/40 hover:bg-black/40"
            >
              EDIT DEPLOYMENT CHOICES
            </button>
            <div className={`flex items-center gap-2 border px-3 py-1.5 text-[0.6rem] uppercase tracking-[0.22em] ${healthClass} ${health.toUpperCase().includes("DEGRADED") ? "border-amber/35 bg-amber/10" : health.toUpperCase().includes("CRITICAL") || health.toUpperCase().includes("FAILED") ? "border-signal/35 bg-signal/10" : "border-emerald-300/35 bg-emerald-300/10"}`}>
              <span className={`h-1.5 w-1.5 rounded-full pulse-core ${health.toUpperCase().includes("DEGRADED") ? "bg-amber" : health.toUpperCase().includes("CRITICAL") || health.toUpperCase().includes("FAILED") ? "bg-signal" : "bg-emerald-300"}`} />
              OPERATING STATUS: {health.toUpperCase()}
            </div>
            {observationsError ? (
              <div className="flex items-center gap-2 border border-amber/40 bg-amber/10 px-3 py-1.5 text-[0.6rem] uppercase tracking-[0.22em] text-amber">
                BACKEND FALLBACK
              </div>
            ) : null}
            <div className="flex items-center gap-3 rounded-2xl border border-signal/30 bg-black/45 px-3 py-1.5 shadow-[0_0_24px_rgba(255,59,59,0.16)] backdrop-blur">
              <AgentAvatarMark avatar={avatar} size={36} />
              <span className="text-sm font-semibold uppercase tracking-[0.08em] text-frost leading-tight">{AGENT}</span>
            </div>
          </div>
        </div>
        <Reveal>
          <div className="mb-4 grid gap-4 lg:grid-cols-[1fr_520px] lg:items-end">
            <div className="flex items-center gap-4">
              <AgentAvatarMark avatar={avatar} size={80} />
              <h1 className="display text-5xl uppercase leading-[0.85] text-frost md:text-6xl">{AGENT}</h1>
            </div>
            <div className="grid gap-2 sm:grid-cols-5">
              {[
                [String(liveObservations?.observations?.length || 0), "TOTAL OBSERVATIONS"],
                [String(liveSummary.total || 0), "ACTIVE ALERTS"],
                [String(liveSummary.p1 + liveSummary.p2 || 0), "ACTIVE INCIDENTS"],
                [String(liveObservations?.actions?.length || 0), "PENDING ACTIONS"]
              ].map(([value, label]) => (
                <div key={label} className="rounded-2xl border border-white/10 bg-black/30 px-3 py-2">
                  <div className="text-base text-frost">{value}</div>
                  <div className="mt-1 text-[0.54rem] uppercase tracking-[0.16em] text-muted">{label}</div>
                </div>
              ))}
              <div className={cx("rounded-2xl border px-3 py-2", health.toUpperCase().includes("CRITICAL") || health.toUpperCase().includes("FAIL") ? "bg-signal/20 border-signal/50" : health.toUpperCase().includes("DEGRADED") ? "bg-amber/20 border-amber/50" : "bg-emerald-300/20 border-emerald-300/50")}>
                  <div className={cx("text-base font-bold", health.toUpperCase().includes("CRITICAL") || health.toUpperCase().includes("FAIL") ? "text-signal" : health.toUpperCase().includes("DEGRADED") ? "text-amber" : "text-emerald-300")}>
                    {health.toUpperCase().includes("CRITICAL") || health.toUpperCase().includes("FAIL") ? "🔴 CRITICAL" : health.toUpperCase().includes("DEGRADED") ? "🟡 ATTENTION" : "🟢 HEALTHY"}
                  </div>
                  <div className="mt-1 text-[0.54rem] uppercase tracking-[0.16em] text-muted">OVERALL STATUS</div>
              </div>
            </div>
          </div>
        </Reveal>

      </div>
    </section>
  );
}

function BulletSummary({ text }: { text: string }) {
  if (!text) return null;
  const bullets = text.split(/(?<=\.|\n)(?=\s+[A-Z])/).map(s => s.trim().replace(/^\.+|\.$/g, '')).filter(Boolean);
  return (
    <ul className="list-disc pl-4 mt-1 space-y-1 marker:text-amber">
      {bullets.map((b, i) => (
        <li key={i}>{b}</li>
      ))}
    </ul>
  );
}

function TreeGroup({ title, count, children, tone = "text-frost", defaultOpen = false }: { title: string, count: number, children: ReactNode, tone?: string, defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="mb-2 rounded-2xl border border-white/10 bg-black/25">
      <div 
        className="flex items-center justify-between cursor-pointer p-3 select-none" 
        onClick={() => setOpen(!open)}
      >
        <div className="flex items-center gap-2">
          <ChevronDown size={14} className={cx("transition-transform duration-300", open ? "" : "-rotate-90")} />
          <span className={cx("text-[0.65rem] uppercase tracking-[0.2em]", tone)}>{title}</span>
        </div>
        <span className="text-[0.6rem] bg-white/5 px-1.5 py-0.5 rounded text-muted">{count} items</span>
      </div>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} className="overflow-hidden">
            <div className="p-3 pt-0">
              {children}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function OperationalIntelligencePanel({
  permissionsCount,
  liveEvents,
  liveObservations,
  liveSecurity,
  liveCompliance,
  sync
}: {
  permissionsCount: number;
  liveEvents?: LiveOpsEvent[];
  liveObservations?: string[];
  liveSecurity?: string[];
  liveCompliance?: string;
  sync: ObservationsSyncState;
}) {
  const alerts = liveEvents && liveEvents.length
    ? liveEvents.map((event) => ({
        id: event.id,
        severity: event.severity,
        text: event.incident,
        time: event.time,
        service: event.service,
        region: event.region,
        resourceId: event.resourceId
      }))
    : [];

  const p1Alerts = alerts.filter(a => a.severity === "P1");
  const p2Alerts = alerts.filter(a => a.severity === "P2");
  const p3Alerts = alerts.filter(a => a.severity === "P3" || a.severity === "P4");

  const donutData = [
    { name: "P1", value: p1Alerts.length, color: "#ff3b3b" },
    { name: "P2", value: p2Alerts.length, color: "#ffb800" },
    { name: "P3/P4", value: p3Alerts.length, color: "#ffffffb3" }
  ].filter(d => d.value > 0);

  const tone = {
    P1: "text-signal",
    P2: "text-amber",
    P3: "text-frost/70",
    P4: "text-frost/70"
  } as const;

  const observationRows = liveObservations || [];
  const securityRows = liveSecurity || [];
  const complianceText = liveCompliance || `${permissionsCount || 0} governed access scopes active.`;

  return (
    <Reveal className="glass overflow-hidden p-4 mb-4">
      <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="text-[0.65rem] uppercase tracking-[0.22em] text-muted">OPERATIONAL INTELLIGENCE</div>
          <div className="mt-2 text-sm text-frost/70">Live alerts, observations, recommended actions, and security posture.</div>
        </div>
        <div className="grid gap-2 sm:min-w-[360px]">
          <div className="rounded-2xl border border-white/10 bg-black/30 px-3 py-2 text-[0.68rem] uppercase tracking-[0.18em] text-muted">
            {alerts.length} alerts - governed
          </div>
          <OperationalRetryNotice sync={sync} />
        </div>
      </div>
      
      <div className="grid gap-4 lg:grid-cols-[1fr_250px]">
        <div className="grid gap-3 lg:grid-cols-2">
          {/* ALERTS TREE */}
          <div className="operational-scroll max-h-[400px] overflow-y-auto pr-2">
            <TreeGroup title="P1 Critical Alerts" count={p1Alerts.length} tone="text-signal" defaultOpen={true}>
              <div className="grid gap-2">
                {p1Alerts.map(a => (
                  <div key={a.id} className="rounded border border-signal/20 bg-signal/5 p-2">
                    <div className="flex justify-between items-center text-[0.65rem]">
                      <span className="text-signal uppercase tracking-wider font-semibold">{a.severity}</span>
                      <span className="text-muted">{a.time}</span>
                    </div>
                    <div className="text-xs text-frost/90 mt-1">{a.text}</div>
                  </div>
                ))}
              </div>
            </TreeGroup>
            
            <TreeGroup title="P2 Warnings" count={p2Alerts.length} tone="text-amber">
              <div className="grid gap-2">
                {p2Alerts.map(a => (
                  <div key={a.id} className="rounded border border-amber/20 bg-amber/5 p-2">
                    <div className="flex justify-between items-center text-[0.65rem]">
                      <span className="text-amber uppercase tracking-wider font-semibold">{a.severity}</span>
                      <span className="text-muted">{a.time}</span>
                    </div>
                    <div className="text-xs text-frost/90 mt-1">{a.text}</div>
                  </div>
                ))}
              </div>
            </TreeGroup>

            <TreeGroup title="P3/P4 Info Alerts" count={p3Alerts.length} tone="text-frost/70">
              <div className="grid gap-2">
                {p3Alerts.map(a => (
                  <div key={a.id} className="rounded border border-white/10 bg-black/30 p-2">
                    <div className="flex justify-between items-center text-[0.65rem]">
                      <span className="text-frost/70 uppercase tracking-wider font-semibold">{a.severity}</span>
                      <span className="text-muted">{a.time}</span>
                    </div>
                    <div className="text-xs text-frost/90 mt-1">{a.text}</div>
                  </div>
                ))}
              </div>
            </TreeGroup>
          </div>
          
          {/* FINDINGS TREE */}
          <div className="operational-scroll max-h-[400px] overflow-y-auto pr-2">
            <TreeGroup title="Observations" count={observationRows.length} tone="text-frost" defaultOpen={true}>
              <div className="space-y-3">
                {observationRows.map((obs, i) => (
                  <div key={i} className="text-xs text-frost/80"><BulletSummary text={obs} /></div>
                ))}
              </div>
            </TreeGroup>

            <TreeGroup title="Security Findings" count={securityRows.length} tone="text-signal">
              <div className="space-y-3">
                {securityRows.map((sec, i) => (
                  <div key={i} className="text-xs text-frost/80"><BulletSummary text={sec} /></div>
                ))}
              </div>
            </TreeGroup>

            <TreeGroup title="Compliance Findings" count={1} tone="text-emerald-300">
              <div className="text-xs text-frost/80"><BulletSummary text={complianceText} /></div>
            </TreeGroup>
          </div>
        </div>

        {/* DONUT CHART */}
        <div className="flex flex-col items-center justify-center rounded-2xl border border-white/10 bg-black/20 p-4">
          <div className="text-[0.65rem] uppercase tracking-[0.2em] text-muted mb-2">Alert Distribution</div>
          <div className="h-[160px] w-full">
            {donutData.length > 0 ? (
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie
                    data={donutData}
                    cx="50%"
                    cy="50%"
                    innerRadius={50}
                    outerRadius={70}
                    paddingAngle={5}
                    dataKey="value"
                  >
                    {donutData.map((entry, index) => (
                      <Cell key={`cell-${index}`} fill={entry.color} />
                    ))}
                  </Pie>
                  <Tooltip 
                    contentStyle={{ backgroundColor: "#000000cc", border: "1px solid #ffffff20", borderRadius: "8px" }}
                    itemStyle={{ color: "#ffffff" }}
                  />
                </PieChart>
              </ResponsiveContainer>
            ) : (
              <div className="flex h-full items-center justify-center text-xs text-muted">No alerts</div>
            )}
          </div>
          <div className="mt-2 flex gap-3 text-[0.6rem] uppercase tracking-wider text-muted">
            <div className="flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-signal"></span> P1</div>
            <div className="flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-amber"></span> P2</div>
            <div className="flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-white/70"></span> P3</div>
          </div>
        </div>
      </div>
    </Reveal>
  );
}

function CircularProgress({ percent, color }: { percent: number; color: string }) {
  const radius = 24;
  const circumference = 2 * Math.PI * radius;
  const strokeDashoffset = circumference - (percent / 100) * circumference;

  return (
    <div className="relative flex items-center justify-center w-16 h-16">
      <svg className="w-full h-full transform -rotate-90" viewBox="0 0 60 60">
        <circle cx="30" cy="30" r={radius} fill="transparent" stroke="#ffffff10" strokeWidth="4" />
        <circle cx="30" cy="30" r={radius} fill="transparent" stroke={color} strokeWidth="4"
                strokeDasharray={circumference} strokeDashoffset={strokeDashoffset} strokeLinecap="round" />
      </svg>
      <div className="absolute text-xs font-bold" style={{ color }}>{percent}%</div>
    </div>
  );
}

function KRAMetricsReview({
  activeKras,
  liveEvaluations
}: {
  activeKras: { code: string; name: string }[];
  liveEvaluations?: LiveKraEvaluation[];
}) {
  // Derive scores deterministically from backend status
  const derivedKRAs = activeKras.map(kra => {
    const backendData = liveEvaluations?.find(e => e.code === kra.code || e.name.includes(kra.name.split(" ")[0]));
    const status = backendData?.status?.toUpperCase() || "UNKNOWN";
    
    let score = 0;
    let color = "#ffffff70";
    if (status.includes("GREEN") || status.includes("HEALTHY") || status.includes("STABLE")) {
      score = 92 + (kra.code.length % 5);
      color = "#4ade80"; // emerald-300
    } else if (status.includes("YELLOW") || status.includes("AMBER") || status.includes("WARN") || status.includes("DEGRADED")) {
      score = 75 + (kra.code.length % 10);
      color = "#ffb800"; // amber
    } else if (status.includes("RED") || status.includes("CRIT") || status.includes("FAIL")) {
      score = 45 + (kra.code.length % 15);
      color = "#ff3b3b"; // signal
    } else {
      score = 80;
    }

    return {
      ...kra,
      backendData,
      score,
      color,
      statusDisplay: status
    };
  });

  const radarData = derivedKRAs.map(k => ({
    subject: k.code,
    A: k.score,
    fullMark: 100,
  }));

  return (
    <section className="section-shell">
      <div className="section-inner">
        <SectionHead label="KRA PERFORMANCE" sub="Capability-driven operational review" />
        <Reveal>
          <div className="grid gap-4 lg:grid-cols-[1fr_300px]">
            <div className="space-y-3">
              {derivedKRAs.map((kra) => (
                <TreeGroup 
                  key={kra.code}
                  title={`${kra.code} — ${kra.name} — ${kra.score}%`} 
                  count={1}
                  tone={kra.color === "#4ade80" ? "text-emerald-300" : kra.color === "#ffb800" ? "text-amber" : kra.color === "#ff3b3b" ? "text-signal" : "text-frost"}
                >
                  <div className="flex gap-4 p-2 bg-black/20 rounded">
                    <CircularProgress percent={kra.score} color={kra.color} />
                    <div className="flex-1">
                      <div className="text-[0.6rem] uppercase tracking-widest text-muted mb-1">Status: <span style={{ color: kra.color }}>{kra.statusDisplay}</span></div>
                      {kra.backendData?.achievement ? (
                        <BulletSummary text={kra.backendData.achievement} />
                      ) : (
                        <div className="text-xs text-muted mt-2">No direct achievement logged for this capability. Evaluated automatically.</div>
                      )}
                      {kra.backendData?.note && (
                        <div className="text-xs text-frost/70 mt-2 italic">{kra.backendData.note}</div>
                      )}
                    </div>
                  </div>
                </TreeGroup>
              ))}
            </div>

            <div className="flex flex-col items-center justify-center rounded-2xl border border-white/10 bg-black/20 p-4 min-h-[300px]">
              <div className="text-[0.65rem] uppercase tracking-[0.2em] text-muted mb-2">KRA Radar Profile</div>
              <ResponsiveContainer width="100%" height="100%">
                <RadarChart cx="50%" cy="50%" outerRadius="65%" data={radarData}>
                  <PolarGrid stroke="#ffffff20" />
                  <PolarAngleAxis dataKey="subject" tick={{ fill: "#ffffff80", fontSize: 10 }} />
                  <PolarRadiusAxis angle={30} domain={[0, 100]} tick={false} axisLine={false} />
                  <Radar name="KRA Score" dataKey="A" stroke="#4ade80" fill="#4ade80" fillOpacity={0.2} />
                  <Tooltip 
                    contentStyle={{ backgroundColor: "#000000cc", border: "1px solid #ffffff20", borderRadius: "8px", fontSize: 12 }}
                    itemStyle={{ color: "#4ade80" }}
                  />
                </RadarChart>
              </ResponsiveContainer>
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  );
}

function IncidentCard({ incident }: { incident: Incident }) {
  const tone = {
    P1: "text-signal",
    P2: "text-amber",
    P3: "text-frost/70",
    P4: "text-frost/70"
  } as const;

  return (
    <div className="rounded-2xl border border-white/10 bg-black/25 p-3 flex flex-col justify-between">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2 text-[0.6rem] uppercase tracking-[0.16em]">
          <span className={cx("font-bold", tone[incident.severity as keyof typeof tone])}>{incident.severity}</span>
          <span className="text-muted">•</span>
          <span className="text-frost/70">{incident.status}</span>
        </div>
        <div className="text-[0.6rem] text-muted">{incident.time}</div>
      </div>
      <div className="text-[0.7rem] font-semibold text-frost/90 leading-tight">
        {incident.incident}
      </div>
    </div>
  );
}

function ActiveIncidents({ source, sync }: { source?: Incident[]; sync: ObservationsSyncState }) {
  const [isOpen, setIsOpen] = useState(true);

  const rows = Array.isArray(source) ? source : [];
  const p1 = rows.filter(r => r.severity === "P1");
  const p2 = rows.filter(r => r.severity === "P2");
  const p3 = rows.filter(r => r.severity === "P3" || r.severity === "P4");

  return (
    <Reveal className="glass overflow-hidden p-4">
      <div className="section-head cursor-pointer select-none" onClick={() => setIsOpen(!isOpen)}>
        <div className="flex items-center gap-2">
          <ChevronDown size={14} className={cx("transition-transform duration-300", isOpen ? "" : "-rotate-90")} />
          <span className="section-label">ACTIVE INCIDENTS</span>
        </div>
        <div className="ml-auto flex items-center gap-3">
          <span className="section-sub">{rows.length} matched · supervised</span>
        </div>
      </div>
      <AnimatePresence initial={false}>
        {isOpen && (
          <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} className="overflow-hidden">
            <div className="pt-4 grid gap-4 lg:grid-cols-3">
              <div>
                <div className="text-[0.65rem] uppercase tracking-[0.2em] text-signal mb-3 border-b border-signal/20 pb-1">P1 Critical Incidents ({p1.length})</div>
                <div className="space-y-2 max-h-[300px] overflow-y-auto pr-1">
                  {p1.map(inc => <IncidentCard key={inc.id} incident={inc} />)}
                  {p1.length === 0 && <div className="text-xs text-muted">No P1 incidents</div>}
                </div>
              </div>
              <div>
                <div className="text-[0.65rem] uppercase tracking-[0.2em] text-amber mb-3 border-b border-amber/20 pb-1">P2 High Incidents ({p2.length})</div>
                <div className="space-y-2 max-h-[300px] overflow-y-auto pr-1">
                  {p2.map(inc => <IncidentCard key={inc.id} incident={inc} />)}
                  {p2.length === 0 && <div className="text-xs text-muted">No P2 incidents</div>}
                </div>
              </div>
              <div>
                <div className="text-[0.65rem] uppercase tracking-[0.2em] text-frost/70 mb-3 border-b border-white/10 pb-1">P3/P4 Low Incidents ({p3.length})</div>
                <div className="space-y-2 max-h-[300px] overflow-y-auto pr-1">
                  {p3.map(inc => <IncidentCard key={inc.id} incident={inc} />)}
                  {p3.length === 0 && <div className="text-xs text-muted">No P3 incidents</div>}
                </div>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </Reveal>
  );
}

function generateApprovalEmail(approval: ApprovalRow) {
  const subject = `[Action Required] ${approval.severity} Incident Approval for ${approval.account}`;
  const body = `Incident: ${approval.incident}\nSeverity: ${approval.severity}\nAWS Account: ${approval.account}\nRecommendation: ${approval.note}\nAI Confidence: ${approval.confidence}%\nImpact: ${approval.pendingReason}\nRisk: High-risk operational action requiring supervisor review.\n\nPlease approve or reject via the operations dashboard.\n\nActions:\n- APPROVE REMEDIATION\n- REJECT ACTION\n- ESCALATE TO SECURITY\n`;
  return { subject, body };
}

function createEmailPayload(approval: ApprovalRow) {
  const { subject, body } = generateApprovalEmail(approval);
  return {
    to: `${approval.reviewer.replace(/ /g, ".").toLowerCase()}@example.com`,
    subject,
    body,
    providerHints: ["AWS SES", "SendGrid", "Nodemailer", "Enterprise SMTP"]
  };
}

function deriveAuditRowsFromLive(events: OpsEvent[], approvals: ApprovalRow[], complianceSummary?: string): AuditRow[] {
  const compliance = complianceSummary || "Live-Control";
  const eventRows = events.map((event, index) => ({
    timestamp: `${new Date().toISOString().slice(0, 10)} ${event.time}`,
    incidentId: `LIVE-${String(index + 1).padStart(4, "0")}`,
    remediation: event.resolution,
    account: event.account,
    confidence: event.confidence,
    reviewer: event.reviewer ?? "Chandra AI",
    compliance,
    evidencePack: `LIVE-EVD-${String(index + 1).padStart(4, "0")}`
  }));
  const approvalRows = approvals.map((approval, index) => ({
    timestamp: `${new Date().toISOString().slice(0, 10)} ${approval.requested}`,
    incidentId: approval.id,
    remediation: approval.note,
    account: approval.account,
    confidence: approval.confidence,
    reviewer: approval.reviewer,
    compliance,
    evidencePack: `APPROVAL-${String(index + 1).padStart(4, "0")}`
  }));
  return [...eventRows, ...approvalRows];
}

type EnrichedApprovalRow = ApprovalRow & {
  severity?: string;
  jiraUrl?: string;
  autoApproved?: boolean;
};

function HumanReviewQueue({ seed, rawActions, sync, onAutoApproved }: { seed?: ApprovalRow[]; rawActions?: ActionItem[]; sync?: ObservationsSyncState; onAutoApproved?: (action: any) => void }) {
  const [approvals, setApprovals] = useState<EnrichedApprovalRow[]>(Array.isArray(seed) ? seed : []);
  const [analyzedResults, setAnalyzedResults] = useState<ActionResult[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [isOpen, setIsOpen] = useState(true);
  // Track which actions have already been dispatched for execution — prevents re-runs of the
  // effect (triggered by new rawActions references) from submitting the same action twice
  const dispatchedActionsRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    setApprovals(Array.isArray(seed) ? seed : []);
  }, [seed]);

  useEffect(() => {
    if (!rawActions || rawActions.length === 0) return;

    const enrichApprovals = async () => {
      setIsLoading(true);
      try {
        const results = await analyzeActions(rawActions);
        setAnalyzedResults(results);

        setApprovals((current) =>
          current.map((row) => {
            const analyzed = results.find((r) => r.actionName === row.incident);
            const isAutoApproved = analyzed ? !analyzed.HumanReviewNeeded : false;

            if (isAutoApproved && onAutoApproved) {
              // Use jiraKey + actionName as a unique dedup key
              const dedupKey = `${analyzed?.JiraIssueKey || row.id}::${row.incident}`;
              if (!dispatchedActionsRef.current.has(dedupKey)) {
                dispatchedActionsRef.current.add(dedupKey);
                setTimeout(() => {
                  onAutoApproved({
                    ...row,
                    incident: row.incident,
                    actionName: row.incident,
                    severity: (analyzed?.severity as Severity) || row.severity,
                    jiraUrl: analyzed?.JiraUrl,
                    jiraKey: analyzed?.JiraIssueKey,
                    service: "AWS",
                    kraCode: row.kraCode || "",
                    steps: row.steps || [],
                    note: row.note
                  });
                }, 500);
              }
            }

            return analyzed
              ? {
                  ...row,
                  severity: analyzed.severity as Severity,
                  jiraUrl: analyzed.JiraUrl,
                  autoApproved: !analyzed.HumanReviewNeeded,
                  state: analyzed.HumanReviewNeeded ? ("Awaiting Review" as ApprovalState) : ("Approved" as ApprovalState)
                }
              : row;
          })
        );
      } catch (error) {
        console.error("Failed to analyze actions:", error);
      } finally {
        setIsLoading(false);
      }
    };

    enrichApprovals();
  }, [rawActions]);

  const pendingApprovals = approvals.filter((row) => row.state === "Awaiting Review" || row.state === "Escalated");
  const visibleApprovals = approvals;

  function markApproval(id: string, nextState: ApprovalState) {
    if (nextState === "Approved" && onAutoApproved) {
      const row = approvals.find((r) => r.id === id);
      if (row) {
        onAutoApproved({
          ...row,
          incident: row.incident,
          actionName: row.incident,
          severity: row.severity,
          jiraUrl: row.jiraUrl,
          service: "AWS",
          kraCode: row.kraCode || "",
          steps: row.steps || [],
          note: `${row.note} Approved by supervisor.`
        });
      }
    }

    setApprovals((current) =>
      current.map((row) =>
        row.id === id
          ? {
              ...row,
              state: nextState,
              decided: nextState === "Awaiting Review" ? "-" : nowTime(),
              emailStatus: nextState === "Approved" ? "approved" : nextState === "Rejected" ? "rejected" : row.emailStatus,
              note:
                nextState === "Approved"
                  ? `${row.note} Approved by supervisor.`
                  : nextState === "Rejected"
                  ? `${row.note} Rejected: human reviewer blocked execution.`
                  : row.note
            }
          : row
      )
    );
  }

  function handleSendEmail(id: string) {
    setApprovals((current) =>
      current.map((row) =>
        row.id === id
          ? {
              ...row,
              emailStatus: row.emailStatus === "pending" ? "sent" : row.emailStatus === "sent" ? "viewed" : row.emailStatus
            }
          : row
      )
    );
  }

  return (
    <Reveal className="glass overflow-hidden p-3">
      <div className="section-head cursor-pointer select-none" onClick={() => setIsOpen(!isOpen)}>
        <div className="flex items-center gap-2">
          <ChevronDown size={14} className={cx("transition-transform duration-300", isOpen ? "" : "-rotate-90")} />
          <span className="section-label">HUMAN APPROVAL CENTER</span>
        </div>
        <div className="ml-auto flex items-center gap-3">
          <span className="section-sub">High-risk review queue · {pendingApprovals.length} pending</span>
          <div className="flex items-center gap-2">
            {(["Awaiting Review", "Approved", "Rejected", "Escalated"] as ApprovalState[]).map((s) => (
              <div key={s} className="kpi-card px-2 py-1 text-[0.68rem]">{s}: {approvals.filter((r) => r.state === s).length}</div>
            ))}
          </div>
        </div>
      </div>

      <AnimatePresence initial={false}>
        {isOpen && (
          <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} className="overflow-hidden">
            <div className="pt-4">
      <div className="operational-scroll flex max-h-[360px] gap-3 overflow-x-auto py-1">
        {isLoading || (!visibleApprovals.length && sync && sync.status !== "success") ? (
          <div className="w-full flex items-center justify-center">
            <div className="text-center space-y-3">
              <div className="h-8 w-8 rounded-full border-2 border-signal/30 border-t-signal animate-spin mx-auto" />
              <div className="text-[0.7rem] uppercase tracking-[0.16em] text-muted">
                {isLoading ? "Analyzing actions..." : "Loading operational data..."}
              </div>
            </div>
          </div>
        ) : visibleApprovals.length ? (
          visibleApprovals.map((approval) => (
            <div key={approval.id} className="glass flex-shrink-0 w-[340px] border border-white/10 p-3">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2 text-[0.65rem] uppercase tracking-[0.14em] text-amber">
                    <span>{approval.id}</span>
                    <ApprovalBadge state={approval.state} />
                    {approval.autoApproved ? (
                      <span className="border border-emerald-300/40 bg-emerald-300/10 px-1.5 py-0.5 text-[0.55rem] tracking-[0.16em] text-emerald-300">
                        AUTO-APPROVED
                      </span>
                    ) : null}
                    {approval.kraCode ? (
                      <span className="border border-white/15 bg-white/[0.04] px-1.5 py-0.5 text-[0.55rem] tracking-[0.16em] text-frost/80">
                        {approval.kraCode}
                      </span>
                    ) : null}
                  </div>
                  <div className="mt-1 text-sm font-semibold text-frost">{approval.incident}</div>
                  <div className="mt-1 text-[0.68rem] text-muted">{approval.account} · {approval.severity} · {approval.requested}</div>
                  {approval.jiraUrl ? (
                    <div className="mt-2">
                      <a href={approval.jiraUrl} target="_blank" rel="noopener noreferrer" className="text-[0.65rem] text-signal hover:text-signal/80 underline">
                        View Jira Issue
                      </a>
                    </div>
                  ) : null}
                </div>
                <div className="flex flex-col gap-2 w-[110px]">
                  {approval.state === "Awaiting Review" ? (
                    <>
                      <button onClick={() => markApproval(approval.id, "Approved")} className="rounded-md border border-emerald-300/30 bg-emerald-300/10 px-2 py-1 text-[0.68rem] uppercase tracking-[0.08em] text-emerald-200 hover:bg-emerald-300/20 transition">Approve</button>
                      <button onClick={() => markApproval(approval.id, "Rejected")} className="rounded-md border border-signal/30 bg-signal/10 px-2 py-1 text-[0.68rem] uppercase tracking-[0.08em] text-signal hover:bg-signal/20 transition">Reject</button>
                      <button onClick={() => markApproval(approval.id, "Escalated")} className="rounded-md border border-white/10 bg-white/[0.03] px-2 py-1 text-[0.68rem] uppercase tracking-[0.08em] text-frost hover:bg-white/10 transition">Escalate</button>
                    </>
                  ) : (
                    <div className={`text-[0.6rem] uppercase tracking-[0.16em] text-center py-1 font-semibold ${
                      approval.state === "Approved" ? "text-emerald-300" :
                      approval.state === "Rejected" ? "text-signal" : "text-amber"
                    }`}>
                      {approval.autoApproved ? "Auto-Approved" : approval.state}
                    </div>
                  )}
                </div>
              </div>
              <div className="mt-3 grid gap-2 text-[0.68rem] text-frost/75">
                <div className="flex items-center justify-between border-t border-white/8 pt-2">
                  <span className="text-muted">Lock</span>
                  <span>{approval.lockState}</span>
                </div>
                <div className="normal-case leading-5">{approval.note}</div>
                {approval.steps && approval.steps.length ? (
                  <div className="rounded-2xl border border-white/8 bg-black/30 p-2">
                    <div className="text-[0.55rem] uppercase tracking-[0.2em] text-amber">REMEDIATION STEPS</div>
                    <ol className="mt-1 list-decimal space-y-1 pl-4 text-[0.66rem] leading-5 text-frost/85">
                      {approval.steps.map((step, index) => (
                        <li key={`${approval.id}-step-${index}`}>{step}</li>
                      ))}
                    </ol>
                  </div>
                ) : null}
              </div>
            </div>
          ))
        ) : (
          <div className="w-full">
            <EmptyOperationalState label="No live remediation actions returned" />
          </div>
        )}
      </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </Reveal>
  );
}

function AuditLogs({ rows }: { rows?: AuditRow[] }) {
  const [query, setQuery] = useState("");
  const [timeWindow, setTimeWindow] = useState<"hourly" | "daily" | "weekly" | "monthly">("daily");
  const sourceRows = Array.isArray(rows) ? rows : [];
  const [isOpen, setIsOpen] = useState(true);

  const filtered = useMemo(() => {
    const text = query.toLowerCase();
    return sourceRows.filter((row) =>
      `${row.incidentId} ${row.account} ${row.remediation} ${row.reviewer} ${row.compliance} ${row.evidencePack}`
        .toLowerCase()
        .includes(text)
    );
  }, [query, sourceRows]);

  return (
    <Reveal className="glass overflow-hidden p-4">
      <div className="section-head cursor-pointer select-none" onClick={() => setIsOpen(!isOpen)}>
        <div className="flex items-center gap-2">
          <ChevronDown size={14} className={cx("transition-transform duration-300", isOpen ? "" : "-rotate-90")} />
          <span className="section-label">AUDITS & COMPLIANCE</span>
        </div>
        <div className="ml-auto flex items-center gap-3">
          <span className="section-sub">{filtered.length} records · {timeWindow}</span>
          <div className="flex items-center gap-1.5">
            <button onClick={() => exportCsv(filtered)} className="flex items-center gap-1 border border-white/15 bg-white/[0.04] px-2 py-1 text-[0.58rem] uppercase tracking-[0.18em] text-frost/85 hover:border-emerald-300/40 hover:text-emerald-300">
              <FileText size={11} /> CSV
            </button>
            <button onClick={() => exportXlsx(filtered)} className="flex items-center gap-1 border border-white/15 bg-white/[0.04] px-2 py-1 text-[0.58rem] uppercase tracking-[0.18em] text-frost/85 hover:border-amber/45 hover:text-amber">
              <FileSpreadsheet size={11} /> XLSX
            </button>
            <button onClick={() => exportPdf(filtered)} className="flex items-center gap-1 border border-white/15 bg-white/[0.04] px-2 py-1 text-[0.58rem] uppercase tracking-[0.18em] text-frost/85 hover:border-signal/40 hover:text-signal">
              <Download size={11} /> PDF
            </button>
          </div>
        </div>
      </div>
      <AnimatePresence initial={false}>
        {isOpen && (
          <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} className="overflow-hidden">
            <div className="pt-4">
              <div className="mb-3 grid gap-2 lg:grid-cols-[1fr_auto]">
                <label className="flex items-center gap-2 border border-white/12 bg-black/35 px-3 py-1.5 text-[0.7rem] text-muted">
                  <Search size={13} />
                  <input
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    className="w-full bg-transparent text-frost outline-none placeholder:text-muted"
                    placeholder="Search incident, account, evidence pack, compliance control"
                  />
                </label>
                <div className="flex items-center gap-1 border border-white/12 bg-black/35 p-1">
                  {(["hourly", "daily", "weekly", "monthly"] as const).map((option) => (
                    <button
                      key={option}
                      onClick={() => setTimeWindow(option)}
                      className={cx(
                        "px-2.5 py-1 text-[0.58rem] uppercase tracking-[0.16em] transition",
                        timeWindow === option ? "bg-signal/15 text-signal" : "text-muted hover:text-frost"
                      )}
                    >
                      {option}
                    </button>
                  ))}
                </div>
              </div>
              <div className="max-h-[400px] overflow-auto scrollbar-mini space-y-2 pr-2">
                {filtered.length ? (
                  filtered.map((row, idx) => (
                    <TreeGroup 
                      key={idx} 
                      title={`[${row.timestamp}] ${row.incidentId} — ${row.compliance}`} 
                      count={1}
                      tone="text-emerald-300"
                    >
                      <div className="grid gap-4 lg:grid-cols-2 p-2 bg-black/20 rounded">
                        <div>
                          <div className="text-[0.6rem] uppercase tracking-[0.2em] text-muted mb-1">Details</div>
                          <div className="text-[0.75rem] text-frost/90"><span className="text-muted">Account:</span> {row.account}</div>
                          <div className="text-[0.75rem] text-frost/90"><span className="text-muted">Reviewer:</span> {row.reviewer}</div>
                          <div className="text-[0.75rem] text-frost/90"><span className="text-muted">Remediation:</span> {row.remediation}</div>
                          <div className="text-[0.75rem] text-frost/90"><span className="text-muted">Confidence:</span> {row.confidence}%</div>
                        </div>
                        <div>
                          <div className="text-[0.6rem] uppercase tracking-[0.2em] text-muted mb-1">Evidence Pack</div>
                          <a href={row.evidencePack} target="_blank" rel="noopener noreferrer" className="text-[0.75rem] text-blue-400 hover:underline break-all">
                            {row.evidencePack}
                          </a>
                        </div>
                      </div>
                    </TreeGroup>
                  ))
                ) : (
                  <div className="py-4 text-center text-xs text-muted">No audit logs match your query</div>
                )}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </Reveal>
  );
}

function OperationsCopilot({ latestEvent, unread }: { latestEvent?: OpsEvent; unread: number }) {
  const [open, setOpen] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [loading, setLoading] = useState(false);
  const [sessionId] = useState(() => `session-${Math.random().toString(36).slice(2, 10)}`);
  const [messages, setMessages] = useState<CopilotChatMessage[]>([
    { role: "system", text: "Context synchronized. Latest high-risk incident status available.", meta: "memory: incidents / approvals / audit" },
    { role: "chandra", text: "Live operational context is ready. Ask about incidents, approvals, cost posture, compliance, or remediation risk.", meta: "live copilot ready" }
  ]);
  const alerts = useMemo(
    () =>
      latestEvent
        ? [
            `${latestEvent.severity} ${latestEvent.status.toLowerCase()} on ${latestEvent.account}`,
            `${latestEvent.service} confidence ${latestEvent.confidence}%`,
            latestEvent.approvalState ? `Approval state: ${latestEvent.approvalState}` : "Live context available"
          ]
        : ["No active live incidents returned"],
    [latestEvent]
  );
  const suggestions = ["/review-pending-approvals", "/summarize-high-risk-incidents", "/draft-approval-email", "/explain-governance"];

  async function submit(value = prompt) {
    const command = value.trim();
    if (!command || loading) return;
    const eventContext = latestEvent
      ? `${latestEvent.incident} / ${latestEvent.service} / ${latestEvent.account} / ${latestEvent.status}`
      : "No active live incident context";
    setLoading(true);
    setPrompt("");
    setMessages((current) => [
      ...current,
      { role: "supervisor", text: command, meta: "human supervisor command" }
    ]);
    setOpen(true);
    try {
      const response = await sendCopilotMessage({
        sessionId,
        message: `${command}\n\nLive context: ${eventContext}`
      });
      setMessages((current) => [...current, response]);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Copilot request failed";
      setMessages((current) => [
        ...current,
        {
          role: "chandra",
          text: "I could not reach the live copilot endpoint. Operational context is preserved; retry when the backend is reachable.",
          meta: message
        }
      ]);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="fixed bottom-4 right-4 z-[70] w-[calc(100vw-2rem)] max-w-[420px]">
      <AnimatePresence>
        {open ? (
          <motion.div initial={{ opacity: 0, y: 14, scale: 0.96 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 14, scale: 0.96 }} className="glass overflow-hidden">
            <div className="flex items-center justify-between border-b border-white/10 p-3">
              <div className="flex items-center gap-2">
                <Bot size={16} className="text-signal" />
                <div>
                  <div className="text-[0.72rem] uppercase tracking-[0.2em] text-frost">Chandra Ops Copilot</div>
                  <div className="text-[0.58rem] uppercase tracking-[0.16em] text-emerald-300">approval-aware workflow</div>
                </div>
              </div>
              <button aria-label="Close copilot" onClick={() => setOpen(false)} className="text-muted hover:text-frost">
                <X size={15} />
              </button>
            </div>
            <div className="border-b border-white/8 bg-signal/[0.06] px-3 py-2">
              <div className="mb-1 flex items-center gap-1.5 text-[0.55rem] uppercase tracking-[0.18em] text-signal">
                <AlertTriangle size={11} /> {alerts.length} approval-aware alerts
              </div>
              <ul className="space-y-0.5 text-[0.65rem] text-frost/85">
                {alerts.map((alert) => (
                  <li key={alert} className="flex items-start gap-1.5"><span className="mt-1 h-1 w-1 rounded-full bg-signal" />{alert}</li>
                ))}
              </ul>
            </div>
            <div className="max-h-[300px] space-y-2 overflow-y-auto p-3 scrollbar-mini">
              {messages.map((message, index) => (
                <div key={`message-${index}-${message.role}`} className={cx("border p-2 text-[0.7rem]", message.role === "supervisor" ? "ml-6 border-amber/30 bg-amber/8" : "mr-3 border-white/10 bg-black/35")}>
                  <div className="mb-1 text-[0.55rem] uppercase tracking-[0.18em] text-muted">{message.role}</div>
                  <div className="leading-5 text-frost/88">
                    {message.text.split(/(SUMMARY:|RECOMMENDED ACTIONS:)/).map((part, i) => 
                      part === "SUMMARY:" || part === "RECOMMENDED ACTIONS:" ? (
                        <strong key={i} className="text-amber block mt-2 mb-1">{part}</strong>
                      ) : (
                        <span key={i}>{part}</span>
                      )
                    )}
                  </div>
                  <div className="mt-1.5 border-l border-signal/40 pl-2 text-[0.55rem] uppercase tracking-[0.14em] text-muted">{message.meta}</div>
                </div>
              ))}
              <div className="border border-emerald-300/20 bg-emerald-300/8 p-2 text-[0.7rem]">
                <div className="mb-1 flex items-center gap-1.5 text-[0.55rem] uppercase tracking-[0.18em] text-emerald-300">
                  <MailCheck size={11} /> Supervisor guidance ready
                </div>
                <p className="text-frost/82">Chandra will not execute dangerous operations until explicit approval is recorded.</p>
              </div>
              {loading ? (
                <div className="border border-signal/25 bg-signal/8 p-2 text-[0.7rem] text-frost/82">
                  <div className="mb-1 text-[0.55rem] uppercase tracking-[0.18em] text-signal">copilot thinking</div>
                  Querying live operational assistant...
                </div>
              ) : null}
            </div>
            <div className="border-t border-white/10 p-3">
              <div className="mb-2 flex flex-wrap gap-1.5">
                {suggestions.map((item) => (
                  <button key={item} onClick={() => submit(item)} className="border border-white/10 bg-white/[0.03] px-1.5 py-0.5 text-[0.58rem] uppercase tracking-[0.12em] text-muted hover:border-signal/40 hover:text-frost">
                    {item}
                  </button>
                ))}
              </div>
              <form onSubmit={(event) => { event.preventDefault(); submit(); }} className="flex items-center gap-2 border border-white/12 bg-black/45 px-2 py-1.5">
                <Command size={13} className="text-amber" />
                <input
                  value={prompt}
                  onChange={(event) => setPrompt(event.target.value)}
                  disabled={loading}
                  placeholder="Ask about approvals, risk, or remediation state"
                  className="min-w-0 flex-1 bg-transparent text-[0.7rem] text-frost outline-none placeholder:text-muted"
                />
                <button aria-label="Send" disabled={loading} className="text-signal hover:text-frost disabled:opacity-45"><Send size={13} /></button>
              </form>
            </div>
          </motion.div>
        ) : null}
      </AnimatePresence>
      {!open ? (
        <button
          onClick={() => setOpen(true)}
          className="relative ml-auto flex items-center gap-2 border border-signal/35 bg-black/80 px-3 py-2 text-[0.62rem] uppercase tracking-[0.18em] text-frost shadow-signal backdrop-blur"
        >
          <Sparkles size={13} className="text-signal" />
          Ops Copilot
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-300 pulse-core" />
          {unread > 0 ? (
            <span className="absolute -right-1.5 -top-1.5 flex h-5 min-w-[1.25rem] items-center justify-center rounded-full border border-signal bg-signal/90 px-1 text-[0.55rem] font-semibold text-frost">
              {unread}
            </span>
          ) : null}
        </button>
      ) : null}
    </div>
  );
}

function useOperationalFeed(seed?: OpsEvent[]) {
  const [events, setEvents] = useState<OpsEvent[]>(Array.isArray(seed) ? seed : []);

  useEffect(() => {
    setEvents(Array.isArray(seed) ? seed : []);
  }, [seed]);

  return events;
}

function severityRank(severity: Severity) {
  return { P1: 4, P2: 3, P3: 2, P4: 1 }[severity];
}

function RealTimeActivityFeed({ events }: { events: OpsEvent[] }) {
  const [isOpen, setIsOpen] = useState(true);

  return (
    <Reveal className="glass overflow-hidden p-4 h-full">
      <div className="section-head cursor-pointer select-none" onClick={() => setIsOpen(!isOpen)}>
        <div className="flex items-center gap-2">
          <ChevronDown size={14} className={cx("transition-transform duration-300", isOpen ? "" : "-rotate-90")} />
          <span className="section-label">REAL-TIME ACTIVITY FEED</span>
        </div>
        <div className="ml-auto flex items-center gap-3">
          <span className="section-sub">{events.length} latest activities</span>
        </div>
      </div>
      <AnimatePresence initial={false}>
        {isOpen && (
          <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} className="overflow-hidden">
            <div className="pt-4">
              <div className="feed-stream max-h-[300px] space-y-2 overflow-y-auto pr-1">
                {events.length > 0 ? events.map(e => (
                  <div key={e.id} className="flex gap-3 text-[0.7rem] border-b border-white/5 pb-2 last:border-b-0">
                    <span className="text-muted shrink-0 w-12">{e.time.substring(0,5)}</span>
                    <div className="flex-1">
                      <span className={cx("uppercase font-medium mr-2", e.severity === "P1" ? "text-signal" : e.severity === "P2" ? "text-amber" : "text-frost")}>
                        {e.status === "Awaiting Approval" ? "Approval Requested" : e.severity === "P1" ? "Critical Alert Generated" : e.severity === "P2" ? "Cost Alert Generated" : "Observation Created"}
                      </span>
                      <span className="text-frost/80">{e.incident}</span>
                    </div>
                  </div>
                )) : (
                  <div className="text-xs text-muted">No activity events</div>
                )}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </Reveal>
  );
}

function CostMonitoring({
  cards,
  breakdown: globalBreakdown,
  rawMetrics,
  costDays = 7,
  onDaysChange
}: {
  cards?: LiveCostCard[];
  breakdown?: ReturnType<typeof deriveCostBreakdown>;
  rawMetrics?: CostMetricsOutput | null;
  costDays?: number;
  onDaysChange?: (days: number) => void;
}) {
  const [selectedRegion, setSelectedRegion] = useState<string | null>(null);

  const regionBreakdown = useMemo(() => {
    if (!rawMetrics || !rawMetrics.DailyBreakdown) return null;
    if (!selectedRegion) return null;

    let total = 0;
    const serviceTotals = new Map<string, number>();

    rawMetrics.DailyBreakdown.forEach((day) => {
      const regionData = day.Regions?.[selectedRegion];
      if (regionData) {
        total += regionData.RegionTotal ?? 0;
        Object.entries(regionData.Services ?? {}).forEach(([service, amount]) => {
          serviceTotals.set(service, (serviceTotals.get(service) ?? 0) + amount);
        });
      }
    });

    const services = Array.from(serviceTotals.entries())
      .map(([service, sum]) => ({ service, total: sum }))
      .sort((a, b) => b.total - a.total);

    return { total, services };
  }, [rawMetrics, selectedRegion]);

  const chartData = useMemo(() => {
    if (!rawMetrics?.DailyBreakdown) return [];
    return rawMetrics.DailyBreakdown.slice(-costDays).map(d => {
      let cost = 0;
      if (selectedRegion) {
        cost = d.Regions?.[selectedRegion]?.RegionTotal ?? 0;
      } else {
        cost = Number(d.TotalDailyCost) || 0;
      }
      return {
        date: d.Date.slice(5),
        cost
      };
    });
  }, [rawMetrics, selectedRegion, costDays]);

  const avgCost = chartData.length > 0 ? chartData.reduce((sum, d) => sum + d.cost, 0) / chartData.length : 0;
  const maxCost = chartData.length > 0 ? Math.max(...chartData.map(d => d.cost)) : 0;
  const activeBreakdown = selectedRegion && regionBreakdown ? regionBreakdown : globalBreakdown;
  const totalCost = activeBreakdown?.total || 0;

  if (!rawMetrics) {
    return (
      <Reveal className="glass overflow-hidden p-4 min-h-[300px] flex flex-col items-center justify-center">
        <div className="absolute top-4 left-4">
          <SectionHead label="COST MONITORING" sub="FinOps · Trend & Anomalies" />
        </div>
        <div className="flex-1 flex flex-col items-center justify-center space-y-4">
          <div className="h-8 w-8 rounded-full border-2 border-emerald-300/30 border-t-emerald-300 animate-spin" />
          <div className="text-[0.7rem] uppercase tracking-[0.16em] text-muted">Retrieving cost intelligence...</div>
        </div>
      </Reveal>
    );
  }

  return (
    <Reveal className="glass overflow-hidden p-4">
      <SectionHead label="COST MONITORING" sub="FinOps · Trend & Anomalies" />
      <div className="flex gap-2 items-center overflow-x-auto scrollbar-mini py-2 mb-2 mt-4">
        <span className="text-[0.6rem] text-muted shrink-0 uppercase tracking-widest mr-2">Timeline:</span>
        {Array.from({ length: 30 }, (_, i) => i + 1).map(d => (
          <button
            key={d}
            onClick={() => onDaysChange?.(d)}
            className={cx(
              "shrink-0 px-2 py-0.5 rounded text-[0.65rem] transition-colors border",
              costDays === d
                ? "bg-emerald-400/20 text-emerald-300 border-emerald-400"
                : "bg-white/5 text-frost/70 border-white/10 hover:border-white/30 hover:text-frost"
            )}
          >
            {d}d
          </button>
        ))}
      </div>
      <div className="mt-2 grid gap-4 lg:grid-cols-[1.5fr_1fr]">
        <div className="flex flex-col gap-4">
          <div className="h-[240px] w-full rounded-2xl border border-white/5 bg-black/20 p-2">
            {chartData.length > 0 ? (
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={chartData} margin={{ top: 15, right: 15, bottom: 10, left: 0 }}>
                  <defs>
                    <linearGradient id="colorCost" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%" stopColor="#4ade80" stopOpacity={0.3}/>
                      <stop offset="95%" stopColor="#4ade80" stopOpacity={0}/>
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke="#ffffff08" vertical={false} />
                  <XAxis dataKey="date" stroke="#ffffff40" fontSize={10} tickLine={false} axisLine={false} dy={10} />
                  <YAxis stroke="#ffffff40" fontSize={10} tickFormatter={(v) => `$${v.toFixed(0)}`} tickLine={false} axisLine={false} dx={-10} />
                  <Tooltip
                    contentStyle={{ backgroundColor: "rgba(0,0,0,0.8)", border: "1px solid rgba(255,255,255,0.1)", borderRadius: "8px", backdropFilter: "blur(8px)" }}
                    itemStyle={{ color: "#4ade80" }}
                    formatter={(v: any) => [typeof v === "number" ? `$${v.toFixed(2)}` : v, "Daily Spend"]}
                    labelFormatter={(label) => `Date: ${label}`}
                  />
                  <Area type="monotone" dataKey="cost" stroke="#4ade80" fillOpacity={1} fill="url(#colorCost)" strokeWidth={2} />
                </AreaChart>
              </ResponsiveContainer>
            ) : (
              <div className="flex h-full items-center justify-center text-xs text-muted">No trend data available</div>
            )}
          </div>
        </div>

        <div className="flex flex-col gap-3">
          <div className="rounded-2xl border border-white/10 bg-black/30 p-4 h-full flex flex-col justify-center">
            <div className="text-[0.65rem] uppercase tracking-[0.2em] text-emerald-300 mb-3 flex items-center gap-2">
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 pulse-core" />
              {costDays}-Day Cost Intelligence
            </div>
            
            <div className="grid grid-cols-2 gap-3 mb-4">
              <div>
                <div className="text-[0.6rem] text-muted uppercase tracking-widest">Total Spend</div>
                <div className="text-xl font-light text-frost">${totalCost.toFixed(2)}</div>
              </div>
              <div>
                <div className="text-[0.6rem] text-muted uppercase tracking-widest">Daily Avg</div>
                <div className="text-xl font-light text-frost">${avgCost.toFixed(2)}</div>
              </div>
            </div>
            
            {activeBreakdown && activeBreakdown.services.length > 0 && (
              <div className="mt-2 pt-4 border-t border-white/10 flex-1 flex flex-col min-h-0">
                <div className="text-[0.6rem] uppercase tracking-widest text-muted mb-2 shrink-0">Cost By Service {selectedRegion ? `(${selectedRegion})` : ""}</div>
                <div className="space-y-2 overflow-y-auto scrollbar-mini pr-2 flex-1 max-h-[150px]">
                  {activeBreakdown.services.map((s, i) => (
                    <div key={i} className="flex justify-between items-center text-[0.7rem] gap-2">
                      <span className="text-frost/80 truncate flex-1 min-w-0" title={s.service}>
                        {s.service.replace("Amazon ", "").replace("AWS ", "")}
                      </span>
                      <span className="font-mono text-emerald-300/90 shrink-0">${s.total.toFixed(2)}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {globalBreakdown && globalBreakdown.regions.length > 0 && (
              <div className="mt-4 pt-4 border-t border-white/10 shrink-0">
                <div className="text-[0.6rem] uppercase tracking-widest text-muted mb-2 flex justify-between items-center">
                  <span>Active Regions</span>
                  {selectedRegion && (
                    <button onClick={() => setSelectedRegion(null)} className="text-[0.55rem] text-emerald-300/70 hover:text-emerald-300 transition-colors">
                      Clear Filter
                    </button>
                  )}
                </div>
                <div className="flex flex-wrap gap-1.5 max-h-[60px] overflow-y-auto scrollbar-mini">
                  {globalBreakdown.regions.map((r, i) => {
                    const isSelected = selectedRegion === r.region;
                    return (
                      <button
                        key={i}
                        onClick={() => setSelectedRegion(isSelected ? null : r.region)}
                        className={cx(
                          "px-1.5 py-0.5 rounded border text-[0.65rem] transition-all duration-200",
                          isSelected 
                            ? "border-emerald-400 bg-emerald-400/20 text-emerald-300" 
                            : "border-white/10 bg-white/5 text-frost/70 hover:border-white/30 hover:text-frost"
                        )}
                      >
                        {r.region}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </Reveal>
  );
}

function LiveOpsStream({ sync }: { sync?: ObservationsSyncState }) {
  const [logs, setLogs] = useState<BackendLog[]>([]);
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: number;
    let autoScroll = true;

    async function poll() {
      if (cancelled) return;
      try {
        const batch = await fetchBackendLogs(25);
        if (cancelled) return;
        setLogs(batch);
        
        if (autoScroll && containerRef.current) {
          containerRef.current.scrollTop = containerRef.current.scrollHeight;
        }
      } catch (e) {
        // silently ignore polling errors
      }
      timer = window.setTimeout(poll, 1500);
    }
    
    poll();
    
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, []);

  return (
    <Reveal className="glass overflow-hidden p-4 h-full">
      <div className="section-head cursor-pointer select-none" onClick={() => setIsOpen(!isOpen)}>
        <div className="flex items-center gap-2">
          <ChevronDown size={14} className={cx("transition-transform duration-300", isOpen ? "" : "-rotate-90")} />
          <span className="section-label">LIVE LOGS</span>
        </div>
        <div className="ml-auto flex items-center gap-3">
          <span className="section-sub flex items-center gap-1">
            {sync?.status === "loading" || sync?.status === "retrying" ? (
              <span className="h-1.5 w-1.5 rounded-full bg-amber pulse-core" />
            ) : (
              <span className="h-1.5 w-1.5 rounded-full bg-emerald-300 pulse-core" />
            )}
            backend stream connected
          </span>
        </div>
      </div>
      <AnimatePresence initial={false}>
        {isOpen && (
          <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} className="overflow-hidden">
            <div className="pt-4">
              <div 
                ref={containerRef}
                className="bg-black/50 p-2 font-mono text-[0.6rem] h-[300px] overflow-y-auto scrollbar-mini border border-white/5"
              >
                {logs.length > 0 ? logs.map((l, idx) => (
                  <div key={idx} className="text-frost/70 mb-1">
                    <span className="text-muted mr-2">[{new Date(l.timestamp * 1000).toISOString().substring(11, 23)}]</span>
                    <span className={cx(l.level === "ERROR" ? "text-signal" : l.level === "WARN" ? "text-amber" : "text-emerald-300", "mr-2")}>
                      {l.level.padEnd(5)}
                    </span>
                    <span className="text-frost/90">{l.message}</span>
                  </div>
                )) : (
                  <div className="text-muted italic">Waiting for backend logs...</div>
                )}
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </Reveal>
  );
}

export function ChandraExperience() {
  const {
    selectedKRAs,
    predefinedKras,
    customKras,
    agentName,
    employeeId,
    role,
    maturity,
    permissions,
    observations,
    costMetrics,
    setCostMetrics,
    setObservations
  } = useOnboarding();

  // Ref to WorkerActionExecutionCenter — allows HumanReviewQueue to trigger execution
  // without duplicating state or the job-polling logic
  const workerRef = useRef<WorkerActionExecutionCenterHandle>(null);

  const handleExecuteAction = useCallback((action: any) => {
    workerRef.current?.execute(action);
  }, []);

  console.log("🔵 ChandraExperience RENDER - observations:", observations ? `health=${observations.health}` : "null");

  const [observationsSync, setObservationsSync] = useState<ObservationsSyncState>({
    status: observations ? "success" : "loading",
    attempt: 0,
    message: observations ? "Live operational intelligence loaded." : "Retrieving live observability data.",
    nextDelayMs: 0
  });

  const [costDays, setCostDays] = useState(7);
  const isFirstCostFetchRef = useRef(true);
  const observationsInitRef = useRef(false);
  const setCostMetricsRef = useRef(setCostMetrics);
  const setObservationsRef = useRef(setObservations);
  useEffect(() => {
    setCostMetricsRef.current = setCostMetrics;
    setObservationsRef.current = setObservations;
  }, [setCostMetrics, setObservations]);

  useEffect(() => {
    const controller = new AbortController();
    
    // Only clear out data to show the spinner on the very first mount fetch
    if (isFirstCostFetchRef.current) {
      setCostMetricsRef.current(null);
      isFirstCostFetchRef.current = false;
    }
    
    console.log(`FETCHING COST METRICS (${costDays} days)...`);
    fetchCostMetrics(costDays, { signal: controller.signal })
      .then((data) => {
        console.log("COST METRICS SUCCESS", data);
        setCostMetricsRef.current(data);
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        const message = error instanceof Error ? error.message : "Cost metrics request failed";
        console.error("COST METRICS ERROR", message);
        setCostMetricsRef.current(null, message);
      });
    return () => controller.abort();
  }, [costDays]);

  const observationsPayload = useMemo(
    () => ({
      region: "us-east-1",
      kras: buildKraPayload(predefinedKras, customKras),
      selected_kras: selectedKRAs,
      custom_kras: customKras,
      maturity_level: maturity,
      deployment: {
        role,
        permissions,
        agent_name: agentName,
        employee_id: employeeId
      }
    }),
    [agentName, employeeId, role, maturity, permissions, predefinedKras, customKras, selectedKRAs]
  );

  const observationsPayloadRef = useRef(observationsPayload);
  useEffect(() => {
    observationsPayloadRef.current = observationsPayload;
  }, [observationsPayload]);

  const activeKras = useMemo(() => {
    return buildKraPayload(predefinedKras, customKras).map(k => ({
      code: k.code,
      name: kraCodeToName(k.code, customKras)
    }));
  }, [predefinedKras, customKras]);

  useEffect(() => {
    if (observationsInitRef.current) return;
    if (observations) {
      observationsInitRef.current = true;
      setObservationsSync({
        status: "success",
        attempt: 0,
        message: "Live operational intelligence loaded.",
        nextDelayMs: 0
      });
      return;
    }
    observationsInitRef.current = true;

    let cancelled = false;
    let retryTimer: number | null = null;
    let controller: AbortController | null = null;

    async function loadOperationalIntelligence(attempt: number) {
      if (cancelled) return;
      controller?.abort();
      controller = new AbortController();
      setObservationsSync({
        status: attempt === 1 ? "loading" : "retrying",
        attempt,
        message:
          attempt === 1
            ? "Retrieving live observability data from LangGraph."
            : "Waiting for LangGraph response; retrying security analysis.",
        nextDelayMs: 0
      });

      try {
        const data = await fetchAgentObservations(observationsPayloadRef.current, { signal: controller.signal });
        if (cancelled) return;
        setObservationsRef.current(data, null);
        setObservationsSync({
          status: "success",
          attempt,
          message: "Live operational intelligence loaded.",
          nextDelayMs: 0
        });
      } catch (error) {
        if (cancelled || controller.signal.aborted) return;
        const message = error instanceof Error ? error.message : "Operational intelligence request failed";
        const nextDelayMs = Math.min(30_000, 2_000 * 2 ** Math.min(attempt - 1, 4));
        setObservationsRef.current(null, message);
        setObservationsSync({
          status: "retrying",
          attempt,
          message: `${message}. Dashboard remains operational while Chandra retries.`,
          nextDelayMs
        });
        retryTimer = window.setTimeout(() => loadOperationalIntelligence(attempt + 1), nextDelayMs);
      }
    }

    loadOperationalIntelligence(1);

    return () => {
      cancelled = true;
      controller?.abort();
      if (retryTimer) window.clearTimeout(retryTimer);
    };
  }, []);

  const liveEvents: LiveOpsEvent[] = useMemo(
    () => (observations?.issues ? deriveOpsEvents(observations.issues) : []),
    [observations]
  );
  const liveIncidents: LiveIncident[] = useMemo(() => deriveIncidents(liveEvents), [liveEvents]);
  const liveApprovals: LiveApprovalRow[] = useMemo(
    () => (observations?.actions ? deriveApprovals(observations.actions) : []),
    [observations]
  );
  const liveKraEvaluations = useMemo(
    () => (observations?.kra_status ? deriveKraEvaluations(observations.kra_status, customKras) : []),
    [observations, customKras]
  );
  const costBreakdown = useMemo(() => deriveCostBreakdown(costMetrics ?? null), [costMetrics]);
  const liveCostCards = useMemo<LiveCostCard[]>(() => {
    const snapshotCards = observations?.cost_snapshot ? deriveCostCards(observations.cost_snapshot) : [];
    if (snapshotCards.length) return snapshotCards;
    return costBreakdown.services.map((row) => ({
      label: row.service,
      value: `$${row.total.toFixed(2)}`,
      delta: "live",
      tone: "text-frost",
      note: "Derived from live cost metrics service breakdown"
    }));
  }, [observations, costBreakdown]);
  const liveSummary = useMemo(() => summarizeIssuesBySeverity(liveEvents), [liveEvents]);

  const eventsAsOpsEvent: OpsEvent[] = useMemo(
    () => liveEvents.map((entry) => ({ ...entry })),
    [liveEvents]
  );
  const incidentsAsIncident: Incident[] = useMemo(
    () => liveIncidents.map((entry) => ({ ...entry })),
    [liveIncidents]
  );
  const approvalsAsRow: ApprovalRow[] = useMemo(
    () => liveApprovals.map((entry) => ({ ...entry })),
    [liveApprovals]
  );

  const events = useOperationalFeed(eventsAsOpsEvent);
  const auditRows = useMemo(
    () => deriveAuditRowsFromLive(eventsAsOpsEvent, approvalsAsRow, observations?.compliance_summary),
    [eventsAsOpsEvent, approvalsAsRow, observations?.compliance_summary]
  );
  const unread = useMemo(
    () => events.filter((e) => e.severity === "P1" || e.status === "Awaiting Approval" || e.status === "Escalated").length,
    [events]
  );
  const AGENT = agentName || "Chandra";

  const hasInfra = selectedKRAs.includes("Infrastructure Monitoring");
  const hasIncident = selectedKRAs.includes("Incident Detection");
  const hasCost = selectedKRAs.includes("Cost Optimization");
  const hasAudit = selectedKRAs.includes("Audit & Compliance");

  return (
    <main className="bg-obsidian text-frost">
      <CommandHeader liveObservations={observations} liveSummary={liveSummary} sync={observationsSync} />

      {hasCost ? (
      <section className="section-shell">
        <div className="section-inner">
          <CostMonitoring 
            cards={liveCostCards} 
            breakdown={costBreakdown} 
            rawMetrics={costMetrics} 
            costDays={costDays}
            onDaysChange={setCostDays}
          />
        </div>
      </section>
      ) : null}

      <div className="px-5 md:px-10 mb-4 mx-auto max-w-[1480px]">
        <OperationalIntelligencePanel
          permissionsCount={permissions.length}
          liveEvents={liveEvents}
          liveObservations={observations?.observations}
          liveSecurity={observations?.security_posture}
          liveCompliance={observations?.compliance_summary}
          sync={observationsSync}
        />
      </div>

      <section className="section-shell">
        <div className="section-inner">
          <HumanReviewQueue seed={approvalsAsRow} rawActions={observations?.actions} sync={observationsSync} onAutoApproved={handleExecuteAction} />
        </div>
      </section>

      <section className="section-shell">
        <div className="section-inner grid gap-3 lg:grid-cols-2">
          <RealTimeActivityFeed events={events} />
          <LiveOpsStream sync={observationsSync} />
        </div>
      </section>

      <section className="section-shell">
        <div className="section-inner">
          <WorkerActionExecutionCenter ref={workerRef} />
        </div>
      </section>

      {hasIncident ? (
        <section className="section-shell">
          <div className="section-inner">
            <ActiveIncidents source={incidentsAsIncident} sync={observationsSync} />
          </div>
        </section>
      ) : null}

      <KRAMetricsReview activeKras={activeKras} liveEvaluations={liveKraEvaluations} />

      {hasAudit ? (
        <section className="section-shell">
          <div className="section-inner">
            <AuditLogs rows={auditRows} />
          </div>
        </section>
      ) : null}

      <section className="px-5 py-8 md:px-10">
        <div className="mx-auto max-w-[1480px] border-t border-white/10 pt-6">
          <div className="flex flex-wrap items-center justify-between gap-3 text-[0.6rem] uppercase tracking-[0.24em] text-muted">
              <div className="flex items-center gap-2">
                <ShieldCheck size={13} className="text-emerald-300" />
                Observable · Accountable · Supervised
              </div>
              <div>{AGENT} · Enterprise AI Workforce System · L3 Human-Supervised</div>
          </div>
        </div>
      </section>

      <OperationsCopilot latestEvent={events[0]} unread={unread} />
    </main>
  );
}

function downloadBlob(content: string | Blob, filename: string, mime: string) {
  const blob = typeof content === "string" ? new Blob([content], { type: mime }) : content;
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

function escapeCsv(value: string | number) {
  const text = String(value);
  if (text.includes('"') || text.includes(",") || text.includes("\n")) {
    return `"${text.replace(/"/g, '""')}"`;
  }
  return text;
}

function exportCsv(rows: AuditRow[]) {
  const header = ["timestamp", "incident_id", "remediation", "account", "ai_confidence", "reviewer", "compliance", "evidence_pack"];
  const body = rows.map((row) =>
    [row.timestamp, row.incidentId, row.remediation, row.account, row.confidence, row.reviewer, row.compliance, row.evidencePack]
      .map(escapeCsv)
      .join(",")
  );
  downloadBlob([header.join(","), ...body].join("\n"), `chandra-audit-${Date.now()}.csv`, "text/csv;charset=utf-8");
}

function exportXlsx(rows: AuditRow[]) {
  const header = ["Timestamp", "Incident ID", "Remediation", "Account", "AI Confidence", "Reviewer", "Compliance", "Evidence Pack"];
  const headerXml = header.map((cell) => `<Cell><Data ss:Type="String">${cell}</Data></Cell>`).join("");
  const rowsXml = rows
    .map((row) => {
      const cells = [
        `<Cell><Data ss:Type="String">${row.timestamp}</Data></Cell>`,
        `<Cell><Data ss:Type="String">${row.incidentId}</Data></Cell>`,
        `<Cell><Data ss:Type="String">${row.remediation}</Data></Cell>`,
        `<Cell><Data ss:Type="String">${row.account}</Data></Cell>`,
        `<Cell><Data ss:Type="Number">${row.confidence}</Data></Cell>`,
        `<Cell><Data ss:Type="String">${row.reviewer}</Data></Cell>`,
        `<Cell><Data ss:Type="String">${row.compliance}</Data></Cell>`,
        `<Cell><Data ss:Type="String">${row.evidencePack}</Data></Cell>`
      ].join("");
      return `<Row>${cells}</Row>`;
    })
    .join("");
  const xml = `<?xml version="1.0"?>\n<?mso-application progid="Excel.Sheet"?>\n<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">\n  <Worksheet ss:Name="Chandra Audit">\n    <Table>\n      <Row>${headerXml}</Row>\n      ${rowsXml}\n    </Table>\n  </Worksheet>\n</Workbook>`;
  downloadBlob(xml, `chandra-audit-${Date.now()}.xls`, "application/vnd.ms-excel");
}

function exportPdf(rows: AuditRow[]) {
  const rowsHtml = rows
    .map(
      (row) => `
      <tr>
        <td>${row.timestamp}</td>
        <td>${row.incidentId}</td>
        <td>${row.remediation}</td>
        <td>${row.account}</td>
        <td>${row.confidence}%</td>
        <td>${row.reviewer}</td>
        <td>${row.compliance}</td>
        <td>${row.evidencePack}</td>
      </tr>`
    )
    .join("");
  const doc = `<!doctype html><html><head><title>Chandra Audit Export</title>\n<style>\n  body{font-family:ui-monospace,Consolas,monospace;color:#111;padding:24px;}\n  h1{font-size:18px;margin:0 0 8px;}\n  .meta{color:#555;font-size:11px;margin-bottom:18px;}\n  table{width:100%;border-collapse:collapse;font-size:11px;}\n  th,td{border-bottom:1px solid #ddd;padding:6px 8px;text-align:left;}\n  th{background:#f4f4f4;text-transform:uppercase;font-size:9px;letter-spacing:1px;}\n  @media print{button{display:none;}}\n</style></head><body>\n  <h1>Chandra Audit Trail — Evidence Export</h1>\n  <div class="meta">Generated ${new Date().toISOString()} · Records: ${rows.length} · L3 Human-Supervised AI Digital Worker</div>\n  <button onclick="window.print()" style="margin-bottom:12px;padding:6px 10px;font-size:11px;">Print / Save as PDF</button>\n  <table>\n    <thead><tr><th>Timestamp</th><th>Incident</th><th>Remediation</th><th>Account</th><th>Conf</th><th>Reviewer</th><th>Compliance</th><th>Evidence</th></tr></thead>\n    <tbody>${rowsHtml}</tbody>\n  </table>\n  <script>setTimeout(function(){window.print();},250);</script>\n</body></html>`;
  const printWindow = window.open("", "_blank");
  if (printWindow) {
    printWindow.document.open();
    printWindow.document.write(doc);
    printWindow.document.close();
  } else {
    downloadBlob(doc, `chandra-audit-${Date.now()}.html`, "text/html;charset=utf-8");
  }
}
