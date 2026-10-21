"""Redact secrets from diagnostic messages returned by administrative APIs."""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:authorization|proxy-authorization|bearer|token|access[_ -]?token|"
    r"refresh[_ -]?token|api[_ -]?key|secret|password|passwd|credential|cookie|session)\b"
    r"\s*[:=]\s*)(?:Bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
_SENSITIVE_FIELD = re.compile(
    r"(?i)(authorization|proxy[_-]?authorization|bearer|access[_-]?token|refresh[_-]?token|"
    r"api[_-]?key|secret|password|passwd|credential|cookie|session[_-]?token)"
)
_SERVICE_TOKEN = re.compile(r"\bpwsvc_[A-Za-z0-9._~-]+\b")
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _redact_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    suffix = ""
    while raw and raw[-1] in ".,;:)]}":
        suffix = raw[-1] + suffix
        raw = raw[:-1]
    try:
        parts = urlsplit(raw)
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        if parts.username is not None or parts.password is not None:
            host = "<redacted>@" + host
        query = urlencode([
            (key, "[redigido]" if re.search(
                r"(?i)(token|key|secret|password|passwd|auth|credential|session|cookie)", key
            ) else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ])
        return urlunsplit((parts.scheme, host, parts.path, query, parts.fragment)) + suffix
    except (TypeError, ValueError):
        return "[URL redigida]" + suffix


def _sanitize_text(value: str) -> str:
    value = _URL.sub(_redact_url, value)
    value = _SENSITIVE_ASSIGNMENT.sub(r"\1[redigido]", value)
    value = _SERVICE_TOKEN.sub("[credencial redigida]", value)
    return value[:500]


def is_sensitive_field(name) -> bool:
    """Identify structured diagnostic keys that must never be serialized."""
    return bool(_SENSITIVE_FIELD.search(str(name)))


def sanitize_error(value):
    """Return a response-safe diagnostic while leaving the persisted source untouched."""
    if value is None:
        return None
    if isinstance(value, dict):
        return {key: "[redigido]" if is_sensitive_field(key) else sanitize_error(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_error(item) for item in value]
    if not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return _sanitize_text(value)
    if not isinstance(parsed, (dict, list)):
        return _sanitize_text(value)
    cleaned = sanitize_error(parsed)
    return json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))[:500]
