"""Include the canonical generated MCP contract and reader assets in builds."""

from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist

PACKAGE_ROOT = Path(__file__).resolve().parent
CATALOG = "mcp_tools.resolved.json"
SOURCE_DATA = Path("src/vks_connect")
ASSET_DIR = PACKAGE_ROOT.parents[1] / "services/api/vks_api/reader_assets"
DATA_FILES = {
    CATALOG: PACKAGE_ROOT.parent / "contracts" / CATALOG,
    **{name: ASSET_DIR / name for name in ("markdown-vendor.js", "markdown-vendor.css", "markdown-licenses.txt")},
}


def data_path(name) -> Path:
    packaged = PACKAGE_ROOT / SOURCE_DATA / name
    if packaged.is_file():
        return packaged
    canonical = DATA_FILES[name]
    if canonical.is_file():
        return canonical
    raise FileNotFoundError(f"VKS generated package data is missing: {name}")


class BuildPython(build_py):
    def run(self):
        super().run()
        for name in DATA_FILES:
            self.copy_file(str(data_path(name)), str(Path(self.build_lib) / "vks_connect" / name))


class SourceDistribution(sdist):
    def make_release_tree(self, base_dir, files):
        super().make_release_tree(base_dir, files)
        for name in DATA_FILES:
            self.copy_file(str(data_path(name)), str(Path(base_dir) / SOURCE_DATA / name))


setup(cmdclass={"build_py": BuildPython, "sdist": SourceDistribution})
