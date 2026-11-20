"""Unambiguous, finite, UTF-8 JSON for bounded HTTP request bodies."""

import json
import math


def validate_json(raw: bytes, max_depth: int = 64) -> None:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError("nonfinite JSON number")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite JSON number")
        return number

    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=unique,
        parse_constant=invalid_constant,
        parse_float=finite_float,
    )
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, (dict, list)):
            if depth >= max_depth:
                raise ValueError("JSON nesting limit exceeded")
            values = list(item.keys()) + list(item.values()) if isinstance(item, dict) else item
            pending.extend((child, depth + 1) for child in values)
        elif isinstance(item, str):
            # Escaped lone surrogates cannot be represented safely in UTF-8 responses.
            item.encode("utf-8")
