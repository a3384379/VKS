"""Build and verify the independent VKS connector wheel and native plugin bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO / "packages/vks-connect/src")]

from vks_connect.local_embeddings import ASSETS, PROFILE

from scripts.fetch_vks_local_model import fetch_model

PACKAGE = REPO / "packages/vks-connect"
PLUGIN = REPO / "plugins/vks"
CATALOG = REPO / "packages/contracts/mcp_tools.resolved.json"
READER_ASSETS = REPO / "services/api/vks_api/reader_assets"
PY_EXE = "Scripts/python.exe" if os.name == "nt" else "bin/python"
CLI_EXE = "Scripts/vks-connect.exe" if os.name == "nt" else "bin/vks-connect"
ARCHES = {("darwin", "arm64"): "macos-arm64", ("darwin", "x86_64"): "macos-x64", ("win32", "amd64"): "windows-x64"}


def run(*argv, cwd, capture=False):
    result = subprocess.run(
        [str(arg) for arg in argv],
        cwd=cwd,
        check=True,
        encoding="utf-8",
        stdout=subprocess.PIPE if capture else None,
        env={k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}},
    )
    return result.stdout.strip() if capture else None


def publish_verified(source, directory):
    artifact = directory / source.name
    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    temporary = []
    try:
        with NamedTemporaryFile(dir=directory, delete=False) as stream:
            temporary.append(Path(stream.name))
            with source.open("rb") as original:
                shutil.copyfileobj(original, stream)
        with NamedTemporaryFile(dir=directory, delete=False) as stream:
            temporary.append(Path(stream.name))
            stream.write(f"{digest}  {artifact.name}\n".encode())
        temporary[0].replace(artifact)
        temporary[1].replace(artifact.with_name(artifact.name + ".sha256"))
    finally:
        for path in temporary:
            path.unlink(missing_ok=True)
    return artifact


def make_wheel(uv, work):
    stage = work / "wheel-source"
    stage.mkdir()
    for name in ("pyproject.toml", "setup.py"):
        shutil.copyfile(PACKAGE / name, stage / name)
    source = stage / "src/vks_connect"
    source.mkdir(parents=True)
    for module in (PACKAGE / "src/vks_connect").glob("*.py"):
        shutil.copyfile(module, source / module.name)
    for name in ("sidebar.html", "library_reader.html"):
        shutil.copyfile(PACKAGE / "src/vks_connect" / name, source / name)
    shutil.copyfile(CATALOG, source / CATALOG.name)
    for name in ("markdown-vendor.js", "markdown-vendor.css", "markdown-licenses.txt"):
        shutil.copyfile(READER_ASSETS / name, source / name)
    target = work / "wheel"
    run(uv, "build", "--wheel", "--python", sys.executable, "--out-dir", target, stage, cwd=work)
    (wheel,) = target.glob("*.whl")
    return wheel, source


# Run inside the clean wheel environment, before adding freezer dependencies.
INVENTORY = r"""
import importlib.metadata as md
import importlib.util
import json
import shutil
import sys
from pathlib import Path

excluded = ['sqlalchemy', 'playwright', 'tree_sitter', 'celery']
if len(sys.argv) == 2:
    excluded += ['numpy', 'onnxruntime', 'tokenizers']
assert not any(importlib.util.find_spec(n) for n in excluded)
target = Path(sys.argv[1]); target.mkdir(parents=True, exist_ok=True)
records = []
for d in sorted(md.distributions(), key=lambda d: d.metadata['Name'].lower()):
    name = d.metadata['Name']
    paths = []
    for file in d.files or []:
        if not any(part.endswith('.dist-info') for part in file.parts):
            continue
        if not ('licenses' in file.parts or file.name.lower().startswith(('license', 'copying', 'notice'))):
            continue
        source = d.locate_file(file)
        if source.is_file():
            dest = target / name / file.name
            dest.parent.mkdir(exist_ok=True)
            shutil.copyfile(source, dest)
            paths.append(dest.relative_to(target).as_posix())
    records.append({'name': name, 'version': d.version,
                    'license': d.metadata.get('License-Expression') or d.metadata.get('License') or
                               ', '.join(c for c in d.metadata.get_all('Classifier', []) if c.startswith('License ::')),
                    'license_files': paths})
(target / 'dependencies.json').write_text(json.dumps(records, indent=2, ensure_ascii=True) + '\n', encoding='utf-8')
"""


def bundle(uv, work, out, source, python, installer, platform_name, model_dir):
    manifest = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    version = manifest["version"]
    wheel_version = tomllib.loads((PACKAGE / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if version.replace("-rc.", "rc") != wheel_version:
        raise ValueError("Plugin and wheel versions differ")
    run(
        uv,
        "pip",
        "sync",
        "--python",
        python,
        "--require-hashes",
        PACKAGE / "requirements.lock.txt",
        PACKAGE / "freeze.lock.txt",
        PACKAGE / "offline.lock.txt",
        cwd=work,
    )
    # Sync intentionally pruned the first wheel before installing the staged
    # wheel again without dependency resolution.
    run(uv, "pip", "install", "--python", python, "--no-deps", installer, cwd=work)
    run(python, "-c", INVENTORY, work / "licenses", "offline", cwd=work)
    extra = ["--target-arch", platform.machine()] if sys.platform == "darwin" else []
    run(
        python,
        "-m",
        "PyInstaller",
        "--onedir",
        "--noupx",
        "--name",
        "vks-connect",
        "--clean",
        "--collect-data",
        "vks_connect",
        "--collect-data",
        "jsonschema_specifications",
        "--collect-binaries",
        "onnxruntime",
        "--recursive-copy-metadata",
        "vks-connect",
        "--distpath",
        work / "frozen",
        *extra,
        source / "__main__.py",
        cwd=work,
    )
    root = work / "plugin"
    shutil.copytree(PLUGIN / "skills", root / "skills")
    shutil.copyfile(PLUGIN / "plugin.json", root / "plugin.json")
    shutil.copytree(work / "licenses", root / "licenses")
    shutil.copyfile(PACKAGE / "licenses/CPython-LICENSE.txt", root / "licenses/CPython-LICENSE.txt")
    shutil.copyfile(PACKAGE / "requirements.lock.txt", root / "licenses/requirements.lock.txt")
    shutil.copyfile(PACKAGE / "freeze.lock.txt", root / "licenses/freeze.lock.txt")
    shutil.copyfile(PACKAGE / "offline.lock.txt", root / "licenses/offline.lock.txt")
    shutil.copyfile(PACKAGE / "licenses/BGE-LICENSE.txt", root / "licenses/BGE-LICENSE.txt")
    shutil.copyfile(REPO / "docs/upgrades/v3.1-plugin/连接器安装与诊断.md", root / "README.md")
    shutil.copyfile(REPO / "docs/upgrades/v3.1-plugin/windows-codex.md", root / "windows-codex.md")
    shutil.copyfile(REPO / "docs/upgrades/v3.1-plugin/chatgpt-plan.md", root / "chatgpt-plan.md")
    shutil.copytree(work / "frozen/vks-connect", root / "bin", symlinks=True)
    if os.name == "nt":
        (root / "Open VKS Library.cmd").write_text(
            '@echo off\n"%~dp0bin\\vks-connect.exe" library open\n', encoding="ascii"
        )
    else:
        launcher = root / "Open VKS Library.command"
        launcher.write_text(
            '#!/bin/sh\ncd -- "$(dirname -- "$0")" || exit 1\nexec ./bin/vks-connect library open\n',
            encoding="ascii",
        )
        launcher.chmod(0o755)
    models = root / "models" / PROFILE
    models.mkdir(parents=True)
    for name in ASSETS:
        shutil.copyfile(model_dir / name, models / name)
    exe = "vks-connect.exe" if os.name == "nt" else "vks-connect"
    config = {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
        "mcpServers": {
            "vks": {
                "type": "stdio",
                "command": "./bin/" + exe,
                "args": ["mcp", "--codex-library"],
            }
        },
    }
    (root / "mcp.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    if sys.platform == "darwin":
        run("codesign", "--verify", "--strict", root / "bin" / exe, cwd=work)
    git = shutil.which("git")
    (root / "build-info.json").write_text(
        json.dumps(
            {
                "version": version,
                "platform": platform_name,
                "python": platform.python_version(),
                "os": platform.platform(),
                "contract_sha256": hashlib.sha256((source / CATALOG.name).read_bytes()).hexdigest(),
                "wheel_sha256": hashlib.sha256(installer.read_bytes()).hexdigest(),
                "commit": run(git, "-C", REPO, "rev-parse", "HEAD", cwd=work, capture=True) if git else None,
                "working_tree_clean": not bool(run(git, "-C", REPO, "status", "--porcelain", cwd=work, capture=True))
                if git
                else None,
                "codesign": "adhoc" if sys.platform == "darwin" else "unsigned",
                "notarized": False,
                "local_embedding_profile": PROFILE,
                "local_embedding_assets": {name: asset[2] for name, asset in ASSETS.items()},
            },
            ensure_ascii=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    archive = Path(
        shutil.make_archive(
            str(work / f"vks-plugin-{version}-{platform_name}"), "zip" if os.name == "nt" else "gztar", root_dir=root
        )
    )
    unpack = work / "已解压 插件"
    shutil.unpack_archive(archive, unpack)
    run(python, REPO / "scripts/smoke_vks_connector.py", "--manifest", unpack / "mcp.json", cwd=work)
    print(publish_verified(archive, out))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=REPO / "output/vks-connect")
    parser.add_argument("--native", choices=ARCHES.values(), help="freeze and verify on its native runner")
    parser.add_argument("--model-dir", type=Path, help="cache directory for the pinned native model assets")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        parser.error("Python 3.12 is required")
    host = ARCHES.get((sys.platform, platform.machine().lower()))
    if args.native and host != args.native:
        parser.error(f"requested {args.native} needs its own native host; running {host}")
    if args.native and sys.platform == "darwin":
        translated = subprocess.run(
            ["/usr/sbin/sysctl", "-in", "sysctl.proc_translated"],
            capture_output=True,
            text=True,
            check=False,
        )
        if translated.stdout.strip() == "1":
            parser.error("a translated process cannot produce the native macOS package")
    uv = shutil.which("uv")
    if not uv:
        parser.error("uv is required")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="vks-connect-build-") as directory:
        work = Path(directory)
        wheel, source = make_wheel(uv, work)
        venv = work / "runtime"
        run(uv, "venv", "--python", sys.executable, venv, cwd=work)
        python = venv / PY_EXE
        run(uv, "pip", "sync", "--python", python, "--require-hashes", PACKAGE / "requirements.lock.txt", cwd=work)
        run(uv, "pip", "install", "--python", python, "--no-deps", wheel, cwd=work)
        run(python, "-c", INVENTORY, work / "licenses", cwd=work)
        run(python, REPO / "scripts/smoke_vks_connector.py", "--command", venv / CLI_EXE, cwd=work)
        publish_verified(wheel, out)
        if args.native:
            model_dir = fetch_model((args.model_dir or out / "local-model").resolve())
            bundle(uv, work, out, source, python, wheel, args.native, model_dir)
    print(out)


if __name__ == "__main__":
    main()
