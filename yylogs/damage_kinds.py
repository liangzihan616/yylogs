"""Separate healing from damage by exact calculation point, never skill name."""
from functools import lru_cache
import json
from pathlib import Path

CATALOG = Path(__file__).parent / "resources/calcpoint_kinds.json"


@lru_cache(maxsize=1)
def calcpoint_kinds():
    data = json.loads(CATALOG.read_text("utf8"))
    if (data.get("schema") != "yylogs.calcpoint-kinds/1"
            or data.get("variant") != "ordinary" or data.get("table") != "calcpoint"):
        raise ValueError("Unsupported calculation-point kind catalog")
    kinds = data["kinds"]
    if not isinstance(kinds, dict) or any(
            not key.isdecimal() or type(kind) is not int or kind not in (0, 1, 2, 3)
            for key, kind in kinds.items()):
        raise ValueError("Invalid calculation-point kind catalog")
    return kinds


def event_kind(row):
    point = row.get("calcpoint_id")
    configured = calcpoint_kinds().get(str(point)) if type(point) is int else None
    explicit = row.get("damage_type")
    if type(explicit) is not int:
        explicit = None
    if explicit in (1, 2) and configured in (1, 2) and explicit != configured:
        return "conflict", "damage_type_calcpoint_conflict"
    if explicit == 2:
        return "healing", "event.damage_type"
    if explicit == 1:
        return "damage", "event.damage_type"
    if configured == 2:
        return "healing", "ordinary_calcpoint.jm_calc_type"
    if configured == 1:
        return "damage", "ordinary_calcpoint.jm_calc_type"
    return "unknown", "unresolved_candidate_semantics"
