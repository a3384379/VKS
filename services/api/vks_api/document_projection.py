"""Keep whole Markdown context without revealing unselected chapter content."""

import re


def readable_document(document, chapters, visible_ids):
    visible = [row for row in chapters if row.id in visible_ids]
    complete = bool(chapters) and len(visible) == len(chapters)
    parts, sections, next_line = [], [], 1
    for row in visible:
        content = row.content
        original_start = row.locator.get("startLine") or row.locator.get("line_start") or 1
        start = original_start if complete else next_line
        end = (
            (row.locator.get("endLine") or row.locator.get("line_end") or start + content.count("\n"))
            if complete
            else start + content.count("\n")
        )
        heading = re.match(r"^ {0,3}(#{1,6})\s+", content)
        level = len(heading[1]) if heading else 0
        first_block = content.split("\n\n", 1)[0].splitlines()
        if not level and len(first_block) >= 2:
            underline = re.fullmatch(r" {0,3}(=+|-+)[ \t]*", first_block[-1])
            if underline:
                level = 1 if underline[1].startswith("=") else 2
        sections.append(
            {
                "id": row.entity_id,
                "revision_id": row.id,
                "name": row.name,
                "content": content,
                "start_line": start,
                "end_line": end,
                "heading_level": level,
            }
        )
        parts.append(content)
        next_line = start + content.count("\n") + 2
    return {
        "content": document.content if complete else "\n\n".join(parts),
        "partial": not complete,
        "sections": sections,
    }
