"""Formats the ReAct agent's message stream into readable, live investigation narration.

`cli.py` drives the agent via `astream(..., stream_mode="values")` and calls
`print_new_messages` on each new state snapshot. Narration is a side effect of the
agent's real reasoning trace: the LLM's own between-step commentary (its message content,
which the system prompt asks it to write before/after each tool call) is printed as-is,
and each tool call/result is rendered as a short, human-readable line instead of a raw
JSON dump -- nothing here is generated after the fact.
"""

import json

_TRUNCATE_AT = 400


def _text_of(content) -> str:
    """AIMessage/ToolMessage content is usually a str, but reasoning-tier models can
    return a list of content blocks (e.g. [{"type": "text", "text": "..."}]) -- join
    just the text parts.
    """
    if isinstance(content, list):
        return "".join(
            block.get("text", "") for block in content if isinstance(block, dict)
        )
    return content or ""


def _format_call(call: dict) -> str:
    args = ", ".join(f"{key}={value!r}" for key, value in call["args"].items())
    return f"  -> {call['name']}({args})"


def _format_result(content, tool_name: str | None = None) -> str:
    text = _text_of(content) if not isinstance(content, str) else content
    if tool_name == "report_findings":
        # This is already a short, deliberately-formatted explanation (the
        # confidence checklist + computed severity) -- truncating it would cut off
        # exactly the reasoning it exists to show. Indent each line under the
        # "<-" marker instead of collapsing it to one line.
        return "\n".join(
            f"  <- {line}" if i == 0 else f"     {line}"
            for i, line in enumerate(text.split("\n"))
        )
    try:
        text = json.dumps(json.loads(text))
    except (json.JSONDecodeError, TypeError):
        pass
    if len(text) > _TRUNCATE_AT:
        text = text[:_TRUNCATE_AT].rstrip() + f"... [+{len(text) - _TRUNCATE_AT} chars]"
    return f"  <- {text}"


def format_new_messages(messages, seen: int) -> tuple[list[tuple[bool, str]], int]:
    """Return (blank_line_before, text) pairs for every message past index `seen`, plus
    the new count. Called once per astream() snapshot so narration is available the
    moment each step completes, not only after the whole investigation finishes. Shared
    by cli.py (prints the lines) and webapp.py (streams them to a browser via SSE).
    """
    lines: list[str] = []
    for message in messages[seen:]:
        kind = message.__class__.__name__
        if kind == "AIMessage":
            text = _text_of(message.content).strip()
            calls = getattr(message, "tool_calls", None) or []
            if text and not calls:
                # No further tool calls queued -- this is the concluding summary
                # (system prompt step 7), not intermediate reasoning.
                lines.append((True, "=== Final report ==="))
                lines.append((False, text))
            elif text:
                lines.append((True, text))
            for call in calls:
                lines.append((False, _format_call(call)))
        elif kind == "ToolMessage":
            lines.append((False, _format_result(message.content, getattr(message, "name", None))))
    return lines, len(messages)


def print_new_messages(messages, seen: int) -> int:
    """Print every message past index `seen`, return the new count."""
    lines, new_seen = format_new_messages(messages, seen)
    for blank_before, line in lines:
        print(f"\n{line}" if blank_before else line)
    return new_seen
