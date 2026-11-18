"""Purpose-specific budgets and offline text estimates, never provider calls.

o200k_base counts plain text for supported models. GPT-6 has no published
offline encoding mapping: use that encoding with 10% headroom as an estimate,
not an exact provider token count. Unknown providers retain the byte upper
bound. Request/schema framing gets a further 256 tokens of headroom.
"""

import json
import math
import re
import threading
from functools import lru_cache
from pathlib import Path

ASSOCIATION_TEXT_LIMIT = 512000
_ENCODING_LOCK = threading.Lock()


def is_luna(model):
    return bool(re.fullmatch(r"gpt-6-luna(?:-\d{4}-\d{2}-\d{2})?", model))


def association_policy(config):
    result = dict(config)
    luna = is_luna(config["model_id"])
    result["max_output_tokens"] = config.get("association_max_output_tokens") or (
        16000 if luna else config["max_output_tokens"]
    )
    requested = config.get("association_max_input_tokens") or (128000 if luna else config["max_input_tokens"])
    result["max_input_tokens"] = min(requested, 1050000 - result["max_output_tokens"] - 2000) if luna else requested
    return result


def _encoding():
    # functools caches do not coalesce concurrent misses. Initial association
    # waves can have 128 workers; construct the vocabulary only once.
    with _ENCODING_LOCK:
        return _load_encoding()


@lru_cache(maxsize=1)
def _load_encoding():
    # Regex and vocabulary are from OpenAI tiktoken (MIT). Keep vocabulary
    # local so an ordinary preflight never downloads files or contacts a host.
    import tiktoken
    from tiktoken.load import load_tiktoken_bpe

    pattern = "|".join(  # noqa: FLY002 -- match the upstream tokenizer regex exactly
        [
            r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
            r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
            r"\p{N}{1,3}",
            r" ?[^\s\p{L}\p{N}]+[\r\n/]*",
            r"\s*[\r\n]+",
            r"\s+(?!\S)",
            r"\s+",
        ]
    )
    ranks = load_tiktoken_bpe(
        str(Path(__file__).with_name("tokenizers") / "o200k_base.tiktoken"),
        expected_hash="446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d",
    )
    return tiktoken.Encoding(name="vks_o200k_base", pat_str=pattern, mergeable_ranks=ranks, special_tokens={})


def request_input_tokens(body):
    text = json.dumps(body, ensure_ascii=False)
    model = body.get("model", "")
    if model.startswith(("gpt-6", "gpt-5", "gpt-4o", "gpt-4.1", "o1", "o3", "o4-mini")):
        count = len(_encoding().encode_ordinary(text))
        return math.ceil(count * 1.1) + 256
    return len(text.encode()) + 256


def batch_fits(config, body, limit):
    output = body.get("max_output_tokens", body.get("max_completion_tokens", 0))
    inputs = request_input_tokens(body)
    return output <= 128000 and inputs <= limit and (not is_luna(config["model_id"]) or inputs + output <= 1048000)
