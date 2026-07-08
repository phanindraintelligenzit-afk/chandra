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

function RequestDetailPanel({ jobId }: { jobId: string }) {
  const [detail, setDetail] = useState<DigitalWorkerRequestDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const data = await getDigitalWorkerRequest(jobId);
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
  }, [jobId]);

  if (error) return <div className="text-sm text-rose-300">{error}</div>;
  if (!detail) return <div className="text-sm text-frost/60">Loading detail…</div>;

  const approval: ApprovalRequestDetail | undefined = detail.detail?.approval_request;
  const output = detail.detail?.output as Record<string, any> | undefined;
  const auditTrail: any[] = (output?.audit_trail as any[]) ?? [];
  const plan = approval?.plan ?? (output?.plan as ApprovalRequestDetail["plan"]);
  const rootCause = approval?.root_cause ?? (output?.root_cause as ApprovalRequestDetail["root_cause"]);
  const guidance = output?.guidance_md as string | undefined;

  return (
    <div className="mt-3 flex flex-col gap-4 rounded-2xl border border-white/10 bg-black/30 p-4">
      {rootCause?.summary && (
        <section>
          <h5 className="mb-1 text-[11px] uppercase tracking-widest text-frost/50">Root Cause Analysis</h5>
          <p className="text-sm text-frost/85">{rootCause.summary}</p>
          {rootCause.probable_causes && rootCause.probable_causes.length > 0 && (
            <ul className="mt-1 list-disc pl-5 text-xs text-frost/70">
              {rootCause.probable_causes.map((c, i) => (
                <li key={i}>{c}</li>
              ))}
            </ul>
          )}
          <span className="mt-1 inline-block text-[10px] uppercase tracking-wider text-frost/40">
            source: {rootCause.generated_by ?? "deterministic"}
          </span>
        </section>
      )}

      {plan?.steps && plan.steps.length > 0 && (
        <section>
          <h5 className="mb-1 text-[11px] uppercase tracking-widest text-frost/50">
            Resolution Plan · {plan.generated_by ?? "deterministic"}
            {plan.automation_available ? " · automation available" : ""}
          </h5>
          <ol className="flex flex-col gap-1.5">
            {plan.steps.map((step) => (
              <li key={step.order} className="rounded-lg border border-white/8 bg-black/20 px-3 py-2 text-sm text-frost/85">
                <span className="mr-2 text-frost/50">{step.order}.</span>
                {step.action}
                {step.detail && <div className="mt-0.5 text-xs text-frost/60">{step.detail}</div>}
                {step.command && (
                  <code className="mt-1 block overflow-x-auto rounded bg-black/40 px-2 py-1 text-[11px] text-emerald-200">
                    {step.command}
                  </code>
                )}
              </li>
            ))}
          </ol>
        </section>
      )}

      {guidance && (
        <section>
          <h5 className="mb-1 text-[11px] uppercase tracking-widest text-frost/50">Engineer Guidance</h5>
          <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-lg border border-white/8 bg-black/40 p-3 text-xs text-frost/80">
            {guidance}
          </pre>
        </section>
      )}

      {auditTrail.length > 0 && (
        <section>
          <h5 className="mb-1 text-[11px] uppercase tracking-widest text-frost/50">Execution Timeline</h5>
          <ol className="flex flex-col gap-1">
            {auditTrail.map((event, i) => (
              <li key={i} className="flex items-center gap-2 text-xs text-frost/70">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-400/70" />
                <span className="text-frost/50">{event.node}</span>
                <span className="text-frost/85">{event.event}</span>
              </li>
            ))}
          </ol>
        </section>
      )}
    </div>
  );
}

function RequestCard({
  req,
  onDecision
}: {
  req: DigitalWorkerRequestSummary;
  onDecision: (jobId: string, approved: boolean) => Promise<void>;
}) {
  const [expanded, setExpanded] = useState(req.status === "awaiting_approval");
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState<null | "approve" | "reject">(null);

  const decide = async (approved: boolean) => {
    setBusy(approved ? "approve" : "reject");
    try {
      await onDecision(req.job_id, approved);
    } finally {
      setBusy(null);
    }
  };

  const isPending = req.status === "awaiting_approval";
  const isRunning = req.status === "pending" || req.status === "running";

  return (
    <div className="rounded-2xl border border-white/10 bg-black/25 p-4 backdrop-blur-sm">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge status={req.status} />
            {req.source && <Badge>{req.source}</Badge>}
            {req.external_id && <Badge className="text-sky-300 border-sky-400/30 bg-sky-400/10">{req.external_id}</Badge>}
            <RiskBadge level={req.risk_level} score={req.risk_score} />
            {req.priority && <Badge className="text-frost/80 border-white/15 bg-white/5">{req.priority}</Badge>}
          </div>
          <h4 className="mt-2 truncate text-base font-medium text-frost/90">{req.title ?? "Untitled request"}</h4>
        </div>
        <button
          onClick={() => setExpanded((v) => !v)}
          className="rounded-full border border-white/15 px-3 py-1 text-xs text-frost/70 transition hover:bg-white/10"
        >
          {expanded ? "Hide" : "Details"}
        </button>
      </div>

      <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
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
          <p className="mt-1 text-xs text-frost/60">{req.message}</p>
        </div>
      )}

      {req.reason && (
        <p className="mt-3 rounded-lg border border-white/8 bg-black/20 px-3 py-2 text-xs text-frost/70">
          <span className="text-frost/50">Reason: </span>
          {req.reason}
        </p>
      )}

      {isPending && (
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <input
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            placeholder="Optional decision note…"
            className="min-w-0 flex-1 rounded-lg border border-white/15 bg-black/40 px-3 py-2 text-sm text-frost/85 outline-none placeholder:text-frost/40 focus:border-white/30"
          />
          <button
            onClick={() => decide(true)}
            disabled={busy !== null}
            className="rounded-lg border border-emerald-400/40 bg-emerald-400/15 px-4 py-2 text-sm font-medium text-emerald-200 transition hover:bg-emerald-400/25 disabled:opacity-50"
          >
            {busy === "approve" ? "Approving…" : "Approve"}
          </button>
          <button
            onClick={() => decide(false)}
            disabled={busy !== null}
            className="rounded-lg border border-rose-400/40 bg-rose-400/15 px-4 py-2 text-sm font-medium text-rose-200 transition hover:bg-rose-400/25 disabled:opacity-50"
          >
            {busy === "reject" ? "Rejecting…" : "Reject"}
          </button>
        </div>
      )}

      {expanded && <RequestDetailPanel jobId={req.job_id} />}
    </div>
  );
}

export function HumanApprovalCenter() {
  const [requests, setRequests] = useState<DigitalWorkerRequestSummary[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [filter, setFilter] = useState<DigitalWorkerStatus | "all">("all");
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const filterRef = useRef(filter);
  filterRef.current = filter;

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
    async (jobId: string, approved: boolean) => {
      await submitDigitalWorkerApproval(jobId, { approved, approver: "console" });
      // Optimistically refresh so the card leaves the awaiting bucket fast.
      await load();
    },
    [load]
  );

  const awaitingCount = counts.awaiting_approval ?? 0;
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
          <span className="text-emerald-300">{counts.completed ?? 0} completed</span>
          <span className="text-amber-300">{awaitingCount} awaiting</span>
          <span className="text-sky-300">{(counts.running ?? 0) + (counts.pending ?? 0)} in-flight</span>
          <span className="text-rose-300">{counts.failed ?? 0} failed</span>
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
        {loaded && requests.length === 0 && !error && (
          <div className="rounded-2xl border border-dashed border-white/10 bg-black/20 px-4 py-8 text-center text-sm text-frost/50">
            No Digital Worker requests yet. Incoming Jira / Slack / Teams / webhook requests appear here automatically.
          </div>
        )}
        {requests.map((req) => (
          <RequestCard key={req.job_id} req={req} onDecision={onDecision} />
        ))}
      </div>
    </div>
  );
}

export default HumanApprovalCenter;
