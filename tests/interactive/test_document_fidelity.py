"""Whole-document Markdown context remains exact while partial reads stay scoped."""

from sqlalchemy import select
from vks_api.documents import parse_sections
from vks_api.interactive import InteractiveKnowledgeService
from vks_api.models import Artifact, Revision, Selection

from tests.interactive import test_materials

materials = test_materials.materials


def test_full_document_keeps_preamble_references_and_original_markdown_bytes(materials):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    markdown = "前言：**完整保留。**紧接中文。\r\n\r\n# 操作说明\r\n\r\n请看[跨章节引用][next]。\r\n\r\n## 其他说明\r\n\r\n[next]: https://example.invalid/reference\r\n"
    package = test_materials.stage(service, owner, manager, pid, aid, "documents", markdown)
    test_materials.activate(service, owner, manager, pid, aid, [package])
    links = InteractiveKnowledgeService(service)
    entry = links.catalog(owner, pid, aid)["documents"][0]
    assert entry["name"] == "操作说明"
    before = links.catalog(owner, pid, aid)["selection_version"]
    document = links.document(owner, pid, entry["revision_id"], aid)
    assert document["content"] == markdown
    assert document["partial"] is False
    assert len(document["sections"]) == 3
    assert document["sections"][0]["content"].startswith("前言")
    assert document["sections"][1]["start_line"] == 3
    assert document["sections"][1]["heading_level"] == 1
    assert document["sections"][2]["start_line"] == 7
    assert "[next]:" in document["content"]
    with service.db() as s:
        revision = s.get(Revision, entry["revision_id"])
        artifact = s.get(Artifact, revision.artifact_id)
        assert (service.data_dir / "objects" / artifact.object_key).read_bytes() == markdown.encode()
    assert links.catalog(owner, pid, aid)["selection_version"] == before


def test_partial_document_never_uses_full_raw_body_or_hidden_reference_definitions(materials):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    markdown = "# 可读开头\n\n第一段\n\n## 不再选中的章节\n\nHIDDEN_BODY_AND_REFERENCE\n\n[secret]: https://hidden.invalid/secret\n\n## 可读结尾\n\n最后一段"
    package = test_materials.stage(service, owner, manager, pid, aid, "documents", markdown)
    test_materials.activate(service, owner, manager, pid, aid, [package])
    links = InteractiveKnowledgeService(service)
    rid = links.catalog(owner, pid, aid)["documents"][0]["revision_id"]
    full = links.document(owner, pid, rid, aid)
    hidden = full["sections"][1]
    with service.db() as s:
        selected = s.scalar(
            select(Selection).where(
                Selection.analysis_set_id == aid, Selection.entity_revision_id == hidden["revision_id"]
            )
        )
        s.delete(selected)
    projected = links.document(owner, pid, rid, aid)
    assert projected["partial"] is True
    assert "HIDDEN_BODY" not in projected["content"] and "hidden.invalid" not in projected["content"]
    assert len(projected["sections"]) == 2
    for chapter in projected["sections"]:
        location = projected["content"].index(chapter["content"])
        assert chapter["start_line"] == projected["content"][:location].count("\n") + 1


def test_heading_parser_preserves_intro_as_indexable_section():
    source = "原始介绍与[说明][ref]。\n\n# 第一章\n\n正文\n\n[ref]: https://example.invalid"
    sections = parse_sections(source, "手册")
    assert sections[0]["startLine"] == 1
    assert sections[0]["content"] == "原始介绍与[说明][ref]。\n"
    assert sections[1]["startLine"] == 3
    assert "[ref]:" in sections[1]["content"]
