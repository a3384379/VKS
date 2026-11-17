"""Detached value snapshots for shared caches; ORM rollback cannot expire them."""

from copy import deepcopy
from dataclasses import dataclass


@dataclass(frozen=True)
class RevisionFact:
    id: str
    entity_id: str
    project_id: str
    artifact_id: str | None
    name: str
    content: str
    content_sha256: str
    semantic_content_sha256: str
    locator: dict
    metadata_json: dict
    created_at: str


def revision_fact(row):
    return RevisionFact(
        row.id,
        row.entity_id,
        row.project_id,
        row.artifact_id,
        row.name,
        row.content,
        row.content_sha256,
        row.semantic_content_sha256,
        deepcopy(row.locator),
        deepcopy(row.metadata_json),
        row.created_at,
    )
