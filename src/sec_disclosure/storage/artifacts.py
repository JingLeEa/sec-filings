"""Path-compatible PostgreSQL artifacts for the pipeline's existing algorithms.

Registered PostgreSQL roots and imported legacy aliases use this adapter.
Other paths keep normal pathlib behavior, including source/configuration files. Cache,
working artifacts and progress are committed to PostgreSQL on every write;
optional exports copy result files to a normal local directory.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path as LocalPath, PurePosixPath
import posixpath
import sqlite3
import stat
import zlib
from threading import RLock


ROOT = LocalPath(__file__).resolve().parents[3]
_registry = {}
_registry_lock = RLock()


class ArtifactStorageError(RuntimeError):
    """Durable pipeline storage failed; never fall back to a local cache."""


class PostgresArtifactStore:
    def __init__(self, namespace, env_file=None, *, read_only=False):
        self.namespace, self.env_file = namespace, env_file
        self.savers, self.saver_lock = {}, RLock()
        self.read_only = read_only

    @contextmanager
    def connection(self):
        import psycopg
        from .disclosures import connection_arguments
        try:
            with psycopg.connect(**connection_arguments(self.env_file)) as conn:
                if self.read_only:
                    conn.read_only = True
                yield conn
        except psycopg.Error:
            raise ArtifactStorageError("PostgreSQL cache/progress storage failed. Check the connection and pipeline_artifacts permissions; local storage is not used as a fallback.") from None

    def ensure_schema(self):
        with self.connection() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(80808008)')
            conn.execute((ROOT / 'db/init/08_pipeline_artifacts.sql').read_text())

    def get(self, path):
        with self.connection() as conn:
            if self.read_only and conn.execute("SELECT to_regclass('pipeline_artifacts')").fetchone()[0] is None:
                return None
            row = conn.execute("SELECT kind, content, extract(epoch FROM modified_at) FROM pipeline_artifacts WHERE namespace=%s AND path=%s",
                               (self.namespace, path)).fetchone()
        return None if row is None else (row[0], bytes(row[1]) if row[1] is not None else None, float(row[2]))

    def put(self, path, content=None, kind='file'):
        if self.read_only:
            raise ArtifactStorageError('Cannot write artifacts during a dry run.')
        with self.connection() as conn:
            conn.execute("""INSERT INTO pipeline_artifacts (namespace,path,kind,content)
                VALUES (%s,%s,%s,%s) ON CONFLICT (namespace,path) DO UPDATE SET
                kind=EXCLUDED.kind, content=EXCLUDED.content, modified_at=now()""",
                (self.namespace, path, kind, content))

    def entries(self, prefix):
        prefix = '' if prefix == '.' else prefix.rstrip('/') + '/'
        with self.connection() as conn:
            if self.read_only and conn.execute("SELECT to_regclass('pipeline_artifacts')").fetchone()[0] is None:
                return []
            return conn.execute("SELECT path,kind FROM pipeline_artifacts WHERE namespace=%s AND starts_with(path,%s) ORDER BY path",
                                (self.namespace, prefix)).fetchall()

    def files(self, prefix):
        """Read a directory's file contents in one consistent database query."""
        prefix = '' if prefix == '.' else prefix.rstrip('/') + '/'
        with self.connection() as conn:
            rows = conn.execute("SELECT path,content FROM pipeline_artifacts WHERE namespace=%s AND kind='file' AND starts_with(path,%s) ORDER BY path",
                                (self.namespace, prefix)).fetchall()
        return [(path, bytes(content)) for path, content in rows]

    def import_files(self, records):
        """Import bounded batches without replacing artifacts already in PostgreSQL."""
        if self.read_only:
            raise ArtifactStorageError('Cannot write artifacts during a dry run.')
        copied, batch, size = 0, [], 0

        def save(batch):
            directories = {str(parent) for key, _ in batch for parent in PurePosixPath(key).parents}
            query = "INSERT INTO pipeline_artifacts(namespace,path,kind,content) VALUES (%s,%s,%s,%s) ON CONFLICT(namespace,path) DO NOTHING"
            with self.connection() as conn, conn.cursor() as cursor:
                cursor.executemany(query, [(self.namespace, key, 'directory', None) for key in sorted(directories)])
                cursor.executemany(query, [(self.namespace, key, 'file', content) for key, content in batch])
                return cursor.rowcount

        for key, content in records:
            batch.append((key, content))
            size += len(content)
            if len(batch) >= 128 or size >= 16 * 1024 * 1024:
                copied += save(batch)
                batch, size = [], 0
        if batch:
            copied += save(batch)
        return copied

    def delete(self, path):
        if self.read_only:
            raise ArtifactStorageError('Cannot write artifacts during a dry run.')
        with self.connection() as conn:
            conn.execute("DELETE FROM pipeline_artifacts WHERE namespace=%s AND path=%s", (self.namespace, path))

    def move(self, source, destination):
        if self.read_only:
            raise ArtifactStorageError('Cannot write artifacts during a dry run.')
        with self.connection() as conn:
            rows = conn.execute("SELECT path,kind,content FROM pipeline_artifacts WHERE namespace=%s AND (path=%s OR starts_with(path,%s)) ORDER BY length(path)",
                                (self.namespace, source, source.rstrip('/') + '/')).fetchall()
            if not rows:
                raise FileNotFoundError(source)
            for path, kind, content in rows:
                target = destination + path[len(source):]
                conn.execute("""INSERT INTO pipeline_artifacts(namespace,path,kind,content) VALUES (%s,%s,%s,%s)
                    ON CONFLICT(namespace,path) DO UPDATE SET kind=EXCLUDED.kind,content=EXCLUDED.content,modified_at=now()""",
                    (self.namespace, target, kind, content))
            conn.execute("DELETE FROM pipeline_artifacts WHERE namespace=%s AND (path=%s OR starts_with(path,%s))",
                         (self.namespace, source, source.rstrip('/') + '/'))

    @contextmanager
    def lock(self, key):
        # Normalized stage tables are shared across cache profiles. Serialize
        # overlapping year selections/configurations for the same company.
        scope = 'main-pipeline' if key.startswith('pipeline_runs/') else self.namespace
        value = int.from_bytes(hashlib.sha256((scope + ':' + key).encode()).digest()[:8], 'big', signed=True)
        with self.connection() as conn:
            if not conn.execute('SELECT pg_try_advisory_lock(%s)', (value,)).fetchone()[0]:
                raise ValueError('This pipeline is already running. Wait for it to finish before rerunning.')
            conn.commit()  # Keep the session lock without holding an idle transaction.
            try:
                yield
            finally:
                conn.execute('SELECT pg_advisory_unlock(%s)', (value,))

    def close(self):
        with self.saver_lock:
            for saver in self.savers.values():
                saver.conn.close()
            self.savers.clear()


def _route(value):
    raw = str(value)
    with _registry_lock:
        for root, (store, prefix) in _registry.items():
            if raw == root or raw.startswith(root + '/'):
                normalized = posixpath.normpath(raw)
                if normalized != root and not normalized.startswith(root + '/'):
                    raise ValueError('Artifact path cannot leave its PostgreSQL workspace.')
                relative = normalized[len(root):].lstrip('/')
                return root, store, posixpath.join(prefix, relative).strip('/') or '.'
    return None


def canonical_artifact_path(value):
    """Identify an imported alias and its PostgreSQL path as the same artifact."""
    route = _route(value)
    if route is None:
        return str(value)
    with _registry_lock:
        root = next(root for root, (store, prefix) in _registry.items()
                    if root.startswith('/__sec_postgres__/') and store is route[1] and not prefix)
    return root if route[2] == '.' else root + '/' + route[2]


class _PathType(type):
    """Preserve existing isinstance(value, Path) checks for ordinary paths."""
    def __instancecheck__(cls, value):
        return isinstance(value, LocalPath)


class ArtifactPath(type(LocalPath()), metaclass=_PathType):
    """A pathlib path whose registered workspace operations go to PostgreSQL."""
    def __new__(cls, *args):
        value = LocalPath(*args)
        database = str(value).startswith('/__sec_postgres__/') or _route(value) is not None
        return super().__new__(cls, *args) if database else value

    @property
    def artifact_store(self):
        route = _route(self)
        if route is None:
            raise ArtifactStorageError('PostgreSQL artifact workspace is not active; refusing a local file operation.')
        return route[1]

    @property
    def artifact_key(self):
        route = _route(self)
        if route is None:
            raise ArtifactStorageError('PostgreSQL artifact workspace is not active; refusing a local file operation.')
        return route[2]

    def _entry(self):
        return self.artifact_store.get(self.artifact_key)

    def exists(self, **kwargs):
        return self._entry() is not None

    def is_file(self, **kwargs):
        row = self._entry()
        return row is not None and row[0] == 'file'

    def is_dir(self, **kwargs):
        row = self._entry()
        return row is not None and row[0] == 'directory'

    def is_symlink(self):
        return False

    def read_bytes(self):
        row = self._entry()
        if row is None:
            raise FileNotFoundError(str(self))
        if row[0] != 'file':
            raise IsADirectoryError(str(self))
        return row[1]

    def read_text(self, encoding=None, errors=None, **kwargs):
        return self.read_bytes().decode(encoding or 'utf-8', errors or 'strict')

    def write_bytes(self, data):
        data = bytes(data)
        if self.is_dir():
            raise IsADirectoryError(str(self))
        self.artifact_store.put(self.artifact_key, data)
        return len(data)

    def write_text(self, data, encoding=None, errors=None, newline=None):
        if newline is not None:
            data = data.replace('\n', newline)
        self.write_bytes(data.encode(encoding or 'utf-8', errors or 'strict'))
        return len(data)

    def mkdir(self, mode=0o777, parents=False, exist_ok=False):
        row = self._entry()
        if row is not None:
            if exist_ok and row[0] == 'directory':
                return
            raise FileExistsError(str(self))
        root, store, key = _route(self)
        if key != '.' and not self.parent.exists():
            if not parents:
                raise FileNotFoundError(str(self.parent))
            self.parent.mkdir(parents=True, exist_ok=True)
        store.put(key, None, 'directory')

    def iterdir(self):
        prefix = '' if self.artifact_key == '.' else self.artifact_key + '/'
        for key, kind in self.artifact_store.entries(self.artifact_key):
            relative = key[len(prefix):]
            if relative and '/' not in relative:
                yield self / relative

    def glob(self, pattern, **kwargs):
        prefix = '' if self.artifact_key == '.' else self.artifact_key + '/'
        patterns = [str(pattern)]
        if str(pattern).startswith('**/'):
            patterns.append(str(pattern)[3:])
        for key, kind in self.artifact_store.entries(self.artifact_key):
            relative = key[len(prefix):]
            if not relative or ('/' not in str(pattern) and '/' in relative):
                continue
            if any(PurePosixPath(relative).match(item) for item in patterns):
                yield self / relative

    def rglob(self, pattern, **kwargs):
        return self.glob('**/' + str(pattern), **kwargs)

    def unlink(self, missing_ok=False):
        if not self.exists() and not missing_ok:
            raise FileNotFoundError(str(self))
        self.artifact_store.delete(self.artifact_key)

    def rmdir(self):
        if any(self.iterdir()):
            raise OSError('Directory is not empty')
        self.unlink()

    def replace(self, target):
        target = ArtifactPath(target)
        if not isinstance(target, ArtifactPath) or not hasattr(target, 'artifact_store') or target.artifact_store is not self.artifact_store:
            raise ValueError('Use an explicit output export to copy PostgreSQL artifacts to local storage.')
        if target == self:
            return target
        self.artifact_store.move(self.artifact_key, target.artifact_key)
        return target

    rename = replace

    def resolve(self, strict=False):
        if strict and not self.exists():
            raise FileNotFoundError(str(self))
        return ArtifactPath(posixpath.normpath(str(self)))

    def absolute(self):
        return self.resolve()

    def expanduser(self):
        return self

    def stat(self, **kwargs):
        row = self._entry()
        if row is None:
            raise FileNotFoundError(str(self))
        mode = stat.S_IFDIR | 0o700 if row[0] == 'directory' else stat.S_IFREG | 0o600
        return os.stat_result((mode, 0, 0, 1, 0, 0, len(row[1] or b''), row[2], row[2], row[2]))

    lstat = stat

    def open(self, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
        writable = any(flag in mode for flag in 'wax+')
        present = self.exists()
        if 'x' in mode and present:
            raise FileExistsError(str(self))
        if 'r' in mode and not present:
            raise FileNotFoundError(str(self))
        data = self.read_bytes() if present and ('r' in mode or 'a' in mode) else b''
        path = self

        class Buffer(io.BytesIO):
            def flush(self):
                if writable and not self.closed:
                    path.write_bytes(self.getvalue())
                super().flush()

            def close(self):
                if not self.closed:
                    try:
                        self.flush()
                    finally:
                        super().close()

        stream = Buffer(data)
        if 'a' in mode:
            stream.seek(0, io.SEEK_END)
        if 'b' in mode:
            return stream
        return io.TextIOWrapper(stream, encoding=encoding or 'utf-8', errors=errors, newline=newline)


class PostgresWorkspace:
    def __init__(self, namespace, env_file=None, *, store=None, read_only=False):
        self.namespace = namespace
        self.store = store if store is not None else PostgresArtifactStore(namespace, env_file, read_only=read_only)
        self.prefix = '/__sec_postgres__/' + namespace
        self.read_only = read_only

    def __enter__(self):
        if not self.read_only:
            self.store.ensure_schema()
        with _registry_lock:
            if self.prefix in _registry:
                raise ArtifactStorageError('This PostgreSQL workspace is already active in this process.')
            _registry[self.prefix] = (self.store, '')
        self.aliases = {}
        try:
            aliases = self.store.get('__storage__/aliases.json')
            if aliases:
                saved = json.loads(aliases[1])
                if isinstance(saved, list):
                    saved = dict.fromkeys(saved, '')
                for alias, prefix in saved.items():
                    self._add_alias(alias, prefix)
            self.root = ArtifactPath(self.prefix)
            if not self.read_only:
                self.root.mkdir(parents=True, exist_ok=True)
            return self
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *exc):
        try:
            self.store.close()
        finally:
            with _registry_lock:
                _registry.pop(self.prefix, None)
                for alias in self.aliases:
                    _registry.pop(alias, None)

    def _add_alias(self, alias, prefix=''):
        with _registry_lock:
            if alias in _registry and _registry[alias] != (self.store, prefix):
                raise ArtifactStorageError('Legacy cache root belongs to another active workspace.')
            _registry[alias] = (self.store, prefix)
        self.aliases[alias] = prefix

    def import_local(self, source, *, prefixes=None, destination=None, marker='legacy'):
        """One-time, read-only import of old artifacts; original files are untouched."""
        source = LocalPath(source)
        marker_key = '__storage__/imports/' + marker + '.json'
        if self.read_only or self.store.get(marker_key) is not None or not source.is_dir():
            return False
        destination = destination or self.root
        candidates = []
        for prefix in prefixes or ['.']:
            folder = source / prefix
            if folder.is_dir() and not folder.is_symlink():
                candidates.extend(folder.rglob('*'))
        # An old runner must finish before its cache can be moved safely.
        import fcntl
        for lock in (path for path in candidates if path.name == '.run.lock'):
            with lock.open('rb') as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ArtifactStorageError('An older local pipeline is still running. Let it finish before starting the PostgreSQL-backed pipeline.') from None
        existing = {key for key, _ in self.store.entries(destination.artifact_key)}
        def records():
            for path in sorted(set(candidates)):
                if (not path.is_file() or path.is_symlink() or path.name == '.run.lock'
                        or path.suffix == '.tmp' or path.name.endswith(('-wal', '-shm'))):
                    continue
                # Replay request records instead of an incomplete SQLite WAL image.
                if path.suffix == '.sqlite' and (path.stat().st_size == 0 or path.with_name(path.name + '-wal').is_file()):
                    continue
                target = destination / path.relative_to(source)
                if target.artifact_key not in existing:
                    yield target.artifact_key, path.read_bytes()
        copied = self.store.import_files(records())
        if copied and destination == self.root:
            for prefix in prefixes or ['.']:
                folder = source / prefix
                if folder.is_dir():
                    key = '' if prefix == '.' else str(PurePosixPath(prefix))
                    self._add_alias(str(folder.absolute()), key)
                    self._add_alias(str(folder.resolve()), key)
            self.store.put('__storage__/aliases.json', json.dumps(self.aliases).encode())
        self.store.put(marker_key, json.dumps({'files_imported': copied}).encode())
        return copied > 0

    def export(self, destination, *, source=None, prefixes=None):
        source = source or self.root
        destination = LocalPath(destination)
        excluded = {'requests', 'jobs', 'cache', 'graph', 'archives', 'sec_cache', 'taxonomy_cache',
                    'change_analysis_cache', 'pipeline_runs', 'embeddings', 'validation_errors', '__storage__'}
        exported = 0
        for path in source.rglob('*'):
            relative = path.relative_to(source)
            name = relative.as_posix()
            if prefixes and not any(name == prefix or name.startswith(prefix) for prefix in prefixes):
                continue
            if (not path.is_file() or any(part in excluded for part in relative.parts)
                    or path.name in {'manifest.json', '.run.lock', 'run_state.json', 'status.json', 'verification.json'}
                    or path.suffix in {'.sqlite', '.tmp', '.db'}):
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
            exported += 1
        return exported


@contextmanager
def checkpoint_saver(database):
    """SQLite executes in memory; its complete state is committed to PostgreSQL."""
    from langgraph.checkpoint.sqlite import SqliteSaver
    database = ArtifactPath(database)
    if not hasattr(database, 'artifact_store'):
        with SqliteSaver.from_conn_string(str(database)) as saver:
            saver.conn.execute('PRAGMA busy_timeout = 60000')
            yield saver
        return
    store, key = database.artifact_store, database.artifact_key
    with store.saver_lock:
        saver = store.savers.get(key)
        if saver is None:
            class DurableConnection(sqlite3.Connection):
                def commit(self):
                    super().commit()
                    if hasattr(self, 'serialize'):
                        payload = b'SQLITEZ\n' + zlib.compress(self.serialize())
                    else:
                        payload = b'SQLDUMP\n' + '\n'.join(self.iterdump()).encode()
                    store.put(key, payload)

            conn = sqlite3.connect(':memory:', check_same_thread=False, factory=DurableConnection)
            database.parent.mkdir(parents=True, exist_ok=True)
            previous = store.get(key)
            if previous:
                payload = previous[1]
                if payload.startswith(b'SQLDUMP\n'):
                    conn.executescript(payload[8:].decode())
                else:
                    if payload.startswith(b'SQLITEZ\n'):
                        payload = zlib.decompress(payload[8:])
                    # Closed legacy WAL files contain complete pages but retain
                    # WAL read/write flags. In-memory SQLite cannot open a WAL;
                    # use rollback-journal flags for the deserialized image.
                    if payload.startswith(b'SQLite format 3\x00'):
                        payload = payload[:18] + b'\x01\x01' + payload[20:]
                    conn.deserialize(payload)
            conn.execute('PRAGMA temp_store=MEMORY')
            conn.execute('PRAGMA busy_timeout = 60000')
            saver = SqliteSaver(conn)
            store.savers[key] = saver
    yield saver
