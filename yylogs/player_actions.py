"""Server-observed action lifetimes, adapted from LQ wire_timeline.action_timing.

Upstream: Liquidzk/LQ-yysls-logs, f67312f0b821a918308422a8aeaed715550f6dc0.
Queue occupancy is not client animation time, movement, or a damage hit.
"""
from collections import defaultdict

ACTION_FIELDS = frozenset((
    'observedAt', 'actionKind', 'sourceActorHash', 'targetActorHash', 'rawSkillId',
    'identifierHash', 'segment', 'queue', 'sequence', 'transportRecordOrdinal',
    'syncSubIndex', 'endConfirmed', 'endReason', 'carrier', 'initial',
))
KINDS = frozenset(('cast', 'snapshot', 'end', 'boundary', 'visibility', 'segment'))


def _matches(row, current):
    return (row.get('rawSkillId') == current.get('rawSkillId') and
            (not row.get('identifierHash') or
             row['identifierHash'] == current.get('identifierHash')))


def validate_actions(rows, start, end, members):
    from yylogs.packet_records import timestamp, MAX_ROWS
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ValueError('抓包动作列表无效或过大')
    lower, upper = timestamp(start), timestamp(end)
    for row in rows:
        if not isinstance(row, dict) or not set(row) <= ACTION_FIELDS:
            raise ValueError('抓包动作包含不支持的字段')
        kind, actor = row.get('actionKind'), row.get('sourceActorHash')
        if (not isinstance(kind, str) or kind not in KINDS or
                (not isinstance(actor, (str, type(None)))) or
                (actor not in members and not (kind == 'visibility' and actor is None))):
            raise ValueError('抓包动作类型或角色无效')
        try:
            at = timestamp(row.get('observedAt', ''))
        except (ValueError, TypeError, AttributeError, OverflowError) as exc:
            raise ValueError('抓包动作时间无效') from exc
        if not lower <= at <= upper or (at == upper and kind not in ('end', 'boundary', 'visibility')):
            raise ValueError('抓包动作超出战斗边界')
        skill = row.get('rawSkillId')
        if kind != 'visibility' and (type(skill) is not int or not 0 < skill <= 2**53-1):
            raise ValueError('抓包动作技能编号无效')
        for key in ('identifierHash', 'targetActorHash', 'queue', 'endReason', 'carrier'):
            value = row.get(key)
            if value is not None and (not isinstance(value, str) or not 0 < len(value) <= 160):
                raise ValueError('抓包动作标识无效')
        for key in ('sequence', 'transportRecordOrdinal', 'syncSubIndex', 'segment'):
            value = row.get(key)
            if value is not None and (type(value) is not int or not 0 <= value <= 2**53-1):
                raise ValueError('抓包动作顺序或分段无效')
        if kind == 'segment' and row.get('segment') is None:
            raise ValueError('抓包动作缺少分段')
        for key in ('endConfirmed', 'initial'):
            if key in row and type(row[key]) is not bool:
                raise ValueError('抓包动作边界标记无效')
    return rows


def battle_actions(rows, start, end, members, cancelled=lambda: False):
    """Keep the battle window plus censored active snapshots at its left edge."""
    from yylogs.packet_records import timestamp
    lower, upper = timestamp(start), timestamp(end)
    ordered = []
    seen = set()
    for index, raw in enumerate(rows):
        if index % 256 == 0 and cancelled():
            raise InterruptedError()
        if raw.get('actionKind') not in KINDS:
            continue
        actor = raw.get('sourceActorHash')
        if actor not in members and not (raw['actionKind'] == 'visibility' and actor is None):
            continue
        at = timestamp(raw['observedAt'])
        if at > upper or (at == upper and raw['actionKind'] not in ('end', 'boundary', 'visibility')):
            continue
        identity = (raw.get('sequence'), raw.get('transportRecordOrdinal'))
        if identity[0] is not None:
            if identity in seen:
                continue
            seen.add(identity)
        row = {key: value for key, value in raw.items() if key in ACTION_FIELDS}
        ordered.append((at, row.get('transportRecordOrdinal') or 0, index, row))
    ordered.sort(key=lambda item: item[:3])
    active, result = {}, []
    for at, _, _, row in ordered:
        if at >= lower:
            result.append(row)
            continue
        actor, kind = row.get('sourceActorHash'), row['actionKind']
        if kind == 'visibility':
            for slot in list(active):
                if actor is None or slot[0] == actor:
                    del active[slot]
            continue
        slot = (actor, row.get('queue') or 'skill')
        if kind in ('cast', 'snapshot'):
            active[slot] = row
        elif kind in ('end', 'boundary') and slot in active and _matches(row, active[slot]):
            del active[slot]
    seeds = [{**row, 'observedAt': start, 'actionKind': 'snapshot', 'initial': True}
             for row in active.values()]
    return validate_actions(seeds + result, start, end, members)


def action_timing(rows, start, end, cancelled=lambda: False):
    """Pair actor/queue/instance lifetimes; never pair by skill ID alone."""
    from yylogs.packet_records import timestamp
    lower, upper = timestamp(start)/1000, timestamp(end)/1000
    active, last_end, spans, gaps = {}, {}, [], []
    epochs = defaultdict(int)

    def close(slot, at, known, reason):
        current = active.pop(slot)
        known = bool(known and at >= current['at'])
        a, b = max(lower, current['at']), min(upper, at)
        if at >= lower and current['at'] <= upper:
            start_known = current['startKnown'] and current['at'] >= lower
            end_known = known and at <= upper
            spans.append({
                'actor': slot[0], 'skillId': current['rawSkillId'],
                'identifier': current.get('identifierHash'), 'queue': slot[1],
                'start': a-lower, 'end': b-lower,
                'startKnown': start_known, 'endKnown': end_known,
                'duration': at-current['at'] if start_known and end_known else None,
                'observedSeconds': max(0, b-a), 'endReason': reason,
                'target': current.get('targetActorHash'),
                'record': current.get('transportRecordOrdinal'), 'epoch': epochs[slot[0]],
                'gapBefore': current['gapBefore'], 'phases': current['phases'],
            })
        if known:
            last_end[slot] = (at, current['rawSkillId'])
        else:
            last_end.pop(slot, None)

    ordered = sorted(enumerate(rows), key=lambda item: (
        timestamp(item[1]['observedAt']), item[1].get('transportRecordOrdinal') or 0, item[0]))
    for index, (_, row) in enumerate(ordered):
        if index % 256 == 0 and cancelled():
            raise InterruptedError()
        at = timestamp(row['observedAt'])/1000
        if at > upper:
            continue
        actor, kind = row.get('sourceActorHash'), row['actionKind']
        if kind == 'visibility':
            affected = ({slot[0] for slot in set(active) | set(last_end)} if actor is None else {actor})
            for slot in list(active):
                if slot[0] in affected:
                    close(slot, at, False, row.get('endReason') or 'visibility')
            for slot in list(last_end):
                if slot[0] in affected:
                    del last_end[slot]
            for who in affected:
                epochs[who] += 1
            continue
        if not actor:
            continue
        slot = (actor, row.get('queue') or 'skill')
        current = active.get(slot)
        ident, skill = row.get('identifierHash'), row.get('rawSkillId')
        if kind in ('cast', 'snapshot'):
            if current and ident and ident == current.get('identifierHash') and skill == current['rawSkillId']:
                continue
            ambiguous = bool(current and current['rawSkillId'] == skill)
            if current:
                close(slot, at, False, 'next-action')
            gap = None
            previous = last_end.pop(slot, None)
            if kind == 'cast' and previous and lower <= previous[0] <= at:
                gap = at-previous[0]
                if gap > 1e-6:
                    gaps.append({'actor': actor, 'start': previous[0]-lower, 'end': at-lower,
                                 'duration': gap, 'previousSkill': previous[1], 'nextSkill': skill})
            active[slot] = {**row, 'at': at, 'startKnown': kind == 'cast',
                            'ambiguous': ambiguous, 'gapBefore': gap, 'phases': []}
        elif kind in ('end', 'boundary') and current and _matches(row, current):
            known = (kind == 'end' and row.get('endConfirmed') is not False and
                     (bool(ident) or not current['ambiguous']))
            close(slot, at, known, row.get('endReason') or 'server-skill-end')
        elif kind == 'segment' and current and skill == current['rawSkillId'] and at >= lower:
            current['phases'].append({'t': at-lower, 'segment': row.get('segment')})
    for slot in list(active):
        close(slot, upper, False, 'range-end')
    spans.sort(key=lambda row: (row['start'], row['record'] or 0, row['actor']))
    counts = defaultdict(int)
    for row in spans:
        counts[row['actor']] += 1
        row['order'] = counts[row['actor']]
    return {'spans': spans, 'gaps': gaps, 'duration': upper-lower}


def prepare_player_actions(record, cancelled=lambda: False):
    """Called by AnalysisPreparation, never on the GUI thread."""
    from yylogs.game_names import skill_metadata
    from yylogs.packet_records import recover_packet_actions
    snapshot = (record.get('buff_timeline') or {}).get('packet_snapshot')
    if not snapshot:
        return {'spans': [], 'gaps': [], 'duration': 0, 'available': False,
                'message': '此记录没有服务器动作观测数据'}
    if cancelled():
        raise InterruptedError()
    if record.get('packet_observations_recovered') and 'actions' in snapshot:
        from yylogs.packet_action_archive import archive_actions
        try:
            archive_actions(snapshot, record.get('record_folder'), snapshot['actions'])
        except (OSError, ValueError):
            pass
    snapshot = recover_packet_actions(snapshot, record.get('record_folder'), cancelled)
    if cancelled():
        raise InterruptedError()
    result = action_timing(snapshot.get('actions', []), snapshot['start'], snapshot['end'], cancelled)
    labels = {}
    for row in result['spans']:
        if cancelled():
            raise InterruptedError()
        skill = row['skillId']
        if skill not in labels:
            labels[skill] = skill_metadata(skill)['name'] or f'技能 {skill}'
        row['name'] = labels[skill]
    for row in result['gaps']:
        row['previousName'] = labels.get(row['previousSkill'], f"技能 {row['previousSkill']}")
        row['nextName'] = labels.get(row['nextSkill'], f"技能 {row['nextSkill']}")
    result.update(available=bool(result['spans']), message=(
        '本场未记录到玩家动作；需要包含动作事件的抓包记录' if 'actions' in snapshot else
        '此旧记录未内嵌动作，原始动作档案或副本已缺失；请导入包含动作的日志包'))
    return result
