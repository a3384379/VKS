import pytest
from vks_api import ai_gateway, association_pressure
from vks_api.ai_gateway import ModelNotSent
from vks_api.association_pressure import AssociationPressure
from vks_api.service import DomainError


def test_capacity_rises_slowly_and_reacts_to_memory_cpu_and_rate_limits():
    clock = [0.0]
    host = [0.2, 0.50]
    pressure = AssociationPressure(12, sample=lambda: tuple(host), clock=lambda: clock[0])

    def advance(seconds=2):
        clock[0] += seconds
        pressure.refresh()

    assert pressure.limit == 4
    for _ in range(3):
        advance()
    assert pressure.limit == 6
    host[:] = [0.2, 0.19]
    advance()
    assert pressure.limit == 3
    host[:] = [0.81, 0.50]
    advance()
    assert pressure.limit == 1
    host[:] = [0.2, 0.50]
    for _ in range(6):
        advance()
    assert pressure.limit == 5
    pressure.throttled()
    assert pressure.limit == 2
    for _ in range(8):
        advance()
    assert pressure.limit == 2
    with pressure.request():
        assert pressure.active == 1
    assert pressure.active == 0


def test_gateway_revalidates_under_admission_and_reduces_capacity_on_provider_limit(monkeypatch):
    pressure = AssociationPressure(8, sample=lambda: (0.30, 0.50))
    calls = []

    def stale():
        assert pressure.active == 1
        raise DomainError("VKS_VERSION_CONFLICT", "资料已变化", 412)

    def limited(_config, body):
        calls.append(body)
        raise DomainError("VKS_MODEL_FAILED", "服务限流", 429, details={"upstream_status": 429})

    monkeypatch.setattr(association_pressure, "ASSOCIATION_PRESSURE", pressure)
    monkeypatch.setattr(ai_gateway, "_request_model", limited)
    body = {"input": ["synthetic"]}
    config = {"_adaptive_association": True}
    with pytest.raises(ModelNotSent):
        ai_gateway.request_model({**config, "_association_preflight": stale}, body)
    assert not calls and pressure.active == 0
    with pytest.raises(DomainError, match="服务限流"):
        ai_gateway.request_model(config, body)
    assert calls == [body] and pressure.limit == 2 and pressure.active == 0
