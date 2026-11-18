"""Session identity and installed PVE catalog; never a blanket positive-SID allowance."""
import json
from functools import lru_cache
from pathlib import Path

def entity_id(value):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 128:
        raise ValueError("invalid_runtime_entity_id")
    return value

def same_entity(observed, expected=None):
    value = entity_id(observed)
    if expected is not None and value != entity_id(expected):
        raise ValueError("runtime_entity_changed")
    return value

@lru_cache(maxsize=1)
def dungeon_catalog():
    raw = json.loads((Path(__file__).parent / "resources" / "pve_dungeons.json").read_text(encoding="utf-8"))
    if raw.get("schema") != "yylogs.pve-dungeon-catalog/1" or raw.get("count") != len(raw["entries"]):
        raise ValueError("invalid_dungeon_catalog")
    entries = raw["entries"]
    for key, row in entries.items():
        if str(row.get("dungeon_sid")) != key or type(row.get("dungeon_sid")) is not int or row["dungeon_sid"] <= 0:
            raise ValueError("invalid_dungeon_catalog_row")
    return entries

def is_supported_pve_sid(value):
    return type(value) is int and str(value) in dungeon_catalog()

def require_pve_sid(value):
    if not is_supported_pve_sid(value):
        raise ValueError("outside_configured_pve_dungeon")
    return value

def dungeon_info(value):
    require_pve_sid(value)
    return dict(dungeon_catalog()[str(value)])

SUPPORTED_PVE_SIDS = tuple(sorted(int(key) for key in dungeon_catalog()))
