"use client";

import { orchestrateAction, fetchBackendLogs, type BackendLog } from "@/services/api";
import { motion, AnimatePresence } from "framer-motion";
import { useEffect, useRef, useState, Fragment } from "react";
import { ChevronDown, Play, Pause, CheckCircle2, AlertTriangle, Clock, X } from "lucide-react";

type ExecutingAction = {
  id: string;
  actionName: string;
  actionDescription: string;
  service: string;
  kraCode: string;
  priorityLevel: string;
  steps: string[];
  jiraKey: string;
  status: "pending" | "running" | "completed" | "failed" | "awaiting_input" | "exhausted";
  threadId: string;
  startedAt: number;
  completedAt?: number;
  logs: BackendLog[];
  lastLogTimestamp?: number;
  errorMessage?: string;
};

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

function StatusBadge({ status }: { status: ExecutingAction["status"] }) {
  const tones: Record<string, string> = {
    pending: "border-amber/45 bg-amber/12 text-amber",
    running: "border-signal/45 bg-signal/12 text-signal animate-pulse",
    completed: "border-emerald-300/40 bg-emerald-300/12 text-emerald-300",
    failed: "border-signal/45 bg-signal/15 text-signal",
    awaiting_input: "border-blue-400/40 bg-blue-400/12 text-blue-300",
    exhausted: "border-orange-400/40 bg-orange-400/12 text-orange-300"
  };
  const icons: Record<string, JSX.Element> = {
    pending: <Clock size={12} />,
    running: <Play size={12} />,
    completed: <CheckCircle2 size={12} />,
    failed: <AlertTriangle size={12} />,
    awaiting_input: <Clock size={12} />,
    exhausted: <AlertTriangle size={12} />
  };

  const displayLabel = status === "awaiting_input" ? "AWAITING INPUT" : status === "exhausted" ? "EXHAUSTED" : status;

  return (
    <div className={cx("inline-flex items-center gap-1.5 border px-2 py-1 text-[0.6rem] uppercase tracking-[0.18em] rounded", tones[status] || "border-white/20 bg-white/10")}>
      {icons[status]}
      {displayLabel}
    </div>
  );
}

export function WorkerActionExecutionCenter({
  actions,
  onActionApproved
}: {
  actions: any[];
  onActionApproved?: (action: any) => void;
}) {
  const [executingActions, setExecutingActions] = useState<ExecutingAction[]>([]);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const logsEndRef = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const logPollInterval = useRef<number | null>(null);

  const scrollToBottom = () => {
    logsEndRef.current?.scrollIntoView({ behavior: "auto" });
  };

  useEffect(() => {
    scrollToBottom();
  }, [executingActions]);

  // Poll for logs periodically
  useEffect(() => {
    const pollLogs = async () => {
      if (executingActions.length === 0) return;

      try {
        const allLogs = await fetchBackendLogs(1000, 0);

        setExecutingActions((current) => {
          return current.map((action) => {
            if (action.status !== "running") {
              return action;
            }

            let actionLogs: BackendLog[] = [];
            const startTimeMs = action.startedAt;
            const startTimeSecs = Math.floor(startTimeMs / 1000);

            // First, try to match by threadId if available
            if (action.threadId && action.threadId.length > 0 && action.threadId !== "error") {
              actionLogs = allLogs.filter((log) => {
                const logText = `${log.message} ${log.logger}`.toLowerCase();
                return logText.includes(action.threadId.toLowerCase());
              });
            }

            // If no threadId match, use Jira key + orchestrator keywords
            if (actionLogs.length === 0) {
              actionLogs = allLogs.filter((log) => {
                const logText = `${log.message} ${log.logger}`.toLowerCase();
                const jiraKeyLower = action.jiraKey.toLowerCase();
                // Match orchestrator logs with jira key, or any logs after action started
                const hasJiraContext = logText.includes(jiraKeyLower) || logText.includes("orchestrat");
                const isAfterStart = log.timestamp >= startTimeSecs - 5;
                return hasJiraContext && isAfterStart;
              });
            }

            // Fallback: include any logs from action start time onwards
            if (actionLogs.length === 0) {
              actionLogs = allLogs.filter((log) => log.timestamp >= startTimeSecs - 2);
            }

            return {
              ...action,
              logs: actionLogs,
              lastLogTimestamp: actionLogs.length > 0 ? actionLogs[actionLogs.length - 1].timestamp : action.lastLogTimestamp
            };
          });
        });
      } catch (error) {
        console.error("Failed to poll logs:", error);
      }
    };

    if (executingActions.some((a) => a.status === "running")) {
      logPollInterval.current = window.setInterval(pollLogs, 1000);
    }

    return () => {
      if (logPollInterval.current) window.clearInterval(logPollInterval.current);
    };
  }, [executingActions]);

  const handleExecuteAction = async (action: any) => {
    const actionId = `action-${Date.now()}`;
    const executing: ExecutingAction = {
      id: actionId,
      actionName: action.actionName || action.incident || "Unnamed Action",
      actionDescription: action.actionDescription || action.note || "",
      service: action.service || "AWS",
      kraCode: action.kraCode || "",
      priorityLevel: action.severity || "P3",
      steps: action.steps || [],
      jiraKey: action.jiraUrl?.split("/").pop() || "DEV-000",
      status: "running",
      threadId: "",
      startedAt: Date.now(),
      logs: [],
      errorMessage: ""
    };

    setExecutingActions((current) => [executing, ...current]);
    setExpandedId(actionId);

    try {
      const response = await orchestrateAction({
        action: {
          actionName: executing.actionName,
          actionDescription: executing.actionDescription,
          steps: executing.steps
        },
        jira_issue_key: executing.jiraKey,
        command_timeout: 300,
        max_iterations: 5
      });

      const isSuccess = response.statusCode === 200;
      const isAwaitingClarification = response.statusCode === 202;
      const isExhausted = response.statusCode === 207;

      let finalStatus: "completed" | "awaiting_input" | "exhausted" | "failed" = "failed";
      if (isSuccess) finalStatus = "completed";
      else if (isAwaitingClarification) finalStatus = "awaiting_input";
      else if (isExhausted) finalStatus = "exhausted";

      const errorMsg = !isSuccess ? (response.exception || "Execution failed") : "";

      setExecutingActions((current) =>
        current.map((a) =>
          a.id === actionId
            ? {
                ...a,
                status: finalStatus,
                threadId: response.thread_id || "",
                completedAt: Date.now(),
                errorMessage: errorMsg
              }
            : a
        )
      );

      if (isSuccess && onActionApproved) {
        onActionApproved(action);
      }
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : String(error);
      console.error("Action execution failed:", errorMessage);
      setExecutingActions((current) =>
        current.map((a) =>
          a.id === actionId
            ? {
                ...a,
                status: "failed",
                completedAt: Date.now(),
                errorMessage: errorMessage
              }
            : a
        )
      );
    }
  };

  const removeAction = (id: string) => {
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

  return (
    <div className="glass overflow-hidden p-4 rounded-2xl border border-white/10">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <div className="text-[0.65rem] uppercase tracking-[0.22em] text-muted">WORKER ACTION EXECUTION CENTER</div>
          <div className="mt-1 text-sm text-frost/70">Auto-approved actions executing with live logs</div>
        </div>
        <div className="rounded-full bg-signal/20 px-3 py-1 text-[0.7rem] font-semibold uppercase tracking-[0.08em] text-signal">
          {executingActions.length} EXECUTING
        </div>
      </div>

      <div className="space-y-2">
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
                          <StatusBadge status={action.status} />
                          <span className="text-[0.55rem] uppercase tracking-[0.16em] text-muted">{action.priorityLevel}</span>
                        </div>
                        <div className="mt-2 text-sm font-semibold text-frost">{action.actionName}</div>
                        <div className="mt-1 text-[0.65rem] text-muted">
                          {action.service} • {action.kraCode || "No KRA"}
                        </div>
                        <div className="mt-2 text-[0.68rem] text-frost/75">{action.actionDescription}</div>
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

                      <div>
                        <div className="text-[0.6rem] uppercase tracking-[0.18em] text-muted mb-2">LIVE EXECUTION LOGS</div>
                        <div ref={containerRef} className="max-h-[250px] space-y-1 overflow-y-auto pr-1 scrollbar-mini">
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
                                <div className="flex items-center gap-2">
                                  <span className="text-muted">[{new Date(log.timestamp * 1000).toLocaleTimeString()}]</span>
                                  <span className={`font-semibold text-[0.6rem] ${levelColor[log.level] || "text-frost"}`}>
                                    {log.level}
                                  </span>
                                  <span className="flex-1 truncate text-frost/75">{log.message}</span>
                                </div>
                              </div>
                            ))
                          ) : (
                            <div className="text-[0.65rem] text-muted text-center py-2">{action.status === "running" ? "Waiting for logs..." : "No logs captured"}</div>
                          )}
                          <div ref={logsEndRef} />
                        </div>
                      </div>

                      <div className="mt-3 pt-3 border-t border-white/8 text-[0.6rem] text-muted">
                        Started: {new Date(action.startedAt).toLocaleTimeString()} {action.completedAt && `• Completed: ${new Date(action.completedAt).toLocaleTimeString()}`}
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
  );
}

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
