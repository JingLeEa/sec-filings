"""Validation for LLM-written SQL before it reaches the database.

Defence in depth: these checks reject anything but a single read-only SELECT over the allowed tables, and
the query then runs in a READ ONLY transaction with a statement timeout (db.Database.query), wrapped in an
outer LIMIT. Either layer alone would stop writes; together they also keep the model away from system
catalogs and slow, unbounded scans.
"""
from __future__ import annotations

import re

ALLOWED_TABLES = {
    "alignment_changes", "alignments", "alignment_runs", "alignment_members", "disclosures", "filings",
    "chunks", "sentences", "sentence_context", "sentence_embeddings", "disclosure_embeddings",
    "chatbot_evidence_embeddings", "embedding_models",
}
FORBIDDEN_KEYWORDS = re.compile(
    r"\b(insert|update|delete|merge|upsert|drop|alter|create|truncate|grant|revoke|copy|call|do|execute|prepare|"
    r"deallocate|vacuum|analyze|cluster|reindex|lock|listen|notify|unlisten|set|reset|comment|refresh|import|"
    r"security|owner|into|begin|commit|rollback|savepoint|discard|load)\b", re.IGNORECASE)
FORBIDDEN_FUNCTIONS = re.compile(
    r"\b(pg_\w+|lo_\w+|dblink\w*|set_config|current_setting|query_to_xml\w*|table_to_xml\w*|"
    r"database_to_xml\w*|xpath)\s*\(", re.IGNORECASE)
MAX_ROWS = 200


class UnsafeSQL(ValueError):
    pass


def _strip_literals(sql: str) -> str:
    """Remove string literals and quoted identifiers so their contents are not mistaken for SQL."""
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)
    sql = re.sub(r"\$(\w*)\$.*?\$\1\$", "''", sql, flags=re.DOTALL)
    return sql


def validate_sql(sql: str) -> str:
    """Return a normalised, LIMIT-wrapped query or raise UnsafeSQL with a reason the LLM can act on."""
    if not sql or not sql.strip():
        raise UnsafeSQL("empty query")
    text = re.sub(r"--[^\n]*", " ", sql)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL).strip()
    text = re.sub(r";\s*$", "", text)
    bare = re.sub(r'\bas\s+"[^"]*"', "AS alias", _strip_literals(text), flags=re.IGNORECASE)  # quoted aliases are fine
    if '"' in bare:
        raise UnsafeSQL("quoted identifiers are not allowed; use plain lowercase names")
    if ";" in bare:
        raise UnsafeSQL("only one statement is allowed")
    if not re.match(r"^\s*(select|with)\b", bare, re.IGNORECASE):
        raise UnsafeSQL("query must start with SELECT or WITH")
    keyword = FORBIDDEN_KEYWORDS.search(bare)
    if keyword:
        raise UnsafeSQL(f"keyword '{keyword.group(1)}' is not allowed in a read-only query")
    function = FORBIDDEN_FUNCTIONS.search(bare)
    if function:
        raise UnsafeSQL(f"function '{function.group(1)}' is not allowed")
    if re.search(r"\b(?:pg_catalog|information_schema)\b", bare, re.IGNORECASE):
        raise UnsafeSQL("system catalogs are not allowed")

    ctes = {m.lower() for m in re.findall(r"(?:\bwith|,)\s*(?:recursive\s+)?([a-z_]\w*)\s+as\s*(?:not\s+)?(?:materialized\s+)?\(",
                                          bare, re.IGNORECASE)}
    for match in re.finditer(r"\b(?:from|join)\s+(?:lateral\s+|only\s+)?([a-z_][\w.]*)\s*(\()?", bare, re.IGNORECASE):
        name, is_call = match.group(1).lower(), match.group(2)
        if is_call:
            continue  # set-returning function such as jsonb_array_elements(...) or unnest(...)
        table = name.split(".")[-1] if name.startswith("public.") else name
        if "." in table:
            raise UnsafeSQL(f"schema-qualified name '{name}' is not allowed")
        if table not in ALLOWED_TABLES and table not in ctes:
            raise UnsafeSQL(f"table '{name}' is not available; use alignment_changes or the documented tables")
    # Comma joins: FROM a, b
    for match in re.finditer(r"\bfrom\s+([a-z_]\w*)(?:\s+(?:as\s+)?\w+)?\s*,\s*([a-z_]\w*)\b(?!\s*\()", bare, re.IGNORECASE):
        for name in match.groups():
            if name.lower() not in ALLOWED_TABLES and name.lower() not in ctes:
                raise UnsafeSQL(f"table '{name}' is not available")
    return f"SELECT * FROM (\n{text}\n) AS chatbot_query LIMIT {MAX_ROWS}"
