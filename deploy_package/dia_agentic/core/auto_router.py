"""Deterministic semi-automatic Level-1 router.

Replaces the external TypeSafe JEV dependency with rules that run on signals the
app already computes. Nothing here recalculates a risk score, re-runs a test, or
calls a model: it only reads the deterministic assessment and sorts each invoice
into one of three lanes.

    escalate        clear high risk     -> reviewer confirms the finding
    needs_evidence  genuinely uncertain -> reviewer opens evidence and decides
    auto_clear      clear low risk      -> reviewer bulk-accepts

Every decision records the rule that fired, so an override is always auditable
against a named rule rather than an opaque model answer.
"""
from __future__ import annotations

from .evidence import evidence_for

ROUTER_VERSION = "deterministic-v2"

# The lanes key off risk_scorer's band labels rather than raw score cut-offs.
# The raw score is partly relative to the dataset (it is damped by the largest
# amount in the file), so a fixed numeric threshold silently stops firing on a
# file whose scores top out low and the whole queue piles into one lane. The
# bands are already calibrated, so they transfer across datasets.
ESCALATE_BANDS = ("Critical", "High")
CLEAR_BAND = "Low"
CORROBORATED_OVERLAP = 2     # two or more independent tests hitting one invoice

ROUTES = ("escalate", "needs_evidence", "auto_clear")

# A route the reviewer accepts resolves the invoice in the main review queue too,
# so one click finishes the invoice instead of leaving it pending in two places.
ROUTE_TO_REVIEW_STATUS = {
    "escalate": "confirmed",
    "needs_evidence": "needs_info",
    "auto_clear": "false_positive",
}

LANE_LABELS = {
    "escalate": "Needs you",
    "needs_evidence": "Needs evidence",
    "auto_clear": "Auto-cleared",
}


def _collect_evidence(row: dict, flagged_by: list) -> list[dict]:
    evidence = []
    for hit in flagged_by:
        evidence.extend(evidence_for(hit.get("test_id", ""), row))
    return evidence


def _evidence_is_complete(evidence: list[dict], flagged_by: list) -> bool:
    """evidence_for() already drops blank values, so a test with no surviving
    evidence item means the supporting field was missing on the invoice."""
    if not flagged_by:
        return False
    return len(evidence) >= len(flagged_by)


def _test_names(flagged_by: list) -> list[str]:
    return [h.get("test_name") or h.get("test_id", "") for h in flagged_by]


def classify(row: dict, risk) -> dict:
    """Route one invoice. Pure function of the deterministic assessment."""
    evidence = _collect_evidence(row, risk.flagged_by)
    complete = _evidence_is_complete(evidence, risk.flagged_by)
    tests = _test_names(risk.flagged_by)
    score = risk.risk_score
    band = risk.band_label
    overlap = risk.overlap_count

    # ── escalate: a standout, not merely a hit ───────────────────────────────
    # A single test firing is not a discriminator: on a typical file most
    # flagged invoices have exactly one hit, so escalating on that alone just
    # rebuilds the unsorted queue. Escalation needs a band the scorer itself
    # rates high, or corroboration from a second independent test.
    if band in ESCALATE_BANDS:
        return _decision(
            risk, "escalate", 0.90, "high_band",
            f"{band} risk at {score}/100 across {overlap} triggered "
            f"test{'s' if overlap != 1 else ''} ({', '.join(tests[:2])}).",
            evidence, complete,
        )
    if overlap >= CORROBORATED_OVERLAP:
        return _decision(
            risk, "escalate", 0.85, "corroborated_tests",
            f"{overlap} independent tests flagged this invoice "
            f"({', '.join(tests[:2])}) at {score}/100.",
            evidence, complete,
        )

    # ── auto_clear: lowest band, one warning-level test, nothing missing ─────
    # An error-level control exception still needs a human even if its amount
    # pulls the numeric score into the lowest band.
    if (band == CLEAR_BAND and overlap == 1 and complete
            and risk.flagged_by[0].get("severity") != "error"):
        return _decision(
            risk, "auto_clear", 0.90, "low_risk_complete",
            f"Low risk at {score}/100 — a single test ({tests[0]}) with its "
            f"supporting evidence present.",
            evidence, complete,
        )

    # ── needs_evidence: the deterministic signal alone cannot settle it ───────
    if not complete:
        reason = (
            f"{band} risk at {score}/100, but supporting evidence is incomplete "
            f"({len(evidence)} item{'s' if len(evidence) != 1 else ''} for {overlap} test"
            f"{'s' if overlap != 1 else ''}) — check the source document."
        )
        rule = "incomplete_evidence"
    elif overlap == 1 and risk.flagged_by[0].get("severity") == "error":
        reason = (
            f"{band} risk at {score}/100, but {tests[0]} is an error-level "
            "control exception — open the invoice before deciding."
        )
        rule = "control_exception"
    else:
        reason = (
            f"{band} risk at {score}/100 from a single test ({tests[0]}) sits between "
            f"the auto-clear and escalation bands — open the invoice before deciding."
        )
        rule = "uncertain_band"
    return _decision(risk, "needs_evidence", 0.55, rule, reason, evidence, complete)


def _decision(risk, route: str, confidence: float, rule: str, reason: str,
              evidence: list[dict], complete: bool) -> dict:
    """Shape the result so it persists through save_level1_routes() unchanged."""
    return {
        "row_idx": risk.row_idx,
        "suggested_route": route,
        "confidence": round(confidence, 4),
        "reason": reason,
        "model": f"{ROUTER_VERSION}:{rule}",
        "answers": {
            "rule": rule,
            "risk_score": risk.risk_score,
            "risk_band": risk.band_label,
            "overlap_count": risk.overlap_count,
            "evidence_complete": complete,
            "evidence_count": len(evidence),
        },
        "state": {
            "row_idx": risk.row_idx,
            "deterministic_assessment": {
                "risk_score": risk.risk_score,
                "risk_band": risk.band_label,
                "overlap_count": risk.overlap_count,
                "flagged_by": [
                    {
                        "test_id": h.get("test_id"),
                        "test_name": h.get("test_name"),
                        "severity": h.get("severity"),
                        "weightage": h.get("weightage"),
                    }
                    for h in risk.flagged_by
                ],
                "evidence": evidence,
            },
        },
        "usage": {},
    }


def route_invoices(rows: list[dict], risks: list) -> list[dict]:
    """Route a batch. No network, no model, no retry — safe to run on every row."""
    return [classify(rows[risk.row_idx], risk) for risk in risks]
