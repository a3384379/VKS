"""Cache source projections by a database-maintained version, never permissions.

SQLite triggers cover raw SQL and other processes too. Random tokens avoid
reusing a speculative cache key after rollback. PostgreSQL keeps full reads.
"""

from collections import OrderedDict
from threading import RLock

from sqlalchemy import text


def install_sqlite(connection):
    connection.exec_driver_sql("INSERT OR IGNORE INTO input_epoch(id, token) VALUES (1, lower(hex(randomblob(16))))")
    # Inserts of model-authored points do not change a source corpus. Updates
    # and deletes are deliberately conservative, including identity changes.
    conditions = {
        "entity": "NEW.type NOT IN ('action','business_point')",
        "entity_revision": "NOT EXISTS (SELECT 1 FROM entity WHERE id=NEW.entity_id "
        "AND type IN ('action','business_point'))",
        "analysis_set_entity": "NOT EXISTS (SELECT 1 FROM entity WHERE id=NEW.entity_id "
        "AND type IN ('action','business_point'))",
        "artifact": "NEW.relative_path NOT LIKE 'prototype-points/%'",
        "source_snapshot": "1",
    }
    for table, inserted in conditions.items():
        for operation in ("INSERT", "UPDATE", "DELETE"):
            condition = inserted if operation == "INSERT" else "1"
            if table == "analysis_set_entity" and operation == "UPDATE":
                condition = (
                    "NOT EXISTS (SELECT 1 FROM entity WHERE id=OLD.entity_id "
                    "AND type IN ('action','business_point')) OR "
                    "NOT EXISTS (SELECT 1 FROM entity WHERE id=NEW.entity_id "
                    "AND type IN ('action','business_point'))"
                )
            connection.exec_driver_sql(
                f'CREATE TRIGGER IF NOT EXISTS "input_epoch_{table}_{operation.lower()}" '
                f'AFTER {operation} ON "{table}" WHEN {condition} BEGIN '
                "UPDATE input_epoch SET token=lower(hex(randomblob(16))) WHERE id=1; END"
            )


def cached_projection(service, session, key, compute):
    """Read the version in THIS snapshot; cache only source facts/digests."""
    if session.bind.dialect.name != "sqlite":
        return compute()
    session.flush()
    scoped = session.info.get("vks_read_batch") or session.info.get("vks_completion_batch")
    if scoped and "vks_source_token" in session.info:
        token = session.info["vks_source_token"]
    else:
        token = session.scalar(text("SELECT token FROM input_epoch WHERE id=1"))
        if scoped:
            session.info["vks_source_token"] = token
    if token is None:
        return compute()
    lock, cache = service.__dict__.setdefault("_input_projections", (RLock(), OrderedDict()))
    identity = (token, key)
    with lock:
        if identity in cache:
            cache.move_to_end(identity)
            return cache[identity]
    value = compute()
    large_scope = key[0] == "personal-document-scope"
    if large_scope and sum(len(row["content"]) * 4 + 1024 for row in value[0]) > 64 * 1024 * 1024:
        return value
    with lock:
        if large_scope:
            prior = [item for item in cache if item[1][0] == "personal-document-scope"]
            while len(prior) >= 2:
                cache.pop(prior.pop(0), None)
        cache[identity] = value
        cache.move_to_end(identity)
        while len(cache) > 128:
            cache.popitem(last=False)
    return value
