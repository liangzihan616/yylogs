"""Offline Boss status observations; never an exact lifecycle or rDPS claim."""
from copy import deepcopy
import json
import math
from pathlib import Path

SCHEMA = "yylogs.boss-debuff-observation/1"
MAX_ROWS = 3000
MAX_BYTES = 32 * 1024 * 1024
MAX_GAP_MS = 2000

# Names are not a verified gameplay formula, duration unit, or damage multiplier.
from yylogs.game_names import metadata


def _time(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("invalid Boss observation time")
    return value


def build_boss_debuff_timeline(rows, *, origin_unix_ms, end_unix_ms, scene_id, target_entity_id):
    """Keep per-instance, per-source sampled intervals and explicit gaps.

    Only static buff_estimate=2 is a classified debuff. Unknown Boss status
    records remain in the raw journal, not silently classified or discarded there.
    """
    origin = _time(origin_unix_ms)
    end = _time(end_unix_ms)
    if end < origin or end-origin > 605000:
        raise ValueError("invalid Boss observation window")
    if not scene_id or not target_entity_id or len(rows) > MAX_ROWS:
        raise ValueError("invalid Boss scope or row bound")
    tracks = {}
    active = {}
    previous = None
    previous_started = None
    last_sequence = -1
    continuous = False
    previous_complete = False
    gaps = []
    unclassified = 0
    def finish_all(at, reason):
        for seg in active.values():
            seg.update(end_ms=max(seg["start_ms"], at-origin), right_censored=True,
                       close_reason=reason, observed_span_ms=max(0, at-origin-seg["start_ms"]))
        active.clear()
    for row in rows:
        if row.get("schema") != SCHEMA or row.get("scene_id") != scene_id or row.get("target_entity_id") != target_entity_id:
            raise ValueError("Boss observation scope mismatch")
        t = _time(row.get("observed_unix_ms"))
        started = _time(row.get("read_started_unix_ms",t))
        if started > t or started < origin or previous is not None and started < previous:
            raise ValueError("invalid Boss sample read interval")
        seq = row.get("sequence")
        if type(seq) is not int or seq <= last_sequence or previous is not None and t < previous or not origin <= t <= end:
            raise ValueError("unordered/out-of-window Boss observation")
        last_sequence = seq
        if row.get("status") == "gap" or previous is not None and t-previous > MAX_GAP_MS:
            finish_all(previous if previous is not None else t, "observation_gap_not_removal")
            gaps.append({"start_ms":(previous if previous is not None else t)-origin,"end_ms":t-origin,"reason":row.get("reason", "sampling_interval_exceeded")})
            continuous = False
        if row.get("status") == "gap":
            previous = t
            previous_started = started
            previous_complete = False
            continue
        partial = row.get("status") == "partial_snapshot"
        if row.get("status") not in ("snapshot","partial_snapshot") or type(row.get("buffs")) is not list or len(row["buffs"]) > 64:
            raise ValueError("invalid Boss snapshot")
        current = {}
        instance_keys = set()
        for item in row["buffs"]:
            if item.get("owner_id") != target_entity_id:
                raise ValueError("Boss buff owner mismatch")
            no = item.get("buff_no")
            instance = item.get("instance_id")
            if type(no) is not int or no <= 0 or type(instance) not in (int,str) or isinstance(instance,bool) or not str(instance):
                raise ValueError("invalid Boss buff identity")
            estimate = item.get("sys_data",{}).get("buff_estimate")
            if type(estimate) is not int or estimate != 2:
                unclassified += 1
                continue
            source = item.get("source_entity_id")
            if source is not None and (not isinstance(source,str) or not source.strip()):
                raise ValueError("invalid Boss buff source")
            if (no,str(instance)) in instance_keys:
                raise ValueError("duplicate Boss instance across sources")
            instance_keys.add((no,str(instance)))
            key = (no, str(instance), source)
            if key in current:
                raise ValueError("duplicate Boss buff instance/source")
            current[key] = item
        for key in list(active):
            if key not in current:
                if partial:
                    # Keep the last positive state until a complete inventory can
                    # confirm absence. A partial read may omit unrelated items.
                    continue
                seg = active.pop(key)
                seg.update(end_ms=t-origin, right_censored=False,
                           end_observation_range_ms=[seg["last_seen_ms"],t-origin],
                           close_reason="observed_absent_or_identity_changed",
                           observed_span_ms=t-origin-seg["start_ms"])
        for key,item in current.items():
            fields = deepcopy(item.get("state",{}))
            if key in active and active[key]["fields"] == fields and active[key].get("source_evidence",{}) == item.get("source_evidence",{}):
                active[key]["last_seen_ms"] = t-origin
                continue
            left = not continuous or not previous_complete
            if key in active:
                old = active.pop(key)
                old.update(end_ms=t-origin,right_censored=False,
                           end_observation_range_ms=[old["last_seen_ms"],t-origin],
                           close_reason="observed_state_change_not_reapplication",
                           observed_span_ms=t-origin-old["start_ms"])
            no,instance,source = key
            track_key = "boss|"+str(no)+"|"+instance+"|"+str(source)
            track = tracks.setdefault(key, {"key":track_key,"buff_no":no,**metadata("buff",no),"channel":"boss_sampled","category":"boss_debuff",
                "target_entity_id":target_entity_id,"instance_id":instance,
                "source_entity_id":source,"segments":[]})
            seg = {"buff_no":no,"channel":"boss_sampled","mode":"sampled_boss_state",
                   "instance_id":instance,"source_entity_id":source,
                   "source_status":"reported_unverified" if source else ("conflicting_reports" if item.get("source_evidence",{}).get("conflict") else "unknown"),
                   "source_evidence":deepcopy(item.get("source_evidence",{})),
                   "fields":fields,"start_ms":t-origin,"end_ms":None,
                   "last_seen_ms":t-origin,"start_sequence":seq,"end_sequence":None,
                   "left_censored":left,"right_censored":True,
                   "start_observation_range_ms":None if left else [previous_started-origin,t-origin],
                   "end_observation_range_ms":None,"close_reason":None,
                   "observed_span_ms":None,"gameplay_uptime_verified":False}
            track["segments"].append(seg)
            active[key] = seg
        continuous = True
        previous_complete = not partial
        previous = t
        previous_started = started
    finish_all(previous if previous is not None else origin,"last_observation_not_removal")
    successful=[r for r in rows if r.get("status")=="snapshot"]
    unobserved=[];last_success=None;pending=[]
    for row in rows:
        if row.get("status") in ("gap","partial_snapshot"):
            pending.append(row.get("reason","read_unavailable"));continue
        if row.get("status")!="snapshot":continue
        now=row["observed_unix_ms"]
        if last_success is None or pending or now-last_success>MAX_GAP_MS:
            a=origin if last_success is None else last_success
            if now>a:unobserved.append({"start_ms":a-origin,"end_ms":now-origin,"reason":"; ".join(dict.fromkeys(pending)) or ("before_first_snapshot" if last_success is None else "sampling_interval_exceeded")})
        last_success=now;pending=[]
    if last_success is not None and last_success<end:
        unobserved.append({"start_ms":last_success-origin,"end_ms":end-origin,"reason":"; ".join(dict.fromkeys(pending)) or "after_last_successful_snapshot"})
    elif last_success is None and end>origin:
        unobserved=[{"start_ms":0,"end_ms":end-origin,"reason":"no_successful_snapshot"}]

    return {"schema":"yylogs.boss-debuff-timeline/1","available":bool(rows),
            "sample_count":len(rows),"successful_snapshots":len(successful),
            "terminal_reason":next((r.get("reason","unknown") for r in reversed(rows)
                if r.get("status")=="gap" and r.get("recoverable_next_scheduled_sample") is False),None),
            "partial_snapshots":sum(r.get("status")=="partial_snapshot" for r in rows),
            "partial_observations":[{"sequence":r["sequence"],"observed_ms":r["observed_unix_ms"]-origin,
                "reason":r.get("reason"),"item_issues":deepcopy(r.get("item_issues",[]))}
                for r in rows if r.get("status")=="partial_snapshot"],"unobserved_intervals":unobserved,
            "last_success_ms":successful[-1]["observed_unix_ms"]-origin if successful else None,
            "scene_id":scene_id,"target_entity_id":target_entity_id,
            "tracks":list(tracks.values()),"gaps":gaps,"unclassified_observations":unclassified,
            "sampled_interval_only":True,"exact_buff_events":False,
            "full_coverage_verified":False,"rdps_supported":False}


def read_boss_debuff_timeline(folder, *, origin_unix_ms, end_unix_ms, scene_id, own_entity_id=None, dungeon_sid=None, target_entity_id=None):
    path = Path(folder)/"boss_debuff_observations.jsonl"
    if not path.exists():
        return {"available":False,"tracks":[],"gaps":[],"rdps_supported":False}
    if path.stat().st_size > MAX_BYTES:
        raise ValueError("Boss journal byte limit")
    rows = []
    with path.open(encoding="utf-8-sig") as stream:
        for line in stream:
            if len(rows) >= MAX_ROWS:
                raise ValueError("Boss journal row limit")
            rows.append(json.loads(line))
    if not rows:
        return {"available":False,"tracks":[],"gaps":[],"rdps_supported":False}
    target=rows[0].get("target_entity_id")
    if target_entity_id is not None and target != target_entity_id:
        raise ValueError("Boss journal target differs from encounter")
    if own_entity_id is not None and any(r.get("own_entity_id")!=own_entity_id for r in rows):
        raise ValueError("Boss journal local actor mismatch")
    if dungeon_sid is not None and any(r.get("dungeon_sid")!=dungeon_sid for r in rows):
        raise ValueError("Boss journal dungeon mismatch")
    if any(r.get("scene_id")!=scene_id or r.get("target_entity_id")!=target for r in rows):
        raise ValueError("Boss journal scope mismatch")
    included=[r for r in rows if origin_unix_ms <= _time(r.get("read_started_unix_ms",r.get("observed_unix_ms"))) <= _time(r.get("observed_unix_ms")) <= end_unix_ms]
    result=build_boss_debuff_timeline(included,origin_unix_ms=origin_unix_ms,
        end_unix_ms=end_unix_ms,scene_id=scene_id,target_entity_id=target)
    result["outside_capture_rows"]=len(rows)-len(included)
    result["boundary_gap_observations"]=[{
        "sequence":r.get("sequence"),"reason":r.get("reason"),
        "read_started_unix_ms":r.get("read_started_unix_ms"),
        "observed_unix_ms":r.get("observed_unix_ms"),
        "read_overlaps_capture_boundary":True,"exact_missing_interval_verified":False
    } for r in rows if r.get("status")=="gap"
        and _time(r.get("read_started_unix_ms",r.get("observed_unix_ms")))<=end_unix_ms
        and _time(r.get("observed_unix_ms"))>=origin_unix_ms
        and r not in included]
    return result


def _presence_sources(item, segments):
    """Keep attribution explicit on chart unions; never select a default caster."""
    sources=sorted({s["source_entity_id"] for s in segments if s.get("source_entity_id")})
    item["source_entity_ids"]=sources
    item["has_unknown_source"]=any(not s.get("source_entity_id") for s in segments)
    item["source_entity_id"]=sources[0] if len(sources)==1 and not item["has_unknown_source"] else None
    item["source_status"]="reported_unverified" if item["source_entity_id"] else "multiple_or_unknown"


def presence_tracks(tracks):
    """Combine duplicate instances per Buff ID and reported caster only."""
    grouped={}
    for track in tracks:
        if track.get("channel") not in ("boss_sampled","boss_events","packet_boss"):continue
        source=track.get("source_entity_id")
        # Unknown-source instances cannot safely be attributed to one another.
        identity=(source if source else ("unknown",track.get("instance_id"),track.get("key")))
        key=(track["buff_no"],identity)
        if key not in grouped:
            grouped[key]={**deepcopy({k:v for k,v in track.items() if k!='segments'}),"key":f"boss_presence:{track['buff_no']}:{source or track.get('instance_id')}",
                          "instance_id":None if source else track.get("instance_id"),"source_entity_id":source,
                          "segments":[],"presence_aggregated":True,
                          "source_channels":[track["channel"]]}
        elif track["channel"] not in grouped[key]["source_channels"]:
            grouped[key]["source_channels"].append(track["channel"])
        grouped[key]["segments"].extend(track["segments"])
    result=list(grouped.values())
    for item in result:
        if len(item["source_channels"]) > 1:
            item["channel"] = "boss_combined"
            item["key"] = f"boss_combined_presence:{item['buff_no']}:{item.get('source_entity_id') or item.get('instance_id')}"
        raw_segments=item["segments"]
        _presence_sources(item,raw_segments)
        merged=[]
        for source in sorted(raw_segments,key=lambda s:(s["start_ms"],s["end_ms"])):
            seg=dict(source)
            seg["state_observation_segments"]=[source]
            # Merge exact shared endpoints; explicit gap closures remain split.
            overlaps=bool(merged and seg["start_ms"]<merged[-1]["end_ms"])
            touches=bool(merged and seg["start_ms"]==merged[-1]["end_ms"]
                         and merged[-1]["close_reason"] not in ("observation_gap_not_removal","event_observation_gap_not_removal","unresolved_event_gap"))
            same_point=bool(merged and seg["start_ms"]==seg["end_ms"]
                            ==merged[-1]["start_ms"]==merged[-1]["end_ms"])
            if overlaps or touches or same_point:
                previous=merged[-1]
                previous["state_observation_segments"].extend(seg["state_observation_segments"])
                if seg["end_ms"]>=previous["end_ms"]:
                    previous.update({k:deepcopy(seg[k]) for k in ("end_ms","right_censored","close_reason","end_observation_range_ms","fields","last_seen_ms") if k in seg})
                previous["observed_span_ms"]=previous["end_ms"]-previous["start_ms"]
            else:merged.append(seg)
        for seg in merged:
            _presence_sources(seg,seg["state_observation_segments"])
        item["segments"]=deepcopy(merged)
    # A handoff is only labeled when both casters switch at the same sampled
    # boundary. The interval proves observation order, not exact application time.
    by_buff={}
    for item in result:by_buff.setdefault(item["buff_no"],[]).append(item)
    for items in by_buff.values():
        endings={}
        for item in items:
            if item.get('source_entity_id') is None:continue
            for candidate in item['segments']:
                boundary=candidate.get('end_observation_range_ms')
                if boundary:endings[(id(item),boundary[1])]=candidate
        for newer in items:
            for segment in newer["segments"]:
                start=segment.get("start_observation_range_ms")
                if not start or newer.get("source_entity_id") is None:continue
                for older in items:
                    if older is newer or older.get("source_entity_id") is None:continue
                    previous=endings.get((id(older),start[1]))
                    if previous:
                        segment["handoff_from_source_id"]=older["source_entity_id"]
                        segment["handoff_kind"]="same_sample_source_handoff"
                        break
    return sorted(result,key=lambda item:(item["buff_no"],item.get("segments",[{}])[0].get("start_ms",0),item.get("source_entity_id") or ""))


def provider_tracks(tracks):
    """One Boss effect row, preserving provider changes and observed refreshes.

    Each Boss effect has one owner. Later observations replace earlier owners;
    stale overlapping intervals never resurrect a replaced owner. Same-time
    conflicting owners without transport ordering remain unresolved.
    """
    grouped = {}
    for track in tracks:
        if track.get('channel') not in ('boss_sampled','boss_events','packet_boss'):
            continue
        key=(track.get('target_entity_id'),track['buff_no'])
        group=grouped.setdefault(key,{**deepcopy({k:v for k,v in track.items() if k!='segments'}),
            'key':f'boss_providers:{key[0]}:{key[1]}','segments':[],
            'provider_segmented':True,'presence_aggregated':True})
        group['segments'].extend(deepcopy(track.get('segments',[])))
    for group in grouped.values():
        raw=group['segments'];events={}
        for i,seg in enumerate(raw):
            if seg['end_ms']<=seg['start_ms']:continue
            events.setdefault(seg['start_ms'],[]).append((True,i))
            events.setdefault(seg['end_ms'],[]).append((False,i))
        points=sorted(events);active=set();segments=[]
        for j,point in enumerate(points[:-1]):
            for add,i in sorted(events[point]):
                if add:active.add(i)
                else:active.discard(i)
            new_ids=[i for add,i in events[point] if add]
            if new_ids:
                # Retire all prior state, including intervals still present in a
                # legacy snapshot. Ending the replacement cannot restore it.
                active=set(new_ids)
                ordered=[i for i in active if raw[i].get('transport_record_ordinal') is not None]
                if len(ordered)==len(active):
                    last=max(raw[i]['transport_record_ordinal'] for i in ordered)
                    active={i for i in ordered if raw[i]['transport_record_ordinal']==last}
            if not active:continue
            originals=[raw[i] for i in sorted(active)]
            sources=sorted({s['source_entity_id'] for s in originals if s.get('source_entity_id')})
            conflict=len(sources)>1
            if conflict:sources=[]
            new=[s for s in originals if s['start_ms']==point]
            previous=segments[-1] if segments and segments[-1]['end_ms']==point else None
            change='applied'
            old_sources=previous.get('source_entity_ids',[]) if previous else []
            if conflict:change='unresolved'
            elif previous and old_sources!=sources and old_sources and sources:change='handoff'
            elif any(s.get('change_kind')=='handoff' for s in new):change='handoff'
            elif previous and old_sources==sources:
                change='refresh' if any(s.get('change_kind')=='refresh' for s in new) else 'state-update'
            segment={**originals[-1],'start_ms':point,'end_ms':points[j+1],
                'observed_span_ms':points[j+1]-point,'source_entity_ids':sources,
                'source_entity_id':sources[0] if len(sources)==1 else None,
                'has_unknown_source':conflict or any(not s.get('source_entity_id') for s in originals),
                'source_status':'conflicting_reports' if conflict else originals[-1].get('source_status','reported'),
                'change_kind':change,'previous_source_entity_ids':old_sources,
                'state_observation_segments':originals}
            # Retain genuine refresh and handoff boundaries; merge field-only
            # updates for the same visible provider, keeping original evidence.
            if previous and change=='state-update' and previous.get('source_entity_ids')==sources and previous.get('has_unknown_source')==segment['has_unknown_source']:
                previous['end_ms']=segment['end_ms'];previous['observed_span_ms']=previous['end_ms']-previous['start_ms']
                previous['state_observation_segments'].extend(originals)
                for field in ('right_censored','close_reason','end_observation_range_ms','last_seen_ms'):
                    if field in segment:previous[field]=deepcopy(segment[field])
            else:segments.append(segment)
        group['segments']=segments;_presence_sources(group,segments)
        # A row may contain several providers across time; preserve all names.
        group['source_entity_ids']=sorted({v for seg in segments for v in seg.get('source_entity_ids',[])})
        group['has_unknown_source']=any(seg.get('has_unknown_source') for seg in segments)
        group['source_entity_id']=group['source_entity_ids'][0] if len(group['source_entity_ids'])==1 and not group.get('has_unknown_source') else None
    return sorted(grouped.values(),key=lambda t:(t['buff_no'],t.get('target_entity_id') or ''))
