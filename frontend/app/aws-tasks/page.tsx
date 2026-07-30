"use client";

import { useState, useEffect, useCallback } from "react";
import { motion } from "framer-motion";
import { Plus, Search, Edit3, Trash2, Play, X, Check, AlertTriangle, Server } from "lucide-react";

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
  S3: "🗄️", EC2: "🖥️", VPC: "🌐", Lambda: "⚡", CloudWatch: "📊",
  RDS: "🗃️", IAM: "🔑", DynamoDB: "📊", SQS: "📨", SNS: "🔔",
  ECS: "🐳", ELB: "⚖️", CloudFront: "🌍", ElastiCache: "⚡", APIGateway: "🔌",
};

export default function AwsTasksPage() {
  const [tasks, setTasks] = useState<AwsTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [filterType, setFilterType] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [editingTask, setEditingTask] = useState<AwsTask | null>(null);
  const [form, setForm] = useState({ title: "", description: "", resource_type: "S3", operation: "", resource_config: "" });
  const [currentUser, setCurrentUser] = useState("nagendra");

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

  useEffect(() => { fetchTasks(); }, [fetchTasks]);

  const createTask = async () => {
    try {
      const res = await fetch(`${API_BASE}/aws-tasks`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...form, created_by: currentUser, operation: form.operation, resource_config: form.resource_config }),
      });
      if (res.ok) {
        setShowCreate(false);
        setForm({ title: "", description: "", resource_type: "S3", operation: "", resource_config: "" });
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
        setForm({ title: "", description: "", resource_type: "S3", operation: "", resource_config: "" });
        fetchTasks();
      }
    } catch (e) {
      console.error("Failed to update task:", e);
    }
  };

  const deleteTask = async (id: string) => {
    if (!confirm("Delete this task?")) return;
    try {
      const res = await fetch(`${API_BASE}/aws-tasks/${id}`, { method: "DELETE" });
      if (res.ok) fetchTasks();
    } catch (e) {
      console.error("Failed to delete task:", e);
    }
  };

  const openEdit = (task: AwsTask) => {
    setEditingTask(task);
    setForm({ title: task.title, description: task.description, resource_type: task.resource_type, operation: task.operation || "", resource_config: task.resource_config || "" });
  };

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100 p-6">
      <div className="max-w-6xl mx-auto">
        {/* Header */}
        <div className="flex items-center justify-between mb-8">
          <div className="flex items-center gap-3">
            <Server className="w-7 h-7 text-blue-400" />
            <h1 className="text-2xl font-bold text-white">AWS Tasks</h1>
          </div>
          <button
            onClick={() => { setShowCreate(true); setEditingTask(null); setForm({ title: "", description: "", resource_type: "S3", operation: "", resource_config: "" }); }}
            className="flex items-center gap-2 px-4 py-2.5 bg-blue-600 hover:bg-blue-700 rounded-lg text-white transition-all"
          >
            <Plus className="w-4 h-4" />
            Create New Task
          </button>
        </div>

        {/* Search & Filter */}
        <div className="flex gap-4 mb-6">
          <div className="flex-1 relative">
            <Search className="absolute left-3 top-3 w-4 h-4 text-gray-500" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search tasks..."
              className="w-full bg-gray-800 border border-gray-700 rounded-lg pl-10 pr-4 py-2.5 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <select
            value={filterType}
            onChange={(e) => setFilterType(e.target.value)}
            className="bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            <option value="">All Types</option>
            <option value="S3">S3 Bucket</option>
            <option value="EC2">EC2 Instance</option>
            <option value="VPC">VPC Network</option>
            <option value="Lambda">Lambda Function</option>
            <option value="RDS">RDS Database</option>
            <option value="IAM">IAM Role/Policy</option>
          </select>
        </div>

        {/* Task List */}
        {loading ? (
          <div className="text-center py-12 text-gray-500">Loading tasks...</div>
        ) : tasks.length === 0 ? (
          <div className="text-center py-12">
            <AlertTriangle className="w-12 h-12 mx-auto mb-4 text-gray-600" />
            <p className="text-gray-500">No AWS tasks found. Create one to get started.</p>
          </div>
        ) : (
          <div className="space-y-3">
            {tasks.map((task) => (
              <motion.div
                key={task.id}
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                className="bg-gray-900 border border-gray-800 rounded-xl p-5 hover:border-gray-700 transition-all"
              >
                <div className="flex items-start justify-between">
                  <div className="flex items-start gap-4">
                    <span className="text-2xl">{RESOURCE_ICONS[task.resource_type] || "📋"}</span>
                    <div>
                      <h3 className="text-lg font-semibold text-white">{task.title}</h3>
                      <p className="text-sm text-gray-400 mt-1">{task.description}</p>
                      <div className="flex items-center gap-3 mt-2">
                        <span className="text-xs bg-gray-800 text-blue-400 px-2 py-0.5 rounded-full">
                          {task.resource_type}
                        </span>
                        <span className="text-xs text-gray-500">
                          Updated: {new Date(task.updated_at).toLocaleDateString()}
                        </span>
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => openEdit(task)}
                      className="p-2 text-gray-500 hover:text-blue-400 hover:bg-gray-800 rounded-lg transition-all"
                      title="Edit"
                    >
                      <Edit3 className="w-4 h-4" />
                    </button>
                    <button
                      onClick={() => deleteTask(task.id)}
                      className="p-2 text-gray-500 hover:text-red-400 hover:bg-gray-800 rounded-lg transition-all"
                      title="Delete"
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                    <button
                      className="p-2 text-gray-500 hover:text-green-400 hover:bg-gray-800 rounded-lg transition-all"
                      title="Execute"
                    >
                      <Play className="w-4 h-4" />
                    </button>
                  </div>
                </div>
              </motion.div>
            ))}
          </div>
        )}
      </div>

      {/* Create/Edit Modal */}
      {(showCreate || editingTask) && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
          <motion.div
            initial={{ opacity: 0, scale: 0.95 }}
            animate={{ opacity: 1, scale: 1 }}
            className="bg-gray-900 border border-gray-800 rounded-2xl p-6 w-full max-w-lg mx-4"
          >
            <div className="flex items-center justify-between mb-6">
              <h2 className="text-xl font-semibold text-white">
                {editingTask ? "Edit Task" : "Create New AWS Task"}
              </h2>
              <button onClick={() => { setShowCreate(false); setEditingTask(null); }} className="text-gray-500 hover:text-white">
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-400 mb-1">Title</label>
                <input
                  value={form.title}
                  onChange={(e) => setForm({ ...form, title: e.target.value })}
                  placeholder="e.g. Create S3 bucket for logs"
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-400 mb-1">Description</label>
                <textarea
                  value={form.description}
                  onChange={(e) => setForm({ ...form, description: e.target.value })}
                  placeholder="Describe what this task does..."
                  rows={3}
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-400 mb-1">Resource Type</label>
                <select
                  value={form.resource_type}
                  onChange={(e) => setForm({ ...form, resource_type: e.target.value })}
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
                >
                  {["S3", "EC2", "VPC", "Lambda", "CloudWatch", "RDS", "IAM", "DynamoDB", "SQS", "SNS", "ECS", "ELB", "CloudFront", "ElastiCache", "APIGateway"].map((t) => (
                    <option key={t} value={t}>{t}</option>
                  ))}
                </select>
              </div>
            </div>

            <div className="flex gap-3 justify-end mt-6 pt-4 border-t border-gray-800">
              <button
                onClick={() => { setShowCreate(false); setEditingTask(null); }}
                className="px-5 py-2.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={editingTask ? updateTask : createTask}
                disabled={!form.title.trim()}
                className={`flex items-center gap-2 px-5 py-2.5 rounded-lg transition-all ${
                  form.title.trim() ? "bg-blue-600 hover:bg-blue-700 text-white" : "bg-gray-800 text-gray-500 cursor-not-allowed"
                }`}
              >
                <Check className="w-4 h-4" />
                {editingTask ? "Update Task" : "Create Task"}
              </button>
            </div>
          </motion.div>
        </div>
      )}
    </div>
  );
}