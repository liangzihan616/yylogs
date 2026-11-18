"""Offline bridge for LQ wire ledgers; no game or network access.

Packet snapshots contain only encounter data, never connection/cipher state.
Counters and detailed settlements are cumulative, not extra damage events.
"""
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
import hashlib
import json
import math
import re
import struct
import zlib

from yylogs.packet_limits import MAX_BATTLE_SECONDS, MAX_BATTLE_MS

SCHEMA = 'yylogs.packet-record/1'
OBSERVATION_REVISION = 7
UPSTREAM = 'f67312f0b821a918308422a8aeaed715550f6dc0'
MAX_LEDGER = 256 * 1024 * 1024
MAX_LINE = 4 * 1024 * 1024
MAX_ROWS = 200000


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e15


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.utcoffset() is None:
        raise ValueError('抓包记录时间缺少时区')
    return parsed.timestamp() * 1000


def read_json(path):
    if path.is_symlink() or path.stat().st_size > MAX_LINE:
        raise ValueError('抓包元数据文件无效或过大')
    return json.loads(path.read_text('utf-8-sig'))


def ledger(folder, name, *, limits=None):
    """Read plain JSONL or bounded YLZ1 frames, rejecting truncation."""
    plain = Path(folder) / (name + '.jsonl')
    packed = Path(str(plain) + '.ylz')
    if limits is not None:
        # Checkpoints explicitly list absent files too (by omission). Never read
        # a ledger created after the decoder sealed this view.
        selected = [p for p in (plain, packed) if p.name in limits]
        if not selected:return
        if len(selected) != 1:raise ValueError('同一账本存在两个版本')
        path = selected[0]
    else:
        path = plain if plain.exists() else packed
    if limits is None and plain.exists() and packed.exists():
        raise ValueError('同一账本存在两个版本，不能重复导入')
    if not path.exists():
        if limits is not None:raise ValueError('已刷新的账本丢失')
        return
    if path.is_symlink() or path.stat().st_size > MAX_LEDGER:
        raise ValueError('抓包账本无效或超过大小限制')
    size = path.stat().st_size if limits is None else limits[path.name]
    if type(size) is not int or not 0 <= size <= MAX_LEDGER or path.stat().st_size < size:
        raise ValueError('账本快照边界无效')
    def chunks(stream):
        remaining = size
        def read(count):
            nonlocal remaining
            part = stream.read(min(count, remaining))
            remaining -= len(part)
            return part
        if path == plain:
            while part := read(1024 * 1024):
                yield part
            if remaining:raise ValueError('抓包账本被截断')
            return
        while header := read(12):
            if len(header) != 12:
                raise ValueError('压缩账本帧头不完整')
            magic, encoded, decoded = struct.unpack('<4sII', header)
            if magic != b'YLZ1' or not 0 < encoded <= 8 * 1024 * 1024 or not 0 < decoded <= 8 * 1024 * 1024:
                raise ValueError('压缩账本帧长度无效')
            raw = read(encoded)
            if len(raw) != encoded:
                raise ValueError('压缩账本被截断')
            inflater = zlib.decompressobj()
            part = inflater.decompress(raw, decoded + 1)
            if len(part) != decoded or not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
                raise ValueError('压缩账本帧校验失败')
            yield part
        if remaining:raise ValueError('抓包账本被截断')
    total = count = 0
    pending = b''
    with path.open('rb') as stream:
        for part in chunks(stream):
            total += len(part)
            if total > MAX_LEDGER:
                raise ValueError('解压后的账本超过大小限制')
            lines = (pending + part).split(b'\n')
            pending = lines.pop()
            if len(pending) > MAX_LINE:
                raise ValueError('抓包账本行过大')
            for line in lines:
                if not line.strip():
                    continue
                count += 1
                if count > MAX_ROWS or len(line) > MAX_LINE:
                    raise ValueError('抓包账本超过行数或行大小限制')
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError('抓包事件必须是对象')
                yield row
    if pending.strip():
        raise ValueError('抓包账本末行不完整；原始文件已保留')


def _identity(value):
    return isinstance(value, str) and 0 < len(value) <= 160


def _within_total(parts, total):
    amount = math.fsum(parts)
    return amount <= total or math.isclose(amount, total, rel_tol=1e-12, abs_tol=1e-6)


def _settlement_proofs(capture, *, limits=None):
    """The decoder's accepted timer transition links a reply to one attempt."""
    proofs = {}
    for row in ledger(capture, 'timer-events', limits=limits):
        if row.get('trigger') == 'server-detailed-settlement':
            key = ((row.get('source') or {}).get('sessionId'), row.get('observedAt'),
                   row.get('transportRecordOrdinal'))
            proofs[key] = row.get('battleTimer') or {}
    # Older collectors finalized the timer from the periodic counter, then
    # rejected the detailed reply because the timer was no longer 'ended'.
    # Keep the independently recorded completed counters as strict evidence.
    for row in ledger(capture, 'statistics', limits=limits):
        if row.get('counterTiming') == 'progress':
            continue  # A ticking elapsed value is not a completed-duration proof.
        clock = row.get('encounterClock') or {}
        session = (row.get('source') or {}).get('sessionId')
        duration = row.get('timeCostSeconds')
        if (not session or (clock.get('source') or {}).get('sessionId') != session
                or not number(duration) or not 0 < duration <= MAX_BATTLE_SECONDS
                or any(clock.get(k) is None for k in ('startObservedAt', 'endObservedAt', 'npcNo', 'spaceHash'))
                or row.get('npcNo') != clock['npcNo']):
            continue
        try:
            delay = timestamp(row['observedAt']) - timestamp(clock['endObservedAt'])
            elapsed = (timestamp(clock['endObservedAt']) - timestamp(clock['startObservedAt'])) / 1000
        except (ValueError, TypeError, KeyError):
            continue
        if not 0 <= delay <= 5000 or not 0 < elapsed <= MAX_BATTLE_SECONDS or abs(duration - elapsed) > 5:
            continue
        key = ('counter', session, *(clock[k] for k in ('startObservedAt', 'endObservedAt', 'npcNo', 'spaceHash')))
        proofs.setdefault(key, []).append(row)
    return proofs


def _completed_counter_matches(row, clock, session, proofs):
    context = row.get('encounterContext') or {}
    keys = ('startObservedAt', 'endObservedAt', 'npcNo', 'spaceHash')
    if any(clock.get(k) is None or context.get(k) != clock[k] for k in keys):
        return False
    if (context.get('source') or {}).get('sessionId') != session:
        return False
    try:
        if not 0 <= timestamp(row['observedAt']) - timestamp(clock['endObservedAt']) <= 5000:
            return False
    except (KeyError, ValueError, TypeError):
        return False
    actors = row.get('actors') or []
    ids = [a.get('actorHash') for a in actors]
    if not actors or len(set(ids)) != len(ids):
        return False
    for counter in proofs.get(('counter', session, *(clock[k] for k in keys)), []):
        if counter.get('timeCostSeconds') != row.get('timeCostSeconds'):
            continue
        final_rows = counter.get('actors') or []
        final = {a.get('idHash'): a for a in final_rows}
        if len(final) != len(final_rows) or set(final) != set(ids):
            continue
        if all(all(number((a.get('totals') or {}).get(src)) and
                       (a.get('totals') or {}).get(src) == final[a['actorHash']].get(dst)
                       for src, dst in (('damage', 'damage'), ('heal', 'heal'), ('absorb_dmg', 'absorbDmg')))
               for a in actors):
            return True
    return False


def _settlement_matches(row, clock, session, proofs):
    context = row.get('encounterContext') or {}
    if (row.get('kind') != 'server-detailed-settlement' or row.get('statType') != 2
            or (row.get('source') or {}).get('sessionId') not in (None, session)
            or context.get('startObservedAt') != clock['startObservedAt']
            or context.get('npcNo') != clock.get('npcNo')):
        return False
    if row.get('associationValidated') is True:
        return True
    if _completed_counter_matches(row, clock, session, proofs):
        return True
    # Receive order alone is insufficient. Require the exact reply that the
    # live timer accepted after an observed end, with matching scope/roster.
    proof = proofs.get((session, row.get('observedAt'), row.get('transportRecordOrdinal')), {})
    if (proof.get('phase') != 'finalized' or not proof.get('key')
            or not str(proof['key']).startswith(session + ':')
            or proof.get('settledAt') != row.get('observedAt')
            or proof.get('durationSeconds') != row.get('timeCostSeconds')):
        return False
    for key in ('startObservedAt', 'endObservedAt', 'npcNo', 'spaceHash'):
        if clock.get(key) is None or context.get(key) != clock[key] or proof.get(key) != clock[key]:
            return False
    final = {a.get('idHash'): a for a in proof.get('finalActors') or []}
    actors = row.get('actors') or []
    if not final or len(actors) != len(final) or {a.get('actorHash') for a in actors} != set(final):
        return False
    return all(all((a.get('totals') or {}).get(src) == final[a['actorHash']].get(dst)
                   for src, dst in (('damage', 'damage'), ('heal', 'heal'), ('absorb_dmg', 'absorbDmg')))
               for a in actors)


def _boss_qi_rows(events, boss_ids, lower, upper):
    """A replicated property persists while the same visible NPC stays live.

    HP messages are presence evidence, not new qi samples. They only retain an
    already received state; only an explicit invalidation stops the hold.
    """
    if len(boss_ids) != 1:
        return []
    output, current, seed = [], None, None
    for row in events:
        when = timestamp(row['observedAt'])
        if when >= upper:
            break
        kind = row.get('kind')
        if kind == 'target-qi-state' and row.get('targetActorHash') in boss_ids:
            current = ({'qi': row['currentQi'], 'state': True, 'weak': row.get('weak'),
                        **({'qi_max': row['maxQi']} if number(row.get('maxQi')) and row['maxQi'] > 0 else {})}
                       if number(row.get('currentQi')) and not row.get('reason') else None)
            if when >= lower:
                output.append({'timestamp': row['observedAt'], **(current or {'gap': True})})
        elif kind == 'npc-health' and row.get('actorHash') in boss_ids:
            if (row.get('available') is False
                    and row.get('reason') not in ('unsupported-health-build', 'incomplete-health-snapshot')):
                current = None
                if when >= lower:
                    output.append({'timestamp': row['observedAt'], 'gap': True})
            elif current is not None:
                if when >= lower:
                    output.append({'timestamp': row['observedAt'], **current})
        if when < lower:
            seed = dict(current) if current else None
    if seed:
        from datetime import datetime, timezone
        output.insert(0, {'timestamp': datetime.fromtimestamp(lower / 1000, timezone.utc).isoformat(), **seed})
    return output


@lru_cache(maxsize=1)
def _boss_observation_templates():
    value = read_json(Path(__file__).parent / 'resources/boss_observation_templates.json')
    ids = value.get('template_ids')
    if (value.get('schema') != 'yylogs.boss-observation-templates/1'
            or not isinstance(ids, list) or not ids or len(ids) > 10000
            or any(type(no) is not int or no <= 0 for no in ids)):
        raise ValueError('Boss 观测配置无效')
    return frozenset(ids)


def encounter_boss_ids(context, npc_no):
    """Counter npcNo is a boss group, not necessarily an NPC template number."""
    npcs = context.get('npcs') or []
    # Dungeon-bound entities are explicit identities; ambient NPCs use -1.
    bound = {row['actorHash'] for row in npcs
             if _identity(row.get('actorHash')) and row.get('entityClass') == 'Npc'
             and type(row.get('dungeonSerialId')) is int and row['dungeonSerialId'] >= 0}
    if any('dungeonSerialId' in row for row in npcs):
        if len(bound) <= 1:
            return bound
        # A dungeon also binds mechanism NPCs. Only an unambiguous configured
        # Boss HUD template can disambiguate them; never rank by HP or damage.
        configured = {row['actorHash'] for row in npcs if row.get('actorHash') in bound
                      and row.get('npcTemplateNo') in _boss_observation_templates()}
        return configured if len(configured) == 1 else set()
    matched = {row['actorHash'] for row in npcs
               if _identity(row.get('actorHash')) and row.get('npcTemplateNo') == npc_no}
    return matched if len(matched) == 1 else set()


def _member_health_rows(events, members, lower, upper, start):
    """Keep wire property changes and the last valid state at pull time."""
    prior, output = {}, []
    for row in events:
        actor = row.get('actorHash')
        if row.get('kind') != 'member-health' or actor not in members:
            continue
        when = timestamp(row['observedAt'])
        if when >= upper:
            break
        current, maximum = row.get('currentHp'), row.get('maxHp')
        valid = row.get('available') is True and number(current)
        if not number(maximum) or maximum <= 0 or (valid and current > maximum):
            maximum = None
        sample = {'actor': actor, 'timestamp': row['observedAt'], 'observed_at': row['observedAt'],
                  'hp': current if valid else None, 'hp_max': maximum if valid else None,
                  'gap': not valid, 'state': True}
        if when < lower:
            prior[actor] = sample
        else:
            output.append(sample)
    seeds = [{**row, 'timestamp': start} for row in prior.values() if not row['gap']]
    return sorted(seeds + output, key=lambda row: timestamp(row['timestamp']))


def snapshot_from_battle(path, *, battle_bytes=None, ledger_limits=None, identity_names=None):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > MAX_LINE:raise ValueError('抓包元数据无效或过大')
    raw_battle = path.read_bytes() if battle_bytes is None else battle_bytes
    if path.is_symlink() or len(raw_battle) > MAX_LINE:raise ValueError('抓包元数据无效或过大')
    battle = json.loads(raw_battle.decode('utf-8-sig'))
    clock, stats = battle['clock'], battle['statistics']
    start, end = clock['startObservedAt'], clock.get('endObservedAt')
    if not end or battle.get('status') == 'active':
        raise ValueError('战斗尚未结束，请先停止采集')
    lower, upper = timestamp(start), timestamp(end)
    if not 0 < upper - lower <= MAX_BATTLE_MS:
        raise ValueError('抓包战斗区间无效')
    session = battle['source']['sessionId']
    context = clock.get('identityContext') or {}
    snapshot = {'schema': SCHEMA, 'backend': 'packet', 'upstream_commit': UPSTREAM,
                'session': session, 'battle_id': battle['id'], 'start': start, 'end': end,
                'partial': battle.get('status') != 'ended', 'dungeon_sid': context.get('dungeonSid'),
                'npc_no': clock.get('npcNo'), 'names': {}, 'actors': [],
                'hits': [], 'hp': [], 'qi': [], 'buffs': [], 'details': {},
                'duration_seconds': (upper-lower)/1000, 'duration_basis': 'packet_receive_window',
                'source_sha256': hashlib.sha256(raw_battle).hexdigest(), 'observation_revision': OBSERVATION_REVISION}
    timer = stats.get('battleTimer') or {}
    if (timer.get('startObservedAt') == start and timer.get('phase') == 'finalized'
            and number(timer.get('durationSeconds')) and 0 < timer['durationSeconds'] <= MAX_BATTLE_SECONDS):
        snapshot.update(duration_seconds=timer['durationSeconds'], duration_basis='server_battle_timer')
    actors = stats.get('actors') or []
    for row in actors:
        if _identity(row.get('idHash')):
            if isinstance(row.get('name'),str):snapshot['names'][row['idHash']]=row['name'][:160]
            snapshot['actors'].append({'id': row['idHash'],
                'damage': row.get('damage') if number(row.get('damage')) else None,
                'healing': row.get('heal') if number(row.get('heal')) else None})
    names = battle.get('identities') or []
    if isinstance(names, dict):
        names = names.get('names', [])
    for row in names:
        if _identity(row.get('idHash')) and isinstance(row.get('name'), str):
            snapshot['names'][row['idHash']] = row['name'][:160]
    from yylogs.packet_battle_logs import event_folder
    capture = event_folder(path)
    if identity_names is not None or (capture / 'identities.json').is_file():
        for row in identity_names if identity_names is not None else read_json(capture / 'identities.json').get('names', []):
            if _identity(row.get('idHash')) and isinstance(row.get('name'), str):
                snapshot['names'].setdefault(row['idHash'], row['name'][:160])
    # Session ledger timestamps are receive timestamps. Never attach a late
    # settlement by amount, player name or proximity alone.
    def rows(name):
        seen = set()
        for row in ledger(capture, name, limits=ledger_limits):
            if (row.get('source') or {}).get('sessionId') not in (None, session):
                continue
            observed = row.get('observedAt')
            if not observed or not lower <= timestamp(observed) < upper:
                continue
            key = (row.get('sequence'), row.get('transportRecordOrdinal'))
            if row.get('sequence') is not None:
                if key in seen:
                    continue
                seen.add(key)
            yield row
    for row in rows('damage-events'):
        if _identity(row.get('sourceActorHash')) and number(row.get('amount')):
            snapshot['hits'].append({key: row.get(source) for key, source in (
                ('source', 'sourceActorHash'), ('target', 'targetActorHash'), ('amount', 'amount'),
                ('skill', 'rawSkillId'), ('calcpoint', 'rawCalcpointId'), ('time', 'observedAt'))})
            snapshot['hits'][-1]['transportRecordOrdinal'] = row.get('transportRecordOrdinal')
    from yylogs.player_actions import battle_actions
    snapshot['actions'] = battle_actions((row for row in ledger(capture, 'action-events', limits=ledger_limits)
        if (row.get('source') or {}).get('sessionId') in (None, session)),
        start, end, {row['id'] for row in snapshot['actors']})
    boss_ids = encounter_boss_ids(context, clock.get('npcNo'))
    encounter_events = [row for row in ledger(capture, 'encounter-events', limits=ledger_limits)
                        if (row.get('source') or {}).get('sessionId') in (None, session)]
    encounter_events.sort(key=lambda row: (timestamp(row['observedAt']), row.get('transportRecordOrdinal', 0)))
    hp_seed = None
    for row in encounter_events:
        when = timestamp(row['observedAt'])
        if when >= upper:
            continue
        if row.get('kind') == 'npc-health' and len(boss_ids)==1 and row.get('actorHash') in boss_ids:
            sample = {'timestamp': row['observedAt'], 'hp': row.get('currentHp'),
                      'hp_max': row.get('maxHp'), 'gap': row.get('available') is False, 'state': True}
            if when < lower:
                hp_seed = sample
            else:
                snapshot['hp'].append(sample)
    if hp_seed and not hp_seed['gap']:
        snapshot['hp'].insert(0, {**hp_seed, 'timestamp': start})
    snapshot['qi'] = _boss_qi_rows(encounter_events, boss_ids, lower, upper)
    snapshot['member_hp'] = _member_health_rows(encounter_events,
        {row['id'] for row in snapshot['actors']}, lower, upper, start)
    from yylogs.rdps_history import snapshot_rd_history
    snapshot['buffs'], snapshot['attributes'] = snapshot_rd_history(
        ledger(capture, 'buff-events', limits=ledger_limits), encounter_events, session, start, end)
    from yylogs.rdps_gaps import snapshot_statistics
    snapshot['rd_statistics'] = snapshot_statistics(ledger(capture, 'statistics', limits=ledger_limits),
                                                    session, start, end, clock.get('npcNo'))
    snapshot['boss_ids'] = sorted(value for value in boss_ids if value)
    from yylogs.packet_entities import entity_observations, select_entity
    snapshot['entities'] = entity_observations(context, encounter_events, lower, upper, start, snapshot['names'])
    snapshot = select_entity(snapshot)
    proofs = _settlement_proofs(capture, limits=ledger_limits)
    for row in ledger(capture, 'settlement-details', limits=ledger_limits):
        if not _settlement_matches(row, clock, session, proofs):
            continue
        for actor in row.get('actors', []):
            skill_totals = defaultdict(float)
            for target in actor.get('targets', []):
                for skill in (target.get('categories') or {}).get('damage', {}).get('skills', []):
                    no = str(skill.get('skillId'))
                    if no.isascii() and no.isdigit() and 0 < int(no) <= 10**19 and number(skill.get('amount')):
                        skill_totals[str(int(no))] += skill['amount']
            total = (actor.get('totals') or {}).get('damage')
            if _identity(actor.get('actorHash')) and number(total) and _within_total(skill_totals.values(), total):
                counts, maxima = defaultdict(int), defaultdict(float)
                for target in (actor.get('ownSkillDetails') or {}).get('targets', []):
                    for skill in (target.get('categories') or {}).get('damage', []):
                        no = str(skill.get('skillId'))
                        if (no in skill_totals and type(skill.get('count')) is int and skill['count'] > 0
                                and number(skill.get('max'))):
                            counts[no] += skill['count']
                            maxima[no] = max(maxima[no], skill['max'])
                snapshot['details'][actor['actorHash']] = {
                    'damage': total, 'skills': dict(skill_totals),
                    'counts': dict(counts), 'maxima': dict(maxima)}
    return validate_snapshot(snapshot)


def validate_snapshot(value):
    if not isinstance(value, dict) or value.get('schema') != SCHEMA or value.get('backend') != 'packet':
        raise ValueError('抓包记录格式不受支持')
    allowed={'schema','backend','upstream_commit','session','battle_id','start','end','partial',
             'dungeon_sid','npc_no','names','actors','hits','hp','qi','buffs','details',
             'duration_seconds','duration_basis','source_sha256','boss_ids'}
    if set(value) - {'observation_revision', 'member_hp', 'actions', 'attributes', 'rd_statistics', 'entities'} != allowed:raise ValueError('抓包记录包含不支持的字段')
    if 'observation_revision' in value and value['observation_revision'] not in (2, 3, 4, 5, 6, OBSERVATION_REVISION):raise ValueError('抓包观测修订无效')
    if not _identity(value['session']) or not _identity(value['battle_id']):raise ValueError('抓包场次身份无效')
    if value['duration_basis'] not in ('packet_receive_window','server_battle_timer'):raise ValueError('抓包计时来源无效')
    if not isinstance(value['names'],dict) or len(value['names'])>5000 or any(
            not _identity(k) or not isinstance(v,str) or len(v)>160 for k,v in value['names'].items()):
        raise ValueError('抓包名称列表无效')
    if not isinstance(value['details'],dict) or len(value['details'])>500:raise ValueError('抓包技能明细无效')
    for actor,detail in value['details'].items():
        if (not _identity(actor) or not {'damage','skills'} <= set(detail) <= {'damage','skills','counts','maxima'}
                or not number(detail['damage']) or not isinstance(detail['skills'],dict)):raise ValueError('抓包结算明细无效')
        if len(detail['skills'])>10000 or any(not str(k).isdigit() or int(k)<=0 or not number(v) for k,v in detail['skills'].items()):raise ValueError('抓包结算技能无效')
        if not _within_total(detail['skills'].values(), detail['damage']):raise ValueError('抓包技能超过总伤')
        for key in ('counts', 'maxima'):
            extra = detail.get(key, {})
            if not isinstance(extra, dict) or not set(extra) <= set(detail['skills']):raise ValueError('抓包技能统计无效')
            if any(not number(v) or (key == 'counts' and (type(v) is not int or v <= 0)) for v in extra.values()):raise ValueError('抓包技能统计数值无效')
    fields={'hits':{'source','target','amount','skill','calcpoint','time','transportRecordOrdinal'},
            'hp':{'timestamp','hp','hp_max','gap','state'},'qi':{'timestamp','qi','qi_max','gap','state','weak'},
            'buffs':{'kind','observedAt','targetActorHash','sourceActorHash','buffInstanceHash',
                     'buffNo','level','charge_level','durationSeconds','initial','sourceChanged',
                     'resourceChangeSuccess','resourceChangeRemaining','resourceChangeResource',
                     'rawStartTimestamp','transportRecordOrdinal'}}
    lower,upper=timestamp(value['start']),timestamp(value['end'])
    if not 0 < upper-lower <= MAX_BATTLE_MS:
        raise ValueError('抓包战斗时间无效')
    if not number(value.get('duration_seconds')) or not 0 < value['duration_seconds'] <= MAX_BATTLE_SECONDS:
        raise ValueError('抓包战斗时长无效')
    for key in ('hits', 'buffs', 'hp', 'qi'):
        if not isinstance(value.get(key), list) or len(value[key]) > MAX_ROWS:
            raise ValueError('抓包观测列表无效')
        for row in value[key]:
            if not isinstance(row,dict) or not set(row)<=fields[key]:raise ValueError('抓包事件包含不支持的字段')
            when=row.get('time' if key=='hits' else 'observedAt' if key=='buffs' else 'timestamp')
            if not lower<=timestamp(when)<upper:raise ValueError('抓包事件超出战斗边界')
            if key=='buffs':
                if row.get('kind') == 'invalidation':
                    if row.get('targetActorHash') is not None and not _identity(row['targetActorHash']):raise ValueError('抓包 Buff 失效身份无效')
                elif type(row.get('buffNo')) is not int or row['buffNo']<=0 or not _identity(row.get('targetActorHash')):raise ValueError('抓包 Buff 字段无效')
                from yylogs.rdps_history import validate_buff_evidence
                validate_buff_evidence(row)
    from yylogs.rdps_history import validate_attributes
    validate_attributes(value.get('attributes', []), lower, upper)
    from yylogs.rdps_gaps import validate_statistics
    validate_statistics(value.get('rd_statistics', []), lower, upper)
    if not isinstance(value.get('actors'), list) or len(value['actors']) > 500:
        raise ValueError('抓包人物列表无效')
    for row in value['actors']:
        if not _identity(row.get('id')) or any(row.get(k) is not None and not number(row[k]) for k in ('damage', 'healing')):
            raise ValueError('抓包人物统计无效')
    if len({row['id'] for row in value['actors']})!=len(value['actors']):raise ValueError('抓包人物统计重复')
    health = value.get('member_hp', [])
    members = {row['id'] for row in value['actors']}
    if not isinstance(health, list) or len(health) > MAX_ROWS:raise ValueError('抓包队员血量列表无效')
    for row in health:
        if (not isinstance(row, dict) or set(row) != {'actor','timestamp','observed_at','hp','hp_max','gap','state'}
                or row['actor'] not in members or type(row['gap']) is not bool or row['state'] is not True):
            raise ValueError('抓包队员血量字段无效')
        when = timestamp(row['timestamp'])
        if not lower <= when < upper or timestamp(row['observed_at']) > when:
            raise ValueError('抓包队员血量超出战斗边界')
        if row['gap']:
            if row['hp'] is not None or row['hp_max'] is not None:raise ValueError('血量缺口包含数值')
        elif (not number(row['hp']) or (row['hp_max'] is not None and
                (not number(row['hp_max']) or row['hp_max'] <= 0 or row['hp'] > row['hp_max']))):
            raise ValueError('抓包队员血量数值无效')
    if 'actions' in value:
        from yylogs.player_actions import validate_actions
        validate_actions(value['actions'], value['start'], value['end'], members)
    entities = value.get('entities', [])
    if not isinstance(entities, list) or len(entities) > 5000:
        raise ValueError('抓包实体列表无效')
    seen, counts = set(), {'hp': 0, 'qi': 0}
    for entity in entities:
        if (not isinstance(entity, dict) or set(entity) != {'id','name','template_no','hp','qi'}
                or not _identity(entity['id']) or entity['id'] in seen
                or entity['id'] in members
                or (entity['name'] is not None and (not isinstance(entity['name'], str) or len(entity['name']) > 160))
                or (entity['template_no'] is not None and (type(entity['template_no']) is not int or entity['template_no'] <= 0))):
            raise ValueError('抓包实体身份无效')
        seen.add(entity['id'])
        for key in ('hp', 'qi'):
            rows = entity[key]
            if not isinstance(rows, list):raise ValueError('抓包实体状态无效')
            counts[key] += len(rows)
            if counts[key] > MAX_ROWS:raise ValueError('抓包实体状态过多')
            for row in rows:
                if (not isinstance(row, dict) or not set(row) <= fields[key]
                        or not lower <= timestamp(row.get('timestamp')) < upper
                        or type(row.get('gap', False)) is not bool
                        or (not row.get('gap') and row.get('state') is not True)):
                    raise ValueError('抓包实体状态字段无效')
                if row.get('gap'):
                    if row.get(key) is not None or row.get(key+'_max') is not None:
                        raise ValueError('抓包实体状态缺口包含数值')
                elif (not number(row.get(key)) or (row.get(key+'_max') is not None and
                        (not number(row[key+'_max']) or row[key+'_max'] <= 0 or row[key] > row[key+'_max']))):
                    raise ValueError('抓包实体状态数值无效')
    return value


def _settlement_hit_statistics(settled, skill, observed):
    amount = settled['skills'][str(skill)]
    count = settled.get('counts', {}).get(str(skill))
    maximum = settled.get('maxima', {}).get(str(skill))
    result = {'hits': count, 'maximum': maximum,
              'average': amount / count if count else None,
              'hits_basis': 'settlement' if count is not None else None,
              'maximum_basis': 'settlement' if maximum is not None else None,
              'average_basis': 'settlement' if count else None}
    if not observed:
        return result
    tolerance = max(1e-6, amount * 1e-9)
    if (observed['damage'] > amount + tolerance
            or (count is not None and observed['hits'] > count)
            or (maximum is not None and observed['maximum'] > maximum + tolerance)):
        return result
    reconciled = (amount > 0 and abs(observed['damage'] - amount) <= tolerance
                  and (count is None or count == observed['hits']))
    result.update(observed_damage=observed['damage'], observed_hits=observed['hits'],
                  observed_maximum=observed['maximum'],
                  observed_statistics_reconciled=reconciled,
                  raw_skill_ids=sorted(observed['raw_skill_ids']),
                  calcpoint_ids=sorted(observed['calcpoint_ids']))
    if count is None:
        result.update(hits=observed['hits'], hits_basis='observed', average_basis='observed',
                      average=(amount if reconciled else observed['damage']) / observed['hits'])
    if maximum is None:
        result.update(maximum=observed['maximum'], maximum_basis='observed')
    return result


def load_packet_record(snapshot, folder, *, boss_target_id=None):
    from yylogs.damage_kinds import event_kind
    from yylogs.damage_effects import damage_label, display_identity
    from yylogs.game_names import apply_label
    from yylogs.member_status import MemberDamageBins, MemberHealingBins
    from yylogs.dungeon_identity import resolve_dungeon_identity
    from yylogs.final_count_ownership import point_owner, skill_root
    value = validate_snapshot(snapshot)
    from yylogs.packet_entities import entity_options, preferred_entity, select_entity
    options = entity_options(value)
    chosen = boss_target_id or preferred_entity({'record_folder': str(folder), 'source_sha256': value['source_sha256']})
    if not _identity(chosen) or chosen not in {row['id'] for row in options}:chosen = None
    value = select_entity(value, chosen)
    origin, duration = timestamp(value['start']), value['duration_seconds']
    elapsed = timestamp(value['end'])-origin
    bins = MemberDamageBins(origin, {'start_ms': 0, 'end_ms': elapsed}, maximum_ms=MAX_BATTLE_MS)
    heal_bins = MemberHealingBins(origin, {'start_ms': 0, 'end_ms': elapsed}, maximum_ms=MAX_BATTLE_MS)
    received_bins = MemberHealingBins(origin, {'start_ms': 0, 'end_ms': elapsed},
                                      entity_key='receiver_entity_id', maximum_ms=MAX_BATTLE_MS)
    party_ids = {row['id'] for row in value['actors']}
    received_by_recipient = defaultdict(int)
    totals, heals, skills, counts = defaultdict(float), defaultdict(float), defaultdict(lambda: defaultdict(float)), defaultdict(int)
    heal_skills,heal_counts=defaultdict(lambda:defaultdict(float)),defaultdict(int)
    maxima, heal_maxima = defaultdict(float), defaultdict(float)
    skill_labels, skill_ids, calcpoint_ids = {}, defaultdict(set), defaultdict(set)
    unknown = 0
    rd_damage_rows = []
    statistic_roots, observed_statistics = {}, defaultdict(dict)
    for actor, detail in value['details'].items():
        roots = defaultdict(list)
        for skill in detail['skills']:
            roots[skill_root(int(skill))].append(skill)
        statistic_roots[actor] = roots
    for row in value['hits']:
        if not _identity(row.get('source')) or not number(row.get('amount')):
            raise ValueError('抓包命中事件无效')
        kind, _ = event_kind({'calcpoint_id': row.get('calcpoint')})
        if kind not in ('damage', 'healing'):
            unknown += 1
            continue
        actor, amount, skill = row['source'], row['amount'], row.get('skill')
        if not 0 <= timestamp(row['time'])-origin < elapsed:
            raise ValueError('抓包命中事件超出战斗范围')
        (heals if kind == 'healing' else totals)[actor] += amount
        # Shared damage/healing curves accumulate exact decimal amounts.
        observation = {'fromer_id': actor, 'observed_unix_ms': timestamp(row['time']),
                       'receiver_entity_id':row.get('target')}
        label = damage_label(skill, row.get('calcpoint'))
        key, name = display_identity({'skill_id': skill,
            'calcpoint_id': row.get('calcpoint'), 'skill_name': label['name']})
        skill_labels[key] = label
        if kind == 'damage':
            bins.observe(observation, Decimal(str(amount)), skill_key=key, skill_name=name)
        else:
            heal_bins.observe(observation, Decimal(str(amount)))
            if actor in party_ids and row.get('target') in party_ids:
                if received_bins.observe(observation, Decimal(str(amount))):
                    received_by_recipient[row['target']] += 1
        group = (actor, key)
        if type(skill) is int:skill_ids[group].add(skill)
        if type(row.get('calcpoint')) is int:calcpoint_ids[group].add(row['calcpoint'])
        if kind == 'damage':
            skills[actor][key] += amount
            counts[group] += 1
            maxima[group] = max(maxima[group], amount)
            if actor in statistic_roots:
                # Calculation-point ownership precedes the triggering skill.
                owner = point_owner(row.get('calcpoint'))
                if owner is None and type(skill) is int and skill > 0:
                    owner = skill
                final_id = str(owner)
                if final_id not in value['details'][actor]['skills']:
                    matches = statistic_roots[actor].get(skill_root(owner), []) if owner else []
                    final_id = matches[0] if len(matches) == 1 else None
                if final_id is not None:
                    stat = observed_statistics[actor].setdefault(final_id, {
                        'damage': 0.0, 'hits': 0, 'maximum': 0.0,
                        'raw_skill_ids': set(), 'calcpoint_ids': set()})
                    stat['damage'] += amount
                    stat['hits'] += 1
                    stat['maximum'] = max(stat['maximum'], amount)
                    if type(skill) is int:stat['raw_skill_ids'].add(skill)
                    if type(row.get('calcpoint')) is int:stat['calcpoint_ids'].add(row['calcpoint'])
            if actor in party_ids:
                rd_damage_rows.append({'fromer_id': actor,
                    'observed_unix_ms': timestamp(row['time']), 'skill_id': skill,
                    'calcpoint_id': row.get('calcpoint'),
                    'transportRecordOrdinal': row.get('transportRecordOrdinal'),
                    'damage': amount,
                    'receiver_entity_id': row.get('target'),
                    'receiver_identity_verified': row.get('target') is not None})
        else:
            heal_skills[actor][key]+=amount;heal_counts[group]+=1
            heal_maxima[group] = max(heal_maxima[group], amount)
    members, details, healing_details = [], {}, {}
    counters = {row['id']: row for row in value['actors']}
    # The server party roster is authoritative; NPC damage notifications must
    # never become a new party member just because they have a source ID.
    identities = sorted(counters)
    if not identities:raise ValueError('未收到服务器队员统计，无法确认玩家与敌方实体')
    for index, actor in enumerate(identities, 1):
        counter, settled = counters.get(actor, {}), value['details'].get(actor)
        damage = settled['damage'] if settled else counter.get('damage')
        if damage is None:
            damage = totals[actor]
        healing = counter.get('healing')
        if healing is None:
            healing = heals[actor]
        member = {'id': actor, 'name': value['names'].get(actor) or f'人物 {index}',
                  'level': None, 'local': False, 'damage': damage, 'dps': damage/duration,
                  'healing': healing, 'hps': healing/duration, 'taken': None, 'rdps': None}
        members.append(member)
        source = settled['skills'] if settled else skills[actor]
        rows = []
        if _within_total(source.values(), damage):
            for skill, amount in sorted(source.items(), key=lambda pair: pair[1], reverse=True):
                label = None if settled else skill_labels[skill]
                skill_id = int(skill) if settled else label['resolved_skill_id']
                hit_count = settled.get('counts', {}).get(str(skill)) if settled else counts[(actor, skill)]
                row = {'skill_id': skill_id, 'damage': amount, 'name': None,
                       'uses': None, 'hits': hit_count,
                       'average': amount / hit_count if hit_count else None,
                       'critical_percent': None, 'insight_percent': None,
                       'graze_percent': None, 'maximum': settled.get('maxima', {}).get(str(skill)) if settled else maxima[(actor, skill)]}
                if settled:
                    row.update(_settlement_hit_statistics(settled, skill, observed_statistics[actor].get(str(skill))))
                    apply_label(row, 'skill', skill_id)
                else:
                    row.update(label, raw_skill_ids=sorted(skill_ids[(actor, skill)]),
                               calcpoint_ids=sorted(calcpoint_ids[(actor, skill)]), display_key=list(skill))
                rows.append(row)
        details[actor] = {'status': 'verified', 'rows': rows,
                          'basis': 'packet_settlement' if settled else 'classified_packet_hits'}
        healing_rows=[]
        if sum(heal_skills[actor].values())<=healing:
            for skill,amount in sorted(heal_skills[actor].items(),key=lambda pair:pair[1],reverse=True):
                label=skill_labels[skill]
                row={'skill_id':label['resolved_skill_id'],'damage':amount,'name':None,'hits':heal_counts[(actor,skill)],
                     'uses':None,'average':amount/heal_counts[(actor,skill)],
                     'critical_percent':None,'insight_percent':None,
                     'graze_percent':None,'maximum':heal_maxima[(actor,skill)]}
                row.update(label,raw_skill_ids=sorted(skill_ids[(actor,skill)]),
                           calcpoint_ids=sorted(calcpoint_ids[(actor,skill)]),display_key=list(skill))
                healing_rows.append(row)
        healing_details[actor]={'status':'verified','rows':healing_rows,'basis':'classified_packet_healing'}
    total = sum(row['damage'] for row in members)
    for row in members:
        row['damage_share'] = row['damage']/total if total else 0
    folder = Path(folder)
    encounter = resolve_dungeon_identity(value.get('dungeon_sid'))
    default_name = encounter['label'] + ' ' + datetime.fromisoformat(value['start'].replace('Z', '+00:00')).strftime('%m-%d %H:%M')
    meta = read_json(folder / 'record_meta.json') if (folder / 'record_meta.json').is_file() else {}
    timeline = {'schema': 'yylogs.packet-timeline/1', 'origin': value['start'], 'end': value['end'],
                'duration_ms': elapsed, 'scene': {'dungeon_sid': value.get('dungeon_sid')},
                'own_entity_id': None, 'tracks': [], 'source_sha256': {},
                'boss_curves': {'hp': value['hp'], 'qi': [{**row,'scale':'absolute'} for row in value['qi']]}, 'gaps': [],
                'actor_names': value['names'], 'packet_snapshot': value}
    from yylogs.packet_observations import buff_tracks
    timeline['tracks']=buff_tracks(value, include_members=True)
    timeline['snapshot_filter_windows']=[{'start_ms':0,'end_ms':elapsed}]
    from yylogs.rdps_estimate import estimate_summary
    rd_players = {actor: {'id': actor, 'name': value['names'].get(actor),
                          'local': False, 'party_membership_verified': True}
                  for actor in party_ids}
    rd_window = {'target_id': (value['boss_ids'][0] if value['boss_ids'] else None),
                 'target_ids': list(value['boss_ids']), 'start_ms': 0, 'end_ms': elapsed}
    rd_estimate = estimate_summary(timeline, rd_window, rd_players, members,
                                   rd_damage_rows, Decimal(str(duration)))
    observed_damage = sum((Decimal(str(row['damage'])) for row in rd_damage_rows), Decimal(0))
    member_damage = sum((Decimal(str(member['damage'])) for member in members), Decimal(0))
    rd_estimate['unlocated_damage'] = max(Decimal(0), member_damage - observed_damage)
    rd_estimate['unestimated_damage'] = max(Decimal(0), rd_estimate['unlocated_damage'] - rd_estimate['gap_reconstructed_damage'])
    from yylogs.skill_styles import annotate_members
    usage = defaultdict(set)
    for event in value['hits']:
        if event.get('amount', 0) > 0:
            usage[event['source']].add(event.get('skill'))
    for event in value.get('actions', []):
        if event.get('actionKind') == 'cast' and not event.get('initial'):
            usage[event['sourceActorHash']].add(event.get('rawSkillId'))
    annotate_members(members, details, usage)
    healing_summary = {'total_healing': sum(row['healing'] for row in members), 'members': members,
                       'timeline': heal_bins.finish(members),
                       'received_timeline': received_bins.finish(members),
                       'received_record_count': sum(received_by_recipient.values()),
                       'received_by_recipient': dict(received_by_recipient)}
    return {'id': folder.name, 'record_kind': 'saved_packet_capture', 'capture_backend': 'packet',
            'account': '抓包记录', 'display_name': meta.get('name', default_name),
            'display_name_is_custom': bool(meta.get('name')), 'timestamp': value['start'],
            'dungeon_sid': value.get('dungeon_sid'), 'duration': duration,
            'duration_label': '服务器计时' if value['duration_basis'] == 'server_battle_timer' else '接收观测用时',
            'duration_kind': value['duration_basis'], 'team_damage': total, 'team_dps': total/duration,
            'team_healing': healing_summary['total_healing'], 'team_taken': None,
            'rdps': None, 'rdps_supported': False,
            'rdps_estimate_supported': rd_estimate['supported'],
            'rd_estimate': rd_estimate,
            'boss_entity_options': options, 'boss_entity_id': next(iter(value['boss_ids']), None),
            'boss_entity_mode': 'manual' if chosen else 'auto',
            'members': members, 'skill_details': details, 'buffs': [], 'buff_timeline': timeline,
            'healing_skill_details':healing_details,
            'capture_summary': {'member_status': bins.finish(members), 'record_count': len(value['hits'])},
            'healing_summary': healing_summary, 'event_count': len(value['hits']),
            'verified': False, 'capture_active': False, 'record_folder': str(folder),
            'source_path': str(folder / 'packet_snapshot.json'), 'source_sha256': value['source_sha256'],
            'checks_passed': 1, 'checks_total': 1, 'encounter': encounter,
            'encounter_label': encounter['label'], 'dungeon_name': encounter['dungeon_name'],
            'boss_name': encounter['boss_name'], 'boss_order': encounter['boss_order'],
            'packet_limitations': {'unknown_hit_kinds': unknown, 'partial': value['partial'],
                'own_actor_unconfirmed': True, 'rd_attribution_unverified': True,
                'qi_maximum_unavailable': True,
                'member_health_unavailable': not any(not row['gap'] for row in value.get('member_hp', []))}}


def import_packet_sessions(root, store, *, exclude_battles=()):
    from yylogs.battle_records import _save_payload
    from yylogs.packet_battle_logs import linked_records, is_independent
    root = Path(root).resolve()
    records = list(dict.fromkeys(linked_records(root, store)))
    for path in sorted(root.rglob('battle.json')):
        if path.parent.parent.name != 'battles' or not path.resolve().is_relative_to(root):
            continue
        if path.resolve() in exclude_battles:continue
        canonical = path.with_name('saved-snapshot.json')
        if canonical.is_file():
            if canonical.is_symlink() or canonical.is_junction() or canonical.stat().st_size > MAX_LEDGER:
                raise ValueError('已保存战斗快照无效')
            raw = canonical.read_bytes()
            snapshot = validate_snapshot(json.loads(raw))
            battle = read_json(path)
            if (snapshot['session'],snapshot['battle_id'],snapshot['start']) != (
                    battle['source']['sessionId'],battle['id'],battle['clock']['startObservedAt']):
                raise ValueError('已保存战斗快照与原场次不匹配')
        else:
            snapshot = snapshot_from_battle(path)
            raw = json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode('utf8')
        payload = {'packet_snapshot.json':raw}
        logs = {'path':path,'battle_bytes':path.read_bytes(),'identities':read_json(path).get('identities',[])} if is_independent(path) else None
        report = _save_payload(payload, store, packet_logs=logs)
        if report['id'] not in records:
            records.append(report['id'])
    return records


def _packet_original_ready(folder):
    try:
        status = read_json(Path(folder) / 'packet_capture_result.json')
        return (isinstance(status, dict) and status.get('cleanup_verified') is True
                and status.get('schema') == 'yylogs.packet-result/1'
                and status.get('backend') == 'packet')
    except (OSError, ValueError, TypeError):
        return False


@lru_cache(maxsize=64)
def _matching_battle(store, digest, session, battle_id):
    """Find only a stopped, local original with the exact saved battle hash."""
    root = Path(store).resolve().parent.parent / 'manual_captures'
    if not root.is_dir() or not isinstance(battle_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', battle_id):
        return None
    try:
        folders = sorted(root.glob('*_packet_*'), reverse=True)[:200]
        for folder in folders:
            if (folder.is_symlink() or folder.is_junction()
                    or folder.resolve().parent != root):
                continue
            if not _packet_original_ready(folder):
                continue
            for index, path in enumerate((folder / 'packet/sessions').glob(f'*/capture/battles/{battle_id}/battle.json')):
                if index >= 256:
                    break
                if (path.parent.name != battle_id or path.is_symlink()
                        or not path.resolve().is_relative_to(folder)
                        or path.stat().st_size > MAX_LINE):
                    continue
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != digest:
                    continue
                value = read_json(path)
                if (value.get('source') or {}).get('sessionId') == session and value.get('id') == battle_id:
                    return str(path)
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return None


def recover_packet_actions(snapshot, folder, cancelled=lambda: False):
    """Lazily read old action ledgers only after exact battle/session/hash proof."""
    from yylogs.packet_action_archive import archived_actions, archive_actions
    if cancelled():raise InterruptedError()
    snapshot = archived_actions(snapshot, folder)
    if 'actions' in snapshot or not folder or not snapshot.get('source_sha256'):
        return snapshot
    path = _matching_battle(str(Path(folder).parent), snapshot['source_sha256'],
                            snapshot.get('session'), snapshot.get('battle_id'))
    if not path:
        return snapshot
    original = Path(path)
    root = Path(folder).resolve().parent.parent.parent / 'manual_captures'
    source = original.parents[6]
    if (source.is_symlink() or source.is_junction() or source.resolve().parent != root
            or original.is_symlink() or not original.resolve().is_relative_to(source)
            or not _packet_original_ready(source)):
        return snapshot
    if hashlib.sha256(original.read_bytes()).hexdigest() != snapshot['source_sha256']:
        return snapshot
    battle = read_json(original)
    clock = battle.get('clock') or {}
    if (clock.get('startObservedAt') != snapshot['start'] or clock.get('endObservedAt') != snapshot['end']
            or battle.get('id') != snapshot['battle_id']
            or (battle.get('source') or {}).get('sessionId') != snapshot['session']):
        return snapshot
    capture = original.parents[2]
    if not any((capture / name).is_file() for name in ('action-events.jsonl', 'action-events.jsonl.ylz')):
        return snapshot
    from yylogs.player_actions import battle_actions
    actions = battle_actions((row for row in ledger(capture, 'action-events')
        if (row.get('source') or {}).get('sessionId') in (None, snapshot['session'])),
        snapshot['start'], snapshot['end'], {row['id'] for row in snapshot['actors']}, cancelled)
    if cancelled():raise InterruptedError()
    try:
        archive_actions(snapshot, folder, actions)
    except (OSError, ValueError):
        # A read-only record must still display the verified observations.
        # Raw deletion separately requires the durable copy to have succeeded.
        pass
    return {**snapshot, 'actions': actions}


def _snapshot_from_checkpoint(original, sealed, snapshot, allowed):
    capture = original.parents[2]
    try:
        if (sealed.is_symlink() or not sealed.resolve().is_relative_to(capture)
                or sealed.stat().st_size > 68 * 1024 * 1024):
            return None
        value = json.loads(sealed.read_text('utf8'))
        if (value.get('schema') != 'yylogs.packet-checkpoint/1' or value.get('error')
                or value.get('session') != snapshot['session']
                or value.get('token') != sealed.stem):
            return None
        files, names, battles = value.get('files'), value.get('identities', []), value.get('battles')
        if (not isinstance(files, dict) or set(files) - allowed
                or any(type(n) is not int or not 0 <= n <= MAX_LEDGER for n in files.values())
                or not isinstance(names, list) or len(names) > 5000
                or not isinstance(battles, list) or len(battles) > 32):
            return None
        for row in battles:
            if row.get('id') != snapshot['battle_id'] or not isinstance(row.get('text'), str):
                continue
            raw = row['text'].encode('utf8')
            if hashlib.sha256(raw).hexdigest() == snapshot['source_sha256']:
                return snapshot_from_battle(original, battle_bytes=raw,
                                            ledger_limits=files, identity_names=names)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return None


def _checkpoint_observations(snapshot, folder):
    """An ended round can be read while its manually started session continues.

    Use only the decoder-sealed prefixes and exact archived battle bytes. An
    active capture's current ledger length is not a safe read boundary.
    """
    LEDGERS = ('damage-events', 'buff-events', 'action-events', 'encounter-events',
               'timer-events', 'settlement-details', 'statistics')
    root = Path(folder).resolve().parent.parent.parent / 'manual_captures'
    battle_id = snapshot.get('battle_id')
    if not isinstance(battle_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', battle_id):
        return None
    allowed = {name + suffix for name in LEDGERS for suffix in ('.jsonl', '.jsonl.ylz')}
    for source in sorted(root.glob('*_packet_*'), reverse=True)[:200]:
        if source.is_symlink() or source.is_junction() or source.resolve().parent != root:
            continue
        for original in list((source / 'packet/sessions').glob(f'*/capture/battles/{battle_id}/battle.json'))[:256]:
            try:
                if (original.is_symlink() or not original.resolve().is_relative_to(source)
                        or original.stat().st_size > MAX_LINE):
                    continue
                # The mutable archive may receive a late final counter. The
                # checkpoint's exact hashed bytes below remain the authority.
                battle = read_json(original)
                clock = battle.get('clock') or {}
                if (battle.get('id') != battle_id
                        or (battle.get('source') or {}).get('sessionId') != snapshot['session']
                        or clock.get('startObservedAt') != snapshot['start']
                        or clock.get('endObservedAt') != snapshot['end']):
                    continue
                capture = original.parents[2]
                for sealed in sorted((capture / 'checkpoints').glob('*.json'), reverse=True)[:256]:
                    recovered = _snapshot_from_checkpoint(original, sealed, snapshot, allowed)
                    if recovered is not None:
                        return recovered
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                continue
    return None


def recover_packet_observations(snapshot, folder):
    """Repair the derived view without changing the verified saved payload."""
    if snapshot.get('observation_revision') == OBSERVATION_REVISION or not snapshot.get('source_sha256'):
        return snapshot
    unchanged = ('session', 'battle_id', 'start', 'end', 'duration_seconds',
                 'duration_basis', 'names', 'actors', 'hits', 'buffs')
    recovered = _checkpoint_observations(snapshot, folder)
    if recovered and recovered['boss_ids'] and all(recovered[key] == snapshot[key] for key in unchanged):
        return {**snapshot, **{key: recovered[key] for key in ('boss_ids', 'hp', 'qi', 'entities')},
                'rd_statistics': snapshot.get('rd_statistics') or recovered.get('rd_statistics', []),
                'observation_revision': OBSERVATION_REVISION}
    path = _matching_battle(str(Path(folder).parent), snapshot['source_sha256'],
                            snapshot.get('session'), snapshot.get('battle_id'))
    if not path:
        return snapshot
    try:
        original = Path(path)
        root = Path(folder).resolve().parent.parent.parent / 'manual_captures'
        source = original.parents[6]
        if (source.is_symlink() or source.is_junction() or source.resolve().parent != root
                or original.is_symlink() or not original.resolve().is_relative_to(source)
                or not _packet_original_ready(source)):
            return snapshot
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != snapshot['source_sha256']:
            return snapshot
        recovered = snapshot_from_battle(path)
        if recovered['boss_ids'] and all(recovered[key] == snapshot[key] for key in unchanged):
            return recovered
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return snapshot


@lru_cache(maxsize=16)
def _recovered_settlement(path, digest, signatures):
    original = Path(path)
    if hashlib.sha256(original.read_bytes()).hexdigest() != digest:
        return None
    value = snapshot_from_battle(original)
    keys = ('session', 'battle_id', 'start', 'end', 'duration_seconds', 'duration_basis', 'actors', 'details')
    return {key: value[key] for key in keys}


_SETTLEMENT_RECOVERY_CACHE = {}


def recover_packet_settlement(snapshot, folder):
    """Recover proven skill detail in memory; never rewrite a saved record."""
    if snapshot.get('details') or not folder or not snapshot.get('source_sha256'):
        return snapshot
    cache_key = (str(Path(folder).resolve()), snapshot['source_sha256'])
    try:
        recovered = _SETTLEMENT_RECOVERY_CACHE.get(cache_key)
        if recovered is None:
            # The original archive can advance after an individual round was
            # saved. A sealed checkpoint preserves its exact hashed bytes and
            # ledger boundaries even while the same capture is still running.
            recovered = _checkpoint_observations(snapshot, folder)
        if recovered is None:
            path = _matching_battle(str(Path(folder).parent), snapshot['source_sha256'],
                                    snapshot.get('session'), snapshot.get('battle_id'))
            if not path:
                return snapshot
            original = Path(path)
            source = original.parents[6]
            root = Path(folder).resolve().parent.parent.parent / 'manual_captures'
            if (source.is_symlink() or source.is_junction() or source.resolve().parent != root
                    or not original.resolve().is_relative_to(source) or not _packet_original_ready(source)):
                return snapshot
            capture = original.parent.parent.parent
            signatures = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size)
                               for name in ('settlement-details', 'statistics', 'timer-events')
                               for suffix in ('.jsonl', '.jsonl.ylz')
                               if (p := capture / (name + suffix)).exists())
            recovered = _recovered_settlement(path, snapshot['source_sha256'], signatures)
        stable = ('session', 'battle_id', 'start', 'end', 'duration_seconds', 'duration_basis', 'actors')
        if not recovered or any(recovered[k] != snapshot[k] for k in stable):
            return snapshot
        details = recovered.get('details') or {}
        counters = {a['id']: a.get('damage') for a in snapshot['actors']}
        # Recovery adds skill attribution only: preserve every saved total.
        details = {actor: detail for actor, detail in details.items()
                   if counters.get(actor) == detail['damage']}
        if details:
            if len(_SETTLEMENT_RECOVERY_CACHE) >= 16:
                _SETTLEMENT_RECOVERY_CACHE.pop(next(iter(_SETTLEMENT_RECOVERY_CACHE)))
            _SETTLEMENT_RECOVERY_CACHE[cache_key] = {**{key: recovered[key] for key in stable}, 'details': details}
            return {**snapshot, 'details': details}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        pass
    return snapshot
