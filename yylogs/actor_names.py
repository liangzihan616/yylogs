"""Portable current-session name evidence; never resolves IDs from old sessions."""
import json
from pathlib import Path


def valid_name(value):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= 64
            and not any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value))


def actor_rows(value, depth=0):
    if depth > 12:
        return []
    found = []
    if isinstance(value, dict):
        rows = value.get("scene_actor_metadata")
        if isinstance(rows, list):
            found.extend(row for row in rows if isinstance(row, dict))
        for key, child in value.items():
            if key != "scene_actor_metadata" and isinstance(child, (dict, list)):
                found.extend(actor_rows(child, depth + 1))
    elif isinstance(value, list):
        for child in value:
            found.extend(actor_rows(child, depth + 1))
    return found


def saved_actors(folder):
    folder = Path(folder)
    actors = []
    preflight = folder / "damage_preflight.json"
    if preflight.is_file() and preflight.stat().st_size <= 8 * 1024 * 1024:
        actors = actor_rows(json.loads(preflight.read_text("utf-8-sig")))
    journal = folder / "actor_names.jsonl"
    scope_path = folder / "joint_capture_scope.json"
    if not journal.is_file() or not scope_path.is_file():
        return actors
    if journal.stat().st_size > 16 * 1024 * 1024 or scope_path.stat().st_size > 32768:
        return actors
    try:
        scope = json.loads(scope_path.read_text("utf-8-sig"))
        keys = ("session_id", "game_pid", "process_creation_ticks", "scene_id", "dungeon_sid", "own_entity_id")
        if any(key not in scope for key in keys):
            return actors
        observed = []
        with journal.open(encoding="utf-8-sig") as stream:
            for sequence, line in enumerate(stream, 1):
                if sequence > 4096 or len(line) > 32768:
                    return actors
                row = json.loads(line)
                if (row.get("schema") != "yylogs.actor-names/1" or row.get("sequence") != sequence
                        or row.get("game_function_called") is not False
                        or any(type(row.get(key)) is not type(scope[key]) or row.get(key) != scope[key] for key in keys)):
                    return actors
                rows = row.get("scene_actor_metadata")
                if not isinstance(rows, list) or len(rows) > 128 or any(not isinstance(item, dict) for item in rows):
                    return actors
                observed.extend(rows)
        return actors + observed
    except (OSError, ValueError, TypeError, AttributeError):
        return actors


def source_name_map(folder):
    candidates = {}
    for row in saved_actors(folder):
        entity, name, classes = row.get("entity_id"), row.get("character_name"), row.get("class_names", [])
        if (isinstance(entity, str) and entity and len(entity) <= 128 and valid_name(name)
                and isinstance(classes, list)
                and any(item in classes for item in ("PlayerAvatar", "Avatar", "AIAvatar", "Npc"))):
            candidates.setdefault(entity, set()).add(name.strip())
    return {entity: next(iter(names)) for entity, names in candidates.items() if len(names) == 1}
