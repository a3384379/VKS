"""Keep generated association titles separate from their Markdown body."""

import re
import unicodedata

from markdown_it import MarkdownIt


def association_body_without_title(body: str, title: str, *aliases: str) -> str:
    """Remove only leading standalone titles, preserving the remaining bytes.

    This is for newly generated associations, never imported documents, saved
    revisions or historical previews. Content headings and later mentions stay.
    """
    parser = MarkdownIt("commonmark")

    def text_key(value):
        inline = parser.parseInline(value)[0]
        # Images and raw HTML may contain information beyond their visible title.
        if any(token.type in {"image", "html_inline"} for token in inline.children or []):
            return None
        text = "".join(token.content for token in inline.children or [] if token.type in {"text", "code_inline"})
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()

    titles = {key for value in (title, *aliases) if value and (key := text_key(value))}
    if not titles:
        return body
    lines = body.splitlines(keepends=True)
    index, end = 0, 0
    while index < len(lines):
        while index < len(lines) and not lines[index].strip():
            index += 1
        if index == len(lines):
            break
        line = lines[index].rstrip("\r\n")
        # Indented code and fenced blocks are content, even if they name a title.
        if line.expandtabs(4).startswith("    "):
            break
        heading = re.fullmatch(r" {0,3}#{1,6}(?:[ \t]+(.*))?", line)
        candidate = re.sub(r"[ \t]+#+[ \t]*$", "", heading[1] or "") if heading else line.strip()
        key = text_key(candidate)
        if not key or re.sub(r"^(?:文档标题|关联标题|标题):", "", key) not in titles:
            break
        index += 1
        if not heading and index < len(lines) and re.fullmatch(r" {0,3}(?:=+|-+)[ \t]*", lines[index].rstrip("\r\n")):
            index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1
        end = index
    return "".join(lines[end:]) if end else body
