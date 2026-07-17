"use client";

import { orchestrateAction, getJobStatus, fetchBackendLogs, listDigitalWorkerRequests, getApiUrl, getDigitalWorkerSettings, updateDigitalWorkerSettings, type BackendLog } from "@/services/api";
import { motion, AnimatePresence } from "framer-motion";
import { useEffect, useRef, useState, forwardRef, useImperativeHandle } from "react";
import { Play, CheckCircle2, AlertTriangle, Clock, X, Loader2, Download, RotateCcw, Trash2, StopCircle, Octagon } from "lucide-react";
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
  status: "pending" | "running" | "completed" | "failed" | "awaiting_input" | "exhausted" | "destroying" | "destroyed" | "stopping" | "stopped" | "skipped";
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
    exhausted: "border-orange-400/40 bg-orange-400/12 text-orange-300",
    destroying: "border-red-500/40 bg-red-500/12 text-red-400",
    destroyed: "border-gray-500/40 bg-gray-500/12 text-gray-400",
    stopping: "border-red-400/40 bg-red-400/12 text-red-300",
    stopped: "border-red-400/40 bg-red-400/12 text-red-300",
    skipped: "border-slate-400/40 bg-slate-400/12 text-slate-300"
  };
  const icons: Record<string, JSX.Element> = {
    pending: <Clock size={12} />,
    running: <Loader2 size={12} className="animate-spin" />,
    completed: <CheckCircle2 size={12} />,
    failed: <AlertTriangle size={12} />,
    awaiting_input: <Clock size={12} />,
    exhausted: <AlertTriangle size={12} />,
    destroying: <Loader2 size={12} className="animate-spin" />,
    destroyed: <Trash2 size={12} />,
    stopping: <Loader2 size={12} className="animate-spin" />,
    stopped: <StopCircle size={12} />,
    skipped: <Octagon size={12} />
  };

  const displayLabel =
    status === "awaiting_input"
      ? "AWAITING INPUT"
      : status === "destroying"
      ? "DESTROYING..."
      : status === "stopping"
      ? "STOPPING..."
      : status.toUpperCase();

  return (
    <div className={cx("inline-flex items-center gap-1.5 border px-2 py-1 text-[0.6rem] uppercase tracking-[0.18em] rounded", tones[status] || "border-white/20 bg-white/10")}>
      {icons[status]}
      {displayLabel}
    </div>
  );
}

export type WorkerActionExecutionCenterHandle = {
  execute: (action: any) => void;
  submitActionAnswers: (actionId: string, answers: string[]) => Promise<void>;
};

export const WorkerActionExecutionCenter = forwardRef<
  WorkerActionExecutionCenterHandle,
  { 
    actions?: any[]; 
    onActionApproved?: (action: any) => void;
    onActionCompleted?: (kraCode: string | undefined, actionId: string) => void;
    onActionDestroyed?: (kraCode: string | undefined, actionId: string) => void;
    onPendingHitlChange?: (pendingRequests: {actionId: string, actionName: string, kraCode: string, questions: string[]}[]) => void;
  }
>(function WorkerActionExecutionCenter({ onActionApproved, onActionCompleted, onActionDestroyed, onPendingHitlChange }, ref) {
  const [executingActions, setExecutingActions] = useState<ExecutingAction[]>([]);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [confirmDestroyId, setConfirmDestroyId] = useState<string | null>(null);
  const [confirmStopId, setConfirmStopId] = useState<string | null>(null);
  const [globalSearchQuery, setGlobalSearchQuery] = useState("");
  const [logSearchQueries, setLogSearchQueries] = useState<Record<string, string>>({});
  const [maxIterations, setMaxIterations] = useState(5);
  const [timeoutMins, setTimeoutMins] = useState(5);
  const dismissedJobIdsRef = useRef<Set<string>>(new Set());
  // Per-action refs for the logs scroll container — keyed by action id
  const logsContainerRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  const pollIntervalsRef = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map());
  const logsIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const cachedLogsRef = useRef<BackendLog[]>([]);

  // ── Stable refs so setInterval callbacks always read current values ──────────
  // setInterval closes over values from the render it was created in; using refs
  // ensures the interval always sees the latest state/props without being recreated.
  const executingActionsRef = useRef<ExecutingAction[]>(executingActions);
  const onActionCompletedRef = useRef(onActionCompleted);
  const onActionApprovedRef = useRef(onActionApproved);
  const onPendingHitlChangeRef = useRef(onPendingHitlChange);
  useEffect(() => { executingActionsRef.current = executingActions; }, [executingActions]);
  useEffect(() => { onActionCompletedRef.current = onActionCompleted; }, [onActionCompleted]);
  useEffect(() => { onActionApprovedRef.current  = onActionApproved;  }, [onActionApproved]);
  useEffect(() => { onPendingHitlChangeRef.current = onPendingHitlChange; }, [onPendingHitlChange]);

  useEffect(() => {
    getDigitalWorkerSettings().then(settings => {
      setMaxIterations(settings.max_iterations);
      setTimeoutMins(Math.floor(settings.command_timeout / 60));
    }).catch(err => console.error("Failed to load digital worker settings", err));
    
    try {
      const stored = localStorage.getItem("dw_dismissed_job_ids");
      if (stored) {
        const ids = JSON.parse(stored);
        if (Array.isArray(ids)) {
          dismissedJobIdsRef.current = new Set(ids);
        }
      }
    } catch (e) {
      console.error("Failed to load dismissed job IDs", e);
    }
  }, []);

  useEffect(() => {
    if (onPendingHitlChangeRef.current) {
      const hitlActions = executingActions.filter(a => a.status === "awaiting_input" && a.questions && a.questions.length > 0);
      onPendingHitlChangeRef.current(hitlActions.map(a => ({
        actionId: a.id,
        actionName: a.actionName || "UNKNOWN ACTION",
        kraCode: a.kraCode || "KRA-XX",
        questions: a.questions!
      })));
    }
  }, [executingActions]);

  const formatDuration = (ms: number) => {
    const totalSeconds = Math.floor(ms / 1000);
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    return `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
  };

  useImperativeHandle(ref, () => ({
    execute: handleExecuteAction,
    submitActionAnswers: async (actionId, answers) => {
      await submitAnswers(actionId, answers);
    }
  }));

  // Cleanup all intervals/timeouts on unmount
  useEffect(() => {
    return () => {
      pollIntervalsRef.current.forEach((timer) => clearTimeout(timer));
      pollIntervalsRef.current.clear();
      if (logsIntervalRef.current) {
        clearTimeout(logsIntervalRef.current);
        logsIntervalRef.current = null;
      }
    };
  }, []);

  // --- Auto-discover Digital Worker jobs (Jira webhooks) ---
  useEffect(() => {
    const pollBackgroundJobs = async () => {
      try {
        const res = await listDigitalWorkerRequests();
        if (res && res.requests) {
          // Discover jobs that belong in the Worker Action Execution Center.
          //
          // Three phases to handle for a DW job that needs human approval:
          //   Phase 2 — Initial running (classifying/planning):
          //             status=running, requires_approval=false, decision_mode=null
          //             → EXCLUDE: still processing, hasn't reached the approval gate yet
          //   Phase 3 — Awaiting human decision:
          //             status=awaiting_approval, requires_approval=true
          //             → EXCLUDE: belongs in Human Approval Center only
          //   Phase 4 — Post-approval execution:
          //             status=running, requires_approval=false, approved_by_human=true
          //             → INCLUDE: user clicked Approve, execution is live
          //
          // Auto-execute jobs (no approval needed):
          //   decision_mode="auto_execute" or completed/failed → always include
          const dwJobs = res.requests.filter(r => {
            // Must be in a trackable state
            if (r.requires_approval) return false;
            if (r.status !== "running" && r.status !== "completed" && r.status !== "failed") return false;
            // Completed/failed jobs always surface (post-execution summary)
            if (r.status === "completed" || r.status === "failed") return true;
            // For running jobs, apply smart gating:
            // - decision_mode is null → still in early processing (Phase 2) → exclude
            // - decision_mode is auto_execute → no approval needed → include
            // - decision_mode is await_approval + approved_by_human → approved (Phase 4) → include
            // - decision_mode is await_approval + NOT approved → still pre-approval → exclude
            if (!r.decision_mode) return false;
            if (r.decision_mode.toLowerCase() === "auto_execute") return true;
            if (r.decision_mode.toLowerCase() === "await_approval") return r.approved_by_human === true;
            return true;
          });
          
          setExecutingActions(current => {
            // Create sets of both job IDs and action IDs to accurately detect duplicates
            // even after an action's jobId has been updated via submitAnswers
            const currentJobIds = new Set(current.map(a => a.jobId));
            const currentActionIds = new Set(current.map(a => a.id));
            
            // Build a set of all active KRA action names to filter out duplicate webhooks
            const kraActionNames = new Set<string>();
            current.forEach(a => {
               if (a.kraCode && a.kraCode.toUpperCase() !== "JIRA") {
                  kraActionNames.add(a.actionName.toLowerCase().trim());
               }
            });
            dwJobs.forEach(j => {
               const cat = (j.category || j.source || "JIRA").toUpperCase();
               if (cat.startsWith("KRA") && j.title) {
                  kraActionNames.add(j.title.toLowerCase().trim());
               }
            });

            const newActions: ExecutingAction[] = [];
            
            for (const job of dwJobs) {
              const titleLower = (job.title || "").toLowerCase().trim();
              const isWebhook = !job.category;
              
              // Skip webhooks if a KRA action with the exact same name already exists
              if (isWebhook && kraActionNames.has(titleLower)) {
                 continue;
              }

              const actionId = `dw-${job.job_id}`;
              if (!currentJobIds.has(job.job_id) && !currentActionIds.has(actionId) && !dismissedJobIdsRef.current.has(job.job_id)) {
                newActions.push({
                  id: actionId,
                  actionName: job.title || "Webhook Task",
                  actionDescription: job.reason || "Automated Digital Worker execution",
                  service: job.platform || "AWS",
                  kraCode: job.category || (job.source ? job.source.toUpperCase() : "JIRA"),
                  priorityLevel: job.priority || "P3",
                  steps: [],
                  jiraKey: job.external_id || job.job_id.substring(0,8),
                  jiraUrl: job.external_id ?? undefined,
                  status: job.status === "running" ? "running" : (job.status === "completed" ? "completed" : (job.status === "skipped" ? "skipped" : "failed")),
                  jobId: job.job_id,
                  threadId: job.job_id,
                  startedAt: job.started_at ? job.started_at * 1000 : Date.now(),
                  logs: [],
                  progress: job.progress || 0,
                  jobMessage: job.message,
                  sandboxPath: (job as any).sandbox_path
                });
              }
            }
            
            if (newActions.length > 0) {
              // Start polling for the newly discovered running jobs
              setTimeout(() => {
                newActions.forEach(a => {
                   if (a.status === "running") {
                     startPolling(a.id, a.jobId, a.startedAt, a.jiraKey);
                   } else if (a.status === "completed") {
                     // Verify if the job was actually exhausted or failed
                     getJobStatus(a.jobId).then(jobStatus => {
                        const result = jobStatus.result as any;
                        const statusCode = result?.statusCode ?? 0;
                        let finalStatus = "completed";
                        if (statusCode === 207) finalStatus = "exhausted";
                        else if (statusCode !== 200 && statusCode !== 0) finalStatus = "failed";
                        if (finalStatus !== "completed") {
                           setExecutingActions(curr => curr.map(currAction => currAction.id === a.id ? { ...currAction, status: finalStatus as any } : currAction));
                        }
                     }).catch(e => console.error("Failed to verify completed job status", e));
                   }
                });
              }, 100);
              return [...newActions, ...current];
            }
            return current;
          });
        }
      } catch (e: any) {
        // AbortError = 8 s poll timed out (backend busy with a running job).
        // Swallow silently — next poll cycle (5 s) will retry automatically.
        if (e?.name === "AbortError") return;
        console.error("Failed to fetch background digital worker jobs:", e);
      }
    };
    
    // Initial fetch, then schedule next fetch after completion
    let timeoutId: NodeJS.Timeout;
    let isMounted = true;
    const runPoll = async () => {
      await pollBackgroundJobs();
      if (isMounted) {
        timeoutId = setTimeout(runPoll, 5000);
      }
    };
    runPoll();
    
    return () => {
      isMounted = false;
      clearTimeout(timeoutId);
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
    const timer = pollIntervalsRef.current.get(actionId);
    if (timer) {
      clearTimeout(timer);
      pollIntervalsRef.current.delete(actionId);
    }
  };

  const startLogsPolling = () => {
    if (logsIntervalRef.current) return;
    
    // Set to a dummy value so we know it's active
    logsIntervalRef.current = setTimeout(() => {}, 0) as any;
    
    const runLogsPoll = async () => {
      try {
        cachedLogsRef.current = await fetchBackendLogs(1000, 0);
      } catch (error) {
        console.error("Failed to fetch logs:", error);
      } finally {
        if (logsIntervalRef.current !== null) {
          logsIntervalRef.current = setTimeout(runLogsPoll, 1000);
        }
      }
    };
    runLogsPoll();
  };

  const startPolling = (actionId: string, jobId: string, startedAt: number, jiraKey: string) => {
    startLogsPolling();
    const runJobPoll = async () => {
      try {
        const jobStatus = await getJobStatus(jobId);
        const allLogs = cachedLogsRef.current;

        const jobIdLower = jobId ? jobId.toLowerCase().trim() : "";

        // Match only by exact job_id (case-insensitive, trimmed).
        let actionLogs = allLogs.filter((log) =>
          log.job_id?.toLowerCase().trim() === jobIdLower
        );

        const jobDone =
          jobStatus.status === "completed" ||
          jobStatus.status === "failed" ||
          jobStatus.status === "stopped" ||
          jobStatus.status === "skipped" ||
          jobStatus.status === "not_found";

        if (jobDone) {
          stopPolling(actionId);
          // Clear any pending stop-confirm dialog for this action so it can't get stuck
          setConfirmStopId((cur) => (cur === actionId ? null : cur));

          // Force a final log fetch to catch any burst of logs (like tracebacks) just before the job died
          try {
            const finalLogs = await fetchBackendLogs(2000, 0);
            cachedLogsRef.current = finalLogs;
            // Same exact job_id match for the final log burst.
            actionLogs = finalLogs.filter((log: any) =>
              log.job_id?.toLowerCase().trim() === jobIdLower
            );
          } catch (e) {
            console.error("Failed to fetch final logs", e);
          }

          const result = jobStatus.result as any;
          const statusCode: number = result?.statusCode ?? 0;

          let finalStatus: ExecutingAction["status"] = "failed";
          if (jobStatus.status === "stopped") {
            finalStatus = "stopped";
          } else if (jobStatus.status === "completed") {
            if (statusCode === 200 || statusCode === 0) finalStatus = "completed";
            else if (statusCode === 202) finalStatus = "awaiting_input";
            else if (statusCode === 207) finalStatus = "exhausted";
            else finalStatus = "failed";
          } else if (jobStatus.status === "failed") {
            finalStatus = "failed";
          } else if (jobStatus.status === "skipped") {
            finalStatus = "skipped";
          }

          const errorMsg =
            jobStatus.error ||
            (jobStatus.status === "failed" ? jobStatus.message : "") ||
            (result?.exception ?? "");

          // ✅ Read from ref — always the latest executingActions, not stale closure value
          const currentAction = executingActionsRef.current.find(a => a.id === actionId);
          const kraCodeToFire = currentAction?.kraCode || "";
          
          let updatedActionForCallback: ExecutingAction | null = null;

          setExecutingActions((current) =>
            current.map((a) => {
              if (a.id === actionId) {
                  // Fallback for poorly formed backend responses that return 202 but forget the questions array
                  const fallbackQuestions = finalStatus === "awaiting_input" ? ["Please provide the required input to proceed."] : [];
                  const extractedQuestions = (result?.questions && result.questions.length > 0) ? result.questions : fallbackQuestions;

                  // Pull LLM summary from all possible result shapes:
                  // • result.summary                    → PipelineResponse / KRA orchestrate path
                  // • result.output.execution.detail    → DW graph path (ExecutionOutcome.detail)
                  // • result.detail                     → direct detail fallback
                  // • jobStatus.message                 → last-resort human-readable status
                  const llmSummary =
                    result?.summary ||
                    (result?.output as any)?.execution?.detail ||
                    result?.detail ||
                    jobStatus.message ||
                    "";

                  const updated: ExecutingAction = {
                    ...a,
                    status: finalStatus,
                    threadId: result?.thread_id || "",
                    completedAt: Date.now(),
                    logs: actionLogs.length > 0 ? actionLogs : a.logs,
                    errorMessage: errorMsg,
                    progress: 100,
                    jobMessage: jobStatus.message,
                    summary: llmSummary,
                    questions: extractedQuestions,
                    sandboxPath: finalStatus === "stopped" ? "" : ((jobStatus as any).sandbox_path || result?.sandbox_path || a.sandboxPath || "")
                  };
                  updatedActionForCallback = updated;
                  return updated;
              }
              return a;
            })
          );
          
          // Note: HITL state is now synced declaratively via useEffect above
          
          scrollLogsToBottom(actionId);

          if (finalStatus === "completed") {
            // ✅ Read callbacks from refs — always latest props, not stale closure
            if (onActionApprovedRef.current && currentAction) onActionApprovedRef.current({ ...currentAction, status: "completed", progress: 100 });
            if (onActionCompletedRef.current) onActionCompletedRef.current(kraCodeToFire, actionId);
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
          
          if (pollIntervalsRef.current.has(actionId)) {
            pollIntervalsRef.current.set(actionId, setTimeout(runJobPoll, 5000));
          }
        }
      } catch (error: any) {
        if (error?.name !== "AbortError") {
          console.error(`Failed to poll job ${jobId}:`, error);
        }
        if (pollIntervalsRef.current.has(actionId)) {
          pollIntervalsRef.current.set(actionId, setTimeout(runJobPoll, 5000));
        }
      }
    };

    pollIntervalsRef.current.set(actionId, setTimeout(runJobPoll, 5000));
  };


  const submitAnswers = async (actionId: string, providedAnswers?: string[]) => {
    // Reading from ref ensures programmatic calls use latest state
    const action = executingActionsRef.current.find((a) => a.id === actionId);
    if (!action || !action.questions || action.status !== "awaiting_input") return;

    // Collect answers from DOM if not provided programmatically
    const answers: string[] = providedAnswers || [];
    if (!providedAnswers) {
      for (let i = 0; i < action.questions.length; i++) {
        const el = document.getElementById(`input-${actionId}-${i}`) as HTMLInputElement;
        answers.push(el?.value || "");
      }
    }

    // Reset status to running, clear completedAt, and clear questions
    setExecutingActions((current) =>
      current.map((a) =>
        a.id === actionId
          ? {
              ...a,
              status: "running",
              completedAt: undefined,
              progress: 5,
              jobMessage: "Resuming orchestration with answers...",
              errorMessage: "",
              questions: undefined,
              summary: undefined
            }
          : a
      )
    );

    try {
      // Call the dedicated resume endpoint — reuses the SAME job_id
      // No new job_id created, no frontend tracking complexity
      const resumeResp = await fetch(getApiUrl(`/orchestrate/${action.jobId}/resume`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ answers }),
      });

      if (!resumeResp.ok) {
        const err = await resumeResp.json().catch(() => ({ detail: resumeResp.statusText }));
        throw new Error(err.detail || `Resume failed: ${resumeResp.status}`);
      }

      // job_id is exactly the same — just restart polling on it
      startPolling(actionId, action.jobId, Date.now(), action.jiraKey);
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

    // Safety-net dedup: skip if an active entry with the same jiraKey, actionName, and resourceId already exists
    const resourceId = action.resourceId || "";
    const isDuplicate = executingActions.some(
      (a) =>
        (a.status === "pending" || a.status === "running") &&
        a.jiraKey === jiraKey &&
        a.actionName === actionName &&
        (a as any).resourceId === resourceId
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
      jobMessage: "Submitting job...",
      resourceId: resourceId
    } as ExecutingAction;

    setExecutingActions((current) => [executing, ...current]);

    try {
      const jobResponse = await orchestrateAction({
        action: {
          actionName: executing.actionName,
          actionDescription: executing.actionDescription,
          steps: executing.steps,
          service: executing.service || "AWS",
          kraCode: executing.kraCode,
          priorityLevel: executing.priorityLevel,
          detectorId: action.detectorId,
          resourceArn: action.region === "us-east-1" ? action.resourceId : action.resourceId, // dummy check to pass resourceId
          region: action.region || "us-east-1"
        },
        jiraUrl: executing.jiraUrl,
        command_timeout: timeoutMins * 60,
        max_iterations: maxIterations
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
    const actionToRemove = executingActionsRef.current.find(a => a.id === id);
    if (actionToRemove?.jobId) {
      dismissedJobIdsRef.current.add(actionToRemove.jobId);
      try {
        localStorage.setItem("dw_dismissed_job_ids", JSON.stringify(Array.from(dismissedJobIdsRef.current)));
      } catch (e) {
        console.error("Failed to save dismissed job IDs", e);
      }
    }
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
  const pendingCount = executingActions.filter(a => a.status === "pending" || a.status === "running" || a.status === "awaiting_input" || a.status === "stopping").length;
  const failedCount = executingActions.filter(a => a.status === "failed" || a.status === "exhausted").length;

  const donutData = [
    { name: "Executed", value: executedCount, color: "#4ade80" },
    { name: "Pending", value: pendingCount, color: "#ffb800" },
    { name: "Failed", value: failedCount, color: "#ff3b3b" }
  ].filter(d => d.value > 0);

  return (
    <div className="glass overflow-hidden p-4 rounded-2xl border border-white/10">
      <div className="mb-4 flex items-center justify-between">
        <div className="flex items-center gap-6">
          <div>
            <div className="text-[0.65rem] uppercase tracking-[0.22em] text-muted">WORKER ACTION EXECUTION CENTER</div>
            <div className="mt-1 text-sm text-frost/70">Auto-approved actions executing with live logs</div>
          </div>
          <div className="flex items-center gap-3">
            <input 
              type="text" 
              placeholder="Search by Action Name or Job ID..." 
              className="bg-black/40 border border-white/10 rounded-md px-3 py-1.5 text-xs text-frost focus:outline-none focus:border-frost/50 transition-colors w-64"
              value={globalSearchQuery}
              onChange={(e) => setGlobalSearchQuery(e.target.value)}
            />
            <div className="flex items-center gap-2 border border-white/10 bg-black/40 rounded-md px-2 py-1.5">
              <span className="text-[0.6rem] uppercase tracking-[0.1em] text-muted">Iterations</span>
              <input
                type="number"
                min={1}
                max={20}
                className="bg-transparent border-none text-xs text-frost w-10 text-center focus:outline-none p-0 m-0"
                value={maxIterations}
                onChange={(e) => setMaxIterations(Number(e.target.value) || 5)}
              />
            </div>
            <div className="flex items-center gap-2 border border-white/10 bg-black/40 rounded-md px-2 py-1.5">
              <span className="text-[0.6rem] uppercase tracking-[0.1em] text-muted">Timeout (m)</span>
              <input
                type="number"
                min={1}
                max={60}
                className="bg-transparent border-none text-xs text-frost w-10 text-center focus:outline-none p-0 m-0"
                value={timeoutMins}
                onChange={(e) => setTimeoutMins(Number(e.target.value) || 5)}
              />
            </div>
            <button
              onClick={() => {
                updateDigitalWorkerSettings({ max_iterations: maxIterations, command_timeout: timeoutMins * 60 })
                  .then(() => alert("Default settings saved successfully! Webhooks will now use these settings."))
                  .catch(err => alert("Failed to save settings: " + err.message));
              }}
              className="text-[0.65rem] uppercase tracking-[0.1em] bg-blue-500/20 hover:bg-blue-500/30 text-blue-300 border border-blue-500/30 px-3 py-1.5 rounded-md transition-colors whitespace-nowrap"
            >
              Save as Default
            </button>
          </div>
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

        <div className="space-y-2 max-h-[600px] overflow-y-auto pr-2">
        {executingActions.length === 0 ? (
          <div className="rounded-2xl border border-white/10 bg-black/25 px-3 py-4 text-center text-[0.68rem] uppercase tracking-[0.16em] text-muted">
            No actions executing. Auto-approved actions will appear here.
          </div>
        ) : (
          <AnimatePresence initial={false}>
            {executingActions
              .filter(action => {
                const q = globalSearchQuery.toLowerCase();
                if (!q) return true;
                const name = (action.actionName || "").toLowerCase();
                const job = (action.jobId || "").toLowerCase();
                const kra = (action.kraCode || "").toLowerCase();
                return name.includes(q) || job.includes(q) || kra.includes(q);
              })
              .map((action) => {
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
                            <span className="ml-2 font-mono text-white/30">JOBID:{action.jobId}</span>
                          )}
                        </div>
                        <div className="mt-2 text-[0.68rem] text-frost/75">{action.actionDescription}</div>

                        {action.summary && (
                          <div className="mt-3 rounded-lg border border-frost/30 bg-frost/5 p-3 cursor-text" onClick={(e) => e.stopPropagation()}>
                            <div className="flex items-center justify-between mb-3 border-b border-white/10 pb-2">
                              <div className="text-[0.6rem] uppercase tracking-[0.18em] text-frost/70 font-semibold">EXECUTION SUMMARY</div>
                              <div className="flex gap-4 text-[0.55rem] tracking-[0.05em] text-muted">
                                <div><span className="text-frost/40 mr-1">START:</span>{new Date(action.startedAt).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', dateStyle: 'short', timeStyle: 'medium' })}</div>
                                {action.completedAt && (
                                  <>
                                    <div><span className="text-frost/40 mr-1">END:</span>{new Date(action.completedAt).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', dateStyle: 'short', timeStyle: 'medium' })}</div>
                                    <div className="font-mono text-frost"><span className="text-frost/40 mr-1 font-sans">TIME:</span>{formatDuration(action.completedAt - action.startedAt)}</div>
                                  </>
                                )}
                              </div>
                            </div>
                            <div className="text-[0.65rem] text-frost whitespace-pre-wrap leading-relaxed">{action.summary}</div>
                          </div>
                        )}

                        {/* Action Buttons — only for terminal states that have buttons */}
                        {(action.sandboxPath || action.status === "failed" || action.status === "exhausted" || action.status === "stopped") && (
                          <div className="flex flex-wrap gap-2 mt-3" onClick={(e) => e.stopPropagation()}>
                            {action.sandboxPath && action.status === "completed" && (
                              <>
                              <button 
                                onClick={(e) => {
                                  e.stopPropagation();
                                  const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:6001";
                                  window.open(`${apiUrl}/download_sandbox?path=${encodeURIComponent(action.sandboxPath!)}`, '_blank');
                                }}
                                className="flex items-center gap-2 rounded border border-frost/30 bg-frost/10 px-3 py-1.5 text-[0.65rem] uppercase tracking-[0.1em] text-frost hover:bg-frost/20 transition"
                              >
                                <Download size={12} />
                                Download Execution Artifacts
                              </button>
                              {confirmDestroyId === action.id ? (
                                <div className="flex items-center gap-2 rounded border border-red-500/30 bg-red-500/5 px-2 py-1 text-[0.65rem] tracking-[0.1em] text-red-400">
                                  <span className="mr-2 uppercase">Are you sure?</span>
                                  <button
                                    onClick={async (e) => {
                                      e.stopPropagation();
                                      setConfirmDestroyId(null);
                                      
                                      // Instantly update UI to show it's destroying
                                      setExecutingActions(current => 
                                        current.map(a => a.id === action.id ? { ...a, status: "destroying" } : a)
                                      );

                                      startLogsPolling();
                                      const logUpdater = setInterval(() => {
                                        const allLogs = cachedLogsRef.current;
                                        const jobIdLower = action.jobId.toLowerCase();
                                        const actionLogs = allLogs.filter(log => log.job_id && log.job_id.toLowerCase() === jobIdLower);
                                        if (actionLogs.length > 0) {
                                            setExecutingActions(current => 
                                                current.map(a => a.id === action.id ? { ...a, logs: actionLogs } : a)
                                            );
                                        }
                                      }, 2000);

                                      try {
                                        const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:6001";
                                        const response = await fetch(`${apiUrl}/destroy_sandbox`, {
                                          method: "POST",
                                          headers: { "Content-Type": "application/json" },
                                          body: JSON.stringify({ path: action.sandboxPath!, job_id: action.jobId, jiraUrl: action.jiraUrl })
                                        });
                                        if (!response.ok) {
                                          const errData = await response.json().catch(() => ({}));
                                          alert("Failed to destroy infrastructure: " + (errData.error || errData.details || response.statusText));
                                          setExecutingActions(current => 
                                            current.map(a => a.id === action.id ? { ...a, status: "completed" } : a)
                                          );
                                          return;
                                        }
                                        // Silently update state to destroyed without popup
                                        setExecutingActions(current => 
                                          current.map(a => a.id === action.id ? { ...a, status: "destroyed" } : a)
                                        );
                                        if (onActionDestroyed) onActionDestroyed(action.kraCode, action.id);
                                      } catch (error) {
                                        console.error(error);
                                        alert("Error occurred while destroying infrastructure.");
                                        setExecutingActions(current => 
                                          current.map(a => a.id === action.id ? { ...a, status: "completed" } : a)
                                        );
                                      } finally {
                                        clearInterval(logUpdater);
                                      }
                                    }}
                                    className="bg-red-500/20 hover:bg-red-500/40 px-2 py-0.5 rounded transition uppercase"
                                  >
                                    Yes, Destroy
                                  </button>
                                  <button
                                    onClick={(e) => {
                                      e.stopPropagation();
                                      setConfirmDestroyId(null);
                                    }}
                                    className="bg-frost/10 hover:bg-frost/20 text-frost px-2 py-0.5 rounded transition uppercase"
                                  >
                                    Cancel
                                  </button>
                                </div>
                              ) : (
                                <button 
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    setConfirmDestroyId(action.id);
                                  }}
                                  className="flex items-center gap-2 rounded border border-red-500/30 bg-red-500/10 px-3 py-1.5 text-[0.65rem] uppercase tracking-[0.1em] text-red-400 hover:bg-red-500/20 transition"
                                >
                                  <Trash2 size={12} />
                                  Destroy Infrastructure
                                </button>
                              )}
                              </>
                            )}


                            {(action.status === "failed" || action.status === "exhausted" || action.status === "stopped") && (
                              <button 
                                onClick={(e) => {
                                  e.stopPropagation();
                                  handleExecuteAction(action);
                                  removeAction(action.id);
                                }}
                                className="flex items-center gap-2 rounded border border-signal/30 bg-signal/10 px-3 py-1.5 text-[0.65rem] uppercase tracking-[0.1em] text-signal hover:bg-signal/20 transition"
                              >
                                <RotateCcw size={12} />
                                Retry Execution
                              </button>
                            )}
                            
                            {action.jobId && (action.status === "completed" || action.status === "failed" || action.status === "exhausted" || action.status === "stopped") && (
                              <button 
                                onClick={(e) => {
                                  e.stopPropagation();
                                  const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:6001";
                                  window.open(`${apiUrl}/orchestrate/logs/${encodeURIComponent(action.jobId)}`, '_blank');
                                }}
                                className="flex items-center gap-2 rounded border border-frost/30 bg-frost/10 px-3 py-1.5 text-[0.65rem] uppercase tracking-[0.1em] text-frost hover:bg-frost/20 transition"
                              >
                                <Download size={12} />
                                Download Logs
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
                      { (action.status === "pending" || action.status === "running") ? (
                        confirmStopId === action.id ? (
                          <div className="flex items-center gap-1 text-[0.6rem] tracking-[0.1em] border border-red-500/30 bg-red-500/5 px-2 py-0.5 rounded" onClick={(e) => e.stopPropagation()}>
                            <span className="mr-1 uppercase text-red-400">Stop?</span>
                            <button
                              onClick={async (e) => {
                                e.stopPropagation();
                                setConfirmStopId(null);
                                // Stop polling FIRST so no poll can race and overwrite 'stopping'
                                stopPolling(action.id);
                                // Show 'stopping' spinner immediately
                                setExecutingActions(current => 
                                  current.map(a => a.id === action.id ? { ...a, status: "stopping" } : a)
                                );
                                try {
                                  const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:6001";
                                  await fetch(`${apiUrl}/orchestrate/stop/${action.jobId}`, { method: "POST" });
                                } catch (error) {
                                  console.error("Failed to stop action:", error);
                                } finally {
                                  // Transition to fully stopped once API call resolves
                                  setExecutingActions(current => 
                                    current.map(a => a.id === action.id && a.status === "stopping" ? { ...a, status: "stopped" } : a)
                                  );
                                }
                              }}
                              className="bg-red-500/20 hover:bg-red-500/40 text-red-400 px-1.5 py-0.5 rounded transition uppercase"
                            >
                              Yes
                            </button>
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                setConfirmStopId(null);
                              }}
                              className="bg-frost/10 hover:bg-frost/20 text-frost px-1.5 py-0.5 rounded transition uppercase"
                            >
                              No
                            </button>
                          </div>
                        ) : (
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              setConfirmStopId(action.id);
                            }}
                            className="flex items-center gap-1 text-[0.6rem] font-semibold uppercase tracking-[0.1em] text-red-400/80 hover:text-red-400 border border-red-500/20 bg-red-500/5 px-2 py-1 rounded transition"
                          >
                            <Octagon size={12} className="mr-1" />
                            Stop Action
                          </button>
                        )
                      ) : action.status === "stopping" ? (
                        // While stopping: show nothing — badge already shows STOPPING...
                        <div className="w-6" />
                      ) : (
                        <button
                          onClick={(e) => {
                            e.stopPropagation();
                            removeAction(action.id);
                          }}
                          className="text-muted hover:text-frost transition"
                        >
                          <X size={16} />
                        </button>
                      )}
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
                          <div className="flex items-center gap-4">
                            <div className="text-[0.6rem] uppercase tracking-[0.18em] text-muted">LIVE EXECUTION LOGS</div>
                            <input 
                              type="text" 
                              placeholder="Search logs..." 
                              className="bg-black/50 border border-white/10 rounded px-2 py-0.5 text-[0.6rem] text-frost focus:outline-none focus:border-frost/50 transition-colors w-48"
                              value={logSearchQueries[action.id] || ""}
                              onChange={(e) => setLogSearchQueries(prev => ({ ...prev, [action.id]: e.target.value }))}
                              onClick={(e) => e.stopPropagation()}
                            />
                          </div>
                          <div className="text-[0.55rem] text-muted">
                            {(() => {
                               const q = logSearchQueries[action.id]?.toLowerCase();
                               const count = q ? action.logs.filter(l => l.message.toLowerCase().includes(q) || l.level.toLowerCase().includes(q)).length : action.logs.length;
                               return `${count} entries`;
                            })()}
                          </div>
                        </div>
                        <div
                          ref={(el) => {
                            if (el) logsContainerRefs.current.set(action.id, el);
                            else logsContainerRefs.current.delete(action.id);
                          }}
                          className="max-h-[600px] space-y-0.5 overflow-y-auto pr-1 scrollbar-mini font-mono"
                        >
                          {action.logs.length ? (
                            action.logs
                              .filter(log => {
                                const q = logSearchQueries[action.id]?.toLowerCase();
                                if (!q) return true;
                                return log.message.toLowerCase().includes(q) || log.level.toLowerCase().includes(q);
                              })
                              .map((log, idx) => (
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
                        {action.completedAt && action.status === "stopped" && `• Stopped: ${new Date(action.completedAt).toLocaleTimeString()}`}
                        {action.completedAt && action.status !== "stopped" && `• Completed: ${new Date(action.completedAt).toLocaleTimeString()}`}
                        {action.jobId && ` • JobId: ${action.jobId}`}
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
