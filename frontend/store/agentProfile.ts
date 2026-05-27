export type AgentGender = "Male" | "Female" | "Neutral / Synthetic AI";

export type AgentAvatar = {
  id: string;
  label: string;
  specialty: string;
  signal: string;
  image: string;
};

export type AgentPermission = {
  id: string;
  label: string;
  desc: string;
};

export const agentAvatars: AgentAvatar[] = [
  {
    id: "manager",
    label: "Operations Lead",
    specialty: "supervised operations",
    signal: "OPS",
    image: "Manager Avatar.png"
  },
  {
    id: "male-operator",
    label: "Cloud Operator",
    specialty: "AWS control plane",
    signal: "CLD",
    image: "Male Avatars.png"
  },
  {
    id: "female-operator",
    label: "Telemetry Analyst",
    specialty: "telemetry synthesis",
    signal: "ANL",
    image: "Female Characters.png"
  },
  {
    id: "long-hair-glasses",
    label: "Governance Copilot",
    specialty: "policy & approvals",
    signal: "GOV",
    image: "Long Hair Woman with Glasses.png"
  },
  {
    id: "short-hair-glasses",
    label: "Cyber Operator",
    specialty: "security posture",
    signal: "SEC",
    image: "Short Hair Man with Glasses.png"
  },
  {
    id: "hijab",
    label: "Compliance Lead",
    specialty: "audit & evidence",
    signal: "AUD",
    image: "Woman with Hijab.png"
  }
];

export const agentGenders: AgentGender[] = ["Male", "Female", "Neutral / Synthetic AI"];

export const existingAgentNames = ["AURA", "NOVA", "ATHENA", "SENTINEL", "ORION"];

export const permissionCatalog: AgentPermission[] = [
  { id: "aws-iam", label: "AWS IAM access", desc: "Identity policy review and least-privilege analysis." },
  { id: "cloudwatch", label: "CloudWatch access", desc: "Metrics, logs, and operational telemetry." },
  { id: "security-hub", label: "Security Hub", desc: "Security findings and posture monitoring." },
  { id: "api-keys", label: "API keys", desc: "Governed API credential usage." },
  { id: "incident-management", label: "Incident management", desc: "Ticket context, incident routing, and triage." },
  { id: "audit-logs", label: "Audit logs", desc: "Evidence capture and compliance trails." },
  { id: "cost-monitoring", label: "Cost monitoring", desc: "Spend visibility and FinOps recommendations." },
  { id: "infra-remediation", label: "Infrastructure remediation", desc: "Guardrailed infrastructure actions." },
  { id: "webhook-access", label: "Webhook access", desc: "Outbound workflow notifications." },
  { id: "read-only-governance", label: "Read-only governance", desc: "Policy checks without write authority." },
  { id: "approval-escalation", label: "Approval escalation access", desc: "Supervisor review and escalation routing." }
];

export function normalizeAgentName(name: string) {
  return name.replace(/\s+/g, " ").trim();
}

export function isDuplicateAgentName(name: string) {
  return existingAgentNames.includes(normalizeAgentName(name).toUpperCase());
}

export function generateEmployeeId(name: string) {
  const normalized = normalizeAgentName(name);
  if (!normalized) return "REG101";
  const prefix = normalized.replace(/[^a-zA-Z]/g, "").slice(0, 3).toUpperCase().padEnd(3, "X");
  const seed = Array.from(normalized).reduce((total, char, index) => total + char.charCodeAt(0) * (index + 3), 0);
  return `${prefix}${String(100 + (seed % 900)).padStart(3, "0")}`;
}

export function getAvatarById(id: string) {
  return agentAvatars.find((avatar) => avatar.id === id) ?? agentAvatars[0];
}

const basePath = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

export function getAvatarImageSrc(avatar: AgentAvatar) {
  return `${basePath}/avatars/${encodeURI(avatar.image)}`;
}

export function getRoleIconSrc(file: string) {
  return `${basePath}/icons/${file}`;
}
