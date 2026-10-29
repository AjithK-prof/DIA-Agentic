"""
ERP Profile loader.
Loads pre-defined field mapping profiles for known ERP systems.
Each profile maps ERP-native column names -> UDM field names.
"""
import json
from pathlib import Path


_PROFILES_DIR = Path(__file__).parent.parent / "data" / "erp_profiles"

# Cache loaded profiles
_cache: dict[str, dict] = {}


def load_profile(erp_name: str) -> dict[str, str]:
    """
    Returns {source_column: udm_field} for the given ERP name.
    Case-insensitive ERP name matching.
    Returns empty dict if profile not found.
    """
    key = erp_name.strip().lower()
    if key in _cache:
        return _cache[key]

    for profile_path in _PROFILES_DIR.glob("*.json"):
        try:
            data = json.loads(profile_path.read_text(encoding="utf-8"))
            profile_key = data.get("erp", "").strip().lower()
            if profile_key == key:
                mappings = {k.strip(): v for k, v in data.get("mappings", {}).items()}
                _cache[key] = mappings
                return mappings
        except Exception:
            continue

    _cache[key] = {}
    return {}


def list_profiles() -> list[dict]:
    """Return summary of all available ERP profiles."""
    profiles = []
    for p in _PROFILES_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            profiles.append({
                "erp": data.get("erp"),
                "versions": data.get("versions", []),
                "module": data.get("module"),
                "column_count": len(data.get("mappings", {})),
            })
        except Exception:
            pass
    return profiles


def detect_erp_from_columns(columns: list[str]) -> tuple[str | None, float]:
    """
    Heuristic: match uploaded columns against each ERP profile.
    Returns (erp_name, match_ratio) for the best match, or (None, 0).
    """
    col_set = {c.strip().lower() for c in columns}
    best_erp = None
    best_ratio = 0.0

    for p in _PROFILES_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            erp = data.get("erp", "")
            profile_cols = {k.strip().lower() for k in data.get("mappings", {}).keys()}
            if not profile_cols:
                continue
            matched = col_set & profile_cols
            ratio = len(matched) / len(profile_cols)
            if ratio > best_ratio:
                best_ratio = ratio
                best_erp = erp
        except Exception:
            continue

    return (best_erp, round(best_ratio, 3)) if best_ratio > 0.2 else (None, 0.0)

