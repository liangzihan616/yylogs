"""Display isolated cumulative snapshots; never supplement damage accounting."""
import csv
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path

from yylogs.runtime_scope import is_supported_pve_sid
from yylogs.game_names import skill_metadata

SKILL_NAMES = {23700043: '失魂落魄', 20901210: '天威伤害',
               23700033: '鼠鼠追命', 2050310511: '醉杀状态'}
FILENAME = 'hook_events.jsonl'
MAX_FILE = 34 * 1024 * 1024
MAX_LINE = 262144
MODES = {'STAT_TICK': (11, 'E_DUNGEON_TICK_SHOW_COMBAT_STATISTIC'),
         'STAT_DETAIL': (20, 'E_DUNGEON_SHOW_COMBAT_STATISTIC'),
         'STAT_JIESUAN': (70, 'E_DUNGEON_END_STATISTIC')}
OTHER_KINDS = {'ready', 'armed', 'stopped', 'receiver_body_verified',
               'diagnostic_error', 'settlement_schema_diagnostic'}


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 9007199254740991


def validate_snapshot(row, sequence, report):
    if not isinstance(row, dict):
        raise ValueError('结算快照不是对象')
    scene = {k: report.get('scene', {}).get(k) for k in ('scene_id', 'dungeon_sid')}
    if (not isinstance(report.get('own_entity_id'), str) or not report['own_entity_id']
            or not isinstance(scene['scene_id'], str) or not scene['scene_id']
            or not is_supported_pve_sid(scene['dungeon_sid'])):
        raise ValueError('当前记录缺少可核验的副本与本人身份')
    if (row.get('kind') != 'settlement_stat_snapshot'
            or type(row.get('sequence')) is not int or row['sequence'] != sequence
            or not 1 <= sequence <= 128 or row.get('scene') != scene
            or row.get('own_entity_id') != report['own_entity_id']
            or row.get('receiver_identity_verified') is not True
            or row.get('included_in_damage') is not False
            or row.get('fight_link_verified') is not False
            or row.get('timestamp_kind') != 'callback_observed_not_hit_time'):
        raise ValueError('结算快照身份、场景、序号或隔离标记不匹配')
    if not number(row.get('observed_unix_ms')) or (row.get('duration_candidate') is not None
            and not number(row['duration_candidate'])):
        raise ValueError('结算快照时间或候选用时无效')
    event = row.get('event')
    if not isinstance(event, dict) or type(event.get('stat_type')) is not int:
        raise ValueError('结算快照事件无效')
    source = row.get('source')
    if source == 'natural_damage_statistic_panel':
        if (row.get('panel_identity_verified') is not True or type(row.get('statistic_type')) is not int
                or row['statistic_type'] != 2 or not number(row.get('total_damage_candidate'))
                or event != {'pc': 84, 'event': 'E_MUZHUANG_SIDE_DAMAGE_UPDATE',
                             'stat_type': 2, 'mode': 'PANEL_DAMAGE'}):
            raise ValueError('不是已绑定人物身份的伤害模式面板快照')
    elif source == 'natural_rpc_dungeon_show_statistic':
        expected = MODES.get(event.get('mode'))
        if (expected is None or set(event) != {'pc', 'event', 'stat_type', 'mode'}
                or (event['pc'], event['event']) != expected or type(event['pc']) is not int
                or not 0 <= event['stat_type'] <= 2147483647):
            raise ValueError('统计回调边界无法核验')
    else:
        raise ValueError('未知结算快照来源；不读取旧空间缓存')
    actor = row.get('actor_entity_id', row['own_entity_id'])
    if ('actor_entity_id' in row or 'actor_identity_verified' in row):
        if (not isinstance(actor, str) or not actor.strip() or len(actor) > 128
                or any(ord(c) < 32 or ord(c) == 127 for c in actor)
                or 'actor_entity_id' not in row or row.get('actor_identity_verified') is not True):
            raise ValueError('结算人物身份无法核验')
    skills = row.get('skills')
    if not isinstance(skills, list) or len(skills) > 512:
        raise ValueError('结算快照技能数超限')
    seen = set()
    for skill in skills:
        if (not isinstance(skill, dict) or set(skill) != {'skill_id', 'damage'}
                or type(skill['skill_id']) is not int or not 0 < skill['skill_id'] <= 9007199254740991
                or skill['skill_id'] in seen or not number(skill['damage'])):
            raise ValueError('结算快照技能重复、金额或技能 ID 无效')
        seen.add(skill['skill_id'])
    selected = [s for s in skills if s['skill_id'] in SKILL_NAMES]
    if row.get('settlement_skills') != selected:
        raise ValueError('结算技能子集与原始技能表不符')
    return {'sequence': sequence, 'own_entity_id': row['own_entity_id'],
        'actor_entity_id': actor, 'scene': scene,
        'source': source, 'event_mode': event['mode'], 'observed_unix_ms': row['observed_unix_ms'],
        'final_settlement': (source == 'natural_rpc_dungeon_show_statistic'
            and event['mode'] == 'STAT_JIESUAN' and event['stat_type'] == 2
            and row.get('actor_identity_verified') is True),
        'duration_candidate': Decimal(str(row['duration_candidate'])) if row.get('duration_candidate') is not None else None,
        'total_damage_candidate': Decimal(str(row['total_damage_candidate'])) if source == 'natural_damage_statistic_panel' else None,
        'skills': [{'skill_id': s['skill_id'], 'name': SKILL_NAMES[s['skill_id']],
                    'damage': Decimal(str(s['damage']))} for s in selected],
        'all_damage_skills': [{'skill_id': s['skill_id'],
            'name': SKILL_NAMES.get(s['skill_id']) or skill_metadata(s['skill_id'])['name'] or f"技能 {s['skill_id']}",
            'damage': Decimal(str(s['damage']))} for s in skills],
        'included_in_damage': False, 'fight_link_verified': False}


def load_settlement_summary(folder, report, *, path=None, cancelled=lambda: False):
    """Read only the selected record or an explicitly selected file, no search."""
    path = Path(path) if path is not None else Path(folder) / FILENAME
    result = {'status': 'missing', 'snapshots': [], 'source_file': path.name,
              'source_sha256': None, 'error': None, 'included_in_damage': False,
              'fight_link_verified': False}
    if not path.exists():
        return result
    try:
        if path.is_symlink() or path.stat().st_size > MAX_FILE:
            raise ValueError('不读取链接文件或超限结算日志')
        snapshots = []
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for index in range(512):
                if cancelled():
                    raise InterruptedError('结算快照读取已取消')
                line = stream.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE:
                    raise ValueError('结算日志单条超限')
                digest.update(line)
                row = json.loads(line.decode('utf-8-sig'))
                if not isinstance(row, dict):
                    raise ValueError('结算日志条目不是对象')
                kind = row.get('kind')
                if kind == 'settlement_stat_snapshot':
                    snapshots.append(validate_snapshot(row, len(snapshots) + 1, report))
                elif kind not in OTHER_KINDS:
                    raise ValueError('不是支持的自然结算 Hook 日志')
            else:
                if stream.read(1):
                    raise ValueError('结算日志条数超限')
        result.update(status='available' if snapshots else 'empty', snapshots=snapshots,
                      source_sha256=digest.hexdigest())
    except InterruptedError:
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        result.update(status='invalid', error=str(exc))
    return result


def write_settlement_csv(path, snapshot, summary):
    """Export one chosen cumulative snapshot, never combine repeated readings."""
    with Path(path).open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['人物实体ID', '技能', '技能ID', '结算累计候选', '快照序号',
            '回调观测Unix毫秒_非命中时刻', '候选用时秒', '面板总伤候选',
            '来源', '来源SHA256', '本场已核验', '已计入伤害'])
        for skill in snapshot['skills']:
            writer.writerow([snapshot.get('actor_entity_id', snapshot['own_entity_id']), skill['name'], skill['skill_id'],
                skill['damage'], snapshot['sequence'], snapshot['observed_unix_ms'],
                snapshot['duration_candidate'], snapshot['total_damage_candidate'],
                snapshot['source'], summary.get('source_sha256'), False, False])
