"""Build connection URLs from separate secrets without interpolation ambiguity."""

from __future__ import annotations
import os
from urllib.parse import quote
from sqlalchemy.engine import URL


def database_url(default=None, *, environ=None):
    env = os.environ if environ is None else environ
    if env.get("VKS_DB_HOST"):
        return URL.create(
            "postgresql+psycopg",
            username=env.get("VKS_DB_USER", "vks"),
            password=env.get("VKS_DB_PASSWORD", ""),
            host=env["VKS_DB_HOST"],
            port=int(env.get("VKS_DB_PORT", "5432")),
            database=env.get("VKS_DB_NAME", "vks"),
        ).render_as_string(hide_password=False)
    return env.get("VKS_DATABASE_URL") or default


def broker_url(default="amqp://guest:guest@127.0.0.1:5672//", *, environ=None):
    env = os.environ if environ is None else environ
    if env.get("VKS_BROKER_HOST"):
        host = env["VKS_BROKER_HOST"]
        if ":" in host and not host.startswith("["):
            host = "[" + host + "]"
        user = quote(env.get("VKS_BROKER_USER", "vks"), safe="")
        password = quote(env.get("VKS_BROKER_PASSWORD", ""), safe="")
        virtual_host = quote(env.get("VKS_BROKER_VHOST", "vks"), safe="")
        port = int(env.get("VKS_BROKER_PORT", "5672"))
        return f"amqp://{user}:{password}@{host}:{port}/{virtual_host}"
    return env.get("VKS_BROKER_URL") or default
