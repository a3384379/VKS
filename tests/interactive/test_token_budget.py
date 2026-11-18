import json

from vks_api.ai_gateway import PolicyInput
from vks_api.model_connections import ConnectionForm
from vks_api.token_budget import association_policy, batch_fits, request_input_tokens


def test_association_caps_preserve_chat_and_leave_context_headroom():
    config = PolicyInput(model_id="gpt-6-luna", max_output_tokens=8000).model_dump()
    associated = association_policy(config)
    assert (associated["max_input_tokens"], associated["max_output_tokens"]) == (128000, 16000)
    assert (config["max_input_tokens"], config["max_output_tokens"]) == (12000, 8000)
    config.update(association_max_input_tokens=1050000, association_max_output_tokens=64000)
    assert association_policy(config)["max_input_tokens"] == 984000
    assert association_policy({**config, "model_id": "custom"})["max_output_tokens"] == 64000
    assert ConnectionForm(endpoint="https://example.test", association_max_output_tokens=128000)


def test_text_estimate_uses_offline_tokens_and_counts_full_schema(monkeypatch):
    import requests

    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("No network")))
    body = {"model": "gpt-6-luna", "input": [{"role": "user", "content": "计费公式：单价 × 面积 × 月数。" * 2000}]}
    count = request_input_tokens(body)
    assert 2000 < count < len(json.dumps(body, ensure_ascii=False).encode()) / 2
    assert request_input_tokens({**body, "text": {"format": {"description": "原文表格" * 300}}}) > count
    unknown = {**body, "model": "custom-unknown"}
    assert request_input_tokens(unknown) == len(json.dumps(unknown, ensure_ascii=False).encode()) + 256


def test_batch_cannot_multiply_output_past_model_or_context_cap():
    config = {"model_id": "gpt-6-luna"}
    body = {"model": "gpt-6-luna", "input": "公式", "max_output_tokens": 128000}
    assert batch_fits(config, body, 920000)
    assert not batch_fits(config, {**body, "max_output_tokens": 256000}, 920000)
    assert not batch_fits(config, body, 1)


def test_candidate_cap_never_discards_required_or_old_references():
    from vks_api.interactive_ai import merge_guided_candidates

    rows = [{"revision_id": str(i), "document_revision_id": "same"} for i in range(8)]
    result = merge_guided_candidates(rows[:6], rows[6:], limit=2, per_document=1, protected={"0", "6", "7"})
    assert [row["revision_id"] for row in result] == ["0", "6", "7"]


def test_cold_tokenizer_is_shared_across_concurrent_associations(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from tiktoken import load
    from vks_api.token_budget import _encoding, _load_encoding

    _load_encoding.cache_clear()
    original = load.load_tiktoken_bpe
    reads = []

    def counted(*args, **kwargs):
        reads.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(load, "load_tiktoken_bpe", counted)
    with ThreadPoolExecutor(max_workers=16) as pool:
        encodings = list(pool.map(lambda _: _encoding(), range(16)))
    assert len(reads) == 1 and len({id(encoder) for encoder in encodings}) == 1
