"""Small platform adapters shared by pipeline entry points and storage."""
from contextlib import contextmanager
import errno
import hashlib
import os
import time


def source_digest(path):
    """Fingerprint source code independently of Git's checkout line endings."""
    return hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()


def atomic_replace(source, target):
    """Retry Windows sharing violations while publishing a completed file."""
    for attempt in range(6):
        try:
            return source.replace(target)
        except PermissionError as error:
            if os.name != 'nt' or getattr(error, 'winerror', None) not in {5, 32, 33} or attempt == 5:
                raise
            time.sleep(0.01 * 2 ** attempt)


@contextmanager
def exclusive_file_lock(handle):
    """Take a nonblocking lock, including on empty legacy lock files."""
    position = handle.tell()
    handle.seek(0)
    if os.name == 'nt':
        import msvcrt
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise BlockingIOError(error.errno, 'File is already locked') from error
            raise
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            handle.seek(position)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.seek(position)
