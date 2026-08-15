"""Detector registry.

Each detector lives in its own module. ``ALL_DETECTORS`` is the default set the
screener runs; add a new detector by writing a module and appending its factory
here — no other file needs to change.
"""

from typing import Optional

from wolf.config import LadderSettings
from wolf.detectors.base import (
    DEFAULT_LADDER,
    Detector,
    SignalCandidate,
    build_targets,
    ladder_from_risk,
)
from wolf.detectors.momentum import MomentumBreakoutDetector
from wolf.detectors.prepump import PrePumpDetector
from wolf.detectors.predump import PreDumpDetector
from wolf.detectors.scalp import ScalpDetector
from wolf.detectors.swing import SwingDetector
from wolf.detectors.trap import LiquidityTrapDetector


def default_detectors(ladder: Optional[LadderSettings] = None) -> list[Detector]:
    """Return a fresh list of the default detector instances.

    ``ladder`` is threaded into every detector so the reward:risk policy is set
    in one place and cannot drift between strategies.
    """
    ladder = ladder or DEFAULT_LADDER
    return [
        MomentumBreakoutDetector(ladder=ladder),
        PrePumpDetector(ladder=ladder),
        PreDumpDetector(ladder=ladder),
        ScalpDetector(ladder=ladder),
        SwingDetector(ladder=ladder),
        LiquidityTrapDetector(ladder=ladder),
    ]


__all__ = [
    "Detector",
    "SignalCandidate",
    "build_targets",
    "ladder_from_risk",
    "MomentumBreakoutDetector",
    "PrePumpDetector",
    "PreDumpDetector",
    "ScalpDetector",
    "SwingDetector",
    "LiquidityTrapDetector",
    "default_detectors",
]
