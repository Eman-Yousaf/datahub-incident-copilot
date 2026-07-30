"""Entry point: python cli.py "our revenue dashboard looks wrong" """

import asyncio
import sys

from dotenv import load_dotenv

from incident_copilot.agent import build_agent
from incident_copilot.mcp_client import get_datahub_tools
from incident_copilot.narrate import LiveNarrationHandler


async def run(incident_report: str) -> None:
    load_dotenv()
    tools = await get_datahub_tools()
    agent = build_agent(tools)

    print(f"Incident Copilot investigating: {incident_report!r}\n")

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": incident_report}]},
        config={"callbacks": [LiveNarrationHandler()], "recursion_limit": 50},
    )

    print("\n=== Final report ===")
    print(result["messages"][-1].content)

    print("\n=== Tool-call trace ===")
    for msg in result["messages"]:
        for call in getattr(msg, "tool_calls", None) or []:
            print(f"  {call['name']}({call['args']})")


def main() -> None:
    if len(sys.argv) < 2:
        print('usage: python cli.py "<incident report>"')
        raise SystemExit(1)
    asyncio.run(run(sys.argv[1]))


if __name__ == "__main__":
    main()
