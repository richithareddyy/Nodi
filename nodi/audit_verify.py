"""Offline verification of an exact audit prefix against a separately held checkpoint."""

import argparse
import hashlib
import json
import re
import sys

ROOT = "0" * 64
MAX_RECORD = 1024 * 1024


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid(_value):
        raise ValueError("nonfinite JSON number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def checkpoint(value):
    if not isinstance(value, dict) or set(value) != {"version", "sequence", "event_hash"}:
        raise ValueError("invalid checkpoint fields")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("unsupported checkpoint version")
    if type(value["sequence"]) is not int or not 0 <= value["sequence"] <= 2**63 - 1:
        raise ValueError("invalid checkpoint sequence")
    if not isinstance(value["event_hash"], str) or not re.fullmatch(
        "[a-f0-9]{64}", value["event_hash"]
    ):
        raise ValueError("invalid checkpoint hash")
    if value["sequence"] == 0 and value["event_hash"] != ROOT:
        raise ValueError("invalid empty checkpoint")
    return value


def load_checkpoint(path):
    with open(path, "rb") as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError("checkpoint too large")
    return checkpoint(strict_json(raw))


class Verifier:
    def __init__(self, anchor):
        self.anchor = checkpoint(anchor)
        self.sequence, self.previous = 0, ROOT

    def add(self, row):
        if not isinstance(row, dict) or set(row) != {
            "sequence",
            "event",
            "previous_hash",
            "event_hash",
        }:
            raise ValueError("invalid audit record fields")
        if (
            type(row["sequence"]) is not int
            or row["sequence"] != self.sequence + 1
            or row["sequence"] > self.anchor["sequence"]
        ):
            raise ValueError(
                "audit sequence is missing, reordered, duplicated, or beyond checkpoint"
            )
        if not isinstance(row["event"], dict) or row["previous_hash"] != self.previous:
            raise ValueError("invalid audit event or chain link")
        payload = json.dumps(row["event"], sort_keys=True, separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256((self.previous + payload).encode()).hexdigest()
        if row["event_hash"] != digest:
            raise ValueError("audit event hash mismatch")
        self.sequence, self.previous = row["sequence"], digest

    def finish(self):
        if self.sequence != self.anchor["sequence"] or self.previous != self.anchor["event_hash"]:
            raise ValueError("audit does not match checkpoint (missing records or changed history)")
        return {
            "valid": True,
            "sequence": self.sequence,
            "event_hash": self.previous,
            "scope": "exact prefix matching supplied checkpoint; checkpoint trust must be established separately",
        }


def verify(path, anchor):
    verifier = Verifier(anchor)
    with open(path, "rb") as stream:
        while raw := stream.readline(MAX_RECORD + 1):
            if len(raw) > MAX_RECORD:
                raise ValueError("audit record too large")
            verifier.add(strict_json(raw))
    return verifier.finish()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--audit", required=True)
    args = parser.parse_args(argv)
    try:
        result = verify(args.audit, load_checkpoint(args.checkpoint))
    except (ValueError, TypeError, OSError, RecursionError):
        print("Audit verification failed: invalid files, chain, or checkpoint.", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
