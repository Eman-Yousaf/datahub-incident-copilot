"""The Incident Copilot agent loop.

Built during the "core agent loop" milestone as a real ReAct-style LangGraph agent (all
DataHub MCP tools, read + mutation, bound directly -- no hardcoded call sequence). Before
writing this, the concrete decision points below must be nailed down in the system
prompt/tool design, per the plan:

  - stop condition for the upstream walk: keep going only while no schema-change /
    quality signal has been found and the hop-limit safety cap hasn't been hit
  - ambiguous multi-parent lineage: prioritize a branch using recency of change /
    query frequency (get_dataset_queries), and say why
  - write-back choice: tag-only vs tag+note vs higher-severity structured property,
    chosen from what was actually found, not a fixed branch

TODO(milestone 3): implement build_agent() once mutation tools + datapack trigger
points are confirmed (milestones 1-2).
"""
