"""Tests for the hypothesis registry — the list that outlives a conversation."""

from __future__ import annotations

import json

import pytest

from wolf.hypotheses import REGISTRY_PATH, RegistryError, STATUSES, load, render


def _write(tmp_path, payload) -> object:
    path = tmp_path / "hypotheses.json"
    path.write_text(json.dumps(payload))
    return path


def test_the_shipped_registry_loads_and_is_not_empty():
    """The file in the repo is the artefact; a broken one fails here, not live."""
    entries = load()
    assert entries
    assert all(e["status"] in STATUSES for e in entries)


def test_every_entry_names_the_evidence_that_settled_it():
    """A status with no measurement behind it is a rumour, and rumours recur."""
    for entry in load():
        assert entry["evidence"].strip()
        assert entry["question"].strip().endswith("?")


def test_the_rejected_findings_from_the_measurement_run_are_all_present():
    """The specific list this registry was built to stop losing."""
    by_id = {e["id"]: e for e in load()}
    assert by_id["drop-breakeven-stop"]["status"] == "REJECTED"
    assert by_id["cost-model-refinement"]["status"] == "REJECTED"
    assert by_id["llm-in-signal-path"]["status"] == "REJECTED"
    assert by_id["stop-advance-ladder"]["status"] == "INCONCLUSIVE"


def test_open_is_distinct_from_inconclusive():
    """"We looked and could not separate it" is not "we have not looked".

    The difference decides whether spending the sample again is worth it, and
    collapsing the two would lose exactly that.
    """
    statuses = {e["status"] for e in load()}
    assert "OPEN" in statuses and "INCONCLUSIVE" in statuses


def test_a_missing_registry_is_empty_not_an_error(tmp_path):
    assert load(tmp_path / "absent.json") == []


def test_malformed_json_is_named_rather_than_swallowed(tmp_path):
    path = tmp_path / "hypotheses.json"
    path.write_text("{not json")
    with pytest.raises(RegistryError):
        load(path)


def test_an_entry_missing_its_evidence_is_rejected_at_load(tmp_path):
    """Skipping the bad row would render a shorter list that looks complete."""
    path = _write(tmp_path, {"entries": [
        {"id": "x", "question": "Does it?", "status": "REJECTED", "evidence": ""}
    ]})
    with pytest.raises(RegistryError, match="evidence"):
        load(path)


def test_an_invented_status_is_rejected_at_load(tmp_path):
    path = _write(tmp_path, {"entries": [
        {"id": "x", "question": "Does it?", "status": "MAYBE", "evidence": "n=10"}
    ]})
    with pytest.raises(RegistryError, match="MAYBE"):
        load(path)


def test_a_duplicate_id_is_rejected_at_load(tmp_path):
    """Two entries under one id means one of them can never be pointed at."""
    entry = {"id": "x", "question": "Does it?", "status": "OPEN", "evidence": "n=10"}
    path = _write(tmp_path, {"entries": [entry, dict(entry)]})
    with pytest.raises(RegistryError, match="duplicate"):
        load(path)


def test_settled_entries_render_before_open_ones(tmp_path):
    """The list earns its keep by stopping a repeat, and repeats are of closed work."""
    path = _write(tmp_path, {"entries": [
        {"id": "later", "question": "Q?", "status": "OPEN", "evidence": "e"},
        {"id": "earlier", "question": "Q?", "status": "REJECTED", "evidence": "e"},
    ]})
    out = render(load(path))
    assert out.index("earlier") < out.index("later")


def test_an_empty_registry_says_so_rather_than_rendering_nothing():
    assert "empty" in render([])


def test_the_registry_file_sits_beside_the_module_that_reads_it():
    """It has to travel with the code, not with a container that gets wiped."""
    assert REGISTRY_PATH.name == "hypotheses.json"
    assert REGISTRY_PATH.parent.name == "wolf"
    assert REGISTRY_PATH.exists()


# ── reachable from where the idea gets proposed again ───────────────────────


def _router(store, fake_client):
    from types import SimpleNamespace

    from wolf.config import Settings, TrackerSettings
    from wolf.notify.commands import CommandRouter
    from wolf.tracker import Tracker

    return CommandRouter(SimpleNamespace(
        analyze=None,
        tracker=Tracker(store, fake_client, TrackerSettings()),
        settings=Settings(),
        screener=SimpleNamespace(_validator=None),
        account=None,
        learning=None,
    ))


def test_the_registry_is_reachable_from_telegram(store, fake_client):
    """A registry pays off when somebody is about to re-run a test.

    At that moment nobody opens the repository, so the list has to be where the
    proposal is made.
    """
    reply = _router(store, fake_client).handle("/tested")
    assert "WOLF-HYPOTHESES" in reply
    assert "drop-breakeven-stop" in reply


def test_hypotheses_is_an_alias_for_tested(store, fake_client):
    router = _router(store, fake_client)
    assert router.handle("/hypotheses") == router.handle("/tested")


def test_the_registry_is_listed_in_help(store, fake_client):
    assert "/tested" in _router(store, fake_client).handle("/help")


def test_an_unreadable_registry_never_renders_as_nothing_tried(
    store, fake_client, monkeypatch
):
    """Empty output would read as "no test has ever been run", the one wrong answer."""
    import wolf.hypotheses as hyp

    def _boom(path=None):
        raise hyp.RegistryError("hypotheses.json has no 'entries' list")

    monkeypatch.setattr(hyp, "load", _boom)
    reply = _router(store, fake_client).handle("/tested")
    assert "unreadable" in reply
    assert "WOLF-HYPOTHESES" not in reply
