"""Private POSIX SQLite files in an operator-controlled directory."""

import os
import sqlite3
import stat


class UnsafeStoreError(sqlite3.OperationalError):
    pass


def _check(path, *, optional=False):
    for _ in range(3):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        except FileNotFoundError:
            if optional:
                return
            raise UnsafeStoreError("database file is missing") from None
        except OSError:
            raise UnsafeStoreError("database file cannot be safely opened") from None
        try:
            info = os.fstat(fd)
            # SQLite may unlink a journal between open and fstat. Reopen the path
            # and fully check any replacement; never accept a multiply linked file.
            if optional and info.st_nlink == 0:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UnsafeStoreError(
                    "database files must be regular files without additional hard links"
                )
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise UnsafeStoreError(
                    "database files must belong to the current user with owner-only permissions"
                )
            return
        finally:
            os.close(fd)
    raise UnsafeStoreError("database sidecar changed repeatedly during validation")


def private_database(path, *, create=False):
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise UnsafeStoreError("private database enforcement requires a POSIX host with O_NOFOLLOW")
    path = os.fspath(path)
    if not path or path == ":memory:":
        raise UnsafeStoreError("a persistent database file path is required")
    if create:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        except OSError:
            raise UnsafeStoreError("private database file cannot be created") from None
        else:
            os.close(fd)
    _check(path)
    for suffix in ("-journal", "-wal", "-shm"):
        _check(path + suffix, optional=True)
