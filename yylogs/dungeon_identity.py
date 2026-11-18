"""Human-readable encounter labels derived from the bundled PVE catalog."""
from functools import lru_cache
import re

from yylogs.runtime_scope import dungeon_catalog

_ENCOUNTER_NAME = re.compile(r"^(?P<boss>.+?)-(?P<level>\d+(?:级|季))-(?P<dungeon>.+)$")
_MODE_SUFFIX = re.compile(r"^(?:\d+人|竞速|挑战|自选|常规竞速|挑战竞速)(?:-|$)")


def _parse_catalog_name(name):
    match = _ENCOUNTER_NAME.fullmatch(name)
    if not match:
        return None
    values = match.groupdict()
    dungeon = values["dungeon"]
    if _MODE_SUFFIX.match(dungeon):
        return None
    boss = re.sub(r"^(?:多人|单人)-?", "", values["boss"]).strip("-")
    if not boss:
        return None
    return {"dungeon_name": dungeon, "boss_name": boss, "level_name": values["level"]}


@lru_cache(maxsize=1)
def _catalog_identities():
    rows = dungeon_catalog()
    parsed = {sid: _parse_catalog_name(row.get("name", ""))
              for sid, row in rows.items()}
    boss_orders = {}
    bosses_by_dungeon = {}
    # The catalog preserves source-table order; keep the first appearance of
    # each named boss within a dungeon as its configured encounter position.
    for sid, value in parsed.items():
        if value is None:
            continue
        bosses = bosses_by_dungeon.setdefault(value["dungeon_name"], [])
        if value["boss_name"] not in bosses:
            bosses.append(value["boss_name"])
    for dungeon, bosses in bosses_by_dungeon.items():
        if len(bosses) > 1:
            boss_orders[dungeon] = {name: index for index, name in enumerate(bosses, 1)}
    return rows, parsed, boss_orders


def resolve_dungeon_identity(dungeon_sid, boss_display_name=None):
    if dungeon_sid == -1:
        return {"dungeon_sid": -1, "dungeon_name": "木桩", "catalog_name": "木桩",
                "boss_name": None, "boss_order": None, "level_name": None,
                "boss_order_source": None, "label": "木桩"}

    rows, parsed_rows, boss_orders = _catalog_identities()
    key = str(dungeon_sid) if type(dungeon_sid) is int else ""
    row = rows.get(key)
    catalog_name = row.get("name") if row else None
    parsed = parsed_rows.get(key)
    if parsed:
        boss_name = parsed["boss_name"]
        boss_order = boss_orders.get(parsed["dungeon_name"], {}).get(boss_name)
        label = f"{parsed['dungeon_name']} · {parsed['level_name']}"
        label += f" · Boss {boss_order}" if boss_order is not None else ""
        label += f" · {boss_name}"
        return {"dungeon_sid": dungeon_sid, "dungeon_name": parsed["dungeon_name"],
                "catalog_name": catalog_name, "boss_name": boss_name,
                "boss_order": boss_order, "level_name": parsed["level_name"],
                "boss_order_source": "catalog_order" if boss_order is not None else None,
                "label": label}

    configured_bosses = row.get("boss_names", []) if row else []
    if not boss_display_name and len(configured_bosses) == 1:
        boss_name = configured_bosses[0].get("name")
        if isinstance(boss_name, str) and boss_name:
            label = f"{catalog_name} · {boss_name}" if catalog_name else boss_name
            return {"dungeon_sid": dungeon_sid, "dungeon_name": catalog_name,
                    "catalog_name": catalog_name, "boss_name": boss_name,
                    "boss_order": None, "level_name": None,
                    "boss_order_source": None, "label": label}

    boss_name = boss_display_name if isinstance(boss_display_name, str) and boss_display_name else None
    if catalog_name and boss_name and catalog_name != boss_name:
        label = f"{catalog_name} · {boss_name}"
    else:
        label = catalog_name or boss_name or (f"副本 SID {dungeon_sid}（未收录）" if type(dungeon_sid) is int
                                              else "副本名称未解析")
    return {"dungeon_sid": dungeon_sid, "dungeon_name": None,
            "catalog_name": catalog_name, "boss_name": boss_name,
            "boss_order": None, "level_name": None,
            "boss_order_source": None, "label": label}


def safe_encounter_filename(label):
    value = "".join("_" if char in '<>:"/\\|?*' or ord(char) < 32 else char for char in str(label))
    return value.strip(" .")[:96] or "副本记录"
