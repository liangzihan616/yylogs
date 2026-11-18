"""Record-scoped one-second damage bins and validated optional avatar HP."""
from decimal import Decimal
import json
import math
from pathlib import Path

from yylogs.encounter_timeline import unix_ms

ZERO = Decimal(0)
SCOPE_KEYS = ("session_id", "game_pid", "process_creation_ticks", "scene_id", "dungeon_sid", "own_entity_id")
BLOOD_SACRIFICE_BUFF_IDS = frozenset({1002093,1002097,1002194,1002263,1002387,
                                      1161059,1161080,1161091,1870156})
MAX_CONTINUOUS_FRAME_SECONDS = 2.5


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


class MemberDamageBins:
    """Called only after the existing damage/source/target/time/healing filters."""
    def __init__(self, origin, window, *, maximum_ms=605000):
        self.origin = origin
        self.window = window
        self.bins = {}
        self.skill_bins = {}
        self.duration = window["end_ms"] - window["start_ms"] if window else 0
        if self.duration>maximum_ms:raise ValueError("队员状态区间超出记录范围")

    def observe(self, row, damage, *, skill_key=None, skill_name=None):
        if not self.window or not finite(row.get("observed_unix_ms")):
            return False
        elapsed = row["observed_unix_ms"] - self.origin - self.window["start_ms"]
        if not 0 <= elapsed <= self.duration:
            return False
        count = math.ceil(self.duration / 1000)
        index = min(int(elapsed // 1000), count - 1)
        bins = self.bins.setdefault(row["fromer_id"], {})
        bins[index] = bins.get(index, ZERO) + damage
        if skill_key is not None:
            key = tuple(skill_key)
            cells = self.skill_bins.setdefault(row["fromer_id"], {}).setdefault(index, {})
            item = cells.setdefault(key, {"name": skill_name or "未命名伤害", "damage": ZERO})
            item["damage"] += damage
        return True

    def finish(self, members):
        if not self.window:
            return {"available": False, "duration_ms": 0, "members": {}}
        count = math.ceil(self.duration / 1000)
        widths = [min(1000, self.duration - i * 1000) for i in range(count)]
        def skill_rows(entity, index):
            values = self.skill_bins.get(entity, {}).get(index, {})
            return [{"key": list(key), "name": item["name"], "damage": item["damage"]}
                    for key, item in sorted(values.items(), key=lambda pair: pair[1]["damage"], reverse=True)]
        return {"available": True, "origin_unix_ms": self.origin + self.window["start_ms"],
                "duration_ms": self.duration, "bin_ms": 1000, "bin_widths_ms": widths,
                "basis": "included_damage_candidates_not_server_instantaneous_dps",
                "members": {m["id"]: {"name":m.get("name"),"local":m.get("local",False),
                                       "damage": [self.bins.get(m["id"], {}).get(i, ZERO)
                                               for i in range(count)],
                                       "skills": [skill_rows(m["id"], i) for i in range(count)]}
                             for m in members}}


class MemberHealingBins(MemberDamageBins):
    """Accepted healing events, binned by source or recipient identity."""
    def __init__(self,origin,window,*,entity_key="fromer_id",maximum_ms=605000):
        super().__init__(origin,window,maximum_ms=maximum_ms)
        self.entity_key=entity_key
    def observe(self,row,healing):
        if self.entity_key!="fromer_id":
            row={**row,"fromer_id":row.get(self.entity_key)}
        return super().observe(row,healing)
    def finish(self,members):
        result=super().finish(members)
        result['basis']=('accepted_incoming_healing_candidates_not_effective_healing'
                         if self.entity_key!='fromer_id' else
                         'accepted_outgoing_healing_candidates_not_effective_healing')
        for member in result['members'].values():
            member['healing']=member.pop('damage')
            member.pop('skills',None)
        return result


def blood_sacrifice_segments(buff_timeline,entity,origin_ms,duration):
    """Clip observed self-Buff '血祭' spans to the member chart clock.

    The self-buff timeline is recorded from capture start; member curves use
    the encounter origin. These are observed display spans, not proven buff
    uptime or proof that the damage bonus has been earned.
    """
    if not isinstance(buff_timeline,dict) or not finite(duration) or duration<=0:
        return []
    own=buff_timeline.get("own_entity_id")
    legacy_self = isinstance(own,str) and bool(own) and entity==own
    try:
        buff_origin=unix_ms(buff_timeline.get("origin"))
    except (ValueError,TypeError,OverflowError):
        buff_origin=origin_ms
    result=[]
    for track in buff_timeline.get("tracks",[]):
        target=track.get("target_entity_id")
        if track.get('channel')=='packet_self':
            # Packet Buffs retain the observed recipient, even when the local
            # character is unconfirmed. Match the selected member explicitly.
            if not isinstance(entity,str) or not entity or target!=entity:continue
        elif track.get('channel') in ('replicated','gui_display_only'):
            if not legacy_self:continue
        else:continue
        if target is not None and target!=entity:
            continue
        name=track.get("name")
        if not name and track.get("buff_no") is not None:
            try:
                from yylogs.game_names import metadata
                name=metadata("buff",track["buff_no"]).get("name")
            except (KeyError,TypeError,ValueError):
                name=None
        if not is_blood_sacrifice_buff(track.get("buff_no"),name):
            continue
        for segment in track.get("segments",[]):
            start,end=segment.get("start_ms"),segment.get("end_ms")
            if not finite(start) or not finite(end) or end<=start:
                continue
            left=max(0,buff_origin+start-origin_ms)
            right=min(duration,buff_origin+end-origin_ms)
            if right<=0 or left>=duration or right<=left:
                continue
            buff_no=track.get("buff_no")
            result.append({**segment,"start_ms":left,"end_ms":right,
                "source_start_ms":start,"source_end_ms":end,
                "buff_no":buff_no,"buff_nos":[buff_no] if buff_no is not None else [],
                "name":str(name).strip()})
    result.sort(key=lambda row:(row["start_ms"],row["end_ms"]))
    # The same self Buff can be present in both display and replicated
    # channels. Merge overlapping/adjacent observations into one visual span.
    merged=[]
    for row in result:
        if merged and row["start_ms"]<merged[-1]["end_ms"]:
            merged[-1]["end_ms"]=max(merged[-1]["end_ms"],row["end_ms"])
            merged[-1]["source_end_ms"]=max(merged[-1].get("source_end_ms",0),
                                            row.get("source_end_ms",0))
            merged[-1]["buff_nos"]=sorted(set(merged[-1].get("buff_nos",())) |
                                           set(row.get("buff_nos",())))
        else:
            merged.append(dict(row))
    return merged


def is_blood_sacrifice_buff(buff_no,name):
    """Recognize ordinary, leveled and upgraded Blood Sacrifice Buff IDs."""
    if type(buff_no) is int and buff_no in BLOOD_SACRIFICE_BUFF_IDS:
        return True
    text="".join(str(name or "").split())
    return text=="血祭" or text.startswith("血祭·") or text.startswith("血祭红妆")


def load_member_health(folder, *, record=None, cancelled=lambda:False):
    """All journal rows must belong to this bundle's exact capture identity."""
    if record and record.get('capture_backend') == 'packet':
        snapshot = (record.get('buff_timeline') or {}).get('packet_snapshot') or {}
        members = {row['id'] for row in record.get('members', [])}
        rows = []
        for index, row in enumerate(snapshot.get('member_hp', [])):
            if index % 256 == 0 and cancelled():raise InterruptedError()
            if row['actor'] not in members:continue
            sample = {'entity_id': row['actor'], 'unix_ms': unix_ms(row['timestamp']),
                      'observed_unix_ms': unix_ms(row['observed_at']),
                      'gap': row['gap'], 'state': True}
            if not row['gap']:
                maximum = row['hp_max']
                sample.update(hp=row['hp'], hp_max=maximum,
                              ratio=row['hp']/maximum if maximum else None)
            rows.append(sample)
        available = any(not row['gap'] for row in rows)
        return {'available': available, 'samples': rows, 'basis': 'packet_property_state',
                'reason': '' if available else '本场未保存队员血量数据'}
    if not folder:
        return {"available": False, "samples": [], "reason": "本场未保存队员血量采样"}
    folder = Path(folder)
    path, scope_path = folder / "member_health.jsonl", folder / "joint_capture_scope.json"
    if not path.is_file() or not scope_path.is_file():
        return {"available": False, "samples": [], "reason": "本场未保存队员血量采样"}
    try:
        if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024 or scope_path.stat().st_size > 32768:
            raise ValueError("队员血量日志超出读取范围")
        scope = json.loads(scope_path.read_text("utf-8-sig"))
        if any(k not in scope for k in SCOPE_KEYS):
            raise ValueError("缺少队员血量采集身份")
        rows = []
        with path.open(encoding="utf-8-sig") as stream:
            for sequence, line in enumerate(stream, 1):
                if cancelled():raise InterruptedError("血量读取已取消")
                if sequence > 1450 or len(line) > 65536:
                    raise ValueError("队员血量日志超出读取范围")
                row = json.loads(line)
                if (row.get("schema") != "yylogs.member-health/1" or row.get("sequence") != sequence
                        or row.get("game_function_called") is not False
                        or any(type(row.get(k)) is not type(scope[k]) or row.get(k) != scope[k] for k in SCOPE_KEYS)):
                    raise ValueError("队员血量日志身份或序列不匹配")
                when = unix_ms(row["timestamp"])
                if not finite(when):raise ValueError("队员血量时间无效")
                actors = row.get("actors")
                if not isinstance(actors, list) or len(actors) > 10:
                    raise ValueError("队员血量条目无效")
                for actor in actors:
                    entity = actor.get("entity_id")
                    if not isinstance(entity, str) or not entity:
                        raise ValueError("队员血量缺少实体")
                    value = {"entity_id": entity, "unix_ms": when,
                             "journal_sequence": sequence, "gap": True,
                             "reason": str(actor.get("reason", "读取缺口"))}
                    if actor.get("status") == "observed":
                        actor_when=unix_ms(actor['observed_at']) if actor.get('observed_at') else when
                        if not finite(actor_when) or actor_when>when:
                            raise ValueError('队员血量采样时刻无效')
                        if row.get('read_started_at') and actor_when<unix_ms(row['read_started_at']):
                            raise ValueError('队员血量采样时刻超出读取区间')
                        value['unix_ms']=actor_when
                        hp, maximum = actor.get("hp"), actor.get("hp_max")
                        if (actor.get("identity_verified") is not True
                                or not isinstance(actor.get("server_entity_id"), str)
                                or not finite(hp) or not finite(maximum) or not 0 <= hp <= maximum or maximum <= 0):
                            raise ValueError("队员血量数值或身份无效")
                        value.update(gap=False, hp=hp, hp_max=maximum, ratio=hp / maximum)
                    elif actor.get("status")!="gap":
                        raise ValueError("队员血量状态无效")
                    rows.append(value)
        available=any(not row["gap"] for row in rows)
        reason="" if available else "本场队员血量均为读取缺口"
        status_path=folder/"member_health_status.json"
        if not available and status_path.is_file() and not status_path.is_symlink() and status_path.stat().st_size<=32768:
            try:
                status=json.loads(status_path.read_text("utf-8-sig"))
                if (isinstance(status,dict) and status.get("schema")=="yylogs.member-health-status/1"
                        and all(type(status.get(k)) is type(scope[k]) and status.get(k)==scope[k] for k in SCOPE_KEYS)
                        and isinstance(status.get("reason"),str) and status["reason"]):
                    reason+="："+status["reason"][:240]
            except (OSError,ValueError,TypeError):pass
        return {"available":available,"samples":rows,"reason":reason}
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return {"available": False, "samples": [], "reason": "队员血量日志不可用：" + str(exc)}


def damage_curve(timeline, entity, bin_ms=1000):
    return _rate_curve(timeline,entity,bin_ms,'damage','dps')


def healing_curve(timeline, entity, bin_ms=1000):
    return _rate_curve(timeline,entity,bin_ms,'healing','hps')


def _rate_curve(timeline, entity, bin_ms, amount_key, rate_key):
    data = timeline.get("members", {}).get(entity)
    if not timeline.get("available") or not data:
        return []
    factor = max(1, int(bin_ms // 1000))
    widths = timeline["bin_widths_ms"]
    rows = []
    for index in range(0, len(widths), factor):
        width = sum(widths[index:index + factor])
        amount = sum(data[amount_key][index:index + factor], ZERO)
        skill_totals = {}
        for second in data.get("skills",[])[index:index + factor]:
            for item in second:
                key = tuple(item.get("key") or ())
                record = skill_totals.setdefault(key, {"name": item.get("name") or "未命名伤害", "damage": ZERO})
                record["damage"] += item.get("damage", ZERO)
        skills = []
        if amount_key == "damage" and width > 0:
            for key, item in sorted(skill_totals.items(), key=lambda pair: pair[1]["damage"], reverse=True):
                skill_damage = item["damage"]
                skills.append({"name": item["name"], "damage": skill_damage,
                    "dps": float(skill_damage / (Decimal(str(width)) / 1000)),
                    "share": float(skill_damage / amount) if amount else None})
        start = index * 1000
        rows.append({"start_ms": start, "end_ms": start + width,
                     "elapsed_ms": start + width / 2, amount_key: amount,
                     rate_key: float(amount / (Decimal(str(width)) / 1000)), "skills": skills})
    return rows


def health_curve(health, entity, origin, duration):
    rows = sorted((r for r in health.get("samples", []) if r["entity_id"] == entity), key=lambda r: r["unix_ms"])
    if health.get('basis') == 'packet_property_state':
        # HP is a replicated property: keep its value until a real update or
        # invalidation. These spans are state lifetimes, not invented samples.
        rows = list({row['unix_ms']: row for row in rows}.values())
        result = []
        for index, row in enumerate(rows):
            elapsed = row['unix_ms'] - origin
            if not 0 <= elapsed <= duration:continue
            end = min(duration, rows[index+1]['unix_ms']-origin if index+1<len(rows) else duration)
            result.append({**row, 'elapsed_ms': elapsed, 'valid_until_ms': end, 'continuous': True})
        return result
    result = []
    previous_sequence = None
    pending = None
    for row in rows:
        elapsed = row["unix_ms"] - origin
        if not 0 <= elapsed <= duration:
            continue
        sequence = row.get("journal_sequence")
        if pending is not None:
            pending_sequence = pending.get("journal_sequence")
            pending["continuous"] = (type(pending_sequence) is not int or type(sequence) is not int
                                      or sequence <= pending_sequence + 1)
            result.append(pending)
        pending = {**row, "elapsed_ms": elapsed, "continuous": True}
        previous_sequence = sequence if type(sequence) is int else None
    if pending is not None:
        pending["continuous"] = previous_sequence is not None
        result.append(pending)
    return result


def health_observation_summary(rows,start,end):
    """Union of local observation edges, never extrapolated or filled HP."""
    if any('valid_until_ms' in row for row in rows):
        valid = health_in_range(rows, start, end)
        return {'sample_count': len(valid),
                'gap_count': sum(row.get('gap', False) for row in rows if start<=row['elapsed_ms']<=end),
                'spacing_gap_count': 0, 'max_interval_ms': 0, 'line_break_count': 0,
                'observed_ms': sum(max(0, min(end,row['valid_until_ms'])-max(start,row['elapsed_ms'])) for row in valid),
                'interval_ms': max(0,end-start),
                'first_ms': valid[0]['elapsed_ms'] if valid else None,
                'last_ms': valid[-1]['valid_until_ms'] if valid else None}
    observed=0.0
    intervals=[]
    spacing_gaps=0
    line_breaks=0
    previous=None
    previous_valid=None
    for row in rows:
        if row.get("gap"):
            previous=None
            continue
        if not row.get("continuous", True):
            line_breaks+=1
            previous=None
        when=row["elapsed_ms"]
        if previous_valid is not None and min(end,when)>max(start,previous_valid):
            intervals.append(when-previous_valid)
        previous_valid=when
        if previous is not None and min(end,when)>max(start,previous):
            delta=when-previous
            if row.get("continuous", True) and delta<=MAX_CONTINUOUS_FRAME_SECONDS*1000:
                observed+=max(0,min(end,when)-max(start,previous))
            elif row.get("continuous", True):spacing_gaps+=1
        previous=when
    selected=[row for row in rows if start<=row["elapsed_ms"]<=end]
    valid=[row for row in selected if not row.get("gap")]
    return {"sample_count":len(valid),"gap_count":sum(bool(row.get("gap")) for row in selected),
            "spacing_gap_count":spacing_gaps,"max_interval_ms":max(intervals,default=0),
            "line_break_count":line_breaks,
            "observed_ms":observed,"interval_ms":max(0,end-start),
            "first_ms":valid[0]["elapsed_ms"] if valid else None,
            "last_ms":valid[-1]["elapsed_ms"] if valid else None}


def health_in_range(rows, start, end):
    return [row for row in rows if not row.get('gap') and
            (start <= row['elapsed_ms'] <= end or
             row.get('state') and row['elapsed_ms'] < start < row.get('valid_until_ms', row['elapsed_ms']))]


def health_level_key(rows):
    return 'hp' if any(not row.get('gap') and row.get('ratio') is None for row in rows) else 'ratio'


def interval_stats(dps_rows, hp_rows, start, end, *, rate='dps'):
    """Partial bins use an explicit uniform-within-bin approximation."""
    damage = ZERO
    amount_key='healing' if rate=='hps' else 'damage'
    selected = []
    for row in dps_rows:
        overlap = min(end, row["end_ms"]) - max(start, row["start_ms"])
        if overlap > 0:
            damage += row[amount_key] * Decimal(str(overlap)) / Decimal(str(row["end_ms"] - row["start_ms"]))
            selected.append(row)
    hp = health_in_range(hp_rows, start, end)
    hp_key = health_level_key(hp)
    return {amount_key: damage, 'average_'+rate: float(damage / (Decimal(str(end - start)) / 1000)) if end > start and selected else None,
            "peak": max(selected, key=lambda r: r[rate]) if selected else None,
            "low": min(selected, key=lambda r: r[rate]) if selected else None,
            "hp_high": max(hp, key=lambda r: r[hp_key]) if hp else None,
            "hp_low": min(hp, key=lambda r: r[hp_key]) if hp else None}


def amount_in_segments(rows, segments, start, end, *, amount_key='healing'):
    """Estimate amount inside observed spans using the chart's bin assumption."""
    total=ZERO
    for row in rows:
        width=row["end_ms"]-row["start_ms"]
        if width<=0:continue
        for segment in segments or ():
            overlap=min(end,row["end_ms"],segment["end_ms"])-max(start,row["start_ms"],segment["start_ms"])
            if overlap>0:
                total+=row[amount_key]*Decimal(str(overlap))/Decimal(str(width))
    return total


def boss_sample_at(rows, time):
    """Nearest observed sample only within a continuous local observation.

    No interpolation or extrapolation; explicit gaps and >2.5s spacing break it.
    """
    from bisect import bisect_left, bisect_right
    if not rows:return None
    times=[row["elapsed_ms"] for row in rows]
    previous = bisect_right(times, time)-1
    if previous >= 0 and 'valid_until_ms' in rows[previous]:
        row = rows[previous]
        return row if not row.get('gap') and time <= row['valid_until_ms'] else None
    i=bisect_left(times,time)
    if i<len(rows) and times[i]==time:
        if rows[i].get('state'):
            i=bisect_right(times,time)-1
        return None if rows[i].get("gap") else rows[i]
    if i==0 or i==len(rows):return None
    before,after=rows[i-1],rows[i]
    if before.get("gap") or after.get("gap") or after["elapsed_ms"]-before["elapsed_ms"]>2500:
        return None
    if before.get('state'):
        return before
    return min((before,after),key=lambda row:abs(row["elapsed_ms"]-time))
