import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SENSITIVE_KEY_RE = re.compile(
    r"(token|secret|password|passwd|pwd|cookie|authorization|auth|api[_-]?key|access[_-]?token|refresh[_-]?token|session|key)$",
    re.IGNORECASE,
)


def is_sensitive_key(key):
    key = str(key or "").strip().lower()
    if not key:
        return False

    return bool(SENSITIVE_KEY_RE.search(key))


def mask_secret_value(value):
    raw = str(value if value is not None else "")

    if not raw:
        return ""

    if len(raw) <= 4:
        return "*" * len(raw)

    if len(raw) <= 8:
        return raw[:1] + "*" * (len(raw) - 2) + raw[-1:]

    return raw[:3] + "*" * max(4, len(raw) - 7) + raw[-4:]


def mask_if_sensitive(key, value):
    if is_sensitive_key(key):
        return mask_secret_value(value)

    return value


def mask_sensitive_url(value):
    text = str(value or "")

    try:
        parts = urlsplit(text)
        if not parts.query:
            return text

        pairs = []
        changed = False

        for key, val in parse_qsl(parts.query, keep_blank_values=True):
            if is_sensitive_key(key):
                pairs.append((key, mask_secret_value(val)))
                changed = True
            else:
                pairs.append((key, val))

        if not changed:
            return text

        return urlunsplit((
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(pairs, doseq=True),
            parts.fragment,
        ))
    except Exception:
        return text


def mask_sensitive_text(text):
    text = str(text if text is not None else "")

    def replace_json(match):
        return match.group(1) + mask_secret_value(match.group(2)) + match.group(3)

    def replace_pair(match):
        return match.group(1) + mask_secret_value(match.group(2))

    text = re.sub(
        r'((?:"|\')?(?:token|secret|password|passwd|pwd|cookie|authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|session|key)(?:"|\')?\s*:\s*["\'])([^"\']+)(["\'])',
        replace_json,
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"(\b(?:token|secret|password|passwd|pwd|cookie|authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|session|key)\s*=\s*)([^\s,;&]+)",
        replace_pair,
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"(\bAuthorization\s*:\s*Bearer\s+)([A-Za-z0-9._~+/=-]+)",
        replace_pair,
        text,
        flags=re.IGNORECASE,
    )

    text = re.sub(
        r"([?&](?:token|secret|password|passwd|pwd|cookie|api[_-]?key|access[_-]?token|refresh[_-]?token|session|key)=)([^&\s]+)",
        replace_pair,
        text,
        flags=re.IGNORECASE,
    )

    return text
