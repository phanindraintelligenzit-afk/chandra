"""DPI-LS (Digital FTE Performance Index) scoring engine.

Calculates the performance rating and safety compliance status of AI digital workers
based on 7 core dimensions: Productivity, Quality, Execution, Governance, Risk,
Validation, and Cost.
"""

from typing import Any, Dict, List, Optional, Tuple

DEFAULT_WEIGHTS = {
    "P": 0.15,
    "Q": 0.20,
    "E": 0.15,
    "G": 0.20,
    "R": 0.15,
    "V": 0.10,
    "C": 0.05,
}

DEFAULT_GATES = {
    "G_floor": 0.60,
    "R_floor": 0.50,
    "V_floor": 0.60,
}

INCIDENT_SEVERITY_WEIGHTS = {
    "critical": 1.0,
    "high": 0.5,
    "medium": 0.2,
    "low": 0.05,
}


def calculate_dpi_score(
    metrics: Dict[str, Any],
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Calculate the DPI-LS score and governance gates status.

    Args:
        metrics: Dictionary containing raw metrics or pre-calculated sub-metrics:
            - productivity: dict(ai_output, human_baseline) or float [0, 1]
            - quality: dict(accuracy, consistency, hallucination) or float [0, 1]
            - execution: dict(successful_executions, total_attempts) or float [0, 1]
            - governance: dict(policy_violations, total_actions) or float [0, 1]
            - risk: dict(incidents: list[dict(severity, count)], r_max) or float [0, 1]
            - validation: dict(validated_components, total_required) or float [0, 1]
            - cost: dict(human_cost, ai_cost, utilization) or float [0, 1]
        settings: Optional overrides for weights, thresholds, and parameters.

    Returns:
        A dictionary with sub-metric scores, the overall DPI-LS rating, gate violations,
        and rating band description.
    """
    opts = settings or {}
    weights = opts.get("weights", DEFAULT_WEIGHTS)
    gates = opts.get("gates", DEFAULT_GATES)

    # 1. Productivity (P)
    p_raw = metrics.get("productivity")
    if isinstance(p_raw, dict):
        ai_out = float(p_raw.get("ai_output", 0.0))
        h_base = float(p_raw.get("human_baseline", 1.0))
        p_val = min(1.0, ai_out / h_base) if h_base > 0 else 0.0
    else:
        p_val = float(p_raw if p_raw is not None else 0.0)

    # 2. Quality (Q)
    q_raw = metrics.get("quality")
    if isinstance(q_raw, dict):
        acc = float(q_raw.get("accuracy", 0.0))
        cons = float(q_raw.get("consistency", 0.0))
        hal = float(q_raw.get("hallucination", 0.0))
        w_acc = float(opts.get("q_weights", {}).get("accuracy", 0.7))
        w_cons = float(opts.get("q_weights", {}).get("consistency", 0.2))
        w_hal = float(opts.get("q_weights", {}).get("hallucination", 0.1))
        q_val = (w_acc * acc) + (w_cons * cons) + (w_hal * (1.0 - hal))
    else:
        q_val = float(q_raw if q_raw is not None else 0.0)

    # 3. Execution (E)
    e_raw = metrics.get("execution")
    if isinstance(e_raw, dict):
        success = float(e_raw.get("successful_executions", 0.0))
        attempts = float(e_raw.get("total_attempts", 1.0))
        e_val = success / attempts if attempts > 0 else 0.0
    else:
        e_val = float(e_raw if e_raw is not None else 0.0)

    # 4. Governance (G)
    g_raw = metrics.get("governance")
    if isinstance(g_raw, dict):
        violations = float(g_raw.get("policy_violations", 0.0))
        actions = float(g_raw.get("total_actions", 1.0))
        g_val = 1.0 - (violations / actions) if actions > 0 else 1.0
    else:
        g_val = float(g_raw if g_raw is not None else 1.0)

    # 5. Risk (R)
    r_raw = metrics.get("risk")
    if isinstance(r_raw, dict):
        incidents = r_raw.get("incidents", [])
        r_max = float(r_raw.get("r_max", opts.get("r_max", 10.0)))
        sum_risk = 0.0
        for inc in incidents:
            sev = inc.get("severity", "low").lower()
            count = int(inc.get("count", 1))
            weight = INCIDENT_SEVERITY_WEIGHTS.get(sev, 0.05)
            sum_risk += weight * count
        r_val = 1.0 - min(1.0, sum_risk / r_max) if r_max > 0 else 0.0
    else:
        r_val = float(r_raw if r_raw is not None else 1.0)

    # 6. Validation (V)
    v_raw = metrics.get("validation")
    if isinstance(v_raw, dict):
        val_comp = float(v_raw.get("validated_components", 0.0))
        total_req = float(v_raw.get("total_required", 1.0))
        v_val = val_comp / total_req if total_req > 0 else 1.0
    else:
        v_val = float(v_raw if v_raw is not None else 1.0)

    # 7. Cost (C)
    c_raw = metrics.get("cost")
    if isinstance(c_raw, dict):
        h_cost = float(c_raw.get("human_cost", 0.0))
        ai_cost = float(c_raw.get("ai_cost", 1.0))
        util = float(c_raw.get("utilization", 1.0))
        c_val = (min(1.0, h_cost / ai_cost) if ai_cost > 0 else 0.0) * util
    else:
        c_val = float(c_raw if c_raw is not None else 1.0)

    # Ensure all sub-metrics are bounded [0.0, 1.0]
    p_val = max(0.0, min(1.0, p_val))
    q_val = max(0.0, min(1.0, q_val))
    e_val = max(0.0, min(1.0, e_val))
    g_val = max(0.0, min(1.0, g_val))
    r_val = max(0.0, min(1.0, r_val))
    v_val = max(0.0, min(1.0, v_val))
    c_val = max(0.0, min(1.0, c_val))

    # Calculate overall weighted geometric mean
    w_p = weights.get("P", 0.15)
    w_q = weights.get("Q", 0.20)
    w_e = weights.get("E", 0.15)
    w_g = weights.get("G", 0.20)
    w_r = weights.get("R", 0.15)
    w_v = weights.get("V", 0.10)
    w_c = weights.get("C", 0.05)

    # Prevent math domain error with zero or negative logs by handling zero values
    # 0.0001 is used as a floor for scoring to avoid 0 total score in geo-mean.
    p_f = max(0.0001, p_val)
    q_f = max(0.0001, q_val)
    e_f = max(0.0001, e_val)
    g_f = max(0.0001, g_val)
    r_f = max(0.0001, r_val)
    v_f = max(0.0001, v_val)
    c_f = max(0.0001, c_val)

    geo_mean = (
        (p_f ** w_p) *
        (q_f ** w_q) *
        (e_f ** w_e) *
        (g_f ** w_g) *
        (r_f ** w_r) *
        (v_f ** w_v) *
        (c_f ** w_c)
    )

    score_raw = geo_mean * 100.0

    # 8. Check Compliance Gates (hard floors)
    g_floor = gates.get("G_floor", 0.60)
    r_floor = gates.get("R_floor", 0.50)
    v_floor = gates.get("V_floor", 0.60)

    g_violation = g_val < g_floor
    r_violation = r_val < r_floor
    v_violation = v_val < v_floor
    gate_breached = g_violation or r_violation or v_violation

    # If any gate is breached, overall score is capped at 69 (Needs Optimization threshold)
    score_final = min(score_raw, 69.0) if gate_breached else score_raw

    # Assign rating band
    if gate_breached:
        rating_band = "Needs Optimization (Compliance Gate Breach)" if score_final >= 50.0 else "Underperforming/Unsafe"
    elif score_final >= 85.0:
        rating_band = "Exceptional"
    elif score_final >= 70.0:
        rating_band = "Strong"
    elif score_final >= 50.0:
        rating_band = "Needs Optimization"
    else:
        rating_band = "Underperforming/Unsafe"

    return {
        "metrics": {
            "productivity": p_val,
            "quality": q_val,
            "execution": e_val,
            "governance": g_val,
            "risk": r_val,
            "validation": v_val,
            "cost": c_val,
        },
        "score_raw": round(score_raw, 2),
        "score_final": round(score_final, 2),
        "gate_breached": gate_breached,
        "violations": {
            "governance": g_violation,
            "risk": r_violation,
            "validation": v_violation,
        },
        "rating_band": rating_band,
    }
