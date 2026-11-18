"""Unfingerprinted evidence/capture policies retain authoritative full traversal."""

import pytest
from vks_api import corpus_cache
from vks_api.corpus_validation import validated
from vks_api.interactive_ai import AssistanceInput, InteractiveAssistant
from vks_api.models import Artifact, Evidence, Revision, uid
from vks_api.prototype_edit import _artifact
from vks_api.service import DomainError

from tests.interactive import test_assistant

workspace = test_assistant.workspace
interactive = test_assistant.interactive
assistant = test_assistant.assistant


@pytest.mark.parametrize("change", ["deleted", "ownership", "artifact"])
def test_evidence_change_cannot_reuse_light_fence_or_cross_target_template(assistant, monkeypatch, change):
    (_web, service, owner, pid, aid, _grant, _package, section, _raw), body, _calls = assistant
    engine = InteractiveAssistant(service)
    form = AssistanceInput.model_validate(body)
    with service.db() as s:
        chapter = s.get(Revision, section["entity_revision_id"])
        source = s.get(Artifact, chapter.artifact_id)
        target = _artifact(
            s, service, pid, source.snapshot_id, "audit-inputs", b"Business rule", "fixture", "text/plain", {}
        )
        evidence = Evidence(
            id=uid(),
            project_id=pid,
            analysis_set_id=aid,
            artifact_id=target.id,
            stable_key="audit-input-rule",
            evidence_kind="observed",
            content_sha256=target.sha256,
            locator={},
            excerpt="Business rule",
            claim="Independent evidence for the generated chapter",
        )
        s.add(evidence)
        evidence_id, source_id = evidence.id, source.id
        source.metadata_json = {
            **source.metadata_json,
            "input_provenance": [
                {
                    "artifact_id": target.id,
                    "evidence_id": evidence.id,
                    "sha256": target.sha256,
                    "requires_source_read": False,
                    "requires_trace_read": False,
                }
            ],
        }
    before = engine.prepare(owner, pid, form)
    assert section["entity_revision_id"] in {row["revision_id"] for row in before[1]}
    # This policy reads Evidence outside the lightweight invariant, so no
    # shortcut is registered even while all evidence references are valid.
    assert validated(engine, owner, pid, form, before[3]) is None
    other = service.create_project(owner, {"key": "evidence-other", "name": "Other"}, "evidence-other")["id"]
    with service.db() as s:
        evidence = s.get(Evidence, evidence_id)
        if change == "deleted":
            s.delete(evidence)
        elif change == "ownership":
            evidence.project_id = other
        else:
            evidence.artifact_id = source_id
    assert validated(engine, owner, pid, form, before[3]) is None
    with pytest.raises(DomainError) as error:
        engine._unchanged(owner, pid, form, before[3], None, None)
    assert error.value.code == "VKS_VERSION_CONFLICT"
    hydrated, original = [], corpus_cache.corpus_rows

    def observed(*args, **kwargs):
        hydrated.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(corpus_cache, "corpus_rows", observed)
    next_target = form.model_copy(update={"target": form.target.model_copy(update={"selector": "#another"})})
    with engine.batch(owner, pid):
        after = engine.prepare(owner, pid, next_target)
    assert hydrated and section["entity_revision_id"] not in {row["revision_id"] for row in after[1]}
