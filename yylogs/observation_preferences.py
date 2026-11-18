"""Display preferences, separate from immutable capture evidence."""
from pathlib import Path

from yylogs.local_files import atomic_json, read_json


def preference_path(record):
    explicit = record.get('_ui_preferences_path')
    if explicit:
        return Path(explicit)
    folder = Path(record['record_folder']) if record.get('record_folder') else None
    if folder and folder.parent.name == 'battles' and folder.parent.parent.name == 'yylogs_imported':
        return folder.parent.parent.parent / 'ui_preferences.json'
    return None


def preferences(record):
    path = preference_path(record)
    try:
        value = read_json(path) if path else record.get('_observation_preferences', {})
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save_preference(record, key, value):
    path = preference_path(record)
    data = read_json(path) if path else record.get('_observation_preferences', {})
    if data is not None and not isinstance(data, dict):
        raise ValueError('界面偏好格式无效')
    data = {**(data or {}), key: value}
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(path, data)
    record['_observation_preferences'] = data


def own_actor(record):
    members = {m['id'] for m in record.get('members', [])}
    confirmed = (record.get('buff_timeline') or {}).get('own_entity_id')
    if confirmed in members:
        return confirmed
    local = [m['id'] for m in record.get('members', []) if m.get('local')]
    if len(local) == 1:
        return local[0]
    chosen = record.get('_preferred_own_actor_id') or preferences(record).get('preferred_own_actor_id')
    return chosen if chosen in members else None


def observation_members(record):
    own = own_actor(record)
    return sorted(record.get('members', []),
                  key=lambda m: (m['id'] != own, -(m.get('damage') or 0)))


def remember_own(record, actor):
    if actor not in {m['id'] for m in record.get('members', [])}:
        raise ValueError('请选择本场角色')
    save_preference(record, 'preferred_own_actor_id', actor)
    record['_preferred_own_actor_id'] = actor
