"use client";

import { useState, useEffect, useCallback, useMemo } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Plus, Search, Edit3, Trash2, Check, AlertTriangle, X, Settings2, ShieldAlert } from "lucide-react";
import { fetchAwsTasks, saveAwsTasks, type AwsTask } from "@/services/api";
import { useOnboarding } from "@/store/OnboardingContext";
import { generateEmployeeId, normalizeAgentName } from "@/store/agentProfile";

const RESOURCE_ICONS: Record<string, string> = {
  S3: "🗄️", EC2: "🖥️", VPC: "🌐", Lambda: "⚡", CloudWatch: "📊",
  RDS: "🗃️", IAM: "🔑", DynamoDB: "📊", SQS: "📨", SNS: "🔔",
  ECS: "🐳", ELB: "⚖️", CloudFront: "🌍", ElastiCache: "⚡", APIGateway: "🔌",
};

export default function AwsTasksStep({ onNext, onPrev }: { onNext: () => void; onPrev: () => void }) {
  const { agentName, selectedAwsTasks, toggleAwsTask, addAwsTask, removeAwsTask } = useOnboarding();
  const [tasks, setTasks] = useState<AwsTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  
  // CRUD State
  const [showForm, setShowForm] = useState(false);
  const [editingTask, setEditingTask] = useState<AwsTask | null>(null);
  const [form, setForm] = useState<Partial<AwsTask>>({
    name: "", description: "", category: "S3"
  });

  const currentUser = agentName || "System";

  const loadTasks = useCallback(async () => {
    setLoading(true);
    const data = await fetchAwsTasks();
    setTasks(data);
    setLoading(false);
  }, []);

  useEffect(() => { loadTasks(); }, [loadTasks]);

  const filteredTasks = useMemo(() => {
    const s = search.toLowerCase();
    return tasks.filter(t => 
      t.name.toLowerCase().includes(s) || 
      t.description.toLowerCase().includes(s) ||
      t.category.toLowerCase().includes(s)
    );
  }, [tasks, search]);

  const handleSaveTask = async () => {
    if (!form.name || !form.description) return;
    
    // Check for duplicates
    const isDuplicate = tasks.some(t => t.name.toLowerCase() === form.name?.toLowerCase() && t.id !== editingTask?.id);
    if (isDuplicate) {
      alert("A task with this name already exists.");
      return;
    }

    let updatedTasks = [...tasks];
    if (editingTask) {
      // Create new version if ownership differs, otherwise edit
      if (editingTask.ownership && editingTask.ownership !== currentUser && editingTask.ownership !== "System") {
        // Create new version
        const newTask: AwsTask = {
          ...editingTask,
          ...form,
          id: `task_${Date.now()}`,
          version: (editingTask.version || 1) + 1,
          ownership: currentUser,
          is_preset: false,
          is_predefined: false
        };
        updatedTasks.push(newTask);
      } else {
        // Edit existing
        updatedTasks = updatedTasks.map(t => t.id === editingTask.id ? { ...t, ...form } as AwsTask : t);
      }
    } else {
      // Create new
      const newTask: AwsTask = {
        id: `task_${Date.now()}`,
        name: form.name!,
        description: form.description!,
        category: form.category || "S3",
        ownership: currentUser,
        version: 1,
        is_preset: false,
        is_predefined: false
      };
      updatedTasks.push(newTask);
    }

    await saveAwsTasks(updatedTasks);
    setTasks(updatedTasks);
    setShowForm(false);
    setEditingTask(null);
    setForm({ name: "", description: "", category: "S3" });
  };

  const handleDeleteTask = async (task: AwsTask) => {
    if (task.ownership === "System" || task.is_predefined) {
      alert("System predefined tasks cannot be deleted.");
      return;
    }
    if (task.ownership !== currentUser) {
      alert("You can only delete tasks you own.");
      return;
    }
    if (!confirm(`Delete task ${task.name}?`)) return;
    const updatedTasks = tasks.filter(t => t.id !== task.id);
    await saveAwsTasks(updatedTasks);
    setTasks(updatedTasks);
    // Remove from selection if deleted
    if (selectedAwsTasks.includes(task.id)) {
      removeAwsTask(task.id);
    }
  };

  const openEdit = (task: AwsTask) => {
    setEditingTask(task);
    setForm({
      name: task.name,
      description: task.description,
      category: task.category
    });
    setShowForm(true);
  };

  const canProceed = selectedAwsTasks.length > 0;

  return (
    <motion.div key="aws-tasks" initial={{ opacity: 0, x: 30 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -20 }}>
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-2xl font-semibold uppercase tracking-[0.02em]">
            ASSIGN AWS TASKS TO {(agentName || "THIS AGENT").toUpperCase()}
          </h3>
          <p className="text-muted mt-2">Select the specific AWS operational tasks this worker is authorized to execute.</p>
        </div>
        <button
          onClick={() => { setShowForm(true); setEditingTask(null); setForm({ name: "", description: "", category: "S3" }); }}
          className="flex items-center gap-2 rounded-2xl bg-emerald-300/10 px-4 py-2.5 text-xs font-semibold uppercase tracking-[0.14em] text-emerald-200 transition hover:bg-emerald-300/20"
        >
          <Plus size={14} /> Create Task
        </button>
      </div>

      <div className="mt-6 relative">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="SEARCH AWS TASKS..."
          className="w-full rounded-2xl border border-white/10 bg-black/30 py-3 pl-9 pr-4 text-sm text-frost outline-none transition placeholder:text-muted focus:border-emerald-300/40 focus:ring-2 focus:ring-emerald-300/15 uppercase"
        />
      </div>

      <div className="mt-4 grid gap-4 md:grid-cols-2 max-h-[400px] overflow-y-auto pr-2 custom-kra-scroll">
        {loading ? (
          <div className="col-span-2 text-center py-12 text-muted text-sm uppercase tracking-[0.1em]">Loading AWS Tasks...</div>
        ) : filteredTasks.length === 0 ? (
           <div className="col-span-2 text-center py-12 text-muted text-sm uppercase tracking-[0.1em]">No AWS Tasks Found. Create one.</div>
        ) : (
          filteredTasks.map((task) => {
            const isSelected = selectedAwsTasks.includes(task.id);
            return (
              <div 
                key={task.id} 
                className={`flex flex-col justify-between rounded-3xl border p-5 transition ${isSelected ? "border-emerald-300/40 bg-emerald-300/10" : "border-white/10 bg-black/30 hover:border-emerald-300/20"}`}
              >
                <div className="flex items-start gap-4">
                  <div className="flex items-center justify-center mt-1">
                    <input
                      type="checkbox"
                      checked={isSelected}
                      onChange={() => {
                        if (isSelected) removeAwsTask(task.id);
                        else addAwsTask(task.id);
                      }}
                      className="h-4 w-4 accent-emerald-300 cursor-pointer"
                    />
                  </div>
                  <div className="flex-1">
                    <div className="flex items-center gap-2">
                      <span className="text-xl">{RESOURCE_ICONS[task.category] || "📋"}</span>
                      <div className="font-semibold uppercase tracking-[0.04em] text-frost">{task.name}</div>
                    </div>
                    <div className="mt-2 text-sm text-frost/70 line-clamp-2">{task.description}</div>
                    
                    <div className="mt-3 flex items-center justify-between">
                      <div className="flex gap-2">
                        <span className="rounded-full border border-signal/30 bg-signal/10 px-2 py-0.5 text-[0.55rem] uppercase tracking-[0.16em] text-signal">
                          {task.category}
                        </span>
                        {task.version && (
                          <span className="rounded-full border border-blue-400/30 bg-blue-400/10 px-2 py-0.5 text-[0.55rem] uppercase tracking-[0.16em] text-blue-400">
                            V{task.version}
                          </span>
                        )}
                        <span className="rounded-full border border-white/10 bg-white/5 px-2 py-0.5 text-[0.55rem] uppercase tracking-[0.16em] text-muted">
                          {task.ownership || "System"}
                        </span>
                      </div>
                      
                      <div className="flex gap-1">
                        <button onClick={(e) => { e.stopPropagation(); openEdit(task); }} className="p-1.5 text-muted hover:text-emerald-300 transition" title="Edit">
                          <Edit3 size={14} />
                        </button>
                        <button onClick={(e) => { e.stopPropagation(); handleDeleteTask(task); }} className="p-1.5 text-muted hover:text-red-400 transition" title="Delete">
                          <Trash2 size={14} />
                        </button>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            );
          })
        )}
      </div>

      <div className="mt-6 flex items-center gap-3">
        <button onClick={onPrev} className="rounded-2xl border border-white/10 px-4 py-3 text-sm uppercase tracking-[0.14em] text-muted transition hover:bg-white/5">BACK</button>
        <button onClick={onNext} disabled={!canProceed} className="ml-auto rounded-2xl bg-emerald-300/10 px-5 py-3 text-sm font-semibold uppercase tracking-[0.14em] text-emerald-200 transition hover:bg-emerald-300/20 disabled:opacity-50">CONTINUE</button>
      </div>

      {/* Form Modal */}
      <AnimatePresence>
        {showForm && (
          <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/80 backdrop-blur-sm">
            <motion.div
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.95 }}
              className="w-full max-w-lg rounded-3xl border border-white/10 bg-black/90 p-6 shadow-2xl"
            >
              <div className="mb-6 flex items-center justify-between">
                <h4 className="text-lg font-semibold uppercase tracking-[0.04em] text-frost">
                  {editingTask ? "EDIT AWS TASK" : "CREATE NEW AWS TASK"}
                </h4>
                <button onClick={() => setShowForm(false)} className="text-muted hover:text-frost">
                  <X size={20} />
                </button>
              </div>

              <div className="space-y-4">
                <div>
                  <label className="mb-1 block text-[0.65rem] uppercase tracking-[0.16em] text-muted">TASK NAME</label>
                  <input
                    value={form.name}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                    className="w-full rounded-2xl border border-white/10 bg-black/30 px-4 py-3 text-sm text-frost outline-none transition focus:border-emerald-300/40 focus:ring-2 focus:ring-emerald-300/15"
                    placeholder="e.g. Create S3 Bucket"
                  />
                </div>
                <div>
                  <label className="mb-1 block text-[0.65rem] uppercase tracking-[0.16em] text-muted">DESCRIPTION</label>
                  <textarea
                    value={form.description}
                    onChange={(e) => setForm({ ...form, description: e.target.value })}
                    rows={3}
                    className="w-full resize-none rounded-2xl border border-white/10 bg-black/30 px-4 py-3 text-sm text-frost outline-none transition focus:border-emerald-300/40 focus:ring-2 focus:ring-emerald-300/15"
                    placeholder="Describe what this task does..."
                  />
                </div>
                <div>
                  <label className="mb-1 block text-[0.65rem] uppercase tracking-[0.16em] text-muted">RESOURCE CATEGORY</label>
                  <select
                    value={form.category}
                    onChange={(e) => setForm({ ...form, category: e.target.value })}
                    className="w-full rounded-2xl border border-white/10 bg-black/30 px-4 py-3 text-sm text-frost outline-none transition focus:border-emerald-300/40 focus:ring-2 focus:ring-emerald-300/15 appearance-none"
                  >
                    {Object.keys(RESOURCE_ICONS).map(k => (
                      <option key={k} value={k} className="bg-gray-900">{k}</option>
                    ))}
                  </select>
                </div>
              </div>

              <div className="mt-8 flex justify-end gap-3">
                <button onClick={() => setShowForm(false)} className="rounded-2xl border border-white/10 px-5 py-2.5 text-xs uppercase tracking-[0.14em] text-muted hover:bg-white/5">CANCEL</button>
                <button 
                  onClick={handleSaveTask}
                  disabled={!form.name || !form.description}
                  className="rounded-2xl bg-emerald-300/10 px-5 py-2.5 text-xs font-semibold uppercase tracking-[0.14em] text-emerald-200 transition hover:bg-emerald-300/20 disabled:opacity-50"
                >
                  SAVE TASK
                </button>
              </div>
            </motion.div>
          </div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}
