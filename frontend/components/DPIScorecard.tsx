"use client";

import React, { useState, useEffect } from "react";

// ── Types ─────────────────────────────────────────────────────────────────────
interface DPIDimensions {
  productivity: number;
  quality: number;
  execution: number;
  governance: number;
  risk: number;
  validation: number;
  cost: number;
}

interface DPIResult {
  worker_id?: string;
  worker_name?: string;
  worker_role?: string;
  maturity_level?: string;
  dimensions: DPIDimensions;
  weighted_scores: Record<string, number>;
  overall_score: number;
  rating: string;
  rating_color: string;
  rating_icon: string;
  timestamp: string;
  strengths: string[];
  improvement_areas: string[];
  recommendation: string;
}

interface DPIScorecardProps {
  workerName?: string;
  workerRole?: string;
  maturityLevel?: string;
  initialDimensions?: DPIDimensions;
  /** If true, shows an editable form to score any worker in real time */
  interactive?: boolean;
}

// ── Constants ─────────────────────────────────────────────────────────────────
const DIMENSION_META: {
  key: keyof DPIDimensions;
  label: string;
  letter: string;
  description: string;
  color: string;
}[] = [
  {
    key: "productivity",
    label: "Productivity",
    letter: "P",
    description: "Tasks completed vs assigned",
    color: "#6366F1",
  },
  {
    key: "quality",
    label: "Quality",
    letter: "Q",
    description: "Accuracy, no rework needed",
    color: "#8B5CF6",
  },
  {
    key: "execution",
    label: "Execution",
    letter: "E",
    description: "Autonomous completion rate",
    color: "#EC4899",
  },
  {
    key: "governance",
    label: "Governance",
    letter: "G",
    description: "Policy compliance %",
    color: "#14B8A6",
  },
  {
    key: "risk",
    label: "Risk Control",
    letter: "R",
    description: "100 minus incidents caused",
    color: "#F59E0B",
  },
  {
    key: "validation",
    label: "Validation",
    letter: "V",
    description: "Audit pass rate",
    color: "#10B981",
  },
  {
    key: "cost",
    label: "Cost Efficiency",
    letter: "C",
    description: "Cost efficiency score",
    color: "#3B82F6",
  },
];

const DEFAULT_DIMENSIONS: DPIDimensions = {
  productivity: 90,
  quality: 88,
  execution: 82,
  governance: 95,
  risk: 90,
  validation: 87,
  cost: 80,
};

const API_BASE = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000";

// ── Helpers ───────────────────────────────────────────────────────────────────
function getScoreColor(score: number): string {
  if (score >= 90) return "#10B981";
  if (score >= 75) return "#3B82F6";
  if (score >= 60) return "#F59E0B";
  return "#EF4444";
}

function getScoreGrade(score: number): string {
  if (score >= 90) return "A+";
  if (score >= 80) return "A";
  if (score >= 70) return "B";
  if (score >= 60) return "C";
  return "D";
}

// ── Animated Bar ──────────────────────────────────────────────────────────────
function DimensionBar({
  letter,
  label,
  description,
  value,
  color,
  delay,
}: {
  letter: string;
  label: string;
  description: string;
  value: number;
  color: string;
  delay: number;
}) {
  const [animated, setAnimated] = useState(0);

  useEffect(() => {
    const timer = setTimeout(() => {
      setAnimated(value);
    }, delay);
    return () => clearTimeout(timer);
  }, [value, delay]);

  const scoreColor = getScoreColor(value);

  return (
    <div className="dpi-dimension-row">
      <div className="dpi-letter-badge" style={{ backgroundColor: color + "22", borderColor: color + "44" }}>
        <span style={{ color }}>{letter}</span>
      </div>

      <div className="dpi-label-group">
        <span className="dpi-label">{label}</span>
        <span className="dpi-desc">{description}</span>
      </div>

      <div className="dpi-bar-track">
        <div
          className="dpi-bar-fill"
          style={{
            width: `${animated}%`,
            backgroundColor: color,
            transition: `width 1s cubic-bezier(0.4, 0, 0.2, 1) ${delay}ms`,
          }}
        />
      </div>

      <div className="dpi-score-badge" style={{ color: scoreColor }}>
        {value}
        <span className="dpi-score-max">/100</span>
      </div>
    </div>
  );
}

// ── Circular Score Ring ───────────────────────────────────────────────────────
function ScoreRing({
  score,
  color,
  rating,
  icon,
}: {
  score: number;
  color: string;
  rating: string;
  icon: string;
}) {
  const [animatedScore, setAnimatedScore] = useState(0);
  const r = 54;
  const circumference = 2 * Math.PI * r;
  const progress = (animatedScore / 100) * circumference;

  useEffect(() => {
    const timer = setTimeout(() => setAnimatedScore(score), 200);
    return () => clearTimeout(timer);
  }, [score]);

  return (
    <div className="dpi-ring-container">
      <svg width="140" height="140" viewBox="0 0 140 140">
        {/* Background track */}
        <circle cx="70" cy="70" r={r} fill="none" stroke="rgba(255,255,255,0.08)" strokeWidth="10" />
        {/* Progress arc */}
        <circle
          cx="70"
          cy="70"
          r={r}
          fill="none"
          stroke={color}
          strokeWidth="10"
          strokeLinecap="round"
          strokeDasharray={`${progress} ${circumference}`}
          strokeDashoffset={circumference * 0.25}
          style={{ transition: "stroke-dasharray 1.5s cubic-bezier(0.4, 0, 0.2, 1) 0.3s" }}
          transform="rotate(-90 70 70)"
        />
      </svg>
      <div className="dpi-ring-inner">
        <div className="dpi-ring-icon">{icon}</div>
        <div className="dpi-ring-score" style={{ color }}>
          {animatedScore.toFixed(0)}
        </div>
        <div className="dpi-ring-label">Score</div>
      </div>
    </div>
  );
}

// ── Main Component ────────────────────────────────────────────────────────────
export default function DPIScorecard({
  workerName = "Arjun — AWS Cloud Engineer",
  workerRole = "AWS Cloud Engineer",
  maturityLevel = "L3",
  initialDimensions = DEFAULT_DIMENSIONS,
  interactive = false,
}: DPIScorecardProps) {
  const [result, setResult] = useState<DPIResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [dimensions, setDimensions] = useState<DPIDimensions>(initialDimensions);
  const [editMode, setEditMode] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Fetch demo score on mount
  useEffect(() => {
    fetchScore();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function fetchScore() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/dpi/score`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          worker_id: "worker-001",
          worker_name: workerName,
          worker_role: workerRole,
          maturity_level: maturityLevel,
          dimensions,
        }),
      });
      if (!res.ok) throw new Error(`API error: ${res.status}`);
      const data: DPIResult = await res.json();
      setResult(data);
    } catch (err) {
      // Fallback: compute locally for demo when backend not available
      const weights: Record<keyof DPIDimensions, number> = {
        productivity: 0.2,
        quality: 0.15,
        execution: 0.2,
        governance: 0.15,
        risk: 0.1,
        validation: 0.1,
        cost: 0.1,
      };
      let overall = 0;
      const weighted_scores: Record<string, number> = {};
      for (const [k, w] of Object.entries(weights)) {
        const contribution = dimensions[k as keyof DPIDimensions] * w;
        weighted_scores[k] = contribution;
        overall += contribution;
      }
      overall = Math.round(overall * 10) / 10;
      let rating = "Needs Improvement";
      let rating_color = "#EF4444";
      let rating_icon = "⚠️";
      if (overall >= 90) { rating = "Exceptional Digital Worker"; rating_color = "#10B981"; rating_icon = "🏆"; }
      else if (overall >= 75) { rating = "Strong Digital Worker"; rating_color = "#3B82F6"; rating_icon = "⭐"; }
      else if (overall >= 60) { rating = "Developing Digital Worker"; rating_color = "#F59E0B"; rating_icon = "📈"; }

      setResult({
        worker_name: workerName,
        worker_role: workerRole,
        maturity_level: maturityLevel,
        dimensions,
        weighted_scores,
        overall_score: overall,
        rating,
        rating_color,
        rating_icon,
        timestamp: new Date().toISOString(),
        strengths: Object.entries(dimensions)
          .filter(([, v]) => v >= 85)
          .map(([k]) => `${k} (${dimensions[k as keyof DPIDimensions]}/100)`),
        improvement_areas: Object.entries(dimensions)
          .filter(([, v]) => v < 70)
          .map(([k]) => `${k} (${dimensions[k as keyof DPIDimensions]}/100)`),
        recommendation: overall >= 75
          ? "Strong performer. Focus on Execution to reach Exceptional."
          : "Review improvement areas and set structured KPIs.",
      });
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      {/* Inline styles for the scorecard */}
      <style>{`
        .dpi-card {
          background: linear-gradient(135deg, #0F1117 0%, #161B27 100%);
          border: 1px solid rgba(255,255,255,0.08);
          border-radius: 20px;
          padding: 28px;
          font-family: 'Inter', 'Segoe UI', sans-serif;
          color: #F1F5F9;
        }
        .dpi-header {
          display: flex;
          align-items: flex-start;
          justify-content: space-between;
          margin-bottom: 28px;
          gap: 20px;
          flex-wrap: wrap;
        }
        .dpi-title-block h2 {
          font-size: 1.3rem;
          font-weight: 700;
          margin: 0 0 4px;
          background: linear-gradient(90deg, #A78BFA, #60A5FA);
          -webkit-background-clip: text;
          -webkit-text-fill-color: transparent;
        }
        .dpi-title-block p {
          color: #94A3B8;
          font-size: 0.85rem;
          margin: 0;
        }
        .dpi-badge {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          padding: 4px 12px;
          border-radius: 20px;
          font-size: 0.75rem;
          font-weight: 600;
          margin-top: 8px;
          background: rgba(99,102,241,0.12);
          color: #A78BFA;
          border: 1px solid rgba(99,102,241,0.3);
        }
        .dpi-main {
          display: grid;
          grid-template-columns: 160px 1fr;
          gap: 28px;
          align-items: start;
        }
        @media (max-width: 600px) {
          .dpi-main { grid-template-columns: 1fr; }
        }
        .dpi-ring-container {
          position: relative;
          width: 140px;
          height: 140px;
          flex-shrink: 0;
        }
        .dpi-ring-inner {
          position: absolute;
          inset: 0;
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          gap: 2px;
        }
        .dpi-ring-icon { font-size: 1.4rem; }
        .dpi-ring-score {
          font-size: 1.8rem;
          font-weight: 800;
          line-height: 1;
        }
        .dpi-ring-label {
          font-size: 0.7rem;
          color: #64748B;
          text-transform: uppercase;
          letter-spacing: 0.08em;
        }
        .dpi-dimensions { display: flex; flex-direction: column; gap: 10px; }
        .dpi-dimension-row {
          display: grid;
          grid-template-columns: 36px 130px 1fr 64px;
          align-items: center;
          gap: 10px;
        }
        @media (max-width: 480px) {
          .dpi-dimension-row { grid-template-columns: 32px 110px 1fr 54px; }
        }
        .dpi-letter-badge {
          width: 32px;
          height: 32px;
          border-radius: 8px;
          border: 1px solid;
          display: flex;
          align-items: center;
          justify-content: center;
          font-weight: 800;
          font-size: 0.9rem;
          flex-shrink: 0;
        }
        .dpi-label-group { display: flex; flex-direction: column; gap: 1px; }
        .dpi-label { font-size: 0.8rem; font-weight: 600; color: #E2E8F0; }
        .dpi-desc { font-size: 0.68rem; color: #64748B; }
        .dpi-bar-track {
          height: 8px;
          background: rgba(255,255,255,0.06);
          border-radius: 999px;
          overflow: hidden;
        }
        .dpi-bar-fill {
          height: 100%;
          border-radius: 999px;
        }
        .dpi-score-badge {
          font-size: 0.85rem;
          font-weight: 700;
          text-align: right;
        }
        .dpi-score-max { font-size: 0.65rem; color: #475569; margin-left: 1px; }
        .dpi-footer {
          margin-top: 24px;
          padding-top: 20px;
          border-top: 1px solid rgba(255,255,255,0.06);
          display: flex;
          flex-direction: column;
          gap: 12px;
        }
        .dpi-rating-row {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          flex-wrap: wrap;
        }
        .dpi-rating-pill {
          display: flex;
          align-items: center;
          gap: 8px;
          padding: 8px 18px;
          border-radius: 999px;
          font-weight: 700;
          font-size: 0.9rem;
          border: 1px solid;
        }
        .dpi-grade {
          font-size: 2rem;
          font-weight: 900;
          line-height: 1;
        }
        .dpi-recommendation {
          background: rgba(255,255,255,0.04);
          border-radius: 10px;
          padding: 12px 16px;
          font-size: 0.82rem;
          color: #94A3B8;
          line-height: 1.5;
          border-left: 3px solid #6366F1;
        }
        .dpi-tags { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 4px; }
        .dpi-tag {
          padding: 3px 10px;
          border-radius: 999px;
          font-size: 0.72rem;
          font-weight: 600;
        }
        .dpi-tag-strength {
          background: rgba(16,185,129,0.12);
          color: #10B981;
          border: 1px solid rgba(16,185,129,0.25);
        }
        .dpi-tag-improve {
          background: rgba(245,158,11,0.12);
          color: #F59E0B;
          border: 1px solid rgba(245,158,11,0.25);
        }
        .dpi-section-label {
          font-size: 0.72rem;
          font-weight: 700;
          text-transform: uppercase;
          letter-spacing: 0.08em;
          color: #475569;
          margin-bottom: 4px;
        }
        .dpi-rescore-btn {
          align-self: flex-end;
          padding: 8px 18px;
          border-radius: 10px;
          background: linear-gradient(135deg, #6366F1, #8B5CF6);
          color: white;
          border: none;
          font-weight: 600;
          font-size: 0.82rem;
          cursor: pointer;
          transition: opacity 0.2s;
        }
        .dpi-rescore-btn:hover { opacity: 0.85; }
        .dpi-rescore-btn:disabled { opacity: 0.5; cursor: not-allowed; }
        .dpi-slider-group { display: flex; flex-direction: column; gap: 12px; margin-top: 12px; }
        .dpi-slider-row { display: grid; grid-template-columns: 100px 1fr 40px; align-items: center; gap: 10px; }
        .dpi-slider-row label { font-size: 0.8rem; color: #94A3B8; }
        .dpi-slider-row input[type=range] { accent-color: #6366F1; }
        .dpi-slider-val { font-size: 0.82rem; font-weight: 700; color: #A78BFA; text-align: right; }
        .dpi-loading {
          display: flex; align-items: center; justify-content: center;
          height: 200px; color: #6366F1; font-size: 0.9rem;
        }
        .dpi-spinner {
          width: 24px; height: 24px; border: 3px solid rgba(99,102,241,0.3);
          border-top-color: #6366F1; border-radius: 50%;
          animation: dpi-spin 0.8s linear infinite; margin-right: 10px;
        }
        @keyframes dpi-spin { to { transform: rotate(360deg); } }
      `}</style>

      <div className="dpi-card">
        {/* Header */}
        <div className="dpi-header">
          <div className="dpi-title-block">
            <h2>🏆 Digital Worker Appraisal</h2>
            <p>{result?.worker_role || workerRole} · Maturity {result?.maturity_level || maturityLevel}</p>
            <div className="dpi-badge">DPI-LS Score · Digital FTE Performance Index</div>
          </div>
          {interactive && (
            <button
              className="dpi-rescore-btn"
              onClick={() => setEditMode((e) => !e)}
            >
              {editMode ? "Hide Sliders" : "✏️ Adjust Scores"}
            </button>
          )}
        </div>

        {/* Slider panel */}
        {interactive && editMode && (
          <div className="dpi-slider-group">
            {DIMENSION_META.map((d) => (
              <div key={d.key} className="dpi-slider-row">
                <label>{d.letter} — {d.label}</label>
                <input
                  type="range"
                  min={0}
                  max={100}
                  value={dimensions[d.key]}
                  onChange={(e) =>
                    setDimensions((prev) => ({
                      ...prev,
                      [d.key]: Number(e.target.value),
                    }))
                  }
                />
                <span className="dpi-slider-val">{dimensions[d.key]}</span>
              </div>
            ))}
            <button
              className="dpi-rescore-btn"
              disabled={loading}
              onClick={fetchScore}
            >
              {loading ? "Scoring…" : "⚡ Score Now"}
            </button>
          </div>
        )}

        {/* Loading state */}
        {loading && (
          <div className="dpi-loading">
            <div className="dpi-spinner" />
            Scoring Digital Worker…
          </div>
        )}

        {/* Main content */}
        {!loading && result && (
          <>
            <div className="dpi-main">
              {/* Score ring */}
              <ScoreRing
                score={result.overall_score}
                color={result.rating_color}
                rating={result.rating}
                icon={result.rating_icon}
              />

              {/* Dimension bars */}
              <div className="dpi-dimensions">
                {DIMENSION_META.map((d, i) => (
                  <DimensionBar
                    key={d.key}
                    letter={d.letter}
                    label={d.label}
                    description={d.description}
                    value={result.dimensions[d.key]}
                    color={d.color}
                    delay={i * 80}
                  />
                ))}
              </div>
            </div>

            {/* Footer */}
            <div className="dpi-footer">
              {/* Rating row */}
              <div className="dpi-rating-row">
                <div
                  className="dpi-rating-pill"
                  style={{
                    color: result.rating_color,
                    borderColor: result.rating_color + "44",
                    backgroundColor: result.rating_color + "14",
                  }}
                >
                  <span>{result.rating_icon}</span>
                  <span>{result.rating}</span>
                </div>
                <div
                  className="dpi-grade"
                  style={{ color: result.rating_color }}
                >
                  {getScoreGrade(result.overall_score)}
                </div>
              </div>

              {/* Strengths */}
              {result.strengths.length > 0 && (
                <div>
                  <div className="dpi-section-label">💪 Strengths</div>
                  <div className="dpi-tags">
                    {result.strengths.map((s) => (
                      <span key={s} className="dpi-tag dpi-tag-strength">
                        {s}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {/* Improvement areas */}
              {result.improvement_areas.length > 0 && (
                <div>
                  <div className="dpi-section-label">📈 Improve</div>
                  <div className="dpi-tags">
                    {result.improvement_areas.map((s) => (
                      <span key={s} className="dpi-tag dpi-tag-improve">
                        {s}
                      </span>
                    ))}
                  </div>
                </div>
              )}

              {/* Recommendation */}
              <div className="dpi-recommendation">
                💡 {result.recommendation}
              </div>
            </div>
          </>
        )}

        {/* Error fallback */}
        {!loading && error && (
          <div style={{ color: "#EF4444", padding: "16px", fontSize: "0.85rem" }}>
            ⚠️ {error}
          </div>
        )}
      </div>
    </>
  );
}
