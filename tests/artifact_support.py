"""In-memory database double for testing pipelines without disk caches."""
from contextlib import contextmanager
from pathlib import PurePosixPath
from threading import Lock, RLock
import time

from sec_disclosure.storage.artifacts import PostgresWorkspace


class MemoryArtifactStore:
    def __init__(self):
        self.rows, self.savers, self.locks = {}, {}, {}
        self.saver_lock = RLock()
        self.mutex = RLock()

    def ensure_schema(self):
        pass

    def get(self, key):
        with self.mutex:
            return self.rows.get(key)

    def put(self, key, data=None, kind='file'):
        with self.mutex:
            self.rows[key] = (kind, data, time.time())

    def entries(self, prefix):
        prefix = '' if prefix == '.' else prefix + '/'
        with self.mutex:
            return [(key, row[0]) for key, row in sorted(self.rows.items()) if key.startswith(prefix)]

    def files(self, prefix):
        prefix = '' if prefix == '.' else prefix.rstrip('/') + '/'
        with self.mutex:
            return [(key, row[1]) for key, row in sorted(self.rows.items())
                    if key.startswith(prefix) and row[0] == 'file']

    def import_files(self, records):
        copied = 0
        for key, content in records:
            with self.mutex:
                if key in self.rows:
                    continue
                for parent in PurePosixPath(key).parents:
                    self.rows.setdefault(str(parent), ('directory', None, time.time()))
                self.put(key, content)
                copied += 1
        return copied

    def delete(self, key):
        with self.mutex:
            self.rows.pop(key, None)

    def move(self, source, target):
        with self.mutex:
            keys = [key for key in self.rows if key == source or key.startswith(source + '/')]
            if not keys:
                raise FileNotFoundError(source)
            for key in keys:
                self.rows[target + key[len(source):]] = self.rows.pop(key)

    @contextmanager
    def lock(self, key):
        with self.mutex:
            lock = self.locks.setdefault(key, Lock())
        if not lock.acquire(blocking=False):
            raise ValueError('Pipeline already running')
        try:
            yield
        finally:
            lock.release()

    def close(self):
        for saver in self.savers.values():
            saver.conn.close()
        self.savers.clear()

    def path(self, key):
        return StoredView(self, key)


class StoredView:
    """Inspect a stored artifact outside an active pipeline context."""
    def __init__(self, store, key):
        self.artifact_store, self.artifact_key = store, key

    @property
    def parent(self):
        return StoredView(self.artifact_store, str(PurePosixPath(self.artifact_key).parent))

    def mkdir(self, **kwargs):
        self.artifact_store.put(self.artifact_key, None, 'directory')

    def exists(self):
        return self.artifact_store.get(self.artifact_key) is not None

    def read_bytes(self):
        row = self.artifact_store.get(self.artifact_key)
        if row is None:
            raise FileNotFoundError(self.artifact_key)
        return row[1]

    def read_text(self, **kwargs):
        return self.read_bytes().decode()

    def write_text(self, value, **kwargs):
        self.artifact_store.put(self.artifact_key, value.encode())


def workspace_factory(store):
    return lambda namespace, env_file=None, **kwargs: PostgresWorkspace(namespace, store=store, **kwargs)
