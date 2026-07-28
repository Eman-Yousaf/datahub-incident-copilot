"""Entry point: python cli.py "our revenue dashboard looks wrong"

TODO(milestone 3+): wire up once incident_copilot.agent.build_agent() exists.
"""

import sys


def main() -> None:
    if len(sys.argv) < 2:
        print('usage: python cli.py "<incident report>"')
        raise SystemExit(1)
    incident_report = sys.argv[1]
    print(f"Incident Copilot investigating: {incident_report!r}")
    raise NotImplementedError("agent loop not wired up yet -- see incident_copilot/agent.py")


if __name__ == "__main__":
    main()
