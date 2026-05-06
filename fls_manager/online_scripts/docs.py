import re

from ..utils import h


def markdown_inline(text):
    text = h(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(
        r"!\[([^\]]*)\]\((https?://[^)]+)\)",
        r'<img alt="\1" src="\2" style="max-width:100%;border-radius:10px;margin:8px 0;">',
        text,
    )
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)",
        r'<a href="\2" target="_blank">\1</a>',
        text,
    )
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", text)
    text = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", text)
    return text


def render_markdown_to_html(text):
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    out = []
    in_code = False
    code_lines = []
    in_ul = False
    in_ol = False

    def close_lists():
        nonlocal in_ul, in_ol

        if in_ul:
            out.append("</ul>")
            in_ul = False

        if in_ol:
            out.append("</ol>")
            in_ol = False

    def flush_code():
        nonlocal code_lines
        out.append(
            '<pre class="fls-md-code"><code>{}</code></pre>'.format(
                h("\n".join(code_lines))
            )
        )
        code_lines = []

    for line in lines:
        raw = line.rstrip("\n")

        if raw.strip().startswith("```"):
            if in_code:
                flush_code()
                in_code = False
            else:
                close_lists()
                in_code = True
                code_lines = []
            continue

        if in_code:
            code_lines.append(raw)
            continue

        if not raw.strip():
            close_lists()
            out.append("")
            continue

        m = re.match(r"^(#{1,6})\s+(.+)$", raw)
        if m:
            close_lists()
            level = len(m.group(1))
            out.append(f"<h{level}>{markdown_inline(m.group(2).strip())}</h{level}>")
            continue

        if re.match(r"^\s*[-*_]{3,}\s*$", raw):
            close_lists()
            out.append("<hr>")
            continue

        m = re.match(r"^\s*[-*+]\s+(.+)$", raw)
        if m:
            if in_ol:
                out.append("</ol>")
                in_ol = False
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{markdown_inline(m.group(1).strip())}</li>")
            continue

        m = re.match(r"^\s*\d+\.\s+(.+)$", raw)
        if m:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            if not in_ol:
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{markdown_inline(m.group(1).strip())}</li>")
            continue

        m = re.match(r"^\s*>\s*(.*)$", raw)
        if m:
            close_lists()
            out.append(f"<blockquote>{markdown_inline(m.group(1).strip())}</blockquote>")
            continue

        close_lists()
        out.append(f"<p>{markdown_inline(raw.strip())}</p>")

    if in_code:
        flush_code()

    close_lists()

    return "\n".join(out)


def doc_url_looks_markdown(url):
    u = str(url or "").lower().split("?", 1)[0]
    return u.endswith((".md", ".markdown", ".mdown", ".mkd", ".txt"))


def doc_content_looks_markdown(text):
    text = str(text or "")
    sample = text[:4000]

    patterns = [
        r"^#\s+",
        r"^##\s+",
        r"```",
        r"^\s*[-*+]\s+",
        r"^\s*\d+\.\s+",
        r"\[[^\]]+\]\(https?://",
    ]

    for p in patterns:
        if re.search(p, sample, re.M):
            return True

    return False


def doc_response_is_html(resp, text):
    ctype = str(resp.headers.get("Content-Type", "") or "").lower()

    if "text/html" in ctype:
        return True

    s = str(text or "").lstrip().lower()

    return s.startswith("<!doctype html") or s.startswith("<html") or "<body" in s[:1000]


def doc_response_is_text(resp):
    ctype = str(resp.headers.get("Content-Type", "") or "").lower()

    if not ctype:
        return True

    return (
        "text/" in ctype
        or "json" in ctype
        or "xml" in ctype
        or "markdown" in ctype
        or "javascript" in ctype
    )