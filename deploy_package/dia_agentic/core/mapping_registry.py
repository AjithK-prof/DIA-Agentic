"""
Mapping registry with pluggable storage backends.

Production path: PostgreSQL + pgvector (set DATABASE_URL and MAPPING_DB_BACKEND=pgvector).
Fallback path: SQLite local registry.
"""
import json
import math
import os
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Protocol

try:
    import psycopg
except Exception:  # pragma: no cover
    psycopg = None


DB_PATH = Path(__file__).parent.parent / "data" / "mapping_registry.db"
VECTOR_DIM = 256


def _normalize(name: str) -> str:
    return name.strip().lower()


def _tokenize(text: str) -> list[str]:
    t = _normalize(text).replace("_", " ").replace("-", " ").replace("/", " ")
    words = [w for w in t.split() if w]
    ngrams = []
    for w in words:
        for n in (2, 3):
            ngrams += [w[i:i + n] for i in range(len(w) - n + 1)]
    return words + ngrams


def _embed_dense(text: str, dim: int = VECTOR_DIM) -> list[float]:
    tokens = _tokenize(text)
    vec = [0.0] * dim
    if not tokens:
        return vec
    for tok in tokens:
        idx = hash(tok) % dim
        vec[idx] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _cosine_dense(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a)) or 1.0
    mag_b = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (mag_a * mag_b)


def _to_pgvector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"


class RegistryProtocol(Protocol):
    backend: str

    def exact_lookup(self, source_column: str, erp_type: str | None = None, module: str = "invoices") -> dict | None: ...
    def vector_search(self, source_column: str, module: str = "invoices", threshold: float = 0.55) -> dict | None: ...
    def list_pending(self, module: str = "invoices") -> list[dict]: ...
    def list_all(self, module: str = "invoices") -> list[dict]: ...
    def upsert(self, source_column: str, udm_field: str, status: str = "inferred", confidence: float = 0.0,
               method: str = "", erp_type: str | None = None, module: str = "invoices",
               approved_by: str | None = None) -> int: ...
    def approve(self, mapping_id: int, approved_by: str = "system") -> bool: ...
    def reject(self, mapping_id: int, approved_by: str = "system") -> bool: ...
    def stats(self, module: str = "invoices") -> dict: ...


class SQLiteMappingRegistry:
    backend = "sqlite"

    def __init__(self, db_path: Path = DB_PATH):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._init_db()

    def _init_db(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS column_mappings (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                source_column TEXT NOT NULL,
                source_norm   TEXT NOT NULL,
                erp_type      TEXT,
                module        TEXT DEFAULT 'invoices',
                udm_field     TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'inferred',
                confidence    REAL DEFAULT 0.0,
                method        TEXT,
                vector        TEXT,
                alternatives  TEXT DEFAULT '[]',
                approved_by   TEXT,
                approved_at   TEXT,
                created_at    TEXT DEFAULT (datetime('now')),
                UNIQUE(source_norm, erp_type, module)
            )
        """)
        # add alternatives column to existing DBs that don't have it yet
        try:
            self.conn.execute("ALTER TABLE column_mappings ADD COLUMN alternatives TEXT DEFAULT '[]'")
            self.conn.commit()
        except Exception:
            pass
        self.conn.commit()

    def exact_lookup(self, source_column: str, erp_type: str | None = None, module: str = "invoices") -> dict | None:
        norm = _normalize(source_column)
        erp = (erp_type or "").strip().upper()
        row = self.conn.execute(
            """SELECT source_column, udm_field, status, confidence, method
               FROM column_mappings
               WHERE source_norm = ? AND (erp_type = ? OR erp_type IS NULL OR erp_type = '')
                 AND module = ? AND status = 'confirmed'
               ORDER BY confidence DESC
               LIMIT 1""",
            (norm, erp, module),
        ).fetchone()
        if not row:
            return None
        return {"source": row[0], "udm_field": row[1], "status": row[2], "confidence": row[3], "method": row[4]}

    def vector_search(self, source_column: str, module: str = "invoices", threshold: float = 0.55) -> dict | None:
        query = _embed_dense(source_column)
        rows = self.conn.execute(
            """SELECT source_column, udm_field, vector
               FROM column_mappings
               WHERE status='confirmed' AND module=? AND vector IS NOT NULL""",
            (module,),
        ).fetchall()
        best = None
        best_score = 0.0
        for src, udm, vector_json in rows:
            try:
                score = _cosine_dense(query, json.loads(vector_json))
                if score > best_score:
                    best_score = score
                    best = {"source": src, "udm_field": udm, "status": "confirmed", "confidence": round(score, 3),
                            "method": f"vector({score:.0%})"}
            except Exception:
                continue
        return best if best_score >= threshold else None

    def list_pending(self, module: str = "invoices") -> list[dict]:
        rows = self.conn.execute(
            """SELECT id, source_column, erp_type, udm_field, confidence, method, created_at, alternatives
               FROM column_mappings WHERE status='inferred' AND module=? ORDER BY created_at DESC""",
            (module,),
        ).fetchall()
        result = []
        for r in rows:
            try:
                alts = json.loads(r[7] or "[]")
            except Exception:
                alts = []
            # Ensure primary suggestion is always first in alternatives list
            primary = {"udm_field": r[3], "confidence": r[4], "reason": r[5] or ""}
            if not alts or alts[0].get("udm_field") != r[3]:
                alts = [primary] + [a for a in alts if a.get("udm_field") != r[3]]
            result.append({
                "id": r[0], "source_column": r[1], "erp_type": r[2], "udm_field": r[3],
                "confidence": r[4], "method": r[5], "created_at": r[6], "alternatives": alts,
            })
        return result

    def list_all(self, module: str = "invoices") -> list[dict]:
        rows = self.conn.execute(
            """SELECT id, source_column, erp_type, udm_field, status, confidence, method, approved_by, created_at
               FROM column_mappings WHERE module=?
               ORDER BY CASE status WHEN 'confirmed' THEN 0 WHEN 'inferred' THEN 1 ELSE 2 END, created_at DESC""",
            (module,),
        ).fetchall()
        return [{"id": r[0], "source_column": r[1], "erp_type": r[2], "udm_field": r[3], "status": r[4],
                 "confidence": r[5], "method": r[6], "approved_by": r[7], "created_at": r[8]} for r in rows]

    def upsert(self, source_column: str, udm_field: str, status: str = "inferred", confidence: float = 0.0,
               method: str = "", erp_type: str | None = None, module: str = "invoices",
               approved_by: str | None = None, alternatives: list | None = None) -> int:
        norm = _normalize(source_column)
        erp = (erp_type or "").strip().upper()
        vector_json = json.dumps(_embed_dense(source_column))
        alternatives_json = json.dumps(alternatives or [])
        approved_at = datetime.utcnow().isoformat() if approved_by else None
        self.conn.execute(
            """INSERT INTO column_mappings
               (source_column, source_norm, erp_type, module, udm_field, status, confidence, method, vector, alternatives, approved_by, approved_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_norm, erp_type, module)
               DO UPDATE SET udm_field=excluded.udm_field,status=excluded.status,confidence=excluded.confidence,
                             method=excluded.method,vector=excluded.vector,alternatives=excluded.alternatives,
                             approved_by=excluded.approved_by,approved_at=excluded.approved_at""",
            (source_column, norm, erp, module, udm_field, status, confidence, method, vector_json,
             alternatives_json, approved_by, approved_at),
        )
        self.conn.commit()
        row = self.conn.execute("SELECT id FROM column_mappings WHERE source_norm=? AND erp_type=? AND module=?",
                                (norm, erp, module)).fetchone()
        return row[0] if row else -1

    def approve(self, mapping_id: int, approved_by: str = "system") -> bool:
        cur = self.conn.execute(
            "UPDATE column_mappings SET status='confirmed', approved_by=?, approved_at=datetime('now') WHERE id=?",
            (approved_by, mapping_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def reject(self, mapping_id: int, approved_by: str = "system") -> bool:
        cur = self.conn.execute(
            "UPDATE column_mappings SET status='rejected', approved_by=?, approved_at=datetime('now') WHERE id=?",
            (approved_by, mapping_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def stats(self, module: str = "invoices") -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) FROM column_mappings WHERE module=? GROUP BY status",
            (module,),
        ).fetchall()
        return {r[0]: r[1] for r in rows}


class PgVectorMappingRegistry:
    backend = "pgvector"

    def __init__(self, database_url: str):
        if psycopg is None:
            raise RuntimeError("psycopg is not installed")
        self.conn = psycopg.connect(database_url, autocommit=True)
        self._init_db()

    def _init_db(self):
        with self.conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS column_mappings (
                    id BIGSERIAL PRIMARY KEY,
                    source_column TEXT NOT NULL,
                    source_norm   TEXT NOT NULL,
                    erp_type      TEXT,
                    module        TEXT DEFAULT 'invoices',
                    udm_field     TEXT NOT NULL,
                    status        TEXT NOT NULL DEFAULT 'inferred',
                    confidence    DOUBLE PRECISION DEFAULT 0.0,
                    method        TEXT,
                    vector        VECTOR({VECTOR_DIM}),
                    approved_by   TEXT,
                    approved_at   TIMESTAMPTZ,
                    created_at    TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(source_norm, erp_type, module)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cm_status_module ON column_mappings(status, module)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cm_norm_module ON column_mappings(source_norm, module)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_cm_vector ON column_mappings USING ivfflat (vector vector_cosine_ops)")

    def exact_lookup(self, source_column: str, erp_type: str | None = None, module: str = "invoices") -> dict | None:
        norm = _normalize(source_column)
        erp = (erp_type or "").strip().upper()
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT source_column, udm_field, status, confidence, method
                   FROM column_mappings
                   WHERE source_norm=%s AND (erp_type=%s OR erp_type IS NULL OR erp_type='')
                     AND module=%s AND status = 'confirmed'
                   ORDER BY confidence DESC
                   LIMIT 1""",
                (norm, erp, module),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {"source": row[0], "udm_field": row[1], "status": row[2], "confidence": row[3], "method": row[4]}

    def vector_search(self, source_column: str, module: str = "invoices", threshold: float = 0.55) -> dict | None:
        vec_literal = _to_pgvector_literal(_embed_dense(source_column))
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT source_column, udm_field, (1 - (vector <=> %s::vector)) AS similarity
                   FROM column_mappings
                   WHERE status='confirmed' AND module=%s AND vector IS NOT NULL
                   ORDER BY vector <=> %s::vector
                   LIMIT 1""",
                (vec_literal, module, vec_literal),
            )
            row = cur.fetchone()
        if not row:
            return None
        similarity = float(row[2] or 0.0)
        if similarity < threshold:
            return None
        return {"source": row[0], "udm_field": row[1], "status": "confirmed",
                "confidence": round(similarity, 3), "method": f"vector({similarity:.0%})"}

    def list_pending(self, module: str = "invoices") -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT id, source_column, erp_type, udm_field, confidence, method, created_at
                   FROM column_mappings WHERE status='inferred' AND module=%s ORDER BY created_at DESC""",
                (module,),
            )
            rows = cur.fetchall()
        return [{"id": r[0], "source_column": r[1], "erp_type": r[2], "udm_field": r[3],
                 "confidence": r[4], "method": r[5], "created_at": str(r[6])} for r in rows]

    def list_all(self, module: str = "invoices") -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute(
                """SELECT id, source_column, erp_type, udm_field, status, confidence, method, approved_by, created_at
                   FROM column_mappings WHERE module=%s
                   ORDER BY CASE status WHEN 'confirmed' THEN 0 WHEN 'inferred' THEN 1 ELSE 2 END, created_at DESC""",
                (module,),
            )
            rows = cur.fetchall()
        return [{"id": r[0], "source_column": r[1], "erp_type": r[2], "udm_field": r[3], "status": r[4],
                 "confidence": r[5], "method": r[6], "approved_by": r[7], "created_at": str(r[8])} for r in rows]

    def upsert(self, source_column: str, udm_field: str, status: str = "inferred", confidence: float = 0.0,
               method: str = "", erp_type: str | None = None, module: str = "invoices",
               approved_by: str | None = None) -> int:
        norm = _normalize(source_column)
        erp = (erp_type or "").strip().upper()
        vec_literal = _to_pgvector_literal(_embed_dense(source_column))
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO column_mappings
                   (source_column, source_norm, erp_type, module, udm_field, status, confidence, method, vector, approved_by, approved_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,%s)
                   ON CONFLICT(source_norm, erp_type, module)
                   DO UPDATE SET udm_field=excluded.udm_field,status=excluded.status,confidence=excluded.confidence,
                                 method=excluded.method,vector=excluded.vector,approved_by=excluded.approved_by,approved_at=excluded.approved_at
                   RETURNING id""",
                (source_column, norm, erp, module, udm_field, status, confidence, method,
                 vec_literal, approved_by, datetime.utcnow() if approved_by else None),
            )
            row = cur.fetchone()
        return int(row[0]) if row else -1

    def approve(self, mapping_id: int, approved_by: str = "system") -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE column_mappings SET status='confirmed', approved_by=%s, approved_at=NOW() WHERE id=%s",
                (approved_by, mapping_id),
            )
            return cur.rowcount > 0

    def reject(self, mapping_id: int, approved_by: str = "system") -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE column_mappings SET status='rejected', approved_by=%s, approved_at=NOW() WHERE id=%s",
                (approved_by, mapping_id),
            )
            return cur.rowcount > 0

    def stats(self, module: str = "invoices") -> dict:
        with self.conn.cursor() as cur:
            cur.execute("SELECT status, COUNT(*) FROM column_mappings WHERE module=%s GROUP BY status", (module,))
            rows = cur.fetchall()
        return {r[0]: r[1] for r in rows}


_registry: RegistryProtocol | None = None


def get_registry() -> RegistryProtocol:
    global _registry
    if _registry is not None:
        return _registry

    backend = (os.environ.get("MAPPING_DB_BACKEND") or "").strip().lower()
    database_url = os.environ.get("DATABASE_URL", "").strip()
    prefer_pg = backend == "pgvector" or bool(database_url)

    if prefer_pg and database_url:
        try:
            _registry = PgVectorMappingRegistry(database_url)
            return _registry
        except Exception:
            pass

    _registry = SQLiteMappingRegistry()
    return _registry

