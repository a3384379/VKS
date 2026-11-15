"""Concurrent accounting remains idempotent across repeated settlement requests."""

from concurrent.futures import ThreadPoolExecutor

from vks_api.ai_gateway import ModelLedger, ModelUsage

from tests.models import test_model_gateway

system = test_model_gateway.system


def test_duplicate_concurrent_settlement_charges_a_reservation_once(system):
    service, actor, project, _aid, ai, _requests, _response, config = system
    usage_id = ai.reserve(actor, project["id"], config, 500)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: ai.settle(usage_id, config, 100, 50, "succeeded"), range(16)))
    with service.db() as db:
        usage = db.get(ModelUsage, usage_id)
        ledger = db.get(ModelLedger, usage.ledger_id)
        assert usage.state == "succeeded"
        assert ledger.reserved_tokens == 0 and ledger.reserved_cost_micro == 0
        assert ledger.spent_tokens == 150 and usage.input_tokens + usage.output_tokens == 150
