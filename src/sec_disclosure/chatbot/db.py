"""PostgreSQL access for the chatbot: read-only, time-limited, credentials from connection_arguments()."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..storage.chatbot_schema import SCHEMAS, apply_outdated, outdated
from ..storage.disclosures import connection_arguments

ROOT = Path(__file__).resolve().parents[3]
CHATBOT_SCHEMA = ROOT / "db" / "init" / "09_chatbot.sql"
PEER_SCHEMA = ROOT / "db" / "init" / "10_chatbot_peer_comparisons.sql"
STATEMENT_TIMEOUT_MS = 15000


class DatabaseError(RuntimeError):
    """Failure whose message is safe to show (no host, password or connection string)."""


@dataclass
class QueryResult:
    name: str
    sql: str
    params: Sequence[Any] | dict | None
    columns: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    elapsed_ms: float | None = None

    def as_records(self) -> list[dict]:
        return self.rows


@dataclass(frozen=True)
class Run:
    run_id: int
    company: str
    previous_year: int
    current_year: int


class Database:
    """One reused read-only connection; every query runs in its own READ ONLY transaction.

    Reusing the connection matters on Windows: each new TCP connection to "localhost" can cost ~2 s there
    (IPv6 ::1 is tried first and Docker only listens on 127.0.0.1), and one answer runs 10-20 queries.
    For the same reason "localhost" is replaced by 127.0.0.1, which is what docker-compose.yml binds.
    """

    def __init__(self, env_file: Path | None = None, *, statement_timeout_ms: int = STATEMENT_TIMEOUT_MS):
        self._arguments = dict(connection_arguments(env_file))
        if self._arguments.get("host") == "localhost":
            self._arguments["host"] = "127.0.0.1"
        self.statement_timeout_ms = statement_timeout_ms
        self._runs: list[Run] | None = None
        self._conn = None
        self._lock = threading.Lock()          # Streamlit may call from more than one thread
        self.connect_ms: float | None = None   # time the last connection took to open
        self.peer_sync = None                  # result of the last peer-report sync (None: nothing to sync)

    def _connect(self, *, read_only: bool):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError:
            raise DatabaseError("psycopg is not installed. Run: pip install -r requirements.txt") from None
        try:
            conn = psycopg.connect(**self._arguments, row_factory=dict_row)
        except Exception as error:  # psycopg messages can include the host; keep only the type.
            raise DatabaseError(f"Cannot connect to PostgreSQL ({type(error).__name__}). Is `docker compose up -d` "
                                "running and are POSTGRES_* / DATABASE_URL correct?") from None
        conn.read_only = read_only
        return conn

    def _reader(self):
        if self._conn is None or self._conn.closed or self._conn.broken:
            started = time.perf_counter()
            self._conn = self._connect(read_only=True)
            self.connect_ms = (time.perf_counter() - started) * 1000
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None and not self._conn.closed:
                self._conn.close()
            self._conn = None

    def query(self, name: str, sql: str, params: Sequence[Any] | dict | None = None) -> QueryResult:
        """Run one SELECT inside a READ ONLY transaction with a statement timeout."""
        with self._lock:
            for attempt in (1, 2):
                conn = self._reader()
                started = time.perf_counter()
                try:
                    with conn.transaction(), conn.cursor() as cur:
                        cur.execute(f"SET LOCAL statement_timeout = {int(self.statement_timeout_ms)}")
                        cur.execute(sql, params)
                        columns = [c.name for c in cur.description] if cur.description else []
                        rows = cur.fetchall() if cur.description else []
                    break
                except Exception as error:
                    if conn.broken and attempt == 1:   # server restarted / idle connection dropped: reconnect once
                        self._conn = None
                        continue
                    message = str(error).split("\n")[0][:300]
                    raise DatabaseError(f"Query '{name}' failed: {type(error).__name__}: {message}") from None
        return QueryResult(name=name, sql=sql, params=params, columns=columns, rows=list(rows),
                           elapsed_ms=(time.perf_counter() - started) * 1000)

    def ensure_schema(self) -> bool:
        """Apply chatbot/agent schema files whose recorded version is missing or old (see storage.chatbot_schema),
        then bring the chatbot's peer-comparison tables up to date with peer_comparison_reports."""
        rows = self.query("schema_versions", "SELECT key, value FROM schema_info WHERE key = ANY(%s)",
                          ([key for _, key, _ in SCHEMAS],)).rows
        stale = outdated({r["key"]: r["value"] for r in rows})
        if stale:
            self._write(lambda cur: apply_outdated(cur), "Could not update the chatbot schema. Apply "
                        + ", ".join(str(p.relative_to(ROOT)) for p in stale) + " manually.")
        self.sync_peer_reports()
        return bool(stale)

    def sync_peer_reports(self):
        """Rebuild the chatbot's copy of peer reports added or re-saved by the peer comparison pipeline."""
        from ..storage.peer_comparisons import peer_sync_needed, sync_peer_reports
        result = None

        def sync(cur):
            nonlocal result
            if peer_sync_needed(cur):
                result = sync_peer_reports(cur)
        self._write(sync, "Could not sync peer comparisons from peer_comparison_reports. Run "
                          "scripts/load_peer_comparisons_to_postgres.py or check the database.")
        self.peer_sync = result
        return result

    def _write(self, action, message: str) -> None:
        from psycopg.rows import tuple_row
        conn = self._connect(read_only=False)
        try:
            with conn, conn.cursor(row_factory=tuple_row) as cur:   # storage helpers read rows by position
                action(cur)
        except Exception as error:
            raise DatabaseError(f"{message} ({type(error).__name__})") from None
        finally:
            conn.close()

    def runs(self, refresh: bool = False) -> list[Run]:
        if self._runs is None or refresh:
            rows = self.query("runs", "SELECT run_id, company, previous_year, current_year FROM alignment_runs "
                                      "ORDER BY company, previous_year").rows
            self._runs = [Run(**row) for row in rows]
        return self._runs

    def run_overview(self) -> list[dict]:
        return self.query("run_overview", """
            SELECT r.company, r.previous_year, r.current_year,
                   count(a.match_id) AS alignments,
                   count(*) FILTER (WHERE a.status = 'needs_review') AS needs_review,
                   count(*) FILTER (WHERE a.extra ? 'materiality_analysis') AS with_materiality
            FROM alignment_runs r LEFT JOIN alignments a USING (run_id)
            GROUP BY 1, 2, 3 ORDER BY 1, 2""").rows

    def synthesis_overview(self) -> list[dict]:
        """Loaded synthesis reports (empty when the synthesis views do not exist yet)."""
        if self.query("synthesis_check",
                      "SELECT to_regclass('public.chatbot_synthesis_reports') AS t").rows[0]["t"] is None:
            return []
        return self.query("synthesis_overview", """
            SELECT r.company, r.start_year || '-' || r.end_year AS years,
                   (SELECT count(*) FROM chatbot_synthesis_findings f WHERE f.report_key = r.report_key) AS findings,
                   (SELECT count(*) FROM chatbot_synthesis_trends t WHERE t.report_key = r.report_key) AS trends,
                   to_char(r.loaded_at, 'YYYY-MM-DD HH24:MI') AS generated
            FROM chatbot_synthesis_reports r ORDER BY r.company, r.start_year""").rows

    def peer_run_overview(self) -> list[dict]:
        """Loaded peer-comparison runs (empty when the peer tables do not exist yet)."""
        if self.query("peer_check", "SELECT to_regclass('public.peer_comparison_runs') AS t").rows[0]["t"] is None:
            return []
        return self.query("peer_run_overview", """
            SELECT target, fiscal_year AS year, array_to_string(peers, ', ') AS peers,
                   CASE WHEN target_selection = 'material_changes' THEN 'material' ELSE 'all' END AS scope,
                   jobs_completed || '/' || jobs_planned AS done,
                   (SELECT count(*) FROM peer_comparisons c WHERE c.peer_run_id = r.peer_run_id) AS comparisons
            FROM peer_comparison_runs r ORDER BY target, fiscal_year""").rows

    def embedding_sources(self) -> dict[str, int]:
        """How many vectors each pgvector source holds (0 when the table is missing)."""
        counts = {}
        for key, table, condition in (("evidence", "chatbot_evidence_embeddings", "TRUE"),
                                      ("disclosures", "disclosure_embeddings", "field = 'content'")):
            exists = self.query("table_check", "SELECT to_regclass(%s) AS t", (f"public.{table}",)).rows[0]["t"]
            counts[key] = int(self.query(f"{table}_count", f"SELECT count(*) AS n FROM {table} WHERE {condition}")
                              .rows[0]["n"]) if exists else 0
        return counts

    def existing_match_ids(self, match_ids: Sequence[str]) -> set[str]:
        if not match_ids:
            return set()
        rows = self.query("match_id_check", "SELECT match_id FROM alignments WHERE match_id = ANY(%s)",
                          (list(match_ids),)).rows
        return {r["match_id"] for r in rows}
