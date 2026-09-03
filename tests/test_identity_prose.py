"""The base persona must not claim a place it does not run.

The same image boots a Plow cloud VM and a developer's own machine, and a
hosted tenant was told "you live on your owner's own private machine" — a
false claim about where their data is, made at the moment they decide whether
to trust it."""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOUL = (ROOT / "image" / "seed" / "SOUL.md").read_text()
SKILL = (ROOT / "image" / "seed" / "skills" / "growth" / "plow-invite" / "SKILL.md").read_text()


def test_the_persona_does_not_claim_to_run_on_the_owners_machine():
    for false_claim in ("own private machine", "one agent on one machine"):
        assert false_claim not in SOUL, f"SOUL.md still says {false_claim!r}"
    assert "Plow Latch on their Mac" in SOUL


def test_the_mirrored_invite_skill_names_the_tool_that_exists():
    assert "plow_offer_invite" in SKILL
    for ghost in ("plow_prepare_invite", "plow_notify_owner_about_invite", "plow_send_invite"):
        assert ghost not in SKILL, f"the mirror still names {ghost}, which the plugin never registered"
