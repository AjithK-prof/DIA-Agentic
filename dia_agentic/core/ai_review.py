"""Agentic review layer: one LLM call produces an executive triage summary plus a
one-line plain-English summary for each cluster. Results are cached by signature.
"""
import json
import os
import re
import time

# ════════════════════════════════════════════════════════════════════════════════
#  LLM PROVIDER TOGGLE
#  Active: GROQ
#  To switch to OpenRouter: comment the Groq block, uncomment the OpenRouter block
# ════════════════════════════════════════════════════════════════════════════════

# ── GROQ (commented — replaced by llm_provider) ──────────────────────────────
# from groq import Groq
# GROQ_MODELS = ["qwen/qwen3.8-27b"]

# ── LLM PROVIDER (Foundry Local or Groq, chosen via .env) ────────────────────
from dia_agentic.core.llm_provider import get_client as _get_llm_client, is_foundry_active

# ── OPENROUTER (commented — free models too slow; re-enable with paid credits) ──
# from openai import OpenAI
# OR_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"   # or any paid model

# ════════════════════════════════════════════════════════════════════════════════

MAX_RETRIES_ON_429 = 2
RETRY_WAIT_SECONDS = 15
MAX_RULE_SENT = 6
MAX_EMERGENT_SENT = 4
DISCLAIMER = "AI-generated. May be inaccurate — verify findings before acting."


def _fmt_amt(n):
    if not n:
        return "$0"
    a = abs(n)
    if a >= 1e9: return f"${n/1e9:.1f}B"
    if a >= 1e6: return f"${n/1e6:.1f}M"
    if a >= 1e3: return f"${n/1e3:.1f}K"
    return f"${n:.0f}"


def generate_triage(rule_clusters: list, emergent_clusters: list | None = None) -> dict:
    """Deterministic triage summary — no LLM, always works instantly."""
    emergent = emergent_clusters or []
    multi_test = [c for c in rule_clusters if c.get("test_count", 1) > 1]
    total_invoices = sum(c["size"] for c in rule_clusters)
    total_amount = sum(c["amount_at_risk"] for c in rule_clusters)
    critical_emergent = [c for c in emergent if c.get("severity") in ("critical", "high")]

    parts = []
    if rule_clusters:
        parts.append(
            f"{len(rule_clusters)} anomaly patterns found across {total_invoices:,} "
            f"flagged invoices ({_fmt_amt(total_amount)} at risk)."
        )
    if multi_test:
        top = multi_test[0]
        parts.append(
            f"Prioritise the {len(multi_test)} multi-test cluster"
            f"{'s' if len(multi_test) > 1 else ''} — invoices that triggered "
            f"multiple alarms simultaneously carry the strongest fraud signal "
            f"(top: {top['title']}, {top['amount_display']})."
        )
    elif rule_clusters:
        top = rule_clusters[0]
        parts.append(
            f"Start with \"{top['title']}\" "
            f"({top['size']} invoices, {top['amount_display']}) — highest risk cluster."
        )
    if critical_emergent:
        parts.append(
            f"{len(critical_emergent)} high-severity data-driven anomal"
            f"{'ies' if len(critical_emergent) > 1 else 'y'} also detected "
            f"beyond your rules — check the 'Also looks abnormal' section."
        )

    priorities = []
    for c in rule_clusters[:3]:
        tc = c.get("test_count", 1)
        if tc > 1:
            reason = (f"{tc} tests fired on same invoices — multi-alarm "
                      f"pattern, {c['amount_display']} at risk")
        else:
            reason = (f"{c['size']:,} invoices, {c['amount_display']} at risk "
                      f"— {c['dominant_band']} risk")
        priorities.append({"cluster_id": c["cluster_id"], "reason": reason})

    for c in emergent[:2]:
        desc = (c.get("description") or c["title"])[:80]
        priorities.append({
            "cluster_id": c["cluster_id"],
            "reason": f"{desc} — {c['size']:,} invoices, {c['amount_display']}"
        })

    summaries = {}
    for c in rule_clusters:
        names = c.get("test_names") or [c["title"]]
        tests_str = " + ".join(names)
        chips = ", ".join(c.get("evidence_chips", []))
        s = (f"{c['size']:,} invoices matched {tests_str} "
             f"({c['amount_display']} at risk, {c['dominant_band']} risk).")
        if chips:
            s += f" Signals: {chips}."
        summaries[c["cluster_id"]] = s

    for c in emergent:
        desc = c.get("description", c["title"])
        summaries[c["cluster_id"]] = (
            f"{desc} "
            f"Affects {c['size']:,} invoices totalling {c['amount_display']}."
        )

    return {
        "executive_summary": " ".join(parts),
        "priorities": priorities,
        "cluster_summaries": summaries,
        "disclaimer": "Auto-generated from cluster data. No AI involved — instant and always available.",
        "source": "deterministic",
    }


def _extract_first_json(text: str, required_keys: list | None = None) -> str | None:
    """Return the first balanced {...} block that parses as valid JSON
    and contains all required_keys (if specified)."""
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == '{':
            if start is None:
                start = i
            depth += 1
        elif ch == '}':
            if depth > 0:
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        data = json.loads(candidate)
                        if required_keys is None or all(k in data for k in required_keys):
                            return candidate
                    except json.JSONDecodeError:
                        pass
                    start = None
    return None


# ── GROQ key helper (active) ─────────────────────────────────────────────────────
def _get_keys() -> list[str]:
    keys = []
    for k in ("GROQ_API_KEY", "GROQ_API_KEY_2"):
        v = os.getenv(k, "").strip()
        if v:
            keys.append(v)
    return keys

# ── OPENROUTER key helper (commented) ────────────────────────────────────────────
# def _get_openrouter_key() -> str:
#     return os.getenv("OPENROUTER_API_KEY", "").strip()


SYSTEM_PROMPT = """You are a P2P fraud analyst reviewing invoice anomaly clusters. Analyse the clusters provided and return ONLY valid JSON — no markdown, no explanation, no preamble.

Use this exact structure, replacing every value with your real analysis:
{
  "executive_summary": "Prioritise the 58 same-day round-dollar invoices totalling $47M — they hit two tests simultaneously and carry the highest fraud risk. Secondary focus: 20 vendor-outlier invoices averaging $18M each.",
  "priorities": [
    {"cluster_id": "rule::a1b2c3d4", "reason": "Two tests fired on same invoices; $47M at risk with no weekday pattern"},
    {"cluster_id": "emg::amount_outlier", "reason": "Amounts 10x above vendor norm — possible fabrication"}
  ],
  "cluster_summaries": {
    "rule::a1b2c3d4": "58 invoices flagged by both round-dollar and same-day-payment tests, concentrated in Q4, averaging $811K each.",
    "emg::amount_outlier": "20 invoices dramatically above each vendor's typical billing amount, suggesting inflated or fictitious charges."
  }
}"""


def _compact(rule_clusters, emergent_clusters):
    out = []
    for c in rule_clusters[:MAX_RULE_SENT]:
        out.append({
            "id": c["cluster_id"],
            "src": "rule",
            "pattern": c["title"],
            "tests": len(c.get("test_ids", [])),
            "invoices": c["size"],
            "amount": c["amount_display"],
            "band": c["dominant_band"],
        })
    for c in (emergent_clusters or [])[:MAX_EMERGENT_SENT]:
        out.append({
            "id": c["cluster_id"],
            "src": "emergent",
            "anomaly": c["title"],
            "invoices": c["size"],
            "amount": c["amount_display"],
            "severity": c.get("severity", "medium"),
        })
    return out


def analyse_clusters(rule_clusters: list[dict], emergent_clusters: list[dict] | None = None) -> dict:
    # ── Get LLM client from provider (Foundry Local or Groq) ─────────────────
    try:
        _llm_client, _llm_model = _get_llm_client()
    except RuntimeError as e:
        return {"error": str(e), "disclaimer": DISCLAIMER}

    # ── GROQ key check (commented — now handled by llm_provider) ─────────────
    # keys = _get_keys()
    # if not keys:
    #     return {"error": "GROQ_API_KEY not set in .env", "disclaimer": DISCLAIMER}

    # ── OPENROUTER key check (commented) ─────────────────────────────────────────
    # or_key = _get_openrouter_key()
    # if not or_key:
    #     return {"error": "OPENROUTER_API_KEY not set in .env", "disclaimer": DISCLAIMER}

    if not rule_clusters and not emergent_clusters:
        return {"executive_summary": "No clusters to analyse.",
                "priorities": [], "cluster_summaries": {}, "disclaimer": DISCLAIMER}

    payload = json.dumps({"clusters": _compact(rule_clusters, emergent_clusters)}, default=str)
    last_error = ""

    for attempt in range(MAX_RETRIES_ON_429 + 1):
        # ── GROQ nested key+model loop (commented — llm_provider handles selection)
        # for key in keys:
        #     for model in GROQ_MODELS:
        #         client = Groq(api_key=key)
        try:
            resp = _llm_client.chat.completions.create(
                model=_llm_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"Analyse these clusters and return the JSON:\n{payload}"},
                ],
                temperature=0.1,
                max_tokens=2000,
            )
            raw = (resp.choices[0].message.content or "").strip() if resp.choices else ""

            if not raw:
                last_error = f"Model {_llm_model} returned empty response"
            else:
                # strip <think>...</think> blocks
                raw = re.sub(r"<think>[\s\S]*?</think>", "", raw, flags=re.IGNORECASE).strip()

                if not raw:
                    last_error = f"Model {_llm_model} returned only a think block"
                else:
                    # strip markdown fences if present
                    if "```" in raw:
                        for p in raw.split("```"):
                            p = p.strip().lstrip("json").strip()
                            if p.startswith("{"):
                                raw = p
                                break

                    extracted = _extract_first_json(raw, required_keys=["executive_summary"])
                    if not extracted:
                        last_error = f"No valid JSON with required keys from {_llm_model}"
                    else:
                        data = json.loads(extracted)
                        data.setdefault("executive_summary", "")
                        data.setdefault("priorities", [])
                        data.setdefault("cluster_summaries", {})
                        data["disclaimer"] = DISCLAIMER
                        return data

        except Exception as e:
            err = str(e)
            if "401" in err or "invalid_api_key" in err:
                return {"error": "API key invalid or expired. Update GROQ_API_KEY in .env.",
                        "disclaimer": DISCLAIMER}
            if "429" in err or "rate_limit" in err:
                last_error = f"Rate limit, attempt {attempt + 1}"
                if attempt < MAX_RETRIES_ON_429:
                    time.sleep(RETRY_WAIT_SECONDS)
                    continue
            last_error = err

        # ── OPENROUTER single call (commented) ───────────────────────────────────
        # try:
        #     client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=or_key)
        #     resp = client.chat.completions.create(model=OR_MODEL, ...)
        #     ...

        break

    return {"error": f"AI triage failed: {last_error}", "disclaimer": DISCLAIMER}


def _chat_json(
    system_prompt: str,
    user_content: str,
    required_keys: list[str] | None = None,
    max_tokens: int = 1200,
    max_retries: int = MAX_RETRIES_ON_429,
) -> dict | None:
    """Single robust LLM call returning a parsed JSON object, or None on failure."""
    required_keys = required_keys or ["suggestions"]

    # ── Get LLM client from provider (Foundry Local or Groq) ─────────────────
    try:
        _llm_client, _llm_model = _get_llm_client()
    except RuntimeError:
        return None

    # ── GROQ key check (commented — now handled by llm_provider) ─────────────
    # keys = _get_keys()
    # if not keys:
    #     return None

    # ── OPENROUTER key check (commented) ─────────────────────────────────────────
    # or_key = _get_openrouter_key()
    # if not or_key:
    #     return None

    for attempt in range(max_retries + 1):
        # ── GROQ nested key+model loop (commented — llm_provider handles selection)
        # for key in keys:
        #     for model in GROQ_MODELS:
        #         resp = Groq(api_key=key).chat.completions.create(...)
        try:
            resp = _llm_client.chat.completions.create(
                model=_llm_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                temperature=0.2,
                max_tokens=max_tokens,
            )
            raw = (resp.choices[0].message.content or "").strip() if resp.choices else ""
            raw = re.sub(r"<think>[\s\S]*?</think>", "", raw).strip()
            if raw:
                extracted = _extract_first_json(raw, required_keys=required_keys)
                if extracted:
                    return json.loads(extracted)
        except Exception as e:
            if ("429" in str(e) or "rate_limit" in str(e)) and attempt < max_retries:
                time.sleep(RETRY_WAIT_SECONDS)
                continue

        # ── OPENROUTER single call (commented) ───────────────────────────────────
        # try:
        #     resp = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=or_key)
        #         .chat.completions.create(model=OR_MODEL, ...)
        #     ...

        break
    return None


INVESTIGATION_PROMPT = """You are a P2P investigation assistant. Use only the supplied
aggregated facts. Do not invent evidence. Return ONLY JSON:
{
  "summary": "A concise 2-4 sentence investigation brief.",
  "next_actions": ["Specific action 1", "Specific action 2", "Specific action 3"]
}"""


def polish_investigation(facts: dict) -> dict | None:
    """Use one compact LLM call to improve an investigation brief."""
    data = _chat_json(
        INVESTIGATION_PROMPT,
        "Aggregated cluster facts:\n" + json.dumps(facts, default=str),
        required_keys=["summary", "next_actions"],
        max_tokens=700,
        max_retries=0,
    )
    if not data or not isinstance(data.get("next_actions"), list):
        return None
    return {
        "summary": str(data["summary"]),
        "next_actions": [str(x) for x in data["next_actions"][:4]],
        "facts": facts,
        "source": "ai",
        "disclaimer": DISCLAIMER,
    }


FINDING_PROMPT = """You are a senior internal auditor. Rewrite the supplied aggregated
finding facts into concise, neutral audit language. Do not allege fraud and do not invent
evidence. Return ONLY JSON:
{
  "title": "Short finding title",
  "condition": "What was confirmed, including count and exposure.",
  "cause": "A cautious likely control cause that states when management validation is needed.",
  "risk": "The business and control risk.",
  "recommendation": "Specific corrective actions."
}"""


def polish_finding(facts: dict) -> dict | None:
    """Use one compact LLM call to improve a confirmed-findings report."""
    data = _chat_json(
        FINDING_PROMPT,
        "Aggregated confirmed-finding facts:\n" + json.dumps(facts, default=str),
        required_keys=["title", "condition", "cause", "risk", "recommendation"],
        max_tokens=900,
        max_retries=0,
    )
    if not data:
        return None
    return {
        **{key: str(data[key]) for key in ("title", "condition", "cause", "risk", "recommendation")},
        "facts": facts,
        "source": "ai",
        "disclaimer": DISCLAIMER,
    }


SUGGEST_PROMPT = """You are a P2P fraud analytics lead. You are given emergent data-driven
anomalies discovered by statistical profiling (not by any test the auditor wrote).
For each anomaly, propose a reusable test the auditor could generate.

Return ONLY valid JSON, no markdown:
{
  "suggestions": [
    {"detector_id": "...", "name": "short test name",
     "description": "one sentence on what it catches and why it matters",
     "prompt": "a clear plain-English instruction that a code generator can turn into a Python test using canonical UDM fields"}
  ]
}"""


def suggest_tests(emergent_clusters: list[dict]) -> dict:
    """Propose generatable tests from emergent anomalies. LLM-polished, with a
    deterministic fallback built from each detector's own seed prompt."""
    def _fallback():
        out = []
        for c in emergent_clusters:
            st = c.get("suggested_test")
            if st:
                out.append({
                    "detector_id": c.get("detector_id"),
                    "name": st.get("name", c["title"]),
                    "description": c.get("description", ""),
                    "prompt": st.get("prompt", ""),
                    "anomaly_title": c["title"],
                    "size": c["size"],
                })
        return {"suggestions": out, "disclaimer": DISCLAIMER, "source": "rules"}

    profile = [{
        "detector_id": c.get("detector_id"),
        "anomaly": c["title"],
        "means": c.get("description", ""),
        "size": c["size"],
        "amount_at_risk": c.get("amount_display"),
        "signals": c.get("evidence_chips", []),
        "seed_prompt": (c.get("suggested_test") or {}).get("prompt", ""),
    } for c in emergent_clusters]

    data = _chat_json(
        SUGGEST_PROMPT,
        "Anomalies:\n" + json.dumps({"anomalies": profile}, default=str),
        required_keys=["suggestions"],
        max_tokens=1600,
    )
    if not data or "suggestions" not in data or not isinstance(data["suggestions"], list):
        return _fallback()

    size_by_id = {c.get("detector_id"): c["size"] for c in emergent_clusters}
    title_by_id = {c.get("detector_id"): c["title"] for c in emergent_clusters}
    for s in data["suggestions"]:
        did = s.get("detector_id")
        s["size"] = size_by_id.get(did, 0)
        s["anomaly_title"] = title_by_id.get(did, s.get("name", ""))
    data["disclaimer"] = DISCLAIMER
    data["source"] = "ai"
    return data
