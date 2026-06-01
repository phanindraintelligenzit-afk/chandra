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
  completedPercentage: number;
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

export type DetectorIssue = {
  kra: string;
  severity: string;
  resource_arn: string;
  resource_type: string;
  region: string;
  title: string;
  recommendation: string;
  detector_id: string;
};

export type DetectorIssuesOutput = Record<string, DetectorIssue[]>;

export type DetectorIssuesResponse = {
  status: string;
  output: DetectorIssuesOutput;
};

export type CloudWatchDatapoint = {
  timestamp: string;
  average: number;
};

export type CloudWatchMetricSeries = {
  metric_name: string;
  dimensions: Record<string, string>;
  datapoints: CloudWatchDatapoint[];
};

export type CloudWatchMetadata = {
  region: string;
  start_time: string;
  end_time: string;
  period_seconds: number;
  total_metrics_found: number;
  total_metrics_fetched: number;
};

/** namespaces: { "AWS/RDS": { "CPUUtilization": [ series... ] } } */
export type CloudWatchNamespaces = Record<string, Record<string, CloudWatchMetricSeries[]>>;

export type CloudWatchMetricsOutput = {
  metadata: CloudWatchMetadata;
  namespaces: CloudWatchNamespaces;
};

export type CloudWatchMetricsResponse = {
  status: string;
  output: CloudWatchMetricsOutput;
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

export type ActionResult = {
  actionName: string;
  severity: string;
  HumanReviewNeeded: boolean;
  JiraIssueKey: string;
  JiraUrl: string;
  priority: string;
};

export type AnalyzerPipelineResponse = {
  statusCode: number;
  status: string;
  exception?: string | null;
  output: ActionResult[];
};

const DEFAULT_TIMEOUT_MS = 60_000;
const ORCHESTRATE_TIMEOUT_MS = 1_800_000; // 30 minutes for long-running orchestrations
const DEV_PROXY_PREFIX = "/api/backend";
const DEFAULT_API_URL = "http://localhost:6001";

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

  // If the caller supplied a signal that is already aborted, fail immediately
  // without touching the network.
  const callerSignal = init.signal instanceof AbortSignal ? init.signal : undefined;
  if (callerSignal?.aborted) {
    const err = new Error("Request timed out before the backend responded");
    err.name = "AbortError";
    throw err;
  }

  // Forward the caller's abort to our internal controller so both the timeout
  // and an external cancel will abort the same fetch.
  let forwardListener: (() => void) | undefined;
  if (callerSignal) {
    forwardListener = () => controller.abort();
    callerSignal.addEventListener("abort", forwardListener, { once: true });
  }

  // Always drive the fetch with our internal controller so the timeout abort
  // (via setTimeout below) is guaranteed to reach the running fetch regardless
  // of whether the caller also supplied a signal.
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(getApiUrl(path), {
      ...init,
      signal: controller.signal,
      headers: {
        Accept: "application/json",
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...(init.headers ?? {})
      }
    });
    const text = await response.text();
    if (!text) {
      throw new Error("Empty response body");
    }
    try {
      const parsed = JSON.parse(text) as T;
      return parsed;
    } catch {
      throw new Error(`Malformed JSON response: ${text.substring(0, 100)}`);
    }
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      const err = new Error("Request timed out before the backend responded");
      err.name = "AbortError";
      throw err;
    }
    throw error;
  } finally {
    clearTimeout(timer);
    if (callerSignal && forwardListener) {
      callerSignal.removeEventListener("abort", forwardListener);
    }
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
      completedPercentage: Number(row.completedPercentage ?? row.completed_percentage ?? row.percentage ?? 0),
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
  const daily = Array.isArray(output.DailyBreakdown)
    ? output.DailyBreakdown.map((d: any) => ({
        Date: String(d.Date ?? ""),
        TotalDailyCost: Number(d.TotalDailyCost ?? 0),
        Regions: (d.Regions ?? {}) as Record<string, CostRegion>
      }))
    : [];
  return {
    LookbackPeriod: (output.LookbackPeriod as CostMetricsOutput["LookbackPeriod"]) ?? { Start: "", End: "" },
    Granularity: String(output.Granularity ?? ""),
    DailyBreakdown: daily
  };
}

let activeObservationsRequest: Promise<AgentObservation> | null = null;

export async function fetchAgentObservations(
  payload: AgentObservationsRequest,
  options: { signal?: AbortSignal } = {}
): Promise<AgentObservation> {
  if (typeof window !== "undefined") {
    console.log("🌐 FETCH AGENT OBSERVATIONS - region:", payload.region, "kras:", payload.selected_kras?.length ?? 0);
  }

  if (activeObservationsRequest) {
    if (typeof window !== "undefined") {
      console.log("🌐 REUSING ACTIVE OBSERVATIONS REQUEST");
    }
    return activeObservationsRequest;
  }

  activeObservationsRequest = (async () => {
    try {
      const response = await request<AgentObservationsResponse>("/getAgentObservations", {
        method: "POST",
        body: JSON.stringify(payload),
        signal: options.signal
      }, 180_000);

      if (typeof window !== "undefined") {
        console.log("🌐 LIVE OBSERVABILITY RESPONSE", response);
      }

      const statusCode = response?.statusCode ?? 0;
      if (statusCode && statusCode !== 200) {
        throw new Error(`Backend returned statusCode ${statusCode}`);
      }

      const output = response?.output;
      if (!output || typeof output !== "object") {
        throw new Error("Operational intelligence response did not include output payload");
      }

      const normalized = normalizeAgentObservation(output);
      if (typeof window !== "undefined") {
        console.log("✅ NORMALIZED OBSERVATIONS", normalized);
      }
      return normalized;
    } finally {
      activeObservationsRequest = null;
    }
  })();

  return activeObservationsRequest;
}

let activeCostMetricsRequests: Record<number, Promise<CostMetricsOutput> | undefined> = {};

export async function fetchCostMetrics(
  daysLookback: number = 7,
  options: { signal?: AbortSignal } = {}
): Promise<CostMetricsOutput> {
  if (activeCostMetricsRequests[daysLookback]) {
    return activeCostMetricsRequests[daysLookback]!;
  }

  activeCostMetricsRequests[daysLookback] = (async () => {
    try {
      const data = await request<CostMetricsResponse | CostMetricsOutput>("/getCostMetrics", {
        method: "POST",
        body: JSON.stringify({ days_lookback: daysLookback, granularity: "DAILY" })
      });
      const normalized = normalizeCostMetrics(data);
      if (!Array.isArray(normalized.DailyBreakdown)) {
        throw new Error("Cost metrics response did not include a daily breakdown");
      }
      return normalized;
    } finally {
      delete activeCostMetricsRequests[daysLookback];
    }
  })();

  return activeCostMetricsRequests[daysLookback]!;
}

let activeCwMetricsRequests: Record<number, Promise<CloudWatchMetricsOutput> | undefined> = {};

export async function fetchCloudWatchMetrics(
  hoursLookback: number = 6,
  options: { signal?: AbortSignal } = {}
): Promise<CloudWatchMetricsOutput> {
  if (activeCwMetricsRequests[hoursLookback]) {
    return activeCwMetricsRequests[hoursLookback]!;
  }

  // Do NOT pass the caller's signal into the IIFE — passing it would mean a
  // StrictMode cleanup-abort of the first call kills the shared dedup promise
  // before the second call can reuse or replace it. The signal is only needed
  // to abort a request that hasn't started deduplication yet (handled above).
  const promise = (async () => {
    try {
      const data = await request<CloudWatchMetricsResponse | CloudWatchMetricsOutput>("/getCloudWatchMetrics", {
        method: "POST",
        body: JSON.stringify({ last_hours: hoursLookback, period: 1200 })
      }, 180_000);
      const output = (data as any).output ?? data;
      return output as CloudWatchMetricsOutput;
    } finally {
      delete activeCwMetricsRequests[hoursLookback];
    }
  })();

  activeCwMetricsRequests[hoursLookback] = promise;
  return promise;
}

let activeDetectorIssuesRequest: Promise<DetectorIssuesOutput> | null = null;

export async function fetchDetectorIssues(
  options: { signal?: AbortSignal } = {}
): Promise<DetectorIssuesOutput> {
  if (activeDetectorIssuesRequest) {
    return activeDetectorIssuesRequest;
  }

  // Do NOT pass the caller's signal into the IIFE for the same reason as
  // fetchCloudWatchMetrics above — a StrictMode cleanup abort of the first
  // effect would kill the shared dedup promise and the second effect run
  // would then find an already-failed (aborted) cached promise.
  // The request runs without an external signal; the 300s internal timeout
  // in request() still applies.
  const promise = (async () => {
    try {
      const data = await request<DetectorIssuesResponse | DetectorIssuesOutput>(`/getDetectorIssues?t=${Date.now()}`, {
        method: "GET",
        cache: "no-store"
      }, 300_000);
      const output = (data as any).output ?? data;
      return output as DetectorIssuesOutput;
    } finally {
      activeDetectorIssuesRequest = null;
    }
  })();

  activeDetectorIssuesRequest = promise;
  return promise;
}

export async function analyzeActions(
  actions: ActionItem[],
  options: { signal?: AbortSignal } = {}
): Promise<ActionResult[]> {
  const payload = {
    projectKey: "DEV",
    actions: actions.map((action) => {
      const item: Record<string, any> = {
        actionName: action.actionName,
        actionDescription: action.actionDescription,
        service: action.service
      };
      if (action.kraCode) item.kraCode = action.kraCode;
      if (action.priorityLevel) item.priorityLevel = action.priorityLevel;
      if (action.steps) item.steps = action.steps;
      return item;
    })
  };

  if (typeof window !== "undefined") {
    console.log("ANALYZE ACTIONS PAYLOAD", payload);
  }

  const response = await request<AnalyzerPipelineResponse>("/analyzeActions", {
    method: "POST",
    body: JSON.stringify(payload),
    signal: options.signal
  });

  if (!response?.output) {
    throw new Error("Analyzer response did not include output");
  }

  return response.output;
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

export interface BackendLog {
  timestamp: number;
  level: string;
  logger: string;
  message: string;
  job_id?: string;
}

export type LogsResponse = {
  logs: BackendLog[];
};

export async function fetchBackendLogs(
  limit: number = 500,
  offset: number = 0,
  options: { signal?: AbortSignal } = {}
): Promise<BackendLog[]> {
  try {
    const response = await request<LogsResponse>(`/logs?limit=${limit}&offset=${offset}`, {
      method: "GET",
      signal: options.signal
    });
    return response?.logs ?? [];
  } catch (error) {
    console.error("Failed to fetch backend logs:", error);
    return [];
  }
}

export type OrchestrateRequest = {
  action: {
    actionName: string;
    actionDescription: string;
    steps?: string[];
  };
  sandbox_path?: string;
  reference_folder?: string;
  thread_id?: string;
  answers?: string[];
  generator_thread_id?: string;
  command_timeout?: number;
  jira_issue_key?: string;
  max_iterations?: number;
};

export type OrchestratorResponse = {
  statusCode: number;
  status: string;
  exception?: string | null;
  thread_id: string;
  output?: unknown;
};

export type OrchestrateJobResponse = {
  job_id: string;
  status: string;
  message: string;
  poll_url: string;
};

export type JobStatusResponse = {
  job_id: string;
  status: string; // "pending", "running", "completed", "failed", "not_found"
  progress: number;
  message: string;
  result?: OrchestratorResponse;
  error?: string;
  started_at?: number;
  completed_at?: number;
};

export async function orchestrateAction(
  payload: OrchestrateRequest,
  options: { signal?: AbortSignal } = {}
): Promise<OrchestrateJobResponse> {
  const response = await request<OrchestrateJobResponse>("/orchestrate", {
    method: "POST",
    body: JSON.stringify(payload),
    signal: options.signal
  }, 30_000); // Shorter timeout for initial request

  return response;
}

export async function getJobStatus(
  jobId: string,
  options: { signal?: AbortSignal } = {}
): Promise<JobStatusResponse> {
  const response = await request<JobStatusResponse>(`/orchestrate/status/${jobId}`, {
    method: "GET",
    signal: options.signal
  }, 30_000);

  return response;
}

export function subscribeToOperationsStream(_handlers: OperationsStreamHandlers): () => void {
  return () => {};
}
