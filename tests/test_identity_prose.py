"""The base persona must not claim a place it does not run.

The same image boots a Plow cloud VM and a developer's own machine, and a
hosted tenant was told "you live on your owner's own private machine" — a
false claim about where their data is, made at the moment they decide whether
to trust it."""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOUL = (ROOT / "image" / "seed" / "SOUL.md").read_text()
# Prose wraps; the contract is the words. Assert sentences against this so a
# reflow that keeps the rule intact does not read as the rule going missing.
SOUL_FLOW = " ".join(SOUL.split())
DOCKERFILE = (ROOT / "Dockerfile").read_text()


def test_the_persona_does_not_claim_to_run_on_the_owners_machine():
    for false_claim in ("own private machine", "one agent on one machine"):
        assert false_claim not in SOUL, f"SOUL.md still says {false_claim!r}"
    assert "Plow Latch on their Mac" in SOUL


def test_the_persona_separates_its_own_lines_from_the_owners_accounts():
    """Voice follows the account a message leaves from, not the medium: an
    agent has its own number and its own address, and it also reaches the
    owner's mailbox and the owner's Messages. iMessage sits on both sides, so
    a persona that names only the medium cannot tell the model which it is."""
    assert "## Your own lines, and your owner's accounts" in SOUL
    assert "what you send goes out under their name, in their voice" in SOUL_FLOW
    for rule in (
        "signed as yourself",
        "Never send a message through your owner's channels as yourself",
        "The medium does not decide this; the account does.",
    ):
        assert rule in SOUL_FLOW, f"SOUL.md no longer says {rule!r}"


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
    for skills_root in ("/var/lib/hermes/skills/", "/opt/hermes/skills/"):
        assert f"COPY --from=plugin /staged/seed-skills/ {skills_root}" in DOCKERFILE, (
            f"Dockerfile does not copy the staged seed skills into {skills_root}"
        )


def test_the_persona_defers_sharing_to_the_chat_and_keeps_send_discipline():
    """One owner of what may be shared: the chat platform's per-turn rule.
    A persona that restates its own non-owner rule is how a trusted room
    refused its own owner (hermes-plugin-plow#125)."""
    assert "Do not disclose the owner's private data" not in SOUL_FLOW
    assert "You keep no secrets in your replies" not in SOUL_FLOW
    for rule in (
        "Each turn's chat instructions say whether a request carries your owner's authority",
        "Compose the whole message in the one command that sends it",
        "never rephrase, split, or reroute a send to get past the prompt",
    ):
        assert rule in SOUL_FLOW, f"SOUL.md no longer says {rule!r}"
