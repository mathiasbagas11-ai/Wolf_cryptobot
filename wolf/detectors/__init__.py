"""Detector registry.

Each detector lives in its own module. ``ALL_DETECTORS`` is the default set the
screener runs; add a new detector by writing a module and appending its factory
here — no other file needs to change.
"""

from wolf.detectors.base import Detector, SignalCandidate, build_targets
from wolf.detectors.momentum import MomentumBreakoutDetector
from wolf.detectors.prepump import PrePumpDetector
from wolf.detectors.predump import PreDumpDetector
from wolf.detectors.scalp import ScalpDetector
from wolf.detectors.swing import SwingDetector


def default_detectors() -> list[Detector]:
    """Return a fresh list of the default detector instances."""
    return [
        MomentumBreakoutDetector(),
        PrePumpDetector(),
        PreDumpDetector(),
        ScalpDetector(),
        SwingDetector(),
    ]


__all__ = [
    "Detector",
    "SignalCandidate",
    "build_targets",
    "MomentumBreakoutDetector",
    "PrePumpDetector",
    "PreDumpDetector",
    "ScalpDetector",
    "SwingDetector",
    "default_detectors",
]
