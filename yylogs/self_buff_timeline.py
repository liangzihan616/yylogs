"""Offline self Buff evidence timeline. Never attaches, polls or invokes a game.

Display segments represent observations, not proven gameplay uptime. Native and
GUI channels remain separate. Existing-at-start and final-open spans are censored.
"""
from __future__ import annotations
import csv
import datetime as dt
import hashlib
import json
import math
from pathlib import Path

SCHEMA="yylogs.self-buff-timeline/1"
MAX_ROWS=100000
MAX_FILE_BYTES=64*1024*1024
TZ=dt.timezone(dt.timedelta(hours=8))
from yylogs.game_names import metadata
class SelfBuffTimelineError(ValueError):pass

def number(value,name):
    if type(value) not in (int,float) or not math.isfinite(value):raise SelfBuffTimelineError("invalid "+name)
    return value

def load_json(path):
    if path.stat().st_size>MAX_FILE_BYTES:raise SelfBuffTimelineError("file limit: "+path.name)
    return json.loads(path.read_text(encoding="utf-8-sig"))


def saved_boss_curves(folder):
    """Embed compact same-capture HP/Qi observations in exported snapshots."""
    folder = Path(folder)
    curves = {"hp": [], "qi": []}
    samples = folder / "samples.jsonl"
    if samples.is_file() and samples.stat().st_size <= MAX_FILE_BYTES:
        with samples.open(encoding="utf-8-sig") as stream:
            for index, line in enumerate(stream):
                if index >= MAX_ROWS:
                    break
                try:
                    row = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(row, dict):
                    continue
                health = row.get("boss_health") if isinstance(row.get("boss_health"), dict) else {}
                hp, maximum = health.get("hp"), health.get("hp_max")
                if (type(hp) in (int, float) and type(maximum) in (int, float)
                        and math.isfinite(hp) and math.isfinite(maximum)
                        and maximum > 0 and 0 <= hp <= maximum):
                    curves["hp"].append({"timestamp": row.get("timestamp"),
                                         "hp": hp, "hp_max": maximum})
    qi_path = folder / "boss_qi_candidates.jsonl"
    if qi_path.is_file() and qi_path.stat().st_size <= MAX_FILE_BYTES:
        with qi_path.open(encoding="utf-8-sig") as stream:
            for index, line in enumerate(stream):
                if index >= MAX_ROWS:
                    break
                try:
                    row = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(row, dict):
                    continue
                resource = row.get("resource") if isinstance(row.get("resource"), dict) else {}
                current, maximum = resource.get("current"), resource.get("maximum")
                if (type(current) in (int, float) and type(maximum) in (int, float)
                        and math.isfinite(current) and math.isfinite(maximum)
                        and maximum > 0 and 0 <= current <= maximum
                        and resource.get("numeric_range_consistent") is not False):
                    curves["qi"].append({"timestamp": row.get("timestamp"),
                                         "current": current, "maximum": maximum})
                else:
                    curves["qi"].append({"timestamp": row.get("timestamp"), "gap": True})
    for name, key in (("boss_qi_read_gaps.json", "qi"),):
        path = folder / name
        if path.is_file() and path.stat().st_size <= MAX_FILE_BYTES:
            gaps = load_json(path).get("gaps", [])
            curves[key].extend({"timestamp": gap.get("timestamp"), "gap": True}
                               for gap in gaps if isinstance(gap, dict))
    return curves if curves["hp"] or curves["qi"] else None

def read_journal(path,kind):
    if path.stat().st_size>MAX_FILE_BYTES:raise SelfBuffTimelineError("journal byte limit")
    rows=[];previous_seq=-1;previous_time=-1
    with path.open(encoding="utf-8-sig") as stream:
        for index,line in enumerate(stream,1):
            if index>MAX_ROWS:raise SelfBuffTimelineError("journal row limit")
            message=json.loads(line)
            if message.get("type")=="error":raise SelfBuffTimelineError("agent error in journal")
            row=message.get("payload",{})
            if row.get("kind")!=kind:continue
            t=number(row.get("observed_unix_ms"),"observation timestamp")
            seq=row.get("sequence")
            if type(seq) is not int or seq<=previous_seq or t<previous_time:raise SelfBuffTimelineError("unordered observations")
            previous_seq=seq;previous_time=t;rows.append(row)
    return rows

def label(buff_no):
    return {"buff_no":buff_no,**metadata("buff",buff_no)}

def display_mode(fields):
    if fields.get("duration")==-1 and fields.get("end_ts")==-1:return "persistent_display"
    if type(fields.get("duration")) in (int,float) and fields["duration"]>0:return "finite_display"
    return "unspecified_display"

def refine_saved_health_window(folder, report, state, window):
    from .encounter_timeline import refine_partial_health_window, EncounterTimelineError
    path=folder/"samples.jsonl"
    if not window.get("partial_observation") or state.get("start_mode")!="boss_health" or not path.exists():
        return window
    try:
        if path.stat().st_size>MAX_FILE_BYTES:
            raise EncounterTimelineError("health journal byte limit")
        # Hash the exact bytes being parsed; a stale/truncated journal cannot
        # narrow a window using a different recorder state.
        digest=hashlib.sha256()
        def frames():
            with path.open("rb") as stream:
                for index,line in enumerate(stream,1):
                    if index>MAX_ROWS or not line.endswith(b"\n"):
                        raise EncounterTimelineError("health journal row limit or torn frame")
                    digest.update(line)
                    yield json.loads(line)
        result=refine_partial_health_window(report,state,window,frames())
        if result is not window:
            if digest.hexdigest()!=state.get("stream_sha256"):
                raise EncounterTimelineError("health journal hash mismatch")
            report["source_sha256"][path.name]=digest.hexdigest()
            result["source_sha256"]=dict(report["source_sha256"])
        return result
    except (OSError,ValueError,KeyError,TypeError,AttributeError) as exc:
        report["warnings"].append("HP 归零计时边界未接纳："+str(exc))
        return window

def build_timeline(folder, *, encounter_state=None):
    folder=Path(folder).resolve()
    result=load_json(folder/"capture_result.json")
    state=load_json(folder/"capture_state.json")
    rows=read_journal(folder/"self_buff_state_snapshots.jsonl","self_buff_state_snapshot")
    events=read_journal(folder/"buff_observations.jsonl","buff_property_observation")
    if not rows or rows[0].get("phase")!="baseline":raise SelfBuffTimelineError("initial baseline required")
    actor=rows[0].get("target_entity_id");scene=rows[0].get("scene")
    if not isinstance(actor,str) or not actor:raise SelfBuffTimelineError("missing local recipient")
    if not isinstance(scene,dict) or not (scene.get("pve_verified") is True or scene.get("training_verified") is True):raise SelfBuffTimelineError("unverified PVE/training scope")
    for row in rows+events:
        if row.get("target_entity_id")!=actor or row.get("recipient_kind")!="self" or row.get("scene")!=scene:raise SelfBuffTimelineError("recipient or scene mismatch")
    finished=dt.datetime.fromisoformat(result["finished_at"])
    if result.get('observations_finished_at') is not None:
        observed_finish=dt.datetime.fromisoformat(result['observations_finished_at'])
        if (observed_finish.utcoffset() is None or finished.utcoffset() is None
                or observed_finish>finished):raise SelfBuffTimelineError('invalid observation finish')
        finished=observed_finish
    if finished.utcoffset() is None:raise SelfBuffTimelineError("capture finish needs timezone")
    origin=rows[0]["observed_unix_ms"];end=number(finished.timestamp()*1000,"finish timestamp")
    if end<max(r["observed_unix_ms"] for r in rows+events):raise SelfBuffTimelineError("finish precedes observations")
    limit=605000 if result.get("manual_capture") is True else 185000
    if end-origin>limit:raise SelfBuffTimelineError("outside finite prototype window")
    sequences=[row["sequence"] for row in rows+events]
    if len(set(sequences))!=len(sequences):raise SelfBuffTimelineError("duplicate global sequence")
    tracks={};observations=[];warnings=[]
    display_disabled=rows[0].get("display",{}).get("available") is False and rows[0].get("display",{}).get("reason")=="prototype_metadata_unmaterialized"
    if display_disabled:warnings.append("显示专用 Buff 通道未启用：函数元数据未展开；本场仅保留已校验通道，不代表显示类 Buff 不存在。")
    def track(channel,buff_no,instance_id=None):
        if type(buff_no) is not int or buff_no<=0:raise SelfBuffTimelineError("invalid Buff number")
        key=(channel,buff_no,str(instance_id) if instance_id is not None else None)
        if key not in tracks:tracks[key]={**label(buff_no),"key":"|".join(str(x) for x in key),"channel":channel,"target_entity_id":actor,"instance_id":instance_id,"segments":[]}
        return tracks[key]
    def segment(channel,buff_no,instance_id,t,seq,fields,source,status,left=False,previous=None):
        return {"buff_no":buff_no,"channel":channel,"instance_id":instance_id,"start_ms":t-origin,"end_ms":None,"start_sequence":seq,"end_sequence":None,"fields":fields,"mode":display_mode(fields) if channel=="gui_display_only" else "replicated_state","source_entity_id":source,"source_status":status,"left_censored":left,"right_censored":True,"start_observation_range_ms":None if left else [previous-origin if previous is not None else t-origin,t-origin],"end_observation_range_ms":None,"close_reason":None,"observed_span_ms":None,"gameplay_uptime_verified":False}
    def close(seg,t,seq,reason,previous=None,censored=False):
        seg.update(end_ms=t-origin,end_sequence=seq,right_censored=censored,close_reason=reason,observed_span_ms=t-origin-seg["start_ms"],end_observation_range_ms=None if censored else [previous-origin if previous is not None else t-origin,t-origin])
    # GUI state snapshots, not fixed-rate samples and not application counts.
    active={};previous=None;available=False
    for index,row in enumerate(rows):
        now=row["observed_unix_ms"];seq=row["sequence"];display=row.get("display",{})
        if type(display.get("available")) is not bool:raise SelfBuffTimelineError("display availability missing")
        if not display["available"]:
            for seg in active.values():close(seg,previous,seq,"unavailable_gap",censored=True)
            active={};available=False;previous=now
            observations.append({"channel":"gui_display_only","sequence":seq,"time_ms":now-origin,"kind":"unavailable_gap","buff_no":None,"source_entity_id":None})
            continue
        current={}
        for item in display.get("items",[]):
            no=item.get("buff_no");fields=item.get("fields",{})
            if type(no) is not int or no<=0 or no in current:raise SelfBuffTimelineError("invalid/duplicate display ID")
            if fields.get("buff_no")!=no or fields.get("buff_id")!=str(no) or fields.get("owner_id")!=actor or fields.get("is_only_show") is not True:raise SelfBuffTimelineError("display identity mismatch")
            current[no]=fields
        for no,seg in list(active.items()):
            if no not in current:
                close(seg,now,seq,"observed_absent",previous);del active[no]
                observations.append({"channel":"gui_display_only","sequence":seq,"time_ms":now-origin,"kind":"observed_absent","buff_no":no,"source_entity_id":None})
        for no,fields in current.items():
            existing=active.get(no)
            if existing and existing["fields"]==fields:continue
            if existing:close(existing,now,seq,"observed_fields_changed",previous)
            left=index==0 or not available
            seg=segment("gui_display_only",no,None,now,seq,fields,None,"unknown",left,previous)
            track("gui_display_only",no)["segments"].append(seg);active[no]=seg
            observations.append({"channel":"gui_display_only","sequence":seq,"time_ms":now-origin,"kind":"present_at_capture_start" if index==0 else ("observed_fields_changed" if existing else "observed_present"),"buff_no":no,"source_entity_id":None})
        previous=now;available=True
    for seg in active.values():close(seg,end,None,"capture_stopped_open",censored=True)
    # Native baseline/events remain separate even when simultaneous with a GUI ID.
    active_native={}
    def raw_fields(row,names):
        return [(name,row.get(name,{}).get("fields",{})) for name in names if row.get(name,{}).get("fields")]
    def native_source(fields):
        raw=fields.get("fromid");return (raw,"reported_unverified") if isinstance(raw,str) and raw.strip() else (None,"unknown")
    baseline=rows[0].get("replicated_baseline",{})
    if baseline.get("available") is not True:raise SelfBuffTimelineError("replicated baseline unavailable")
    for item in baseline.get("items",[]):
        fields=item.get("payload_snapshot",{}).get("fields",{});wrapper=item.get("wrapper_fields",{})
        if wrapper.get("owner_id")!=actor:raise SelfBuffTimelineError("baseline owner mismatch")
        no=fields.get("No");instance=fields.get("ID")
        if no is None or instance is None:raise SelfBuffTimelineError("native baseline ID unavailable")
        instance=str(instance)
        if instance in active_native:raise SelfBuffTimelineError("duplicate baseline instance")
        source,status=native_source(fields);seg=segment("replicated",no,instance,origin,rows[0]["sequence"],fields,source,status,True)
        track("replicated",no,instance)["segments"].append(seg);active_native[instance]=seg
    last_native=origin
    for row in events:
        t=row["observed_unix_ms"];seq=row["sequence"];action=row.get("action");raw_instance=row.get("instance_id")
        if not isinstance(raw_instance,(str,int)) or isinstance(raw_instance,bool) or not str(raw_instance):raise SelfBuffTimelineError("native instance ID unavailable")
        instance=str(raw_instance)
        if t<origin:raise SelfBuffTimelineError("event precedes baseline")
        if action not in ("insert","delete","modify","update"):raise SelfBuffTimelineError("unsupported native action")
        candidates=raw_fields(row,("old_snapshot",) if action=="delete" else ("new_snapshot","map_snapshot","update_snapshot"))
        fields=next((f for n,f in candidates if f.get("No") is not None),{})
        existing=active_native.get(instance)
        no=fields.get("No") if fields.get("No") is not None else (existing["buff_no"] if existing else None)
        if no is None:
            warnings.append("Unresolved Buff ID at native sequence "+str(seq));last_native=t;continue
        if fields.get("ID") is not None and str(fields["ID"])!=instance:raise SelfBuffTimelineError("native instance payload mismatch")
        if existing and no!=existing["buff_no"]:raise SelfBuffTimelineError("native instance Buff number changed")
        source,status=native_source(fields)
        if row.get("changed_key")=="fromid":
            scalar=row.get("new_snapshot",{}).get("value")
            source,status=(scalar,"reported_unverified") if isinstance(scalar,str) and scalar.strip() else (None,"unknown")
        # Every event preserves its own source evidence; do not silently carry it.
        observations.append({"channel":"replicated","sequence":seq,"time_ms":t-origin,"kind":action,"buff_no":no,"instance_id":instance,"changed_key":row.get("changed_key"),"source_entity_id":source,"source_status":status,"source_observations":row.get("source_observations",[]),"raw_snapshots":{key:row.get(key) for key in ("old_snapshot","new_snapshot","map_snapshot","update_snapshot")}})
        if existing:
            close(existing,t,seq,"native_"+action,last_native)
            del active_native[instance]
        if action!="delete":
            seg=segment("replicated",no,instance,t,seq,fields,source,status,False,last_native)
            track("replicated",no,instance)["segments"].append(seg);active_native[instance]=seg
        elif not existing:warnings.append("Unpaired removal at native sequence "+str(seq))
        last_native=t
    for seg in active_native.values():close(seg,end,None,"capture_stopped_open",censored=True)
    observations.sort(key=lambda r:(r["sequence"],r["channel"]))
    all_tracks=sorted(tracks.values(),key=lambda r:(r["channel"]!="gui_display_only",r["buff_no"],str(r["instance_id"])))
    cleanup=all(result.get(k) is True for k in ("script_unloaded","session_detached","cleanup_verified")) and (result.get("agent_stop") or {}).get("hook_detached") is True and state.get("active") is False and result.get("background_collection_active") is False
    if not cleanup:warnings.append("Capture cleanup not fully verified")
    if result.get("status") not in ("finite_self_buff_probe_finished","manual_self_buff_capture_stopped"):warnings.append("Capture did not finish a normal finite window")
    report={"schema":SCHEMA,"status":"offline_self_buff_observation_timeline","timeline_kind":"capture_diagnostics","encounter_start_verified":False,"encounter_end_verified":False,"encounter_windows":[],"source_capture":str(folder),"source_sha256":{name:hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in ("capture_result.json","self_buff_state_snapshots.jsonl","buff_observations.jsonl")},"own_entity_id":actor,"numeric_account_id_verified":False,"scene":scene,"origin":dt.datetime.fromtimestamp(origin/1000,TZ).isoformat(),"end":finished.astimezone(TZ).isoformat(),"duration_ms":end-origin,"capture_status":result.get("status"),"cleanup_verified":cleanup,"tracks":all_tracks,"observations":observations,"counts":{"display_snapshots":len(rows),"native_events":len(events),"initial_native_instances":len(baseline.get("items",[])),"tracks":len(all_tracks),"segments":sum(len(x["segments"]) for x in all_tracks)},"warnings":warnings,"coverage_calculated":False,"rdps_supported":False,"complete_encounter_verified":False,"full_coverage_verified":False,"game_process_access":False,"limitations":["横轴零点是采集开始，不是开战时刻。","显示常驻和限时显示不等于增伤生效；两通道不合并。","区间连接的是观测边界，不保证中间没有未通知的变化；不计算覆盖率。","开始前已存在、结束时仍存在或遇到缺口的区间保留边界未知。","显示来源未知；原生 fromid 仅为上报来源，未独立核验。"]}
    curves = saved_boss_curves(folder)
    if curves:
        report["boss_curves"] = curves
        if (folder / "boss_qi_candidates.jsonl").is_file():
            report["source_sha256"]["boss_qi_candidates.jsonl"] = hashlib.sha256(
                (folder / "boss_qi_candidates.jsonl").read_bytes()).hexdigest()


    if display_disabled:
        report["display_channel_available"]=False
        report["display_channel_unavailable_reason"]="prototype_metadata_unmaterialized"
    from .boss_buff_events import read_boss_event_timeline
    event_path=folder/"boss_buff_events.jsonl"
    if not event_path.exists() and result.get("boss_buff_event_channel"):
        binding=result["boss_buff_event_channel"]
        report["boss_buff_events"]={"available":False,"tracks":[],"observations":[],"gaps":[],"event_count":0,
            "binding_enabled":binding.get("enabled") is True,"unavailable_reason":binding.get("reason","event_journal_missing"),
            "full_coverage_verified":False,"rdps_supported":False}
    if event_path.exists():
        report["source_sha256"][event_path.name]=hashlib.sha256(event_path.read_bytes()).hexdigest()
        try:
            binding=result.get("boss_buff_event_channel") or {}
            target=binding.get("target_entity_id")
            if binding.get("enabled") is not True or binding.get("scope_verified") is not True or not target:
                raise ValueError("Boss event binding evidence unavailable")
            boss_events=read_boss_event_timeline(folder,origin_unix_ms=origin,end_unix_ms=end,
                scene_id=scene["scene_id"],dungeon_sid=scene.get("dungeon_sid"),target_entity_id=target)
            report["boss_buff_events"]=boss_events
            report["tracks"].extend(boss_events["tracks"])
            report["observations"].extend(boss_events["observations"])
            report["observations"].sort(key=lambda row:(row["time_ms"],row["sequence"],row["channel"]))
            report["source_sha256"][event_path.name]=hashlib.sha256(event_path.read_bytes()).hexdigest()
            report["warnings"].append("Boss Buff \u4e8b\u4ef6\u901a\u9053\u4e3a\u5ba2\u6237\u7aef\u5206\u53d1\u89c2\u6d4b\uff0c\u5168\u573a\u5b8c\u6574\u6027\u4e0e\u670d\u52a1\u5668\u751f\u6548\u65f6\u523b\u672a\u6838\u9a8c\uff1b\u4e0d\u4e0e\u5feb\u7167\u81ea\u52a8\u8865\u9f50\u3002")
        except (ValueError,OSError,KeyError,TypeError) as exc:
            report["boss_buff_events"]={"available":False,"tracks":[],"gaps":[],"error":str(exc),"event_count":0}
            report["warnings"].append("Boss event channel rejected: "+str(exc))
    report["counts"].update(tracks=len(report["tracks"]),segments=sum(len(t["segments"]) for t in report["tracks"]))
    from .boss_debuff_timeline import read_boss_debuff_timeline
    boss_path=folder/"boss_debuff_observations.jsonl"
    try:
        boss=read_boss_debuff_timeline(folder,origin_unix_ms=origin,end_unix_ms=end,scene_id=scene["scene_id"],own_entity_id=actor,dungeon_sid=scene.get("dungeon_sid"),target_entity_id=(load_json(folder/"live_status.json").get("target_id") if (folder/"live_status.json").exists() else None))
        report["boss_debuff"]=boss
        if boss.get("boundary_gap_observations"):
            report["warnings"].append("Boss Debuff 读取跨越采集边界并失败，末尾存在未验证区间；不能把空白当作未生效。")
        report["tracks"].extend(boss["tracks"])
        report["counts"].update(tracks=len(report["tracks"]),segments=sum(len(t["segments"]) for t in report["tracks"]))
        if boss.get("available"):
            report["warnings"].append("Boss Debuff 为状态采样，可能遗漏短效果或刷新；施加者为上报实体，未计算 RD。")
        if boss_path.exists():report["source_sha256"][boss_path.name]=hashlib.sha256(boss_path.read_bytes()).hexdigest()
    except (ValueError,OSError,KeyError,TypeError) as exc:
        report["boss_debuff"]={"available":False,"error":str(exc),"tracks":[],"gaps":[]}
        report["warnings"].append("Boss Debuff 未接纳："+str(exc))

    config_path=folder/"launch_config.json"
    report["capture_identity"]=None
    if config_path.exists():
        config=load_json(config_path)
        pid,ticks=config.get("game_pid"),config.get("game_creation_native_utc_ticks")
        # Launch configs preserve native 64-bit ticks as a decimal string to
        # avoid JSON/JavaScript precision loss. Parse exactly, never via float.
        if isinstance(ticks,str) and ticks.isascii() and ticks.isdecimal():
            ticks=int(ticks)
        if all(type(v) is int and v>0 for v in (pid,ticks)):
            report["capture_identity"]={"game_pid":pid,"process_creation_ticks":ticks}
    # Only a co-located explicitly saved recorder state is eligible. Never scan
    # other sessions or infer boundaries from Buff timing or a one-minute span.
    status_path=folder/"live_status.json"
    if encounter_state is None and status_path.exists():
        encounter_state=load_json(status_path)
    if encounter_state is not None:
        from .encounter_timeline import window_from_recorder,EncounterTimelineError
        try:
            controller_path=folder/"controller_status.json"
            controller=load_json(controller_path) if controller_path.exists() else None
            joint_scope_path=folder/"joint_capture_scope.json"
            if controller is not None and joint_scope_path.exists():
                controller["joint_capture_scope"]=load_json(joint_scope_path)
            window=window_from_recorder(report,encounter_state,controller)
            window=refine_saved_health_window(folder,report,encounter_state,window)
            report["encounter_windows"]=[window]
            report.update(encounter_start_verified=True,encounter_end_verified=window["end_verified"],partial_observation=window.get("partial_observation",False))
        except EncounterTimelineError as exc:
            report["warnings"].append("战斗边界未接纳："+str(exc))
    return report

def write_report(folder,destination):
    report=build_timeline(folder);destination=Path(destination)
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=destination.with_name(destination.name+".tmp")
    temporary.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")
    temporary.replace(destination);return report

def write_csv(report,destination):
    with Path(destination).open("w",encoding="utf-8-sig",newline="") as stream:
        names=["buff_no","name","name_source","name_configuration_verified","name_evidence","name_locale","channel","instance_id","mode","start_ms","end_ms","observed_span_ms","source_entity_id","source_status","left_censored","right_censored","close_reason","raw_fields_json","start_observation_range_ms","end_observation_range_ms","timeline_kind","encounter_id","capture_start_ms","capture_end_ms","inherited_at_window_start","clipped_at_window_end"]
        writer=csv.DictWriter(stream,fieldnames=names);writer.writeheader()
        for track in report["tracks"]:
            for segment in track["segments"]:writer.writerow({name:track.get(name) if name in ("buff_no","name","name_source","name_configuration_verified","name_evidence","name_locale","channel","instance_id") else (json.dumps(segment["fields"],ensure_ascii=False) if name=="raw_fields_json" else report.get("timeline_kind","capture_diagnostics") if name=="timeline_kind" else report.get("selected_encounter",{}).get("id") if name=="encounter_id" else segment.get(name)) for name in names})
