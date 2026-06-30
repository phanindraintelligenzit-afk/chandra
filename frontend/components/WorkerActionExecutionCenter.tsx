"use client";

import { orchestrateAction, getJobStatus, fetchBackendLogs, type BackendLog } from "@/services/api";
import { motion, AnimatePresence } from "framer-motion";
import { useEffect, useRef, useState, forwardRef, useImperativeHandle } from "react";
import { Play, CheckCircle2, AlertTriangle, Clock, X, Loader2, Download } from "lucide-react";
import { PieChart, Pie, Cell, Tooltip, ResponsiveContainer } from "recharts";

type ExecutingAction = {
  id: string;
  actionName: string;
  actionDescription: string;
  service: string;
  kraCode: string;
  priorityLevel: string;
  steps: string[];
  jiraKey: string;
  jiraUrl?: string;
  status: "pending" | "running" | "completed" | "failed" | "awaiting_input" | "exhausted";
  jobId: string;
  threadId: string;
  startedAt: number;
  completedAt?: number;
  logs: BackendLog[];
  errorMessage?: string;
  progress?: number;
  jobMessage?: string;
  summary?: string;
  questions?: string[];
  sandboxPath?: string;
};

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

function StatusBadge({ status, progress }: { status: ExecutingAction["status"]; progress?: number }) {
  const tones: Record<string, string> = {
    pending: "border-amber/45 bg-amber/12 text-amber",
    running: "border-signal/45 bg-signal/12 text-signal",
    completed: "border-emerald-300/40 bg-emerald-300/12 text-emerald-300",
    failed: "border-signal/45 bg-signal/15 text-signal",
    awaiting_input: "border-blue-400/40 bg-blue-400/12 text-blue-300",
    exhausted: "border-orange-400/40 bg-orange-400/12 text-orange-300"
  };
  const icons: Record<string, JSX.Element> = {
    pending: <Clock size={12} />,
    running: <Loader2 size={12} className="animate-spin" />,
    completed: <CheckCircle2 size={12} />,
    failed: <AlertTriangle size={12} />,
    awaiting_input: <Clock size={12} />,
    exhausted: <AlertTriangle size={12} />
  };

  const displayLabel =
    status === "awaiting_input"
      ? "AWAITING INPUT"
      : status === "exhausted"
      ? "EXHAUSTED"
      : status === "running" && progress != null && progress > 0
      ? `RUNNING ${progress}%`
      : status;

  return (
    <div className={cx("inline-flex items-center gap-1.5 border px-2 py-1 text-[0.6rem] uppercase tracking-[0.18em] rounded", tones[status] || "border-white/20 bg-white/10")}>
      {icons[status]}
      {displayLabel}
    </div>
  );
}

export type WorkerActionExecutionCenterHandle = {
  execute: (action: any) => void;
};

export const WorkerActionExecutionCenter = forwardRef<
  WorkerActionExecutionCenterHandle,
  { actions?: any[]; onActionApproved?: (action: any) => void }
>(function WorkerActionExecutionCenter({ onActionApproved }, ref) {
  const [executingActions, setExecutingActions] = useState<ExecutingAction[]>([]);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  // Per-action refs for the logs scroll container — keyed by action id
  const logsContainerRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  const pollIntervalsRef = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map());
  const logsIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const cachedLogsRef = useRef<BackendLog[]>([]);

  useImperativeHandle(ref, () => ({
    execute: handleExecuteAction
  }), [executingActions]); // re-bind when state changes so dedup check reads fresh list

  // Cleanup all intervals on unmount
  useEffect(() => {
    return () => {
      pollIntervalsRef.current.forEach((interval) => clearInterval(interval));
      pollIntervalsRef.current.clear();
      if (logsIntervalRef.current) clearInterval(logsIntervalRef.current);
    };
  }, []);

  // Scroll the logs container (not the page) to bottom when logs update for the open action
  const scrollLogsToBottom = (actionId: string) => {
    const container = logsContainerRefs.current.get(actionId);
    if (container) {
      const isNearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 80;
      if (isNearBottom) {
        container.scrollTop = container.scrollHeight;
      }
    }
  };

  const stopPolling = (actionId: string) => {
    const interval = pollIntervalsRef.current.get(actionId);
    if (interval) {
      clearInterval(interval);
      pollIntervalsRef.current.delete(actionId);
    }
  };

  const startLogsPolling = () => {
    if (logsIntervalRef.current) return;
    logsIntervalRef.current = setInterval(async () => {
      try {
        cachedLogsRef.current = await fetchBackendLogs(1000, 0);
      } catch (error) {
        console.error("Failed to fetch logs:", error);
      }
    }, 4000);
  };

  const startPolling = (actionId: string, jobId: string, startedAt: number, jiraKey: string) => {
    startLogsPolling();
    stopPolling(actionId);

    const interval = setInterval(async () => {
      try {
        const jobStatus = await getJobStatus(jobId);
        const allLogs = cachedLogsRef.current;

        const startTimeSecs = Math.floor(startedAt / 1000);
        // Upper bound: completed time + 30s buffer, or now + 30s if still running
        const nowSecs = Math.floor(Date.now() / 1000);

        // Build jiraKey match variants: "DEV-742" → also match "DEV 742" and "DEV742"
        const jiraLower = jiraKey ? jiraKey.toLowerCase() : "";        // "dev-742"
        const jiraSpaced = jiraLower ? jiraLower.replace("-", " ") : "";// "dev 742"
        const hasValidJira = jiraKey && jiraKey !== "DEV-000" && jiraKey.length > 3;
        const jobIdLower = jobId ? jobId.toLowerCase() : "";

        let actionLogs = allLogs.filter((log) => {
          const isInWindow = log.timestamp >= startTimeSecs - 5 && log.timestamp <= nowSecs + 30;
          if (!isInWindow) return false;

          // 1. Explicit thread-local jobId match (perfect isolation)
          if (log.job_id && jobIdLower && log.job_id.toLowerCase() === jobIdLower) {
            return true;
          }

          const text = `${log.message} ${log.logger}`.toLowerCase();

          // 1. jobId match — most reliable
          if (jobIdLower && text.includes(jobIdLower)) return true;

          // 2. jiraKey match (both formats)
          if (hasValidJira && (text.includes(jiraLower) || text.includes(jiraSpaced))) return true;

          // 3. Time-window fallback: include all logs in the time window 
          // UNLESS they explicitly belong to a DIFFERENT orchestration job.
          const mentionsDifferentJira =
            text.includes("jira issue key") &&
            (!hasValidJira || (!text.includes(jiraLower) && !text.includes(jiraSpaced)));
            
          const mentionsDifferentJob =
            text.includes("job_id=") &&
            jobIdLower && !text.includes(jobIdLower);

          return !mentionsDifferentJira && !mentionsDifferentJob;
        });

        const jobDone =
          jobStatus.status === "completed" ||
          jobStatus.status === "failed" ||
          jobStatus.status === "not_found";

        if (jobDone) {
          stopPolling(actionId);

          const result = jobStatus.result as any;
          const statusCode: number = result?.statusCode ?? 0;

          let finalStatus: ExecutingAction["status"] = "failed";
          if (jobStatus.status === "completed") {
            if (statusCode === 200 || statusCode === 0) finalStatus = "completed";
            else if (statusCode === 202) finalStatus = "awaiting_input";
            else if (statusCode === 207) finalStatus = "exhausted";
            else finalStatus = "failed";
          }

          const errorMsg =
            jobStatus.error ||
            (jobStatus.status === "failed" ? jobStatus.message : "") ||
            (result?.exception ?? "");

          setExecutingActions((current) =>
            current.map((a) =>
              a.id === actionId
                ? {
                    ...a,
                    status: finalStatus,
                    threadId: result?.thread_id || "",
                    completedAt: Date.now(),
                    logs: actionLogs.length > 0 ? actionLogs : a.logs,
                    errorMessage: errorMsg,
                    progress: 100,
                    jobMessage: jobStatus.message,
                    summary: result?.summary || "",
                    questions: result?.questions || [],
                    sandboxPath: result?.sandbox_path || ""
                  }
                : a
            )
          );
          scrollLogsToBottom(actionId);

          if (finalStatus === "completed" && onActionApproved) {
            setExecutingActions((current) => {
              const found = current.find((a) => a.id === actionId);
              if (found) onActionApproved(found);
              return current;
            });
          }
        } else {
          setExecutingActions((current) =>
            current.map((a) =>
              a.id === actionId
                ? {
                    ...a,
                    logs: actionLogs.length > 0 ? actionLogs : a.logs,
                    progress: jobStatus.progress ?? a.progress,
                    jobMessage: jobStatus.message
                  }
                : a
            )
          );
          scrollLogsToBottom(actionId);
        }
      } catch (error) {
        console.error(`Failed to poll job ${jobId}:`, error);
      }
    }, 2000);

    pollIntervalsRef.current.set(actionId, interval);
  };


  const submitAnswers = async (actionId: string) => {
    const action = executingActions.find((a) => a.id === actionId);
    if (!action || !action.questions) return;

    // Collect answers from DOM
    const answers: string[] = [];
    for (let i = 0; i < action.questions.length; i++) {
      const el = document.getElementById(`input-${actionId}-${i}`) as HTMLInputElement;
      answers.push(el?.value || "");
    }

    // Reset status to running and clear completedAt
    setExecutingActions((current) =>
      current.map((a) =>
        a.id === actionId
          ? {
              ...a,
              status: "running",
              completedAt: undefined,
              progress: 5,
              jobMessage: "Resuming orchestration with answers...",
              errorMessage: ""
            }
          : a
      )
    );

    try {
      // Re-call orchestrateAction
      const jobResponse = await orchestrateAction({
        action: {
          actionName: action.actionName,
          actionDescription: action.actionDescription,
          steps: action.steps,
          service: action.service || "AWS"
        },
        jiraUrl: action.jiraUrl,
        command_timeout: 300,
        max_iterations: 5,
        thread_id: action.threadId,
        sandbox_path: action.sandboxPath,
        answers: answers
      });

      const jobId = jobResponse.job_id;
      setExecutingActions((current) =>
        current.map((a) =>
          a.id === actionId
            ? {
                ...a,
                jobId,
                jobMessage: jobResponse.message || "Job resumed..."
              }
            : a
        )
      );

      startPolling(actionId, jobId, Date.now(), action.jiraKey);
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : String(error);
      setExecutingActions((current) =>
        current.map((a) =>
          a.id === actionId
            ? {
                ...a,
                status: "failed",
                errorMessage: errorMessage,
                jobMessage: "Failed to resume job"
              }
            : a
        )
      );
    }
  };


  const handleExecuteAction = async (action: any) => {
    const actionId = `action-${Date.now()}-${Math.random().toString(36).substring(2, 9)}`;
    const startedAt = Date.now();

    let jiraKey = action.jiraUrl?.split("/").pop() || action.jiraKey || "DEV-000";
    if (jiraKey.toUpperCase() === "ERROR") {
      jiraKey = "DEV-000";
    }
    const actionName = action.actionName || action.incident || "Unnamed Action";

    // Safety-net dedup: skip if an active entry with the same jiraKey+actionName already exists
    const isDuplicate = executingActions.some(
      (a) =>
        (a.status === "pending" || a.status === "running") &&
        a.jiraKey === jiraKey &&
        a.actionName === actionName
    );
    if (isDuplicate) {
      console.warn(`[WorkerCenter] Skipping duplicate submission for ${jiraKey} - ${actionName}`);
      return;
    }

    const executing: ExecutingAction = {
      id: actionId,
      actionName,
      actionDescription: action.actionDescription || action.note || "",
      service: action.service || "AWS",
      kraCode: action.kraCode || "",
      priorityLevel: action.severity || action.priorityLevel || "P3",
      steps: action.steps || [],
      jiraKey,
      jiraUrl: action.jiraUrl,
      status: "pending",
      jobId: "",
      threadId: "",
      startedAt,
      logs: [],
      errorMessage: "",
      progress: 0,
      jobMessage: "Submitting job..."
    };

    setExecutingActions((current) => [executing, ...current]);
    setExpandedId(actionId);

    try {
      const jobResponse = await orchestrateAction({
        action: {
          actionName: executing.actionName,
          actionDescription: executing.actionDescription,
          steps: executing.steps,
          service: executing.service || "AWS"
        },
        jiraUrl: executing.jiraUrl,
        command_timeout: 300,
        max_iterations: 5
      });

      const jobId = jobResponse.job_id;

      setExecutingActions((current) =>
        current.map((a) =>
          a.id === actionId
            ? {
                ...a,
                status: "running",
                jobId,
                jobMessage: jobResponse.message || "Job accepted, running...",
                progress: 5
              }
            : a
        )
      );

      startPolling(actionId, jobId, startedAt, executing.jiraKey);
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : String(error);
      console.error("Action submission failed:", errorMessage);
      setExecutingActions((current) =>
        current.map((a) =>
          a.id === actionId
            ? {
                ...a,
                status: "failed",
                completedAt: Date.now(),
                errorMessage: errorMessage,
                jobMessage: "Failed to submit job"
              }
            : a
        )
      );
    }
  };

  const removeAction = (id: string) => {
    stopPolling(id);
    setExecutingActions((current) => current.filter((a) => a.id !== id));
    if (expandedId === id) setExpandedId(null);
  };

  const levelColor: Record<string, string> = {
    INFO: "text-frost/80",
    WARNING: "text-amber",
    ERROR: "text-signal",
    DEBUG: "text-muted",
    CRITICAL: "text-signal"
  };

  const levelBorder: Record<string, string> = {
    INFO: "border-l-frost/40",
    WARNING: "border-l-amber",
    ERROR: "border-l-signal",
    DEBUG: "border-l-white/20",
    CRITICAL: "border-l-signal"
  };

  const executedCount = executingActions.filter(a => a.status === "completed").length;
  const pendingCount = executingActions.filter(a => a.status === "pending" || a.status === "running" || a.status === "awaiting_input").length;
  const failedCount = executingActions.filter(a => a.status === "failed" || a.status === "exhausted").length;

  const donutData = [
    { name: "Executed", value: executedCount, color: "#4ade80" },
    { name: "Pending", value: pendingCount, color: "#ffb800" },
    { name: "Failed", value: failedCount, color: "#ff3b3b" }
  ].filter(d => d.value > 0);

  return (
    <div className="glass overflow-hidden p-4 rounded-2xl border border-white/10">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <div className="text-[0.65rem] uppercase tracking-[0.22em] text-muted">WORKER ACTION EXECUTION CENTER</div>
          <div className="mt-1 text-sm text-frost/70">Auto-approved actions executing with live logs</div>
        </div>
        <div className="rounded-full bg-signal/20 px-3 py-1 text-[0.7rem] font-semibold uppercase tracking-[0.08em] text-signal">
          {pendingCount} EXECUTING
        </div>
      </div>

      <div className="mb-4 grid gap-4 lg:grid-cols-[200px_1fr]">
        <div className="flex flex-col items-center justify-center rounded-2xl border border-white/10 bg-black/20 p-4">
          <div className="text-[0.6rem] uppercase tracking-[0.2em] text-muted mb-2">Execution Status</div>
          <div className="h-[120px] w-full">
            {donutData.length > 0 ? (
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie
                    data={donutData}
                    cx="50%"
                    cy="50%"
                    innerRadius={40}
                    outerRadius={55}
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
              <div className="flex h-full items-center justify-center text-xs text-muted">No actions</div>
            )}
          </div>
          <div className="mt-2 flex gap-3 text-[0.55rem] uppercase tracking-wider text-muted">
            <div className="flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-emerald-300"></span> Done</div>
            <div className="flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-amber"></span> Wait</div>
            <div className="flex items-center gap-1"><span className="h-2 w-2 rounded-full bg-signal"></span> Fail</div>
          </div>
        </div>

        <div className="space-y-2 max-h-[300px] overflow-y-auto pr-2">
        {executingActions.length === 0 ? (
          <div className="rounded-2xl border border-white/10 bg-black/25 px-3 py-4 text-center text-[0.68rem] uppercase tracking-[0.16em] text-muted">
            No actions executing. Auto-approved actions will appear here.
          </div>
        ) : (
          <AnimatePresence initial={false}>
            {executingActions.map((action) => {
              const open = expandedId === action.id;
              return (
                <motion.div
                  key={action.id}
                  layout
                  initial={{ opacity: 0, y: -10 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, height: 0 }}
                  transition={{ duration: 0.3 }}
                  className="rounded-2xl border border-white/10 bg-black/25 overflow-hidden"
                >
                  <div
                    onClick={() => setExpandedId(open ? null : action.id)}
                    className="cursor-pointer p-3 hover:bg-white/[0.02] transition"
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2 gap-y-1 flex-wrap">
                          <span className="text-[0.6rem] uppercase tracking-[0.18em] text-amber font-semibold">{action.jiraKey}</span>
                          <StatusBadge status={action.status} progress={action.progress} />
                          <span className="text-[0.55rem] uppercase tracking-[0.16em] text-muted">{action.priorityLevel}</span>
                        </div>
                        <div className="mt-2 text-sm font-semibold text-frost">{action.actionName}</div>
                        <div className="mt-1 text-[0.65rem] text-muted">
                          {action.service} • {action.kraCode || "No KRA"}
                          {action.jobId && (
                            <span className="ml-2 font-mono text-white/30">job:{action.jobId.slice(0, 8)}</span>
                          )}
                        </div>
                        <div className="mt-2 text-[0.68rem] text-frost/75">{action.actionDescription}</div>

                        {action.summary && (
                          <div className="mt-3 rounded-lg border border-frost/30 bg-frost/5 p-3 cursor-text" onClick={(e) => e.stopPropagation()}>
                            <div className="text-[0.6rem] uppercase tracking-[0.18em] text-frost/70 mb-2 font-semibold">EXECUTION SUMMARY</div>
                            <div className="text-[0.65rem] text-frost whitespace-pre-wrap leading-relaxed">{action.summary}</div>
                            {action.sandboxPath && action.status === "completed" && (
                              <button 
                                onClick={(e) => {
                                  e.stopPropagation();
                                  const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:6001";
                                  window.open(`${apiUrl}/download_sandbox?path=${encodeURIComponent(action.sandboxPath!)}`, '_blank');
                                }}
                                className="mt-3 flex items-center gap-2 rounded border border-frost/30 bg-frost/10 px-3 py-1.5 text-[0.65rem] uppercase tracking-[0.1em] text-frost hover:bg-frost/20 transition"
                              >
                                <Download size={12} />
                                Download Execution Artifacts
                              </button>
                            )}
                          </div>
                        )}

                        {action.jobMessage && (action.status === "running" || action.status === "pending") && (
                          <div className="mt-1 text-[0.62rem] text-blue-300/70 italic">{action.jobMessage}</div>
                        )}
                        {action.status === "running" && (
                          <div className="mt-2 h-0.5 w-full rounded-full bg-white/10 overflow-hidden">
                            <motion.div
                              className="h-full bg-signal/60 rounded-full"
                              initial={{ width: "5%" }}
                              animate={{ width: `${action.progress ?? 5}%` }}
                              transition={{ duration: 0.5 }}
                            />
                          </div>
                        )}
                      </div>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          removeAction(action.id);
                        }}
                        className="text-muted hover:text-frost transition"
                      >
                        <X size={16} />
                      </button>
                    </div>
                  </div>

                  {open && (
                    <motion.div
                      initial={{ opacity: 0, height: 0 }}
                      animate={{ opacity: 1, height: "auto" }}
                      exit={{ opacity: 0, height: 0 }}
                      className="border-t border-white/8 bg-black/40 p-3"
                    >
                      <div className="mb-3">
                        <div className="text-[0.6rem] uppercase tracking-[0.18em] text-muted mb-2">EXECUTION STEPS</div>
                        <ol className="space-y-1 text-[0.65rem] text-frost/80">
                          {action.steps.map((step, idx) => (
                            <li key={idx} className="flex gap-2">
                              <span className="text-muted shrink-0">{idx + 1}.</span>
                              <span>{step}</span>
                            </li>
                          ))}
                        </ol>
                      </div>

                      {action.errorMessage && (
                        <div className="mb-3 rounded-lg border border-signal/30 bg-signal/10 p-2">
                          <div className="text-[0.6rem] uppercase tracking-[0.18em] text-signal mb-1 font-semibold">ERROR</div>
                          <div className="text-[0.65rem] text-signal/90">{action.errorMessage}</div>
                        </div>
                      )}

                      {action.status === "awaiting_input" && action.questions && action.questions.length > 0 && (
                        <div className="mb-3 rounded-lg border border-blue-400/30 bg-blue-400/10 p-3">
                          <div className="text-[0.6rem] uppercase tracking-[0.18em] text-blue-300 mb-2 font-semibold">AGENT NEEDS INPUT</div>
                          <div className="space-y-3">
                            {action.questions.map((q, qIdx) => (
                              <div key={qIdx}>
                                <div className="text-[0.65rem] text-frost mb-1.5">{q}</div>
                                <input
                                  type="text"
                                  placeholder="Type your answer here..."
                                  className="w-full bg-black/40 border border-white/10 rounded px-2 py-1.5 text-[0.65rem] text-frost outline-none focus:border-blue-400/50 transition"
                                  id={`input-${action.id}-${qIdx}`}
                                />
                              </div>
                            ))}
                            <button
                              onClick={() => submitAnswers(action.id)}
                              className="mt-2 w-full rounded border border-blue-400/40 bg-blue-400/20 px-3 py-1.5 text-[0.65rem] uppercase tracking-[0.1em] text-blue-200 hover:bg-blue-400/30 transition"
                            >
                              Submit Responses
                            </button>
                          </div>
                        </div>
                      )}

                      <div>
                        <div className="flex items-center justify-between mb-2">
                          <div className="text-[0.6rem] uppercase tracking-[0.18em] text-muted">LIVE EXECUTION LOGS</div>
                          <div className="text-[0.55rem] text-muted">{action.logs.length} entries</div>
                        </div>
                        <div
                          ref={(el) => {
                            if (el) logsContainerRefs.current.set(action.id, el);
                            else logsContainerRefs.current.delete(action.id);
                          }}
                          className="max-h-[300px] space-y-0.5 overflow-y-auto pr-1 scrollbar-mini font-mono"
                        >
                          {action.logs.length ? (
                            action.logs.map((log, idx) => (
                              <div
                                key={`${action.id}-${log.timestamp}-${idx}`}
                                className={cx(
                                  "border-l-2 px-2 py-1 text-[0.65rem] transition",
                                  "bg-white/[0.025] hover:bg-white/[0.05]",
                                  levelBorder[log.level] || "border-l-white/20"
                                )}
                              >
                                <div className="flex items-start gap-2">
                                  <span className="text-muted shrink-0">[{new Date(log.timestamp * 1000).toLocaleTimeString()}]</span>
                                  <span className={`font-semibold text-[0.6rem] shrink-0 ${levelColor[log.level] || "text-frost"}`}>
                                    {log.level}
                                  </span>
                                  <span className="flex-1 text-frost/75 break-all whitespace-pre-wrap leading-relaxed">{log.message}</span>
                                </div>
                              </div>
                            ))
                          ) : (
                            <div className="text-[0.65rem] text-muted text-center py-4">
                              {action.status === "running" || action.status === "pending"
                                ? "⏳ Waiting for logs..."
                                : "No logs captured"}
                            </div>
                          )}
                        </div>
                      </div>

                      <div className="mt-3 pt-3 border-t border-white/8 text-[0.6rem] text-muted">
                        Started: {new Date(action.startedAt).toLocaleTimeString()}{" "}
                        {action.completedAt && `• Completed: ${new Date(action.completedAt).toLocaleTimeString()}`}
                        {action.jobId && ` • Job: ${action.jobId.slice(0, 8)}…`}
                      </div>
                    </motion.div>
                  )}
                </motion.div>
              );
            })}
          </AnimatePresence>
        )}
      </div>
      </div>
    </div>
  );
});

export function ExecuteActionButton({ action, onExecute }: { action: any; onExecute: (action: any) => void }) {
  return (
    <button
      onClick={() => onExecute(action)}
      className="inline-flex items-center gap-1.5 border border-signal/30 bg-signal/10 px-2.5 py-1 text-[0.65rem] uppercase tracking-[0.12em] text-signal hover:bg-signal/15 transition rounded"
    >
      <Play size={11} /> Execute
    </button>
  );
}
