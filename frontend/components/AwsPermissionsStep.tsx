"use client";

import { useState, useEffect, useCallback, useMemo } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Plus, Search, Edit3, Trash2, Check, X, Shield, FileText, User, ShieldCheck, Ban } from "lucide-react";
import { 
  fetchPermissionSets, 
  savePermissionSets, 
  fetchAwsActions,
  fetchResourceArns,
  type PermissionSet 
} from "@/services/api";
import { useOnboarding } from "@/store/OnboardingContext";

const AWS_SERVICES = [
  "S3", "EC2", "VPC", "Lambda", "CloudWatch", "RDS", "IAM", "DynamoDB", 
  "SQS", "SNS", "ECS", "ELB", "CloudFront", "ElastiCache", "APIGateway",
  "KMS", "SecretsManager", "Route53", "StepFunctions", "Athena"
];

const SERVICE_ICONS: Record<string, string> = {
  S3: "🗄️", EC2: "🖥️", VPC: "🌐", Lambda: "⚡", CloudWatch: "📊",
  RDS: "🗃️", IAM: "🔑", DynamoDB: "📊", SQS: "📨", SNS: "🔔",
  ECS: "🐳", ELB: "⚖️", CloudFront: "🌍", ElastiCache: "⚡", APIGateway: "🔌",
  KMS: "🔐", SecretsManager: "🤫", Route53: "🔄", StepFunctions: "⛓️", Athena: "🦉"
};

interface Template {
  name: string;
  description: string;
  aws_service: string;
  actions: string[];
  resource_arn: string;
}

const PREDEFINED_TEMPLATES: Template[] = [
  { name: "S3 Full Access", description: "Full read/write access to S3 buckets and objects", aws_service: "S3", actions: ["s3:*"], resource_arn: "arn:aws:s3:::*" },
  { name: "S3 Read Only", description: "Read-only access to S3 buckets and objects", aws_service: "S3", actions: ["s3:GetObject", "s3:ListBucket", "s3:GetBucketLocation"], resource_arn: "arn:aws:s3:::*" },
  { name: "EC2 Operator", description: "Full EC2 instance lifecycle management", aws_service: "EC2", actions: ["ec2:RunInstances", "ec2:StartInstances", "ec2:StopInstances", "ec2:TerminateInstances", "ec2:DescribeInstances"], resource_arn: "arn:aws:ec2:*:*:instance/*" },
  { name: "VPC Admin", description: "Full VPC and subnet management", aws_service: "VPC", actions: ["ec2:CreateVpc", "ec2:CreateSubnet", "ec2:CreateRouteTable", "ec2:CreateInternetGateway", "ec2:DescribeVpcs", "ec2:DescribeSubnets"], resource_arn: "arn:aws:ec2:*:*:vpc/*" }
];

export default function AwsPermissionsStep({ onNext, onPrev }: { onNext: () => void; onPrev: () => void }) {
  const { agentName, selectedAwsPermissions, toggleAwsPermission, addAwsPermission, removeAwsPermission } = useOnboarding();
  const [permissionSets, setPermissionSets] = useState<PermissionSet[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  
  // Form state
  const [showForm, setShowForm] = useState(false);
  const [editingPs, setEditingPs] = useState<PermissionSet | null>(null);
  const [form, setForm] = useState<Partial<PermissionSet>>({
    name: "", description: "", aws_service: "S3", actions: [], resource_arn: ""
  });
  
  // Available Actions
  // Available Actions & ARNs
  const [availableActions, setAvailableActions] = useState<{action: string; description?: string}[]>([]);
  const [actionsLoading, setActionsLoading] = useState(false);
  const [arnSuggestions, setArnSuggestions] = useState<string[]>([]);
  const [showArnDropdown, setShowArnDropdown] = useState(false);
  const [showTemplatePicker, setShowTemplatePicker] = useState(false);

  const currentUser = agentName || "System";

  const loadData = useCallback(async () => {
    setLoading(true);
    const [data, arns] = await Promise.all([
      fetchPermissionSets(),
      fetchResourceArns()
    ]);
    setPermissionSets(data);
    setArnSuggestions(arns.resource_arns || []);
    setLoading(false);
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  const loadActions = useCallback(async (service: string) => {
    if (!service) return;
    setActionsLoading(true);
    const res = await fetchAwsActions(service);
    setAvailableActions((res.actions || []).map(a => ({ action: a })));
    setActionsLoading(false);
  }, []);

  useEffect(() => {
    if ((showForm || editingPs) && form.aws_service) {
      loadActions(form.aws_service);
    }
  }, [form.aws_service, showForm, editingPs, loadActions]);

  const filteredSets = useMemo(() => {
    const s = search.toLowerCase();
    return permissionSets.filter(ps => 
      ps.name.toLowerCase().includes(s) || 
      ps.description.toLowerCase().includes(s) ||
      ps.aws_service?.toLowerCase().includes(s)
    );
  }, [permissionSets, search]);

  const handleSave = async () => {
    if (!form.name || !form.description || !form.aws_service || !form.actions?.length) return;
    
    // Check dups
    const isDuplicate = permissionSets.some(ps => ps.name.toLowerCase() === form.name?.toLowerCase() && ps.id !== editingPs?.id);
    if (isDuplicate) {
      alert("A permission set with this name already exists.");
      return;
    }

    let updated = [...permissionSets];
    if (editingPs) {
      // Create new version if ownership differs
      if (editingPs.ownership && editingPs.ownership !== currentUser && editingPs.ownership !== "System") {
        const newPs: PermissionSet = {
          ...editingPs, ...form,
          id: `ps_${Date.now()}`,
          version: (typeof editingPs.version === 'number' ? editingPs.version : 1) + 1,
          ownership: currentUser,
          is_preset: false,
          is_predefined: false
        } as PermissionSet;
        updated.push(newPs);
      } else {
        // Edit existing
        updated = updated.map(ps => ps.id === editingPs.id ? { ...ps, ...form } as PermissionSet : ps);
      }
    } else {
      const newPs: PermissionSet = {
        id: `ps_${Date.now()}`,
        name: form.name!,
        description: form.description!,
        aws_service: form.aws_service,
        actions: form.actions || [],
        resource_arns: form.resource_arn ? [form.resource_arn] : [],
        resource_arn: form.resource_arn,
        ownership: currentUser,
        version: 1,
        is_preset: false,
        is_predefined: false
      };
      updated.push(newPs);
    }

    await savePermissionSets(updated);
    setPermissionSets(updated);
    setShowForm(false);
    setEditingPs(null);
  };

  const handleDelete = async (ps: PermissionSet) => {
    if (ps.ownership === "System" || ps.is_predefined) {
      alert("System predefined permissions cannot be deleted.");
      return;
    }
    if (ps.ownership !== currentUser) {
      alert("You can only delete permissions you own.");
      return;
    }
    if (!confirm(`Delete permission set ${ps.name}?`)) return;
    const updated = permissionSets.filter(p => p.id !== ps.id);
    await savePermissionSets(updated);
    setPermissionSets(updated);
    if (selectedAwsPermissions.includes(ps.id)) {
      removeAwsPermission(ps.id);
    }
  };

  const openEdit = (ps: PermissionSet) => {
    setEditingPs(ps);
    setForm({
      name: ps.name,
      description: ps.description,
      aws_service: ps.aws_service || "S3",
      actions: ps.actions || [],
      resource_arn: ps.resource_arn || (ps.resource_arns?.[0] || "")
    });
    setShowForm(true);
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
    setShowForm(true);
    setEditingPs(null);
  };

  const canProceed = true; // optional permissions

  return (
    <motion.div key="aws-permissions" initial={{ opacity: 0, x: 30 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -20 }}>
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-2xl font-semibold uppercase tracking-[0.02em]">
            ASSIGN AWS PERMISSIONS
          </h3>
          <p className="text-muted mt-2">Map exact IAM permission sets this agent needs to fulfill its assigned tasks.</p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => setShowTemplatePicker(true)}
            className="flex items-center gap-2 rounded-2xl border border-white/10 bg-black/40 px-4 py-2.5 text-xs font-semibold uppercase tracking-[0.14em] text-frost hover:bg-white/10 transition"
          >
            <FileText size={14} /> Templates
          </button>
          <button
            onClick={() => { setShowForm(true); setEditingPs(null); setForm({ name: "", description: "", aws_service: "S3", actions: [], resource_arn: "" }); }}
            className="flex items-center gap-2 rounded-2xl bg-emerald-300/10 px-4 py-2.5 text-xs font-semibold uppercase tracking-[0.14em] text-emerald-200 transition hover:bg-emerald-300/20"
          >
            <Plus size={14} /> Create Permission Set
          </button>
        </div>
      </div>

      <div className="mt-6 relative">
        <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="SEARCH PERMISSIONS..."
          className="w-full rounded-2xl border border-white/10 bg-black/30 py-3 pl-9 pr-4 text-sm text-frost outline-none transition placeholder:text-muted focus:border-emerald-300/40 focus:ring-2 focus:ring-emerald-300/15 uppercase"
        />
      </div>

      <div className="mt-4 grid gap-4 md:grid-cols-2 max-h-[400px] overflow-y-auto pr-2 custom-kra-scroll">
        {loading ? (
          <div className="col-span-2 text-center py-12 text-muted text-sm uppercase tracking-[0.1em]">Loading AWS Permissions...</div>
        ) : filteredSets.length === 0 ? (
           <div className="col-span-2 text-center py-12 text-muted text-sm uppercase tracking-[0.1em]">No Permissions Found.</div>
        ) : (
          filteredSets.map((ps) => {
            const isSelected = selectedAwsPermissions.includes(ps.id);
            return (
              <div 
                key={ps.id} 
                className={`flex flex-col justify-between rounded-3xl border p-5 transition ${isSelected ? "border-emerald-300/40 bg-emerald-300/10" : "border-white/10 bg-black/30 hover:border-emerald-300/20"}`}
              >
                <div className="flex items-start gap-4">
                  <div className="flex items-center justify-center mt-1">
                    <input
                      type="checkbox"
                      checked={isSelected}
                      onChange={() => {
                        if (isSelected) removeAwsPermission(ps.id);
                        else addAwsPermission(ps.id);
                      }}
                      className="h-4 w-4 accent-emerald-300 cursor-pointer"
                    />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-xl">{SERVICE_ICONS[ps.aws_service || "S3"] || "📋"}</span>
                      <div className="font-semibold uppercase tracking-[0.04em] text-frost truncate">{ps.name}</div>
                    </div>
                    <div className="mt-2 text-sm text-frost/70 line-clamp-2">{ps.description}</div>
                    
                    <div className="mt-3 flex flex-wrap gap-2">
                      <span className="rounded-full border border-signal/30 bg-signal/10 px-2 py-0.5 text-[0.55rem] uppercase tracking-[0.16em] text-signal">
                        {ps.aws_service}
                      </span>
                      {ps.actions?.slice(0, 3).map(a => (
                        <span key={a} className="rounded-full border border-white/20 bg-white/5 px-2 py-0.5 text-[0.55rem] uppercase tracking-[0.16em] text-frost/80">
                          {a.split(":").pop()}
                        </span>
                      ))}
                      {(ps.actions?.length || 0) > 3 && (
                        <span className="text-xs text-muted">+{ps.actions!.length - 3}</span>
                      )}
                    </div>
                    
                    <div className="mt-3 flex items-center justify-between">
                      <div className="flex gap-2">
                        {ps.version && (
                          <span className="rounded-full border border-blue-400/30 bg-blue-400/10 px-2 py-0.5 text-[0.55rem] uppercase tracking-[0.16em] text-blue-400">
                            V{ps.version}
                          </span>
                        )}
                        <span className="rounded-full border border-white/10 bg-white/5 px-2 py-0.5 text-[0.55rem] uppercase tracking-[0.16em] text-muted">
                          {ps.ownership || "System"}
                        </span>
                      </div>
                      
                      <div className="flex gap-1">
                        <button onClick={(e) => { e.stopPropagation(); openEdit(ps); }} className="p-1.5 text-muted hover:text-emerald-300 transition" title="Edit">
                          <Edit3 size={14} />
                        </button>
                        <button onClick={(e) => { e.stopPropagation(); handleDelete(ps); }} className="p-1.5 text-muted hover:text-red-400 transition" title="Delete">
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
        <button onClick={onNext} disabled={!canProceed} className="ml-auto rounded-2xl bg-signal px-5 py-3 text-sm font-semibold uppercase tracking-[0.14em] text-black transition hover:bg-red-500 disabled:opacity-50">DEPLOY DIGITAL EMPLOYEE</button>
      </div>

      {/* Form Modal */}
      <AnimatePresence>
        {showForm && (
          <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/80 backdrop-blur-sm">
            <motion.div
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.95 }}
              className="w-full max-w-2xl rounded-3xl border border-white/10 bg-black/90 p-6 shadow-2xl max-h-[90vh] overflow-y-auto custom-kra-scroll"
            >
              <div className="mb-6 flex items-center justify-between">
                <h4 className="text-lg font-semibold uppercase tracking-[0.04em] text-frost">
                  {editingPs ? "EDIT PERMISSION SET" : "CREATE PERMISSION SET"}
                </h4>
                <button onClick={() => setShowForm(false)} className="text-muted hover:text-frost">
                  <X size={20} />
                </button>
              </div>

              <div className="space-y-4">
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <label className="mb-1 block text-[0.65rem] uppercase tracking-[0.16em] text-muted">NAME</label>
                    <input
                      value={form.name}
                      onChange={(e) => setForm({ ...form, name: e.target.value })}
                      className="w-full rounded-2xl border border-white/10 bg-black/30 px-4 py-3 text-sm text-frost outline-none transition focus:border-emerald-300/40 focus:ring-2 focus:ring-emerald-300/15"
                      placeholder="e.g. S3 Read Only"
                    />
                  </div>
                  <div>
                    <label className="mb-1 block text-[0.65rem] uppercase tracking-[0.16em] text-muted">AWS SERVICE</label>
                    <select
                      value={form.aws_service}
                      onChange={(e) => setForm({ ...form, aws_service: e.target.value, actions: [] })}
                      className="w-full rounded-2xl border border-white/10 bg-black/30 px-4 py-3 text-sm text-frost outline-none transition focus:border-emerald-300/40 focus:ring-2 focus:ring-emerald-300/15 appearance-none"
                    >
                      {AWS_SERVICES.map(s => <option key={s} value={s} className="bg-gray-900">{s}</option>)}
                    </select>
                  </div>
                </div>
                
                <div>
                  <label className="mb-1 block text-[0.65rem] uppercase tracking-[0.16em] text-muted">DESCRIPTION</label>
                  <textarea
                    value={form.description}
                    onChange={(e) => setForm({ ...form, description: e.target.value })}
                    rows={2}
                    className="w-full resize-none rounded-2xl border border-white/10 bg-black/30 px-4 py-3 text-sm text-frost outline-none transition focus:border-emerald-300/40 focus:ring-2 focus:ring-emerald-300/15"
                    placeholder="Describe permissions..."
                  />
                </div>

                <div className="relative">
                  <label className="mb-1 block text-[0.65rem] uppercase tracking-[0.16em] text-muted">RESOURCE ARN (Optional)</label>
                  <input
                    value={form.resource_arn}
                    onChange={(e) => {
                      setForm({ ...form, resource_arn: e.target.value });
                      setShowArnDropdown(true);
                    }}
                    onFocus={() => setShowArnDropdown(true)}
                    onBlur={() => setTimeout(() => setShowArnDropdown(false), 200)}
                    className="w-full rounded-2xl border border-white/10 bg-black/30 px-4 py-3 text-sm font-mono text-frost outline-none transition focus:border-emerald-300/40 focus:ring-2 focus:ring-emerald-300/15"
                    placeholder="arn:aws:s3:::my-bucket/*"
                  />
                  {showArnDropdown && arnSuggestions.length > 0 && (
                    <div className="absolute z-10 mt-1 w-full max-h-40 overflow-y-auto rounded-xl border border-white/10 bg-gray-900 shadow-xl custom-kra-scroll">
                      {arnSuggestions.filter(a => a.toLowerCase().includes((form.resource_arn || "").toLowerCase())).map(arn => (
                        <div 
                          key={arn}
                          onClick={() => { setForm({ ...form, resource_arn: arn }); setShowArnDropdown(false); }}
                          className="px-4 py-2 text-xs font-mono text-frost cursor-pointer hover:bg-emerald-300/20"
                        >
                          {arn}
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                <div>
                  <div className="mb-1 flex items-center justify-between">
                    <label className="block text-[0.65rem] uppercase tracking-[0.16em] text-muted">ACTIONS</label>
                    <div className="flex gap-2">
                      <button type="button" onClick={() => setForm(p => ({ ...p, actions: availableActions.map(a => a.action) }))} className="text-[0.6rem] uppercase tracking-[0.1em] text-emerald-400 hover:text-emerald-300">Select All</button>
                      <button type="button" onClick={() => setForm(p => ({ ...p, actions: [] }))} className="text-[0.6rem] uppercase tracking-[0.1em] text-muted hover:text-white">Clear</button>
                    </div>
                  </div>
                  
                  {actionsLoading ? (
                    <div className="py-4 text-center text-xs text-muted">Loading actions...</div>
                  ) : availableActions.length === 0 ? (
                    <div className="py-4 text-center text-xs text-muted">No actions available for this service.</div>
                  ) : (
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-2 max-h-40 overflow-y-auto rounded-xl border border-white/10 bg-black/20 p-2 custom-kra-scroll">
                      {availableActions.map(aa => {
                        const checked = form.actions?.includes(aa.action);
                        return (
                          <label key={aa.action} className="flex items-center gap-2 rounded-lg p-1.5 hover:bg-white/5 cursor-pointer">
                            <input
                              type="checkbox"
                              checked={checked || false}
                              onChange={() => {
                                const newActions = checked
                                  ? (form.actions || []).filter(a => a !== aa.action)
                                  : [...(form.actions || []), aa.action];
                                setForm({ ...form, actions: newActions });
                              }}
                              className="accent-emerald-400"
                            />
                            <span className="text-xs font-mono text-frost truncate">{aa.action}</span>
                          </label>
                        );
                      })}
                    </div>
                  )}
                  <div className="mt-1 text-right text-[0.6rem] text-muted">{form.actions?.length || 0} selected</div>
                </div>
              </div>

              <div className="mt-8 flex justify-end gap-3">
                <button onClick={() => setShowForm(false)} className="rounded-2xl border border-white/10 px-5 py-2.5 text-xs uppercase tracking-[0.14em] text-muted hover:bg-white/5">CANCEL</button>
                <button 
                  onClick={handleSave}
                  disabled={!form.name || !form.description || !form.aws_service || !form.actions?.length}
                  className="rounded-2xl bg-emerald-300/10 px-5 py-2.5 text-xs font-semibold uppercase tracking-[0.14em] text-emerald-200 transition hover:bg-emerald-300/20 disabled:opacity-50"
                >
                  SAVE PERMISSIONS
                </button>
              </div>
            </motion.div>
          </div>
        )}
      </AnimatePresence>

      {/* Templates Modal */}
      <AnimatePresence>
        {showTemplatePicker && (
          <div className="fixed inset-0 z-[110] flex items-center justify-center bg-black/80 backdrop-blur-sm">
            <motion.div
              initial={{ opacity: 0, scale: 0.95 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.95 }}
              className="w-full max-w-3xl rounded-3xl border border-white/10 bg-black/90 p-6 shadow-2xl max-h-[90vh] overflow-y-auto custom-kra-scroll"
            >
              <div className="mb-6 flex items-center justify-between">
                <h4 className="text-lg font-semibold uppercase tracking-[0.04em] text-frost">
                  SELECT A TEMPLATE
                </h4>
                <button onClick={() => setShowTemplatePicker(false)} className="text-muted hover:text-frost">
                  <X size={20} />
                </button>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {PREDEFINED_TEMPLATES.map((tmpl) => (
                  <div 
                    key={tmpl.name} 
                    onClick={() => applyTemplate(tmpl)}
                    className="cursor-pointer rounded-2xl border border-white/10 bg-black/30 p-4 transition hover:border-emerald-300/40 hover:bg-emerald-300/10"
                  >
                    <div className="flex items-center gap-2 mb-2">
                      <span className="text-xl">{SERVICE_ICONS[tmpl.aws_service || "S3"] || "📋"}</span>
                      <div className="font-semibold uppercase tracking-[0.04em] text-frost truncate">{tmpl.name}</div>
                    </div>
                    <p className="text-xs text-frost/70 line-clamp-2">{tmpl.description}</p>
                    <div className="mt-3 flex flex-wrap gap-1">
                      {tmpl.actions.slice(0, 3).map(a => (
                        <span key={a} className="rounded-md bg-white/5 px-1.5 py-0.5 text-[0.55rem] uppercase tracking-[0.16em] text-muted">
                          {a.split(":").pop()}
                        </span>
                      ))}
                      {tmpl.actions.length > 3 && (
                        <span className="text-[0.55rem] text-muted">+{tmpl.actions.length - 3}</span>
                      )}
                    </div>
                  </div>
                ))}
              </div>

              <div className="mt-8 flex justify-end">
                <button onClick={() => setShowTemplatePicker(false)} className="rounded-2xl border border-white/10 px-5 py-2.5 text-xs uppercase tracking-[0.14em] text-muted hover:bg-white/5">CLOSE</button>
              </div>
            </motion.div>
          </div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}
