"""Loads DataHub's real showcase-ecommerce datapack into the local quickstart.

Primary path: load the datapack as-is and check whether it already contains a usable
recent schema-change event to serve as an incident trigger. Only if it doesn't: overlay
a single timestamped event onto a real entity -- the lineage graph itself stays genuine,
only the triggering signal is synthetic. See milestone 2 in the plan for the decision.

TODO(milestone 2): implement once the datapack's actual contents have been inspected.
"""
