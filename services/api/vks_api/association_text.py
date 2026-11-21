"""Keep generated association titles separate from their Markdown body."""

import re
import unicodedata

from markdown_it import MarkdownIt

# The parking formula catalog names this rule “月固定数×月数”. The
# monthly-card billing rules express the same calculation as fixed price
# multiplied by renewal months. Match the complete expressions only.
_PARKING_MONTHLY_FIXED = ("月固定金额×月数", "月固定数×月数")


def preserve_formulas(instruction: str) -> bool:
    return bool(re.search(r"(?:保留|沿用|不要删(?:除)?|别删)[^，。；,;\n]{0,24}(?:公式|计算式)", instruction))


def formula_key(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"\[\d+\]|\*{2,}|[`$\s]", "", text)
    return text.replace(r"\times", "×").replace(r"\div", "÷").translate(str.maketrans("*−", "×-"))


def formula_anchors(body: str) -> dict[str, str]:
    """Extract identifiable formula codes and symbolic expressions from a working draft."""
    anchors = {}
    formula_level = None
    for line in body.splitlines():
        if heading := re.match(r"^(#{1,6})\s+(.*)", line):
            level = len(heading[1])
            if formula_level is not None and level <= formula_level:
                formula_level = None
            if re.search(r"公式|计算式", heading[2]):
                formula_level = level
            continue
        if (formula_level is not None or re.search(r"公式|计算式|[×÷]", line)) and (
            code := re.match(r"\s*\|\s*`?([0-9]{5,8})`?\s*\|", line)
        ):
            anchors[code[1]] = code[1]
        # Keep calculation cells and explicitly delimited inline expressions;
        # prose surrounding them is free to change during revision.
        segments = line.split("|") if line.lstrip().startswith("|") else re.findall(r"\*\*([^*]+)\*\*|`([^`]+)`|\$([^$]+)\$", line)
        for segment in segments:
            text = segment if isinstance(segment, str) else next(part for part in segment if part)
            for part in re.split(r"[，。；：,;:]", text):
                part = part.strip()
                key = formula_key(part).rstrip(".")
                if len(key) <= 120 and re.search(r"[\w)]+(?:[×÷=]|(?<=\))-(?=[a-z]+\())[\w(]+", key):
                    anchors[key] = part
    return anchors


def contains_formula(text: str, key: str) -> bool:
    if key.isascii() and key.isdigit():
        return bool(re.search(rf"(?<![0-9]){key}(?![0-9])", text))
    if key in _PARKING_MONTHLY_FIXED:
        return any(
            (match.start() == 0 or text[match.start() - 1] not in "×÷+-/")
            and (match.end() == len(text) or text[match.end()] not in "×÷=+-/")
            for expression in _PARKING_MONTHLY_FIXED
            for match in re.finditer(r"[([{]*" + re.escape(expression) + r"[)\]}]*", text)
        )
    if not key.endswith((")", "]")):
        # Do not count a shorter expression if it is followed by an operator,
        # since that would accept an added multiplier or another operand.
        return bool(re.search(re.escape(key) + r"(?![×÷=])", text))
    return key in text


def has_unmapped_citations(body: str, count: int) -> bool:
    """Check numbered Markdown prose citations, excluding code and links."""
    if not re.search(r"\[\d+\]", body):
        return False
    for block in MarkdownIt("commonmark").parse(body):
        link_depth = 0
        for token in block.children or []:
            if token.type == "link_open":
                link_depth += 1
            elif token.type == "link_close":
                link_depth -= 1
            elif token.type == "text" and not link_depth:
                for number in re.findall(r"\[([0-9]+)\]", token.content):
                    digits = number.lstrip("0")
                    if not digits or len(digits) > 2 or int(digits) > count:
                        return True
    return False


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
