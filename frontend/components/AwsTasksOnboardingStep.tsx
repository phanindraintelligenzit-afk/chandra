"use client";

import { useState, useEffect, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  Plus,
  Search,
  Edit3,
  Trash2,
  Play,
  X,
  Check,
  AlertTriangle,
  ListChecks,
  Server
} from "lucide-react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:6001";

interface AwsTask {
  id: string;
  title: string;
  description: string;
  resource_type: string;
  operation: string;
  resource_config: string;
  created_by: string;
  created_at: string;
  updated_at: string;
}

const RESOURCE_ICONS: Record<string, string> = {
  S3: "\uD83D\uDCC4",
  EC2: "\uD83D\uDDA5\uFE0F",
  VPC: "\uD83C\uDF10",
  Lambda: "\u26A1",
  CloudWatch: "\uD83D\uDCCA",
  RDS: "\uD83D\uDDC3\uFE0F",
  IAM: "\uD83D\uDD11",
  DynamoDB: "\uD83D\uDCCA",
  SQS: "\uD83D\uDCE8",
  SNS: "\uD83D\uDD14",
  ECS: "\uD83D\uDC33",
  ELB: "\u2696\uFE0F",
  CloudFront: "\uD83C\uDF0D",
  ElastiCache: "\u26A1",
  APIGateway: "\uD83D\uDD0C",
};

const RESOURCE_TYPE_OPTIONS = [
  "S3",
  "EC2",
  "VPC",
  "Lambda",
  "CloudWatch",
  "RDS",
  "IAM",
  "DynamoDB",
  "SQS",
  "SNS",
  "ECS",
  "ELB",
  "CloudFront",
  "ElastiCache",
  "APIGateway",
];

const FILTER_OPTIONS = [
  { value: "", label: "All Types" },
  { value: "S3", label: "S3 Bucket" },
  { value: "EC2", label: "EC2 Instance" },
  { value: "VPC", label: "VPC Network" },
  { value: "Lambda", label: "Lambda Function" },
  { value: "RDS", label: "RDS Database" },
  { value: "IAM", label: "IAM Role/Policy" },
];

interface AwsTasksOnboardingStepProps {
  onNext: () => void;
  onBack: () => void;
  agentName?: string;
}

export function AwsTasksOnboardingStep({
  onNext,
  onBack,
  agentName,
}: AwsTasksOnboardingStepProps) {
  const [tasks, setTasks] = useState<AwsTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [filterType, setFilterType] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [editingTask, setEditingTask] = useState<AwsTask | null>(null);
  const [form, setForm] = useState({
    title: "",
    description: "",
    resource_type: "S3",
    operation: "",
    resource_config: "",
  });
  const [currentUser] = useState("nagendra");

  const fetchTasks = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (search) params.set("search", search);
      if (filterType) params.set("resource_type", filterType);
      const res = await fetch(`${API_BASE}/aws-tasks?${params}`);
      if (res.ok) setTasks(await res.json());
    } catch (e) {
      console.error("Failed to fetch tasks:", e);
    } finally {
      setLoading(false);
    }
  }, [search, filterType]);

  useEffect(() => {
    fetchTasks();
  }, [fetchTasks]);

  const resetForm = () => {
    setForm({
      title: "",
      description: "",
      resource_type: "S3",
      operation: "",
      resource_config: "",
    });
  };

  const createTask = async () => {
    try {
      const res = await fetch(`${API_BASE}/aws-tasks`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...form,
          created_by: currentUser,
        }),
      });
      if (res.ok) {
        setShowCreate(false);
        resetForm();
        fetchTasks();
      }
    } catch (e) {
      console.error("Failed to create task:", e);
    }
  };

  const updateTask = async () => {
    if (!editingTask) return;
    try {
      const res = await fetch(`${API_BASE}/aws-tasks/${editingTask.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });
      if (res.ok) {
        setEditingTask(null);
        resetForm();
        fetchTasks();
      }
    } catch (e) {
      console.error("Failed to update task:", e);
    }
  };

  const deleteTask = async (id: string) => {
    if (!confirm("Delete this task?")) return;
    try {
      const res = await fetch(`${API_BASE}/aws-tasks/${id}`, {
        method: "DELETE",
      });
      if (res.ok) fetchTasks();
    } catch (e) {
      console.error("Failed to delete task:", e);
    }
  };

  const openEdit = (task: AwsTask) => {
    setEditingTask(task);
    setForm({
      title: task.title,
      description: task.description,
      resource_type: task.resource_type,
      operation: task.operation || "",
      resource_config: task.resource_config || "",
    });
  };

  const openCreate = () => {
    setShowCreate(true);
    setEditingTask(null);
    resetForm();
  };

  const closeModal = () => {
    setShowCreate(false);
    setEditingTask(null);
    resetForm();
  };

  return (
    <motion.div
      key="aws-tasks"
      initial={{ opacity: 0, x: 30 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: -20 }}
    >
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <ListChecks size={24} className="text-emerald-300" />
          <h3 className="text-2xl font-semibold uppercase tracking-[0.02em]">
            AWS Tasks
          </h3>
        </div>
        <button
          onClick={openCreate}
          className="flex items-center gap-2 rounded-2xl border border-emerald-300/30 bg-emerald-300/10 px-4 py-2.5 text-sm font-semibold uppercase tracking-[0.12em] text-emerald-200 transition hover:border-emerald-300/60 hover:bg-emerald-300/15"
        >
          <Plus className="w-4 h-4" />
          Create Task
        </button>
      </div>
      <p className="text-muted text-sm">
        Manage AWS operational tasks for{" "}
        {(agentName || "this agent").toUpperCase()}.
      </p>

      {/* Search & Filter */}
      <div className="flex gap-3 mt-5 mb-4">
        <div className="flex-1 relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search tasks..."
            className="w-full rounded-2xl border border-white/10 bg-black/30 py-2.5 pl-10 pr-4 text-sm text-frost outline-none transition placeholder:text-muted focus:border-emerald-300/40 focus:ring-2 focus:ring-emerald-300/15"
          />
        </div>
        <select
          value={filterType}
          onChange={(e) => setFilterType(e.target.value)}
          className="rounded-2xl border border-white/10 bg-black/30 px-4 py-2.5 text-sm text-frost outline-none transition focus:border-emerald-300/40 focus:ring-2 focus:ring-emerald-300/15"
        >
          {FILTER_OPTIONS.map((opt) => (
            <option key={opt.value || "all"} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      </div>

      {/* Task List */}
      <div className="space-y-3 max-h-[360px] overflow-y-auto pr-2 scrollbar-mini">
        {loading ? (
          <div className="flex items-center justify-center py-12">
            <div className="h-8 w-8 rounded-full border-2 border-emerald-300/30 border-t-emerald-300 animate-spin" />
          </div>
        ) : tasks.length === 0 ? (
          <div className="rounded-2xl border border-white/10 bg-black/30 p-8 text-center">
            <AlertTriangle className="w-10 h-10 mx-auto mb-3 text-muted" />
            <div className="text-[0.7rem] uppercase tracking-[0.16em] text-muted">
              No AWS tasks found
            </div>
            <p className="mt-2 text-sm text-frost/60">
              Create a task to get started with agent operations.
            </p>
          </div>
        ) : (
          tasks.map((task) => (
            <motion.div
              key={task.id}
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              className="rounded-2xl border border-white/10 bg-black/30 p-4 hover:border-white/20 transition-all"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="flex items-start gap-3 min-w-0 flex-1">
                  <span className="text-xl shrink-0 mt-0.5">
                    {RESOURCE_ICONS[task.resource_type] || "\uD83D\uDCCB"}
                  </span>
                  <div className="min-w-0 flex-1">
                    <h4 className="font-semibold uppercase tracking-[0.04em] text-frost truncate">
                      {task.title}
                    </h4>
                    <p className="text-sm text-frost/70 mt-1 line-clamp-2">
                      {task.description}
                    </p>
                    <div className="flex items-center gap-3 mt-2">
                      <span className="rounded-full border border-white/10 bg-white/5 px-2.5 py-0.5 text-[0.6rem] uppercase tracking-[0.16em] text-amber">
                        {task.resource_type}
                      </span>
                      <span className="text-[0.6rem] text-muted">
                        by {task.created_by}
                      </span>
                      <span className="text-[0.6rem] text-muted">
                        {new Date(task.updated_at).toLocaleDateString()}
                      </span>
                    </div>
                  </div>
                </div>
                <div className="flex items-center gap-1 shrink-0">
                  <button
                    onClick={() => openEdit(task)}
                    className="p-2 text-muted hover:text-emerald-300 hover:bg-white/5 rounded-xl transition-all"
                    title="Edit"
                  >
                    <Edit3 className="w-4 h-4" />
                  </button>
                  <button
                    onClick={() => deleteTask(task.id)}
                    className="p-2 text-muted hover:text-red-400 hover:bg-white/5 rounded-xl transition-all"
                    title="Delete"
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                  <button
                    className="p-2 text-muted hover:text-blue-400 hover:bg-white/5 rounded-xl transition-all"
                    title="Execute"
                  >
                    <Play className="w-4 h-4" />
                  </button>
                </div>
              </div>
            </motion.div>
          ))
        )}
      </div>

      {/* Bottom Navigation */}
      <div className="mt-6 flex items-center gap-3">
        <button
          onClick={onBack}
          className="rounded-2xl border border-white/10 px-4 py-3 text-sm uppercase tracking-[0.14em] text-muted hover:text-frost transition-colors"
        >
          BACK
        </button>
        <button
          onClick={onNext}
          className="ml-auto rounded-2xl bg-emerald-300/10 px-5 py-3 text-sm font-semibold uppercase tracking-[0.14em] text-emerald-200 hover:bg-emerald-300/15 transition-colors"
        >
          CONTINUE TO PERMISSIONS
        </button>
      </div>

      {/* Create/Edit Modal */}
      <AnimatePresence>
        {(showCreate || editingTask) && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
            <motion.div
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.95 }}
              className="bg-gray-900 border border-gray-800 rounded-2xl p-6 w-full max-w-lg mx-4 shadow-2xl"
            >
              {/* Modal Header */}
              <div className="flex items-center justify-between mb-6">
                <div className="flex items-center gap-3">
                  <Server className="w-5 h-5 text-blue-400" />
                  <h2 className="text-xl font-semibold text-white">
                    {editingTask ? "Edit Task" : "Create New AWS Task"}
                  </h2>
                </div>
                <button
                  onClick={closeModal}
                  className="text-gray-500 hover:text-white transition-colors rounded-lg p-1"
                >
                  <X className="w-5 h-5" />
                </button>
              </div>

              {/* Modal Form */}
              <div className="space-y-4">
                <div>
                  <label className="block text-sm font-medium text-gray-400 mb-1">
                    Title
                  </label>
                  <input
                    value={form.title}
                    onChange={(e) =>
                      setForm({ ...form, title: e.target.value })
                    }
                    placeholder="e.g. Create S3 bucket for logs"
                    className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-400 mb-1">
                    Description
                  </label>
                  <textarea
                    value={form.description}
                    onChange={(e) =>
                      setForm({ ...form, description: e.target.value })
                    }
                    placeholder="Describe what this task does..."
                    rows={3}
                    className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-400 mb-1">
                    Resource Type
                  </label>
                  <select
                    value={form.resource_type}
                    onChange={(e) =>
                      setForm({ ...form, resource_type: e.target.value })
                    }
                    className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
                  >
                    {RESOURCE_TYPE_OPTIONS.map((t) => (
                      <option key={t} value={t}>
                        {t}
                      </option>
                    ))}
                  </select>
                </div>
              </div>

              {/* Modal Actions */}
              <div className="flex gap-3 justify-end mt-6 pt-4 border-t border-gray-800">
                <button
                  onClick={closeModal}
                  className="px-5 py-2.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={editingTask ? updateTask : createTask}
                  disabled={!form.title.trim()}
                  className={`flex items-center gap-2 px-5 py-2.5 rounded-lg transition-all ${
                    form.title.trim()
                      ? "bg-blue-600 hover:bg-blue-700 text-white"
                      : "bg-gray-800 text-gray-500 cursor-not-allowed"
                  }`}
                >
                  <Check className="w-4 h-4" />
                  {editingTask ? "Update Task" : "Create Task"}
                </button>
              </div>
            </motion.div>
          </div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}