"""Connects to the DataHub MCP server and exposes its tools as LangChain tools.

Local dev target: `uvx mcp-server-datahub@latest`, configured via DATAHUB_GMS_URL /
DATAHUB_GMS_TOKEN / TOOLS_IS_MUTATION_ENABLED env vars (see .env.example).
"""

import os

from langchain_mcp_adapters.client import MultiServerMCPClient


def build_mcp_client() -> MultiServerMCPClient:
    return MultiServerMCPClient(
        {
            "datahub": {
                "command": "uvx",
                "args": ["mcp-server-datahub@latest"],
                "transport": "stdio",
                "env": {
                    "DATAHUB_GMS_URL": os.environ["DATAHUB_GMS_URL"],
                    "DATAHUB_GMS_TOKEN": os.environ.get("DATAHUB_GMS_TOKEN", ""),
                    "TOOLS_IS_MUTATION_ENABLED": os.environ.get(
                        "TOOLS_IS_MUTATION_ENABLED", "true"
                    ),
                },
            }
        }
    )


async def get_datahub_tools():
    client = build_mcp_client()
    return await client.get_tools()
