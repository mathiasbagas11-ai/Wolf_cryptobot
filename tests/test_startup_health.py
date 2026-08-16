"""Tests for the two failures that hide in plain sight at startup.

Both were live on a 24h sample: every signal came back ABSTAIN while the
startup card read "AI debate: MONITOR", and the whole outcome history sat on
an ephemeral filesystem while nothing said so.
"""

from __future__ import annotations

import os
from unittest import mock

from wolf.config import Settings, _resolve_state_dir, state_is_persistent
from wolf.main import _ai_mode_label, _state_label
from wolf.state import StateStore


# ── state location ───────────────────────────────────────────────────────
def test_explicit_state_dir_always_wins():
    with mock.patch.dict(os.environ, {"STATE_DIR": "/custom/path",
                                      "RAILWAY_VOLUME_MOUNT_PATH": "/data"}):
        assert _resolve_state_dir() == "/custom/path"


def test_state_dir_adopts_a_mounted_volume():
    """Attaching a volume should be enough — no second setting to remember."""
    with mock.patch.dict(os.environ, {"RAILWAY_VOLUME_MOUNT_PATH": "/data"}, clear=False):
        os.environ.pop("STATE_DIR", None)
        assert _resolve_state_dir() == "/data/state_data"


def test_state_dir_falls_back_when_no_volume_is_attached():
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("STATE_DIR", None)
        os.environ.pop("RAILWAY_VOLUME_MOUNT_PATH", None)
        assert _resolve_state_dir() == "state_data"


def test_relative_path_is_never_persistent():
    assert state_is_persistent("state_data") is False


def test_absolute_path_outside_the_volume_is_not_persistent():
    """The case a bare "is it absolute?" check waves through.

    ``/app/state_data`` is absolute and still inside the container.
    """
    with mock.patch.dict(os.environ, {"RAILWAY_VOLUME_MOUNT_PATH": "/data"}):
        assert state_is_persistent("/app/state_data") is False
        assert state_is_persistent("/data/state_data") is True


def test_absolute_path_is_trusted_without_a_platform_hint():
    """A real bind mount off-platform must not be called ephemeral."""
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("RAILWAY_VOLUME_MOUNT_PATH", None)
        assert state_is_persistent("/mnt/disk/state") is True


# ── what the startup card says ───────────────────────────────────────────
class _App:
    def __init__(self, state_dir: str) -> None:
        self.store = StateStore(state_dir)
        self.settings = Settings(state_dir=state_dir)


def test_state_label_warns_when_history_will_be_wiped(tmp_path):
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("RAILWAY_VOLUME_MOUNT_PATH", None)
        label = _state_label(_App("state_data"))
    assert "EPHEMERAL" in label
    assert "volume" in label.lower()


def test_state_label_is_quiet_when_state_is_durable(tmp_path):
    volume = tmp_path / "vol"
    with mock.patch.dict(os.environ, {"RAILWAY_VOLUME_MOUNT_PATH": str(volume)}):
        label = _state_label(_App(str(volume / "state_data")))
    assert "EPHEMERAL" not in label
    assert "(volume)" in label


def test_ai_label_reports_reality_not_intent():
    """Enabled-but-unusable must not read the same as working."""
    assert _ai_mode_label({"enabled": False}) == "OFF"
    assert _ai_mode_label({"enabled": True, "available": True}) == "MONITOR"

    broken = _ai_mode_label(
        {"enabled": True, "available": False, "degraded_roles": ["bear", "arbiter"]}
    )
    assert "UNAVAILABLE" in broken
    assert "bear" in broken and "arbiter" in broken   # names the dead roles


def test_ai_label_names_a_role_even_when_the_list_is_empty():
    broken = _ai_mode_label({"enabled": True, "available": False, "degraded_roles": []})
    assert "UNAVAILABLE" in broken and "arbiter" in broken
