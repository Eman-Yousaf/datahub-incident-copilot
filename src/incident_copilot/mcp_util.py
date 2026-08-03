"""Small helpers for dealing with MCP tool results.

MCP tool results are NOT always a bare string -- `langchain_mcp_adapters` often returns
`([{"type": "text", "text": "..."}], {"structured_content": {...}})`, a list of content
blocks. Code that checked `isinstance(content, str)` silently no-op'd on that shape
without ever raising, which hid a real bug for most of this project's life (the
response-trimming built to control token cost was inert the whole time). These helpers
handle both shapes explicitly so that class of silent mismatch can't come back.

Lives in its own module so both mcp_client.py (which wraps tools) and memory.py (which
calls a few of them directly) can use it without importing each other.
"""

import json


def extract_text(content) -> str | None:
    """Pull the text payload out of either result shape, or None if there isn't one."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "".join(parts) if parts else None
    return None


def with_text(content, new_text: str):
    """Rebuild `content` in whatever shape it started in, carrying `new_text`."""
    if isinstance(content, str):
        return new_text
    if isinstance(content, list):
        rebuilt = [
            block
            for block in content
            if not (isinstance(block, dict) and block.get("type") == "text")
        ]
        rebuilt.insert(0, {"type": "text", "text": new_text})
        return rebuilt
    return content


def result_json(result) -> dict:
    """Decode a tool result (tuple-wrapped or not, string or content blocks) into a
    dict. Returns {} rather than raising -- callers here treat an unreadable result as
    "no data", which is always a safe fallback for the memory layer: worst case the
    agent investigates from scratch instead of inheriting.
    """
    content = result[0] if isinstance(result, tuple) else result
    text = extract_text(content)
    if not text:
        return {}
    try:
        decoded = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}
