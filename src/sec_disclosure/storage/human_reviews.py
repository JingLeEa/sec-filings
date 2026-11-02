"""Shared PostgreSQL home for human decisions and their refresh queue."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path

from .artifacts import PostgresWorkspace

NAMESPACE = "human-reviews"


@contextmanager
def document_lock(path):
    """Serialize a document's read/modify/write across app processes."""
    if not hasattr(path, "artifact_store"):
        yield
        return
    store = path.artifact_store
    key = int.from_bytes(hashlib.sha256(
        (NAMESPACE + ":document:" + path.artifact_key).encode()).digest()[:8], "big", signed=True)
    with store.connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
        yield


def workspace(env_file=None, *, prefix=None):
    result = PostgresWorkspace(NAMESPACE, env_file)
    if prefix is not None:
        if (not prefix.startswith("/__sec_postgres__/human-reviews-")
                or Path(prefix).parent != Path("/__sec_postgres__")):
            raise ValueError("Invalid human review workspace.")
        result.prefix = prefix
    return result


def migrate_local(root, destination):
    """Read old files once; merge missing decisions, retaining PostgreSQL choices.

    The marker is committed last, so interrupted imports can safely resume.
    Original files remain untouched. No file is used as a write fallback.
    """
    root = Path(root).expanduser().resolve()
    marker_id = hashlib.sha256(str(root).encode()).hexdigest()
    marker = destination / "__imports__" / (marker_id + ".json")
    with document_lock(marker):
        if marker.exists():
            return
        for source in sorted(root.rglob("manual_reviews.json")) if root.is_dir() else []:
            relative = source.relative_to(root)
            # Only the two known annotation layouts are migration inputs.
            parts = relative.parts
            if not (len(parts) == 3 or len(parts) == 4 and parts[0] == "disclosures"):
                continue
            incoming = json.loads(source.read_text(encoding="utf-8"))
            if incoming.get("schema_version") != 1 or not isinstance(incoming.get("reviews"), dict):
                raise ValueError("Invalid legacy human review document; migration stopped without deleting files.")
            target = destination / relative
            with document_lock(target):
                current = json.loads(target.read_text()) if target.exists() else None
                if current is not None:
                    if ({k: v for k, v in current.items() if k != "reviews"} !=
                            {k: v for k, v in incoming.items() if k != "reviews"}):
                        raise ValueError("Legacy and PostgreSQL human review scopes do not match.")
                    incoming["reviews"].update(current["reviews"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(incoming, ensure_ascii=False, indent=2) + "\n")
        for source in sorted((root / "pipeline_refresh/jobs").glob("*.json")):
            target = destination / "pipeline_refresh/jobs" / source.name
            with document_lock(target):
                if target.exists():
                    continue
                job = json.loads(source.read_text())
                settings = job["settings"]
                settings["reviews_dir"] = str(destination)
                if settings["input_source"] != "postgres":
                    settings["input_source"] = "postgres"
                    if job["status"] in {"queued", "running"}:
                        job.update(status="failed", completed_steps=[],
                                   phase="Review migrated; request a PostgreSQL refresh",
                                   error="The previous file refresh was paused during migration. Retry to refresh PostgreSQL.")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"source": str(root), "complete": True}) + "\n")
