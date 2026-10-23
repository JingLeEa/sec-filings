"""Retrieve source-grounded disclosure evidence for financial synthesis."""

from typing import Any


def _rows_as_dicts(cursor) -> list[dict[str, Any]]:
    """Convert database rows to dictionaries."""
    columns = [column.name for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def fetch_change_evidence(
    conn,
    company: str,
    previous_year: int,
    current_year: int,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """
    Retrieve disclosure changes, materiality assessments, members,
    and source-level evidence from PostgreSQL.

    Args:
        conn: An open psycopg database connection.
        company: Company ticker, e.g. 'amd'.
        previous_year: Earlier fiscal year.
        current_year: Later fiscal year.
        limit: Maximum number of alignments to retrieve.

    Returns:
        A list of structured change records.
    """

    # 1. Retrieve alignment records and their assessments.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                ar.run_id,
                ar.company,
                ar.previous_year,
                ar.current_year,
                a.match_id,
                a.status AS alignment_status,
                a.relationship,
                a.change_analysis->>'final_taxonomy' AS change_taxonomy,
                a.extra->'materiality_analysis' AS materiality_analysis,
                a.evidence
            FROM alignments AS a
            JOIN alignment_runs AS ar
              ON ar.run_id = a.run_id
            WHERE LOWER(ar.company) = LOWER(%s)
              AND ar.previous_year = %s
              AND ar.current_year = %s
              AND ar.run_complete = TRUE
            ORDER BY
                (a.extra->'materiality_analysis'
                    ->>'materiality_score')::numeric DESC NULLS LAST,
                a.match_id
            LIMIT %s
            """,
            (company, previous_year, current_year, limit),
        )
        changes = _rows_as_dicts(cur)

    if not changes:
        return []

    # 2. Retrieve the previous/current disclosure members.
    run_ids = list({change["run_id"] for change in changes})
    match_ids = list({change["match_id"] for change in changes})

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                am.run_id,
                am.match_id,
                am.side,
                am.disclosure_id,
                am.item,
                am.section,
                am.taxonomy,
                am.summary,
                am.extraction_status
            FROM alignment_members AS am
            WHERE am.run_id = ANY(%s)
              AND am.match_id = ANY(%s)
            ORDER BY am.match_id, am.side, am.disclosure_id
            """,
            (run_ids, match_ids),
        )
        members = _rows_as_dicts(cur)

    members_by_key: dict[tuple, list[dict[str, Any]]] = {}

    for member in members:
        key = (member["run_id"], member["match_id"])
        members_by_key.setdefault(key, []).append(
            {
                "side": member["side"],
                "disclosure_id": member["disclosure_id"],
                "item": member["item"],
                "section": member["section"],
                "taxonomy": member["taxonomy"],
                "summary": member["summary"],
                "extraction_status": member["extraction_status"],
            }
        )

    # 3. Flatten evidence arrays into individual source sentences.
    results = []

    for change in changes:
        key = (change["run_id"], change["match_id"])
        source_evidence = []

        evidence = change.get("evidence") or []

        if isinstance(evidence, list):
            for disclosure in evidence:
                if not isinstance(disclosure, dict):
                    continue

                disclosure_id = disclosure.get("disclosure_id")
                sentences = disclosure.get("sentences") or []

                if not isinstance(sentences, list):
                    continue

                for sentence in sentences:
                    if not isinstance(sentence, dict):
                        continue

                    source_evidence.append(
                        {
                            "disclosure_id": disclosure_id,
                            "sentence_id": sentence.get("sentence_id"),
                            "paragraph_id": sentence.get("paragraph_id"),
                            "text": sentence.get("text"),
                            "source_url": sentence.get("source_url"),
                        }
                    )

        materiality = change.get("materiality_analysis")

        # Missing assessment stays missing; it is not treated as "No".
        if not isinstance(materiality, dict):
            materiality = None

        results.append(
            {
                "company": change["company"],
                "previous_year": change["previous_year"],
                "current_year": change["current_year"],
                "match_id": change["match_id"],
                "alignment_status": change["alignment_status"],
                "relationship": change["relationship"],
                "taxonomy": change["change_taxonomy"],
                "materiality": materiality,
                "members": members_by_key.get(key, []),
                "source_evidence": source_evidence,
                "evidence_count": len(source_evidence),
            }
        )

    return results
