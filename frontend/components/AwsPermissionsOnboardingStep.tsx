"use client";

import { useState, useEffect, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  Plus,
  Search,
  Edit3,
  Trash2,
  X,
  Check,
  AlertTriangle,
  Shield,
  ShieldCheck,
  Copy,
  User,
  FileText,
  Ban,
  ArrowLeft,
  ChevronRight,
} from "lucide-react";

/* ------------------------------------------------------------------ */
/*  Props                                                              */
/* ------------------------------------------------------------------ */
export interface AwsPermissionsOnboardingStepProps {
  onNext: () => void;
  onBack: () => void;
  agentName: string;
}

/* ------------------------------------------------------------------ */
/*  API base & current user                                            */
/* ------------------------------------------------------------------ */
const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:6001";
const CURRENT_USER = "admin@chandra.io";
const ADMIN_USERS = ["admin@chandra.io", "superadmin@chandra.io"];

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */
interface PermissionSet {
  id: string;
  name: string;
  description: string;
  aws_service: string;
  actions: string[];
  resource_arn: string;
  created_by: string;
  created_at: string;
  updated_at: string;
}

interface PermissionForm {
  name: string;
  description: string;
  aws_service: string;
  actions: string[];
  resource_arn: string;
}

interface AwsAction {
  action: string;
  description: string;
}

interface Template {
  name: string;
  description: string;
  aws_service: string;
  actions: string[];
  resource_arn: string;
}

/* ------------------------------------------------------------------ */
/*  AWS service list                                                   */
/* ------------------------------------------------------------------ */
const AWS_SERVICES = [
  "S3", "EC2", "VPC", "Lambda", "CloudWatch", "RDS", "IAM",
  "DynamoDB", "SQS", "SNS", "ECS", "ELB", "CloudFront",
  "ElastiCache", "APIGateway", "KMS", "SecretsManager",
  "Route53", "StepFunctions", "Athena",
];

const SERVICE_ICONS: Record<string, string> = {
  S3: "🗄️", EC2: "🖥️", VPC: "🌐", Lambda: "⚡",
  CloudWatch: "📊", RDS: "🗃️", IAM: "🔑", DynamoDB: "📊",
  SQS: "📨", SNS: "🔔", ECS: "🐳", ELB: "⚖️",
  CloudFront: "🌍", ElastiCache: "⚡", APIGateway: "🔌",
  KMS: "🔐", SecretsManager: "🤫", Route53: "🔄",
  StepFunctions: "⛓️", Athena: "🦉",
};

/* ------------------------------------------------------------------ */
/*  Predefined templates                                               */
/* ------------------------------------------------------------------ */
const PREDEFINED_TEMPLATES: Template[] = [
  {
    name: "S3 Full Access",
    description: "Full read/write access to S3 buckets and objects",
    aws_service: "S3",
    actions: ["s3:*"],
    resource_arn: "arn:aws:s3:::*",
  },
  {
    name: "S3 Read Only",
    description: "Read-only access to S3 buckets and objects",
    aws_service: "S3",
    actions: ["s3:GetObject", "s3:ListBucket", "s3:GetBucketLocation"],
    resource_arn: "arn:aws:s3:::*",
  },
  {
    name: "EC2 Operator",
    description: "Full EC2 instance lifecycle management",
    aws_service: "EC2",
    actions: [
      "ec2:RunInstances", "ec2:StartInstances",
      "ec2:StopInstances", "ec2:TerminateInstances",
      "ec2:DescribeInstances",
    ],
    resource_arn: "arn:aws:ec2:*:*:instance/*",
  },
  {
    name: "VPC Admin",
    description: "Full VPC and subnet management",
    aws_service: "VPC",
    actions: [
      "ec2:CreateVpc", "ec2:CreateSubnet",
      "ec2:CreateRouteTable", "ec2:CreateInternetGateway",
      "ec2:DescribeVpcs", "ec2:DescribeSubnets",
    ],
    resource_arn: "arn:aws:ec2:*:*:vpc/*",
  },
  {
    name: "Read Only (All Services)",
    description: "Read-only access across all AWS services",
    aws_service: "ALL",
    actions: [
      "s3:Get*", "s3:List*", "ec2:Describe*",
      "cloudwatch:Describe*", "cloudwatch:Get*",
      "iam:Get*", "iam:List*",
    ],
    resource_arn: "*",
  },
];

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */
function isAdmin(user: string): boolean {
  return ADMIN_USERS.includes(user);
}

function canEdit(ps: PermissionSet, user: string): boolean {
  return isAdmin(user) || ps.created_by === user;
}

/* ------------------------------------------------------------------ */
/*  Component                                                          */
/* ------------------------------------------------------------------ */
function AwsPermissionsOnboardingStep({
  onNext,
  onBack,
  agentName,
}: AwsPermissionsOnboardingStepProps) {
  /* ---- state ---- */
  const [permissionSets, setPermissionSets] = useState<PermissionSet[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [filterService, setFilterService] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [editingPs, setEditingPs] = useState<PermissionSet | null>(null);
  const [form, setForm] = useState<PermissionForm>({
    name: "",
    description: "",
    aws_service: "S3",
    actions: [],
    resource_arn: "",
  });
  const [availableActions, setAvailableActions] = useState<AwsAction[]>([]);
  const [actionsLoading, setActionsLoading] = useState(false);
  const [resourceArns, setResourceArns] = useState<string[]>([]);
  const [showDedupPopup, setShowDedupPopup] = useState(false);
  const [duplicateSets, setDuplicateSets] = useState<PermissionSet[]>([]);
  const [templates] = useState<Template[]>(PREDEFINED_TEMPLATES);
  const [showTemplatePicker, setShowTemplatePicker] = useState(false);
  const [createdByFilter, setCreatedByFilter] = useState<string>("");

  /* ---- fetch permission sets ---- */
  const fetchPermissionSets = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (search) params.set("search", search);
      if (filterService) params.set("aws_service", filterService);
      const res = await fetch(`${API_BASE}/api/permission-sets?${params}`);
      if (res.ok) setPermissionSets(await res.json());
    } catch (e) {
      console.error("Failed to fetch permission sets:", e);
    } finally {
      setLoading(false);
    }
  }, [search, filterService]);

  useEffect(() => {
    fetchPermissionSets();
  }, [fetchPermissionSets]);

  /* ---- fetch actions for the selected AWS service ---- */
  const fetchActions = useCallback(async (service: string) => {
    if (!service) { setAvailableActions([]); return; }
    setActionsLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/permission-sets/actions?aws_service=${encodeURIComponent(service)}`);
      if (res.ok) setAvailableActions((await res.json()).actions || []);
    } catch (e) {
      console.error("Failed to fetch actions:", e);
      setAvailableActions([]);
    } finally {
      setActionsLoading(false);
    }
  }, []);

  /* ---- fetch resource ARN suggestions ---- */
  const fetchResourceArns = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/permission-sets/resource-arns`);
      if (res.ok) setResourceArns((await res.json()).templates?.map((t: {value: string}) => t.value) || []);
    } catch (e) {
      console.error("Failed to fetch resource ARNs:", e);
    }
  }, []);

  useEffect(() => { fetchResourceArns(); }, [fetchResourceArns]);

  useEffect(() => {
    if (showCreate || editingPs) fetchActions(form.aws_service);
  }, [form.aws_service, showCreate, editingPs, fetchActions]);

  /* ---- dedup check ---- */
  const findDuplicates = (aws_service: string, actions: string[], excludeId?: string): PermissionSet[] => {
    const sortedNew = [...actions].sort().join(",");
    return permissionSets.filter((ps) => {
      if (excludeId && ps.id === excludeId) return false;
      if (ps.aws_service !== aws_service) return false;
      const sortedExisting = [...ps.actions].sort().join(",");
      if (sortedExisting === sortedNew) return true;
      const overlap = actions.filter((a) => ps.actions.includes(a)).length;
      const maxLen = Math.max(actions.length, ps.actions.length);
      return maxLen > 0 && overlap / maxLen > 0.66;
    });
  };

  /* ---- CRUD ---- */
  const submitCreate = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/permission-sets`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...form, created_by: CURRENT_USER }),
      });
      if (res.ok) { setShowCreate(false); resetForm(); fetchPermissionSets(); }
    } catch (e) { console.error("Failed to create permission set:", e); }
  };

  const submitUpdate = async () => {
    if (!editingPs) return;
    try {
      const res = await fetch(`${API_BASE}/api/permission-sets/${editingPs.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });
      if (res.ok) { setEditingPs(null); resetForm(); fetchPermissionSets(); }
    } catch (e) { console.error("Failed to update permission set:", e); }
  };

  const createPermissionSet = async () => {
    const dups = findDuplicates(form.aws_service, form.actions);
    if (dups.length > 0) { setDuplicateSets(dups); setShowDedupPopup(true); return; }
    await submitCreate();
  };

  const updatePermissionSet = async () => {
    if (!editingPs) return;
    const dups = findDuplicates(form.aws_service, form.actions, editingPs.id);
    if (dups.length > 0) { setDuplicateSets(dups); setShowDedupPopup(true); return; }
    await submitUpdate();
  };

  const deletePermissionSet = async (id: string) => {
    if (!confirm("Delete this permission set? This action cannot be undone.")) return;
    try {
      const res = await fetch(`${API_BASE}/api/permission-sets/${id}?user=${encodeURIComponent(CURRENT_USER)}`, { method: "DELETE" });
      if (res.ok) fetchPermissionSets();
    } catch (e) { console.error("Failed to delete permission set:", e); }
  };

  /* ---- form helpers ---- */
  const resetForm = () => setForm({ name: "", description: "", aws_service: "S3", actions: [], resource_arn: "" });

  const openCreate = () => { setEditingPs(null); resetForm(); setShowDedupPopup(false); setShowCreate(true); };

  const openEdit = (ps: PermissionSet) => {
    setEditingPs(ps);
    setForm({ name: ps.name, description: ps.description, aws_service: ps.aws_service, actions: [...ps.actions], resource_arn: ps.resource_arn });
    setShowCreate(false); setShowDedupPopup(false);
  };

  const toggleAction = (action: string) => setForm((prev) => ({
    ...prev,
    actions: prev.actions.includes(action) ? prev.actions.filter((a) => a !== action) : [...prev.actions, action],
  }));

  const applyTemplate = (tmpl: Template) => {
    setForm({ name: tmpl.name, description: tmpl.description, aws_service: tmpl.aws_service, actions: [...tmpl.actions], resource_arn: tmpl.resource_arn });
    setShowTemplatePicker(false);
  };

  const handleDedupProceed = () => {
    setShowDedupPopup(false);
    if (editingPs) submitUpdate(); else submitCreate();
  };

  const filteredSets = permissionSets.filter((ps) => {
    if (createdByFilter && ps.created_by !== createdByFilter) return false;
    return true;
  });

  const displayedSets = filteredSets;
  const totalCount = permissionSets.length;

  /* ---- render ---- */
  return (
    <motion.div
      key="aws-permissions-step"
      initial={{ opacity: 0, x: 30 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: -20 }}
      className="space-y-6"
    >
      {/* Header */}
      <div className="flex items-center gap-3 mb-2">
        <ShieldCheck className="w-6 h-6 text-emerald-300" />
        <h3 className="text-2xl font-semibold uppercase tracking-[0.02em] text-frost">
          AWS Permissions
        </h3>
      </div>
      <p className="text-muted text-sm">
        Manage AWS IAM permission sets for {(agentName || "this agent").toUpperCase()}.
        Create, edit, and review access scopes before deployment.
      </p>

      {/* ================ SEARCH & FILTER TOOLBAR ================ */}
      <div className="flex flex-col sm:flex-row gap-3">
        <div className="flex-1 relative">
          <Search className="absolute left-3 top-3 w-4 h-4 text-muted" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search permission sets..."
            className="w-full bg-black/30 border border-white/10 rounded-2xl pl-10 pr-4 py-2.5 text-frost placeholder-muted text-sm focus:outline-none focus:border-emerald-300/40 focus:ring-2 focus:ring-emerald-300/15"
          />
        </div>
        <div className="flex gap-2">
          <select
            value={filterService}
            onChange={(e) => setFilterService(e.target.value)}
            className="bg-black/30 border border-white/10 rounded-2xl px-4 py-2.5 text-frost text-sm focus:outline-none focus:border-emerald-300/40 focus:ring-2 focus:ring-emerald-300/15"
          >
            <option value="">All Services</option>
            {AWS_SERVICES.map((s) => (
              <option key={s} value={s}>{SERVICE_ICONS[s] || "📋"} {s}</option>
            ))}
          </select>
          <button
            onClick={() => setShowTemplatePicker(true)}
            className="flex items-center gap-2 px-4 py-2.5 bg-black/30 border border-white/10 rounded-2xl text-muted hover:text-frost hover:border-white/20 transition-all text-sm"
          >
            <FileText className="w-4 h-4" />
            <span className="hidden sm:inline">Templates</span>
          </button>
          <button
            onClick={openCreate}
            className="flex items-center gap-2 px-4 py-2.5 bg-emerald-300/10 border border-emerald-300/30 rounded-2xl text-emerald-200 hover:bg-emerald-300/20 transition-all text-sm font-semibold"
          >
            <Plus className="w-4 h-4" />
            <span className="hidden sm:inline">Create</span>
          </button>
        </div>
      </div>

      {/* ================ PERMISSION COUNT BADGE ================ */}
      {!loading && (
        <div className="text-[0.6rem] uppercase tracking-[0.2em] text-muted">
          {totalCount} permission set{totalCount !== 1 ? "s" : ""} configured
          {createdByFilter ? ` (filtered by ${createdByFilter})` : ""}
        </div>
      )}

      {/* ================ LIST ================ */}
      <div className="space-y-3 max-h-[340px] overflow-y-auto pr-1 scrollbar-mini">
        {loading ? (
          <div className="flex items-center justify-center py-12">
            <div className="h-8 w-8 rounded-full border-2 border-emerald-300/30 border-t-emerald-300 animate-spin" />
          </div>
        ) : displayedSets.length === 0 ? (
          <div className="rounded-2xl border border-white/10 bg-black/30 p-8 text-center">
            <Shield className="w-10 h-10 mx-auto mb-3 text-muted" />
            <div className="text-[0.7rem] uppercase tracking-[0.16em] text-muted mb-1">
              No AWS permission sets found
            </div>
            <p className="text-sm text-frost/60 mb-4">
              Create one from scratch or use a predefined template.
            </p>
            <div className="flex items-center justify-center gap-3">
              <button onClick={openCreate} className="flex items-center gap-2 px-4 py-2 bg-emerald-300/10 border border-emerald-300/30 rounded-2xl text-emerald-200 hover:bg-emerald-300/20 transition-all text-sm font-semibold">
                <Plus className="w-4 h-4" /> Create New
              </button>
              <button onClick={() => setShowTemplatePicker(true)} className="flex items-center gap-2 px-4 py-2 bg-black/30 border border-white/10 rounded-2xl text-muted hover:text-frost hover:border-white/20 transition-all text-sm">
                <FileText className="w-4 h-4" /> Use Template
              </button>
            </div>
          </div>
        ) : (
          displayedSets.map((ps) => {
            const editable = canEdit(ps, CURRENT_USER);
            return (
              <motion.div
                key={ps.id}
                initial={{ opacity: 0, y: 10 }}
                animate={{ opacity: 1, y: 0 }}
                className="rounded-2xl border border-white/10 bg-black/30 p-4 hover:border-white/20 transition-all"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="flex items-start gap-3 flex-1 min-w-0">
                    <span className="text-xl shrink-0">{SERVICE_ICONS[ps.aws_service] || "📋"}</span>
                    <div className="min-w-0 flex-1">
                      <h4 className="text-sm font-semibold uppercase tracking-[0.04em] text-frost truncate">{ps.name}</h4>
                      <p className="text-xs text-frost/60 mt-0.5 line-clamp-2">{ps.description}</p>
                      <div className="flex flex-wrap items-center gap-1.5 mt-2">
                        <span className="text-[0.55rem] bg-emerald-300/10 text-emerald-300 px-2 py-0.5 rounded-full font-medium uppercase tracking-[0.12em]">{ps.aws_service}</span>
                        {ps.actions.slice(0, 3).map((a) => (
                          <span key={a} className="text-[0.55rem] bg-white/5 text-frost/70 px-1.5 py-0.5 rounded-full border border-white/10" title={a}>{a.split(":").pop()}</span>
                        ))}
                        {ps.actions.length > 3 && <span className="text-[0.55rem] text-muted">+{ps.actions.length - 3}</span>}
                      </div>
                      {ps.resource_arn && (
                        <div className="flex items-center gap-1 mt-1.5">
                          <span className="text-[0.5rem] text-muted uppercase tracking-wider font-mono">ARN:</span>
                          <code className="text-[0.55rem] text-muted font-mono bg-white/5 px-1.5 py-0.5 rounded truncate block max-w-[240px]">{ps.resource_arn}</code>
                        </div>
                      )}
                      <div className="flex items-center gap-2 mt-1.5">
                        <div className="flex items-center gap-1 text-[0.55rem] text-muted">
                          <User className="w-2.5 h-2.5" />{ps.created_by}
                        </div>
                        <span className="text-[0.5rem] text-white/20">·</span>
                        <span className="text-[0.55rem] text-muted">{new Date(ps.updated_at).toLocaleDateString()}</span>
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    <button onClick={() => openEdit(ps)} disabled={!editable}
                      className={`p-1.5 rounded-lg transition-all ${editable ? "text-muted hover:text-emerald-300 hover:bg-white/5" : "text-white/10 cursor-not-allowed"}`}
                      title={editable ? "Edit" : "View only — not your permission set"}
                    >
                      {editable ? <Edit3 className="w-3.5 h-3.5" /> : <Ban className="w-3.5 h-3.5" />}
                    </button>
                    <button onClick={() => deletePermissionSet(ps.id)} disabled={!editable}
                      className={`p-1.5 rounded-lg transition-all ${editable ? "text-muted hover:text-red-400 hover:bg-white/5" : "text-white/10 cursor-not-allowed"}`}
                      title={editable ? "Delete" : "View only — not your permission set"}
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                    {ps.created_by === CURRENT_USER && (
                      <span className="text-[0.5rem] bg-blue-900/40 text-blue-400 px-1.5 py-0.5 rounded-full flex items-center gap-1">
                        <ShieldCheck className="w-2 h-2" /> Owner
                      </span>
                    )}
                    {isAdmin(CURRENT_USER) && ps.created_by !== CURRENT_USER && (
                      <span className="text-[0.5rem] bg-purple-900/40 text-purple-400 px-1.5 py-0.5 rounded-full flex items-center gap-1">
                        <ShieldCheck className="w-2 h-2" /> Admin
                      </span>
                    )}
                  </div>
                </div>
              </motion.div>
            );
          })
        )}
      </div>

      {/* ================ OWNERSHIP FILTER TOGGLE ================ */}
      {permissionSets.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <button onClick={() => setCreatedByFilter("")}
            className={`text-[0.55rem] uppercase tracking-[0.16em] px-2.5 py-1 rounded-full border transition-all ${
              !createdByFilter ? "border-emerald-300/30 bg-emerald-300/10 text-emerald-300" : "border-white/10 text-muted hover:text-frost hover:border-white/20"
            }`}
          >All</button>
          <button onClick={() => setCreatedByFilter(createdByFilter === CURRENT_USER ? "" : CURRENT_USER)}
            className={`text-[0.55rem] uppercase tracking-[0.16em] px-2.5 py-1 rounded-full border transition-all ${
              createdByFilter === CURRENT_USER ? "border-emerald-300/30 bg-emerald-300/10 text-emerald-300" : "border-white/10 text-muted hover:text-frost hover:border-white/20"
            }`}
          >My Sets</button>
        </div>
      )}

      {/* ================ BOTTOM NAV ================ */}
      <div className="flex items-center gap-3 pt-2">
        <button onClick={onBack}
          className="flex items-center gap-2 rounded-2xl border border-white/10 px-4 py-3 text-sm uppercase tracking-[0.14em] text-muted hover:text-frost hover:border-white/20 transition-all"
        >
          <ArrowLeft className="w-3.5 h-3.5" /> Back
        </button>
        <button onClick={onNext}
          className="ml-auto flex items-center gap-2 rounded-2xl bg-emerald-300/10 px-5 py-3 text-sm font-semibold uppercase tracking-[0.14em] text-emerald-200 hover:bg-emerald-300/20 transition-all"
        >
          Continue to Deployment <ChevronRight className="w-3.5 h-3.5" />
        </button>
      </div>

      {/* ================ CREATE / EDIT MODAL ================ */}
      <AnimatePresence>
        {(showCreate || editingPs) && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm">
            <motion.div
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.95 }}
              className="bg-carbon border border-white/10 rounded-2xl p-6 w-full max-w-2xl mx-4 max-h-[90vh] overflow-y-auto shadow-[0_30px_80px_rgba(0,0,0,0.5)]"
            >
              <div className="flex items-center justify-between mb-6">
                <h2 className="text-lg font-semibold text-frost uppercase tracking-[0.04em]">
                  {editingPs ? "Edit Permission Set" : "Create Permission Set"}
                </h2>
                <button onClick={() => { setShowCreate(false); setEditingPs(null); setShowDedupPopup(false); }}
                  className="text-muted hover:text-frost transition-colors"
                ><X className="w-5 h-5" /></button>
              </div>

              <div className="space-y-5">
                <div>
                  <label className="block text-[0.65rem] uppercase tracking-[0.18em] text-muted mb-1.5">Name</label>
                  <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })}
                    placeholder="e.g. S3 Read-Only Access"
                    className="w-full bg-black/30 border border-white/10 rounded-2xl px-4 py-2.5 text-frost placeholder-muted text-sm focus:outline-none focus:border-emerald-300/40 focus:ring-2 focus:ring-emerald-300/15"
                  />
                </div>

                <div>
                  <label className="block text-[0.65rem] uppercase tracking-[0.18em] text-muted mb-1.5">Description</label>
                  <textarea value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })}
                    placeholder="Describe what this permission set allows..." rows={2}
                    className="w-full bg-black/30 border border-white/10 rounded-2xl px-4 py-2.5 text-frost placeholder-muted text-sm focus:outline-none focus:border-emerald-300/40 focus:ring-2 focus:ring-emerald-300/15 resize-none"
                  />
                </div>

                <div>
                  <label className="block text-[0.65rem] uppercase tracking-[0.18em] text-muted mb-1.5">AWS Service</label>
                  <select value={form.aws_service}
                    onChange={(e) => { setForm({ ...form, aws_service: e.target.value, actions: [] }); }}
                    className="w-full bg-black/30 border border-white/10 rounded-2xl px-4 py-2.5 text-frost text-sm focus:outline-none focus:border-emerald-300/40 focus:ring-2 focus:ring-emerald-300/15"
                  >
                    {AWS_SERVICES.map((s) => (<option key={s} value={s}>{SERVICE_ICONS[s] || "📋"} {s}</option>))}
                  </select>
                </div>

                <div>
                  <div className="flex items-center justify-between mb-1.5">
                    <label className="block text-[0.65rem] uppercase tracking-[0.18em] text-muted">Actions</label>
                    <div className="flex gap-2">
                      <button type="button" onClick={() => setForm((prev) => ({ ...prev, actions: availableActions.map((a) => a.action) }))}
                        className="text-[0.6rem] text-emerald-300 hover:text-emerald-200 uppercase tracking-[0.12em]"
                      >Select All</button>
                      <span className="text-[0.6rem] text-white/20">·</span>
                      <button type="button" onClick={() => setForm((prev) => ({ ...prev, actions: [] }))}
                        className="text-[0.6rem] text-muted hover:text-frost uppercase tracking-[0.12em]"
                      >Clear</button>
                    </div>
                  </div>
                  {actionsLoading ? (
                    <div className="text-sm text-muted py-2 flex items-center gap-2">
                      <div className="h-4 w-4 rounded-full border-2 border-emerald-300/30 border-t-emerald-300 animate-spin" /> Loading actions...
                    </div>
                  ) : availableActions.length === 0 ? (
                    <div className="text-sm text-muted py-2 italic border border-white/10 rounded-2xl px-4 bg-black/20">
                      No actions loaded for {form.aws_service}. Try selecting a different service or check the API.
                    </div>
                  ) : (
                    <div className="bg-black/20 border border-white/10 rounded-2xl p-3 max-h-48 overflow-y-auto space-y-1 scrollbar-mini">
                      {availableActions.map((aa, i) => {
                        const checked = form.actions.includes(aa.action);
                        return (
                          <label key={`${aa.action}-${i}`}
                            className={`flex items-start gap-2.5 p-1.5 rounded-xl cursor-pointer transition-colors ${
                              checked ? "bg-emerald-300/10 border border-emerald-300/20" : "hover:bg-white/5 border border-transparent"
                            }`}
                          >
                            <input type="checkbox" checked={checked} onChange={() => toggleAction(aa.action)} className="mt-0.5 accent-emerald-300" />
                            <div className="flex-1 min-w-0">
                              <span className="text-xs text-frost font-mono">{aa.action}</span>
                              {aa.description && <p className="text-[0.6rem] text-muted mt-0.5 line-clamp-1">{aa.description}</p>}
                            </div>
                          </label>
                        );
                      })}
                    </div>
                  )}
                  <div className="flex justify-end mt-1">
                    <span className="text-[0.55rem] text-muted uppercase tracking-[0.12em]">
                      {form.actions.length} action{form.actions.length !== 1 ? "s" : ""} selected
                    </span>
                  </div>
                </div>

                <div>
                  <label className="block text-[0.65rem] uppercase tracking-[0.18em] text-muted mb-1.5">Resource ARN</label>
                  <input value={form.resource_arn} onChange={(e) => setForm({ ...form, resource_arn: e.target.value })}
                    placeholder="arn:aws:s3:::my-bucket/*" list="arn-suggestions"
                    className="w-full bg-black/30 border border-white/10 rounded-2xl px-4 py-2.5 text-frost placeholder-muted font-mono text-sm focus:outline-none focus:border-emerald-300/40 focus:ring-2 focus:ring-emerald-300/15"
                  />
                  <datalist id="arn-suggestions">{resourceArns.map((arn) => (<option key={arn} value={arn} />))}</datalist>
                </div>
              </div>

              <div className="flex gap-3 justify-end mt-6 pt-4 border-t border-white/10">
                <button onClick={() => { setShowCreate(false); setEditingPs(null); setShowDedupPopup(false); }}
                  className="px-5 py-2.5 rounded-2xl border border-white/10 text-muted hover:text-frost hover:border-white/20 transition-colors text-sm uppercase tracking-[0.12em]"
                >Cancel</button>
                <button onClick={editingPs ? updatePermissionSet : createPermissionSet} disabled={!form.name.trim()}
                  className={`flex items-center gap-2 px-5 py-2.5 rounded-2xl transition-all text-sm font-semibold uppercase tracking-[0.12em] ${
                    form.name.trim()
                      ? "bg-emerald-300/10 border border-emerald-300/30 text-emerald-200 hover:bg-emerald-300/20"
                      : "bg-black/30 border border-white/10 text-muted cursor-not-allowed"
                  }`}
                ><Check className="w-4 h-4" />{editingPs ? "Update" : "Create"}</button>
              </div>
            </motion.div>
          </div>
        )}
      </AnimatePresence>

      {/* ================ DEDUP POPUP ================ */}
      <AnimatePresence>
        {showDedupPopup && (
          <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/70 backdrop-blur-sm">
            <motion.div
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.95 }}
              className="bg-carbon border border-amber/30 rounded-2xl p-6 w-full max-w-lg mx-4 shadow-[0_30px_80px_rgba(0,0,0,0.5)]"
            >
              <div className="flex items-center gap-3 mb-4">
                <AlertTriangle className="w-6 h-6 text-amber shrink-0" />
                <h2 className="text-lg font-semibold text-frost uppercase tracking-[0.04em]">Duplicate Detected</h2>
              </div>
              <p className="text-sm text-muted mb-4">
                The following permission set(s) already have similar actions for{" "}
                <span className="text-emerald-300 font-semibold">{form.aws_service}</span>:
              </p>
              <div className="space-y-2 mb-5 max-h-40 overflow-y-auto scrollbar-mini">
                {duplicateSets.map((dup) => (
                  <div key={dup.id} className="bg-black/30 border border-white/10 rounded-2xl p-3">
                    <p className="text-sm font-medium text-frost">{dup.name}</p>
                    <p className="text-xs text-muted mt-0.5">by {dup.created_by}</p>
                    <div className="flex flex-wrap gap-1 mt-1.5">
                      {dup.actions.slice(0, 3).map((a) => (<span key={a} className="text-[0.5rem] bg-white/10 text-frost/70 px-1.5 py-0.5 rounded">{a.split(":").pop()}</span>))}
                      {dup.actions.length > 3 && <span className="text-[0.5rem] text-muted">+{dup.actions.length - 3}</span>}
                    </div>
                  </div>
                ))}
              </div>
              <div className="flex gap-3 justify-end">
                <button onClick={() => setShowDedupPopup(false)}
                  className="px-5 py-2.5 rounded-2xl border border-white/10 text-muted hover:text-frost hover:border-white/20 transition-colors text-sm uppercase tracking-[0.12em]"
                >Cancel</button>
                <button onClick={handleDedupProceed}
                  className="flex items-center gap-2 px-5 py-2.5 rounded-2xl bg-amber/10 border border-amber/30 text-amber hover:bg-amber/20 transition-all text-sm font-semibold uppercase tracking-[0.12em]"
                ><Copy className="w-4 h-4" /> Create Anyway</button>
              </div>
            </motion.div>
          </div>
        )}
      </AnimatePresence>

      {/* ================ TEMPLATE PICKER MODAL ================ */}
      <AnimatePresence>
        {showTemplatePicker && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm">
            <motion.div
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.95 }}
              className="bg-carbon border border-white/10 rounded-2xl p-6 w-full max-w-2xl mx-4 max-h-[80vh] overflow-y-auto shadow-[0_30px_80px_rgba(0,0,0,0.5)]"
            >
              <div className="flex items-center justify-between mb-6">
                <div className="flex items-center gap-3">
                  <FileText className="w-6 h-6 text-emerald-300" />
                  <h2 className="text-lg font-semibold text-frost uppercase tracking-[0.04em]">Predefined Templates</h2>
                </div>
                <button onClick={() => setShowTemplatePicker(false)} className="text-muted hover:text-frost transition-colors"><X className="w-5 h-5" /></button>
              </div>
              <p className="text-sm text-muted mb-4">Select a template to pre-fill a permission set. You can customize it before saving.</p>
              <div className="space-y-2">
                {templates.map((tmpl, idx) => (
                  <motion.div
                    key={idx}
                    initial={{ opacity: 0, x: -10 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ delay: idx * 0.05 }}
                    onClick={() => { applyTemplate(tmpl); openCreate(); }}
                    className="bg-black/30 border border-white/10 rounded-2xl p-4 cursor-pointer hover:border-emerald-300/40 hover:bg-black/40 transition-all"
                  >
                    <div className="flex items-start gap-3">
                      <span className="text-xl">{SERVICE_ICONS[tmpl.aws_service] || "📋"}</span>
                      <div className="flex-1 min-w-0">
                        <h3 className="text-sm font-semibold text-frost">{tmpl.name}</h3>
                        <p className="text-xs text-muted mt-0.5">{tmpl.description}</p>
                        <div className="flex flex-wrap gap-1 mt-2">
                          <span className="text-[0.5rem] bg-white/10 text-emerald-300 px-1.5 py-0.5 rounded uppercase tracking-[0.1em]">{tmpl.aws_service}</span>
                          {tmpl.actions.slice(0, 3).map((a) => (<span key={a} className="text-[0.5rem] bg-white/5 text-frost/70 px-1.5 py-0.5 rounded">{a.split(":").pop()}</span>))}
                          {tmpl.actions.length > 3 && <span className="text-[0.5rem] text-muted">+{tmpl.actions.length - 3}</span>}
                        </div>
                        <code className="text-[0.5rem] text-muted font-mono mt-1 block truncate">{tmpl.resource_arn}</code>
                      </div>
                    </div>
                  </motion.div>
                ))}
              </div>
            </motion.div>
          </div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

export { AwsPermissionsOnboardingStep as default, AwsPermissionsOnboardingStep };