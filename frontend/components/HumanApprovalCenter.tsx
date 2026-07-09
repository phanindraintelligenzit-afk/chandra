"use client";

/**
 * Human Approval Center — the operator surface for the Digital Worker.
 *
 * Polls GET /requests and renders every Digital Worker request that flowed
 * in from any channel (Jira / Slack / Teams / Email / REST / CloudWatch /
 * Azure / GCP / webhook). Requests paused at the LangGraph human approval
 * gate (`awaiting_approval`) surface an Approve / Reject control that calls
 * POST /requests/{job_id}/approve, resuming the graph.
 *
 * This is wired to the *Digital Worker* workflow, distinct from the legacy
 * WorkerActionExecutionCenter (which drives the /orchestrate escalation
 * flow). Real-time updates use short-interval polling — the same pattern
 * the rest of services/api.ts already uses for async jobs.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  getDigitalWorkerRequest,
  listDigitalWorkerRequests,
  submitDigitalWorkerApproval,
  type ApprovalRequestDetail,
  type DigitalWorkerRequestDetail,
  type DigitalWorkerRequestSummary,
  type DigitalWorkerStatus
} from "../services/api";

const POLL_INTERVAL_MS = 4000;

const STATUS_META: Record<string, { label: string; className: string }> = {
  pending: { label: "Queued", className: "text-frost/70 border-white/15 bg-white/5" },
  running: { label: "Analyzing", className: "text-sky-300 border-sky-400/30 bg-sky-400/10" },
  awaiting_approval: {
    label: "Awaiting Approval",
    className: "text-amber-300 border-amber-400/40 bg-amber-400/10"
  },
  completed: { label: "Completed", className: "text-emerald-300 border-emerald-400/30 bg-emerald-400/10" },
  failed: { label: "Failed", className: "text-rose-300 border-rose-400/30 bg-rose-400/10" },
  stopped: { label: "Stopped", className: "text-frost/60 border-white/10 bg-black/20" }
};

const RISK_META: Record<string, string> = {
  low: "text-emerald-300 border-emerald-400/30 bg-emerald-400/10",
  medium: "text-amber-300 border-amber-400/30 bg-amber-400/10",
  high: "text-orange-300 border-orange-400/40 bg-orange-400/10",
  critical: "text-rose-300 border-rose-400/40 bg-rose-400/10"
};

export type UnifiedRequest = {
  isKra: boolean;
  job_id: string;
  status: string;
  progress: number;
  message: string;
  source: string | null;
  title: string | null;
  external_id: string | null;
  category: string | null;
  platform: string | null;
  priority: string | null;
  risk_level: string | null;
  risk_score: number | null;
  decision_mode: string | null;
  reason: string | null;
  requires_approval: boolean;
  submitted_at: number | null;
  kraData?: any;
};

function Badge({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-[11px] font-medium uppercase tracking-wider ${
        className ?? "text-frost/70 border-white/15 bg-white/5"
      }`}
    >
      {children}
    </span>
  );
}

function relativeTime(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "—";
  const deltaMs = Date.now() - epochSeconds * 1000;
  const mins = Math.floor(deltaMs / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function StatusBadge({ status }: { status: string }) {
  const meta = STATUS_META[status] ?? STATUS_META.pending;
  return <Badge className={meta.className}>{meta.label}</Badge>;
}

function RiskBadge({ level, score }: { level: string | null; score: number | null }) {
  if (!level) return null;
  return (
    <Badge className={RISK_META[level] ?? RISK_META.medium}>
      risk {level}
      {typeof score === "number" ? ` · ${score}` : ""}
    </Badge>
  );
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[10px] uppercase tracking-widest text-frost/50">{label}</span>
      <span className="text-sm text-frost/85">{value ?? "—"}</span>
    </div>
  );
}

function RequestDetailPanel({ req }: { req: UnifiedRequest }) {
  const [detail, setDetail] = useState<DigitalWorkerRequestDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (req.isKra) return; // KRA requests don't need to fetch from backend
    
    let cancelled = false;
    const load = async () => {
      try {
        const data = await getDigitalWorkerRequest(req.job_id);
        if (!cancelled) setDetail(data);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load detail");
      }
    };
    load();
    const timer = setInterval(load, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [req.job_id, req.isKra]);

  if (req.isKra) {
    const steps = (req.kraData?.steps as string[]) || [];
    return (
      <div className="mt-3 flex flex-col gap-4 rounded-2xl border border-white/10 bg-black/30 p-4">
        {steps.length > 0 && (
          <section>
            <h5 className="mb-1 text-[11px] uppercase tracking-widest text-frost/50">
              Remediation Steps
            </h5>
            <ol className="flex flex-col gap-1.5">
              {steps.map((step, idx) => (
                <li key={idx} className="rounded-lg border border-white/8 bg-black/20 px-3 py-2 text-sm text-frost/85">
                  <span className="mr-2 text-frost/50">{idx + 1}.</span>
                  {step}
                </li>
              ))}
            </ol>
          </section>
        )}
      </div>
    );
  }

  if (error) return <div className="text-sm text-rose-300">{error}</div>;
  if (!detail) return <div className="text-sm text-frost/60">Loading detail…</div>;

  const approval: ApprovalRequestDetail | undefined = detail.detail?.approval_request;
  const output = detail.detail?.output as Record<string, any> | undefined;
  const auditTrail: any[] = (output?.audit_trail as any[]) ?? [];
  const rootCause = approval?.root_cause ?? (output?.root_cause as ApprovalRequestDetail["root_cause"]);
  const guidance = output?.guidance_md as string | undefined;

  let planSteps: Array<{ label: string; text: string; command?: string | null; detail?: string }> = [];
  const plan = approval?.plan ?? (output?.plan as ApprovalRequestDetail["plan"]);
  
  if (plan?.steps && plan.steps.length > 0) {
    planSteps = plan.steps.map(s => ({ label: `${s.order}.`, text: s.action, command: s.command, detail: s.detail }));
  } else if (output?.plan?.steps && Array.isArray(output.plan.steps) && output.plan.steps.length > 0) {
    planSteps = output.plan.steps.map((s: any, i: number) => ({ label: `${i + 1}.`, text: typeof s === 'string' ? s : (s.action || JSON.stringify(s)) }));
  } else if (output?.steps && Array.isArray(output.steps) && output.steps.length > 0) {
    planSteps = output.steps.map((s: any, i: number) => ({ label: `${i + 1}.`, text: String(s) }));
  } else if (output?.remediation_steps && Array.isArray(output.remediation_steps) && output.remediation_steps.length > 0) {
    planSteps = output.remediation_steps.map((s: any, i: number) => ({ label: `${i + 1}.`, text: String(s) }));
  }

  return (
    <div className="mt-3 grid gap-2 text-[0.68rem] text-frost/75">
      {rootCause?.summary && (
        <div className="rounded-xl border border-white/10 bg-black/40 p-4 mt-2 overflow-hidden">
          <div className="text-[0.6rem] font-medium uppercase tracking-[0.2em] text-amber mb-3">ROOT CAUSE ANALYSIS</div>
          <p className="text-xs text-frost/90 leading-relaxed uppercase">{rootCause.summary}</p>
          {rootCause.probable_causes && rootCause.probable_causes.length > 0 && (
            <ul className="mt-2 list-disc pl-5 text-xs text-frost/70 uppercase">
              {rootCause.probable_causes.map((c, i) => (
                <li key={i}>{c}</li>
              ))}
            </ul>
          )}
          <span className="mt-2 inline-block text-[10px] uppercase tracking-wider text-frost/40">
            SOURCE: {rootCause.generated_by ?? "DETERMINISTIC"}
          </span>
        </div>
      )}

      {planSteps.length > 0 && (
        <div className="rounded-xl border border-white/10 bg-black/40 p-4 mt-2 overflow-hidden">
          <div className="text-[0.6rem] font-medium uppercase tracking-[0.2em] text-amber mb-3">
            REMEDIATION STEPS {plan?.generated_by ? `· ${plan.generated_by}` : ""}
            {plan?.automation_available ? " · AUTOMATION AVAILABLE" : ""}
          </div>
          <ol className="list-decimal space-y-3 pl-5 text-xs leading-relaxed text-frost/90 uppercase">
            {planSteps.map((step, idx) => (
              <li key={idx} className="break-all whitespace-pre-wrap">
                {step.text}
                {step.detail && <div className="mt-1 text-[0.65rem] text-frost/60">{step.detail}</div>}
                {step.command && (
                  <code className="mt-1 block overflow-x-auto rounded border border-emerald-400/20 bg-emerald-400/5 px-2 py-1 text-[11px] text-emerald-200 normal-case">
                    {step.command}
                  </code>
                )}
              </li>
            ))}
          </ol>
        </div>
      )}

      {guidance && (
        <div className="rounded-xl border border-white/10 bg-black/40 p-4 mt-2 overflow-hidden">
          <div className="text-[0.6rem] font-medium uppercase tracking-[0.2em] text-amber mb-3">ENGINEER GUIDANCE</div>
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap text-xs text-frost/80">
            {guidance}
          </pre>
        </div>
      )}

      {auditTrail.length > 0 && (
        <div className="rounded-xl border border-white/10 bg-black/40 p-4 mt-2 overflow-hidden">
          <div className="text-[0.6rem] font-medium uppercase tracking-[0.2em] text-amber mb-3">EXECUTION TIMELINE</div>
          <ol className="flex flex-col gap-2">
            {auditTrail.map((event, i) => (
              <li key={i} className="flex items-center gap-2 text-xs text-frost/70 uppercase">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-400/70" />
                <span className="text-frost/50 font-semibold">{event.node}</span>
                <span className="text-frost/85">{event.event}</span>
              </li>
            ))}
          </ol>
        </div>
      )}
    </div>
  );
}

function RequestCard({
  req,
  onDecision
}: {
  req: UnifiedRequest;
  onDecision: (req: UnifiedRequest, approved: boolean) => Promise<void>;
}) {
  const [expanded, setExpanded] = useState(req.requires_approval);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState<null | "approve" | "reject">(null);

  const decide = async (approved: boolean) => {
    setBusy(approved ? "approve" : "reject");
    try {
      await onDecision(req, approved);
    } finally {
      setBusy(null);
    }
  };

  const isPending = req.requires_approval;
  const isRunning = req.status === "pending" || req.status === "running";

  return (
    <div className="glass w-full rounded-2xl border border-white/10 p-4 flex flex-col h-fit">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2 text-[0.65rem] uppercase tracking-[0.14em]">
            <span className="border border-white/20 bg-white/10 px-1.5 py-0.5 text-[0.55rem] tracking-[0.16em] text-white">
              {STATUS_META[req.status]?.label ?? req.status}
            </span>
            <span className={`border px-1.5 py-0.5 text-[0.55rem] tracking-[0.16em] ${req.isKra ? "border-fuchsia-400/30 bg-fuchsia-400/10 text-fuchsia-300" : "border-sky-400/30 bg-sky-400/10 text-sky-300"}`}>
              {req.isKra ? "KRA" : "JIRA"}
            </span>
            {(req.kraData?.kraCode || req.external_id) && (
              <span className="border border-sky-400/30 bg-sky-400/10 px-1.5 py-0.5 text-[0.55rem] tracking-[0.16em] text-sky-300">
                {req.kraData?.kraCode || req.external_id}
              </span>
            )}
            {req.risk_level && (
              <span className="border border-orange-400/30 bg-orange-400/10 px-1.5 py-0.5 text-[0.55rem] tracking-[0.16em] text-orange-300">
                RISK {req.risk_level} {req.risk_score ? `· ${req.risk_score}` : ""}
              </span>
            )}
            {req.priority && (
              <span className="border border-white/15 bg-white/[0.04] px-1.5 py-0.5 text-[0.55rem] tracking-[0.16em] text-frost/80">
                {req.priority}
              </span>
            )}
          </div>
          
          <div className="mt-2 text-sm font-semibold text-frost break-words uppercase">{req.title ?? "UNTITLED REQUEST"}</div>
          <div className="mt-2 grid grid-cols-2 gap-3 sm:grid-cols-4 max-w-2xl">
            <Field label="Category" value={req.category ?? "—"} />
            <Field label="Platform" value={req.platform ?? "—"} />
            <Field label="Decision" value={req.decision_mode ?? "—"} />
            <Field label="Submitted" value={relativeTime(req.submitted_at)} />
          </div>
          
          {isRunning && (
            <div className="mt-3">
              <div className="h-1.5 w-full overflow-hidden rounded-full bg-white/10">
                <div
                  className="h-full rounded-full bg-sky-400/70 transition-all"
                  style={{ width: `${Math.max(8, req.progress)}%` }}
                />
              </div>
              <p className="mt-1 text-[10px] uppercase tracking-wider text-frost/60">{req.message}</p>
            </div>
          )}

          {req.reason && (
             <p className="mt-3 rounded-lg border border-white/8 bg-black/20 px-3 py-2 text-[0.68rem] text-frost/70 uppercase">
               <span className="text-frost/50">REASON: </span>
               {req.reason}
             </p>
          )}
          
          {isPending && (
             <div className="mt-4 flex flex-wrap items-center gap-2">
               <input
                 value={comment}
                 onChange={(e) => setComment(e.target.value)}
                 placeholder="OPTIONAL DECISION NOTE…"
                 className="min-w-0 flex-1 rounded-lg border border-white/15 bg-black/40 px-3 py-2 text-sm text-frost/85 outline-none placeholder:text-frost/40 focus:border-white/30 uppercase"
               />
               <button onClick={() => decide(true)} disabled={busy !== null} className="rounded-md border border-emerald-300/30 bg-emerald-300/10 px-4 py-2 text-[0.68rem] uppercase tracking-[0.08em] text-emerald-200 hover:bg-emerald-300/20 transition disabled:opacity-50">{busy === "approve" ? "APPROVING…" : "Approve"}</button>
               <button onClick={() => decide(false)} disabled={busy !== null} className="rounded-md border border-signal/30 bg-signal/10 px-4 py-2 text-[0.68rem] uppercase tracking-[0.08em] text-signal hover:bg-signal/20 transition disabled:opacity-50">{busy === "reject" ? "REJECTING…" : "Reject"}</button>
             </div>
          )}

          {expanded && <RequestDetailPanel req={req} />}
        </div>
        
        <button
          onClick={() => setExpanded((v) => !v)}
          className="rounded-full border border-white/15 px-3 py-1 text-[0.65rem] uppercase tracking-[0.1em] text-frost/70 transition hover:bg-white/10 flex-shrink-0"
        >
          {expanded ? "Hide" : "Details"}
        </button>
      </div>
    </div>
  );
}

export function HumanApprovalCenter({ 
  kraCards = [], 
  kraActionNames = new Set<string>(), 
  onAutoApproved 
}: { 
  kraCards?: any[]; 
  kraActionNames?: Set<string>; 
  onAutoApproved?: (action: any, approved: boolean) => void;
}) {
  const [requests, setRequests] = useState<DigitalWorkerRequestSummary[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [filter, setFilter] = useState<DigitalWorkerStatus | "all">("all");
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const filterRef = useRef(filter);
  filterRef.current = filter;

  const [kraApprovals, setKraApprovals] = useState<any[]>([]);
  useEffect(() => {
    // Only set initial state or sync if we haven't locally mutated
    // In a real app we might diff, but simple assignment works for seed
    setKraApprovals(kraCards);
  }, [kraCards]);

  const load = useCallback(async () => {
    try {
      const status = filterRef.current === "all" ? undefined : filterRef.current;
      const data = await listDigitalWorkerRequests(status);
      setRequests(data.requests);
      setCounts(data.counts);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to reach the Digital Worker API");
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [load, filter]);

  const onDecision = useCallback(
    async (req: UnifiedRequest, approved: boolean) => {
      if (req.isKra) {
        setKraApprovals(curr => curr.map(r => r.id === req.job_id ? { ...r, state: approved ? "Approved" : "Rejected" } : r));
        const row = kraApprovals.find(r => r.id === req.job_id);
        if (approved && row && onAutoApproved) {
          onAutoApproved({
            ...row,
            incident: row.incident,
            actionName: row.incident,
            severity: row.severity,
            kraCode: row.kraCode || "",
            steps: row.steps || [],
            note: `${row.note} Approved by supervisor.`
          }, true);
        }
      } else {
        await submitDigitalWorkerApproval(req.job_id, { approved, approver: "console" });
        await load();
      }
    },
    [load, kraApprovals, onAutoApproved]
  );

  const unifiedRequests: UnifiedRequest[] = useMemo(() => {
    const kras: UnifiedRequest[] = kraApprovals.map(kra => ({
      isKra: true,
      job_id: kra.id,
      status: kra.state === "Awaiting Review" ? "awaiting_approval" : kra.state === "Approved" ? "completed" : kra.state === "Rejected" ? "failed" : "stopped",
      progress: 0,
      message: "",
      source: "KRA",
      title: kra.incident,
      external_id: kra.kraCode,
      category: "Security",
      platform: kra.account,
      priority: kra.severity,
      risk_level: kra.severity === "P1" ? "critical" : kra.severity === "P2" ? "high" : "medium",
      risk_score: kra.confidence,
      decision_mode: "AWAIT_APPROVAL",
      reason: kra.note,
      requires_approval: kra.state === "Awaiting Review",
      submitted_at: Date.parse(new Date().toDateString() + " " + kra.requested) / 1000 || Date.now() / 1000,
      kraData: kra,
    }));

    const filteredJira = requests.filter(req => {
      if (!req.title) return true;
      return !kraActionNames.has(req.title.toLowerCase().trim());
    }).map(req => ({
      isKra: false,
      job_id: req.job_id,
      status: req.status,
      progress: req.progress,
      message: req.message,
      source: req.source || "JIRA",
      title: req.title,
      external_id: req.external_id,
      category: req.category,
      platform: req.platform,
      priority: req.priority,
      risk_level: req.risk_level,
      risk_score: req.risk_score,
      decision_mode: req.decision_mode,
      reason: req.reason,
      requires_approval: req.status === "awaiting_approval",
      submitted_at: req.submitted_at,
    }));

    const all = [...kras, ...filteredJira];
    
    // Apply local filter since unifiedRequests handles both now
    const currentStatus = filterRef.current;
    const matched = currentStatus === "all" ? all : all.filter(r => r.status === currentStatus);
    return matched.sort((a, b) => (b.submitted_at || 0) - (a.submitted_at || 0));
  }, [kraApprovals, requests, kraActionNames]);

  const awaitingCount = unifiedRequests.filter(r => r.status === "awaiting_approval").length;
  const runningCount = unifiedRequests.filter(r => r.status === "running" || r.status === "pending").length;
  const completedCount = unifiedRequests.filter(r => r.status === "completed").length;
  const failedCount = unifiedRequests.filter(r => r.status === "failed").length;

  const filters: Array<{ key: DigitalWorkerStatus | "all"; label: string }> = useMemo(
    () => [
      { key: "all", label: "All" },
      { key: "awaiting_approval", label: `Pending${awaitingCount ? ` (${awaitingCount})` : ""}` },
      { key: "running", label: "Running" },
      { key: "completed", label: "Completed" },
      { key: "failed", label: "Failed" }
    ],
    [awaitingCount]
  );

  return (
    <div className="rounded-3xl border border-white/10 bg-black/20 p-5 backdrop-blur">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="text-lg font-semibold text-frost/90">Human Approval Center</h3>
          <p className="mt-0.5 text-sm text-frost/60">
            Digital Worker requests across every channel — approve or reject remediations gated for human review.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3 text-xs text-frost/60">
          <span className="text-emerald-300">{completedCount} completed</span>
          <span className="text-amber-300">{awaitingCount} awaiting</span>
          <span className="text-sky-300">{runningCount} in-flight</span>
          <span className="text-rose-300">{failedCount} failed</span>
        </div>
      </div>

      <div className="mt-4 flex flex-wrap gap-2">
        {filters.map((f) => (
          <button
            key={f.key}
            onClick={() => setFilter(f.key)}
            className={`rounded-full border px-3 py-1 text-xs transition ${
              filter === f.key
                ? "border-white/30 bg-white/10 text-frost/90"
                : "border-white/10 text-frost/60 hover:bg-white/5"
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {error && (
        <div className="mt-4 rounded-xl border border-rose-400/30 bg-rose-400/10 px-4 py-3 text-sm text-rose-200">
          {error}
        </div>
      )}

      <div className="mt-4 flex flex-col gap-3">
        {loaded && unifiedRequests.length === 0 && !error && (
          <div className="rounded-2xl border border-dashed border-white/10 bg-black/20 px-4 py-8 text-center text-sm text-frost/50">
            No Digital Worker requests yet. Incoming Jira / Slack / Teams / webhook requests appear here automatically.
          </div>
        )}
        {unifiedRequests.map((req) => (
          <RequestCard key={req.job_id} req={req} onDecision={onDecision} />
        ))}
      </div>
    </div>
  );
}

export default HumanApprovalCenter;
