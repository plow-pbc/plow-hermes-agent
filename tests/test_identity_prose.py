"""The base persona must not claim a place it does not run.

The same image boots a Plow cloud VM and a developer's own machine, and a
hosted tenant was told "you live on your owner's own private machine" — a
false claim about where their data is, made at the moment they decide whether
to trust it."""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOUL = (ROOT / "image" / "seed" / "SOUL.md").read_text()
DOCKERFILE = (ROOT / "Dockerfile").read_text()


def test_the_persona_does_not_claim_to_run_on_the_owners_machine():
    for false_claim in ("own private machine", "one agent on one machine"):
        assert false_claim not in SOUL, f"SOUL.md still says {false_claim!r}"
    assert "Plow Latch on their Mac" in SOUL


def test_the_seed_skills_are_staged_from_the_plugin_archive_not_tracked():
    """growth/plow-invite and productivity/google-workspace are the plugin's
    own; a tracked copy here is a second place for them to drift out of sync
    with the plugin they describe. The Dockerfile stages both from the same
    tarball the plugin is built from, so a pin bump moves both together."""
    for tracked in (
        ROOT / "image" / "seed" / "skills" / "growth" / "plow-invite",
        ROOT / "image" / "seed" / "skills" / "productivity" / "google-workspace",
    ):
        assert not tracked.exists(), f"{tracked} is tracked; it should be staged from the plugin tarball instead"
    for staged in (
        "$top/seed-skills/growth/plow-invite",
        "$top/seed-skills/productivity/google-workspace",
    ):
        assert staged in DOCKERFILE, f"Dockerfile does not stage {staged} from the plugin archive"
