"""Compatibility entry point for the full or read-only VKS MCP gateway."""

import json

from .connector_source import ROOT
from .version import VERSION

# Source checkouts and the legacy service image use the live contract; the
# independent connector wheel instead embeds the generated contract.
# isort: split
from vks_connect import gateway
from vks_connect.gateway import (  # noqa: F401
    FULL,
    REQUIREMENTS_READONLY,
    APIBridge,
    GatewayError,
    create_http_app,
    create_server,
    main,
    public_projection,
    run_stdio,
    tools_for_profile,
)

gateway.install_catalog(
    json.loads((ROOT / "packages/contracts/mcp_tools.resolved.json").read_text(encoding="utf-8")), VERSION
)
CATALOG = gateway.CATALOG
TOOLS = gateway.TOOLS
READONLY_TOOLS = gateway.READONLY_TOOLS
HTTP_TOKEN = gateway.HTTP_TOKEN

if __name__ == "__main__":
    main()
