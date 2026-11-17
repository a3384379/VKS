"""Verified document vectors survive unrelated association progress, not changed inputs."""

import pytest
from vks_api.documents import DocumentService
from vks_api.prototype_edit import BindingEdit, PrototypeEditor
from vks_api.service import DomainError

from tests.interactive import test_rag_export
from tests.prototype.test_v3_workflow import _binding

workspace = test_rag_export.workspace
vectors = test_rag_export.vectors


def test_verified_vector_build_accepts_concurrent_association_save_and_keeps_export_version_strict(
    vectors, monkeypatch
):
    service, owner, pid, aid, rag, calls = vectors
    assert rag.build_documents(owner, pid, analysis_set_id=aid)["complete"]
    baseline = rag.document_scope(owner, pid, analysis_set_id=aid, permission="knowledge:edit")
    export = rag.document_scope(owner, pid, analysis_set_id=aid, permission="knowledge:export")
    before_calls = len(calls)
    original = rag._export_vectors
    saved = []

    def associate_after_verification(*args, **kwargs):
        verified = original(*args, **kwargs)
        version, body = _binding(service, owner, pid, aid)
        saved.append(
            PrototypeEditor(service).save_binding(
                owner, pid, "charge.add", BindingEdit.model_validate(body), version, "concurrent-vector-binding"
            )
        )
        return verified

    monkeypatch.setattr(rag, "_export_vectors", associate_after_verification)
    actual = rag.build_documents(owner, pid, analysis_set_id=aid)
    assert actual["complete"] and actual["indexed_chunks"] == actual["total_chunks"] > 0
    assert len(saved) == 1 and saved[0]["binding_id"] and len(calls) == before_calls
    current = rag.document_scope(owner, pid, analysis_set_id=aid, permission="knowledge:edit")
    current_export = rag.document_scope(owner, pid, analysis_set_id=aid, permission="knowledge:export")
    assert current == baseline
    assert current_export[0] == export[0] and current_export[2] == export[2]
    assert current_export[1] != export[1], "Export projections must still bind the exact draft counter"


def test_verified_vectors_reject_document_replacement_between_verification_and_completion(vectors, monkeypatch):
    service, owner, pid, aid, rag, calls = vectors
    assert rag.build_documents(owner, pid, analysis_set_id=aid)["complete"]
    chapters, _scope, _provenance = rag.document_scope(owner, pid, analysis_set_id=aid, permission="knowledge:edit")
    document = next(
        d
        for d in rag.assistant.links.catalog(owner, pid, aid)["documents"]
        if d["revision_id"] == chapters[0]["document_revision_id"]
    )
    original = rag._export_vectors
    before_calls = len(calls)

    def replace_after_verification(*args, **kwargs):
        verified = original(*args, **kwargs)
        DocumentService(service).save_draft(
            owner,
            document["id"],
            {
                "analysis_set_id": aid,
                "base_revision_id": document["revision_id"],
                "markdown": "# Replacement\n\n## Different rule\n\nThis is a new exact business rule.",
                "locked_section_ids": [],
            },
            1,
        )
        return verified

    monkeypatch.setattr(rag, "_export_vectors", replace_after_verification)
    with pytest.raises(DomainError) as error:
        rag.build_documents(owner, pid, analysis_set_id=aid)
    assert error.value.code == "VKS_VERSION_CONFLICT" and len(calls) == before_calls
