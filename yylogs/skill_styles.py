"""Infer styles from numeric observed martial membership and user-defined pairs."""
from functools import lru_cache
import json
from pathlib import Path
from decimal import Decimal
import math

@lru_cache(maxsize=1)
def catalog():
    return json.loads((Path(__file__).parent/'resources/martial_styles.json').read_text(encoding='utf-8'))

def skill_id(value):
    if type(value) is int and 0 < value <= 2**53-1:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 16:
        number = int(value)
        return number if 0 < number <= 2**53-1 else None
    return None

def infer_style_ids(ids, *, truncated=False):
    data = catalog()
    arts = set()
    for raw in ids:
        key = skill_id(raw)
        entry = data['skills'].get(str(key))
        if entry:
            arts.add(entry['martial_id'])
    styles = [name for name, pair in data['rules'].items() if set(pair) <= arts]
    # Prefer one complete user-defined pair even when another martial art is observed.
    if truncated:
        name = '待判定'
    elif len(styles) == 1:
        name = styles[0]
    elif len(arts) >= 2:
        name = '混搭'
    else:
        name = '待判定'
    return {'name': name, 'styles': styles, 'martial_ids': sorted(arts),
            'evidence': [data['martial_names'][str(k)] for k in sorted(arts)],
            'truncated': bool(truncated), 'basis': 'observed_martial_ids'}

def infer_style(skill_rows):
    ids = set()
    for row in skill_rows or ():
        amount = row.get('damage', row.get('healing', 0))
        if isinstance(amount, bool) or not isinstance(amount, (int, float, Decimal)) or not math.isfinite(amount) or amount <= 0:
            continue
        raw = row.get('raw_skill_ids', [])
        if isinstance(raw, list):
            ids.update(key for value in raw if (key := skill_id(value)) is not None)
        key = skill_id(row.get('skill_id'))
        if key is not None:
            ids.add(key)
    return infer_style_ids(ids)

def style_tooltip(observation):
    text = '本场观察到使用的武学：' + ('、'.join(observation.get('evidence', [])) or '暂无明确归属')
    text += '\n优先匹配完整武学组合；两项及以上无法配对时标为混搭，未使用的武学无法确认。'
    if observation.get('truncated'):
        text += '\n观察记录已达上限，暂不确认流派。'
    return text

def annotate_members(members, details, usage=None):
    for member in members:
        observation = (infer_style_ids(usage.get(member.get('id'), ())) if usage is not None
                       else infer_style(details.get(member.get('id'), {}).get('rows', [])))
        member['style_observation'] = observation
        member['style'] = observation['name']

def snapshot_styles(snapshot, session, clock):
    """Invalid optional evidence cannot invalidate the damage meter."""
    summary = snapshot.get('partySkills')
    if not isinstance(summary, dict) or (summary.get('sessionId'), summary.get('startObservedAt'), summary.get('spaceHash')) != (session, clock.get('startObservedAt'), clock.get('spaceHash')):
        return {}
    rows = summary.get('actors')
    if not isinstance(rows, list) or len(rows) > 128:
        return {}
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            return {}
        actor, ids = row.get('idHash'), row.get('skillIds')
        if not isinstance(actor, str) or not 0 < len(actor) <= 160 or actor in result or not isinstance(ids, list) or len(ids) > 96 or any(type(key) is not int or skill_id(key) is None for key in ids):
            return {}
        result[actor] = infer_style_ids(ids, truncated=bool(summary.get('truncated') or row.get('truncated')))
    return result
