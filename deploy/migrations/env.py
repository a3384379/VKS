from logging.config import fileConfig
from alembic import context
from sqlalchemy import engine_from_config, pool
from vks_api.models import Base
from vks_api import knowledge, scenarios, jobs, identity  # noqa: F401 - register tables
from vks_api import oauth, ai_gateway, documents, versions, relationships, modules, project_profile  # noqa: F401
from vks_api import materials  # noqa: F401
from vks_api import document_presentation  # noqa: F401
from vks_api import bulk_association  # noqa: F401
from vks_api import prototype_scan  # noqa: F401
from vks_api import scan_pipeline  # noqa: F401
from vks_api.connection import database_url

config = context.config

if database_url():
    config.set_main_option("sqlalchemy.url", database_url().replace("%", "%%"))
if config.config_file_name and config.get_section("loggers"):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
if context.is_offline_mode():
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = engine_from_config(
        config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
