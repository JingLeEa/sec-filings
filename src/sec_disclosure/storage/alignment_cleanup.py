"""Apply the one-sided review exclusion to already stored final comparisons."""
from collections import defaultdict
import hashlib
import json

from sec_disclosure.alignment_policy import exclude_from_reports
from .pipeline_snapshots import publish


def replace_document(documents, name, payload):
    raw = (json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode()
    documents[name] = {"payload": payload, "sha256": hashlib.sha256(raw).hexdigest(),
                       "source_path": documents.get(name, {}).get("source_path", name)}


def cleanup_existing(cur, *, pending=None):
    """Idempotent cleanup in the caller's transaction; never touch human notes.

    Skip a company owned by a live pipeline. Its final writer applies the same
    policy. Rows, cascading members/embeddings and snapshots change atomically.
    """
    cur.execute("""SELECT a.run_id, r.company, r.previous_year, r.current_year, a.match_id
        FROM alignments a JOIN alignment_runs r USING (run_id)
        LEFT JOIN alignment_members m ON m.run_id=a.run_id AND m.match_id=a.match_id
        WHERE r.run_complete AND a.status='needs_review'
        GROUP BY a.run_id, r.company, r.previous_year, r.current_year, a.match_id
        HAVING count(DISTINCT m.side)<2 ORDER BY r.company, a.run_id, a.match_id""")
    candidates = defaultdict(list)
    for run_id, company, previous, current, match_id in cur.fetchall():
        candidates[(run_id, company, previous, current)].append(match_id)
    locked, removed = {}, 0
    for (run_id, company, previous, current), ids in candidates.items():
        if company not in locked:
            lock = int.from_bytes(hashlib.sha256(("main-pipeline:pipeline_runs/" + company).encode()).digest()[:8],
                                  "big", signed=True)
            cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (lock,))
            locked[company] = cur.fetchone()[0]
        if not locked[company]:
            if pending is not None:
                pending.add(company)
            continue
        cur.execute("SELECT run_id FROM alignment_runs WHERE run_id=%s FOR UPDATE", (run_id,))
        cur.execute("SELECT documents FROM pipeline_stage_snapshots WHERE company=%s AND kind='alignments' "
                    "AND previous_year=%s AND current_year=%s FOR UPDATE", (company, previous, current))
        snapshot = cur.fetchone()
        if snapshot is not None:
            documents = snapshot[0]
            reports = {name: documents[name]["payload"] for name in ("alignments.json", "needs_review.json")
                       if name in documents}
            if "alignments.json" in reports and exclude_from_reports(reports):
                for name, payload in reports.items():
                    replace_document(documents, name, payload)
                publish(cur, company, "alignments", previous, current, documents)
        # Recheck the predicate after locking; another process may have completed a match.
        cur.execute("""DELETE FROM alignments a WHERE a.run_id=%s AND a.match_id=ANY(%s)
            AND a.status='needs_review' AND (
                NOT EXISTS (SELECT 1 FROM alignment_members m WHERE m.run_id=a.run_id AND m.match_id=a.match_id AND m.side='previous')
                OR NOT EXISTS (SELECT 1 FROM alignment_members m WHERE m.run_id=a.run_id AND m.match_id=a.match_id AND m.side='current'))""",
                    (run_id, ids))
        removed += cur.rowcount
    return removed
