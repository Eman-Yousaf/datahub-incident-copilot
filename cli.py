"""Entry point: python cli.py "our revenue dashboard looks wrong" """

import asyncio
import sys

from dotenv import load_dotenv

from incident_copilot.agent import build_agent
from incident_copilot.mcp_client import datahub_tools
from incident_copilot.narrate import print_new_messages


async def run(incident_report: str) -> None:
    load_dotenv()
    print(f"Incident Copilot investigating: {incident_report!r}\n")

    seen = 0
    final_messages = None
    async with datahub_tools() as tools:
        agent = build_agent(tools)
        async for state in agent.astream(
            {"messages": [{"role": "user", "content": incident_report}]},
            config={"recursion_limit": 50},
            stream_mode="values",
        ):
            final_messages = state["messages"]
            seen = print_new_messages(final_messages, seen)

    print("\n=== Tool-call trace ===")
    for msg in final_messages:
        for call in getattr(msg, "tool_calls", None) or []:
            print(f"  {call['name']}({call['args']})")


def main() -> None:
    if len(sys.argv) < 2:
        print('usage: python cli.py "<incident report>"')
        raise SystemExit(1)
    asyncio.run(run(sys.argv[1]))


if __name__ == "__main__":
    main()
