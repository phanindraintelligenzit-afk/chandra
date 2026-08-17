export type KraInput = {
  code: string;
  description: string;
};

export type CustomKraInput = {
  name: string;
  description: string;
};

export type AgentObservationsRequest = {
  region: string;
  kras: KraInput[];
  selected_kras?: string[];
  custom_kras?: (string | CustomKraInput)[];
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
  evidence?: Record<string, any>;
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
  // If NEXT_PUBLIC_API_URL is explicitly set (e.g., on AWS/staging), always
  // use it directly — even in dev mode — so requests reach the real FastAPI
  // rather than proxying through Next.js to localhost:6001.
  const explicitUrl = process.env.NEXT_PUBLIC_API_URL;
  if (explicitUrl && explicitUrl !== "http://localhost:6001" && explicitUrl !== "http://127.0.0.1:6001") {
    return explicitUrl;
  }
  // Local development: route through the Next.js dev proxy so cookies/CORS
  // are handled seamlessly without needing a running nginx.
  if (isBrowserDev()) return DEV_PROXY_PREFIX;
  return explicitUrl ?? DEFAULT_API_URL;
}

export function getApiUrl(path: string): string {
  const base = getApiBase();
  const normalized = path.startsWith("/") ? path : `/${path}`;
  return `${base}${normalized}`;
}

/**
 * Poll /jobs/status/{jobId} every `intervalMs` until the job is done.
 * Works for any async job submitted to the backend (observations, detectors, cloudwatch).
 */
async function pollJobStatus<T>(
  jobId: string,
  extractResult: (jobResult: unknown) => T,
  intervalMs = 3000,
  maxWaitMs = 1_800_000,
  signal?: AbortSignal
): Promise<T> {
  const deadline = Date.now() + maxWaitMs;
  while (Date.now() < deadline) {
    if (signal?.aborted) {
      const err = new Error("Request cancelled");
      err.name = "AbortError";
      throw err;
    }
    const status = await request<Record<string, unknown>>(`/jobs/status/${jobId}`, { signal }, 30_000);
    if (status.status === "completed") {
      return extractResult(status.result);
    }
    if (status.status === "failed") {
      throw new Error(String(status.error || "Background job failed"));
    }
    if (status.status === "not_found") {
      throw new Error(`Job ${jobId} not found on backend`);
    }
    // Still pending/running — wait then poll again
    await new Promise<void>((resolve) => setTimeout(resolve, intervalMs));
  }
  throw new Error(`Job ${jobId} timed out after ${maxWaitMs / 1000}s`);
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
      cache: "no-store",
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
    console.log("🚀 SUBMIT /getAgentObservations job - region:", payload.region);
  }

  if (activeObservationsRequest) {
    return activeObservationsRequest;
  }

  activeObservationsRequest = (async () => {
    try {
      // Step 1: Submit job — new backend returns job_id immediately (202)
      // Old backend returns the full result directly (200) — handle both
      const jobResp = await request<Record<string, unknown>>(
        "/getAgentObservations",
        { method: "POST", body: JSON.stringify(payload), signal: options.signal },
        30_000
      );

      // Backward-compat: if backend returned result directly (old format)
      if (!jobResp.job_id) {
        const output = (jobResp.output as Record<string, unknown>) ?? jobResp;
        return normalizeAgentObservation(output);
      }

      if (typeof window !== "undefined") {
        console.log("✅ Job submitted:", jobResp.job_id, "polling...");
      }

      // Step 2: Poll until complete (new async format)
      const result = await pollJobStatus(
        jobResp.job_id as string,
        (raw) => {
          const data = raw as Record<string, unknown>;
          const output = (data?.output as Record<string, unknown>) ?? data;
          return normalizeAgentObservation(output);
        },
        3000,
        1_800_000,
        options.signal
      );

      if (typeof window !== "undefined") {
        console.log("✅ OBSERVATIONS COMPLETE", result);
      }
      return result;
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

let activeCwMetricsRequests: Record<string, Promise<CloudWatchMetricsOutput> | undefined> = {};

export async function fetchCloudWatchMetrics(
  region: string = process.env.NEXT_PUBLIC_AWS_REGION || "us-east-1",
  hoursLookback: number = 6,
  period: number = 1200,
  options: { signal?: AbortSignal } = {}
): Promise<CloudWatchMetricsOutput> {
  const cacheKey = `${region}-${hoursLookback}-${period}`;
  if (activeCwMetricsRequests[cacheKey]) {
    return activeCwMetricsRequests[cacheKey]!;
  }

  const promise = (async () => {
    try {
      // Submit job — new backend returns job_id (202), old returns result directly (200)
      const jobResp = await request<Record<string, unknown>>("/getCloudWatchMetrics", {
        method: "POST",
        body: JSON.stringify({ region, last_hours: hoursLookback, period })
      }, 30_000);

      // Backward-compat: old backend returned result directly
      if (!jobResp.job_id) {
        return ((jobResp as any).output ?? jobResp) as CloudWatchMetricsOutput;
      }

      // New async format — poll for result
      const output = await pollJobStatus(
        jobResp.job_id as string,
        (raw) => {
          const data = raw as Record<string, unknown>;
          return ((data as any).output ?? data) as CloudWatchMetricsOutput;
        },
        3000,
        900_000
      );
      return output;
    } catch (e: any) {
      delete activeCwMetricsRequests[cacheKey];
      throw e;
    }
  })();

  activeCwMetricsRequests[cacheKey] = promise;
  try {
    const result = await promise;
    delete activeCwMetricsRequests[cacheKey];
    return result;
  } catch (error) {
    delete activeCwMetricsRequests[cacheKey];
    throw error;
  }
}

export async function fetchAWSRegions(options: { signal?: AbortSignal } = {}): Promise<string[]> {
  try {
    const res = await request<{ regions: string[] }>("/aws/regions", options);
    return res.regions || [process.env.NEXT_PUBLIC_AWS_REGION || "us-east-1"];
  } catch (error) {
    console.error(`Failed to fetch regions, falling back to ${process.env.NEXT_PUBLIC_AWS_REGION || "us-east-1"}`, error);
    return [process.env.NEXT_PUBLIC_AWS_REGION || "us-east-1"];
  }
}

let activeDetectorIssuesRequest: Promise<DetectorIssuesOutput> | null = null;

export async function fetchDetectorIssues(
  options: { signal?: AbortSignal } = {}
): Promise<DetectorIssuesOutput> {
  if (activeDetectorIssuesRequest) {
    return activeDetectorIssuesRequest;
  }

  const promise = (async () => {
    try {
      // Submit job — new backend returns job_id (202), old returns result directly (200)
      const jobResp = await request<Record<string, unknown>>(
        `/getDetectorIssues?t=${Date.now()}`,
        { method: "GET", cache: "no-store" },
        30_000
      );

      // Backward-compat: old backend returned result directly
      if (!jobResp.job_id) {
        return ((jobResp as any).output ?? jobResp) as DetectorIssuesOutput;
      }

      // New async format — poll for result
      const output = await pollJobStatus(
        jobResp.job_id as string,
        (raw) => {
          const data = raw as Record<string, unknown>;
          return ((data?.output ?? data) as DetectorIssuesOutput);
        },
        3000,
        900_000
      );
      return output;
    } finally {
      activeDetectorIssuesRequest = null;
    }
  })();

  activeDetectorIssuesRequest = promise;
  return promise;
}
export async function fetchPredefinedKraIssues(
  selectedKras: string[],
  options: { signal?: AbortSignal } = {}
): Promise<DetectorIssuesOutput> {
  const jobResp = await request<Record<string, unknown>>(
    `/getPredefinedKraIssues?t=${Date.now()}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ selected_kras: selectedKras }),
      signal: options.signal
    },
    30_000
  );

  if (!jobResp.job_id) {
    return ((jobResp as any).output ?? jobResp) as DetectorIssuesOutput;
  }

  const output = await pollJobStatus(
    jobResp.job_id as string,
    (raw) => {
      const data = raw as Record<string, unknown>;
      return ((data?.output ?? data) as DetectorIssuesOutput);
    },
    3000,
    900_000
  );
  return output;
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
    console.log("🚀 SUBMIT /analyzeActions job", payload.actions.length, "actions");
  }

  // Submit job — new backend returns job_id (202), old returns result directly
  const jobResp = await request<Record<string, unknown>>("/analyzeActions", {
    method: "POST",
    body: JSON.stringify(payload),
    signal: options.signal
  }, 30_000);

  // Backward-compat: old backend returned result directly
  if (!jobResp.job_id) {
    const direct = jobResp as unknown as AnalyzerPipelineResponse;
    if (!direct?.output) throw new Error("Analyzer response did not include output");
    return direct.output;
  }

  // New async format — poll for result
  const result = await pollJobStatus(
    jobResp.job_id as string,
    (raw) => {
      const data = raw as AnalyzerPipelineResponse;
      if (!data?.output) throw new Error("Analyzer response did not include output");
      return data.output;
    },
    3000,
    300_000,
    options.signal
  );

  return result;
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
    }, 8_000);
    return response?.logs ?? [];
  } catch (e: any) {
    // AbortError means the 8 s discovery poll timed out (backend is busy
    // running a job). Swallow it silently — the next poll cycle (5 s)
    // will try again once the backend is free.
    if (e?.name === "AbortError") return [];
    console.error("Failed to fetch background digital worker jobs:", e);
    return [];
  }
}

export type OrchestrateRequest = {
  action: {
    actionName: string;
    actionDescription: string;
    steps?: string[];
    service?: string;
    kraCode?: string;
    priorityLevel?: string;
    detectorId?: string;
    resourceArn?: string;
    region?: string;
    isAwsTask?: boolean;
    action_type?: string;
  };
  sandbox_path?: string;
  reference_folder?: string;
  thread_id?: string;
  answers?: string[];
  command_timeout?: number;
  jiraUrl?: string;
  max_iterations?: number;
  aws_permissions?: string[];
};

export type OrchestratorResponse = {
  statusCode: number;
  status: string;
  exception?: string | null;
  thread_id: string;
  sandbox_path?: string;
  iterations_used?: number;
  iterations?: any[];
  execution_results?: any[];
  summary?: string;
  questions?: string[];
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
  sandbox_path?: string;
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

// ── Digital Worker · Human Approval Center ─────────────────────────────────────
// Wires the Next.js console to the omnichannel Digital Worker workflow
// (POST /webhooks/{source}, POST /requests) and its human approval gate
// (POST /requests/{job_id}/approve). Discovery is via GET /requests — the
// approval center polls it, so no job_id needs to be known in advance.

export type DigitalWorkerStatus =
  | "pending"
  | "running"
  | "awaiting_approval"
  | "completed"
  | "failed"
  | "stopped"
  | "skipped";

/** One row in the approval-center list (GET /requests). */
export type DigitalWorkerRequestSummary = {
  job_id: string;
  status: DigitalWorkerStatus;
  progress: number;
  message: string;
  source: string | null;
  title: string | null;
  external_id: string | null;
  category: string | null;
  platform: string | null;
  priority: string | null;
  risk_level: string | null;
  risk_score: number | null;
  decision_mode: string | null;
  reason: string | null;
  requires_approval: boolean;
  approved_by_human?: boolean;
  workflow_status?: string | null;
  submitted_at: number | null;
  action?: string | null;
  kraData?: { kraCode: string } | null;
  started_at: number | null;
  completed_at: number | null;
};

export type DigitalWorkerListResponse = {
  status: string;
  count: number;
  counts: Record<string, number>;
  requests: DigitalWorkerRequestSummary[];
};

export type ResolutionStep = {
  order: number;
  action: string;
  detail?: string;
  command?: string | null;
  expected_outcome?: string | null;
};

export type ApprovalRequestDetail = {
  request_id: string;
  external_id?: string | null;
  source: string;
  title: string;
  description?: string;
  requester?: string | null;
  category?: string;
  platform?: string;
  priority?: string;
  services?: string[];
  root_cause?: {
    summary?: string;
    probable_causes?: string[];
    evidence?: string[];
    confidence?: number;
    generated_by?: string;
  };
  plan?: {
    generated_by?: string;
    steps?: ResolutionStep[];
    rollback_steps?: ResolutionStep[];
    detector_id?: string | null;
    automation_available?: boolean;
    notes?: string;
  };
  risk?: {
    level?: string;
    score?: number;
    factors?: string[];
    reversible?: boolean;
    requires_approval?: boolean;
  };
  decision_mode?: string;
  reason?: string;
  resume_url?: string;
};

export type DigitalWorkerRequestDetail = {
  status: string;
  request: DigitalWorkerRequestSummary;
  detail?: {
    status?: string;
    approval_request?: ApprovalRequestDetail;
    output?: Record<string, unknown>;
  } | null;
  error?: string | null;
};

export type ApprovalDecisionInput = {
  approved: boolean;
  approver?: string;
  comment?: string;
};

export type SubmitRequestInput = {
  source?: string;
  payload: Record<string, unknown>;
  dry_run?: boolean;
};

export type SubmitRequestResponse = {
  job_id: string;
  status: string;
  message: string;
  poll_url: string;
};

export type DigitalWorkerSettings = {
  max_iterations: number;
  command_timeout: number;
};

export async function getDigitalWorkerSettings(): Promise<DigitalWorkerSettings> {
  const url = getApiUrl("/settings/digital-worker");
  const res = await fetch(url);
  if (!res.ok) throw new Error("Failed to fetch digital worker settings");
  return res.json();
}

export async function updateDigitalWorkerSettings(settings: DigitalWorkerSettings): Promise<void> {
  const url = getApiUrl("/settings/digital-worker");
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  });
  if (!res.ok) throw new Error("Failed to update digital worker settings");
}

/** List Digital Worker requests, optionally filtered by status. */
export async function listDigitalWorkerRequests(
  status?: DigitalWorkerStatus,
  options: { signal?: AbortSignal } = {}
): Promise<DigitalWorkerListResponse> {
  const qs = status ? `?status=${encodeURIComponent(status)}` : "";
  return request<DigitalWorkerListResponse>(`/requests${qs}`, {
    method: "GET",
    signal: options.signal
  // 8 s is enough for a simple in-memory read; keeps the discovery poll
  // responsive even when a long-running execution holds the job-store lock.
  }, 8_000);
}

/** Full detail for one Digital Worker request (approval payload + result). */
export async function getDigitalWorkerRequest(
  jobId: string,
  options: { signal?: AbortSignal } = {}
): Promise<DigitalWorkerRequestDetail> {
  return request<DigitalWorkerRequestDetail>(`/requests/${encodeURIComponent(jobId)}`, {
    method: "GET",
    signal: options.signal
  }, 30_000);
}

/** Approve (approved=true) or reject (approved=false) a paused request. */
export async function submitDigitalWorkerApproval(
  jobId: string,
  decision: ApprovalDecisionInput,
  options: { signal?: AbortSignal } = {}
): Promise<SubmitRequestResponse> {
  return request<SubmitRequestResponse>(`/requests/${encodeURIComponent(jobId)}/approve`, {
    method: "POST",
    body: JSON.stringify(decision),
    signal: options.signal
  }, 30_000);
}

/** Submit a cloud operations request via the first-party REST channel. */
export async function submitDigitalWorkerRequest(
  input: SubmitRequestInput,
  options: { signal?: AbortSignal } = {}
): Promise<SubmitRequestResponse> {
  return request<SubmitRequestResponse>("/requests", {
    method: "POST",
    body: JSON.stringify({ source: "rest_api", dry_run: true, ...input }),
    signal: options.signal
  }, 30_000);
}

// ── Custom KRA persistence (customKras.json on backend) ────────────────────────
export type StoredCustomKra = {
  name: string;
  description: string;
  selected?: boolean;
};

export type CustomKrasFileResponse = {
  status: string;
  count: number;
  kras: StoredCustomKra[];
};

export type CustomKrasSaveResponse = {
  status: string;
  count: number;
  message: string;
};

/**
 * Fetch all custom KRAs persisted in customKras.json on the backend.
 * Returns an empty list if the file is missing or the request fails.
 */
export async function fetchCustomKras(
  options: { signal?: AbortSignal } = {}
): Promise<StoredCustomKra[]> {
  try {
    const response = await request<CustomKrasFileResponse>("/customKras", {
      method: "GET",
      signal: options.signal
    });
    return Array.isArray(response?.kras) ? response.kras : [];
  } catch (error) {
    console.error("Failed to fetch customKras.json:", error);
    return [];
  }
}

/**
 * Replace the contents of customKras.json on the backend with the supplied list.
 * The list is sent as-is; the server is responsible for dedup/validation.
 */
export async function saveCustomKras(
  kras: StoredCustomKra[],
  options: { signal?: AbortSignal } = {}
): Promise<CustomKrasSaveResponse> {
  return request<CustomKrasSaveResponse>("/customKras", {
    method: "PUT",
    body: JSON.stringify({ kras }),
    signal: options.signal
  });
}

// ============================================================================
// AWS Tasks & Permissions Data Models & API Services
// ============================================================================

export interface PermissionSet {
  id: string;
  name: string;
  description: string;
  actions: string[];
  resource_arns: string[];
  aws_service?: string;
  resource_arn?: string;
  is_predefined?: boolean;
  is_preset?: boolean;
  ownership?: string;
  version?: number | string;
  created_by?: string;
  created_at?: string;
  updated_at?: string;
}

export interface PermissionAction {
  service: string;
  action: string;
  description?: string;
}

export interface ResourceArnSuggestion {
  service: string;
  arn_pattern: string;
  description?: string;
}

export interface AwsTask {
  id: string;
  name: string;
  description: string;
  category: string;
  default_permissions?: string[];
  parameters?: Record<string, any>;
  required_kras?: string[];
  is_preset?: boolean;
  is_predefined?: boolean;
  ownership?: string;
  version?: number;
  selected?: boolean;
}

export type FetchAwsTasksResponse = {
  status: string;
  count: number;
  tasks: AwsTask[];
};

export async function fetchAwsTasks(
  options: { signal?: AbortSignal } = {}
): Promise<AwsTask[]> {
  try {
    const response = await request<FetchAwsTasksResponse>("/aws-tasks", {
      method: "GET",
      signal: options.signal
    });
    return Array.isArray(response?.tasks) ? response.tasks : [];
  } catch (error) {
    console.error("Failed to fetch AWS Tasks:", error);
    return [];
  }
}

export async function saveAwsTasks(
  tasks: AwsTask[],
  options: { signal?: AbortSignal } = {}
): Promise<any> {
  return request("/aws-tasks", {
    method: "PUT",
    body: JSON.stringify({ tasks }),
    signal: options.signal
  });
}

export type FetchPermissionSetsResponse = {
  status: string;
  count: number;
  permissions: PermissionSet[];
};

export async function fetchPermissionSets(
  options: { signal?: AbortSignal } = {}
): Promise<PermissionSet[]> {
  try {
    const response = await request<FetchPermissionSetsResponse>("/api/permission-sets", {
      method: "GET",
      signal: options.signal
    });
    return Array.isArray(response?.permissions) ? response.permissions : [];
  } catch (error) {
    console.error("Failed to fetch Permission Sets:", error);
    return [];
  }
}

export async function savePermissionSets(
  permissions: PermissionSet[],
  options: { signal?: AbortSignal } = {}
): Promise<any> {
  return request("/api/permission-sets", {
    method: "PUT",
    body: JSON.stringify({ permissions }),
    signal: options.signal
  });
}

export async function fetchAwsActions(
  service?: string,
  options: { signal?: AbortSignal } = {}
): Promise<{ actions: string[]; service?: string }> {
  try {
    const query = service ? `?aws_service=${encodeURIComponent(service)}` : "";
    const response = await request<{ actions: string[]; service?: string }>(`/api/permission-sets/actions${query}`, {
      method: "GET",
      signal: options.signal
    });
    return response;
  } catch (error) {
    console.error("Failed to fetch AWS Actions:", error);
    return { actions: [] };
  }
}

export async function fetchResourceArns(
  options: { signal?: AbortSignal } = {}
): Promise<{ resource_arns: string[] }> {
  try {
    const response = await request<{ resource_arns: string[] }>("/api/permission-sets/resource-arns", {
      method: "GET",
      signal: options.signal
    });
    return response;
  } catch (error) {
    console.error("Failed to fetch Resource ARNs:", error);
    return { resource_arns: [] };
  }
}
