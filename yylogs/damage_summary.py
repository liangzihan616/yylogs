"""Streaming offline sums of observed candidates, never verified game settlement."""
from decimal import Decimal
import csv
import hashlib
import json
import math
from pathlib import Path
from yylogs.damage_candidates import iter_saved_candidates, source_name_map
from yylogs.actor_names import saved_actors
from yylogs.encounter_timeline import unix_ms
from yylogs.damage_effects import display_identity, effect_label, final_count_identity, damage_label
from yylogs.damage_timing import DamageTiming
from yylogs.member_status import MemberDamageBins
from yylogs.healing_summary import HealingSummary
from yylogs.settlement_summary import load_settlement_summary


def player_metadata(folder, own_entity_id):
    names = source_name_map(folder)
    actors = saved_actors(folder)
    classes = {}
    for row in actors:
        entity = row.get('entity_id')
        if isinstance(entity, str) and isinstance(row.get('class_names'), list):
            classes.setdefault(entity, set()).update(row['class_names'])
    players = {key for key, value in classes.items()
               if value.intersection(('PlayerAvatar', 'Avatar', 'AIAvatar'))
               and 'Npc' not in value}
    if own_entity_id:
        players.add(own_entity_id)
    return {entity: {'id': entity, 'name': names.get(entity), 'local': entity == own_entity_id,
                     'party_membership_verified': False} for entity in players}


def observation_windows(report):
    result = []
    for window in report.get('encounter_windows', []):
        a, b = window.get('start_ms'), window.get('end_ms')
        if (window.get('start_verified') is not True
                or not (window.get('end_verified') is True or (
                    window.get('partial_observation') is True
                    and window.get('observation_end_verified') is True))
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in (a, b))
                or not 0 <= a < b <= report.get('duration_ms', 0)):
            continue
        result.append(window)
    return result


def summarize(folder, report, window=None, *, target_mode='all', cancelled=lambda: False,
              settlement_folder=None):
    """O(players * unique skills) retained state; no per-hit lists or widgets."""
    if target_mode not in ('all', 'boss'):
        raise ValueError('未知伤害对象范围')
    own = report.get('own_entity_id')
    players = player_metadata(folder, own)
    capture_path=Path(folder)/'damage_capture_result.json'
    capture=json.loads(capture_path.read_text('utf-8-sig')) if capture_path.is_file() else {}
    capacity_stop=capture.get('bounded_stop_reason') or (capture.get('agent_stop') or {}).get('reason')
    truncated=capacity_stop in ('record_budget','decode_budget')
    origin = unix_ms(report['origin'])
    if window is not None and window not in observation_windows(report):
        raise ValueError('伤害统计窗口不属于本场已观测区间')
    if target_mode == 'boss' and (not window or not isinstance(window.get('target_id'), str)
                                  or not window['target_id'].strip()):
        raise ValueError('选定 Boss 统计需要同场有效观测区间与 Boss 身份')
    lower = origin + window['start_ms'] if window else None
    upper = origin + window['end_ms'] if window else None
    target = window.get('target_id') if window and target_mode == 'boss' else None
    seconds = Decimal(str(window['end_ms'] - window['start_ms'])) / 1000 if window else None
    depletion = window.get('hp_depletion') if window else None
    timing_range = None
    event_clock = DamageTiming(report, window, capture)
    event_digest = hashlib.sha256()
    if depletion:
        start_range, end_range = window['start_interval_ms'], window['end_interval_ms']
        timing_range = [Decimal(str(end_range[0]-start_range[1]))/1000,
                        Decimal(str(end_range[1]-start_range[0]))/1000]
    members = {}
    count = excluded = unknown_time = out_of_window = out_of_target = 0
    excluded_healing = excluded_kind_conflict = 0
    excluded_healing_amount = Decimal(0)
    member_bins = MemberDamageBins(origin, window)
    healing = HealingSummary(report, players, window)
    from yylogs.rdps_estimate import RdEstimate
    rd_estimator = RdEstimate(report, window, players)
    for row in iter_saved_candidates(folder, journal_digest=event_digest):
        if cancelled():
            raise InterruptedError('统计已取消')
        count += 1
        event_clock.observe(row)
        when = row.get('observed_unix_ms')
        if window:
            if type(when) not in (int, float) or not math.isfinite(when):
                unknown_time += 1
                continue
            # The same callback receiver identity used by the existing capture,
            # not an inferred source/target from numeric similarity.
            if not lower <= when <= upper:
                out_of_window += 1
                continue
        entity = row['fromer_id']
        if row['source_role']=='own' and not own:
            players.setdefault(entity,{'id':entity,'name':row.get('source_name'),'local':True,'party_membership_verified':False})
        # Healing is outgoing to players, never filtered to the selected Boss.
        # It shares only the already validated battle window with damage.
        healing.observe(row)
        if target and not (row.get('receiver_identity_verified') is True
                           and row.get('receiver_entity_id') == target):
            out_of_target += 1
            continue
        if entity not in players:
            excluded += 1
            continue
        if row.get('event_kind') == 'healing':
            excluded_healing += 1
            excluded_healing_amount += Decimal(str(row['damage']))
            continue
        if row.get('event_kind') == 'conflict':
            excluded_kind_conflict += 1
            continue
        rd_estimator.observe(row, Decimal(str(row['damage'])))
        member = members.setdefault(entity, {**players[entity], 'damage': Decimal(0),
                                             'record_count': 0, 'skills': {}, 'observed_count_groups': {}})
        key, name = display_identity(row)
        skill = member['skills'].setdefault(key, {'name': name,
            'skill_ids': set(), 'calcpoint_ids': set(), 'buff_ids': set(), 'name_sources': set(),
            'name_evidence': set(), 'record_count': 0, 'positive_count': 0, 'zero_count': 0,
            'negative_count': 0, 'damage': Decimal(0)})
        value = Decimal(str(row['damage']))
        count_key=final_count_identity(row)
        counts=member['observed_count_groups'].setdefault(count_key,{
            'identity':list(count_key),'name':name,'record_count':0,
            'positive_count':0,'zero_count':0,'negative_count':0,'calcpoint_ids':set()})
        counts['record_count']+=1
        counts['positive_count' if value>0 else 'zero_count' if value==0 else 'negative_count']+=1
        if type(row.get('calcpoint_id')) is int:counts['calcpoint_ids'].add(row['calcpoint_id'])
        member_bins.observe(row, value, skill_key=key, skill_name=name)
        member['damage'] += value
        member['record_count'] += 1
        skill['damage'] += value
        skill['record_count'] += 1
        skill['skill_ids'].add(row['skill_id'])
        calcpoint = row.get('calcpoint_id')
        if type(calcpoint) is int:
            skill['calcpoint_ids'].add(calcpoint)
        effect = effect_label(row['skill_id'], calcpoint)
        label = damage_label(row['skill_id'], calcpoint)
        skill['name_sources'].add(label['name_source'] if label['name_source'] != 'unmapped'
                                  else row.get('skill_name_source', 'unmapped'))
        if label.get('name_alias_evidence'):
            skill['name_evidence'].add(label['name_alias_evidence'])
        if effect:
            skill['buff_ids'].update(effect['buff_ids'])
            skill['name_evidence'].add(effect['evidence'])
        skill['positive_count' if value > 0 else 'zero_count' if value == 0 else 'negative_count'] += 1
    total = sum((m['damage'] for m in members.values()), Decimal(0))
    event_timing = event_clock.finish(event_digest.hexdigest())
    estimated_seconds = event_timing['duration_seconds'] if event_timing else None
    result = []
    for member in members.values():
        member['dps'] = member['damage'] / seconds if seconds else None
        member['estimated_dps'] = member['damage'] / estimated_seconds if estimated_seconds else None
        member['share'] = member['damage'] / total if total > 0 else None
        member['skills'] = sorted(member['skills'].values(), key=lambda g: g['damage'], reverse=True)
        member['observed_count_groups']=[{**g,'calcpoint_ids':sorted(g['calcpoint_ids'])}
            for g in member['observed_count_groups'].values()]
        for skill in member['skills']:
            for field in ('skill_ids', 'calcpoint_ids', 'buff_ids', 'name_sources', 'name_evidence'):
                skill[field] = sorted(skill[field])
            skill['share'] = skill['damage'] / member['damage'] if member['damage'] > 0 else None
            skill['dps'] = skill['damage'] / seconds if seconds else None
            skill['estimated_dps'] = skill['damage'] / estimated_seconds if estimated_seconds else None
        result.append(member)
    rd_estimate = rd_estimator.finish(result, seconds)
    summary = {'members': sorted(result, key=lambda m: m['damage'], reverse=True),
            'dungeon_sid': report.get('scene', {}).get('dungeon_sid'),
            'healing_summary': healing.finish(seconds),
            'settlement_summary': load_settlement_summary(
                folder if settlement_folder is None else settlement_folder, report, cancelled=cancelled),
            'total_damage': total, 'record_count': count, 'excluded_nonplayer_or_unresolved': excluded,
            'unknown_timestamp_records': unknown_time, 'outside_selected_window': out_of_window,
            'outside_selected_target': out_of_target, 'target_mode': target_mode,
            'excluded_healing_records': excluded_healing,
            'excluded_healing_candidate_amount': excluded_healing_amount,
            'excluded_kind_conflict_records': excluded_kind_conflict,
            'healing_filter': 'exact_event_type_or_ordinary_calcpoint_not_skill_name',
            'target_id': target,
            'window_start_unix_ms': lower, 'window_end_unix_ms': upper,
            'known_truncated':truncated,'capacity_stop_reason':capacity_stop,
            'duration_seconds': seconds, 'event_timing': event_timing,
            'partial_observation': bool(window and not window.get('end_verified')),
            'dps_timing_basis': 'persistent_zero_hp_observation' if depletion else 'observation_window',
            'duration_interval_seconds': timing_range,
            'excluded_zero_hp_tail_seconds': Decimal(str(depletion['excluded_tail_ms']))/1000 if depletion else None,
            'damage_semantics_verified': False, 'deduplication_verified': False,
            'complete_event_coverage_verified': False, 'rdps_supported': False,
            'rdps_estimate_supported': rd_estimate['supported'], 'rd_estimate': rd_estimate,
            'member_status': member_bins.finish(players.values())}
    from yylogs.settlement_accounting import read_link,merge
    link=read_link(folder if settlement_folder is None else settlement_folder,report,
                   summary['settlement_summary'],observation_windows(report))
    return merge(summary,link,window,players)


def write_summary_csv(path, summary, entity=None, *, settlement_summary=None):
    from yylogs.settlement_details import detail_rows
    from yylogs.dungeon_identity import resolve_dungeon_identity
    settlement = summary.get('settlement_summary', {}) if settlement_summary is None else settlement_summary
    encounter = resolve_dungeon_identity(summary.get('dungeon_sid'))
    encounter_columns = ["副本名称", "Boss序号", "Boss名称", "遭遇标签", "副本SID"]
    encounter_values = [encounter.get("dungeon_name") or encounter.get("catalog_name") or "",
                        encounter.get("boss_order") or "", encounter.get("boss_name") or "",
                        encounter.get("label") or "", encounter.get("dungeon_sid", "")]
    with Path(path).open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['人物名称', '来源实体ID', '技能名称', '技能ID', '伤害记录数', '已采伤害合计',
                         '占本人已采伤害', '观测DPS', '观测秒数', '完整结算已验证', '已去重验证',
                         '伤害对象', 'Boss实体ID', '区间开始Unix毫秒', '区间结束Unix毫秒', '结束已确认',
                         'DPS计时依据', '采样用时下界秒', '采样用时上界秒', '已剔除归零尾段秒',
                         '伤害计算点ID', '归类参考BuffID', '名称来源', '名称依据',
                         '事件估计DPS', '事件估计秒数', '事件计时依据', '事件计时证据SHA256',
                         '排除治疗候选条数', '排除类型冲突条数',
                         '数据类型', '结算累计金额', '占面板总伤', '快照序号',
                         '结算来源文件', '结算来源SHA256', '本场关联已核验', '结算已计入伤害', '次数口径',
                         *encounter_columns, '人物提供RD_估算_非技能RD', 'RD估算模型'])
        for member in summary['members']:
            if entity and member['id'] != entity:
                continue
            for skill in member['skills']:
                merged='settlement_sequence' in skill
                writer.writerow([member['name'] or '名称未解析', member['id'], skill['name'],
                    ','.join(map(str, skill['skill_ids'])),
                    '' if skill.get('game_final_skill_table') and not skill.get('observed_record_count_available') else skill['record_count'], str(skill['damage']),
                    str(skill['share']) if skill['share'] is not None else '',
                    str(skill['dps']) if skill['dps'] is not None else '',
                    str(summary['duration_seconds']) if summary['duration_seconds'] else '', False, False,
                    '选定 Boss' if summary['target_mode'] == 'boss' else '本场所有目标',
                    summary['target_id'] or '', summary['window_start_unix_ms'],
                    summary['window_end_unix_ms'],
                    bool(summary['duration_seconds'] and not summary['partial_observation']),
                    'HP持续归零观测' if summary.get('dps_timing_basis') == 'persistent_zero_hp_observation' else '观测区间',
                    *(summary.get('duration_interval_seconds') or ['', '']),
                    summary.get('excluded_zero_hp_tail_seconds'),
                    ','.join(map(str, skill['calcpoint_ids'])),
                    ','.join(map(str, skill['buff_ids'])),
                    ','.join(skill['name_sources']), ','.join(skill['name_evidence']),
                    str(skill['estimated_dps']) if skill['estimated_dps'] is not None else '',
                    str(summary['event_timing']['duration_seconds']) if summary.get('event_timing') else '',
                    summary['event_timing']['basis'] if summary.get('event_timing') else '',
                    summary['event_timing']['journal_sha256'] if summary.get('event_timing') else '',
                    summary.get('excluded_healing_records', 0),
                    summary.get('excluded_kind_conflict_records', 0),
                    '游戏最终技能统计' if skill.get('game_final_skill_table') else '同场结算累计替换' if merged else '逐次伤害',
                    str(skill['damage']) if merged else '', '',
                    skill.get('settlement_sequence',''),
                    settlement.get('source_file','') if merged else '',
                    settlement.get('source_sha256','') if merged else '',
                    ('本次采集最终回调' if skill.get('settlement_basis')=='same_capture_final_rpc' else '用户确认') if merged else '', True if merged else '',
                    '\u5df2\u91c7\u4f24\u5bb3\u4e8b\u4ef6\uff1b\u975e\u65bd\u653e\u6b21\u6570/\u6e38\u620f\u7ed3\u7b97\u6b21\u6570' if not skill.get('game_final_skill_table') or skill.get('observed_record_count_available') else '\u672a\u77e5\uff1b\u65e0\u53ef\u9760\u5bf9\u5e94\u4e8b\u4ef6',
                     *encounter_values,
                     str(next((m.get('rd_given_dps') for m in summary['members'] if m['id']==member['id']), '')),
                     (summary.get('rd_estimate') or {}).get('model', '')])
        names={m['id']:m.get('name') for m in summary['members']}
        actors=[entity] if entity else dict.fromkeys(
            s.get('actor_entity_id',s.get('own_entity_id')) for s in settlement.get('snapshots',[]))
        for actor in actors:
            for skill in detail_rows(settlement,actor):
                if settlement.get('source_sha256')==summary.get('settlement_summary',{}).get('source_sha256') and any(
                        a['actor_entity_id']==actor and a['skill_id']==skill['skill_id']
                        for a in summary.get('settlement_accounting',{}).get('applied',[])):
                    continue
                # Preserve the existing observed-damage columns; cumulative amounts
                # have their own columns and cannot be mistaken for extra hits.
                values=['']*30
                values[:4]=[names.get(actor) or '名称未解析',actor,skill['name'],skill['skill_id']]
                writer.writerow(values+['结算累计快照',str(skill['damage']),
                    str(skill['share']) if skill['share'] is not None else '',skill['sequence'],
                    skill['source_file'],skill['source_sha256'],False,False,'未知；结算累计金额不含次数',
                    *encounter_values, '', (summary.get('rd_estimate') or {}).get('model', '')])
