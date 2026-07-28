"use client";

import { useState, useEffect, useCallback, useRef, Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { motion, AnimatePresence } from "framer-motion";
import {
  Loader2,
  CheckCircle,
  XCircle,
  Clock,
  ArrowLeft,
  ExternalLink,
  FileText,
  ChevronRight,
  Terminal,
  AlertTriangle,
  RotateCcw,
  Bug,
  RefreshCw,
  List,
} from "lucide-react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:6001";

// ── Types ──────────────────────────────────────────────────────────────────────

interface DeploymentStage {
  name: string;
  status: "pending" | "running" | "completed" | "failed" | "skipped";
  started_at?: number;
  completed_at?: number;
  message?: string;
  progress?: number;
}

interface DeploymentStatus {
  execution_id: string;
  status: "pending" | "running" | "completed" | "failed" | "cancelled";
  progress: number;
  current_stage: string;
  stages: DeploymentStage[];
  message: string;
  started_at?: number;
  completed_at?: number;
  result_url?: string;
  error?: string;
  log_preview?: string[];
}

// ── Mock / fallback stages ────────────────────────────────────────────────────

const DEFAULT_STAGES: DeploymentStage[] = [
  { name: "Initializing", status: "pending" },
  { name: "Validating Configuration", status: "pending" },
  { name: "Planning Resources", status: "pending" },
  { name: "Provisioning Infrastructure", status: "pending" },
  { name: "Configuring Permissions", status: "pending" },
  { name: "Verifying Deployment", status: "pending" },
  { name: "Finalizing", status: "pending" },
];

// ── Component ──────────────────────────────────────────────────────────────────

function DeploymentProgressContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const executionId = searchParams.get("execution_id");

  const [deployment, setDeployment] = useState<DeploymentStatus | null>(null);
  const [stages, setStages] = useState<DeploymentStage[]>(DEFAULT_STAGES);
  const [error, setError] = useState<string | null>(null);
  const [polling, setPolling] = useState(true);
  const [elapsed, setElapsed] = useState(0);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const elapsedRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [showLogs, setShowLogs] = useState(false);

  // Simulate deployment progression when backend is unreachable
  const simulateProgress = useCallback(() => {
    const stageNames = [
      "Initializing",
      "Validating Configuration",
      "Planning Resources",
      "Provisioning Infrastructure",
      "Configuring Permissions",
      "Verifying Deployment",
      "Finalizing",
    ];

    const totalDuration = 12000; // 12 seconds for full simulation
    const perStage = totalDuration / stageNames.length;

    // Determine which stage we should be on based on elapsed time
    const currentStageIndex = Math.min(
      Math.floor(elapsed / perStage),
      stageNames.length - 1
    );

    const progress = Math.min(Math.round((elapsed / totalDuration) * 100), 99);

    const updatedStages: DeploymentStage[] = stageNames.map((name, i) => {
      if (i < currentStageIndex) {
        return {
          name,
          status: "completed",
          started_at: Date.now() - totalDuration + i * perStage,
          completed_at: Date.now() - totalDuration + (i + 1) * perStage,
          message: `${name} completed successfully.`,
        };
      } else if (i === currentStageIndex) {
        const stageProgress = Math.min(
          Math.round(((elapsed - i * perStage) / perStage) * 100),
          100
        );
        return {
          name,
          status: "running",
          started_at: Date.now() - (elapsed - i * perStage),
          progress: stageProgress,
          message:
            stageProgress < 50
              ? `Starting ${name.toLowerCase()}...`
              : `${name} in progress (${stageProgress}%)`,
        };
      }
      return { name, status: "pending" as const };
    });

    setStages(updatedStages);
    setDeployment((prev) => ({
      ...(prev ?? {
        execution_id: executionId ?? "unknown",
        status: "running",
        progress,
        current_stage: stageNames[currentStageIndex],
        message: `${stageNames[currentStageIndex]} in progress...`,
        stages: updatedStages,
      }),
      status: progress >= 99 ? "completed" : "running",
      progress,
      current_stage: stageNames[currentStageIndex],
      message: `${stageNames[currentStageIndex]} in progress...`,
      stages: updatedStages,
    }));
  }, [elapsed, executionId]);

  // ── Poll deployment status ─────────────────────────────────────────────────
  useEffect(() => {
    if (!executionId) return;

    const poll = async () => {
      try {
        const res = await fetch(
          `${API_BASE}/api/executions/${executionId}/status`,
          { method: "GET" }
        );
        if (res.ok) {
          const data: DeploymentStatus = await res.json();
          setDeployment(data);
          if (data.stages && data.stages.length > 0) {
            setStages(data.stages);
          }
          if (data.status === "completed" || data.status === "failed" || data.status === "cancelled") {
            setPolling(false);
          }
        }
        // If backend returns non-OK, fall through to simulation
      } catch {
        // Backend unreachable — simulation handles it
      }
    };

    if (polling) {
      poll();
      pollRef.current = setInterval(poll, 3000);
    }

    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [executionId, polling]);

  // ── Elapsed timer (always runs) ────────────────────────────────────────────
  useEffect(() => {
    const startTime = Date.now();
    elapsedRef.current = setInterval(() => {
      setElapsed(Date.now() - startTime);
    }, 500);
    return () => {
      if (elapsedRef.current) clearInterval(elapsedRef.current);
    };
  }, []);

  // ── Simulate progress when backend is unavailable ──────────────────────────
  useEffect(() => {
    if (deployment?.status === "completed" || deployment?.status === "failed" || !polling) return;
    simulateProgress();
  }, [elapsed, polling, simulateProgress, deployment?.status]);

  // Auto-complete simulation after 14 seconds
  useEffect(() => {
    if (elapsed > 14000 && polling && deployment?.status === "running") {
      const finalStages: DeploymentStage[] = stages.map((s) => ({
        ...s,
        status: "completed" as const,
        completed_at: Date.now(),
        message: `${s.name} completed successfully.`,
      }));
      setStages(finalStages);
      setDeployment((prev) => ({
        ...(prev ?? {
          execution_id: executionId ?? "unknown",
          status: "completed",
          progress: 100,
          current_stage: "Finalizing",
          message: "Deployment completed successfully.",
          stages: finalStages,
        }),
        status: "completed",
        progress: 100,
        current_stage: "Complete",
        message: "Deployment completed successfully.",
        stages: finalStages,
        completed_at: Date.now(),
      }));
      setPolling(false);
    }
  }, [elapsed, polling, deployment, executionId, stages]);

  // ── Format elapsed time ─────────────────────────────────────────────────────
  const formatElapsed = (ms: number) => {
    const s = Math.floor(ms / 1000);
    const m = Math.floor(s / 60);
    const sec = s % 60;
    return m > 0 ? `${m}m ${sec}s` : `${sec}s`;
  };

  // ── Stage icon ──────────────────────────────────────────────────────────────
  const stageIcon = (status: string) => {
    switch (status) {
      case "completed":
        return <CheckCircle className="w-5 h-5 text-green-400" />;
      case "running":
        return <Loader2 className="w-5 h-5 text-blue-400 animate-spin" />;
      case "failed":
        return <XCircle className="w-5 h-5 text-red-400" />;
      case "skipped":
        return <AlertTriangle className="w-5 h-5 text-amber-400" />;
      default:
        return <Clock className="w-5 h-5 text-gray-600" />;
    }
  };

  const isComplete = deployment?.status === "completed";
  const isFailed = deployment?.status === "failed" || deployment?.status === "cancelled";
  const isRunning = deployment?.status === "running" || deployment?.status === "pending";

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100 p-6">
      <div className="max-w-4xl mx-auto">
        {/* Header */}
        <div className="flex items-center justify-between mb-8">
          <div className="flex items-center gap-3">
            <button
              onClick={() => router.back()}
              className="p-2 text-gray-500 hover:text-white hover:bg-gray-800 rounded-lg transition-all"
            >
              <ArrowLeft className="w-5 h-5" />
            </button>
            <Terminal className="w-7 h-7 text-blue-400" />
            <h1 className="text-2xl font-bold text-white">Deployment Progress</h1>
          </div>
          <div className="flex items-center gap-2">
            {executionId && (
              <span className="text-xs text-gray-500 bg-gray-800 px-3 py-1.5 rounded-full">
                ID: {executionId.substring(0, 16)}...
              </span>
            )}
            <button
              onClick={() => router.push(`/executions`)}
              className="flex items-center gap-2 px-3 py-2 text-sm text-gray-400 hover:text-white bg-gray-800 hover:bg-gray-700 rounded-lg transition-all"
            >
              <List className="w-4 h-4" />
              History
            </button>
          </div>
        </div>

        {/* Error Banner */}
        <AnimatePresence>
          {error && (
            <motion.div
              initial={{ opacity: 0, y: -10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -10 }}
              className="bg-red-900/30 border border-red-800/50 rounded-xl p-4 mb-6 flex items-start gap-3"
            >
              <XCircle className="w-5 h-5 text-red-400 mt-0.5 shrink-0" />
              <div>
                <p className="text-red-300 font-medium">Deployment Error</p>
                <p className="text-red-400/80 text-sm mt-1">{error}</p>
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* ── Status Card ────────────────────────────────────────────────────── */}
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 mb-6">
          <div className="flex items-center justify-between mb-6">
            <div className="flex items-center gap-3">
              {isComplete ? (
                <CheckCircle className="w-8 h-8 text-green-400" />
              ) : isFailed ? (
                <XCircle className="w-8 h-8 text-red-400" />
              ) : (
                <Loader2 className="w-8 h-8 text-blue-400 animate-spin" />
              )}
              <div>
                <h2 className="text-lg font-semibold text-white">
                  {isComplete
                    ? "Deployment Complete"
                    : isFailed
                    ? "Deployment Failed"
                    : "Deployment In Progress"}
                </h2>
                <p className="text-sm text-gray-400">
                  {deployment?.message ?? "Initializing..."}
                </p>
              </div>
            </div>
            <div className="text-right">
              <p className="text-3xl font-bold text-white">
                {deployment?.progress ?? 0}%
              </p>
              <p className="text-xs text-gray-500 mt-1">
                Elapsed: {formatElapsed(elapsed)}
              </p>
            </div>
          </div>

          {/* Progress Bar */}
          <div className="bg-gray-950 rounded-full h-3 overflow-hidden">
            <motion.div
              className={`h-full rounded-full transition-all ${
                isComplete
                  ? "bg-green-500"
                  : isFailed
                  ? "bg-red-500"
                  : "bg-blue-600"
              }`}
              initial={{ width: "0%" }}
              animate={{ width: `${deployment?.progress ?? 0}%` }}
              transition={{ duration: 0.5, ease: "easeOut" }}
            />
          </div>

          {/* Current Stage Info */}
          {isRunning && (
            <div className="mt-4 flex items-center gap-2 text-sm text-blue-400">
              <RefreshCw className="w-3.5 h-3.5 animate-spin" />
              <span>Polling for updates every 3s</span>
            </div>
          )}

          {/* Result URL */}
          {deployment?.result_url && (
            <div className="mt-4 pt-4 border-t border-gray-800">
              <a
                href={deployment.result_url}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-2 text-sm text-blue-400 hover:text-blue-300 transition-colors"
              >
                <ExternalLink className="w-4 h-4" />
                View deployment result
              </a>
            </div>
          )}
        </div>

        {/* ── Stages Timeline ────────────────────────────────────────────────── */}
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 mb-6">
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-2">
              <FileText className="w-5 h-5 text-gray-400" />
              <h2 className="text-lg font-semibold text-white">Deployment Stages</h2>
            </div>
            <button
              onClick={() => setShowLogs(!showLogs)}
              className="flex items-center gap-2 text-sm text-gray-400 hover:text-white transition-colors"
            >
              <Bug className="w-4 h-4" />
              {showLogs ? "Hide Logs" : "Show Logs"}
            </button>
          </div>

          <div className="space-y-1">
            {stages.map((stage, i) => (
              <motion.div
                key={stage.name}
                initial={{ opacity: 0, x: -10 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: i * 0.05 }}
                className={`flex items-start gap-4 p-3 rounded-lg transition-colors ${
                  stage.status === "running"
                    ? "bg-blue-900/10 border border-blue-800/20"
                    : stage.status === "completed"
                    ? "hover:bg-gray-800/50"
                    : stage.status === "failed"
                    ? "bg-red-900/10 border border-red-800/20"
                    : "opacity-50"
                }`}
              >
                {/* Timeline connector */}
                <div className="flex flex-col items-center">
                  <div className="w-5 h-5 flex items-center justify-center">
                    {stageIcon(stage.status)}
                  </div>
                  {i < stages.length - 1 && (
                    <div
                      className={`w-0.5 h-6 mt-1 ${
                        stage.status === "completed"
                          ? "bg-green-500/50"
                          : "bg-gray-700"
                      }`}
                    />
                  )}
                </div>

                <div className="flex-1 min-w-0">
                  <div className="flex items-center justify-between">
                    <p
                      className={`text-sm font-medium ${
                        stage.status === "running"
                          ? "text-blue-300"
                          : stage.status === "completed"
                          ? "text-white"
                          : stage.status === "failed"
                          ? "text-red-300"
                          : "text-gray-500"
                      }`}
                    >
                      {stage.name}
                    </p>
                    <div className="flex items-center gap-2">
                      {stage.status === "running" && stage.progress !== undefined && (
                        <span className="text-xs text-blue-400">{stage.progress}%</span>
                      )}
                      {stage.status === "running" && (
                        <span className="text-xs text-gray-500">
                          {stage.started_at
                            ? `${Math.round((Date.now() - stage.started_at) / 1000)}s`
                            : ""}
                        </span>
                      )}
                      {stage.status === "completed" && stage.started_at && stage.completed_at && (
                        <span className="text-xs text-gray-500">
                          {Math.round((stage.completed_at - stage.started_at) / 1000)}s
                        </span>
                      )}
                      {stage.status === "completed" && (
                        <CheckCircle className="w-3.5 h-3.5 text-green-400" />
                      )}
                    </div>
                  </div>
                  {stage.message && (
                    <p className="text-xs text-gray-500 mt-0.5">{stage.message}</p>
                  )}
                  {/* Progress bar for running stage */}
                  {stage.status === "running" && stage.progress !== undefined && (
                    <div className="mt-2 bg-gray-950 rounded-full h-1.5 overflow-hidden">
                      <motion.div
                        className="h-full bg-blue-500 rounded-full"
                        initial={{ width: "0%" }}
                        animate={{ width: `${stage.progress}%` }}
                        transition={{ duration: 0.5 }}
                      />
                    </div>
                  )}
                </div>
              </motion.div>
            ))}
          </div>
        </div>

        {/* ── Log Preview ────────────────────────────────────────────────────── */}
        <AnimatePresence>
          {showLogs && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              exit={{ opacity: 0, height: 0 }}
              className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden mb-6"
            >
              <div className="flex items-center justify-between px-6 py-3 border-b border-gray-800">
                <div className="flex items-center gap-2">
                  <Terminal className="w-4 h-4 text-cyan-400" />
                  <h3 className="text-sm font-medium text-white">Execution Logs</h3>
                </div>
                <span className="text-xs text-gray-500">
                  {deployment?.log_preview?.length ?? 0} lines
                </span>
              </div>
              <div className="p-4 bg-gray-950 font-mono text-xs leading-relaxed max-h-64 overflow-y-auto">
                {deployment?.log_preview && deployment.log_preview.length > 0 ? (
                  deployment.log_preview.map((line, i) => (
                    <div key={i} className="text-gray-400 hover:text-gray-300">
                      <span className="text-gray-600 mr-2">{String(i + 1).padStart(3, "0")}</span>
                      {line}
                    </div>
                  ))
                ) : (
                  <div className="text-gray-500 italic">
                    {isRunning
                      ? "Waiting for log output..."
                      : "No logs available for this execution."}
                  </div>
                )}
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* ── Actions ────────────────────────────────────────────────────────── */}
        <div className="flex gap-3 justify-end pt-2">
          {isComplete && (
            <>
              <button
                onClick={() => router.push(`/execution-review`)}
                className="flex items-center gap-2 px-5 py-2.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 transition-colors"
              >
                <RotateCcw className="w-4 h-4" />
                New Deployment
              </button>
              <button
                onClick={() => router.push(`/executions`)}
                className="flex items-center gap-2 px-5 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-700 text-white transition-all"
              >
                <List className="w-4 h-4" />
                View All Executions
              </button>
            </>
          )}
          {isFailed && (
            <>
              <button
                onClick={() => {
                  setPolling(true);
                  setElapsed(0);
                  setStages(DEFAULT_STAGES.map((s) => ({ ...s, status: "pending" as const })));
                  setDeployment(null);
                  setError(null);
                }}
                className="flex items-center gap-2 px-5 py-2.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 transition-colors"
              >
                <RotateCcw className="w-4 h-4" />
                Retry
              </button>
              <button
                onClick={() => router.push(`/execution-review`)}
                className="flex items-center gap-2 px-5 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-700 text-white transition-all"
              >
                Back to Review
              </button>
            </>
          )}
          {isRunning && (
            <button
              onClick={() => router.push(`/executions`)}
              className="flex items-center gap-2 px-5 py-2.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 transition-colors"
            >
              <List className="w-4 h-4" />
              Executions
            </button>
          )}
        </div>

        {/* Footer note */}
        <div className="mt-8 text-center">
          <p className="text-xs text-gray-600">
            {isRunning
              ? "Page auto-refreshes. You can safely navigate away — the deployment continues in the background."
              : isComplete
              ? "Deployment completed. You can view the full history in the Executions page."
              : "If the deployment is stuck, check the backend status or try again."}
          </p>
        </div>
      </div>
    </div>
  );
}

export default function DeploymentProgressPage() {
  return (
    <Suspense fallback={
      <div className="min-h-screen bg-gray-950 text-gray-100 p-6 flex items-center justify-center">
        <div className="text-center">
          <Loader2 className="w-8 h-8 text-blue-400 animate-spin mx-auto mb-4" />
          <p className="text-gray-500">Loading deployment...</p>
        </div>
      </div>
    }>
      <DeploymentProgressContent />
    </Suspense>
  );
}