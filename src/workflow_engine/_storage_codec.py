"""Private JSONB encoding for opaque execution values, never structural strings.

JSONB normalizes number spellings and loses negative zero. Tag floats and integers
outside signed 64-bit range inside metadata, arguments and results. Escape the same
prefix in those values' ordinary strings and keys. SQL-checked structural values
and unrelated JSONB remain unchanged. The full compact Workflow is still the budget.
"""

import json
import math

from .domain import MAX_DOCUMENT_BYTES

_PREFIX = "~workflow-value:v1:"
_BASE = 10**9
_PAYLOAD_FIELDS = frozenset(
    {
        "request_text",
        "plan",
        "clarification",
        "catalog_version",
        "prompt_version",
        "model_metadata",
        "actions",
        "planning_attempts",
        "retry_phase",
        "history",
    }
)


def _decimal(value: int) -> str:
    negative = value < 0
    value = abs(value)
    chunks = []
    while value >= _BASE:
        value, remainder = divmod(value, _BASE)
        chunks.append(f"{remainder:09d}")
    return ("-" if negative else "") + str(value) + "".join(reversed(chunks))


def _integer(token: str) -> int:
    digits = token.removeprefix("-")
    if (
        not digits
        or len(digits) > MAX_DOCUMENT_BYTES
        or not digits.isascii()
        or not digits.isdigit()
    ):
        raise ValueError("Invalid storage integer")
    value = 0
    for offset in range(0, len(digits), 9):
        chunk = digits[offset : offset + 9]
        value = value * (10 ** len(chunk)) + int(chunk)
    return -value if token.startswith("-") else value


def _encode(value):
    if isinstance(value, str):
        return _PREFIX + "s" + value if value.startswith(_PREFIX) else value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Nonfinite storage number")
        return _PREFIX + "f" + value.hex()
    if isinstance(value, int) and not isinstance(value, bool):
        return value if -(2**63) <= value < 2**63 else _PREFIX + "i" + _decimal(value)
    if isinstance(value, dict):
        return {_encode(key): _encode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    return value


def _decode(value):
    if isinstance(value, str) and value.startswith(_PREFIX):
        token = value[len(_PREFIX) :]
        if token.startswith("s"):
            return token[1:]
        if token.startswith("i"):
            return _integer(token[1:])
        if token.startswith("f"):
            result = float.fromhex(token[1:])
            if math.isfinite(result):
                return result
        raise ValueError("Invalid storage value tag")
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            decoded_key = _decode(key)
            if not isinstance(decoded_key, str) or decoded_key in result:
                raise ValueError("Invalid storage object key")
            result[decoded_key] = _decode(item)
        return result
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


def _opaque_values(payload, transform):
    """Traverse the execution payload once; nested opaque dictionaries stay opaque.

    Engine hooks also see audit metadata. Its distinct root shape passes through,
    as do all structural execution strings, counters, plans and history entries.
    """
    if not isinstance(payload, dict) or payload.keys() != _PAYLOAD_FIELDS:
        return payload
    result = {**payload, "model_metadata": transform(payload["model_metadata"])}
    if isinstance(payload["actions"], list):
        result["actions"] = [
            {
                key: transform(value) if key in {"arguments", "result"} else value
                for key, value in action.items()
            }
            if isinstance(action, dict)
            else action
            for action in payload["actions"]
        ]
    return result


def _storage_dumps(value) -> str:
    return json.dumps(
        _opaque_values(value, _encode), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )


def _storage_loads(value):
    return _opaque_values(json.loads(value, parse_int=_integer), _decode)
