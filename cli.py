"""Entry point: python cli.py "our revenue dashboard looks wrong" """

import asyncio
import sys

from dotenv import load_dotenv

from incident_copilot.agent import build_agent
from incident_copilot.mcp_client import datahub_tools
from incident_copilot.narrate import LiveNarrationHandler


async def run(incident_report: str) -> None:
    load_dotenv()
    print(f"Incident Copilot investigating: {incident_report!r}\n")

    async with datahub_tools() as tools:
        agent = build_agent(tools)
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
