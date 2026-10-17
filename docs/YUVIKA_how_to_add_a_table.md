# YUVIKA: How to add a table to the database

**Maintainer:** YUVIKA — contact YUVIKA for database questions.

> Note: this is my first time using Docker. Everything here was tested on macOS with Docker Desktop; Windows and Linux are untested. If something looks wrong or could be done better, please tell me.

Read [YUVIKA_database_docker_setup.md](YUVIKA_database_docker_setup.md) first for starting the database. This page is for teammates who want their stage's output (JSON) stored in it.

## 1. What is already there

| Table | Holds | Loaded by |
| --- | --- | --- |
| `filings`, `chunks`, `sentences`, `sentence_embeddings` | Text preprocessing output | `scripts/load_to_postgres.py` |
| `disclosures` | Extracted disclosures (`disclosures.json` and `review_candidates.json`, schema "3") | `scripts/load_disclosures_to_postgres.py` |
| `alignment_runs`, `alignments`, `alignment_members` | Alignment results (`alignments.json` and `needs_review.json`, schema "8") | `scripts/load_alignments_to_postgres.py` |
| `disclosure_embeddings` | Vectors for whole disclosures (content, summary, section), from the semantic-retrieval embedding cache | `scripts/load_disclosure_embeddings.py` |

The `disclosures` and alignment rows are for Zi Yang's stages and are described in section 2; `disclosure_embeddings` is described in section 3. The `example_items` table in the templates is only a demo; do not create it on the shared database.

## 2. Loading the disclosure and alignment tables (Zi Yang)

**Step 1: update your database once.** The new tables are in `db/init/02_disclosures_alignments.sql`. Docker only runs files in `db/init` when the database is first created, so on a database that already exists, apply the file by hand. The file can be run more than once safely.

Mac / Linux:

```bash
docker compose exec -T db psql -U sec -d sec_disclosure < db/init/02_disclosures_alignments.sql
```

Windows PowerShell:

```powershell
Get-Content db\init\02_disclosures_alignments.sql | docker compose exec -T db psql -U sec -d sec_disclosure
```

Or start from scratch (this deletes everything loaded so far, which you can load again): `docker compose down -v` then `docker compose up -d`.

**Step 2: load alignments.** The AMD results are already in the repo, so you can try it straight away:

```bash
.venv/bin/python scripts/load_alignments_to_postgres.py --company amd --previous-year 2024 --current-year 2025 --dir tests/fixtures/alignments/amd/2024-2025
```

For your own runs, drop `--dir`; the default folder is `data/alignments/<company>/<previous>-<current>/`. Add `--dry-run` to only check the files. Rerunning replaces that comparison's rows completely, because match IDs change between fresh model runs.

**Step 3: load disclosures.**

```bash
.venv/bin/python scripts/load_disclosures_to_postgres.py --company amd --year 2025
```

Use `--disclosures-dir data/disclosures_run2` if your extraction output is in another folder. The folder must contain `<company>/<year>/disclosures.json` and `review_candidates.json`.

**Step 4: look at the data.**

```bash
docker compose exec db psql -P pager=off -U sec -d sec_disclosure
```

```sql
-- how many decisions of each kind
SELECT r.previous_year, r.current_year, a.status, count(*)
FROM alignments a JOIN alignment_runs r USING (run_id)
WHERE r.company = 'amd' GROUP BY 1, 2, 3 ORDER BY 1, 3;

-- disclosures that are new in 2025
SELECT m.disclosure_id, m.taxonomy, left(m.summary, 80) AS summary
FROM alignments a
JOIN alignment_runs r USING (run_id)
JOIN alignment_members m USING (run_id, match_id)
WHERE r.company = 'amd' AND r.previous_year = 2024 AND r.current_year = 2025
  AND a.unmatched_type = 'introduced_disclosure'
ORDER BY m.disclosure_id;

-- alignment members with their full disclosure text (needs the disclosures loaded)
SELECT a.match_id, a.relationship, m.side, d.taxonomy, left(d.content, 80)
FROM alignments a
JOIN alignment_members m USING (run_id, match_id)
JOIN disclosures d ON d.disclosure_id = m.disclosure_id
LIMIT 10;
```

Type `\q` to leave.

### What the tables contain

**`alignment_runs`**: one row per company and year pair (`company`, `previous_year`, `current_year`, `schema_version`, `run_complete`, `comparison_scope`, `note`).

**`alignments`**: one row per decision, from both `alignments.json` and `needs_review.json` (`source_file` says which). Key is (`run_id`, `match_id`). Columns: `status`, `relationship`, `match_method`, `explanation`, `verifier_review_reason`, `review_reasons[]`, `unmatched_type`, `unmatched_notes[]`; `evidence`, `grouping`, `validation_errors` and `change_analysis` are JSON exactly as delivered; anything unrecognised goes to `extra`. Later stages should update `change_analysis` for a row.

**`alignment_members`**: one row per disclosure and side (`previous` or `current`) in each alignment, with the metadata that comes with the alignment (`item`, `section`, `taxonomy`, `summary`, `extraction_status`). `disclosure_id` is deliberately not a foreign key, so alignments can be loaded before disclosures.

**`disclosures`**: `disclosure_id` (key), `filing_id`, `item`, `section`, `sections[]`, `topic`, `summary`, `content`, `taxonomy` (must be one of the nine labels), `review_status` (`accepted` from `disclosures.json`, `review_candidate` from `review_candidates.json`), `sources`, `verification` and `extra` as JSON.

### What is verified and what is not

- The alignment loader was run on the real AMD 2023-2024 and 2024-2025 fixtures. The loaded counts match the alignment guide exactly: 199 / 106 / 71 / 34 (`ai_verified` / `auto_matched` / `unmatched` / `needs_review`) and 238 / 71 / 82 / 51. Rerunning does not duplicate rows.
- The disclosure loader was written from the field list in the extraction guide and tested only on a small made-up file that follows it, because the real `disclosures.json` files are not in the repo. **Please run it once on a real file and tell me if anything fails**; the most likely problems are a taxonomy label that is not one of the nine, or a missing field.
- Not stored as columns yet: the link between a disclosure and its source sentences (`sources[]` is kept as JSON). It can become a table once the shape of `sources` is confirmed.

## 3. Embeddings (Aarav)

Semantic retrieval embeds whole extracted disclosures, not single sentences, using `bge-m3` (1024 dimensions) through SoCLaaS. It embeds three texts per disclosure: the original `content`, the generated `summary` and the `section` name. The vectors stay in the on-disk cache that code already writes (`data/embeddings`, one file per model and text). This stage only copies them into the database; it creates no embeddings and makes no API calls.

Order: load the disclosures first (section 2, step 3), then:

```bash
# once, on an existing database
docker compose exec -T db psql -U sec -d sec_disclosure < db/init/03_disclosure_embeddings.sql

.venv/bin/python scripts/load_disclosure_embeddings.py --company amd --year 2025
```

Options: `--embeddings-cache PATH` (default `data/embeddings`), `--model NAME` (default `bge-m3`, must exist in `embedding_models`), `--fields content summary section` to load only some. It prints how many vectors were loaded and how many texts were not in the cache for each field. Missing ones are normal, for example a disclosure with an empty summary is never embedded. A vector whose length differs from the model's registered size stops the load.

**`disclosure_embeddings`**: key (`disclosure_id`, `model_id`, `field`); `field` is `content`, `summary` or `section`; `embedding` is `vector(1024)`; a cosine-distance search index exists for the `content` vectors. Deleting a disclosure deletes its vectors. Example, the closest disclosures to one disclosure by content:

```sql
SELECT e.disclosure_id, e.embedding <=> q.embedding AS distance
FROM disclosure_embeddings e
CROSS JOIN (SELECT embedding FROM disclosure_embeddings
            WHERE disclosure_id = 'amd_2025_1A_D001' AND field = 'content') q
WHERE e.field = 'content'
ORDER BY distance LIMIT 5;
```

Tested with made-up vectors in the right format and size, not real `bge-m3` output. The older `sentence_embeddings` table (one vector per sentence) is kept but unused until the team decides sentence-level vectors are needed.

## 4. Adding your own table (template)

You need three things: a SQL file that creates the table, a small script that loads your JSON, and a short note in your docs.

1. **Create the table.** Copy `db/templates/TEMPLATE_new_table.sql` to `db/init/04_<your_stage>.sql`. Rename `example_items` and change the columns to match your JSON (one column per field you will filter on; everything else can go in the `extra` JSON column). Keep `IF NOT EXISTS` so it is safe to run twice.
2. **Apply it** to your running database with the command in section 2, step 1 (using your file name).
3. **Copy the loader.** Copy `scripts/templates/TEMPLATE_load_table.py` to `scripts/load_<your_stage>_to_postgres.py`. Change three things marked `CHANGE`: the table name, the list of known fields, and `record_row()` / `UPSERT` so they match your columns.
4. **Try it.** Run it with `--dry-run` first, then without. Run it twice: the row count should not change, which means reruns do not create duplicates. Check with `SELECT count(*) FROM your_table;`.
5. **Document it.** Add the table's columns, how to run the loader and a small example file under `tests/fixtures/` to your stage's doc, as the backend template asks.

Tips:

- Use the ID your pipeline already creates as the primary key. Then reloading updates rows instead of duplicating them.
- If rows belong to a company and year, add `filing_id BIGINT REFERENCES filings(filing_id)` (see `disclosures`) so they can be joined to the text tables.
- If two runs can produce the same ID for different things, include the run in the key (see `alignments`).
- Keep checks in the table definition (`CHECK`, `NOT NULL`) for values that must be one of a fixed list, so wrong data fails loudly instead of being stored.
- To change a table that already exists, the simplest way is `docker compose down -v`, edit the SQL, `docker compose up -d`, and reload. This deletes all loaded data, so only do it on your own copy.
- Each teammate has their own database on their own computer. Loading data on your laptop does not put it on anyone else's.
