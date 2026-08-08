"""Connects to the DataHub MCP server and exposes its tools as LangChain tools.

Local dev target: `uvx mcp-server-datahub@latest`, configured via DATAHUB_GMS_URL /
DATAHUB_GMS_TOKEN / TOOLS_IS_MUTATION_ENABLED env vars (see .env.example).
"""

import json
import os
from contextlib import asynccontextmanager

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from .decision import SEVERITY_INSTRUCTIONS, build_card, build_report_findings_tool
from .mcp_util import extract_text as _extract_text
from .mcp_util import with_text as _with_text
from .memory import build_recall_tool, build_write_card_tool


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

# Loaded from the MCP server but NOT bound to the agent. These three power the
# persistent-memory layer (memory.py) and are driven from Python instead: recall
# searches/greps stored Investigation Cards, and the card write-back saves one. Keeping
# them out of the model's tool list means which prior investigation gets inherited, and
# what the stored card says, are decided by code -- and it keeps three more multi-KB
# tool schemas out of every turn's token budget.
INTERNAL_TOOL_NAMES = {
    "search_documents",
    "grep_documents",
    "save_document",
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
        "alone. Returns top matches with URNs -- pass promising ones to get_entities. "
        "Results already come back in relevance order; do not pass sort_by/sort_order."
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
        text = _extract_text(content)
        if text is not None:
            try:
                content = _with_text(content, json.dumps(_trim_verbose_fields(json.loads(text))))
            except (json.JSONDecodeError, TypeError):
                pass
        return (content, artifact) if isinstance(result, tuple) else content

    tool.coroutine = trimmed_coroutine
    return tool


TRIMMED_TOOL_NAMES = {"get_entities", "search"}


def _wrap_search_sort_compat(tool):
    """Drop `sort_by`/`sort_order` from `search` calls.

    A recent `mcp-server-datahub` exposes these parameters and the model naturally
    fills in `sort_by="relevance"`, which DataHub OSS rejects outright:

        query_shard_exception: No mapping found for [relevance] in order to sort on
        -> search_phase_execution_exception: all shards failed  (HTTP 400)

    There is no `relevance` field in `datasetindex_v2` to sort on -- relevance is the
    default ranking, not a sortable field -- so every search carrying that argument
    fails, and the agent reads a 400 as "the search backend is down" and correctly
    refuses to act. Left alone it silently disables the entire investigation path.

    Stripped in code rather than discouraged in the prompt, for the same reason the
    severity gate is enforced in code: an instruction the model merely *usually*
    follows isn't a fix when the failure mode is this total.
    """
    original_coroutine = tool.coroutine

    async def compat_coroutine(*args, **kwargs):
        kwargs.pop("sort_by", None)
        kwargs.pop("sort_order", None)
        return await original_coroutine(*args, **kwargs)

    tool.coroutine = compat_coroutine
    return tool


def _wrap_with_provenance(tool, state: dict):
    """Record that this tool really ran, so the Investigation Card's provenance list
    reflects observed calls rather than the model's recollection of what it called.
    """
    original_coroutine = tool.coroutine

    async def tracked_coroutine(*args, **kwargs):
        state.setdefault("tools_used", set()).add(tool.name)
        # A running tally of real DataHub API calls. Every tool wrapped here goes
        # over MCP to GMS, including the ones the automatic drift audit makes on
        # the agent's behalf -- those objects are the same wrapped instances, so
        # the count includes work the model never explicitly asked for, which is
        # the honest number. Appended after the call would undercount a failure;
        # appended before, it reflects what was actually attempted.
        state.setdefault("datahub_calls", []).append(tool.name)
        return await original_coroutine(*args, **kwargs)

    tool.coroutine = tracked_coroutine
    return tool

# Which severity tiers (see decision.py) each mutation tool is authorized to run
# under. add_tags also gets a finer per-call check below, since the severity-high tag
# specifically should only ever go on at the top tier.
_MUTATION_ALLOWED_SEVERITIES = {
    "add_tags": {"tag_only", "tag_and_note", "tag_note_escalated"},
    "update_description": {"tag_and_note", "tag_note_escalated"},
}


def _authorized_targets(tool_name: str, state: dict) -> set[str]:
    """The exact set of entities a mutation tool may touch this run, derived in
    Python from what was actually confirmed -- never from the URNs the model asks
    for.

    Severity tiers answer "how much may I do"; they never answered "to what". That
    gap was real: a run was observed tagging a mirror entity alongside the root
    cause even though the authorization text said "the exact root-cause URN", and
    the schema-drift audit makes it likelier still by surfacing a list of other live
    URNs in report_findings' own output. Prompt wording is not a control -- same
    reasoning as gating on severity in code rather than asking nicely.

    The root cause is where `report_findings` confirmed the signal. `add_tags` may
    additionally flag mirrors the automatic schema-drift audit *proved* are running
    stale schema: those are code-verified findings about those specific entities, so
    flagging them is earned rather than assumed. `update_description` stays
    root-cause-only -- the incident narrative belongs on the entity that caused it,
    not appended onto every mirror that merely inherited the symptom.
    """
    root = state.get("root_cause_urn")
    targets = {root} if root else set()
    if tool_name == "add_tags":
        targets |= {
            mirror["urn"]
            for mirror in (state.get("schema_drift") or {}).get("mirrors_stale", [])
            if mirror.get("urn")
        }
    return targets


def _blocked(mcp_tool, message: str):
    """A refusal, in whatever shape this tool's `response_format` contract demands.

    DataHub's MCP tools are declared `response_format='content_and_artifact'`, so
    returning a bare string raises `ValueError` inside LangChain's tool runner --
    which LangGraph does not recover from. The whole investigation dies with a
    traceback instead of the model reading the refusal and adjusting.

    That made the gate's central promise ("attempting one will just be blocked")
    false end-to-end, and it went unnoticed for a long time because a block only
    fires when the model tries something it was told not to -- which, until the
    target gate landed, it essentially never did. The scratch harness missed it too,
    by using a fake tool that didn't declare the real response_format. A refusal has
    to satisfy exactly the same contract a real result does.
    """
    if getattr(mcp_tool, "response_format", None) == "content_and_artifact":
        return (message, None)
    return message


def _gate_mutation_tool(mcp_tool, state: dict, get_entities_tool):
    """Wraps a mutation tool so it refuses to run until `report_findings` has
    authorized a matching severity tier, and only on the entities this investigation
    actually confirmed something about (see `_authorized_targets`) -- the enforced
    "do not act" path: both checks happen in code, so they can't be reasoned around
    by the model the way a prompt instruction alone could be. On success, also
    re-fetches the entity via get_entities so the result shows the mutation actually
    landed in DataHub, instead of asking the judge to trust a bare `success: true`.
    """
    original_coroutine = mcp_tool.coroutine
    allowed = _MUTATION_ALLOWED_SEVERITIES[mcp_tool.name]

    async def gated_coroutine(*args, **kwargs):
        # A model calling a single-URN/single-tag mutation sometimes passes a bare
        # string instead of a one-item list for `entity_urns`/`tag_urns` -- observed
        # live, not hypothetical. Left alone, `list(dict.fromkeys("urn:li:..."))`
        # iterates the string's *characters*, corrupting both the authorization check
        # (blocks on a nonsense "Not authorized: u, r, n, :, ..." message) and the
        # actions-taken log. The underlying MCP tool accepts a real list just fine
        # (confirmed by the model's own successful retry), so normalize in place here
        # rather than let a shape mismatch silently misbehave -- same family as the
        # `isinstance(content, str)` no-op and the response_format crash found
        # earlier in this file.
        for key in ("entity_urns", "tag_urns"):
            if isinstance(kwargs.get(key), str):
                kwargs[key] = [kwargs[key]]

        # Every gate decision is recorded on the state dict, in order, so the web
        # UI can show the real sequence -- proposed, then allowed or blocked, then
        # applied, then verified -- instead of a hand-drawn animation of what the
        # gate is supposed to do. A blocked attempt is the most informative event
        # here, so it is kept rather than discarded once the model recovers.
        def record(stage: str, **fields) -> None:
            state.setdefault("writeback_events", []).append(
                {"tool": mcp_tool.name, "stage": stage, **fields}
            )

        def refuse(message: str):
            record("blocked", reason=message)
            return _blocked(mcp_tool, message)

        record(
            "proposed",
            entity_urns=list(kwargs.get("entity_urns") or []),
            tag_urns=list(kwargs.get("tag_urns") or []),
        )

        severity = state.get("severity")
        if severity is None:
            return refuse(
                "Blocked: call report_findings first -- it establishes the confidence "
                "level and authorized severity tier this tool checks."
            )
        if severity not in allowed:
            return refuse(
                f"Blocked: severity tier '{severity}' does not authorize "
                f"{mcp_tool.name}. {SEVERITY_INSTRUCTIONS[severity]}"
            )
        if mcp_tool.name == "add_tags":
            tag_urns = kwargs.get("tag_urns") or []
            if "urn:li:tag:incident-severity-high" in tag_urns and severity != "tag_note_escalated":
                return refuse(
                    f"Blocked: severity tier '{severity}' does not authorize the "
                    f"severity-high tag. {SEVERITY_INSTRUCTIONS[severity]}"
                )

        entity_urns = list(
            dict.fromkeys(
                kwargs.get("entity_urns")
                or ([kwargs["entity_urn"]] if "entity_urn" in kwargs else [])
            )
        )
        authorized = _authorized_targets(mcp_tool.name, state)
        unauthorized = [urn for urn in entity_urns if urn not in authorized]
        if unauthorized:
            return refuse(
                f"Blocked: {mcp_tool.name} may only touch entities this investigation "
                f"confirmed something about. Not authorized: {', '.join(unauthorized)}. "
                f"Authorized this run: "
                f"{', '.join(sorted(authorized)) or 'none -- no root cause was confirmed'}."
            )

        record("allowed", entity_urns=entity_urns, severity=severity)
        result = await original_coroutine(*args, **kwargs)
        content, artifact = result if isinstance(result, tuple) else (result, None)
        text = _extract_text(content)

        # Recorded from the call that actually got past the gate and ran, so the
        # Investigation Card's "actions taken" section reports what really happened to
        # the catalog rather than what the model says it did.
        detail = ", ".join(kwargs.get("tag_urns") or []) if mcp_tool.name == "add_tags" else ""
        state.setdefault("actions_taken", []).append(
            f"{mcp_tool.name}({detail}) on {', '.join(entity_urns) or 'unknown entity'}"
            if detail
            else f"{mcp_tool.name} on {', '.join(entity_urns) or 'unknown entity'}"
        )
        record("applied", entity_urns=entity_urns, detail=detail)
        if entity_urns and get_entities_tool is not None and text is not None:
            try:
                after = await get_entities_tool.coroutine(urns=entity_urns)
                after_result = after[0] if isinstance(after, tuple) else after
                after_text = _extract_text(after_result) or repr(after_result)
                content = _with_text(
                    content, f"{text}\n[Verified in DataHub after write-back]: {after_text}"
                )
                record("verified", entity_urns=entity_urns)
            except Exception as exc:  # noqa: BLE001 -- verification is best-effort
                content = _with_text(content, f"{text}\n[Verification read-back failed: {exc}]")
                record("verify_failed", reason=str(exc))

        return (content, artifact) if isinstance(result, tuple) else content

    mcp_tool.coroutine = gated_coroutine
    return mcp_tool


@asynccontextmanager
async def datahub_tools(incident_report: str = ""):
    """Yields `(tools, decision_state)` -- DataHub's MCP tools bound to one
    persistent session for the caller's whole investigation, plus the live policy
    state those tools write into.

    The state dict is handed back rather than kept private so the web UI can render
    the policy layer as it happens (see panel.py) from the very same object the
    mutation gate reads. A UI that reconstructed confidence or severity from the
    narration text could disagree with the gate, and the disagreement would be
    invisible -- which is the opposite of what showing the machinery is for.
    Callers that don't need it, like cli.py, can ignore the second element.

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
        by_name = {tool.name: tool for tool in tools}
        filtered = [tool for tool in tools if tool.name in ALLOWED_TOOL_NAMES]

        # One dict threaded through every wrapper below. It is the only channel by
        # which the policy layer, the mutation gate, and the memory layer share state
        # -- and nothing the model emits can write to it directly.
        decision_state: dict = {"trigger": incident_report}

        for tool in filtered:
            if tool.name in CONCISE_DESCRIPTIONS:
                tool.description = CONCISE_DESCRIPTIONS[tool.name]
            if tool.name in TRIMMED_TOOL_NAMES:
                _wrap_with_trimming(tool)
            if tool.name == "search":
                _wrap_search_sort_compat(tool)

        get_entities_tool = by_name.get("get_entities")
        for tool in filtered:
            if tool.name in _MUTATION_ALLOWED_SEVERITIES:
                _gate_mutation_tool(tool, decision_state, get_entities_tool)
            _wrap_with_provenance(tool, decision_state)

        filtered.append(
            build_report_findings_tool(
                decision_state, by_name.get("get_lineage"), by_name.get("list_schema_fields")
            )
        )

        # The persistent-memory layer. These three MCP tools stay internal (see
        # INTERNAL_TOOL_NAMES) and are called from Python, so the agent gets exactly
        # two memory tools: one to read prior investigations, one to record this one.
        missing = INTERNAL_TOOL_NAMES - by_name.keys()
        if missing:
            # Only possible against an older DataHub than the documents tools require
            # (oss >= 1.4.0) or with save_document disabled server-side. Degrade to the
            # previous single-shot behaviour rather than failing the whole run.
            print(
                f"[incident-copilot] persistent memory disabled -- MCP server did not "
                f"expose: {', '.join(sorted(missing))}"
            )
        else:
            filtered.append(
                build_recall_tool(
                    decision_state, by_name["search_documents"], by_name["grep_documents"]
                )
            )
            filtered.append(
                build_write_card_tool(
                    decision_state,
                    by_name["save_document"],
                    by_name["grep_documents"],
                    build_card,
                )
            )

        yield filtered, decision_state
