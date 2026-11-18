"""Incremental index of immutable selected anchor metadata, never permissions."""

import json
from collections import OrderedDict
from threading import RLock

from pydantic import ValidationError
from sqlalchemy import select

from .models import Revision
from .immutable_facts import revision_fact
from .reading import chunks
from .service import canonical


def selected_targets(service, s, pid, choices, package_id, target, target_type, *, normalize=False):
    # setdefault gives all readers the same tiny cache lock without acquiring
    # the global write lock. Only immutable locator facts enter this cache.
    lock, projects = service.__dict__.setdefault("_interactive_target_index", (RLock(), OrderedDict()))
    selected = set(choices.values())
    signature = {k: v for k, v in target.model_dump().items() if k != "open_steps"}
    key = canonical([package_id, signature])
    with lock:
        if pid not in projects:
            projects[pid] = (set(), {}, {})
        known, raw, normal = projects[pid]
        missing = selected - known
        # Source replacement and large histories cannot retain an unbounded
        # number of superseded locators. Rebuilding changes no selected facts.
        if len(known | missing) > 100000:
            known, raw, normal = projects[pid] = (set(), {}, {})
            missing = selected
        local = {
            rid: row.metadata_json.get("interactive_anchor")
            for rid in missing
            if (row := s.identity_map.get((Revision, (rid,), None))) is not None and row.project_id == pid
        }
        facts = list(local.items())
        for batch in chunks(missing - local.keys()):
            facts.extend(
                s.execute(
                    select(Revision.id, Revision.metadata_json["interactive_anchor"].as_string()).where(
                        Revision.id.in_(batch), Revision.project_id == pid
                    )
                )
            )
        for rid, anchor in facts:
            if anchor is None:
                continue
            if isinstance(anchor, str):
                try:
                    anchor = json.loads(anchor)
                except (ValueError, TypeError):
                    continue
            if not isinstance(anchor, dict) or not isinstance(anchor.get("target"), dict):
                continue
            locator = anchor["target"]
            package = anchor.get("package_id")
            raw_key = canonical([package, {k: v for k, v in locator.items() if k != "open_steps"}])
            raw.setdefault(raw_key, set()).add(rid)
            try:
                locator = target_type.model_validate(locator).model_dump(exclude={"open_steps"})
            except ValidationError:
                continue
            normal.setdefault(canonical([package, locator]), set()).add(rid)
        known.update(missing)
        projects.move_to_end(pid)
        while len(projects) > 4:
            projects.popitem(last=False)
        return (normal if normalize else raw).get(key, set()) & selected


def selected_roots(service, s, pid, choices):
    """Resolve current logical roots, reusing only their immutable revision facts."""
    from .materials import ROOT
    from .models import Entity
    from .reading import ReadingService

    # At most the installed logical packages need examining. Thousands of
    # unrelated selected chapter IDs do not belong in this roots query.
    from .input_versions import cached_projection

    entities = cached_projection(
        service,
        s,
        ("interactive-root-identities", pid),
        lambda: tuple(
            s.scalars(
                select(Entity.id).where(
                    Entity.project_id == pid, Entity.type == "page", Entity.stable_key.startswith("material.")
                )
            )
        ),
    )
    wanted = [(eid, choices[eid]) for eid in entities if eid in choices]
    lock, cache = service.__dict__.setdefault("_interactive_root_facts", (RLock(), OrderedDict()))
    with lock:
        missing = [rid for _eid, rid in wanted if (pid, rid) not in cache]
        for batch in chunks(missing):
            for row in s.scalars(select(Revision).where(Revision.id.in_(batch), Revision.project_id == pid)):
                cache[(pid, row.id)] = revision_fact(row)
        result = []
        for eid, rid in wanted:
            row = cache.get((pid, rid))
            if row and row.entity_id == eid and ROOT in row.metadata_json:
                result.append(row)
                cache.move_to_end((pid, rid))
        while len(cache) > 8:
            cache.popitem(last=False)
    ReadingService(None).preload(s, result)
    s.info.setdefault("vks_prefetched", {}).update({(Revision, row.id): row for row in result})
    return result
