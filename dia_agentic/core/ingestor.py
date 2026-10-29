"""
Ingestor: reads CSV/Excel files robustly.
Handles: quoted commas, NULL-string normalization, duplicate headers, BOM.
Returns rows as list[dict] mapped to UDM fields.
"""
import csv
from pathlib import Path
from .schema_mapper import SchemaMapper
from .udm import InvoiceUDM
import dataclasses


NULL_STRINGS = {"null", "none", "na", "n/a", "not available", "not avaliable", "nan"}
UDM_FIELDS = {f.name for f in dataclasses.fields(InvoiceUDM)}


def _normalize_null(val: str) -> str | None:
    v = val.strip()
    if v.lower() in NULL_STRINGS or v == "":
        return None
    return v


def _deduplicate_headers(headers: list[str]) -> list[str]:
    """Append _2, _3 etc to duplicate header names."""
    seen: dict[str, int] = {}
    result = []
    for h in headers:
        if h in seen:
            seen[h] += 1
            result.append(f"{h}_{seen[h]}")
        else:
            seen[h] = 1
            result.append(h)
    return result


class IngestResult:
    def __init__(self):
        self.rows: list[dict] = []
        self.mapping_result: dict = {}
        self.raw_columns: list[str] = []
        self.total_rows: int = 0
        self.parse_errors: list[dict] = []

    def summary(self) -> dict:
        mapped_count = len(self.mapping_result.get("mapped", {}))
        unmapped = self.mapping_result.get("unmapped", [])
        methods = self.mapping_result.get("method", {})
        return {
            "total_rows": self.total_rows,
            "source_columns": len(self.raw_columns),
            "mapped_columns": mapped_count,
            "unmapped_columns": len(unmapped),
            "unmapped_list": unmapped,
            "mapping_methods": methods,
            "parse_errors": len(self.parse_errors),
        }


def ingest_csv(path: str | Path, use_llm_fallback: bool = False,
               erp_type: str | None = None) -> IngestResult:
    result = IngestResult()
    mapper = SchemaMapper(use_llm_fallback=use_llm_fallback, erp_type=erp_type)

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        raw_headers = next(reader)
        headers = _deduplicate_headers([h.strip() for h in raw_headers])
        result.raw_columns = headers

        # Map source columns to UDM
        mapping = mapper.map_columns(headers)
        result.mapping_result = mapping
        col_to_udm = mapping["mapped"]  # {source_col: udm_field}

        # Build reverse: for each UDM field, which source column to use (first win)
        udm_to_source: dict[str, str] = {}
        for src_col, udm_field in col_to_udm.items():
            if udm_field not in udm_to_source:
                udm_to_source[udm_field] = src_col

        for row_num, row in enumerate(reader, start=2):
            if len(row) != len(headers):
                result.parse_errors.append({
                    "row": row_num,
                    "issue": f"Expected {len(headers)} columns, got {len(row)}",
                })
                continue

            raw = dict(zip(headers, row))
            udm_row: dict = {f: None for f in UDM_FIELDS}

            for udm_field, src_col in udm_to_source.items():
                if src_col in raw:
                    udm_row[udm_field] = _normalize_null(raw[src_col])

            result.rows.append(udm_row)

    result.total_rows = len(result.rows)
    return result
