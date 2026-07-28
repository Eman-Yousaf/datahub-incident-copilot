# DataHub Agent Hackathon — Project Brief

## Deadline
**Aug 11, 2026 @ 2:00am GMT+5** (13 days from July 29, 2026)

## About the hackathon
DataHub is an open-source "context platform" for AI agents — it gives agents structured
knowledge of an organization's data: schemas, lineage, ownership, ML metadata, governance.
The hackathon wants projects that use DataHub (via its MCP Server, Agent Context Kit, or
DataHub Skills) so agents can act on real data context instead of hallucinating or getting stuck.

## Challenge tracks (pick ONE, or combine)
1. **Agents That Do Real Work** — Agent reads DataHub through the MCP Server or Agent
   Context Kit, understands what's connected to what, takes an action, and writes results
   back so the next person/agent inherits the knowledge.
2. **Metadata-Aware Code Generation & Development** — Agent generates production data code
   (transformation models, pipeline DAGs, ingestion scripts, migration code) that works on
   the first try because it reads real schemas/lineage/rules from DataHub before generating.
3. **Production ML Agents** — Agent uses DataHub's end-to-end ML lineage (training data →
   features → models → deployments) to catch silent problems before they cost money.
4. **Open / Wildcard** — Anything creative built on DataHub's open-source stack.

## What must be submitted
- [ ] Live demo / hosted app URL, OR repo with clear setup instructions
- [ ] Public GitHub repo with **Apache 2.0 license** visible in the repo's About section
- [ ] Text description: features, functionality, technologies, data used
- [ ] Demo video, **under 3 minutes**, YouTube or Vimeo, public visibility, showing the
      project actually functioning
- [ ] Optional: `examples/` folder with sample generated outputs (code, queries, reports)
- [ ] Optional: opt in to the feedback survey for bonus prize eligibility

## Judging criteria (in order of what matters)
1. **Use of DataHub** — meaningful use of the context graph / MCP Server / Agent Context
   Kit / DataHub Skills / Analytics Agent. Bonus for contributing back to the graph, not
   just reading from it.
2. **Technical Execution** — does it actually work end-to-end? This is weighted heavily —
   small-and-working beats big-and-broken.
3. **Originality** — should go beyond what DataHub already does out of the box.
4. **Real-World Usefulness** — would a real data/ML/AI platform team actually want this?
5. **Submission Quality** — clear demo video, README, and description a judge can follow
   without needing to ask questions.
6. Bonus: meaningful open-source contributions back to DataHub itself (connectors, skills,
   fixes, docs, RFCs).

## Constraints for this build
- Solo builder, existing background: BS AI student, Agentic AI (LangChain + Groq stack),
  comfortable with Python
- Hard external deadline: GS-JTI test prep due Aug 8 — so realistic hackathon working
  window is smaller than 13 days, likely need to submit by ~Aug 10 to leave buffer
- Preference: pick the **narrowest track that can be built fully working**, not the most
  ambitious one — judging rewards "actually works" over "impressive but broken"

## Suggested starting direction (not final — decide after exploring DataHub docs/SDK)
Track 1 ("Agents That Do Real Work") is likely the best fit: build one agent that reads
DataHub via the MCP Server, does one clearly useful task, and writes the result back.
Keep scope to a single clean use case rather than trying to cover multiple challenge tracks.

## Next steps for whoever picks this up
1. Read DataHub's MCP Server / Agent Context Kit docs and starter kits (linked in the
   hackathon's Resources tab)
2. Pick one narrow, demoable use case within Track 1
3. Build the smallest version that works end-to-end first, then improve
4. Set up the repo early with Apache 2.0 license visible from day one
5. Leave 1-2 days at the end purely for the demo video + README polish
