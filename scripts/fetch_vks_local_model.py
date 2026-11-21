"""Fetch fixed public model assets for builds, never at query time."""

import argparse
import hashlib
import sys
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages/vks-connect/src"))

from vks_connect.local_embeddings import ASSET_SOURCE, ASSETS, LocalModelError, verified_asset


def fetch_model(target):
    target.mkdir(parents=True, exist_ok=True)
    for name, (remote, size, digest) in ASSETS.items():
        try:
            verified_asset(target, name)
            continue
        except (OSError, LocalModelError):
            pass
        with urlopen(ASSET_SOURCE + remote, timeout=45) as response:
            data = response.read(size + 1)
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError(f"模型资源 {name} 未通过校验。")
        (target / name).write_bytes(data)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "output/vks-connect/local-model")
    print(fetch_model(parser.parse_args().out.resolve()))
