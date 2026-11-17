"""Existing SQLite instances gain covering indexes without changing knowledge."""

from sqlalchemy import inspect
from vks_api.query_indexes import INDEXES
from vks_api.service import Service

from tests.interactive import test_materials

materials = test_materials.materials


def immutable_rows(connection):
    return {
        table: connection.exec_driver_sql(f'SELECT * FROM "{table}" ORDER BY 1,2').all()
        for table in (
            "entity",
            "entity_revision",
            "analysis_set_entity",
            "relation",
            "artifact",
            "job",
            "job_checkpoint",
        )
    }


def test_existing_sqlite_upgrade_adds_only_indexes_and_is_repeatable(materials):
    _web, service, owner, manager, projects = materials
    pid, aid = projects[0]
    prototype, _documents = test_materials.install(service, owner, manager, pid, aid)
    test_materials.bind(service, owner, pid, aid, prototype, model=True)
    with service.engine.begin() as connection:
        before = immutable_rows(connection)
        for name, _columns in INDEXES.values():
            connection.exec_driver_sql(f'DROP INDEX "{name}"')
    service.engine.dispose()
    upgraded = Service(service.data_dir)
    try:
        with upgraded.engine.connect() as connection:
            assert immutable_rows(connection) == before
            for table, (name, columns) in INDEXES.items():
                index = next(row for row in inspect(connection).get_indexes(table) if row["name"] == name)
                assert index["column_names"] == columns
            stats = set(connection.exec_driver_sql("SELECT idx FROM sqlite_stat1").scalars())
            assert {name for name, _columns in INDEXES.values()} <= stats
            schema_version = connection.exec_driver_sql("PRAGMA schema_version").scalar_one()
        upgraded.engine.dispose()
        again = Service(service.data_dir)
        try:
            with again.engine.connect() as connection:
                assert immutable_rows(connection) == before
                assert connection.exec_driver_sql("PRAGMA schema_version").scalar_one() == schema_version
        finally:
            again.engine.dispose()
    finally:
        upgraded.engine.dispose()
