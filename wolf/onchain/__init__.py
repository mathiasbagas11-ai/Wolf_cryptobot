"""On-chain / whale / institutional-flow collectors.

Every module here is a **collector**: it fetches from a public API on its own
schedule and writes a timestamped snapshot into the :class:`~wolf.state.StateStore`.
It never formats a message and never gates a signal.

    COLLECTOR (scheduled job) → StateStore ─┬→ REPORTER        → Telegram text
                                            └→ ContextProvider → signal gate

The split matters because the previous bot's reports fetched *inside* ``build()``:
the raw numbers were thrown away with the returned string, so a second consumer
had to fetch again and could reach a different conclusion from the same source.
Here one fetch feeds both consumers, and the two consumers cannot disagree.

Collectors own no module-level state: caches and cooldowns live on the instance
(so tests get a clean object and two collectors never share a cache), and
anything that must outlive the process lives in the StateStore.
"""

from wolf.onchain.valuation import (
    ValuationCollector,
    assess_valuation,
    build_valuation_brief,
    compute_valuation_metrics,
)

__all__ = [
    "ValuationCollector",
    "compute_valuation_metrics",
    "assess_valuation",
    "build_valuation_brief",
]
