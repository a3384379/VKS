"""Real SDK subprocess handshake; no protocol-shaped mocks."""

import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_official_sdk_stdio_handshake_and_schema_permissions():
    env = {**os.environ, "PYTHONPATH": str(ROOT / "services/api"), "VKS_TOKEN": ""}
    params = StdioServerParameters(command=sys.executable, args=["-m", "vks_api.mcp_gateway"], env=env)
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as client:
            initialized = await client.initialize()
            assert initialized.serverInfo.name == "visual-knowledge-studio"
            tools = (await client.list_tools()).tools
            assert len(tools) == 25
            assert not {"approve", "publish"}.intersection({t.name for t in tools})
            for tool in tools:
                assert tool.outputSchema and '"$ref"' not in str(tool.outputSchema)
            templates = await client.list_resource_templates()
            assert len(templates.resourceTemplates) == 4
            assert len((await client.list_prompts()).prompts) == 4
            denied = await client.call_tool("list_projects", {})
            assert denied.isError
            assert denied.structuredContent["error"]["code"] == "VKS_UNAUTHENTICATED"
            # An imported or model-written status can never become a review instruction.
            invalid = await client.call_tool("propose_links", {"status": "confirmed"})
            assert invalid.isError
