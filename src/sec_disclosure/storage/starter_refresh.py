"""Explicit starter updates, limited to records still owned by the starter import."""
from collections import Counter
import hashlib
import json

from . import alignments, disclosures, peer_comparisons
from .artifacts import artifact_lock_key

PROVENANCE = "starter_data_import"
JSON_COLUMNS = {"sources", "verification", "extra", "evidence", "grouping",
                "validation_errors", "change_analysis"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def report_body(report):
    return {key: value for key, value in report.items() if key != PROVENANCE}


def tag_report(document):
    report = report_body(document["payload"])
    return {**report, PROVENANCE: {"source_path": document["source_path"], "sha256": digest(report)}}


def owned_report(report):
    origin = report.get(PROVENANCE) or {}
    return (isinstance(origin, dict) and str(origin.get("source_path", "")).startswith("starter_data/")
            and origin.get("sha256") == digest(report_body(report)))


def payloads(documents):
    return {name: entry["payload"] for name, entry in documents.items() if name != "starter_data.json"}


def normalized(row, excluded):
    return {key: json.loads(value) if key in JSON_COLUMNS and isinstance(value, str) else value
            for key, value in row.items() if key not in excluded}


def rows_digest(rows, excluded=()):
    # Member inserts ignore duplicate IDs, and disclosure upserts keep the last row.
    return digest(sorted({digest(normalized(row, excluded)) for row in rows}))


def stage_rows_match(cur, key, documents):
    kind, company, years, *_ = key
    if kind == "disclosures":
        expected = {}
        for name, status in (("disclosures.json", "accepted"), ("review_candidates.json", "review_candidate")):
            for row in documents.get(name, {}).get("payload", {}).get("disclosures", []):
                expected[row["disclosure_id"]] = disclosures.disclosure_row(row, 0, status)
        cur.execute("SELECT to_jsonb(d) - 'filing_id' FROM disclosures d JOIN filings f USING(filing_id) "
                    "WHERE f.company=%s AND f.fiscal_year=%s AND f.form_type='10-K'", (company, years[0]))
        actual = [row[0] for row in cur.fetchall()]
        return rows_digest(expected.values(), {"filing_id"}) == rows_digest(actual)
    report = documents["alignments.json"]["payload"]
    cur.execute("SELECT run_id,schema_version,run_complete,comparison_scope,note FROM alignment_runs "
                "WHERE company=%s AND previous_year=%s AND current_year=%s FOR UPDATE", (company, *years))
    run = cur.fetchone()
    if run is None or tuple(run[1:]) != (str(report["schema_version"]), bool(report.get("run_complete")),
                                       report.get("comparison_scope"), report.get("note")):
        return False
    expected, members = [], []
    for name in ("alignments.json", "needs_review.json"):
        for row in documents.get(name, {}).get("payload", {}).get("alignments", []):
            expected.append(alignments.alignment_row(row, 0, name.removesuffix(".json")))
            members.extend(alignments.member_rows(row, 0))
    cur.execute("SELECT to_jsonb(a) - 'run_id' FROM alignments a WHERE run_id=%s", (run[0],))
    actual = [row[0] for row in cur.fetchall()]
    cur.execute("SELECT to_jsonb(m) - 'run_id' FROM alignment_members m WHERE run_id=%s", (run[0],))
    actual_members = [row[0] for row in cur.fetchall()]
    return (rows_digest(expected, {"run_id"}) == rows_digest(actual)
            and rows_digest(members, {"run_id"}) == rows_digest(actual_members))


def current_payload(cur, key, known_digests=()):
    """Return the original payload only when its import provenance still matches."""
    kind, company, years, peers, items, selection = key
    if kind in {"disclosures", "alignments"}:
        cur.execute("SELECT documents FROM pipeline_stage_snapshots WHERE company=%s AND kind=%s "
                    "AND previous_year=%s AND current_year=%s FOR UPDATE",
                    (company, kind, years[0], years[-1]))
        found = cur.fetchone()
        if found is None:
            return None
        documents = found[0]
        marker = documents.get("starter_data.json", {}).get("payload", {})
        if marker.get("source") != "test-data" or not stage_rows_match(cur, key, documents):
            return None
        return payloads(documents)
    if kind == "synthesis":
        cur.execute("SELECT report,generated_at FROM synthesis_reports WHERE company=%s "
                    "AND years=%s::integer[] FOR UPDATE", (company.upper(), list(years)))
    else:
        cur.execute("SELECT report,saved_at FROM peer_comparison_reports WHERE target_ticker=%s "
                    "AND fiscal_year=%s AND peer_tickers=%s AND items=%s AND target_selection=%s FOR UPDATE",
                    (company.upper(), years[0], [p.upper() for p in peers], list(items), selection))
    found = cur.fetchone()
    if found is None:
        return None
    report, saved_at = found
    if PROVENANCE in report:
        return {"report": report_body(report)} if owned_report(report) else None
    if digest(report_body(report)) not in known_digests:
        return None
    # Legacy synthesis imports shared one transaction timestamp with their seeded
    # filing snapshots. A later generated/imported report has a different timestamp.
    if kind == "synthesis":
        cur.execute("SELECT 1 FROM pipeline_stage_snapshots WHERE company=%s AND kind='disclosures' "
                    "AND saved_at=%s AND documents->'starter_data.json'->'payload'->>'source'='test-data' LIMIT 1",
                    (company, saved_at))
    else:
        cur.execute("SELECT 1 FROM peer_comparison_runs WHERE target=%s AND fiscal_year=%s AND peers=%s "
                    "AND items=%s AND target_selection=%s AND source_saved_at=%s "
                    "AND source_file LIKE 'starter_data/%%' LIMIT 1",
                    (company, years[0], list(peers), list(items), selection, saved_at))
    return {"report": report_body(report)} if cur.fetchone() else None


def protect_companies(cur, scopes):
    """Share pipeline and review locks with their writers for the entire update."""
    companies = sorted({key[1] for key in scopes} | {peer for key in scopes for peer in key[3]})
    protected = {}
    for company in companies:
        cur.execute("SELECT pg_try_advisory_xact_lock(%s)",
                    (artifact_lock_key("main", "pipeline_runs/" + company),))
        if not cur.fetchone()[0]:
            protected[company] = "a pipeline is running"
    for kind, company, years, peers, _, _ in sorted(scopes):
        if kind in {"disclosures", "alignments"}:
            path = (f"manual_reviews/disclosures/{company}/{years[0]}/manual_reviews.json" if kind == "disclosures"
                    else f"manual_reviews/{company}/{years[0]}-{years[1]}/manual_reviews.json")
            cur.execute("SELECT pg_try_advisory_xact_lock(%s)",
                        (artifact_lock_key("human-reviews", "document:" + path),))
            if not cur.fetchone()[0]:
                protected[company] = "a human review is being saved"
        elif kind == "peer_comparison":
            path = f"peer_comparison/{company}/{years[0]}/{'-'.join(peers)}"
            cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (artifact_lock_key("peer", path),))
            if not cur.fetchone()[0]:
                protected.update({ticker: "a peer comparison is running" for ticker in (company, *peers)})
    cur.execute("SELECT to_regclass('pipeline_artifacts')")
    if cur.fetchone()[0]:
        cur.execute("SELECT path,content FROM pipeline_artifacts WHERE namespace='human-reviews' AND kind='file' "
                    "AND (path LIKE '%%/manual_reviews.json' OR path LIKE 'manual_reviews/pipeline_refresh/jobs/%%')")
        for path, content in cur.fetchall():
            record = json.loads(bytes(content))
            company = str(record.get("company", "")).lower()
            if record.get("reviews") or record.get("status") in {"queued", "running"}:
                if company in companies:
                    protected[company] = "human reviews or pending review updates exist"
        for company in companies:
            cur.execute("SELECT content FROM pipeline_artifacts WHERE kind='file' "
                        "AND path LIKE %s", (f"pipeline_runs/{company}/%/status.json",))
            if any(json.loads(bytes(row[0])).get("completion_source") != "starter_data" for row in cur.fetchall()):
                protected[company] = "your own pipeline progress exists"
    for company in companies:
        cur.execute("SELECT 1 FROM pipeline_stage_snapshots WHERE company=%s AND "
                    "(kind='raw' OR NOT (documents ? 'starter_data.json')) LIMIT 1", (company,))
        if cur.fetchone():
            protected[company] = "your own pipeline inputs or results exist"
    return protected


def replace_scope(cur, key, documents, manifest):
    from . import starter_data as seed
    kind, company, years, peers, items, selection = key
    if kind in {"disclosures", "alignments"}:
        marker = {"source": "test-data", "source_commit": manifest["source_commit"], "browse_only": True}
        documents["starter_data.json"] = {"payload": marker, "sha256": digest(marker),
                                         "source_path": "starter_data/manifest.json"}
        cur.execute("DELETE FROM pipeline_stage_snapshots WHERE company=%s AND kind=%s "
                    "AND previous_year=%s AND current_year=%s", (company, kind, years[0], years[-1]))
        if kind == "disclosures":
            # Retain filings and all raw/XBRL data. Only these starter disclosures
            # are replaced, after checking normalized rows against their snapshot.
            cur.execute("DELETE FROM disclosures WHERE filing_id IN (SELECT filing_id FROM filings "
                        "WHERE company=%s AND fiscal_year=%s AND form_type='10-K')", (company, years[0]))
            saved = seed.import_disclosures(cur, company, years[0], documents)
        else:
            cur.execute("DELETE FROM alignment_runs WHERE company=%s AND previous_year=%s AND current_year=%s",
                        (company, *years))
            saved = seed.import_alignments(cur, company, years, documents)
    else:
        if kind == "synthesis":
            cur.execute("DELETE FROM synthesis_reports WHERE company=%s AND years=%s::integer[]",
                        (company.upper(), list(years)))
        else:
            cur.execute("DELETE FROM peer_comparison_reports WHERE target_ticker=%s AND fiscal_year=%s "
                        "AND peer_tickers=%s AND items=%s AND target_selection=%s",
                        (company.upper(), years[0], [p.upper() for p in peers], list(items), selection))
        saved = seed.import_report(cur, kind, next(iter(documents.values())))
    if not saved:
        raise ValueError("A starter record changed during refresh; the update was rolled back.")


def refresh_existing(cur, bundle, manifest, available):
    from . import starter_data as seed
    wanted = {seed.scope_key(entry["scope"]) for entry in manifest["files"]} & available
    baselines = {seed.scope_key(entry["scope"]): entry.get("known_payload_sha256", [])
                 for entry in manifest["files"]}
    protected = protect_companies(cur, wanted)
    changed = set()
    # Validate the entire archive before replacing anything. Unknown provenance
    # protects the whole company so dependent stages cannot become inconsistent.
    for key, documents in seed.report_groups(bundle, manifest, wanted):
        if key[1] in protected or any(peer in protected for peer in key[3]):
            continue
        current = current_payload(cur, key, baselines.get(key, ()))
        incoming = payloads(documents) if key[0] in {"disclosures", "alignments"} else {
            "report": report_body(next(iter(documents.values()))["payload"])}
        if current is None:
            protected[key[1]] = "records were edited or their starter origin cannot be verified"
        elif digest(current) != digest(incoming):
            changed.add(key)
    changed = {key for key in changed if key[1] not in protected and not any(peer in protected for peer in key[3])}
    refreshed = Counter()
    for key, documents in seed.report_groups(bundle, manifest, changed) if changed else ():
        replace_scope(cur, key, documents, manifest)
        refreshed[key[0]] += 1
    protected_scopes = sum(key[1] in protected or any(peer in protected for peer in key[3]) for key in wanted)
    return {"refreshed": dict(refreshed), "unchanged": len(wanted) - protected_scopes - sum(refreshed.values()),
            "protected_scopes": protected_scopes, "protected_companies": dict(sorted(protected.items()))}
