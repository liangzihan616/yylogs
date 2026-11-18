"""Offline Boss dispatcher observations, isolated from sampled state evidence.

No interval is repaired from a sample, configured duration or user expectation.
These are client dispatch events, not verified server uptime or rDPS evidence.
"""
from copy import deepcopy
import json
import math
from pathlib import Path
from yylogs.game_names import metadata

CHANNEL = "boss_events"
KINDS = {"boss_buff_state_baseline", "boss_buff_event_gap", "buff_property_observation"}
MAX_ROWS = 20000
MAX_BYTES = 64 * 1024 * 1024


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("invalid Boss event timestamp")
    return value


def build_boss_event_timeline(rows, *, origin_unix_ms, end_unix_ms, scene_id, dungeon_sid, target_entity_id):
    origin, end = _time(origin_unix_ms), _time(end_unix_ms)
    if end < origin or end - origin > 725000 or len(rows) > MAX_ROWS:
        raise ValueError("invalid Boss event window")
    if not isinstance(target_entity_id, str) or not target_entity_id or not scene_id:
        raise ValueError("Boss event target required")
    tracks, active, identities, observations, gaps = {}, {}, {}, [], []
    previous, sequence = origin, -1
    first_sequence = None
    disabled = False
    baseline_seen = False
    event_count = 0
    unclassified = 0
    pending_gap = None

    def close(seg, time, seq, reason, censored):
        seg.update(end_ms=time-origin, end_sequence=seq, close_reason=reason,
                   right_censored=censored, observed_span_ms=time-origin-seg["start_ms"],
                   end_observation_range_ms=None if censored else [time-origin, time-origin])

    def fields_of(row, names):
        snapshots = [row.get(name, {}).get("fields", {}) for name in names]
        if any(not isinstance(f, dict) for f in snapshots):
            raise ValueError("invalid Boss event fields")
        return snapshots

    def identity(fields, raw_instance):
        no = fields.get("No")
        if type(no) is not int or no <= 0:
            raise ValueError("Boss event Buff ID unavailable")
        if type(raw_instance) not in (int, str) or not str(raw_instance):
            raise ValueError("Boss event instance unavailable")
        if fields.get("ID") is not None and str(fields["ID"]) != str(raw_instance):
            raise ValueError("Boss event instance mismatch")
        return no, str(raw_instance)

    def open_state(fields, instance, now, seq, left, conflicting_source=False):
        nonlocal unclassified
        no, instance = identity(fields, instance)
        label = metadata("buff", no)
        if label.get("config_buff_estimate") != 2:
            unclassified += 1
            return
        raw_source = None if conflicting_source else fields.get("fromid")
        source = raw_source if isinstance(raw_source, str) and raw_source.strip() else None
        key = (no, instance, source)
        track = tracks.setdefault(key, {"key":f"boss_event|{no}|{instance}|{source}",
            "buff_no":no, **label, "channel":CHANNEL, "category":"boss_debuff",
            "target_entity_id":target_entity_id, "instance_id":instance,
            "source_entity_id":source, "segments":[]})
        seg = {"buff_no":no, "channel":CHANNEL, "mode":"boss_dispatch_state",
            "instance_id":instance, "source_entity_id":source,
            "source_status":"conflicting_reports" if conflicting_source else ("reported_unverified" if source else "unknown"),
            "fields":deepcopy(fields), "start_ms":now-origin, "end_ms":None,
            "start_sequence":seq, "end_sequence":None, "last_seen_ms":now-origin,
            "left_censored":left, "right_censored":True,
            "start_observation_range_ms":None if left else [now-origin, now-origin],
            "end_observation_range_ms":None, "close_reason":None,
            "observed_span_ms":None, "gameplay_uptime_verified":False}
        track["segments"].append(seg)
        active[instance] = seg

    for row in rows:
        kind = row.get("kind")
        current = row.get("scene", {})
        if (kind not in KINDS or row.get("recipient_kind") != "boss"
                or row.get("target_entity_id") != target_entity_id
                or current.get("scene_id") != scene_id or current.get("dungeon_sid") != dungeon_sid
                or current.get("pve_verified") is not True or current.get("training_verified") is not False):
            raise ValueError("Boss event scope mismatch")
        now, seq = _time(row.get("observed_unix_ms")), row.get("sequence")
        if type(seq) is not int or seq <= sequence or now < previous or not origin <= now <= end:
            raise ValueError("unordered/out-of-window Boss events")
        if disabled:
            raise ValueError("Boss events after disabled channel")
        if first_sequence is None:first_sequence=seq
        if kind == "boss_buff_event_gap":
            # The last positive event is the conservative lower end of a gap.
            for seg in active.values():
                close(seg, previous, seq, "event_observation_gap_not_removal", True)
            active.clear(); identities.clear()
            gaps.append({"start_ms":previous-origin, "end_ms":now-origin,
                         "reason":row.get("reason", "unknown"), "channel_disabled":row.get("channel_disabled") is True})
            pending_gap = gaps[-1]
            disabled = row.get("channel_disabled") is True
        elif kind == "boss_buff_state_baseline":
            if baseline_seen or event_count:
                raise ValueError("late or duplicate Boss event baseline")
            state = row.get("state", {})
            if state.get("available") is not True or not isinstance(state.get("items"), list) or len(state["items"]) > 256:
                raise ValueError("Boss event baseline unavailable")
            baseline_seen = True
            if pending_gap is not None:
                pending_gap["end_ms"]=now-origin;pending_gap=None
            for item in state["items"]:
                wrapper = item.get("wrapper_fields", {})
                fields = item.get("payload_snapshot", {}).get("fields", {})
                no, instance = identity(fields, fields.get("ID"))
                if wrapper.get("owner_id") != target_entity_id or str(wrapper.get("id")) != instance or wrapper.get("buff_no") != no:
                    raise ValueError("Boss event baseline owner/identity mismatch")
                if instance in identities:
                    raise ValueError("duplicate Boss event baseline instance")
                identities[instance] = no
                open_state(fields, instance, now, seq, True)
        else:
            event_count += 1
            action = row.get("action")
            if action not in ("insert", "delete", "modify", "update"):
                raise ValueError("unsupported Boss event action")
            raw_instance = row.get("instance_id")
            if type(raw_instance) not in (int, str) or not str(raw_instance):
                raise ValueError("Boss event instance unavailable")
            instance = str(raw_instance)
            candidates = fields_of(row, ("old_snapshot",) if action == "delete" else ("map_snapshot", "new_snapshot", "update_snapshot"))
            numbers = {f["No"] for f in candidates if f.get("No") is not None}
            if len(numbers) > 1:
                raise ValueError("conflicting Boss event Buff IDs")
            for candidate in candidates:
                if candidate.get("ID") is not None and str(candidate["ID"]) != instance:
                    raise ValueError("Boss event snapshot instance mismatch")
            sources={f["fromid"] for f in candidates if isinstance(f.get("fromid"), str) and f["fromid"].strip()}
            conflicting_source=len(sources)>1 and row.get("changed_key")!="fromid"
            fields = next((f for f in candidates if f.get("No") is not None), {})
            fields = deepcopy(fields)
            no = fields.get("No", identities.get(instance))
            if no is None:
                # An unresolved event invalidates continuity; never silently bridge.
                for seg in active.values():close(seg, previous, seq, "unresolved_event_gap", True)
                active.clear();identities.clear()
                if pending_gap is not None:pending_gap["end_ms"]=now-origin
                gaps.append({"start_ms":previous-origin,"end_ms":now-origin,"reason":"unresolved_buff_identity"})
                pending_gap=gaps[-1]
            else:
                if pending_gap is not None:
                    pending_gap["end_ms"]=now-origin;pending_gap=None
                fields["No"] = no
                identity(fields, instance)
                if instance in identities and identities[instance] != no:
                    raise ValueError("Boss event instance Buff number changed")
                identities[instance] = no
                key = row.get("changed_key")
                if key in ("No", "ID", "fromid", "level", "duration", "start_ts", "charge_level", "skill_id"):
                    newer = row.get("new_snapshot", {})
                    if newer.get("status") == "scalar":fields[key] = newer.get("value")
                identity(fields, instance)
                old = active.pop(instance, None)
                if old:close(old, now, seq, "dispatch_"+action, False)
                observations.append({"channel":CHANNEL,"sequence":seq,"time_ms":now-origin,
                    "kind":action,"buff_no":no,"instance_id":instance,
                    "source_observations":deepcopy(row.get("source_observations",[])),
                    "raw_snapshots":{k:deepcopy(row.get(k)) for k in ("old_snapshot","new_snapshot","map_snapshot","update_snapshot")}})
                if action == "delete":identities.pop(instance, None)
                else:open_state(fields, instance, now, seq, action != "insert" and old is None,conflicting_source=conflicting_source)
        previous, sequence = now, seq
    for seg in active.values():close(seg, end, None, "capture_stopped_open", True)
    if pending_gap is not None:pending_gap["end_ms"] = end-origin
    return {"available":bool(rows),"channel":CHANNEL,"tracks":list(tracks.values()),
            "observations":observations,"gaps":gaps,"event_count":event_count,
            "baseline_observed":baseline_seen,"channel_disabled":disabled,
            "unclassified_observations":unclassified,"full_coverage_verified":False,
            "first_sequence":first_sequence,"last_sequence":sequence if rows else None,
            "server_timestamps_verified":False,"rdps_supported":False}


def read_boss_event_timeline(folder, *, origin_unix_ms, end_unix_ms, scene_id, dungeon_sid, target_entity_id=None):
    path = Path(folder)/"boss_buff_events.jsonl"
    if not path.exists():return {"available":False,"tracks":[],"observations":[],"gaps":[],"event_count":0}
    if path.stat().st_size > MAX_BYTES:raise ValueError("Boss event journal byte limit")
    rows = []
    with path.open(encoding="utf-8-sig") as stream:
        for line in stream:
            if len(rows) >= MAX_ROWS:raise ValueError("Boss event journal row limit")
            message = json.loads(line)
            if message.get("type") != "send" or message.get("payload",{}).get("kind") not in KINDS:
                raise ValueError("invalid Boss event journal record")
            rows.append(message["payload"])
    if not rows:return {"available":False,"tracks":[],"observations":[],"gaps":[],"event_count":0}
    target_entity_id = target_entity_id or rows[0].get("target_entity_id")
    return build_boss_event_timeline(rows,origin_unix_ms=origin_unix_ms,end_unix_ms=end_unix_ms,
        scene_id=scene_id,dungeon_sid=dungeon_sid,target_entity_id=target_entity_id)
