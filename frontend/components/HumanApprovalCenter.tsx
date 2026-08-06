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
import { Search } from "lucide-react";

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
  isAwsTask?: boolean;
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
  resourceId?: string | null;
  action?: string | null;
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
                <li key={idx} className="rounded-lg border border-white/8 bg-black/20 px-3 py-2 text-sm text-frost/85 whitespace-pre-wrap font-mono">
                  <span className="mr-2 text-frost/50 font-sans">{idx + 1}.</span>
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
  const [expanded, setExpanded] = useState(false);
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
            <span className={`border px-1.5 py-0.5 text-[0.55rem] tracking-[0.16em] ${req.isAwsTask ? "border-cyan-400/30 bg-cyan-400/10 text-cyan-300" : req.isKra ? "border-fuchsia-400/30 bg-fuchsia-400/10 text-fuchsia-300" : "border-sky-400/30 bg-sky-400/10 text-sky-300"}`}>
              {req.isAwsTask ? "AWS TASK" : req.isKra ? "KRA" : (req.source ? req.source.toUpperCase() : "JIRA")}
            </span>
            {(req.kraData?.kraCode || req.external_id) && (
              <span className="border border-sky-400/30 bg-sky-400/10 px-1.5 py-0.5 text-[0.55rem] tracking-[0.16em] text-sky-300">
                {req.kraData?.kraCode || req.external_id}
              </span>
            )}
            {req.resourceId && (
              <span className="border border-indigo-400/30 bg-indigo-400/10 px-1.5 py-0.5 text-[0.55rem] tracking-[0.16em] text-indigo-300 truncate max-w-[200px]" title={req.resourceId}>
                {req.resourceId.split(':').pop() || req.resourceId}
              </span>
            )}
            {req.priority && (
              <span className="border border-white/15 bg-white/[0.04] px-1.5 py-0.5 text-[0.55rem] tracking-[0.16em] text-frost/80">
                {req.priority}
              </span>
            )}
            {req.action && (
              <span className="border border-emerald-400/30 bg-emerald-400/10 px-1.5 py-0.5 text-[0.55rem] tracking-[0.16em] text-emerald-300 truncate max-w-[250px]" title={req.action}>
                ACT: {req.action.replace(/^remediate_/, "")}
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
             <div className="mt-3 rounded-lg border border-white/8 bg-black/20 px-3 py-2 text-[0.68rem] text-frost/70 uppercase">
               <span className="text-frost/50">REASON: </span>
               {req.reason}
             </div>
          )}
          
          {expanded && <RequestDetailPanel req={req} />}
          
          {isPending && (
            <div className="mt-4 flex flex-wrap items-center gap-2">
              <button onClick={() => decide(true)} disabled={busy !== null} className="rounded-md border border-emerald-300/30 bg-emerald-300/10 px-4 py-2 text-[0.68rem] uppercase tracking-[0.08em] text-emerald-200 hover:bg-emerald-300/20 transition disabled:opacity-50">{busy === "approve" ? "APPROVING…" : "Approve"}</button>
              <button onClick={() => decide(false)} disabled={busy !== null} className="rounded-md border border-signal/30 bg-signal/10 px-4 py-2 text-[0.68rem] uppercase tracking-[0.08em] text-signal hover:bg-signal/20 transition disabled:opacity-50">{busy === "reject" ? "REJECTING…" : "Reject"}</button>
            </div>
          )}
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
  const [searchQuery, setSearchQuery] = useState("");
  const [kraFilter, setKraFilter] = useState("all");
  const [severityFilter, setSeverityFilter] = useState("all");
  const [sourceFilter, setSourceFilter] = useState("all");
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const filterRef = useRef(filter);
  filterRef.current = filter;

  const [kraApprovals, setKraApprovals] = useState<any[]>([]);
  const localDecisions = useRef<Record<string, string>>({});

  useEffect(() => {
    // Merge backend updates with our optimistic local decisions
    // so the button doesn't pop back to "Approve" when the backend polls
    setKraApprovals(kraCards.map(card => {
       if (localDecisions.current[card.id]) {
           return { ...card, state: localDecisions.current[card.id] };
       }
       return card;
    }));
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
      if (req.isKra || req.isAwsTask) {
        const newState = approved ? "Approved" : "Rejected";
        localDecisions.current[req.job_id] = newState;
        
        setKraApprovals(curr => curr.map(r => r.id === req.job_id ? { ...r, state: newState } : r));
        const row = kraApprovals.find(r => r.id === req.job_id);
        if (approved && row && onAutoApproved) {
          onAutoApproved({
            ...row,
            incident: row.incident,
            actionName: row.incident,
            severity: row.severity,
            kraCode: row.kraCode || "",
            steps: row.steps || [],
            // AWS Tasks must NEVER carry detectorId — that routes to the KRA remediation path
            detectorId: row.isAwsTask ? undefined : row.detectorId,
            region: row.region,
            isAwsTask: row.isAwsTask || false,
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
      isKra: !kra.isAwsTask,
      isAwsTask: kra.isAwsTask || false,
      job_id: kra.id,
      status: kra.state === "Awaiting Review" ? "awaiting_approval" : kra.state === "Approved" ? "completed" : kra.state === "Rejected" ? "failed" : "stopped",
      progress: 0,
      message: "",
      source: kra.isAwsTask ? "AWS_TASK" : "KRA",
      title: kra.incident,
      external_id: kra.kraCode,
      category: kra.category || "",
      platform: kra.account,
      priority: kra.severity,
      risk_level: kra.severity === "P1" ? "critical" : kra.severity === "P2" ? "high" : "medium",
      risk_score: kra.confidence,
      decision_mode: "AWAIT_APPROVAL",
      reason: kra.note,
      requires_approval: kra.state === "Awaiting Review",
      submitted_at: (kra.requested ? (new Date(kra.requested).getTime() / 1000) : null) || Date.now() / 1000,
      kraData: kra,
      resourceId: kra.resourceId,
      action: kra.action,
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
      action: req.action || null,
    }));

    const all = [...kras, ...filteredJira];
    
    // Apply local filters
    const currentStatus = filterRef.current;
    let matched = currentStatus === "all" ? all : all.filter(r => r.status === currentStatus);
    
    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase().trim();
      matched = matched.filter(r => 
        (r.title && r.title.toLowerCase().includes(q)) || 
        (r.reason && r.reason.toLowerCase().includes(q)) || 
        (r.message && r.message.toLowerCase().includes(q))
      );
    }

    if (kraFilter !== "all") {
      matched = matched.filter(r => 
        (('kraData' in r && (r as any).kraData?.kraCode === kraFilter)) || 
        (r.category === kraFilter)
      );
    }

    if (severityFilter !== "all") {
      matched = matched.filter(r => 
        (r.priority && r.priority.toLowerCase() === severityFilter.toLowerCase())
      );
    }

    if (sourceFilter !== "all") {
      matched = matched.filter(r => {
        const src = r.source ? r.source.toLowerCase() : "unknown";
        return src === sourceFilter;
      });
    }

    return matched.sort((a, b) => (b.submitted_at || 0) - (a.submitted_at || 0));
  }, [kraApprovals, requests, kraActionNames, searchQuery, kraFilter, severityFilter, sourceFilter]);

  const availableKras = useMemo(() => {
    const set = new Set<string>();
    kraApprovals.forEach(k => { if (k.kraCode) set.add(k.kraCode); });
    requests.forEach(r => { if (r.category) set.add(r.category); });
    return Array.from(set).filter(Boolean).sort();
  }, [kraApprovals, requests]);

  const availableSources = useMemo(() => {
    const set = new Set<string>();
    unifiedRequests.forEach(r => { if (r.source) set.add(r.source.toLowerCase()); });
    return Array.from(set).sort();
  }, [unifiedRequests]);

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

      <div className="mt-4 flex flex-wrap items-center justify-between gap-4">
        <div className="flex flex-wrap gap-2">
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
        
        <div className="flex flex-wrap items-center gap-3">
          <div className="relative">
            <input 
              type="text" 
              placeholder="Search approvals..." 
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-48 rounded-lg border border-white/10 bg-black/40 py-1.5 pl-8 pr-3 text-xs text-frost/90 placeholder:text-frost/40 focus:border-white/30 outline-none transition"
            />
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 text-frost/40 h-3.5 w-3.5" />
          </div>
          
          <select
            value={sourceFilter}
            onChange={(e) => setSourceFilter(e.target.value)}
            className="rounded-lg border border-white/10 bg-black/40 py-1.5 px-3 text-xs text-frost/90 focus:border-white/30 outline-none transition uppercase tracking-wider"
          >
            <option value="all">ALL SOURCES</option>
            {availableSources.map(s => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>

          <select
            value={severityFilter}
            onChange={(e) => setSeverityFilter(e.target.value)}
            className="rounded-lg border border-white/10 bg-black/40 py-1.5 px-3 text-xs text-frost/90 focus:border-white/30 outline-none transition uppercase tracking-wider"
          >
            <option value="all">ALL SEVERITIES</option>
            <option value="critical">CRITICAL</option>
            <option value="high">HIGH</option>
            <option value="medium">MEDIUM</option>
            <option value="low">LOW</option>
          </select>

          <select
            value={kraFilter}
            onChange={(e) => setKraFilter(e.target.value)}
            className="rounded-lg border border-white/10 bg-black/40 py-1.5 px-3 text-xs text-frost/90 focus:border-white/30 outline-none transition uppercase tracking-wider"
          >
            <option value="all">ALL KRAS</option>
            {availableKras.map(k => (
              <option key={k} value={k}>{k}</option>
            ))}
          </select>
        </div>
      </div>

      {error && (
        <div className="mt-4 rounded-xl border border-rose-400/30 bg-rose-400/10 px-4 py-3 text-sm text-rose-200">
          {error}
        </div>
      )}

      <div className="mt-4 flex flex-col gap-3 max-h-[600px] overflow-y-auto pr-2">
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
