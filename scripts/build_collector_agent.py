"""Build the same deterministic, private-data-free archive served by the API."""

import sys
from pathlib import Path
from hashlib import sha256

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.collector.distribution import distribution

if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("output/collector/vks-collector-2.0.zip")
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = distribution()
    target.write_bytes(raw)
    target.with_suffix(".sha256").write_text(sha256(raw).hexdigest() + "  " + target.name + "\n")
    print(target)
