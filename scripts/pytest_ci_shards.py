"""Partition the fully collected suite without changing fixtures or test behavior."""

from pathlib import Path

UI_TESTS = Path(__file__).resolve().parents[1] / "tests" / "ui"


def pytest_addoption(parser):
    parser.getgroup("vks-ci").addoption(
        "--vks-ci-shard", choices=("core", "ui"), default=None, help="Run one disjoint VKS CI partition."
    )


def pytest_collection_modifyitems(config, items):
    shard = config.getoption("--vks-ci-shard")
    if shard is None:
        return
    selected, deselected = [], []
    for item in items:
        is_ui = Path(item.path).resolve().is_relative_to(UI_TESTS)
        (selected if is_ui == (shard == "ui") else deselected).append(item)
    config.hook.pytest_deselected(items=deselected)
    items[:] = selected
