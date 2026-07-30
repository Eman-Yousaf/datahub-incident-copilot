"""Connects to the DataHub MCP server and exposes its tools as LangChain tools.

Local dev target: `uvx mcp-server-datahub@latest`, configured via DATAHUB_GMS_URL /
DATAHUB_GMS_TOKEN / TOOLS_IS_MUTATION_ENABLED env vars (see .env.example).
"""

import json
import os
from contextlib import asynccontextmanager

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools


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
                    # DATA_QUALITY_TOOLS_ENABLED is intentionally not set: the one tool
                    # it gates (get_dataset_assertions) is Cloud-only (@min_version has
                    # no `oss` minimum) and gets filtered out server-side regardless on
                    # this OSS quickstart, so enabling it here would be a no-op.
                },
            }
        }
    )


# Keep this in sync with the tools referenced in agent.py's SYSTEM_PROMPT. The MCP
# server exposes ~20 tools (most mutation tools we don't use: remove_tags, owners,
# domains, structured properties, save_document, etc.), each with a lengthy docstring
# baked into its schema -- binding all of them blew past Groq's free-tier 12k TPM cap
# on the very first call (32k+ tokens just for tool schemas, before any reasoning).
ALLOWED_TOOL_NAMES = {
    "search",
    "get_entities",
    "list_schema_fields",
    "get_lineage",
    "get_dataset_queries",
    "add_tags",
    "update_description",
}

# The MCP server's own docstrings for these tools are each multi-KB (full worked
# examples, filter syntax reference, etc.) -- useful for a general-purpose client, but
# binding all 7 as-is still blew past Groq's free-tier 12k TPM cap even after cutting
# the tool count from ~20 to 7. These are trimmed, task-specific replacements: everything
# the agent actually needs for this investigation, nothing else.
CONCISE_DESCRIPTIONS = {
    "search": (
        "Search DataHub entities by keyword. query='/q term1+term2' requires both "
        "terms; 'term1 OR term2' matches either. filter='entity_type = dataset' narrows "
        "by type -- valid values are dataset, dashboard, chart, dataFlow, dataJob, "
        "container, domain, tag, glossaryTerm, document (all lowercase, no others -- "
        "'report' is NOT a valid type; PowerBI/Tableau/Looker reports are usually typed "
        "'dataset' here). If unsure of the type, omit the filter and search by keyword "
        "alone. Returns top matches with URNs -- pass promising ones to get_entities."
    ),
    "get_entities": (
        "Get full details (schema, properties, tags) for one or more entity URNs. Pass "
        "an array of the top candidate URNs from a search result to compare them."
    ),
    "list_schema_fields": (
        "List a dataset's schema fields, optionally filtered by keyword. Each field "
        "includes a description and a lastModified timestamp -- compare timestamps "
        "across fields to spot one that changed noticeably more recently than the rest."
    ),
    "get_lineage": (
        "Get lineage for an entity: upstream=True for upstream (what feeds it), "
        "upstream=False for downstream (what depends on it). max_hops controls how far "
        "to walk (default 1 -- direct parents/children only)."
    ),
    "get_dataset_queries": (
        "Get real SQL queries referencing a dataset. source='SYSTEM' for BI/dashboard "
        "queries (production usage), 'MANUAL' for user-written ones. Use this to judge "
        "which of several candidate upstream tables is actually load-bearing."
    ),
    "add_tags": (
        "Add tag URN(s) to entity URN(s). Only these tags exist: "
        "urn:li:tag:incident-flagged, urn:li:tag:incident-severity-high."
    ),
    "update_description": (
        "Set (operation='replace') or append (operation='append') a markdown "
        "description on an entity -- use append to add an incident note without "
        "overwriting the existing description."
    ),
}


# `get_entities` (and to a lesser extent `search`) returns the full GraphQL entity
# payload verbatim -- domains, data products, health checks, browse paths, glossary
# terms, structured properties -- none of which the agent's decision framework needs.
# Left untrimmed, this bloats every turn's conversation history and can blow a later
# tool call's token budget even after the initial tool-schema overhead is fixed
# (confirmed: openai/gpt-oss-20b hit its 8k TPM cap mid-investigation, not on the first
# call, from accumulated get_entities results). Drop these keys recursively.
_VERBOSE_KEYS_TO_DROP = {
    "domain",
    "dataProduct",
    "dataProducts",
    "health",
    "parentDomains",
    "browsePathV2",
    "glossaryTerms",
    "structuredProperties",
    "businessAttributes",
    "children",
}


def _trim_verbose_fields(obj):
    if isinstance(obj, dict):
        return {
            key: _trim_verbose_fields(value)
            for key, value in obj.items()
            if key not in _VERBOSE_KEYS_TO_DROP
        }
    if isinstance(obj, list):
        return [_trim_verbose_fields(item) for item in obj]
    return obj


def _wrap_with_trimming(tool):
    original_coroutine = tool.coroutine

    async def trimmed_coroutine(*args, **kwargs):
        result = await original_coroutine(*args, **kwargs)
        content, artifact = result if isinstance(result, tuple) else (result, None)
        if isinstance(content, str):
            try:
                content = json.dumps(_trim_verbose_fields(json.loads(content)))
            except (json.JSONDecodeError, TypeError):
                pass
        return (content, artifact) if isinstance(result, tuple) else content

    tool.coroutine = trimmed_coroutine
    return tool


TRIMMED_TOOL_NAMES = {"get_entities", "search"}


@asynccontextmanager
async def datahub_tools():
    """Yields DataHub's MCP tools bound to one persistent session for the caller's
    whole investigation.

    `MultiServerMCPClient.get_tools()` opens a fresh stdio session -- and therefore a
    fresh `uvx mcp-server-datahub` subprocess -- for every single tool invocation, not
    just once at startup (confirmed via server logs: 15 separate "Starting MCP server"
    lines across one investigation). Using `client.session(...)` + `load_mcp_tools`
    instead holds one session open for the duration of the `async with` block, so a
    multi-turn ReAct investigation reuses one subprocess instead of restarting it on
    every tool call.
    """
    client = build_mcp_client()
    async with client.session("datahub") as session:
        tools = await load_mcp_tools(session)
        filtered = [tool for tool in tools if tool.name in ALLOWED_TOOL_NAMES]
        for tool in filtered:
            if tool.name in CONCISE_DESCRIPTIONS:
                tool.description = CONCISE_DESCRIPTIONS[tool.name]
            if tool.name in TRIMMED_TOOL_NAMES:
                _wrap_with_trimming(tool)
        yield filtered
