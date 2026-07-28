"""Write-back actions the agent can choose between once it has findings.

The agent (not this module) decides which action fits: tag-only for low-severity
findings, tag + note for a confirmed root cause with moderate blast radius, or a
higher-severity structured property when the blast radius includes an ML model or
multiple dashboards. These are exposed to the agent as MCP mutation tools directly
(see mcp_client.py); this module holds the write-back helpers used outside the agent
loop, e.g. for the final summary written to examples/.
"""
