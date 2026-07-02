export type KraDefinition = {
  id: string;
  desc: string;
};

export type CustomKra = {
  name: string;
  description: string;
  selected?: boolean;
};

export type KraOperationalMetric = {
  subtitle: string;
  value: string;
  detail: string;
  tone: string;
  target: string;
  actual: string;
  confidence: number;
  automation: number;
  score: number;
};

export type KraAgentPayload = {
  predefinedKras: string[];
  customKras: CustomKra[];
  selectedKras: string[];
};

export const predefinedKraCatalog: KraDefinition[] = [
  { id: "Infrastructure Monitoring", desc: "Proactively monitor infrastructure and surface anomalies." },
  { id: "Incident Detection", desc: "Detect incidents with high confidence and triage autonomously." },
  { id: "Cost Optimization", desc: "Identify cost anomalies and recommend rightsizing." },
  { id: "Audit & Compliance", desc: "Preserve evidence and enforce policy controls." }
];

export const predefinedKraIds = predefinedKraCatalog.map((kra) => kra.id);

export const predefinedKraMetrics: Record<string, KraOperationalMetric> = {
  "Infrastructure Monitoring": {
    subtitle: "Infrastructure Health",
    value: "72% CPU",
    detail: "Fleet utilization and uptime are stable.",
    tone: "text-emerald-300",
    target: "99.9% uptime",
    actual: "99.98%",
    confidence: 94,
    automation: 76,
    score: 92
  },
  "Incident Detection": {
    subtitle: "Incident Detection",
    value: "5 active alerts",
    detail: "Priority incidents are surfaced in real time.",
    tone: "text-signal",
    target: "P1 detect < 2m",
    actual: "1m 21s",
    confidence: 91,
    automation: 72,
    score: 89
  },
  "Cost Optimization": {
    subtitle: "Cost Optimization",
    value: "$182K saved",
    detail: "FinOps recommendations are active.",
    tone: "text-amber",
    target: "$150K avoided",
    actual: "$182K",
    confidence: 93,
    automation: 81,
    score: 93
  },
  "Audit & Compliance": {
    subtitle: "Audit & Compliance",
    value: "95.4% coverage",
    detail: "Evidence and controls remain aligned.",
    tone: "text-frost",
    target: ">95% coverage",
    actual: "95.4%",
    confidence: 95,
    automation: 84,
    score: 90
  }
};

function hashKraName(name: string) {
  return Array.from(name).reduce((total, char, index) => total + char.charCodeAt(0) * (index + 7), 0);
}

export function normalizeKraName(name: string) {
  return name.replace(/\s+/g, " ").trim();
}

export function normalizeKraDescription(description: string) {
  return description.replace(/\s+/g, " ").trim();
}

/**
 * Normalize a custom KRA entry. Accepts either:
 *  - a string (legacy format) → wraps it as { name, description }
 *  - a { name, description } object
 * Returns null if the entry is invalid.
 */
export function normalizeCustomKra(entry: unknown): CustomKra | null {
  if (typeof entry === "string") {
    const name = normalizeKraName(entry);
    if (!name) return null;
    return { name, description: name };
  }
  if (entry && typeof entry === "object") {
    const row = entry as Record<string, unknown>;
    const name = normalizeKraName(String(row.name ?? row.code ?? ""));
    const description = normalizeKraDescription(String(row.description ?? row.desc ?? ""));
    if (!name) return null;
    return { name, description: description || name };
  }
  return null;
}

/**
 * Build the list of selected KRA identifiers (predefined IDs + custom names).
 * Used for the `selectedKRAs` array in the onboarding context.
 */
export function buildSelectedKras(predefinedKras: string[], customKras: CustomKra[]): string[] {
  return [...predefinedKras, ...customKras.map((k) => k.name)];
}

/**
 * Build the KRA payload sent to the backend.
 * Each entry is { code, description } where:
 *  - code is auto-generated as KRA-XX (predefined first, then custom)
 *  - description is the KRA's free-form goal/objective
 */
export function buildKraAgentPayload(predefinedKras: string[], customKras: CustomKra[]): KraAgentPayload {
  return {
    predefinedKras,
    customKras,
    selectedKras: buildSelectedKras(predefinedKras, customKras)
  };
}

export function generateCustomKraMetric(kra: CustomKra | string): KraOperationalMetric {
  const name = typeof kra === "string" ? kra : kra.name;
  const description = typeof kra === "string" ? kra : kra.description;
  const normalized = normalizeKraName(name);
  const seed = hashKraName(normalized);
  const confidence = 86 + (seed % 11);
  const automation = 58 + (seed % 29);
  const score = Math.round(confidence * 0.55 + automation * 0.35 + 9);
  const throughput = 72 + (seed % 24);

  return {
    subtitle: "Custom Operational Goal",
    value: `${score}% score`,
    detail: `${normalized} — ${description || "Custom KRA with generated operational telemetry."}`,
    tone: seed % 3 === 0 ? "text-amber" : seed % 3 === 1 ? "text-emerald-300" : "text-frost",
    target: `${throughput}% attainment`,
    actual: `${Math.max(68, throughput - 4 + (seed % 9))}%`,
    confidence,
    automation,
    score
  };
}

export function getKraMetric(kra: string | CustomKra) {
  if (typeof kra === "string") {
    return predefinedKraMetrics[kra] ?? generateCustomKraMetric(kra);
  }
  return generateCustomKraMetric(kra);
}