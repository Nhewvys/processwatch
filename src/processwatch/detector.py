"""Decisões puras; nunca pressupõe identidade pelo nome isolado."""
from collections import defaultdict
from .models import Movement


def classify(incoming: list[Movement], current: list[dict], baseline_scopes: set[str]):
    old_by_anchor, new_by_anchor = defaultdict(list), defaultdict(list)
    for row in current:
        old_by_anchor[row["anchor"]].append(row)
    for movement in incoming:
        new_by_anchor[movement.anchor].append(movement)
    decisions = []
    known = {row["fingerprint"] for row in current}
    for movement in incoming:
        if movement.fingerprint in known:
            continue
        old = old_by_anchor[movement.anchor]
        if movement.scope not in baseline_scopes:
            kind, replaces = "historical", None
        elif not old:
            kind, replaces = "new", None
        elif len(old) == len(new_by_anchor[movement.anchor]) == 1:
            kind, replaces = "corrected", old[0]["fingerprint"]
        else:
            kind, replaces = "review", None
        decisions.append((movement, kind, replaces))
    return decisions
