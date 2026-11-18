"""Explicit statistic ownership, separate from raw event grouping."""
from functools import lru_cache
import json
from pathlib import Path

CATALOG = Path(__file__).parent / 'resources/final_count_ownership.json'


@lru_cache(maxsize=1)
def ownership():
    data = json.loads(CATALOG.read_text('utf-8'))
    if data.get('schema') != 'yylogs.final-count-ownership/1' or data.get('variant') != 'ordinary':
        raise ValueError('Unsupported final-count ownership catalog')
    result = {}
    for table, field in (('skills', 'count_belong_skill'), ('calcpoint', 'jm_count_belong_skill')):
        mapping = data['mappings'][table]
        if mapping.get('field') != field:
            raise ValueError('Unexpected count ownership field')
        entries = mapping['entries']
        if any(not key.isdecimal() or int(key) <= 0 or type(row['skill_id']) is not int
               or row['skill_id'] <= 0 for key, row in entries.items()):
            raise ValueError('Invalid count ownership entry')
        result[table] = {int(key): row['skill_id'] for key, row in entries.items()}
    for skill in result['skills']:
        _root(skill, result['skills'])
    return result


def _root(skill, parents):
    seen = set()
    while skill in parents and parents[skill] != skill:
        if skill in seen:
            raise ValueError('Cyclic count ownership')
        seen.add(skill)
        skill = parents[skill]
    return skill


def skill_root(skill):
    return _root(skill, ownership()['skills'])


def point_owner(point):
    return ownership()['calcpoint'].get(point) if type(point) is int else None


def final_count_group(identity, by_id, groups):
    """Assign once, refusing ambiguous owners across final display rows."""
    if identity[0] == 'name':
        return groups.get(identity[1])
    if identity[0] != 'skill':
        return None
    skill = identity[1]
    if skill in by_id:
        return by_id[skill]
    root = skill_root(skill)
    matches = {id(group): group for final_id, group in by_id.items()
               if skill_root(final_id) == root}
    return next(iter(matches.values())) if len(matches) == 1 else None
