# Private database storage

The gateway stores missions, action arguments, approval state and audit history in SQLite. It now creates a new database exclusively with mode `0600`, independent of a permissive shell umask. Existing files are never automatically chmodded or replaced.

Before each SQLite connection, the gateway checks the database and any existing `-journal`, `-wal` and `-shm` files. They must be regular files owned by the current operating-system user, with no group/other permission bits and exactly one hard link. Final-component symbolic links are refused. A missing database after initialization causes failure instead of silently creating a new ledger. Unsafe storage produces a sanitized HTTP 503 response before an effect is attempted.

This implementation requires a POSIX host with `O_NOFOLLOW`, such as macOS or Linux. Native Windows is unsupported. Use an operator-controlled directory whose path components cannot be replaced by untrusted users. These checks do not enforce directory ownership or extended ACLs, do not prevent a privileged user from reading data, and do not encrypt files. There is a gap between checking a file and SQLite opening its path; trusted directory control is essential.

## Existing databases

Older installations may have database files with mode `0644`. Stop all gateway processes and inspect the database, directory ownership, file type, links and any sidecars before restarting. For a verified, operator-owned regular database, explicitly restrict its mode:

```sh
chmod 600 nodi.db
```

Apply owner-only permissions to any existing SQLite sidecars as well, after inspecting them. Do not delete journal or WAL files to bypass a check: they may be necessary for recovery. Do not change unrelated files, follow unexpected links or discard the ledger. Keep backups private too. Once the files satisfy the checks, the existing schema and records remain usable without conversion.

The regression suite verifies that SQLite's rollback journal inherits private database permissions. WAL creation, network filesystems, extended ACL configurations and Windows ACLs have not been validated. Git ignores the standard `.db` file and journal/WAL/shared-memory suffixes; choose private paths and never rely on ignore patterns as access control.

An optional journal can be unlinked by SQLite between the validation open and
`fstat`. In that case validation reopens the path and fully checks any replacement,
with a bounded retry count. The main database, multiply linked files, symlinks,
unsafe modes and ownership mismatches remain rejected. The independent mock payment
provider uses these same file checks; keep it distinct from the authorization ledger.
