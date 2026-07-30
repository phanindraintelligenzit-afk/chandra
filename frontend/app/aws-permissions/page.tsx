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
  Copy,
  User,
  ShieldCheck,
  FileText,
  Ban,
} from "lucide-react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:6001";
const CURRENT_USER = "admin@chandra.io"; // Simulated current user
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
  "KMS",
  "SecretsManager",
  "Route53",
  "StepFunctions",
  "Athena",
];

const SERVICE_ICONS: Record<string, string> = {
  S3: "🗄️",
  EC2: "🖥️",
  VPC: "🌐",
  Lambda: "⚡",
  CloudWatch: "📊",
  RDS: "🗃️",
  IAM: "🔑",
  DynamoDB: "📊",
  SQS: "📨",
  SNS: "🔔",
  ECS: "🐳",
  ELB: "⚖️",
  CloudFront: "🌍",
  ElastiCache: "⚡",
  APIGateway: "🔌",
  KMS: "🔐",
  SecretsManager: "🤫",
  Route53: "🔄",
  StepFunctions: "⛓️",
  Athena: "🦉",
};

/* ------------------------------------------------------------------ */
/*  Predefined templates (seeded on first load)                        */
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
      "ec2:RunInstances",
      "ec2:StartInstances",
      "ec2:StopInstances",
      "ec2:TerminateInstances",
      "ec2:DescribeInstances",
    ],
    resource_arn: "arn:aws:ec2:*:*:instance/*",
  },
  {
    name: "VPC Admin",
    description: "Full VPC and subnet management",
    aws_service: "VPC",
    actions: [
      "ec2:CreateVpc",
      "ec2:CreateSubnet",
      "ec2:CreateRouteTable",
      "ec2:CreateInternetGateway",
      "ec2:DescribeVpcs",
      "ec2:DescribeSubnets",
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
export default function AwsPermissionsPage() {
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
  const [templates, setTemplates] = useState<Template[]>(PREDEFINED_TEMPLATES);
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
    if (!service) {
      setAvailableActions([]);
      return;
    }
    setActionsLoading(true);
    try {
      const res = await fetch(
        `${API_BASE}/api/permission-sets/actions?aws_service=${encodeURIComponent(service)}`
      );
      if (res.ok) setAvailableActions(await res.json());
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
      if (res.ok) setResourceArns(await res.json());
    } catch (e) {
      console.error("Failed to fetch resource ARNs:", e);
    }
  }, []);

  useEffect(() => {
    fetchResourceArns();
  }, [fetchResourceArns]);

  /* ---- when aws_service changes in form, fetch actions ---- */
  useEffect(() => {
    if (showCreate || editingPs) {
      fetchActions(form.aws_service);
    }
  }, [form.aws_service, showCreate, editingPs, fetchActions]);

  /* ---- dedup check ---- */
  const findDuplicates = (
    aws_service: string,
    actions: string[],
    excludeId?: string
  ): PermissionSet[] => {
    const sortedNew = [...actions].sort().join(",");
    return permissionSets.filter((ps) => {
      if (excludeId && ps.id === excludeId) return false;
      if (ps.aws_service !== aws_service) return false;
      const sortedExisting = [...ps.actions].sort().join(",");
      // Same actions → duplicate
      if (sortedExisting === sortedNew) return true;
      // Significant overlap > 66%
      const overlap = actions.filter((a) => ps.actions.includes(a)).length;
      const maxLen = Math.max(actions.length, ps.actions.length);
      if (maxLen > 0 && overlap / maxLen > 0.66) return true;
      return false;
    });
  };

  /* ---- CRUD ---- */
  const createPermissionSet = async () => {
    // Dedup check before creating
    const dups = findDuplicates(form.aws_service, form.actions);
    if (dups.length > 0) {
      setDuplicateSets(dups);
      setShowDedupPopup(true);
      return;
    }
    await submitCreate();
  };

  const submitCreate = async () => {
    try {
      const res = await fetch(`${API_BASE}/api/permission-sets`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...form, created_by: CURRENT_USER }),
      });
      if (res.ok) {
        setShowCreate(false);
        resetForm();
        fetchPermissionSets();
      }
    } catch (e) {
      console.error("Failed to create permission set:", e);
    }
  };

  const updatePermissionSet = async () => {
    if (!editingPs) return;
    // Dedup check before updating
    const dups = findDuplicates(form.aws_service, form.actions, editingPs.id);
    if (dups.length > 0) {
      setDuplicateSets(dups);
      setShowDedupPopup(true);
      return;
    }
    await submitUpdate();
  };

  const submitUpdate = async () => {
    if (!editingPs) return;
    try {
      const res = await fetch(`${API_BASE}/api/permission-sets/${editingPs.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });
      if (res.ok) {
        setEditingPs(null);
        resetForm();
        fetchPermissionSets();
      }
    } catch (e) {
      console.error("Failed to update permission set:", e);
    }
  };

  const deletePermissionSet = async (id: string) => {
    if (!confirm("Delete this permission set? This action cannot be undone."))
      return;
    try {
      const res = await fetch(
        `${API_BASE}/api/permission-sets/${id}?user=${encodeURIComponent(CURRENT_USER)}`,
        { method: "DELETE" }
      );
      if (res.ok) fetchPermissionSets();
    } catch (e) {
      console.error("Failed to delete permission set:", e);
    }
  };

  /* ---- form helpers ---- */
  const resetForm = () => {
    setForm({
      name: "",
      description: "",
      aws_service: "S3",
      actions: [],
      resource_arn: "",
    });
  };

  const openCreate = () => {
    setEditingPs(null);
    resetForm();
    setShowDedupPopup(false);
    setShowCreate(true);
  };

  const openEdit = (ps: PermissionSet) => {
    setEditingPs(ps);
    setForm({
      name: ps.name,
      description: ps.description,
      aws_service: ps.aws_service,
      actions: [...ps.actions],
      resource_arn: ps.resource_arn,
    });
    setShowCreate(false);
    setShowDedupPopup(false);
  };

  const toggleAction = (action: string) => {
    setForm((prev) => ({
      ...prev,
      actions: prev.actions.includes(action)
        ? prev.actions.filter((a) => a !== action)
        : [...prev.actions, action],
    }));
  };

  const applyTemplate = (tmpl: Template) => {
    setForm({
      name: tmpl.name,
      description: tmpl.description,
      aws_service: tmpl.aws_service,
      actions: [...tmpl.actions],
      resource_arn: tmpl.resource_arn,
    });
    setShowTemplatePicker(false);
  };

  const handleDedupProceed = () => {
    setShowDedupPopup(false);
    if (editingPs) {
      submitUpdate();
    } else {
      submitCreate();
    }
  };

  /* ---- filter for view-only logic ---- */
  const filteredSets = permissionSets.filter((ps) => {
    if (createdByFilter && ps.created_by !== createdByFilter) return false;
    return true;
  });

  const displayedSets = filteredSets;

  /* ---- render ---- */
  return (
    <div className="min-h-screen bg-gray-950 text-gray-100 p-6">
      <div className="max-w-6xl mx-auto">
        {/* ================ HEADER ================ */}
        <div className="flex items-center justify-between mb-8">
          <div className="flex items-center gap-3">
            <Shield className="w-7 h-7 text-emerald-400" />
            <h1 className="text-2xl font-bold text-white">AWS Permissions</h1>
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={() => setShowTemplatePicker(true)}
              className="flex items-center gap-2 px-4 py-2.5 bg-gray-800 hover:bg-gray-700 rounded-lg text-gray-300 transition-all border border-gray-700"
            >
              <FileText className="w-4 h-4" />
              Templates
            </button>
            <button
              onClick={openCreate}
              className="flex items-center gap-2 px-4 py-2.5 bg-emerald-600 hover:bg-emerald-700 rounded-lg text-white transition-all"
            >
              <Plus className="w-4 h-4" />
              Create Permission Set
            </button>
          </div>
        </div>

        {/* ================ SEARCH & FILTER ================ */}
        <div className="flex gap-4 mb-6">
          <div className="flex-1 relative">
            <Search className="absolute left-3 top-3 w-4 h-4 text-gray-500" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search permission sets..."
              className="w-full bg-gray-800 border border-gray-700 rounded-lg pl-10 pr-4 py-2.5 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-emerald-500"
            />
          </div>
          <select
            value={filterService}
            onChange={(e) => setFilterService(e.target.value)}
            className="bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-white focus:outline-none focus:ring-2 focus:ring-emerald-500"
          >
            <option value="">All Services</option>
            {AWS_SERVICES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>

        {/* ================ LIST ================ */}
        {loading ? (
          <div className="text-center py-12 text-gray-500">
            Loading permission sets...
          </div>
        ) : displayedSets.length === 0 ? (
          <div className="text-center py-12">
            <Shield className="w-12 h-12 mx-auto mb-4 text-gray-600" />
            <p className="text-gray-400 mb-2">
              No AWS permission sets found.
            </p>
            <p className="text-sm text-gray-500 mb-6">
              Create one from scratch or use a predefined template.
            </p>
            <div className="flex items-center justify-center gap-3">
              <button
                onClick={openCreate}
                className="flex items-center gap-2 px-5 py-2.5 bg-emerald-600 hover:bg-emerald-700 rounded-lg text-white transition-all"
              >
                <Plus className="w-4 h-4" />
                Create New
              </button>
              <button
                onClick={() => setShowTemplatePicker(true)}
                className="flex items-center gap-2 px-5 py-2.5 bg-gray-800 hover:bg-gray-700 rounded-lg text-gray-300 transition-all border border-gray-700"
              >
                <FileText className="w-4 h-4" />
                Use Template
              </button>
            </div>
          </div>
        ) : (
          <div className="space-y-3">
            {displayedSets.map((ps) => {
              const editable = canEdit(ps, CURRENT_USER);
              return (
                <motion.div
                  key={ps.id}
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  className="bg-gray-900 border border-gray-800 rounded-xl p-5 hover:border-gray-700 transition-all"
                >
                  <div className="flex items-start justify-between">
                    <div className="flex items-start gap-4 flex-1 min-w-0">
                      <span className="text-2xl shrink-0">
                        {SERVICE_ICONS[ps.aws_service] || "📋"}
                      </span>
                      <div className="min-w-0 flex-1">
                        <h3 className="text-lg font-semibold text-white truncate">
                          {ps.name}
                        </h3>
                        <p className="text-sm text-gray-400 mt-1 line-clamp-2">
                          {ps.description}
                        </p>

                        {/* Service + Actions badges */}
                        <div className="flex flex-wrap items-center gap-2 mt-3">
                          <span className="text-xs bg-gray-800 text-emerald-400 px-2 py-0.5 rounded-full font-medium">
                            {ps.aws_service}
                          </span>
                          {ps.actions.slice(0, 4).map((a) => (
                            <span
                              key={a}
                              className="text-xs bg-gray-800/60 text-gray-300 px-2 py-0.5 rounded-full border border-gray-700/50"
                              title={a}
                            >
                              {a.split(":").pop()}
                            </span>
                          ))}
                          {ps.actions.length > 4 && (
                            <span className="text-xs text-gray-500">
                              +{ps.actions.length - 4} more
                            </span>
                          )}
                        </div>

                        {/* Resource ARN */}
                        {ps.resource_arn && (
                          <div className="flex items-center gap-1.5 mt-2">
                            <span className="text-[10px] text-gray-500 uppercase tracking-wider font-mono">
                              ARN:
                            </span>
                            <code className="text-[11px] text-gray-400 font-mono bg-gray-800/40 px-1.5 py-0.5 rounded truncate block max-w-md">
                              {ps.resource_arn}
                            </code>
                          </div>
                        )}

                        {/* Meta */}
                        <div className="flex items-center gap-3 mt-2">
                          <div className="flex items-center gap-1 text-xs text-gray-500">
                            <User className="w-3 h-3" />
                            {ps.created_by}
                          </div>
                          <span className="text-xs text-gray-600">·</span>
                          <span className="text-xs text-gray-500">
                            Updated:{" "}
                            {new Date(ps.updated_at).toLocaleDateString()}
                          </span>
                        </div>
                      </div>
                    </div>

                    {/* Actions (ownership-aware) */}
                    <div className="flex items-center gap-2 shrink-0 ml-4">
                      <button
                        onClick={() => openEdit(ps)}
                        disabled={!editable}
                        className={`p-2 rounded-lg transition-all ${
                          editable
                            ? "text-gray-500 hover:text-emerald-400 hover:bg-gray-800"
                            : "text-gray-700 cursor-not-allowed"
                        }`}
                        title={
                          editable ? "Edit" : "View only — not your permission set"
                        }
                      >
                        {editable ? (
                          <Edit3 className="w-4 h-4" />
                        ) : (
                          <Ban className="w-4 h-4" />
                        )}
                      </button>
                      <button
                        onClick={() => deletePermissionSet(ps.id)}
                        disabled={!editable}
                        className={`p-2 rounded-lg transition-all ${
                          editable
                            ? "text-gray-500 hover:text-red-400 hover:bg-gray-800"
                            : "text-gray-700 cursor-not-allowed"
                        }`}
                        title={
                          editable
                            ? "Delete"
                            : "View only — not your permission set"
                        }
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                      {/* Creator badge */}
                      {ps.created_by === CURRENT_USER && (
                        <span className="text-[10px] bg-blue-900/40 text-blue-400 px-1.5 py-0.5 rounded-full flex items-center gap-1">
                          <ShieldCheck className="w-2.5 h-2.5" /> Owner
                        </span>
                      )}
                      {isAdmin(CURRENT_USER) && ps.created_by !== CURRENT_USER && (
                        <span className="text-[10px] bg-purple-900/40 text-purple-400 px-1.5 py-0.5 rounded-full flex items-center gap-1">
                          <ShieldCheck className="w-2.5 h-2.5" /> Admin
                        </span>
                      )}
                    </div>
                  </div>
                </motion.div>
              );
            })}
          </div>
        )}
      </div>

      {/* ================ CREATE / EDIT MODAL ================ */}
      {(showCreate || editingPs) && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
          <motion.div
            initial={{ opacity: 0, scale: 0.95 }}
            animate={{ opacity: 1, scale: 1 }}
            className="bg-gray-900 border border-gray-800 rounded-2xl p-6 w-full max-w-2xl mx-4 max-h-[90vh] overflow-y-auto"
          >
            {/* Modal header */}
            <div className="flex items-center justify-between mb-6">
              <h2 className="text-xl font-semibold text-white">
                {editingPs ? "Edit Permission Set" : "Create Permission Set"}
              </h2>
              <button
                onClick={() => {
                  setShowCreate(false);
                  setEditingPs(null);
                  setShowDedupPopup(false);
                }}
                className="text-gray-500 hover:text-white"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="space-y-5">
              {/* Name */}
              <div>
                <label className="block text-sm font-medium text-gray-400 mb-1">
                  Name
                </label>
                <input
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  placeholder="e.g. S3 Read-Only Access"
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                />
              </div>

              {/* Description */}
              <div>
                <label className="block text-sm font-medium text-gray-400 mb-1">
                  Description
                </label>
                <textarea
                  value={form.description}
                  onChange={(e) =>
                    setForm({ ...form, description: e.target.value })
                  }
                  placeholder="Describe what this permission set allows..."
                  rows={2}
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-emerald-500"
                />
              </div>

              {/* AWS Service */}
              <div>
                <label className="block text-sm font-medium text-gray-400 mb-1">
                  AWS Service
                </label>
                <select
                  value={form.aws_service}
                  onChange={(e) => {
                    setForm({
                      ...form,
                      aws_service: e.target.value,
                      actions: [],
                    });
                  }}
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-white focus:outline-none focus:ring-2 focus:ring-emerald-500"
                >
                  {AWS_SERVICES.map((s) => (
                    <option key={s} value={s}>
                      {SERVICE_ICONS[s] || "📋"} {s}
                    </option>
                  ))}
                </select>
              </div>

              {/* Action Catalog (checkboxes) */}
              <div>
                <div className="flex items-center justify-between mb-1">
                  <label className="block text-sm font-medium text-gray-400">
                    Actions
                  </label>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() =>
                        setForm((prev) => ({
                          ...prev,
                          actions: availableActions.map((a) => a.action),
                        }))
                      }
                      className="text-xs text-emerald-400 hover:text-emerald-300"
                    >
                      Select All
                    </button>
                    <span className="text-xs text-gray-600">·</span>
                    <button
                      type="button"
                      onClick={() =>
                        setForm((prev) => ({ ...prev, actions: [] }))
                      }
                      className="text-xs text-gray-500 hover:text-gray-300"
                    >
                      Clear
                    </button>
                  </div>
                </div>
                {actionsLoading ? (
                  <div className="text-sm text-gray-500 py-2">
                    Loading actions for {form.aws_service}...
                  </div>
                ) : availableActions.length === 0 ? (
                  <div className="text-sm text-gray-500 py-2 italic">
                    No actions loaded for {form.aws_service}. Try selecting a
                    different service or check the API.
                  </div>
                ) : (
                  <div className="bg-gray-800/50 border border-gray-700 rounded-lg p-3 max-h-48 overflow-y-auto space-y-1.5">
                    {availableActions.map((aa) => {
                      const checked = form.actions.includes(aa.action);
                      return (
                        <label
                          key={aa.action}
                          className={`flex items-start gap-2.5 p-1.5 rounded-md cursor-pointer transition-colors ${
                            checked
                              ? "bg-emerald-900/20 border border-emerald-700/30"
                              : "hover:bg-gray-800 border border-transparent"
                          }`}
                        >
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={() => toggleAction(aa.action)}
                            className="mt-0.5 accent-emerald-500"
                          />
                          <div className="flex-1 min-w-0">
                            <span className="text-sm text-gray-200 font-mono">
                              {aa.action}
                            </span>
                            {aa.description && (
                              <p className="text-xs text-gray-500 mt-0.5 line-clamp-1">
                                {aa.description}
                              </p>
                            )}
                          </div>
                        </label>
                      );
                    })}
                  </div>
                )}
                {/* Action count badge */}
                <div className="flex justify-end mt-1">
                  <span className="text-xs text-gray-500">
                    {form.actions.length} action
                    {form.actions.length !== 1 ? "s" : ""} selected
                  </span>
                </div>
              </div>

              {/* Resource ARN */}
              <div>
                <label className="block text-sm font-medium text-gray-400 mb-1">
                  Resource ARN
                </label>
                <input
                  value={form.resource_arn}
                  onChange={(e) =>
                    setForm({ ...form, resource_arn: e.target.value })
                  }
                  placeholder="arn:aws:s3:::my-bucket/*"
                  list="arn-suggestions"
                  className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-white placeholder-gray-500 font-mono text-sm focus:outline-none focus:ring-2 focus:ring-emerald-500"
                />
                <datalist id="arn-suggestions">
                  {resourceArns.map((arn) => (
                    <option key={arn} value={arn} />
                  ))}
                </datalist>
              </div>
            </div>

            {/* Footer buttons */}
            <div className="flex gap-3 justify-end mt-6 pt-4 border-t border-gray-800">
              <button
                onClick={() => {
                  setShowCreate(false);
                  setEditingPs(null);
                  setShowDedupPopup(false);
                }}
                className="px-5 py-2.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={editingPs ? updatePermissionSet : createPermissionSet}
                disabled={!form.name.trim()}
                className={`flex items-center gap-2 px-5 py-2.5 rounded-lg transition-all ${
                  form.name.trim()
                    ? "bg-emerald-600 hover:bg-emerald-700 text-white"
                    : "bg-gray-800 text-gray-500 cursor-not-allowed"
                }`}
              >
                <Check className="w-4 h-4" />
                {editingPs ? "Update" : "Create"}
              </button>
            </div>
          </motion.div>
        </div>
      )}

      {/* ================ DEDUP POPUP ================ */}
      <AnimatePresence>
        {showDedupPopup && (
          <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60">
            <motion.div
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.95 }}
              className="bg-gray-900 border border-amber-600/40 rounded-2xl p-6 w-full max-w-lg mx-4"
            >
              <div className="flex items-center gap-3 mb-4">
                <AlertTriangle className="w-6 h-6 text-amber-400 shrink-0" />
                <h2 className="text-lg font-semibold text-white">
                  Duplicate Permission Set Detected
                </h2>
              </div>
              <p className="text-sm text-gray-400 mb-4">
                The following permission set(s) already have similar actions for{" "}
                <span className="text-emerald-400 font-medium">
                  {form.aws_service}
                </span>
                :
              </p>
              <div className="space-y-2 mb-5 max-h-40 overflow-y-auto">
                {duplicateSets.map((dup) => (
                  <div
                    key={dup.id}
                    className="bg-gray-800/60 border border-gray-700 rounded-lg p-3"
                  >
                    <p className="text-sm font-medium text-white">{dup.name}</p>
                    <p className="text-xs text-gray-400 mt-0.5">
                      by {dup.created_by}
                    </p>
                    <div className="flex flex-wrap gap-1 mt-1.5">
                      {dup.actions.slice(0, 3).map((a) => (
                        <span
                          key={a}
                          className="text-[10px] bg-gray-700 text-gray-300 px-1.5 py-0.5 rounded"
                        >
                          {a.split(":").pop()}
                        </span>
                      ))}
                      {dup.actions.length > 3 && (
                        <span className="text-[10px] text-gray-500">
                          +{dup.actions.length - 3}
                        </span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
              <div className="flex gap-3 justify-end">
                <button
                  onClick={() => setShowDedupPopup(false)}
                  className="px-5 py-2.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 transition-colors"
                >
                  Cancel
                </button>
                <button
                  onClick={handleDedupProceed}
                  className="flex items-center gap-2 px-5 py-2.5 rounded-lg bg-amber-600 hover:bg-amber-700 text-white transition-all"
                >
                  <Copy className="w-4 h-4" />
                  Create Anyway
                </button>
              </div>
            </motion.div>
          </div>
        )}
      </AnimatePresence>

      {/* ================ TEMPLATE PICKER MODAL ================ */}
      <AnimatePresence>
        {showTemplatePicker && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
            <motion.div
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.95 }}
              className="bg-gray-900 border border-gray-800 rounded-2xl p-6 w-full max-w-2xl mx-4 max-h-[80vh] overflow-y-auto"
            >
              <div className="flex items-center justify-between mb-6">
                <div className="flex items-center gap-3">
                  <FileText className="w-6 h-6 text-emerald-400" />
                  <h2 className="text-xl font-semibold text-white">
                    Predefined Templates
                  </h2>
                </div>
                <button
                  onClick={() => setShowTemplatePicker(false)}
                  className="text-gray-500 hover:text-white"
                >
                  <X className="w-5 h-5" />
                </button>
              </div>
              <p className="text-sm text-gray-400 mb-4">
                Select a template to pre-fill a permission set. You can customize
                it before saving.
              </p>
              <div className="space-y-2">
                {templates.map((tmpl, idx) => (
                  <motion.div
                    key={idx}
                    initial={{ opacity: 0, x: -10 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ delay: idx * 0.05 }}
                    onClick={() => {
                      applyTemplate(tmpl);
                      openCreate();
                    }}
                    className="bg-gray-800/60 border border-gray-700 rounded-xl p-4 cursor-pointer hover:border-emerald-600/40 hover:bg-gray-800 transition-all"
                  >
                    <div className="flex items-start gap-3">
                      <span className="text-xl">
                        {SERVICE_ICONS[tmpl.aws_service] || "📋"}
                      </span>
                      <div className="flex-1 min-w-0">
                        <h3 className="text-sm font-semibold text-white">
                          {tmpl.name}
                        </h3>
                        <p className="text-xs text-gray-400 mt-0.5">
                          {tmpl.description}
                        </p>
                        <div className="flex flex-wrap gap-1 mt-2">
                          <span className="text-[10px] bg-gray-700 text-emerald-400 px-1.5 py-0.5 rounded">
                            {tmpl.aws_service}
                          </span>
                          {tmpl.actions.slice(0, 3).map((a) => (
                            <span
                              key={a}
                              className="text-[10px] bg-gray-700/50 text-gray-300 px-1.5 py-0.5 rounded"
                            >
                              {a.split(":").pop()}
                            </span>
                          ))}
                          {tmpl.actions.length > 3 && (
                            <span className="text-[10px] text-gray-500">
                              +{tmpl.actions.length - 3}
                            </span>
                          )}
                        </div>
                        <code className="text-[10px] text-gray-500 font-mono mt-1 block truncate">
                          {tmpl.resource_arn}
                        </code>
                      </div>
                    </div>
                  </motion.div>
                ))}
              </div>
            </motion.div>
          </div>
        )}
      </AnimatePresence>
    </div>
  );
}