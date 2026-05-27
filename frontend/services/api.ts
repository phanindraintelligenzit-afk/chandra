export type KraInput = {
  code: string;
  description: string;
};

export type AgentObservationsRequest = {
  region: string;
  kras: KraInput[];
  selected_kras?: string[];
  custom_kras?: string[];
  maturity_level?: string;
  deployment?: {
    role: string;
    permissions: string[];
    agent_name: string;
    employee_id: string;
  };
};

export type KraStatus = {
  kra_code: string;
  status: string;
  achievement: string;
  note: string;
};

export type LiveIssue = {
  issue: string;
  priorityLevel: string;
  service: string;
  region: string;
  resourceId: string;
};

export type CostSnapshotItem = {
  service: string;
  daily_avg_usd: number;
  change_24h_pct: number;
  note: string;
};

export type ActionItem = {
  actionName: string;
  actionDescription: string;
  service: string;
  kraCode: string;
  priorityLevel: string;
  steps: string[];
};

export type AgentObservation = {
  health: string;
  kra_status: KraStatus[];
  issues: LiveIssue[];
  observations: string[];
  cost_snapshot: CostSnapshotItem[];
  security_posture: string[];
  compliance_summary: string;
  actions: ActionItem[];
};

export type AgentObservationsResponse = {
  statusCode: number;
  status: string;
  exception?: string | null;
  output: AgentObservation;
};

export type CostRegion = {
  RegionTotal: number;
  Services: Record<string, number>;
};

export type CostDailyBreakdown = {
  Date: string;
  TotalDailyCost: number;
  Regions: Record<string, CostRegion>;
};

export type CostMetricsOutput = {
  LookbackPeriod: { Start: string; End: string };
  Granularity: string;
  DailyBreakdown: CostDailyBreakdown[];
};

export type CostMetricsResponse = {
  status: string;
  output: CostMetricsOutput;
};

export type CopilotChatRequest = {
  sessionId: string;
  message: string;
};

export type CopilotChatMessage = {
  role: "system" | "supervisor" | "chandra";
  text: string;
  meta?: string;
};

export type CopilotChatResponse = {
  status?: string;
  output?: unknown;
  response?: string;
  reply?: string;
  message?: string;
  answer?: string;
};

const DEFAULT_TIMEOUT_MS = 120_000;
const DEV_PROXY_PREFIX = "/api/backend";
const DEFAULT_API_URL = "http://184.72.96.181:6002";

function isBrowserDev(): boolean {
  return typeof window !== "undefined" && process.env.NODE_ENV === "development";
}

function getApiBase(): string {
  if (isBrowserDev()) return DEV_PROXY_PREFIX;
  return process.env.NEXT_PUBLIC_API_URL ?? DEFAULT_API_URL;
}

export function getApiUrl(path: string): string {
  const base = getApiBase();
  const normalized = path.startsWith("/") ? path : `/${path}`;
  return `${base}${normalized}`;
}

class HttpError extends Error {
  readonly status: number;
  readonly body: string;
  constructor(status: number, body: string) {
    super(`HTTP ${status}`);
    this.status = status;
    this.body = body;
  }
}

async function request<T>(path: string, init: RequestInit = {}, timeoutMs = DEFAULT_TIMEOUT_MS): Promise<T> {
  const controller = new AbortController();
  const signal = init.signal ?? controller.signal;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(getApiUrl(path), {
      ...init,
      signal,
      headers: {
        Accept: "application/json",
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...(init.headers ?? {})
      }
    });
    if (!response.ok) {
      const body = await response.text().catch(() => "");
      throw new HttpError(response.status, body);
    }
    const text = await response.text();
    if (!text) {
      throw new Error("Empty response body");
    }
    try {
      return JSON.parse(text) as T;
    } catch {
      throw new Error("Malformed JSON response");
    }
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("Request timed out before the backend responded");
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function stringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item ?? "").trim()).filter(Boolean);
}

function normalizeKraStatus(value: unknown): KraStatus[] {
  if (!Array.isArray(value)) return [];
  return value.map((entry, index) => {
    const row = asRecord(entry);
    return {
      kra_code: String(row.kra_code ?? row.kraCode ?? row.code ?? `KRA-${String(index + 1).padStart(2, "0")}`),
      status: String(row.status ?? "UNKNOWN"),
      achievement: String(row.achievement ?? row.summary ?? row.description ?? ""),
      note: String(row.note ?? row.reason ?? row.detail ?? "")
    };
  });
}

function normalizeIssues(value: unknown): LiveIssue[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((entry) => {
      if (typeof entry === "string") {
        return {
          issue: entry.trim(),
          priorityLevel: "",
          service: "",
          region: "",
          resourceId: ""
        };
      }
      const row = asRecord(entry);
      const issueText = String(row.issue ?? row.description ?? row.summary ?? row.title ?? "").trim();
      if (!issueText) return null;
      return {
        issue: issueText,
        priorityLevel: String(row.priorityLevel ?? row.priority ?? row.severity ?? "").trim(),
        service: String(row.service ?? row.Service ?? "").trim(),
        region: String(row.region ?? row.Region ?? "").trim(),
        resourceId: String(row.resourceId ?? row.resource_id ?? row.resource ?? "").trim()
      };
    })
    .filter((entry): entry is LiveIssue => Boolean(entry));
}

function normalizeCostSnapshot(value: unknown): CostSnapshotItem[] {
  if (!Array.isArray(value)) return [];
  return value.map((entry) => {
    const row = asRecord(entry);
    return {
      service: String(row.service ?? row.Service ?? "Unknown service"),
      daily_avg_usd: Number(row.daily_avg_usd ?? row.dailyAverageUsd ?? row.daily_avg ?? row.cost ?? 0),
      change_24h_pct: Number(row.change_24h_pct ?? row.change24hPct ?? row.change_pct ?? row.delta ?? 0),
      note: String(row.note ?? row.description ?? "")
    };
  });
}

function normalizeActions(value: unknown): ActionItem[] {
  if (!Array.isArray(value)) return [];
  return value.map((entry) => {
    const row = asRecord(entry);
    const steps = Array.isArray(row.steps) ? row.steps.map((step) => String(step ?? "").trim()).filter(Boolean) : [];
    return {
      actionName: String(row.actionName ?? row.action_name ?? row.name ?? "Recommended action"),
      actionDescription: String(row.actionDescription ?? row.action_description ?? row.description ?? row.detail ?? ""),
      service: String(row.service ?? row.Service ?? "AWS"),
      kraCode: String(row.kraCode ?? row.kra_code ?? row.code ?? "").trim(),
      priorityLevel: String(row.priorityLevel ?? row.priority ?? row.severity ?? "").trim(),
      steps
    };
  });
}

function normalizeAgentObservation(output: unknown): AgentObservation {
  const record = asRecord(output);
  return {
    health: String(record.health ?? record.status ?? "Unknown"),
    kra_status: normalizeKraStatus(record.kra_status),
    issues: normalizeIssues(record.issues),
    observations: stringArray(record.observations),
    cost_snapshot: normalizeCostSnapshot(record.cost_snapshot),
    security_posture: stringArray(record.security_posture),
    compliance_summary: String(record.compliance_summary ?? ""),
    actions: normalizeActions(record.actions)
  };
}

function normalizeCostMetrics(payload: unknown): CostMetricsOutput {
  const record = asRecord(payload);
  const output = asRecord(record.output ?? record);
  const daily = Array.isArray(output.DailyBreakdown) ? output.DailyBreakdown : [];
  return {
    LookbackPeriod: (output.LookbackPeriod as CostMetricsOutput["LookbackPeriod"]) ?? { Start: "", End: "" },
    Granularity: String(output.Granularity ?? ""),
    DailyBreakdown: daily as CostDailyBreakdown[]
  };
}

export async function fetchAgentObservations(
  payload: AgentObservationsRequest,
  options: { signal?: AbortSignal } = {}
): Promise<AgentObservation> {
  const response = await request<AgentObservationsResponse>("/getAgentObservations", {
    method: "POST",
    body: JSON.stringify(payload),
    signal: options.signal
  });

  if (typeof window !== "undefined") {
    // eslint-disable-next-line no-console
    console.log("LIVE OBSERVABILITY RESPONSE", response);
  }

  const statusCode = response?.statusCode ?? 0;
  if (statusCode && statusCode !== 200) {
    throw new Error(`Backend returned statusCode ${statusCode}`);
  }

  const output = response?.output;
  if (!output || typeof output !== "object") {
    throw new Error("Operational intelligence response did not include output payload");
  }

  return normalizeAgentObservation(output);
}

export async function fetchCostMetrics(options: { signal?: AbortSignal } = {}): Promise<CostMetricsOutput> {
  const data = await request<CostMetricsResponse | CostMetricsOutput>("/getCostMetrics", {
    method: "GET",
    signal: options.signal
  });
  const normalized = normalizeCostMetrics(data);
  if (!Array.isArray(normalized.DailyBreakdown)) {
    throw new Error("Cost metrics response did not include a daily breakdown");
  }
  return normalized;
}

export async function sendCopilotMessage(
  payload: CopilotChatRequest,
  options: { signal?: AbortSignal } = {}
): Promise<CopilotChatMessage> {
  const data = await request<CopilotChatResponse>("/copilot/chat", {
    method: "POST",
    body: JSON.stringify(payload),
    signal: options.signal
  });
  const output = asRecord(data.output);
  const text = String(output.text ?? output.response ?? output.reply ?? data.response ?? data.reply ?? data.answer ?? data.message ?? "");
  if (!text.trim()) {
    throw new Error("Copilot response did not include a message");
  }
  return {
    role: "chandra",
    text,
    meta: String(output.meta ?? output.trace ?? "live copilot response")
  };
}

export type OperationsStreamHandlers = {
  onEvent?: (event: unknown) => void;
  onError?: (error: Error) => void;
  onClose?: () => void;
};

export function subscribeToOperationsStream(_handlers: OperationsStreamHandlers): () => void {
  return () => {};
}
