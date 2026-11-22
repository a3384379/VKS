"""Local setup and the installed connector's stdio entry point."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import shutil
import sys
import tomllib
import warnings
from pathlib import Path

from .clinic import doctor
from .config import Connection, connection_path, default_directory, load_connection, save_connection
from .credentials import credential_store, load_token, session_variable, token_reference, validate_token
from .gateway import READONLY_TOOLS, REQUIREMENTS_READONLY, VERSION, APIBridge, run_stdio
from .library import LibraryStore
from .library_embeddings import DEFAULT_ENDPOINT, EmbeddingSettings, LibraryEmbeddings
from .library_mcp import run_library_stdio


def connection_arguments(parser):
    parser.add_argument("--connection", default="default")


def output_config(args, connection):
    transport = args.transport
    settings = {"enabled": True, "startup_timeout_sec": 15, "tool_timeout_sec": 40}
    if transport == "stdio":
        executable = args.command or (sys.executable if getattr(sys, "frozen", False) else shutil.which("vks-connect"))
        if not executable or not Path(executable).is_absolute() or not Path(executable).is_file():
            raise ValueError("请通过 --command 指定连接器的准确绝对可执行文件路径")
        config_dir = args.config_dir or os.getenv("VKS_CONNECT_CONFIG_DIR")
        profile_args = ["--config-dir", str(Path(config_dir).resolve())] if config_dir else []
        settings.update(
            command=str(Path(executable).resolve()),
            args=[*profile_args, "mcp", "--profile", REQUIREMENTS_READONLY, "--connection", args.connection],
            env_vars=[session_variable("api")] if args.session else [],
        )
    else:
        if not connection.mcp_url:
            raise ValueError("HTTP 模式需要先通过 configure 配置只读 MCP URL")
        settings["url"] = connection.mcp_url
        if args.session:
            settings["bearer_token_env_var"] = session_variable("mcp")
    settings["enabled_tools"] = list(READONLY_TOOLS)
    if args.existing:
        with args.existing.open(encoding="utf-8-sig") as stream:
            raw = stream.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ValueError("现有 TOML 文件超出配置比较大小限制")
        existing = tomllib.loads(raw)
        servers = existing.get("mcp_servers", {})
        plugins = existing.get("plugins", {})
        if not isinstance(servers, dict) or not isinstance(plugins, dict):
            raise ValueError("现有 TOML 中的插件或 MCP 配置格式无效")
        plugin_ids = [
            key
            for key, value in plugins.items()
            if key.split("@")[0] == "vks" and isinstance(value, dict) and value.get("enabled", True)
        ]
        aliases = [
            key
            for key, value in servers.items()
            if key != "vks"
            and isinstance(value, dict)
            and any(field in settings and settings[field] == value.get(field) for field in ("url", "command"))
        ]
        if plugin_ids or aliases:
            raise ValueError("已有 VKS 插件或同一地址/命令的 MCP 服务，请保留一种注册方式")
        current = servers.get("vks")
        if current is None:
            print("拟新增 mcp_servers.vks。", file=sys.stderr)
        elif not isinstance(current, dict):
            raise ValueError("现有 mcp_servers.vks 配置格式无效")
        elif current == settings:
            print("mcp_servers.vks 已匹配。", file=sys.stderr)
        else:
            fields = sorted(
                {
                    "url",
                    "command",
                    "args",
                    "enabled",
                    "env_vars",
                    "bearer_token_env_var",
                    "startup_timeout_sec",
                    "tool_timeout_sec",
                    "enabled_tools",
                }
                & {name for name in current.keys() | settings.keys() if current.get(name) != settings.get(name)}
            )
            print(f"mcp_servers.vks 已存在，差异字段 {', '.join(fields) or '其他现有字段'}。", file=sys.stderr)
    if args.format == "json":
        # Other clients can use the portable server definition. Secret input
        # remains the client's own environment/authorization configuration.
        if transport == "stdio":
            entry = {"type": "stdio", "command": settings["command"], "args": settings["args"]}
        else:
            entry = {"type": "streamable-http", "url": settings["url"]}
        print(json.dumps({"mcpServers": {"vks": entry}}, ensure_ascii=True, indent=2))
    else:
        print("[mcp_servers.vks]")
        for name, value in settings.items():
            print(f"{name} = {json.dumps(value, ensure_ascii=False)}")


def build_parser():
    parser = argparse.ArgumentParser(prog="vks-connect")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--config-dir", type=Path)
    commands = parser.add_subparsers(dest="action", required=True)

    configure = commands.add_parser("configure", help="保存连接地址与默认项目")
    connection_arguments(configure)
    configure.add_argument("--api-url", default=Connection.api_base_url)
    configure.add_argument("--studio-url", default=Connection.studio_base_url)
    configure.add_argument(
        "--runtime-mode", choices=("same-machine", "documents-only"), default=Connection.runtime_mode
    )
    configure.add_argument("--runtime-url")
    configure.add_argument("--mcp-url")
    configure.add_argument("--default-project-id")

    auth = commands.add_parser("auth", help="在系统密钥库中添加或移除连接凭据")
    auth.add_argument("operation", choices=("set", "remove"))
    auth.add_argument("--target", choices=("api", "mcp"), default="api")
    connection_arguments(auth)

    diagnosis = commands.add_parser("doctor", help="检查 VKS 实例、连接和可读版本")
    diagnosis.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    connection_arguments(diagnosis)

    mcp = commands.add_parser("mcp", help="启动只读 MCP stdio")
    mcp.add_argument("--profile", choices=(REQUIREMENTS_READONLY,), default=REQUIREMENTS_READONLY)
    source = mcp.add_mutually_exclusive_group()
    source.add_argument("--codex-sidebar", action="store_true", help="通过 VKS API 启用 Codex 左栏工作台")
    source.add_argument("--codex-library", action="store_true", help="读取本机成品库并启用 Codex 左栏")
    connection_arguments(mcp)

    library = commands.add_parser("library", help="选择、查看或打开本机成品库")
    library.add_argument("operation", choices=("use", "status", "open"))
    library.add_argument("file", type=Path, nargs="?")
    connection_arguments(library)

    embedding = commands.add_parser("embedding", help="管理成品库的查询嵌入连接")
    embedding.add_argument("operation", choices=("set", "status", "remove"))
    embedding.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    connection_arguments(embedding)

    for action in ("codex-config", "config-diff"):
        config = commands.add_parser(action, help="生成或比较配置片段")
        connection_arguments(config)
        config.add_argument("--format", choices=("toml", "json"), default="toml")
        config.add_argument("--transport", choices=("stdio", "http"), default="stdio")
        config.add_argument("--session", action="store_true")
        config.add_argument("--command", type=Path)
        config.add_argument("--existing", type=Path, required=action == "config-diff")
        config.add_argument("--dry-run", action="store_true", help="仅输出到终端")
    return parser


def _read_secret(prompt="VKS bearer token: "):
    if not sys.stdin.isatty():
        raise ValueError("auth set 需要交互式终端，请从终端输入凭据")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return validate_token(getpass.getpass(prompt))
        except (EOFError, getpass.GetPassWarning):
            raise ValueError("终端无法安全读取凭据") from None


def main(argv=None):
    # Console redirects on Windows can otherwise choose a legacy code page.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = build_parser().parse_args(argv)
    root = args.config_dir or os.getenv("VKS_CONNECT_CONFIG_DIR") or default_directory()
    try:
        path = connection_path(Path(root), args.connection)
        if args.action == "embedding":
            store = LibraryStore(path.with_suffix(".library.json"))
            embedding = LibraryEmbeddings(store.preference_path.with_suffix(".embedding.json"))
            if args.operation == "set":
                vectors = store.current().vectors
                if not vectors:
                    raise ValueError("当前成品库没有可用的向量索引。")
                settings = EmbeddingSettings(args.endpoint, vectors.model, vectors.dimensions)
                embedding.configure(settings, _read_secret("VKS embedding API Key: "))
                print("已在系统密钥库保存嵌入服务 Key。")
            elif args.operation == "remove":
                embedding.reset_key()
                print("已移除当前嵌入服务 Key，搜索可继续使用关键词。")
            else:
                settings = embedding.settings()
                print(
                    json.dumps(
                        {
                            "configured": settings is not None,
                            "endpoint": settings.endpoint if settings else None,
                            "model": settings.model if settings else None,
                        },
                        ensure_ascii=False,
                    )
                )
            return 0
        if args.action == "library" or (args.action == "mcp" and args.codex_library):
            store = LibraryStore(path.with_suffix(".library.json"))
            if args.action == "mcp":
                asyncio.run(run_library_stdio(store, VERSION))
            elif args.operation == "open":
                from .library_reader import open_reader

                open_reader(args.file, Path(root))
            elif args.operation == "use" and args.file is not None:
                print(json.dumps(store.select(args.file).context(), ensure_ascii=False, indent=2))
            elif args.operation == "status" and args.file is None:
                print(json.dumps(store.current().context(), ensure_ascii=False, indent=2))
            else:
                raise ValueError("用法：library use <成品库 HTML 路径>、library status 或 library open [成品库 HTML 路径]")
            return 0
        if args.action == "configure":
            runtime = args.runtime_url
            if args.runtime_mode == "same-machine" and runtime is None:
                runtime = Connection.runtime_url
            connection = Connection(
                api_base_url=args.api_url,
                studio_base_url=args.studio_url,
                runtime_mode=args.runtime_mode,
                runtime_url=runtime,
                mcp_url=args.mcp_url,
                default_project_id=args.default_project_id,
            )
            save_connection(path, connection)
            print(f"已保存连接 {args.connection}。")
            return 0
        connection = load_connection(path)
        if args.action == "auth":
            endpoint = connection.mcp_url if args.target == "mcp" else connection.api_base_url
            if not endpoint:
                raise ValueError("请先配置凭据对应的连接地址")
            reference = token_reference(path, args.target, endpoint)
            store = credential_store()
            if args.operation == "set":
                store.set(reference, _read_secret())
            else:
                store.remove(reference)
            print("已更新系统密钥库。")
        elif args.action == "doctor":
            if args.transport == "http" and not connection.mcp_url:
                raise ValueError("请先配置只读 MCP URL")
            result = asyncio.run(doctor(connection, path, args.transport))
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0 if result["ok"] else 1
        elif args.action == "mcp":
            token = load_token(path, "api", connection.api_base_url)
            asyncio.run(
                run_stdio(
                    args.profile,
                    APIBridge(connection.api_base_url, token),
                    default_project_id=connection.default_project_id,
                    sidebar_studio_url=connection.studio_base_url if args.codex_sidebar else None,
                )
            )
        else:
            output_config(args, connection)
        return 0
    except (OSError, UnicodeError):
        print("VKS 连接器无法读取文件或访问系统密钥库，请检查本机路径与权限。", file=sys.stderr)
    except tomllib.TOMLDecodeError:
        print("现有配置不是有效的 UTF-8 TOML 文件。", file=sys.stderr)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
    return 1
