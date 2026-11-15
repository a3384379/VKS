"""Partition the fully collected suite without changing fixtures or test behavior."""

from hashlib import sha256
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1] / "tests"


def shard_for_path(path):
    relative = Path(path).resolve().relative_to(TESTS)
    if relative.parts[0] == "ui":
        return "ui"
    if relative.parts[0] == "interactive":
        # Keep file-scoped fixtures together. Hash only the repository-relative
        # path so checkout directory and collection ordering cannot affect it.
        bucket = sha256(relative.as_posix().encode()).digest()[0] % 2
        return ("interactive-a", "interactive-b")[bucket]
    return "core"


def pytest_addoption(parser):
    parser.getgroup("vks-ci").addoption(
        "--vks-ci-shard",
        choices=("core", "interactive-a", "interactive-b", "ui"),
        default=None,
        help="Run one disjoint VKS CI partition.",
    )


def pytest_collection_modifyitems(config, items):
    shard = config.getoption("--vks-ci-shard")
    if shard is None:
        return
    selected, deselected = [], []
    for item in items:
        (selected if shard_for_path(item.path) == shard else deselected).append(item)
    config.hook.pytest_deselected(items=deselected)
    items[:] = selected
