"""
Schema Mapper - 5-tier column → UDM field resolution pipeline:

  1. ERP profile      exact match from known ERP system profile
  2. Registry exact   confirmed/inferred match from persistent SQLite registry
  3. Vector search    cosine similarity against all confirmed registry entries
  4. Fuzzy string     difflib against alias map (covers typos, abbreviations)
  5. Groq LLM         semantic fallback for truly unknown columns

Results at tiers 1-2 (confirmed) are trusted immediately.
Results at tiers 3-5 are saved as 'inferred' and flagged for human review.
"""
import json
import os
from difflib import get_close_matches
from pathlib import Path

from .erp_profiles import detect_erp_from_columns, load_profile
from .mapping_registry import get_registry
from .udm import COLUMN_ALIAS_MAP, InvoiceUDM
import dataclasses


UDM_FIELDS = {f.name for f in dataclasses.fields(InvoiceUDM)}

# Pre-normalize alias map keys so lookup works regardless of underscore/space/case
_NORMALIZED_ALIAS_MAP: dict[str, str] = {}


# ── Load .env ─────────────────────────────────────────────────────────────────
def _load_env():
    env_path = Path(__file__).parent.parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env()


def _normalize(name: str) -> str:
    return name.strip().lower().replace(" ", "").replace("-", "").replace("_", "")


def _get_normalized_alias_map() -> dict[str, str]:
    global _NORMALIZED_ALIAS_MAP
    if not _NORMALIZED_ALIAS_MAP:
        _NORMALIZED_ALIAS_MAP = {_normalize(k): v for k, v in COLUMN_ALIAS_MAP.items()}
    return _NORMALIZED_ALIAS_MAP


# ── Tier 4 – fuzzy alias match (returns top-N alternatives) ───────────────────

def _fuzzy_alternatives(raw: str, n: int = 5, threshold: float = 0.45) -> list[dict]:
    """Return up to n alternatives [{udm_field, confidence, reason}] from fuzzy match."""
    normalized = _normalize(raw)
    results = []
    seen = set()

    # Match against alias map (more generous threshold for alternatives)
    norm_alias = _get_normalized_alias_map()
    alias_matches = get_close_matches(normalized, list(norm_alias.keys()), n=n, cutoff=threshold)
    for m in alias_matches:
        field = norm_alias[m]
        if field not in seen:
            results.append({"udm_field": field, "confidence": 0.9, "reason": f"alias: {m}"})
            seen.add(field)

    # Match directly against UDM field names (no underscore)
    udm_list = sorted(UDM_FIELDS)
    udm_norms = [f.replace("_", "") for f in udm_list]
    udm_matches = get_close_matches(normalized, udm_norms, n=n, cutoff=threshold)
    for m in udm_matches:
        field = udm_list[udm_norms.index(m)]
        if field not in seen:
            results.append({"udm_field": field, "confidence": 0.75, "reason": f"name match"})
            seen.add(field)

    # Also try partial prefix/suffix matching against UDM field names
    raw_words = set(normalized.replace("/", " ").split())
    for field in udm_list:
        if field in seen:
            continue
        field_words = set(field.replace("_", " ").split())
        common = raw_words & field_words
        if common and len(common) / max(len(raw_words), 1) >= 0.4:
            results.append({"udm_field": field, "confidence": 0.6, "reason": f"partial word match"})
            seen.add(field)

    return results[:n]


def _fuzzy_match(raw: str, threshold: float = 0.75) -> tuple[str | None, float]:
    alts = _fuzzy_alternatives(raw, n=1, threshold=threshold)
    if alts:
        return alts[0]["udm_field"], alts[0]["confidence"]
    return None, 0.0


# ── Tier 5 – Groq LLM (returns top-N alternatives) ───────────────────────────

def _llm_alternatives_groq(raw_column: str, n: int = 3) -> list[dict]:
    """Return top-N UDM field alternatives from LLM (Foundry Local or Groq)."""
    # ── GROQ direct import (commented — replaced by llm_provider) ────────────
    # try:
    #     from groq import Groq
    # except ImportError:
    #     return []
    try:
        from dia_agentic.core.llm_provider import get_client as _get_llm_client
        _llm_client, _llm_model = _get_llm_client()
    except Exception:
        return []

    udm_list = "\n".join(f"- {f}" for f in sorted(UDM_FIELDS))
    prompt = f"""You are a data schema mapper for an invoice processing system.

Source column name from an ERP export: "{raw_column}"

Give the top {n} most likely matches from this list of canonical UDM field names:
{udm_list}

Respond ONLY with valid JSON array (no extra text):
[
  {{"udm_field": "<field>", "confidence": <0.0-1.0>, "reason": "<brief reason>"}},
  ...
]
Return fewer items if fewer are reasonable. Return [] if nothing matches."""

    # ── GROQ key-rotation loop (commented — llm_provider handles selection) ──
    # keys = [os.environ.get("GROQ_API_KEY", ""), os.environ.get("GROQ_API_KEY_2", "")]
    # for key in keys:
    #     if not key: continue
    #     client = Groq(api_key=key)
    try:
        resp = _llm_client.chat.completions.create(
            model=_llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=300,
        )
        content = resp.choices[0].message.content.strip()
        import re
        content = re.sub(r"<think>[\s\S]*?</think>", "", content).strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        candidates = json.loads(content)
        if not isinstance(candidates, list):
            candidates = [candidates]
        valid = [
            c for c in candidates
            if isinstance(c, dict) and c.get("udm_field") in UDM_FIELDS
            and float(c.get("confidence", 0)) >= 0.5
        ]
        return valid[:n]
    except Exception:
        return []


def _llm_batch_map_groq(columns: list[str]) -> dict[str, list[dict]]:
    """Single batched LLM call for multiple columns. Returns {col: [alternatives]}.
    Empty list means no confident UDM match — column stays unmapped in main table."""
    if not columns:
        return {}

    # ── GROQ direct import (commented — replaced by llm_provider) ────────────
    # try:
    #     from groq import Groq
    # except ImportError:
    #     return {}
    try:
        from dia_agentic.core.llm_provider import get_client as _get_llm_client
        _llm_client, _llm_model = _get_llm_client()
    except Exception:
        return {}

    udm_list = "\n".join(f"- {f}" for f in sorted(UDM_FIELDS))
    col_list = "\n".join(f'{i+1}. "{c}"' for i, c in enumerate(columns))
    prompt = (
        "You are a data schema mapper for an invoice/ERP processing system.\n\n"
        "Map each source column to the best UDM field(s) from the list below.\n"
        "RULES:\n"
        "- Many columns are derived analytics fields (fraud scores, ML probabilities, "
        "risk flags, review status, duplicate flags, test results, decision trees, etc.) "
        "that have NO equivalent in an invoice UDM. Return [] for those.\n"
        "- Only return a match if confidence >= 0.65. When in doubt, return [].\n\n"
        f"UDM fields:\n{udm_list}\n\n"
        f"Source columns:\n{col_list}\n\n"
        "Respond ONLY with a valid JSON object (no markdown fences, no extra text):\n"
        '{"<column_name>": [{"udm_field": "<field>", "confidence": 0.8, "reason": "brief"}], "<another>": []}'
    )

    # ── GROQ key-rotation loop (commented — llm_provider handles selection) ──
    # keys = [os.environ.get("GROQ_API_KEY", ""), os.environ.get("GROQ_API_KEY_2", "")]
    # for key in keys:
    #     if not key: continue
    #     client = Groq(api_key=key)
    try:
        resp = _llm_client.chat.completions.create(
            model=_llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=2000,
        )
        content = resp.choices[0].message.content.strip()
        content = re.sub(r"<think>[\s\S]*?</think>", "", content).strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        result = json.loads(content)
        if not isinstance(result, dict):
            return {}
        out = {}
        for col, alts in result.items():
            if not isinstance(alts, list):
                out[col] = []
                continue
            out[col] = [
                a for a in alts
                if isinstance(a, dict) and a.get("udm_field") in UDM_FIELDS
                and float(a.get("confidence", 0)) >= 0.65
            ][:3]
        return out
    except Exception:
        return {}


def _llm_map_groq(raw_column: str) -> tuple[str | None, str]:
    result = _llm_batch_map_groq([raw_column])
    alts = result.get(raw_column, [])
    if alts:
        c = alts[0]
        return c["udm_field"], f"LLM({c['confidence']:.0%}): {c.get('reason', '')}"
    return None, "LLM: no confident match"


# ── Main mapper ───────────────────────────────────────────────────────────────

class SchemaMapper:
    def __init__(self, use_llm_fallback: bool = False, erp_type: str | None = None,
                 module: str = "invoices", auto_detect_erp: bool = True):
        self.use_llm_fallback = use_llm_fallback
        self.erp_type = erp_type
        self.module = module
        self.auto_detect_erp = auto_detect_erp
        self._registry = get_registry()

    def map_columns(self, source_columns: list[str]) -> dict:
        """
        Maps source columns to UDM fields using the 5-tier pipeline.
        Returns:
          mapped        {source_col: udm_field}
          unmapped      [source_col]
          llm_suggested {source_col: {udm_field, reason}}
          method        {source_col: tier_label}
          status        {source_col: 'confirmed'|'inferred'|'unmapped'}
          erp_detected  str|None
          erp_confidence float
        """
        # Auto-detect ERP if not provided
        erp_type = self.erp_type
        erp_confidence = 0.0
        if not erp_type and self.auto_detect_erp:
            erp_type, erp_confidence = detect_erp_from_columns(source_columns)

        erp_profile = load_profile(erp_type) if erp_type else {}

        mapped: dict[str, str] = {}
        unmapped: list[str] = []
        llm_suggested: dict[str, dict] = {}
        method: dict[str, str] = {}
        status: dict[str, str] = {}
        alternatives: dict[str, list] = {}  # {source_col: [{udm_field, confidence, reason}]}

        for col in source_columns:
            resolved_field = None
            resolved_method = "unmapped"
            resolved_status = "unmapped"

            # Tier 1: ERP profile exact match
            if erp_profile and col.strip() in erp_profile:
                resolved_field = erp_profile[col.strip()]
                resolved_method = f"erp_profile({erp_type})"
                resolved_status = "confirmed"

            # Tier 2: Registry exact lookup
            if not resolved_field:
                reg_hit = self._registry.exact_lookup(col, erp_type=erp_type, module=self.module)
                if reg_hit:
                    resolved_field = reg_hit["udm_field"]
                    resolved_method = f"registry({reg_hit['status']})"
                    resolved_status = reg_hit["status"]

            # Tier 3: Vector similarity against confirmed registry entries (no write)
            if not resolved_field:
                vec_hit = self._registry.vector_search(col, module=self.module, threshold=0.70)
                if vec_hit:
                    resolved_field = vec_hit["udm_field"]
                    resolved_method = vec_hit["method"]
                    resolved_status = "inferred"

            # Tier 4: Fuzzy / alias match (no registry write — alias map is source of truth)
            col_alternatives = []
            if not resolved_field:
                norm = _normalize(col)
                if norm in _get_normalized_alias_map():
                    resolved_field = _get_normalized_alias_map()[norm]
                    resolved_method = "alias"
                    resolved_status = "confirmed"
                else:
                    primary_field, primary_conf = _fuzzy_match(col, threshold=0.75)
                    if primary_field:
                        fuzzy_alts = _fuzzy_alternatives(col, n=5, threshold=0.45)
                        resolved_field = primary_field
                        resolved_method = f"fuzzy({int(primary_conf*100)}%)"
                        resolved_status = "inferred"
                        col_alternatives = fuzzy_alts

            if resolved_field:
                mapped[col] = resolved_field
                method[col] = resolved_method
                status[col] = resolved_status
                if resolved_status == "inferred":
                    if not col_alternatives:
                        col_alternatives = _fuzzy_alternatives(col, n=5)
                    seen_alts = {a["udm_field"] for a in col_alternatives}
                    if resolved_field not in seen_alts:
                        col_alternatives.insert(0, {"udm_field": resolved_field, "confidence": 0.75, "reason": resolved_method})
                    alternatives[col] = col_alternatives
                    # Only save to registry if it's a genuinely new column not in alias map
                    # (i.e. arrived via vector search, not fuzzy/alias which are ephemeral)
            else:
                unmapped.append(col)
                method[col] = "unmapped"
                status[col] = "unmapped"

        # Tier 5: single batched LLM call for all still-unmapped columns
        if self.use_llm_fallback and unmapped:
            batch_result = _llm_batch_map_groq(unmapped)
            still_unmapped = []
            for col in unmapped:
                llm_alts = batch_result.get(col, [])
                if llm_alts:
                    # LLM found a confident match — queue for review, NOT auto-mapped
                    fuzzy_extra = _fuzzy_alternatives(col, n=3)
                    seen = {a["udm_field"] for a in llm_alts}
                    col_alternatives = list(llm_alts) + [a for a in fuzzy_extra if a["udm_field"] not in seen]
                    col_alternatives = col_alternatives[:5]
                    # Save as pending review but keep column as "unmapped" in main table
                    self._registry.upsert(col, llm_alts[0]["udm_field"], status="inferred",
                                          confidence=llm_alts[0].get("confidence", 0.7),
                                          method="llm", erp_type=erp_type, module=self.module,
                                          alternatives=col_alternatives)
                    alternatives[col] = col_alternatives
                    llm_suggested[col] = {"udm_field": llm_alts[0]["udm_field"],
                                          "reason": llm_alts[0].get("reason", "")}
                    # Still unmapped in main view — user must approve in review modal
                    still_unmapped.append(col)
                else:
                    still_unmapped.append(col)
            unmapped = still_unmapped

        return {
            "mapped": mapped,
            "unmapped": unmapped,
            "llm_suggested": llm_suggested,
            "method": method,
            "status": status,
            "alternatives": alternatives,
            "erp_detected": erp_type,
            "erp_confidence": erp_confidence,
        }
