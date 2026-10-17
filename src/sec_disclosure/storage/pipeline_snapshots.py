"""Read complete pipeline inputs directly from PostgreSQL, without input files."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DISCLOSURE_FILES = ('disclosures.json', 'review_candidates.json', 'excluded_sources.json',
                    'unassigned_sources.json', 'token_usage.json', 'manifest.json')
ALIGNMENT_FILES = ('alignments.json', 'needs_review.json', 'manifest.json', 'token_usage.json')


class PipelineInputError(ValueError):
    """A database input is missing, incomplete, or unavailable."""


def file_document(path):
    path = Path(path)
    raw = path.read_bytes()
    return {'payload': json.loads(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
            'source_path': str(path.resolve())}


def publish(cur, company, kind, previous, current, documents):
    """Publish inside the caller's existing result-storage transaction."""
    cur.execute((ROOT / 'db/init/05_pipeline_stage_snapshots.sql').read_text())
    serialized = json.dumps(documents, sort_keys=True, ensure_ascii=False)
    revision = hashlib.sha256(serialized.encode()).hexdigest()
    cur.execute('''INSERT INTO pipeline_stage_snapshots
        (company, kind, previous_year, current_year, documents, revision)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s)
        ON CONFLICT (company, kind, previous_year, current_year) DO UPDATE SET
        documents=EXCLUDED.documents, revision=EXCLUDED.revision, saved_at=now()''',
        (company, kind, int(previous), int(current), serialized, revision))


def folder_documents(folder, names, *, include_change_cache=False):
    folder = Path(folder)
    paths = [folder / name for name in names if (folder / name).is_file()]
    if include_change_cache:
        paths.extend(sorted((folder / 'change_analysis_cache').glob('*.json')))
        paths.extend(sorted((folder / 'requests').glob('*.json')))
        paths.extend(path for path in (folder / 'materiality').glob('*.json') if path.is_file())
        paths.extend(sorted((folder / 'materiality/requests').glob('*.json')))
    return {str(path.relative_to(folder)): file_document(path) for path in paths}


def read_snapshots(company, scopes, *, env_file=None):
    """Read all requested scopes in one consistent, read-only transaction."""
    from .disclosures import connection_arguments
    import psycopg
    from psycopg.rows import dict_row
    result = {}
    try:
        with psycopg.connect(**connection_arguments(env_file), row_factory=dict_row) as conn:
            conn.read_only = True
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            with conn.cursor() as cur:
                for kind, previous, current in scopes:
                    cur.execute('''SELECT documents, revision FROM pipeline_stage_snapshots
                        WHERE company=%s AND kind=%s AND previous_year=%s AND current_year=%s''',
                        (company.lower(), kind, int(previous), int(current)))
                    row = cur.fetchone()
                    if row is None:
                        raise PipelineInputError(f'Missing PostgreSQL {kind} inputs for {company} {previous}-{current}. '
                            'Complete/import that stage first; existing JSON can be imported once with scripts/import_pipeline_snapshots.py.')
                    documents = row['documents']
                    serialized = json.dumps(documents, sort_keys=True, ensure_ascii=False)
                    if hashlib.sha256(serialized.encode()).hexdigest() != row['revision']:
                        raise PipelineInputError(f'PostgreSQL {kind} snapshot revision is inconsistent; reimport that stage.')
                    required = ([f'{previous}_chunks.json', f'{previous}_chunk_sentences.json'] if kind == 'raw'
                                else DISCLOSURE_FILES[:-1] if kind == 'disclosures' else ALIGNMENT_FILES[:3])
                    missing = set(required) - documents.keys()
                    if missing:
                        raise PipelineInputError(f'Incomplete PostgreSQL {kind} inputs for {company} {previous}-{current}: '
                                                 + ', '.join(sorted(missing)))
                    result[(kind, str(previous), str(current))] = {'documents': documents, 'revision': row['revision']}
        return result
    except PipelineInputError:
        raise
    except Exception as error:
        raise PipelineInputError(f'Cannot read PostgreSQL pipeline inputs ({type(error).__name__}). '
                                 'Check Docker/database settings and apply db/init/05_pipeline_stage_snapshots.sql. '
                                 'Local JSON is not used as a fallback.') from None


def alignment_snapshot(company, previous, current, *, env_file=None):
    key = ('alignments', str(previous), str(current))
    return read_snapshots(company, [key], env_file=env_file)[key]


def alignment_output(snapshot, output):
    """Preserve database report metadata in this stage's output/checkpoint folder."""
    from sec_disclosure.llm.disclosures import write_json
    output = Path(output)
    # The algorithm receives its input object separately. These are output
    # sidecars for atomic publication and existing checkpoint/report tools.
    for name, entry in snapshot['documents'].items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or relative.suffix != '.json':
            raise PipelineInputError('Invalid artifact path in PostgreSQL alignment snapshot.')
        if name != 'alignments.json':
            write_json(output / name, entry['payload'])
    return deepcopy(snapshot['documents']['alignments.json']['payload'])
