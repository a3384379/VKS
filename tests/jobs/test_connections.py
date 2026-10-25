from kombu import Connection
from sqlalchemy.engine import make_url
from vks_api.connection import database_url, broker_url


def test_component_urls_roundtrip_reserved_passwords_and_unicode_without_logging():
    password = "p@ss:/?#%+$![]&=中文"
    env = {
        "VKS_DB_HOST": "database",
        "VKS_DB_USER": "vks",
        "VKS_DB_PASSWORD": password,
        "VKS_DB_NAME": "vks",
        "VKS_BROKER_HOST": "::1",
        "VKS_BROKER_USER": "vks",
        "VKS_BROKER_PASSWORD": password,
        "VKS_BROKER_VHOST": "vks/private",
    }
    db = make_url(database_url(environ=env))
    assert db.password == password and db.host == "database"
    broker = Connection(broker_url(environ=env))
    assert broker.password == password and broker.hostname == "::1" and broker.virtual_host == "vks/private"
    assert password not in database_url(environ=env) and password not in broker_url(environ=env)


def test_explicit_urls_remain_compatible_and_components_override_image_defaults():
    assert database_url("sqlite://", environ={}) == "sqlite://"
    assert database_url(environ={"VKS_DATABASE_URL": "sqlite://"}) == "sqlite://"
    env = {"VKS_DATABASE_URL": "sqlite://", "VKS_DB_HOST": "database", "VKS_DB_PASSWORD": "x"}
    assert make_url(database_url(environ=env)).host == "database"
    assert broker_url(environ={"VKS_BROKER_URL": "memory://"}) == "memory://"
