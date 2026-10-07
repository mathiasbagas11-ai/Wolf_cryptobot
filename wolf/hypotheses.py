"""The register of questions already settled, and what settled them.

Every entry here is a change that was proposed, measured and closed out. The
list exists because that work is worth almost nothing while it lives only in a
conversation: a rejected idea that nobody can point at gets proposed again a
week later, re-measured on a sample that has barely moved, and rejected again
at the cost of the only scarce resource this project has — trades.

Three properties, each of them the reason a different obvious alternative was
not used:

* **It is a file in the repository, not state.** State is wiped when the
  container restarts, which is exactly the failure this guards against. A
  registry that can evaporate is a registry that will.
* **It is edited by commit, never at runtime.** A verdict that can be
  overwritten by a process nobody is watching is a verdict with no provenance;
  going through a diff means every status change carries an author, a date and
  a reason.
* **Every entry names its evidence.** A status with no number behind it is a
  rumour, and rumours get re-litigated. ``REJECTED`` is a claim about a
  measurement, so the measurement travels with it.

``OPEN`` is deliberately part of the vocabulary. The queue of things not yet
tested is as easy to lose as the list of things already tested, and an entry
that says what is blocking it is what stops the question being rediscovered
from scratch.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

#: Shipped alongside the module, so it travels with the code that reads it.
REGISTRY_PATH = Path(__file__).with_name("hypotheses.json")

#: What a question can be. ``INCONCLUSIVE`` is distinct from ``OPEN``: the
#: first was measured and the measurement did not separate anything, the
#: second has not been measured yet. Collapsing them would lose the difference
#: between "we looked" and "we have not", which is the difference that decides
#: whether it is worth spending the sample again.
STATUSES = ("OPEN", "REJECTED", "ADOPTED", "INCONCLUSIVE")

_REQUIRED = ("id", "question", "status", "evidence")


class RegistryError(ValueError):
    """The registry file is present but does not say what it claims to."""


def load(path: Optional[Path] = None) -> list[dict]:
    """Read and validate the registry.

    Validation is strict and raises, rather than skipping bad rows. A registry
    that silently drops a malformed entry renders as a shorter list with no
    indication anything is missing, which is worse than not having one — the
    reader concludes the question was never asked.
    """
    src = path or REGISTRY_PATH
    try:
        data = json.loads(src.read_text())
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as exc:
        raise RegistryError(f"{src.name} is not valid JSON: {exc}") from exc

    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise RegistryError(f"{src.name} has no 'entries' list")

    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise RegistryError(f"{src.name}: entry is not an object")
        missing = [k for k in _REQUIRED if not str(entry.get(k, "")).strip()]
        if missing:
            raise RegistryError(
                f"{src.name}: entry {entry.get('id', '?')!r} is missing {missing}"
            )
        if entry["status"] not in STATUSES:
            raise RegistryError(
                f"{src.name}: entry {entry['id']!r} has status "
                f"{entry['status']!r}, expected one of {list(STATUSES)}"
            )
        if entry["id"] in seen:
            raise RegistryError(f"{src.name}: duplicate id {entry['id']!r}")
        seen.add(entry["id"])
    return entries


def render(entries: list[dict]) -> str:
    """Render the registry as a compact fixed-shape text block.

    Ordered by status rather than by date, because the question a reader
    arrives with is "has this been tried", not "what happened when". Settled
    entries come first for the same reason: the list earns its keep by stopping
    a repeat, and the repeat is always of something already closed.
    """
    if not entries:
        return "hypotheses: registry is empty"

    order = {"REJECTED": 0, "INCONCLUSIVE": 1, "ADOPTED": 2, "OPEN": 3}
    ranked = sorted(entries, key=lambda e: (order.get(e["status"], 9), e["id"]))

    lines = [f"WOLF-HYPOTHESES | {len(ranked)} entries"]
    for e in ranked:
        settled = e.get("settled_on") or ""
        lines.append("")
        lines.append(f"[{e['status']}] {e['id']}" + (f"  ({settled})" if settled else ""))
        lines.append(f"  Q: {e['question']}")
        lines.append(f"  E: {e['evidence']}")
        if e.get("note"):
            lines.append(f"  N: {e['note']}")
    return "\n".join(lines)
