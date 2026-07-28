"use client";

import { useState, useEffect, useCallback, Fragment } from "react";
import { useRouter } from "next/navigation";
import { motion, AnimatePresence } from "framer-motion";
import {
  List,
  Search,
  ChevronRight,
  ArrowLeft,
  Clock,
  CheckCircle,
  XCircle,
  Loader2,
  AlertTriangle,
  RefreshCw,
  Filter,
  Calendar,
  Server,
  UserCheck,
  ExternalLink,
  RotateCcw,
  Trash2,
  Play,
  Eye,
} from "lucide-react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:6001";

// ── Types ──────────────────────────────────────────────────────────────────────

interface ExecutionRecord {
  execution_id: string;
  task_title: string;
  task_type: string;
  role: string;
  status: "completed" | "failed" | "running" | "pending" | "cancelled";
  progress: number;
  started_at: string;
  completed_at?: string;
  duration_seconds?: number;
  triggered_by?: string;
  error_message?: string;
  result_summary?: string;
}

interface ExecutionListResponse {
  executions: ExecutionRecord[];
  total: number;
  page: number;
  page_size: number;
}

// ── Mock data for fallback ─────────────────────────────────────────────────────

const MOCK_EXECUTIONS: ExecutionRecord[] = [
  {
    execution_id: "exec-a1b2c3d4",
    task_title: "Create S3 bucket for application logs",
    task_type: "S3",
    role: "CloudEngineer",
    status: "completed",
    progress: 100,
    started_at: new Date(Date.now() - 86400000).toISOString(),
    completed_at: new Date(Date.now() - 86300000).toISOString(),
    duration_seconds: 84,
    triggered_by: "admin@chandra.io",
    result_summary: "Bucket created successfully with SSE-S3 encryption and lifecycle policy.",
  },
  {
    execution_id: "exec-e5f6g7h8",
    task_title: "Attach IAM policy for S3 read access",
    task_type: "IAM",
    role: "CloudEngineer",
    status: "completed",
    progress: 100,
    started_at: new Date(Date.now() - 172800000).toISOString(),
    completed_at: new Date(Date.now() - 172700000).toISOString(),
    duration_seconds: 45,
    triggered_by: "admin@chandra.io",
    result_summary: "IAM policy attached to role successfully.",
  },
  {
    execution_id: "exec-i9j0k1l2",
    task_title: "Provision EC2 instance for staging",
    task_type: "EC2",
    role: "CloudEngineer",
    status: "failed",
    progress: 45,
    started_at: new Date(Date.now() - 259200000).toISOString(),
    completed_at: new Date(Date.now() - 259100000).toISOString(),
    duration_seconds: 120,
    triggered_by: "system",
    error_message: "Insufficient capacity in the specified availability zone. Please try a different AZ or instance type.",
  },
  {
    execution_id: "exec-m3n4o5p6",
    task_title: "Configure VPC with private subnets",
    task_type: "VPC",
    role: "NetworkAdmin",
    status: "running",
    progress: 62,
    started_at: new Date(Date.now() - 600000).toISOString(),
    triggered_by: "admin@chandra.io",
  },
  {
    execution_id: "exec-q7r8s9t0",
    task_title: "Deploy Lambda function for log processing",
    task_type: "Lambda",
    role: "CloudEngineer",
    status: "completed",
    progress: 100,
    started_at: new Date(Date.now() - 345600000).toISOString(),
    completed_at: new Date(Date.now() - 345500000).toISOString(),
    duration_seconds: 67,
    triggered_by: "admin@chandra.io",
    result_summary: "Lambda deployed with 128MB memory, 30s timeout, and CloudWatch log group.",
  },
  {
    execution_id: "exec-u1v2w3x4",
    task_title: "Set up RDS PostgreSQL instance",
    task_type: "RDS",
    role: "DatabaseAdmin",
    status: "cancelled",
    progress: 12,
    started_at: new Date(Date.now() - 432000000).toISOString(),
    completed_at: new Date(Date.now() - 431900000).toISOString(),
    duration_seconds: 30,
    triggered_by: "system",
    error_message: "Cancelled due to missing VPC configuration.",
  },
  {
    execution_id: "exec-y5z6a7b8",
    task_title: "Create CloudWatch dashboard for monitoring",
    task_type: "CloudWatch",
    role: "CloudEngineer",
    status: "completed",
    progress: 100,
    started_at: new Date(Date.now() - 518400000).toISOString(),
    completed_at: new Date(Date.now() - 518300000).toISOString(),
    duration_seconds: 55,
    triggered_by: "admin@chandra.io",
    result_summary: "Dashboard created with 6 widgets including CPU, memory, and cost metrics.",
  },
  {
    execution_id: "exec-c9d0e1f2",
    task_title: "Configure SQS queue for async processing",
    task_type: "SQS",
    role: "CloudEngineer",
    status: "pending",
    progress: 0,
    started_at: new Date(Date.now() - 300000).toISOString(),
    triggered_by: "admin@chandra.io",
  },
];

// ── Helpers ────────────────────────────────────────────────────────────────────

const RESOURCE_ICONS: Record<string, string> = {
  S3: "🗄️", EC2: "🖥️", VPC: "🌐", Lambda: "⚡", CloudWatch: "📊",
  RDS: "🗃️", IAM: "🔑", DynamoDB: "📊", SQS: "📨", SNS: "🔔",
  ECS: "🐳", ELB: "⚖️", CloudFront: "🌍", ElastiCache: "⚡", APIGateway: "🔌",
};

const STATUS_STYLES: Record<string, { bg: string; text: string; icon: React.ReactNode }> = {
  completed: { bg: "bg-green-900/20", text: "text-green-400", icon: <CheckCircle className="w-4 h-4" /> },
  failed: { bg: "bg-red-900/20", text: "text-red-400", icon: <XCircle className="w-4 h-4" /> },
  running: { bg: "bg-blue-900/20", text: "text-blue-400", icon: <Loader2 className="w-4 h-4 animate-spin" /> },
  pending: { bg: "bg-gray-800", text: "text-gray-400", icon: <Clock className="w-4 h-4" /> },
  cancelled: { bg: "bg-amber-900/20", text: "text-amber-400", icon: <AlertTriangle className="w-4 h-4" /> },
};

function formatDate(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMs / 3600000);
  const diffDays = Math.floor(diffMs / 86400000);

  if (diffMins < 1) return "Just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

function formatDuration(seconds?: number): string {
  if (!seconds) return "—";
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return s > 0 ? `${m}m ${s}s` : `${m}m`;
}

// ── Component ──────────────────────────────────────────────────────────────────

export default function ExecutionHistoryPage() {
  const router = useRouter();
  const [executions, setExecutions] = useState<ExecutionRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [typeFilter, setTypeFilter] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [selectedExecution, setSelectedExecution] = useState<ExecutionRecord | null>(null);

  // ── Fetch executions ──────────────────────────────────────────────────────────
  const fetchExecutions = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const params = new URLSearchParams();
      if (search) params.set("search", search);
      if (statusFilter) params.set("status", statusFilter);
      if (typeFilter) params.set("type", typeFilter);

      const res = await fetch(`${API_BASE}/api/executions?${params}`);

      if (res.ok) {
        const data: ExecutionListResponse = await res.json();
        setExecutions(data.executions ?? []);
      } else {
        // Fallback: use mock data with filters applied
        applyFilters();
      }
    } catch {
      // Backend unreachable — use mock data
      applyFilters();
    } finally {
      setLoading(false);
    }
  }, [search, statusFilter, typeFilter]);

  const applyFilters = useCallback(() => {
    let filtered = [...MOCK_EXECUTIONS];

    if (search) {
      const q = search.toLowerCase();
      filtered = filtered.filter(
        (e) =>
          e.task_title.toLowerCase().includes(q) ||
          e.execution_id.toLowerCase().includes(q) ||
          e.role.toLowerCase().includes(q)
      );
    }

    if (statusFilter) {
      filtered = filtered.filter((e) => e.status === statusFilter);
    }

    if (typeFilter) {
      filtered = filtered.filter((e) => e.task_type === typeFilter);
    }

    setExecutions(filtered);
  }, [search, statusFilter, typeFilter]);

  useEffect(() => {
    fetchExecutions();
  }, [fetchExecutions]);

  // ── Stats ─────────────────────────────────────────────────────────────────────
  const stats = {
    total: MOCK_EXECUTIONS.length,
    completed: MOCK_EXECUTIONS.filter((e) => e.status === "completed").length,
    failed: MOCK_EXECUTIONS.filter((e) => e.status === "failed").length,
    running: MOCK_EXECUTIONS.filter((e) => e.status === "running").length,
  };

  // ── Resource types for filter ─────────────────────────────────────────────────
  const resourceTypes = [...new Set(MOCK_EXECUTIONS.map((e) => e.task_type))].sort();

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100 p-6">
      <div className="max-w-7xl mx-auto">
        {/* Header */}
        <div className="flex items-center justify-between mb-8">
          <div className="flex items-center gap-3">
            <button
              onClick={() => router.back()}
              className="p-2 text-gray-500 hover:text-white hover:bg-gray-800 rounded-lg transition-all"
            >
              <ArrowLeft className="w-5 h-5" />
            </button>
            <List className="w-7 h-7 text-blue-400" />
            <h1 className="text-2xl font-bold text-white">Execution History</h1>
          </div>
          <button
            onClick={() => router.push(`/execution-review`)}
            className="flex items-center gap-2 px-4 py-2.5 bg-blue-600 hover:bg-blue-700 rounded-lg text-white transition-all"
          >
            <Play className="w-4 h-4" />
            New Execution
          </button>
        </div>

        {/* Stats Cards */}
        <div className="grid grid-cols-4 gap-4 mb-6">
          <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
            <p className="text-2xl font-bold text-white">{stats.total}</p>
            <p className="text-xs text-gray-500 mt-1">Total Executions</p>
          </div>
          <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
            <p className="text-2xl font-bold text-green-400">{stats.completed}</p>
            <p className="text-xs text-gray-500 mt-1">Completed</p>
          </div>
          <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
            <p className="text-2xl font-bold text-red-400">{stats.failed}</p>
            <p className="text-xs text-gray-500 mt-1">Failed</p>
          </div>
          <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
            <p className="text-2xl font-bold text-blue-400">{stats.running}</p>
            <p className="text-xs text-gray-500 mt-1">In Progress</p>
          </div>
        </div>

        {/* Search & Filters */}
        <div className="flex gap-3 mb-6 flex-wrap">
          <div className="flex-1 relative min-w-[200px]">
            <Search className="absolute left-3 top-3 w-4 h-4 text-gray-500" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search executions..."
              className="w-full bg-gray-800 border border-gray-700 rounded-lg pl-10 pr-4 py-2.5 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-white focus:outline-none focus:ring-2 focus:ring-blue-500 min-w-[130px]"
          >
            <option value="">All Statuses</option>
            <option value="completed">Completed</option>
            <option value="failed">Failed</option>
            <option value="running">Running</option>
            <option value="pending">Pending</option>
            <option value="cancelled">Cancelled</option>
          </select>
          <select
            value={typeFilter}
            onChange={(e) => setTypeFilter(e.target.value)}
            className="bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-white focus:outline-none focus:ring-2 focus:ring-blue-500 min-w-[130px]"
          >
            <option value="">All Types</option>
            {resourceTypes.map((type) => (
              <option key={type} value={type}>{type}</option>
            ))}
          </select>
          <button
            onClick={fetchExecutions}
            disabled={loading}
            className="flex items-center gap-2 px-4 py-2.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 transition-all disabled:opacity-50"
          >
            <RefreshCw className={`w-4 h-4 ${loading ? "animate-spin" : ""}`} />
            Refresh
          </button>
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
                <p className="text-red-300 font-medium">Error</p>
                <p className="text-red-400/80 text-sm mt-1">{error}</p>
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        {/* Summary (when no backend) */}
        {executions.length > 0 && executions[0].execution_id.startsWith("exec-") && (
          <div className="bg-amber-900/20 border border-amber-800/30 rounded-xl px-4 py-3 mb-6 flex items-center gap-3">
            <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0" />
            <p className="text-sm text-amber-300/80">
              Using sample data &mdash; backend API at <code className="text-amber-200">{API_BASE}/api/executions</code> is unreachable. Showing demo records.
            </p>
          </div>
        )}

        {/* Loading State */}
        {loading ? (
          <div className="bg-gray-900 border border-gray-800 rounded-xl p-12">
            <div className="flex flex-col items-center gap-3">
              <Loader2 className="w-8 h-8 text-blue-400 animate-spin" />
              <p className="text-gray-500">Loading execution history...</p>
            </div>
          </div>
        ) : executions.length === 0 ? (
          /* Empty State */
          <div className="bg-gray-900 border border-gray-800 rounded-xl p-12 text-center">
            <List className="w-12 h-12 mx-auto mb-4 text-gray-600" />
            <h3 className="text-lg font-medium text-white mb-2">No executions found</h3>
            <p className="text-gray-500 mb-4">
              {search || statusFilter || typeFilter
                ? "No executions match your current filters. Try adjusting them."
                : "No executions have been run yet. Create one to get started."}
            </p>
            {search || statusFilter || typeFilter ? (
              <button
                onClick={() => { setSearch(""); setStatusFilter(""); setTypeFilter(""); }}
                className="px-4 py-2 bg-gray-800 hover:bg-gray-700 rounded-lg text-gray-300 transition-colors"
              >
                Clear Filters
              </button>
            ) : (
              <button
                onClick={() => router.push(`/execution-review`)}
                className="px-4 py-2 bg-blue-600 hover:bg-blue-700 rounded-lg text-white transition-colors"
              >
                Create Execution
              </button>
            )}
          </div>
        ) : (
          /* ── Execution Table ────────────────────────────────────────────────── */
          <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-gray-800">
                    <th className="text-left px-4 py-3 text-xs font-medium text-gray-500 uppercase tracking-wider">Task</th>
                    <th className="text-left px-4 py-3 text-xs font-medium text-gray-500 uppercase tracking-wider">Type</th>
                    <th className="text-left px-4 py-3 text-xs font-medium text-gray-500 uppercase tracking-wider">Role</th>
                    <th className="text-left px-4 py-3 text-xs font-medium text-gray-500 uppercase tracking-wider">Status</th>
                    <th className="text-left px-4 py-3 text-xs font-medium text-gray-500 uppercase tracking-wider">Progress</th>
                    <th className="text-left px-4 py-3 text-xs font-medium text-gray-500 uppercase tracking-wider">Duration</th>
                    <th className="text-left px-4 py-3 text-xs font-medium text-gray-500 uppercase tracking-wider">Started</th>
                    <th className="text-right px-4 py-3 text-xs font-medium text-gray-500 uppercase tracking-wider">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-800">
                  {executions.map((exec) => {
                    const style = STATUS_STYLES[exec.status] ?? STATUS_STYLES.pending;
                    return (
                      <motion.tr
                        key={exec.execution_id}
                        initial={{ opacity: 0 }}
                        animate={{ opacity: 1 }}
                        className="hover:bg-gray-800/50 transition-colors cursor-pointer"
                        onClick={() => router.push(`/deployment?execution_id=${exec.execution_id}`)}
                      >
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-3 min-w-0 max-w-[300px]">
                            <span className="text-lg shrink-0">
                              {RESOURCE_ICONS[exec.task_type] ?? "📋"}
                            </span>
                            <div className="truncate">
                              <p className="text-sm font-medium text-white truncate">{exec.task_title}</p>
                              <p className="text-xs text-gray-500 font-mono">{exec.execution_id.substring(0, 12)}...</p>
                            </div>
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          <span className="text-xs bg-gray-800 text-blue-400 px-2 py-0.5 rounded-full">
                            {exec.task_type}
                          </span>
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-1.5">
                            <UserCheck className="w-3.5 h-3.5 text-gray-500" />
                            <span className="text-sm text-gray-400">{exec.role}</span>
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          <div className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs ${style.bg} ${style.text}`}>
                            {style.icon}
                            <span className="capitalize">{exec.status}</span>
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-2">
                            <div className="w-20 bg-gray-800 rounded-full h-1.5 overflow-hidden">
                              <motion.div
                                className={`h-full rounded-full ${
                                  exec.status === "completed" ? "bg-green-500" :
                                  exec.status === "failed" || exec.status === "cancelled" ? "bg-red-500" :
                                  "bg-blue-500"
                                }`}
                                initial={{ width: "0%" }}
                                animate={{ width: `${exec.progress}%` }}
                                transition={{ duration: 0.5 }}
                              />
                            </div>
                            <span className="text-xs text-gray-500">{exec.progress}%</span>
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          <span className="text-sm text-gray-400">
                            {formatDuration(exec.duration_seconds)}
                          </span>
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-1.5">
                            <Calendar className="w-3.5 h-3.5 text-gray-500" />
                            <span className="text-sm text-gray-400">{formatDate(exec.started_at)}</span>
                          </div>
                        </td>
                        <td className="px-4 py-3 text-right">
                          <div className="flex items-center justify-end gap-1">
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                router.push(`/deployment?execution_id=${exec.execution_id}`);
                              }}
                              className="p-1.5 text-gray-500 hover:text-blue-400 hover:bg-gray-800 rounded-lg transition-all"
                              title="View Details"
                            >
                              <Eye className="w-4 h-4" />
                            </button>
                            {exec.status === "failed" && (
                              <button
                                onClick={(e) => {
                                  e.stopPropagation();
                                  router.push(`/execution-review`);
                                }}
                                className="p-1.5 text-gray-500 hover:text-green-400 hover:bg-gray-800 rounded-lg transition-all"
                                title="Retry"
                              >
                                <RotateCcw className="w-4 h-4" />
                              </button>
                            )}
                          </div>
                        </td>
                      </motion.tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Footer */}
        {executions.length > 0 && (
          <div className="flex items-center justify-between mt-4 text-xs text-gray-600">
            <span>
              Showing {executions.length} of {MOCK_EXECUTIONS.length} executions
              {(search || statusFilter || typeFilter) && " (filtered)"}
            </span>
            <span className="text-gray-700">
              Page auto-refreshes. Pull latest data with the Refresh button.
            </span>
          </div>
        )}
      </div>
    </div>
  );
}