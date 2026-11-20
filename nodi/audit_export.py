"""Private, exclusive audit exports with verification before publication."""

import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlencode

from .audit_verify import MAX_RECORD, Verifier, checkpoint, load_checkpoint


def publish(path, write):
    destination = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".audit-", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            result = write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        # An exclusive hard link publishes only a complete file, without replacing a destination.
        os.link(temporary, destination)
        return result
    finally:
        os.unlink(temporary)


def save_checkpoint(client, path):
    anchor = checkpoint(client.request("GET", "/audit/checkpoint"))
    publish(path, lambda stream: stream.write(json.dumps(anchor, indent=2) + "\n"))
    return {
        "checkpoint_file": str(path),
        **anchor,
        "next_step": "Protect this checkpoint separately from the database and export",
    }


def export_audit(client, checkpoint_path, output):
    anchor = load_checkpoint(checkpoint_path)

    def write(stream):
        verifier = Verifier(anchor)
        while verifier.sequence < anchor["sequence"]:
            page = client.request(
                "GET",
                "/audit/events?"
                + urlencode(
                    {"after": verifier.sequence, "through": anchor["sequence"], "limit": 25}
                ),
            )
            rows = page.get("items")
            if not isinstance(rows, list) or not rows or len(rows) > 25:
                raise ValueError("invalid or incomplete audit page")
            for item in rows:
                row = {
                    key: item[key] for key in ("sequence", "event", "previous_hash", "event_hash")
                }
                verifier.add(row)
                line = (
                    json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
                )
                if len(line.encode()) > MAX_RECORD:
                    raise ValueError("audit record too large")
                stream.write(line)
        return verifier.finish()

    return {"audit_file": str(output), **publish(output, write)}
