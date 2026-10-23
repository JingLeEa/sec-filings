from dotenv import load_dotenv
import psycopg

from sec_disclosure.storage.disclosures import connection_arguments

load_dotenv()

TABLES = [
    "filings",
    "alignment_runs",
    "alignments",
    "alignment_members",
    "pipeline_stage_snapshots",
    "xbrl_extractions",
    "xbrl_metrics",
    "xbrl_values",
    "xbrl_value_context",
]

with psycopg.connect(
    **connection_arguments(),
    autocommit=True,
) as conn:
    with conn.cursor() as cur:
        cur.execute("SET default_transaction_read_only = on")

        print("\n=== DATABASE RELATIONS ===")
        for table in TABLES:
            cur.execute(
                "SELECT to_regclass(%s)",
                (table,),
            )
            print(table, "=>", cur.fetchone()[0])

        print("\n=== AVAILABLE FILINGS ===")
        cur.execute("""
            SELECT company, fiscal_year, form_type
            FROM filings
            ORDER BY company, fiscal_year
            LIMIT 50
        """)
        for row in cur.fetchall():
            print(row)

        print("\n=== ALIGNMENTS COLUMNS ===")
        cur.execute("""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'alignments'
            ORDER BY ordinal_position
        """)
        for row in cur.fetchall():
            print(row)

        print("\n=== XBRL EXTRACTIONS ===")
        cur.execute("""
            SELECT f.company, f.fiscal_year,
                   e.extraction_id, e.loaded_at
            FROM xbrl_extractions e
            JOIN filings f USING (filing_id)
            ORDER BY f.company, f.fiscal_year
            LIMIT 50
        """)
        for row in cur.fetchall():
            print(row)

        print("\n=== MATERIALITY COUNTS ===")
        cur.execute("""
            SELECT
                extra->'materiality_analysis'->>'materiality'
                    AS materiality,
                COUNT(*)
            FROM alignments
            WHERE extra ? 'materiality_analysis'
            GROUP BY 1
            ORDER BY 1
        """)
        for row in cur.fetchall():
            print(row)
