"""Display attribution for verified Buff calculation points; never rewrite IDs."""
from functools import lru_cache

CONFIG_EVIDENCE = 'exports/config_tables_20261026/tables/buff.jsonl'
CONFIG_SHA256 = '456096aaf9d3cedd0547a3269aee15b8426d12cec30b8cde69464e10ff4a5469'
# Exact ordinary-config references. Names normalize hidden/level suffixes only.
# The 爆血 display spelling also matches the user's same-encounter settlement.
EFFECTS = {
    2038001: ('流血', (109619,)),
    2038002: ('流血', (109619,)),
    2038003: ('流血', (109619,)),
    2038004: ('流血', (109619,)),
    2038005: ('流血', (109619,)),
    2038006: ('爆血', (109620, 109624, 109650)),
    2037601: ('易水歌', (10208121, 10208123)),
    5700200105: ('燃烧', (400108, 400101005)),
    5700200201: ('炸伤', (400100,)),
    # 神龙吐火(2300032) 的周期 DOT 结算在游戏里挂在 爆燃/暗灼 两个 Boss Buff 上，
    # 伤害事件仍带触发技能 2300032。按计算点把它们拆成独立显示行，
    # 直击/引爆类计算点(230003203~230003206)仍留在 神龙吐火 名下。
    230003211: ('爆燃', (230003201, 230003202)),
    230003212: ('暗灼', (230003203,)),
}


def effect_label(skill_id, calcpoint_id):
    # Buff damage may carry the triggering skill ID as well as the -1 sentinel.
    # An exact verified calculation point identifies the effect in either case.
    if type(skill_id) is not int or type(calcpoint_id) is not int:
        return None
    effect = EFFECTS.get(calcpoint_id)
    if effect is None:
        return None
    name, buffs = effect
    return {'name': name, 'source': 'buff_calcpoint_config',
            'evidence': CONFIG_EVIDENCE, 'evidence_sha256': CONFIG_SHA256,
            'buff_ids': buffs}


@lru_cache(maxsize=16384)
def damage_label(skill_id, calcpoint_id=None):
    """Shared hit labels; exact effect points take precedence over triggers.

    Returned metadata is cached and read-only. A statistic owner is a display
    attribution, never evidence of an additional hit or an inferred timestamp.
    """
    from yylogs.game_names import skill_metadata
    from yylogs.final_count_ownership import point_owner
    label = skill_metadata(skill_id)
    effect = effect_label(skill_id, calcpoint_id)
    owner = point_owner(calcpoint_id)
    resolved = label.get('name_resolved_skill_id', skill_id)
    if effect:
        return {**label, 'name': effect['name'], 'name_source': effect['source'],
                'name_evidence': effect['evidence'],
                'name_evidence_sha256': effect['evidence_sha256'],
                'name_configuration_verified': True,
                'resolved_skill_id': owner or skill_id,
                'buff_ids': effect['buff_ids']}
    if owner is not None:
        owned = skill_metadata(owner)
        if owned['name']:
            label = {**owned, 'name_alias_evidence':
                     'yylogs/resources/final_count_ownership.json'}
            resolved = owner
    if not label['name']:
        if type(skill_id) is int and skill_id > 0:
            fallback = f'技能 {skill_id}'
        else:
            point = str(calcpoint_id) if type(calcpoint_id) is int else '未记录'
            fallback = f'未识别伤害（技能 {skill_id} / 计算点 {point}）'
        label = {**label, 'name': fallback}
    return {**label, 'resolved_skill_id': resolved, 'buff_ids': ()}


def display_identity(row):
    """Return a grouping key and label without conflating ID namespaces."""
    skill, calc = row['skill_id'], row.get('calcpoint_id')
    effect = effect_label(skill, calc)
    if effect:
        return ('name', effect['name']), effect['name']
    label = damage_label(skill, calc)
    if label['resolved_skill_id'] != skill and label['name_source'] != 'unmapped':
        return ('name', label['name']), label['name']
    name = row.get('skill_name')
    positive = type(skill) is int and skill > 0
    if positive and isinstance(name, str) and name.strip():
        return ('name', name.strip()), name.strip()
    if positive:
        if label['name_source'] != 'unmapped':
            return ('name', label['name']), label['name']
        return ('id', skill), label['name']
    if label['name_source'] != 'unmapped':
        return ('name', label['name']), label['name']
    point = calc if type(calc) is int else None
    return ('unresolved_effect', skill, point), label['name']


def candidate_label(row):
    label = damage_label(row['skill_id'], row.get('calcpoint_id'))
    _, name = display_identity(row)
    return {'damage_name': name,
            'damage_name_source': label['name_source'] if label['name_source'] != 'unmapped'
                else row.get('skill_name_source', 'unmapped'),
            'damage_name_evidence': label.get('name_alias_evidence') or label['name_evidence'],
            'damage_buff_ids': ','.join(map(str, label['buff_ids']))}


def final_count_identity(row):
    """Attribute observed records only; never infer hits from final amounts."""
    from yylogs.final_count_ownership import point_owner
    owner=point_owner(row.get('calcpoint_id'))
    if owner is not None:
        return ('skill',owner)
    # Exact ordinary Buff periodic points already verified by the RD rules.
    # The triggering skill can be Dragon Fire rather than the DOT identity.
    point=row.get('calcpoint_id')
    periodic={230003211:23700035,230003212:23700065}
    if type(point) is int and point in periodic:
        return ('skill',periodic[point])
    effect=effect_label(row['skill_id'],point)
    if effect:return ('name',effect['name'])
    return ('skill',row['skill_id']) if row['skill_id']>0 else ('unresolved',row['skill_id'],point)
