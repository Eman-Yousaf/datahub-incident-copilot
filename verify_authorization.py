"""Recompute every authorization this agent ever issued, straight out of DataHub.

    python verify_authorization.py                 # every stored card
    python verify_authorization.py --urn urn:li:document:...
    python verify_authorization.py --file card.json

The project's central claim is that an autonomous write is permitted by deterministic
policy rather than by the model's judgement, and that the permission is identified by
a hash of the grounds it rested on. That is a testable claim, so it should not have to
be believed.

This script takes no input from the agent and imports no policy decision. It reads the
Investigation Cards out of the catalog, pulls the recorded `authorization_core` off
each one -- the predicates, their verdicts, the permitted targets, the policy version
-- and recomputes the authorization id and hash from that alone, using the same two
functions the live system uses. Then it compares.

A PASS means: the id printed on that card is genuinely a function of the grounds
recorded next to it. The number was derived, not decorated. A FAIL means the card and
its id disagree, which is exactly what you would want to find out.

What this does NOT prove, stated so nobody reads more into it than it earns: it checks
that a card is internally consistent with the policy code in this repository. It cannot
tell you the predicates were observed honestly at the time -- that is what the live
re-check in the mutation gate is for, and what `counterfactual.py` demonstrates. Two
different questions, deliberately answered by two different programs.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from incident_copilot.authorization import authorization_id, proof_hash  # noqa: E402
from incident_copilot.memory import InvestigationCard, parse_card  # noqa: E402

load_dotenv()

BAR = "─" * 78


def verify(card: InvestigationCard) -> tuple[bool, str]:
    """Recompute the id and hash from the card's own recorded grounds."""
    core = card.authorization_core
    if not core:
        return True, "no authorization recorded (card predates the proof layer)"

    want_id = card.authorization_id
    want_hash = card.authorization_hash
    got_id = authorization_id(core)
    got_hash = proof_hash(core)

    if got_id != want_id:
        return False, f"id mismatch: card says {want_id}, grounds recompute to {got_id}"
    if want_hash and got_hash != want_hash:
        return False, f"hash mismatch: card says {want_hash}, grounds recompute to {got_hash}"

    live = [p for p in core.get("predicates", []) if p.get("kind", "").startswith("schema_field")]
    targets = sorted(
        {urn for urns in (core.get("authorized_targets") or {}).values() for urn in urns}
    )
    detail = (
        f"{got_id}  {core.get('decision')}  "
        f"{len(core.get('predicates', []))} predicate(s), {len(live)} read from DataHub, "
        f"{len(targets)} authorized target(s)"
    )
    return True, detail


async def load_from_datahub(urn: str | None) -> list[InvestigationCard]:
    from incident_copilot import datahub_api

    return [
        card
        for card, document_urn in await datahub_api.stored_cards(limit=200)
        if not urn or document_urn == urn
    ]


def load_from_file(path: str) -> list[InvestigationCard]:
    text = Path(path).read_text(encoding="utf-8")
    card = parse_card(text)
    if card is not None:
        return [card]
    # Also accept a bare JSON payload, so a card copied out of the UI works too.
    return [InvestigationCard.model_validate(json.loads(text))]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--urn", help="verify a single stored card by its document URN")
    parser.add_argument("--file", help="verify a card from a local file instead of DataHub")
    args = parser.parse_args()

    try:
        cards = (
            load_from_file(args.file)
            if args.file
            else await load_from_datahub(args.urn)
        )
    except Exception as exc:  # noqa: BLE001 -- report, don't traceback at a reviewer
        print(f"Could not load Investigation Cards: {exc}")
        return 2

    if not cards:
        print("No Investigation Cards found. Run an investigation first.")
        return 1

    print(f"\n{BAR}\n  Recomputing {len(cards)} authorization(s) from their recorded grounds\n{BAR}\n")
    failures = 0
    for card in sorted(cards, key=lambda c: c.timestamp, reverse=True):
        ok, detail = verify(card)
        failures += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {card.incident_id}  {detail}")
        if not ok:
            print(f"        decision on card: {card.authorization_decision}")

    checked = [c for c in cards if c.authorization_core]
    print(
        f"\n{BAR}\n  {len(checked) - failures}/{len(checked)} recomputed authorizations match "
        f"the id stored on their card.\n{BAR}\n"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
