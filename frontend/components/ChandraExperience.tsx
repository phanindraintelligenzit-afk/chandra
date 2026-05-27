"use client";

import { useOnboarding } from "@/store/OnboardingContext";
import { getAvatarById, getAvatarImageSrc, type AgentAvatar } from "@/store/agentProfile";
import { getKraMetric } from "@/store/kraCatalog";
import { fetchAgentObservations, fetchCostMetrics, sendCopilotMessage, type CopilotChatMessage } from "@/services/api";
import {
  buildKraPayload,
  deriveApprovals,
  deriveCostBreakdown,
  deriveCostCards,
  deriveIncidents,
  deriveKraEvaluations,
  deriveOpsEvents,
  healthTone,
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
  YAxis
} from "recharts";
import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

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
                [String(liveSummary.p1 || 0), "P1 ACTIVE"],
                [String(liveSummary.total || 0), "OPEN ISSUES"],
                [String(liveObservations?.actions?.length ?? 0), "PENDING ACTIONS"],
                [health, "HEALTH"],
                [String(liveObservations?.kra_status?.length ?? 0), "KRAS EVALUATED"]
              ].map(([value, label]) => (
                <div key={label} className="rounded-2xl border border-white/10 bg-black/30 px-3 py-2">
                  <div className="text-base text-frost">{value}</div>
                  <div className="mt-1 text-[0.54rem] uppercase tracking-[0.16em] text-muted">{label}</div>
                </div>
              ))}
            </div>
          </div>
        </Reveal>
        <OperationalIntelligencePanel
          permissionsCount={permissions.length}
          liveEvents={liveOpsForPanel}
          liveObservations={liveObservations?.observations}
          liveSecurity={liveObservations?.security_posture}
          liveCompliance={liveObservations?.compliance_summary}
          sync={sync}
        />
      </div>
    </section>
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

  const tone = {
    P1: "text-signal",
    P2: "text-amber",
    P3: "text-frost/70",
    P4: "text-frost/70"
  } as const;

  const liveObservationRows: Array<[string, string]> =
    liveObservations && liveObservations.length
      ? liveObservations.map((text, index) => [
          index === 0 ? "Observation" : index === 1 ? "Telemetry signal" : "Operational note",
          text
        ])
      : [];

  const observationRows = liveObservationRows;
  const securityRows = liveSecurity && liveSecurity.length ? liveSecurity : [];
  const complianceText = liveCompliance && liveCompliance.length ? liveCompliance : `${permissionsCount || 0} governed access scopes active.`;

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
      <div className="grid gap-3 lg:grid-cols-[1.35fr_1fr]">
        <div className="operational-scroll max-h-[330px] pr-1">
          {alerts.length ? (
            <div className="grid gap-3 sm:grid-cols-2">
              {alerts.map((alert) => (
                <div key={alert.id} className="rounded-3xl border border-white/10 bg-black/25 p-3">
                  <div className="flex items-center justify-between gap-3 text-[0.7rem] uppercase tracking-[0.16em] text-muted">
                    <span className={tone[alert.severity]}>{alert.severity}</span>
                    <span>{alert.time}</span>
                  </div>
                  <div className="mt-3 text-sm text-frost">{alert.text}</div>
                  {alert.service || alert.region || alert.resourceId ? (
                    <div className="mt-2 flex flex-wrap gap-1.5 text-[0.55rem] uppercase tracking-[0.16em] text-muted">
                      {alert.service ? <span className="border border-white/10 bg-white/[0.03] px-1.5 py-0.5 text-frost/80">{alert.service}</span> : null}
                      {alert.region ? <span className="border border-amber/30 bg-amber/8 px-1.5 py-0.5 text-amber">{alert.region}</span> : null}
                      {alert.resourceId ? <span className="border border-signal/30 bg-signal/10 px-1.5 py-0.5 text-signal normal-case tracking-[0.1em]">{alert.resourceId}</span> : null}
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          ) : (
            <OperationalSkeleton label={sync.status === "success" ? "No live incidents returned" : "Retrieving live alerts"} />
          )}
        </div>
        <div className="operational-scroll max-h-[330px] pr-1">
          <div className="grid gap-3">
            {observationRows.length ? observationRows.map(([label, text], index) => (
              <div key={`${label}-${index}`} className="rounded-3xl border border-white/10 bg-black/25 p-3">
                <div className="text-[0.62rem] uppercase tracking-[0.2em] text-amber">{label}</div>
                <div className="mt-2 text-sm leading-5 text-frost/80">{text}</div>
              </div>
            )) : <OperationalSkeleton label={sync.status === "success" ? "No live observations returned" : "Waiting for LangGraph response"} />}
            {securityRows.length > 0 ? (
              <div className="rounded-3xl border border-signal/25 bg-signal/8 p-3">
              <div className="flex items-center gap-2 text-[0.62rem] uppercase tracking-[0.18em] text-signal">
                <ShieldCheck size={13} /> Security findings
              </div>
              <ul className="mt-2 space-y-1 text-sm text-frost/85">
                {securityRows.map((row, index) => (
                  <li key={`security-${index}`} className="leading-5">{row}</li>
                ))}
              </ul>
              </div>
            ) : null}
            <div className="rounded-3xl border border-emerald-300/20 bg-emerald-300/8 p-3">
              <div className="flex items-center gap-2 text-[0.62rem] uppercase tracking-[0.18em] text-emerald-300">
                <ShieldCheck size={13} /> Compliance posture
              </div>
              <div className="mt-2 text-sm text-frost/85 leading-5">{complianceText}</div>
            </div>
          </div>
        </div>
      </div>
    </Reveal>
  );
}

function OperationalWaveform() {
  return (
    <Reveal className="glass overflow-hidden p-4">
      <SectionHead label="OPERATIONAL WAVEFORM" sub="24h telemetry" />
      <div className="grid gap-4 lg:grid-cols-[1.7fr_1fr]">
        <div className="h-44 min-h-[176px]">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={trendData.concat(trendData)} margin={{ top: 6, right: 10, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="rgba(255,255,255,0.06)" vertical={false} />
              <XAxis dataKey="t" tick={{ fill: "#928a80", fontSize: 10 }} axisLine={false} tickLine={false} />
              <YAxis hide />
              <Tooltip contentStyle={{ background: "#0d0d0f", border: "1px solid rgba(255,255,255,0.14)", fontSize: 12 }} />
              <Line type="monotone" dataKey="score" stroke="#ff3b30" strokeWidth={1.8} dot={false} name="Incident Activity" />
              <Line type="monotone" dataKey="kra" stroke="#8ed9a8" strokeWidth={1.6} dot={false} name="Stable Operations" />
              <Line type="monotone" dataKey="risk" stroke="#ffb347" strokeWidth={1.4} dot={false} name="Background Load" />
            </LineChart>
          </ResponsiveContainer>
        </div>
        <div className="flex flex-col justify-center gap-2 text-[0.7rem]">
          <div className="text-[0.6rem] uppercase tracking-[0.2em] text-muted">Legend</div>
          {[
            ["#ff3b30", "Incident Activity", "spike = active remediation"],
            ["#8ed9a8", "Stable Operations", "baseline KRA performance"],
            ["#ffb347", "Background Load", "ambient cloud workload"]
          ].map(([color, label, hint]) => (
            <div key={label as string} className="flex items-center gap-2 border-l border-white/8 pl-2">
              <span className="h-2 w-3" style={{ background: color }} />
              <div>
                <div className="text-frost">{label}</div>
                <div className="text-[0.58rem] uppercase tracking-[0.16em] text-muted">{hint}</div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </Reveal>
  );
}

function CostMonitoring({
  cards,
  breakdown
}: {
  cards?: LiveCostCard[];
  breakdown?: ReturnType<typeof deriveCostBreakdown>;
}) {
  const displayCards = cards && cards.length
    ? cards.map((card) => ({ label: card.label, value: card.value, delta: card.delta, tone: card.tone, note: card.note }))
    : [];

  const regionRows = breakdown?.regions ?? [];
  const serviceRows = breakdown?.services ?? [];
  const hasBreakdown = Boolean(breakdown && (breakdown.regions.length || breakdown.services.length));
  const sub = breakdown?.window
    ? `FinOps · ${breakdown.window}`
    : "FinOps - live spend";

  return (
    <Reveal className="glass overflow-hidden p-2">
      <SectionHead label="COST MONITORING" sub={sub} />
      <div className="flex gap-3 overflow-x-auto scrollbar-mini pb-1">
        {displayCards.length ? (
          displayCards.map((card) => (
            <div key={card.label} className="kpi-card px-3 py-2 min-w-[160px]" title={card.note ?? ""}>
              <span className="kpi-label">{card.label}</span>
              <span className="kpi-value">{card.value}</span>
              <span className={cx("kpi-delta", card.tone)}>{card.delta}</span>
            </div>
          ))
        ) : (
          <div className="min-w-full">
            <EmptyOperationalState label="No live cost snapshot returned" />
          </div>
        )}
      </div>
      {hasBreakdown ? (
        <div className="mt-3 grid gap-3 md:grid-cols-2 px-1">
          <div className="rounded-2xl border border-white/10 bg-black/25 p-3">
            <div className="text-[0.6rem] uppercase tracking-[0.18em] text-amber">REGION SPEND</div>
            <ul className="operational-scroll mt-2 max-h-[170px] space-y-1 pr-1 text-[0.72rem]">
              {regionRows.map((row) => (
                <li key={row.region} className="flex items-center justify-between border-b border-white/5 py-1 last:border-b-0">
                  <span className="text-frost">{row.region}</span>
                  <span className="text-frost/80">${row.total.toFixed(2)}</span>
                </li>
              ))}
            </ul>
          </div>
          <div className="rounded-2xl border border-white/10 bg-black/25 p-3">
            <div className="text-[0.6rem] uppercase tracking-[0.18em] text-amber">TOP SERVICES</div>
            <ul className="operational-scroll mt-2 max-h-[170px] space-y-1 pr-1 text-[0.72rem]">
              {serviceRows.map((row) => (
                <li key={row.service} className="flex items-center justify-between border-b border-white/5 py-1 last:border-b-0">
                  <span className="text-frost truncate pr-2" title={row.service}>{row.service}</span>
                  <span className="text-frost/80">${row.total.toFixed(2)}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}
    </Reveal>
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

function LiveOpsStream({ events, sync }: { events: OpsEvent[]; sync: ObservationsSyncState }) {
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const prioritized = useMemo(() => {
    return [...events].sort((a, b) => severityRank(b.severity) - severityRank(a.severity) || b.time.localeCompare(a.time));
  }, [events]);

  const severityBorder: Record<Severity, string> = {
    P1: "border-l-signal",
    P2: "border-l-amber",
    P3: "border-l-white/30",
    P4: "border-l-emerald-300/60"
  };

  return (
    <Reveal className="glass overflow-hidden p-4">
      <SectionHead
        label="LIVE OPS STREAM"
        sub="websocket · 5 latest"
        action={
          <button className="flex items-center gap-2 border border-white/15 bg-white/[0.04] px-2.5 py-1 text-[0.6rem] uppercase tracking-[0.18em] text-frost/85 hover:border-signal/40 hover:text-signal">
            <Terminal size={12} /> View Full Audit Stream
          </button>
        }
      />
      <div className="feed-stream max-h-[300px] space-y-1.5 overflow-y-auto pr-1">
        {prioritized.length ? (
          <AnimatePresence initial={false}>
            {prioritized.map((event) => {
              const open = expandedId === event.id;
              return (
              <motion.div
                key={event.id}
                layout
                initial={{ opacity: 0, y: -10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.3 }}
                onClick={() => setExpandedId(open ? null : event.id)}
                className={cx(
                  "cursor-pointer border-l-2 bg-white/[0.025] px-3 py-2 text-[0.72rem] transition hover:bg-white/[0.05]",
                  severityBorder[event.severity]
                )}
              >
                <div className="flex items-center gap-3">
                  <span className="text-amber/90">[{event.time}]</span>
                  <SeverityPill value={event.severity} />
                  <span className="flex-1 truncate text-frost">{event.incident}</span>
                  <span className="hidden text-muted md:inline">{event.service}</span>
                  <span className="text-emerald-300">{event.confidence}%</span>
                  <StatusDot status={event.status} />
                </div>
                <AnimatePresence>
                  {open ? (
                    <motion.div
                      initial={{ opacity: 0, height: 0 }}
                      animate={{ opacity: 1, height: "auto" }}
                      exit={{ opacity: 0, height: 0 }}
                      className="mt-2 grid gap-1 border-l border-white/8 pl-3 text-[0.66rem] text-muted"
                    >
                      <span><span className="text-frost/70">Account:</span> {event.account}</span>
                      <span><span className="text-frost/70">Resolution:</span> {event.resolution}</span>
                    </motion.div>
                  ) : null}
                </AnimatePresence>
              </motion.div>
              );
            })}
          </AnimatePresence>
        ) : (
          <OperationalSkeleton label={sync.status === "success" ? "No live operational feed events returned" : "Streaming operational events"} />
        )}
      </div>
      <div className="mt-2 flex items-center justify-between border-t border-white/8 pt-2 text-[0.58rem] uppercase tracking-[0.18em] text-muted">
        <span>showing {prioritized.length} live events</span>
        <span className="text-emerald-300">context synced to copilot</span>
      </div>
    </Reveal>
  );
}

function KRAMetricsReview({
  selectedKRAs,
  liveEvaluations
}: {
  selectedKRAs: string[];
  liveEvaluations?: LiveKraEvaluation[];
}) {
  const evalByName = new Map<string, LiveKraEvaluation>();
  (liveEvaluations ?? []).forEach((entry) => evalByName.set(entry.name, entry));

  const selected = selectedKRAs.map((kra) => ({
    name: kra,
    metric: getKraMetric(kra),
    live: evalByName.get(kra) ?? null
  }));

  if (!selected.length) return null;

  return (
    <section className="section-shell">
      <div className="section-inner">
        <SectionHead label="SELECTED KRA SUMMARY" sub="Capability-driven operational review" />
        <Reveal>
          <div className="grid gap-3 lg:grid-cols-2">
            {selected.map(({ name, metric, live }) => {
              const status = live?.status ?? "Active";
              const statusTone = live ? live.tone : metric.tone;
              const detail = live?.achievement ?? metric.detail;
              const note = live?.note;
              return (
                <div key={name} className="glass border border-white/10 p-4">
                  <div className="flex items-center justify-between gap-3">
                    <div>
                      <div className="text-[0.62rem] uppercase tracking-[0.22em] text-amber">{live?.code ? `${live.code} · ${name}` : name}</div>
                      <div className="mt-2 text-base font-semibold uppercase tracking-[0.04em] text-frost">{metric.subtitle}</div>
                    </div>
                    <div className={`rounded-full border border-white/10 bg-white/5 px-3 py-1 text-[0.65rem] uppercase tracking-[0.16em] ${statusTone}`}>
                      {status}
                    </div>
                  </div>
                  <div className="mt-3 grid gap-2 sm:grid-cols-3">
                    <div className="rounded-2xl border border-white/10 bg-black/30 p-3 text-[0.72rem]">
                      <div className="uppercase tracking-[0.16em] text-muted">Metric</div>
                      <div className="mt-1 text-frost">{metric.value}</div>
                    </div>
                    <div className="rounded-2xl border border-white/10 bg-black/30 p-3 text-[0.72rem] sm:col-span-2">
                      <div className="uppercase tracking-[0.16em] text-muted">Insight</div>
                      <div className="mt-1 text-frost">{detail}</div>
                      {note ? <div className="mt-2 text-[0.65rem] text-muted">{note}</div> : null}
                    </div>
                  </div>
                  <div className="mt-2 grid gap-2 sm:grid-cols-4">
                    {[
                      ["Target", metric.target],
                      ["Actual", metric.actual],
                      ["Confidence", `${metric.confidence}%`],
                      ["Automation", `${metric.automation}%`]
                    ].map(([label, value]) => (
                      <div key={`${name}-${label}`} className="rounded-2xl border border-white/10 bg-black/30 p-3 text-[0.68rem]">
                        <div className="uppercase tracking-[0.16em] text-muted">{label}</div>
                        <div className="mt-1 text-frost">{value}</div>
                      </div>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </Reveal>
      </div>
    </section>
  );
}

function ActiveIncidents({ source, sync }: { source?: Incident[]; sync: ObservationsSyncState }) {
  const [query, setQuery] = useState("");
  const [severity, setSeverity] = useState("All");
  const [status, setStatus] = useState("All");
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const rows = Array.isArray(source) ? source : [];
  const filtered = rows.filter((incident) => {
    const text = `${incident.incident} ${incident.account} ${incident.service} ${incident.rootCause}`.toLowerCase();
    return (
      text.includes(query.toLowerCase()) &&
      (severity === "All" || incident.severity === severity) &&
      (status === "All" || incident.status === status)
    );
  });

  return (
    <Reveal className="glass overflow-hidden p-4">
      <SectionHead label="ACTIVE INCIDENTS" sub={`${filtered.length} matched · supervised`} />
      <div className="mb-3 grid gap-2 lg:grid-cols-[1fr_120px_160px]">
        <label className="flex items-center gap-2 border border-white/12 bg-black/35 px-3 py-1.5 text-[0.7rem] text-muted">
          <Search size={13} />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            className="w-full bg-transparent text-frost outline-none placeholder:text-muted"
            placeholder="Search incidents, accounts, root cause"
          />
        </label>
        {[
          ["Severity", severity, setSeverity, ["All", "P1", "P2", "P3", "P4"]],
          ["Status", status, setStatus, ["All", "Resolved", "Investigating", "Escalated", "Monitoring", "Awaiting Approval"]]
        ].map(([label, value, setter, options]) => (
          <label key={label as string} className="flex items-center gap-1.5 border border-white/12 bg-black/35 px-2 py-1.5 text-[0.6rem] uppercase tracking-[0.14em] text-muted">
            <Filter size={11} />
            <select
              value={value as string}
              onChange={(event) => (setter as (value: string) => void)(event.target.value)}
              className="w-full bg-transparent text-frost outline-none"
            >
              {(options as string[]).map((option) => (
                <option key={option} value={option} className="bg-carbon text-frost">{option}</option>
              ))}
            </select>
          </label>
        ))}
      </div>
      <div className="max-h-[360px] overflow-auto scrollbar-mini">
        {filtered.length ? (
        <table className="w-full min-w-[1000px] border-collapse text-left text-[0.72rem]">
          <thead className="sticky top-0 z-10 bg-black/85 text-[0.58rem] uppercase tracking-[0.18em] text-muted backdrop-blur">
            <tr className="border-b border-white/12">
              {[
                "Sev",
                "Incident",
                "Account",
                "Service",
                "Approval",
                "Reviewer",
                "Lock",
                "Escalation",
                "Status",
                "Conf",
                "Triage",
                "ETA",
                ""
              ].map((head) => (
                <th key={head} className="px-2 py-2 font-normal">{head}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {filtered.map((incident) => {
              const open = expandedId === incident.id;
              return (
                <Fragment key={incident.id}>
                  <tr
                    onClick={() => setExpandedId(open ? null : incident.id)}
                    className="cursor-pointer border-b border-white/8 align-top text-frost/85 hover:bg-white/[0.03]"
                  >
                    <td className="px-2 py-2"><SeverityPill value={incident.severity} /></td>
                    <td className="px-2 py-2 text-frost">{incident.incident}</td>
                    <td className="px-2 py-2 text-muted">{incident.account}</td>
                    <td className="px-2 py-2 text-muted">{incident.service}</td>
                    <td className="px-2 py-2"><ApprovalBadge state={incident.approvalState} /></td>
                    <td className="px-2 py-2 text-muted">{incident.reviewer}</td>
                    <td className="px-2 py-2 text-muted">{incident.lockState}</td>
                    <td className="px-2 py-2 text-muted">{incident.escalation}</td>
                    <td className="px-2 py-2"><StatusDot status={incident.status} /></td>
                    <td className="px-2 py-2" title={`AI confidence: ${incident.confidence}%`}>
                      <span className={cx(incident.confidence >= 90 ? "text-emerald-300" : "text-amber")}>{incident.confidence}%</span>
                    </td>
                    <td className="px-2 py-2 text-muted">{incident.triage}</td>
                    <td className="px-2 py-2 text-muted">{incident.eta}</td>
                    <td className="px-2 py-2"><ChevronDown size={12} className={cx("text-muted transition", open && "rotate-180")} /></td>
                  </tr>
                  {open ? (
                    <tr className="border-b border-white/8 bg-black/40">
                      <td colSpan={13} className="px-3 py-2 text-[0.7rem]">
                        <div className="grid gap-2 md:grid-cols-3">
                          <div>
                            <div className="text-[0.56rem] uppercase tracking-[0.2em] text-muted">Root Cause</div>
                            <div className="mt-1 text-frost/85">{incident.rootCause}</div>
                          </div>
                          <div>
                            <div className="text-[0.56rem] uppercase tracking-[0.2em] text-muted">Resolution</div>
                            <div className="mt-1 text-frost/85">{incident.resolution}</div>
                          </div>
                          <div>
                            <div className="text-[0.56rem] uppercase tracking-[0.2em] text-muted">Human Escalation</div>
                            <div className="mt-1 text-amber">{incident.humanEscalation}</div>
                          </div>
                        </div>
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              );
            })}
          </tbody>
        </table>
        ) : (
          <OperationalSkeleton label={sync.status === "success" ? "No live incidents matched" : "Retrieving incident analysis"} />
        )}
      </div>
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

function HumanReviewQueue({ seed }: { seed?: ApprovalRow[] }) {
  const [approvals, setApprovals] = useState<ApprovalRow[]>(Array.isArray(seed) ? seed : []);
  useEffect(() => {
    setApprovals(Array.isArray(seed) ? seed : []);
  }, [seed]);
  const pendingApprovals = approvals.filter((row) => row.state === "Awaiting Review" || row.state === "Escalated");
  const visibleApprovals = approvals;

  function markApproval(id: string, nextState: ApprovalState) {
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
      <SectionHead label="HUMAN APPROVAL CENTER" sub="High-risk review queue" />
      <div className="flex items-center justify-between gap-3 mb-2">
        <div className="flex items-center gap-3">
          <div className="rounded-full bg-amber/20 px-3 py-1 text-[0.8rem] font-semibold uppercase tracking-[0.08em] text-amber">{pendingApprovals.length} PENDING</div>
          <div className="text-[0.72rem] uppercase tracking-[0.12em] text-muted">OPERATIONAL REVIEW QUEUE</div>
        </div>
        <div className="flex items-center gap-2">
          {(["Awaiting Review", "Approved", "Rejected", "Escalated"] as ApprovalState[]).map((s) => (
            <div key={s} className="kpi-card px-2 py-1 text-[0.68rem]">{s}: {approvals.filter((r) => r.state === s).length}</div>
          ))}
        </div>
      </div>

      <div className="operational-scroll flex max-h-[360px] gap-3 overflow-x-auto py-1">
        {visibleApprovals.length ? (
          visibleApprovals.map((approval) => (
            <div key={approval.id} className="glass flex-shrink-0 w-[340px] border border-white/10 p-3">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2 text-[0.65rem] uppercase tracking-[0.14em] text-amber">
                    <span>{approval.id}</span>
                    <ApprovalBadge state={approval.state} />
                    {approval.kraCode ? (
                      <span className="border border-white/15 bg-white/[0.04] px-1.5 py-0.5 text-[0.55rem] tracking-[0.16em] text-frost/80">
                        {approval.kraCode}
                      </span>
                    ) : null}
                  </div>
                  <div className="mt-1 text-sm font-semibold text-frost">{approval.incident}</div>
                  <div className="mt-1 text-[0.68rem] text-muted">{approval.account} · {approval.severity} · {approval.requested}</div>
                </div>
                <div className="flex flex-col gap-2 w-[110px]">
                  <button onClick={() => markApproval(approval.id, "Approved")} className="rounded-md border border-emerald-300/30 bg-emerald-300/10 px-2 py-1 text-[0.68rem] uppercase tracking-[0.08em] text-emerald-200">Approve</button>
                  <button onClick={() => markApproval(approval.id, "Rejected")} className="rounded-md border border-signal/30 bg-signal/10 px-2 py-1 text-[0.68rem] uppercase tracking-[0.08em] text-signal">Reject</button>
                  <button onClick={() => markApproval(approval.id, "Escalated")} className="rounded-md border border-white/10 bg-white/[0.03] px-2 py-1 text-[0.68rem] uppercase tracking-[0.08em] text-frost">Escalate</button>
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
    </Reveal>
  );
}

function AuditLogs({ rows }: { rows?: AuditRow[] }) {
  const [query, setQuery] = useState("");
  const [timeWindow, setTimeWindow] = useState<"hourly" | "daily" | "weekly" | "monthly">("daily");
  const sourceRows = Array.isArray(rows) ? rows : [];

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
      <SectionHead
        label="AUDIT TRAIL"
        sub={`${filtered.length} records · ${timeWindow}`}
        action={
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
        }
      />
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
      <div className="max-h-[300px] overflow-auto scrollbar-mini">
        {filtered.length ? (
        <table className="w-full min-w-[1000px] border-collapse text-left text-[0.7rem]">
          <thead className="sticky top-0 z-10 bg-black/85 text-[0.56rem] uppercase tracking-[0.18em] text-muted backdrop-blur">
            <tr className="border-b border-white/12">
              {["Timestamp", "Incident", "Remediation", "Account", "Conf", "Reviewer", "Compliance", "Evidence"].map((h) => (
                <th key={h} className="px-2 py-2 font-normal">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {filtered.map((row) => (
              <tr key={row.incidentId} className="border-b border-white/8 text-frost/85">
                <td className="px-2 py-2 text-muted">{row.timestamp}</td>
                <td className="px-2 py-2 text-frost">{row.incidentId}</td>
                <td className="px-2 py-2 text-muted">{row.remediation}</td>
                <td className="px-2 py-2 text-muted">{row.account}</td>
                <td className="px-2 py-2 text-emerald-300">{row.confidence}%</td>
                <td className="px-2 py-2 text-muted">{row.reviewer}</td>
                <td className="px-2 py-2 text-amber">{row.compliance}</td>
                <td className="px-2 py-2 text-muted">{row.evidencePack}</td>
              </tr>
            ))}
          </tbody>
        </table>
        ) : (
          <EmptyOperationalState label="No live audit records matched" />
        )}
      </div>
    </Reveal>
  );
}

function InfrastructureHealth() {
  return (
    <Reveal className="glass overflow-hidden p-4">
      <SectionHead label="INFRASTRUCTURE HEALTH" sub="System uptime · EC2 metrics" />
      <div className="grid gap-4 lg:grid-cols-[1.3fr_0.7fr]">
        <div className="rounded-3xl border border-white/10 bg-black/25 p-4">
          <div className="text-sm uppercase tracking-[0.18em] text-muted">EC2 UTILIZATION</div>
          <div className="mt-3 text-3xl font-semibold text-frost">72%</div>
          <div className="mt-2 text-sm text-frost/70">Average CPU utilization across active worker instances.</div>
        </div>
        <div className="rounded-3xl border border-white/10 bg-black/25 p-4">
          <div className="text-sm uppercase tracking-[0.18em] text-muted">UPTIME</div>
          <div className="mt-3 text-3xl font-semibold text-frost">99.98%</div>
          <div className="mt-2 text-sm text-frost/70">Cloud service availability with integrated automated health checks.</div>
        </div>
      </div>
      <div className="mt-4 h-52 min-h-[208px] rounded-3xl border border-white/10 bg-black/20 p-3">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={trendData} margin={{ top: 10, right: 12, bottom: 8, left: 0 }}>
            <defs>
              <linearGradient id="cpuGradient" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#8ed9a8" stopOpacity={0.65} />
                <stop offset="100%" stopColor="#8ed9a8" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke="rgba(255,255,255,0.06)" vertical={false} />
            <XAxis dataKey="t" tick={{ fill: "#928a80", fontSize: 10 }} axisLine={false} tickLine={false} />
            <YAxis hide />
            <Tooltip contentStyle={{ background: "#0d0d0f", border: "1px solid rgba(255,255,255,0.14)", fontSize: 12 }} />
            <Area type="monotone" dataKey="kra" stroke="#8ed9a8" fill="url(#cpuGradient)" strokeWidth={2} />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </Reveal>
  );
}

function DeploymentInsights() {
  return (
    <Reveal className="glass overflow-hidden p-4">
      <SectionHead label="DEPLOYMENT INTELLIGENCE" sub="Release stability · rollback readiness" />
      <div className="grid gap-4 md:grid-cols-2">
        <div className="rounded-3xl border border-white/10 bg-black/25 p-4">
          <div className="text-sm uppercase tracking-[0.18em] text-muted">RELEASE SUCCESS</div>
          <div className="mt-3 text-3xl font-semibold text-frost">98.6%</div>
          <div className="mt-2 text-sm text-frost/70">Stable deployment completion across the last 15 windows.</div>
        </div>
        <div className="rounded-3xl border border-white/10 bg-black/25 p-4">
          <div className="text-sm uppercase tracking-[0.18em] text-muted">ROLLBACK ALERTS</div>
          <div className="mt-3 text-3xl font-semibold text-frost">2</div>
          <div className="mt-2 text-sm text-frost/70">Deployments flagged for rollback review by the AI worker.</div>
        </div>
      </div>
      <div className="mt-4 rounded-3xl border border-white/10 bg-black/20 p-4 text-sm text-frost/70">
        <div className="mb-3 uppercase tracking-[0.18em] text-muted">RECENT DEPLOYMENT LOG</div>
        <ul className="space-y-2">
          <li>10:42 · Release pipeline passed all health gates.</li>
          <li>11:08 · Canary rollout completed with no incidents.</li>
          <li>12:15 · Auto-rollback signal set on latency spike.</li>
        </ul>
      </div>
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
                  <div className="leading-5 text-frost/88">{message.text}</div>
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
  const [observationsSync, setObservationsSync] = useState<ObservationsSyncState>({
    status: observations ? "success" : "loading",
    attempt: 0,
    message: observations ? "Live operational intelligence loaded." : "Retrieving live observability data.",
    nextDelayMs: 0
  });

  const costMetricsInitRef = useRef(false);
  const observationsInitRef = useRef(false);
  const setCostMetricsRef = useRef(setCostMetrics);
  const setObservationsRef = useRef(setObservations);
  useEffect(() => {
    setCostMetricsRef.current = setCostMetrics;
    setObservationsRef.current = setObservations;
  }, [setCostMetrics, setObservations]);

  useEffect(() => {
    if (costMetricsInitRef.current) return;
    if (costMetrics) return;
    costMetricsInitRef.current = true;
    const controller = new AbortController();
    console.log("FETCHING COST METRICS (once)...");
    fetchCostMetrics({ signal: controller.signal })
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
  }, []);

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

  useEffect(() => {
    if (observationsInitRef.current) return;
    if (observations) {
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
      console.log(`DASHBOARD SYNC ATTEMPT ${attempt}`, observationsPayloadRef.current);
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
        console.log("DASHBOARD SYNC SUCCESS", data);
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
        console.error(`DASHBOARD SYNC ERROR (Attempt ${attempt})`, message);
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
  const hasDeploy = selectedKRAs.includes("Deployment Intelligence");
  const hasAudit = selectedKRAs.includes("Audit & Compliance");

  return (
    <main className="bg-obsidian text-frost">
      <CommandHeader liveObservations={observations} liveSummary={liveSummary} sync={observationsSync} />

      <section className="section-shell">
        <div className="section-inner grid gap-3 lg:grid-cols-12">
          <div className={`lg:col-span-7 ${hasCost ? "" : "opacity-60"}`}>
            <CostMonitoring cards={liveCostCards} breakdown={costBreakdown} />
          </div>
          <div className="lg:col-span-5"><HumanReviewQueue seed={approvalsAsRow} /></div>
        </div>
      </section>

      {(hasInfra || hasIncident || hasDeploy) ? (
        <section className="section-shell">
          <div className="section-inner grid gap-3 lg:grid-cols-12">
            <div className="lg:col-span-7 space-y-3">
              {hasInfra ? <InfrastructureHealth /> : null}
            </div>
            <div className="lg:col-span-5 space-y-3">
              {hasIncident ? <LiveOpsStream events={events} sync={observationsSync} /> : null}
              {hasDeploy ? <DeploymentInsights /> : null}
            </div>
          </div>
        </section>
      ) : null}

      {hasIncident ? (
        <section className="section-shell">
          <div className="section-inner grid gap-3 lg:grid-cols-12">
            <div className="lg:col-span-12"><ActiveIncidents source={incidentsAsIncident} sync={observationsSync} /></div>
          </div>
        </section>
      ) : null}

      <KRAMetricsReview selectedKRAs={selectedKRAs} liveEvaluations={liveKraEvaluations} />

      {hasAudit ? (
        <section className="section-shell">
          <div className="section-inner grid gap-3 lg:grid-cols-12">
            <div className="lg:col-span-12"><AuditLogs rows={auditRows} /></div>
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
