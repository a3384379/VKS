"""Deterministic source distribution. A fixed allowlist excludes instance data."""

from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import zipfile

LAUNCHER = """@echo off
setlocal
set PYTHONUTF8=1
cd /d "%~dp0"
py -3.12 --version >nul 2>nul
if errorlevel 1 (
  echo Install official Python 3.12 for Windows with Tcl/Tk and the Python launcher.
  pause
  exit /b 1
)
if not exist .venv\\Scripts\\python.exe py -3.12 -m venv .venv
if errorlevel 1 exit /b 1
.venv\\Scripts\\python.exe -m pip install --require-hashes -r requirements.lock.txt
if errorlevel 1 (
  echo Dependency setup failed. No source folder has been collected.
  pause
  exit /b 1
)
.venv\\Scripts\\pythonw.exe -m packages.collector.agent_gui
"""
README = """# VKS 2.0 本机只读采集 Agent

1. 在个人安装目录解压，不要覆盖业务仓库。安装官方 Python 3.12（包含 Tcl/Tk 与 py 启动器）。
2. 双击 start-windows.cmd；首次按带哈希的锁文件安装依赖。支持目录、本机 Git ref、SVN 固定数字 revision；Git/SVN 客户端由用户预先安装。
3. 在 VKS 项目设置创建仅有本项目 knowledge:read、source:read、source:import 的专用令牌。填写站点、项目与稳定来源，选择目录并勾选本次授权。
4. 可先预览，再同步。每次固定内容/版本，忽略链接、目录联接、秘密与构建目录；不执行资料中的脚本或 hooks。待服务器任务成功才推进游标，断网按原回执恢复。
5. 周期检查只在本程序打开且本会话明确勾选时运行，关闭后停止；下次打开需重新输入令牌并授权。凭据只在内存，授权配置、原始待上传包与回执位于用户目录 .vks/collector/。
6. 空目标会建立独立草稿；合入既有草稿需选择该草稿，已发布版本不可写入。流程、关系与文档仍需人员审核。

源码清单和 SHA-256 在 distribution-manifest.json。Mac/Linux 可在相同依赖环境运行 python -m packages.collector.agent_gui；Windows 原生文件句柄测试与跨平台核心测试分别执行，不能互相替代。
"""


def distribution(root=None):
    root = Path(root or Path(__file__).resolve().parents[2]).resolve()
    files = {"start-windows.cmd": LAUNCHER.replace("\n", "\r\n").encode(), "README.md": README.encode()}
    paths = [
        *root.joinpath("packages").rglob("*.py"),
        *root.joinpath("packages/contracts").glob("*.json"),
        root / "deploy/requirements.lock.txt",
    ]
    for path in sorted(paths):
        relative = path.relative_to(root)
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(root)
            or any(part.startswith(".") or part == "__pycache__" for part in relative.parts)
        ):
            continue
        name = "requirements.lock.txt" if path.name == "requirements.lock.txt" else relative.as_posix()
        files[name] = path.read_bytes()
    manifest = dict(
        version="2.0-development",
        files=[
            dict(path=name, size_bytes=len(raw), sha256=sha256(raw).hexdigest()) for name, raw in sorted(files.items())
        ],
    )
    files["distribution-manifest.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
        for name, raw in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            z.writestr(info, raw)
    return output.getvalue()
