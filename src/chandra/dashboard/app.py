"""Chandra Streamlit dashboard.

Three tabs, all reading directly from Postgres (no API layer):

1. **Latest Briefing** — markdown render + per-KRA gauge plot.
2. **Findings Explorer** — filterable table with evidence drill-down.
3. **Eval Trend** — recall_overall over the last N eval runs.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import select

from chandra.config import settings
from chandra.db.models import Briefing, EvalRun, Finding as FindingRow, Run
from chandra.db.session import session_scope

st.set_page_config(
    page_title="Chandra — Cloud Health Briefing",
    page_icon="☁️",
    layout="wide",
)


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------


@st.cache_data(ttl=30)
def list_runs(limit: int = 100) -> pd.DataFrame:
    with session_scope() as sess:
        stmt = select(Run).order_by(Run.started_at.desc()).limit(limit)
        rows = sess.execute(stmt).scalars().all()
        data = [
            {
                "run_id": r.id,
                "account_id": r.account_id,
                "status": r.status,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
            }
            for r in rows
        ]
    return pd.DataFrame(data)


@st.cache_data(ttl=30)
def load_briefing(run_id: str) -> dict[str, Any] | None:
    with session_scope() as sess:
        briefing = (
            sess.query(Briefing).filter(Briefing.run_id == run_id).one_or_none()
        )
        if briefing is None:
            return None
        return {
            "markdown_text": briefing.markdown_text,
            "scorecard": dict(briefing.scorecard_jsonb or {}),
            "findings_count": briefing.findings_count,
            "created_at": briefing.created_at,
        }


@st.cache_data(ttl=30)
def load_findings(run_id: str) -> pd.DataFrame:
    with session_scope() as sess:
        rows = sess.query(FindingRow).filter(FindingRow.run_id == run_id).all()
        data = [
            {
                "detector_id": r.detector_id,
                "kra": r.kra,
                "severity": r.severity,
                "region": r.region,
                "resource_type": r.resource_type,
                "resource_arn": r.resource_arn,
                "title": r.title,
                "recommendation": r.recommendation,
                "evidence": r.evidence_jsonb,
            }
            for r in rows
        ]
    return pd.DataFrame(data)


@st.cache_data(ttl=30)
def load_eval_history(limit: int = 30) -> pd.DataFrame:
    with session_scope() as sess:
        stmt = (
            select(EvalRun, Run)
            .join(Run, Run.id == EvalRun.run_id)
            .order_by(Run.started_at.desc())
            .limit(limit)
        )
        rows = sess.execute(stmt).all()
        data = [
            {
                "run_id": ev.run_id,
                "started_at": run.started_at,
                "recall_overall": ev.recall_overall,
                "precision_overall": ev.precision_overall,
                "fp_count": ev.fp_count,
                "recall_per_kra": ev.recall_per_kra_jsonb,
            }
            for ev, run in rows
        ]
    df = pd.DataFrame(data)
    if not df.empty:
        df = df.sort_values("started_at")
    return df


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------


def scorecard_gauges(scorecard: dict[str, Any]) -> go.Figure:
    """Five gauges plus an overall gauge."""
    kras = ["cost", "security", "compliance", "performance", "reliability", "overall"]
    fig = go.Figure()
    cols = 3
    for i, kra in enumerate(kras):
        row = i // cols
        col = i % cols
        domain_x = [col / cols, (col + 1) / cols]
        domain_y = [1 - (row + 1) / 2, 1 - row / 2]
        value = int(scorecard.get(kra, 0))
        color = "#2ca02c" if value >= 80 else "#ff7f0e" if value >= 60 else "#d62728"
        fig.add_trace(
            go.Indicator(
                mode="gauge+number",
                value=value,
                title={"text": kra.title()},
                domain={"x": domain_x, "y": domain_y},
                gauge={
                    "axis": {"range": [0, 100]},
                    "bar": {"color": color},
                    "steps": [
                        {"range": [0, 60], "color": "#fde0dc"},
                        {"range": [60, 80], "color": "#ffe9c2"},
                        {"range": [80, 100], "color": "#d9f2d9"},
                    ],
                },
            )
        )
    fig.update_layout(height=520, margin={"t": 20, "b": 0, "l": 0, "r": 0})
    return fig


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------


st.sidebar.title("☁️ Chandra")
st.sidebar.caption("Digital Cloud Engineer")
st.sidebar.markdown(f"**Postgres:** `{settings.postgres_url.split('@')[-1]}`")

runs_df = list_runs(limit=100)
if runs_df.empty:
    st.sidebar.warning("No runs found in Postgres yet.")
    st.title("Welcome to Chandra")
    st.write("Run the agent with `make run` to populate this dashboard.")
    st.stop()

run_options = runs_df["run_id"].tolist()
default_run = run_options[0]
selected_run = st.sidebar.selectbox(
    "Select a run",
    options=run_options,
    index=0,
    format_func=lambda r: f"{r[:8]} — {runs_df.loc[runs_df.run_id == r, 'account_id'].iloc[0]}",
)


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------


tab_briefing, tab_findings, tab_eval = st.tabs(
    ["📄 Latest Briefing", "🔎 Findings Explorer", "📈 Eval Trend"]
)


with tab_briefing:
    briefing = load_briefing(selected_run)
    if briefing is None:
        st.info("This run has no briefing yet — it may still be in progress.")
    else:
        col1, col2 = st.columns([3, 2])
        with col1:
            st.markdown(briefing["markdown_text"])
        with col2:
            st.subheader("KRA Scorecard")
            st.plotly_chart(scorecard_gauges(briefing["scorecard"]), use_container_width=True)
            st.metric("Total findings", briefing["findings_count"])
            st.caption(f"Briefing created at {briefing['created_at']}")


with tab_findings:
    findings_df = load_findings(selected_run)
    if findings_df.empty:
        st.info("No findings for this run — account is clean.")
    else:
        c1, c2, c3 = st.columns(3)
        with c1:
            kra_filter = st.multiselect(
                "KRA", sorted(findings_df["kra"].unique()), default=[]
            )
        with c2:
            severity_filter = st.multiselect(
                "Severity",
                sorted(findings_df["severity"].unique()),
                default=[],
            )
        with c3:
            region_filter = st.multiselect(
                "Region", sorted(findings_df["region"].unique()), default=[]
            )

        filtered = findings_df
        if kra_filter:
            filtered = filtered[filtered["kra"].isin(kra_filter)]
        if severity_filter:
            filtered = filtered[filtered["severity"].isin(severity_filter)]
        if region_filter:
            filtered = filtered[filtered["region"].isin(region_filter)]

        st.caption(f"{len(filtered)} of {len(findings_df)} findings shown")
        st.dataframe(
            filtered.drop(columns=["evidence", "recommendation"]),
            use_container_width=True,
            hide_index=True,
        )

        st.divider()
        st.subheader("Evidence drill-down")
        for _, row in filtered.iterrows():
            with st.expander(
                f"[{row['severity'].upper()}] {row['detector_id']} — {row['title']}"
            ):
                st.markdown(f"**Resource:** `{row['resource_arn']}`")
                st.markdown(f"**Region:** {row['region']}")
                st.markdown("**Recommendation:**")
                st.write(row["recommendation"])
                st.markdown("**Evidence:**")
                st.json(row["evidence"])


with tab_eval:
    eval_df = load_eval_history(limit=30)
    if eval_df.empty:
        st.info("No eval runs found. Run `make eval` to populate.")
    else:
        latest = eval_df.iloc[-1]
        c1, c2, c3 = st.columns(3)
        c1.metric("Recall (latest)", f"{latest['recall_overall']:.2%}")
        c2.metric("Precision (latest)", f"{latest['precision_overall']:.2%}")
        c3.metric("False positives", int(latest["fp_count"]))

        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=eval_df["started_at"],
                y=eval_df["recall_overall"],
                name="recall",
                mode="lines+markers",
            )
        )
        fig.add_trace(
            go.Scatter(
                x=eval_df["started_at"],
                y=eval_df["precision_overall"],
                name="precision",
                mode="lines+markers",
            )
        )
        fig.update_layout(
            yaxis={"range": [0, 1.05], "tickformat": ".0%"},
            height=380,
            legend={"orientation": "h"},
            margin={"t": 30},
            title="Eval recall + precision over time",
        )
        st.plotly_chart(fig, use_container_width=True)

        st.subheader("Per-KRA recall (latest run)")
        per_kra = latest["recall_per_kra"] or {}
        if per_kra:
            kra_df = pd.DataFrame(
                {
                    "kra": list(per_kra.keys()),
                    "recall": list(per_kra.values()),
                }
            )
            st.bar_chart(kra_df.set_index("kra"))
