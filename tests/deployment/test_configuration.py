"""The default deployment must parse before external Docker acceptance runs."""

from pathlib import Path
import yaml


class UniqueMappingLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        # Explicit overrides of an inherited YAML merge are permitted; duplicate
        # keys written twice in the same mapping are a deployment error.
        keys = [key.value for key, _ in node.value]
        if len(keys) != len(set(keys)):
            raise ValueError("Compose contains duplicate mapping keys")
        return super().construct_mapping(node, deep)


def test_compose_has_unique_explicit_mapping_keys_and_runtime_configuration():
    path = Path(__file__).resolve().parents[2] / "deploy/compose.yaml"
    with path.open() as stream:
        config = yaml.load(stream, Loader=UniqueMappingLoader)
    assert config["services"]["api"]["environment"]["VKS_TASK_MODE"] == "celery"
    assert config["services"]["runner"]["environment"]["VKS_RUNNER_ENABLED"] == "${VKS_RUNNER_ENABLED:-0}"
