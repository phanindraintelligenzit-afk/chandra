"use client";

import React, { useState, useEffect, useCallback } from "react";

// ── Types ─────────────────────────────────────────────────────────────────────
type StepStatus = "pending" | "running" | "waiting_approval" | "approved" | "rejected" | "completed" | "failed";

interface WorkerStep {
  id: number;
  name: string;
  icon: string;
  description: string;
  detail?: string;
  status: StepStatus;
  duration?: number; // ms to simulate
}

interface WorkerResult {
  resourcesFound: number;
  monthlySavings: number;
  executedFixes: number;
  validationPassed: boolean;
  complianceScore: number;
  reportUrl: string;
}

// ── Demo step configuration ───────────────────────────────────────────────────
const INITIAL_STEPS: WorkerStep[] = [
  {
    id: 1,
    name: "DETECT",
    icon: "🔍",
    description: "Scanning for idle & over-provisioned resources",
    detail: "Found 3 idle EC2 instances (t3.large), 2 unattached RDS volumes, 1 unused Elastic IP",
    status: "pending",
    duration: 1800,
  },
  {
    id: 2,
    name: "ANALYZE",
    icon: "🧠",
    description: "Calculating potential savings",
    detail: "Estimated monthly savings: $1,240 · Annual: $14,880 · Risk level: Low",
    status: "pending",
    duration: 2200,
  },
  {
    id: 3,
    name: "PLAN",
    icon: "📋",
    description: "Generating remediation plan",
    detail: "Stop 3 EC2 → Save $420/mo · Delete 2 EBS → Save $180/mo · Release Elastic IP → Save $640/mo",
    status: "pending",
    duration: 1500,
  },
  {
    id: 4,
    name: "APPROVAL",
    icon: "✋",
    description: "Requesting human approval",
    detail: "Remediation plan ready. Approve to execute autonomous cleanup.",
    status: "pending",
    duration: 0, // waits for human
  },
  {
    id: 5,
    name: "EXECUTE",
    icon: "⚡",
    description: "Executing approved remediation",
    detail: "Stopping instances · Detaching volumes · Releasing IPs · All operations logged",
    status: "pending",
    duration: 3000,
  },
  {
    id: 6,
    name: "VALIDATE",
    icon: "✅",
    description: "Verifying successful execution",
    detail: "All 6 resources confirmed cleaned · Cost dashboard updated · No service disruptions detected",
    status: "pending",
    duration: 1600,
  },
  {
    id: 7,
    name: "REPORT",
    icon: "📊",
    description: "Generating compliance report",
    detail: "Savings report generated · Audit trail logged · Compliance score: 98% · Notification sent to team",
    status: "pending",
    duration: 1200,
  },
];

// ── Status color + labels ─────────────────────────────────────────────────────
function getStatusStyle(status: StepStatus): {
  color: string;
  bg: string;
  border: string;
  label: string;
} {
  switch (status) {
    case "running":
      return { color: "#60A5FA", bg: "rgba(96,165,250,0.1)", border: "rgba(96,165,250,0.3)", label: "Running…" };
    case "waiting_approval":
      return { color: "#F59E0B", bg: "rgba(245,158,11,0.1)", border: "rgba(245,158,11,0.3)", label: "Awaiting Approval" };
    case "approved":
      return { color: "#10B981", bg: "rgba(16,185,129,0.1)", border: "rgba(16,185,129,0.3)", label: "Approved ✓" };
    case "rejected":
      return { color: "#EF4444", bg: "rgba(239,68,68,0.1)", border: "rgba(239,68,68,0.3)", label: "Rejected ✗" };
    case "completed":
      return { color: "#10B981", bg: "rgba(16,185,129,0.1)", border: "rgba(16,185,129,0.3)", label: "Completed ✓" };
    case "failed":
      return { color: "#EF4444", bg: "rgba(239,68,68,0.1)", border: "rgba(239,68,68,0.3)", label: "Failed" };
    default:
      return { color: "#475569", bg: "rgba(71,85,105,0.05)", border: "rgba(71,85,105,0.15)", label: "Pending" };
  }
}

// ── Step indicator dot ────────────────────────────────────────────────────────
function StepDot({ status }: { status: StepStatus }) {
  return (
    <div className="worker-step-dot-wrap">
      {status === "running" && <div className="worker-pulse-ring" />}
      <div
        className="worker-step-dot"
        style={{
          backgroundColor:
            status === "completed" || status === "approved"
              ? "#10B981"
              : status === "running" || status === "waiting_approval"
              ? "#F59E0B"
              : status === "rejected" || status === "failed"
              ? "#EF4444"
              : "rgba(71,85,105,0.3)",
        }}
      />
    </div>
  );
}

// ── Main Component ────────────────────────────────────────────────────────────
export default function CostOptimizationWorker() {
  const [steps, setSteps] = useState<WorkerStep[]>(INITIAL_STEPS);
  const [running, setRunning] = useState(false);
  const [finished, setFinished] = useState(false);
  const [result, setResult] = useState<WorkerResult | null>(null);
  const [currentStepId, setCurrentStepId] = useState<number | null>(null);
  const [elapsedMs, setElapsedMs] = useState(0);
  const [startTime, setStartTime] = useState<number | null>(null);

  // Elapsed timer
  useEffect(() => {
    if (!running || !startTime) return;
    const interval = setInterval(() => {
      setElapsedMs(Date.now() - startTime);
    }, 100);
    return () => clearInterval(interval);
  }, [running, startTime]);

  const updateStep = useCallback((id: number, patch: Partial<WorkerStep>) => {
    setSteps((prev) => prev.map((s) => (s.id === id ? { ...s, ...patch } : s)));
  }, []);

  const runWorker = useCallback(async () => {
    // Reset
    setSteps(INITIAL_STEPS);
    setRunning(true);
    setFinished(false);
    setResult(null);
    setStartTime(Date.now());
    setElapsedMs(0);

    const wait = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

    try {
      // Steps 1–3: fully autonomous
      for (const step of [1, 2, 3]) {
        setCurrentStepId(step);
        updateStep(step, { status: "running" });
        await wait(INITIAL_STEPS[step - 1].duration!);
        updateStep(step, { status: "completed" });
      }

      // Step 4: wait for human approval
      setCurrentStepId(4);
      updateStep(4, { status: "waiting_approval" });
      // Approval is handled by button click — we pause here via a promise
      await new Promise<void>((resolve) => {
        (window as any).__dpi_approval_resolve = resolve;
      });

      // Steps 5–7: execute after approval
      for (const step of [5, 6, 7]) {
        setCurrentStepId(step);
        updateStep(step, { status: "running" });
        await wait(INITIAL_STEPS[step - 1].duration!);
        updateStep(step, { status: "completed" });
      }

      // Final result
      setResult({
        resourcesFound: 6,
        monthlySavings: 1240,
        executedFixes: 6,
        validationPassed: true,
        complianceScore: 98,
        reportUrl: "#",
      });
    } finally {
      setRunning(false);
      setFinished(true);
      setCurrentStepId(null);
    }
  }, [updateStep]);

  function handleApprove() {
    updateStep(4, { status: "approved" });
    if ((window as any).__dpi_approval_resolve) {
      (window as any).__dpi_approval_resolve();
      delete (window as any).__dpi_approval_resolve;
    }
  }

  function handleReject() {
    updateStep(4, { status: "rejected" });
    if ((window as any).__dpi_approval_resolve) {
      delete (window as any).__dpi_approval_resolve;
    }
    setRunning(false);
    setFinished(true);
    setCurrentStepId(null);
  }

  const allCompleted = steps.every((s) => s.status === "completed" || s.status === "approved");
  const isWaitingApproval = steps[3].status === "waiting_approval";

  return (
    <>
      <style>{`
        .worker-card {
          background: linear-gradient(135deg, #0F1117 0%, #111827 100%);
          border: 1px solid rgba(255,255,255,0.08);
          border-radius: 20px;
          padding: 28px;
          font-family: 'Inter', 'Segoe UI', sans-serif;
          color: #F1F5F9;
        }
        .worker-header { margin-bottom: 24px; }
        .worker-header h2 {
          font-size: 1.25rem; font-weight: 700; margin: 0 0 4px;
          background: linear-gradient(90deg, #34D399, #60A5FA);
          -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        }
        .worker-header p { color: #64748B; font-size: 0.82rem; margin: 0; }
        .worker-top-bar {
          display: flex; align-items: center; justify-content: space-between;
          margin-bottom: 16px; gap: 12px; flex-wrap: wrap;
        }
        .worker-timer {
          font-size: 0.82rem; color: #94A3B8;
          font-variant-numeric: tabular-nums;
        }
        .worker-run-btn {
          padding: 10px 24px; border-radius: 12px;
          background: linear-gradient(135deg, #10B981, #059669);
          color: white; border: none; font-weight: 700;
          font-size: 0.9rem; cursor: pointer; transition: all 0.2s;
          display: flex; align-items: center; gap: 8px;
        }
        .worker-run-btn:hover { transform: translateY(-1px); box-shadow: 0 4px 20px rgba(16,185,129,0.4); }
        .worker-run-btn:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }
        .worker-steps { display: flex; flex-direction: column; gap: 0; }
        .worker-step-row {
          display: grid;
          grid-template-columns: 28px 44px 1fr;
          gap: 0 12px;
          position: relative;
        }
        .worker-connector {
          grid-column: 1;
          width: 2px;
          background: rgba(255,255,255,0.06);
          margin: 0 auto;
          height: 100%;
          min-height: 16px;
        }
        .worker-connector.active { background: #10B981; }
        .worker-step-dot-wrap {
          grid-column: 1;
          display: flex; align-items: center; justify-content: center;
          position: relative;
          padding: 2px 0;
        }
        .worker-step-dot {
          width: 12px; height: 12px; border-radius: 50%;
          z-index: 1; transition: background-color 0.4s;
        }
        .worker-pulse-ring {
          position: absolute;
          width: 22px; height: 22px; border-radius: 50%;
          border: 2px solid #F59E0B;
          animation: worker-pulse 1.2s ease-in-out infinite;
        }
        @keyframes worker-pulse {
          0% { transform: scale(0.8); opacity: 1; }
          100% { transform: scale(1.6); opacity: 0; }
        }
        .worker-step-icon {
          grid-column: 2;
          width: 40px; height: 40px; border-radius: 10px;
          display: flex; align-items: center; justify-content: center;
          font-size: 1.1rem;
          transition: all 0.3s;
          align-self: start;
          margin-top: 2px;
        }
        .worker-step-content {
          grid-column: 3;
          padding: 4px 0 20px;
        }
        .worker-step-name {
          font-size: 0.72rem; font-weight: 800;
          text-transform: uppercase; letter-spacing: 0.1em;
          color: #475569; margin-bottom: 2px;
        }
        .worker-step-desc { font-size: 0.88rem; font-weight: 600; color: #E2E8F0; }
        .worker-step-detail {
          font-size: 0.78rem; color: #64748B; margin-top: 4px; line-height: 1.5;
        }
        .worker-step-status-pill {
          display: inline-flex; align-items: center; gap: 4px;
          padding: 2px 10px; border-radius: 999px;
          font-size: 0.7rem; font-weight: 700; margin-top: 6px;
          border: 1px solid;
        }
        .worker-approval-btns {
          display: flex; gap: 10px; margin-top: 10px;
        }
        .worker-approve-btn {
          padding: 8px 20px; border-radius: 10px;
          background: linear-gradient(135deg, #10B981, #059669);
          color: white; border: none; font-weight: 700;
          font-size: 0.82rem; cursor: pointer; transition: all 0.2s;
        }
        .worker-approve-btn:hover { opacity: 0.85; }
        .worker-reject-btn {
          padding: 8px 20px; border-radius: 10px;
          background: rgba(239,68,68,0.1); color: #EF4444;
          border: 1px solid rgba(239,68,68,0.3); font-weight: 700;
          font-size: 0.82rem; cursor: pointer; transition: all 0.2s;
        }
        .worker-reject-btn:hover { background: rgba(239,68,68,0.2); }
        .worker-result-card {
          margin-top: 24px; padding: 20px;
          background: rgba(16,185,129,0.05);
          border: 1px solid rgba(16,185,129,0.2);
          border-radius: 14px;
        }
        .worker-result-title {
          font-size: 0.85rem; font-weight: 700; color: #10B981;
          text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 14px;
        }
        .worker-result-grid {
          display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px;
        }
        @media (max-width: 480px) { .worker-result-grid { grid-template-columns: repeat(2, 1fr); } }
        .worker-result-stat {
          background: rgba(255,255,255,0.04); border-radius: 10px;
          padding: 12px; text-align: center;
        }
        .worker-result-val {
          font-size: 1.5rem; font-weight: 800; color: #10B981;
        }
        .worker-result-lbl {
          font-size: 0.7rem; color: #64748B;
          text-transform: uppercase; letter-spacing: 0.06em; margin-top: 2px;
        }
        .worker-spinner {
          display: inline-block; width: 12px; height: 12px;
          border: 2px solid rgba(255,255,255,0.3);
          border-top-color: white; border-radius: 50%;
          animation: worker-spin 0.7s linear infinite;
        }
        @keyframes worker-spin { to { transform: rotate(360deg); } }
      `}</style>

      <div className="worker-card">
        {/* Header */}
        <div className="worker-header">
          <h2>⚡ AWS Cost Optimization Worker</h2>
          <p>Autonomous end-to-end worker · Detect → Plan → Approve → Execute → Validate → Report</p>
        </div>

        {/* Top bar */}
        <div className="worker-top-bar">
          <div className="worker-timer">
            {running || finished ? (
              <>⏱ {(elapsedMs / 1000).toFixed(1)}s elapsed</>
            ) : (
              "Ready to run"
            )}
          </div>
          <button
            className="worker-run-btn"
            disabled={running}
            onClick={runWorker}
          >
            {running ? (
              <>
                <span className="worker-spinner" />
                Running…
              </>
            ) : finished ? (
              "▶ Run Again"
            ) : (
              "▶ Run Worker"
            )}
          </button>
        </div>

        {/* Steps */}
        <div className="worker-steps">
          {steps.map((step, idx) => {
            const style = getStatusStyle(step.status);
            const isActive = step.status !== "pending";
            const connectorActive =
              idx < steps.length - 1 &&
              (steps[idx + 1].status !== "pending");

            return (
              <div key={step.id} className="worker-step-row">
                {/* Dot column */}
                <div style={{ gridColumn: 1, display: "flex", flexDirection: "column", alignItems: "center" }}>
                  <StepDot status={step.status} />
                  {idx < steps.length - 1 && (
                    <div
                      className={`worker-connector ${connectorActive ? "active" : ""}`}
                      style={{ flex: 1, minHeight: 16 }}
                    />
                  )}
                </div>

                {/* Icon */}
                <div
                  className="worker-step-icon"
                  style={{
                    background: isActive ? style.bg : "rgba(255,255,255,0.04)",
                    border: `1px solid ${isActive ? style.border : "rgba(255,255,255,0.06)"}`,
                  }}
                >
                  {step.icon}
                </div>

                {/* Content */}
                <div className="worker-step-content">
                  <div className="worker-step-name">Step {step.id} · {step.name}</div>
                  <div className="worker-step-desc">{step.description}</div>

                  {isActive && step.detail && (
                    <div className="worker-step-detail">{step.detail}</div>
                  )}

                  {isActive && (
                    <div
                      className="worker-step-status-pill"
                      style={{
                        color: style.color,
                        borderColor: style.border,
                        backgroundColor: style.bg,
                      }}
                    >
                      {step.status === "running" && <span className="worker-spinner" style={{ borderTopColor: style.color }} />}
                      {style.label}
                    </div>
                  )}

                  {/* Approval buttons */}
                  {step.id === 4 && step.status === "waiting_approval" && (
                    <div className="worker-approval-btns">
                      <button className="worker-approve-btn" onClick={handleApprove}>
                        ✅ Approve &amp; Execute
                      </button>
                      <button className="worker-reject-btn" onClick={handleReject}>
                        ✗ Reject
                      </button>
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>

        {/* Result card */}
        {result && (
          <div className="worker-result-card">
            <div className="worker-result-title">✅ Execution Complete — Business Outcome</div>
            <div className="worker-result-grid">
              <div className="worker-result-stat">
                <div className="worker-result-val">{result.resourcesFound}</div>
                <div className="worker-result-lbl">Resources Cleaned</div>
              </div>
              <div className="worker-result-stat">
                <div className="worker-result-val">${result.monthlySavings.toLocaleString()}</div>
                <div className="worker-result-lbl">Monthly Savings</div>
              </div>
              <div className="worker-result-stat">
                <div className="worker-result-val">${(result.monthlySavings * 12).toLocaleString()}</div>
                <div className="worker-result-lbl">Annual Savings</div>
              </div>
              <div className="worker-result-stat">
                <div className="worker-result-val">{result.executedFixes}</div>
                <div className="worker-result-lbl">Fixes Executed</div>
              </div>
              <div className="worker-result-stat">
                <div className="worker-result-val">{result.complianceScore}%</div>
                <div className="worker-result-lbl">Compliance Score</div>
              </div>
              <div className="worker-result-stat">
                <div className="worker-result-val">✓</div>
                <div className="worker-result-lbl">Validation Passed</div>
              </div>
            </div>
          </div>
        )}
      </div>
    </>
  );
}
