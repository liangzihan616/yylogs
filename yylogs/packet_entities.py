"""Per-encounter NPC views; selection never rewrites captured evidence."""
from collections import defaultdict
from functools import lru_cache


@lru_cache(maxsize=1)
def _template_names():
    from yylogs.runtime_scope import dungeon_catalog
    names = defaultdict(set)
    for dungeon in dungeon_catalog().values():
        for boss in dungeon.get('boss_names', []):
            if type(boss.get('boss_no')) is int and boss.get('name'):
                names[boss['boss_no']].add(boss['name'])
    return {template: next(iter(values)) for template, values in names.items() if len(values) == 1}


def entity_observations(context, events, lower, upper, start, names):
    from yylogs.packet_records import _identity, number, timestamp, _boss_qi_rows
    metadata, grouped, seeds, health = {}, defaultdict(list), {}, defaultdict(list)

    def identify(row):
        actor = row.get('actorHash')
        if _identity(actor) and row.get('entityClass') == 'Npc':
            previous = metadata.get(actor, {})
            template = row.get('npcTemplateNo')
            metadata[actor] = {'id': actor, 'template_no': template
                               if type(template) is int and template > 0 else previous.get('template_no'),
                               'name': names.get(actor) or previous.get('name')}

    for row in context.get('npcs') or []:
        identify(row)
    for row in events:
        when = timestamp(row['observedAt'])
        if when >= upper:
            break
        kind = row.get('kind')
        if kind == 'encounter-identity' and when >= lower:
            for npc in row.get('npcs') or []:
                identify(npc)
        actor = row.get('actorHash') if kind == 'npc-health' else row.get('targetActorHash')
        if kind not in ('npc-health', 'target-qi-state') or not _identity(actor):
            continue
        grouped[actor].append(row)
        if kind != 'npc-health':
            continue
        if when >= lower or actor in metadata:
            identify({**row, 'entityClass': 'Npc'})
        hp, maximum = row.get('currentHp'), row.get('maxHp')
        valid = (row.get('available') is True and number(hp) and number(maximum)
                 and maximum > 0 and hp <= maximum)
        sample = {'timestamp': row['observedAt'], 'hp': hp if valid else None,
                  'hp_max': maximum if valid else None, 'gap': not valid, 'state': True}
        if when < lower:
            seeds[actor] = sample
        else:
            health[actor].append(sample)
    output = []
    for actor, meta in metadata.items():
        hp = health[actor]
        if actor in seeds and not seeds[actor]['gap']:
            hp.insert(0, {**seeds[actor], 'timestamp': start})
        output.append({**meta, 'hp': hp,
                       'qi': _boss_qi_rows(grouped[actor], {actor}, lower, upper)})
    return output


def entity_options(snapshot):
    from yylogs.runtime_scope import dungeon_catalog
    configured = dungeon_catalog().get(str(snapshot.get('dungeon_sid')), {}).get('boss_names', [])
    expected = [boss for boss in configured if boss.get('boss_id') == snapshot.get('npc_no')] or configured
    boss_names = {boss['name'] for boss in expected if boss.get('name')}
    boss_templates = {boss['boss_no'] for boss in expected if type(boss.get('boss_no')) is int}
    options = []
    for entity in snapshot.get('entities', []):
        maxima = [row['hp_max'] for row in entity['hp']
                  if not row.get('gap') and row.get('hp_max') is not None]
        name = entity.get('name') or _template_names().get(entity.get('template_no'))
        options.append({'id': entity['id'], 'name': name,
                        'is_boss': entity.get('template_no') in boss_templates or bool(name and name in boss_names),
                        'template_no': entity.get('template_no'), 'max_hp': max(maxima, default=0),
                        'hp_count': sum(not row.get('gap') for row in entity['hp']),
                        'qi_count': sum(not row.get('gap') for row in entity['qi'])})
    return sorted(options, key=lambda row: (not row['is_boss'], -row['max_hp'], -row['hp_count'], -row['qi_count'], row['id']))


def select_entity(snapshot, actor=None):
    options = entity_options(snapshot)
    if not options:
        return snapshot
    if actor is None:
        actor = options[0]['id']
        if not options[0]['max_hp'] and not options[0]['is_boss']:
            actor = next((row['id'] for row in options if row['id'] in snapshot['boss_ids']), actor)
    selected = next((row for row in snapshot['entities'] if row['id'] == actor), None)
    if selected is None:
        raise ValueError('请选择本场采集到的实体')
    return {**snapshot, 'boss_ids': [actor], 'hp': selected['hp'], 'qi': selected['qi']}


def preferred_entity(record):
    from yylogs.observation_preferences import preferences
    choices = preferences(record).get('boss_entity_by_record', {})
    return choices.get(record.get('source_sha256')) if isinstance(choices, dict) else None


def remember_entity(record, actor):
    from yylogs.observation_preferences import preferences, save_preference
    if actor is not None and actor not in {row['id'] for row in record.get('boss_entity_options', [])}:
        raise ValueError('请选择本场采集到的实体')
    previous = preferences(record).get('boss_entity_by_record', {})
    choices = dict(previous) if isinstance(previous, dict) else {}
    choices[record['source_sha256']] = actor
    save_preference(record, 'boss_entity_by_record', choices)
