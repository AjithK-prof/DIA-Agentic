import math
from dataclasses import dataclass, field

OVERLAP_BONUS = {1: 1.0, 2: 1.2, 3: 1.35}
OVERLAP_MAX = 1.5
MAX_WEIGHTAGE = 10.0
BANDS = [(75, "Critical", "critical"), (50, "High", "high"),
         (25, "Medium", "medium"), (0, "Low", "low")]


@dataclass
class RowRisk:
    row_idx: int
    risk_score: int
    band_label: str
    band_css: str
    flagged_by: list
    overlap_count: int
    amount: float


def compute_risk_scores(rows: list[dict], test_results: list[dict]) -> list[RowRisk]:
    row_to_tests: dict[int, list] = {}
    for tr in test_results:
        if not tr.get("applicable") or tr.get("hit_count", 0) == 0:
            continue
        entry = {
            "test_id": tr["test_id"],
            "test_name": tr["test_name"],
            "weightage": tr.get("weightage", 7),
            "severity": tr.get("severity", "warning"),
            "type": tr.get("type", "generated"),
            "required_fields": tr.get("required_fields", []),
        }
        for idx in tr.get("all_hit_indices", []):
            row_to_tests.setdefault(idx, []).append(entry)

    if not row_to_tests:
        return []

    amounts = []
    for idx in row_to_tests:
        try:
            a = abs(float(str(rows[idx].get("amount_excl_lc") or 0).replace(",", "")))
        except (ValueError, TypeError):
            a = 0.0
        amounts.append(a)
    max_amount = max(amounts) if amounts else 1.0
    log_max = math.log10(max_amount + 1) if max_amount > 0 else 1.0

    results = []
    for idx, tests in row_to_tests.items():
        # Weightage runs 1-10, so 10.0 normalises the mean onto a 0-10 scale.
        contributions = [t["weightage"] for t in tests]
        test_score = (sum(contributions) / len(contributions) / MAX_WEIGHTAGE) * 10.0
        n = len(tests)
        overlap = OVERLAP_BONUS.get(n, OVERLAP_MAX)
        try:
            amt = abs(float(str(rows[idx].get("amount_excl_lc") or 0).replace(",", "")))
        except (ValueError, TypeError):
            amt = 0.0
        amount_score = min(math.log10(amt + 1) / log_max, 1.0) * 10.0
        raw = test_score * overlap * amount_score
        score = min(100, round(raw / 150.0 * 100))

        label, css = next(((l, c) for t, l, c in BANDS if score >= t), ("Low", "low"))
        results.append(RowRisk(
            row_idx=idx, risk_score=score,
            band_label=label, band_css=css,
            flagged_by=tests, overlap_count=n, amount=amt,
        ))

    results.sort(key=lambda r: r.risk_score, reverse=True)
    return results
