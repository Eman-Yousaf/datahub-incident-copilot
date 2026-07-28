"""Live first-person narration of the agent's investigation.

Implemented as a LangChain callback handler that prints tool_start/tool_end events as
they happen, plus the LLM's own between-step commentary -- narration is a side effect of
the agent's real reasoning trace, not text generated after the fact. Filled in during the
"live narration layer" milestone, after the core agent loop's decision points are working.
"""

from langchain_core.callbacks.base import BaseCallbackHandler


class LiveNarrationHandler(BaseCallbackHandler):
    def on_tool_start(self, serialized, input_str, **kwargs):
        name = serialized.get("name", "tool")
        print(f"  -> calling {name}({input_str})")

    def on_tool_end(self, output, **kwargs):
        print(f"  <- {output}")
