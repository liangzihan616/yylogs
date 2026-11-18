"""Portable, hash-checked local battle records. Never accesses a game process."""
from datetime import datetime
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from uuid import uuid4
import zipfile

from yylogs.self_buff_timeline import SCHEMA as TIMELINE_SCHEMA, build_timeline
from yylogs.damage_summary import observation_windows, summarize
from yylogs.runtime_scope import is_supported_pve_sid
from yylogs.dungeon_identity import resolve_dungeon_identity

SCHEMA = "yylogs.battle-bundle/1"
MAX_TOTAL = 256 * 1024 * 1024
MAX_FILE = 64 * 1024 * 1024
FILES = frozenset({
    "capture_result.json", "capture_state.json", "controller_status.json",
    "self_buff_state_snapshots.jsonl", "buff_observations.jsonl",
    "live_status.json", "launch_config.json", "joint_capture_scope.json",
    "samples.jsonl", "entry_events.jsonl", "damage_preflight.json",
    "damage_capture_result.json", "boss_buff_event_binding.json",
    "boss_buff_events.jsonl", "boss_debuff_observations.jsonl",
    "boss_debuff_status.json", "timeline_snapshot.json",
    "boss_qi_candidates.jsonl", "boss_qi_candidate_status.json",
    "boss_qi_read_gaps.json", "boss_qi_live_status.json",
    "boss_health_sample_gaps.jsonl",
    "actor_names.jsonl", "actor_names_status.json",
    "member_health.jsonl", "member_health_status.json",
    "hook_events.jsonl", "settlement_link.json", "packet_snapshot.json",
})
REQUIRED = {"capture_result.json", "capture_state.json",
            "self_buff_state_snapshots.jsonl", "buff_observations.jsonl"}


def store_path(workspace=None):
    if workspace:
        return Path(workspace) / "logs_research/yylogs_imported/battles"
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "yylogs/battles"


def _json(raw):
    return json.loads(raw.decode("utf-8-sig"),
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("非有限数值")))


def _payload_check(payload):
    if not payload or not set(payload) <= FILES:
        raise ValueError("日志包包含不支持的文件")
    if any(len(raw) > MAX_FILE for raw in payload.values()) or sum(map(len, payload.values())) > MAX_TOTAL:
        raise ValueError("日志包超过大小限制")
    if not REQUIRED <= set(payload) and set(payload) not in ({"timeline_snapshot.json"}, {"packet_snapshot.json"}):
        raise ValueError("日志包缺少必要的采集数据")


def _manifest(payload):
    _payload_check(payload)
    return {"schema": SCHEMA,
            "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in sorted(payload.items())}}


def _record_id(manifest):
    return "battle_" + hashlib.sha256(json.dumps(manifest, sort_keys=True).encode("utf8")).hexdigest()[:24]


def _read_payload(folder):
    folder = Path(folder)
    payload = {}
    total = 0
    for name in sorted(FILES):
        path = folder / name
        if path.is_symlink():
            raise ValueError("不读取日志中的符号链接")
        if path.is_file():
            size = path.stat().st_size
            total += size
            if size > MAX_FILE or total > MAX_TOTAL:
                raise ValueError("采集文件超过大小限制")
            payload[name] = path.read_bytes()
    _payload_check(payload)
    return payload


def validate_snapshot(report):
    """Legacy timeline exports retain observations, never regain missing raw hits."""
    if not isinstance(report, dict) or report.get("schema") != TIMELINE_SCHEMA:
        raise ValueError("不是可识别的时间轴 JSON")
    duration = report.get("duration_ms")
    if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 <= duration <= 605000:
        raise ValueError("时间轴时长无效")
    for key in ("origin", "end"):
        if datetime.fromisoformat(report[key]).utcoffset() is None:
            raise ValueError("时间轴缺少时区")
    scene = report["scene"]
    sid = scene.get("dungeon_sid")
    if not ((scene.get("pve_verified") is True and is_supported_pve_sid(sid))
            or (scene.get("training_verified") is True and sid == -1)):
        raise ValueError("时间轴不属于已支持的副本或木桩")
    if not isinstance(report.get("own_entity_id"), str) or not report["own_entity_id"]:
        raise ValueError("时间轴缺少自身身份")
    tracks = report.get("tracks")
    if not isinstance(tracks, list) or len(tracks) > 10000:
        raise ValueError("时间轴轨道无效")
    segments = 0
    for track in tracks:
        if track.get("channel") not in ("gui_display_only", "replicated", "boss_sampled", "boss_events"):
            raise ValueError("未知观测通道")
        if type(track.get("buff_no")) is not int or not isinstance(track.get("segments"), list):
            raise ValueError("Buff 轨道无效")
        for segment in track["segments"]:
            segments += 1
            a, b = segment["start_ms"], segment["end_ms"]
            if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (a, b))
                    or not 0 <= a <= b <= duration):
                raise ValueError("Buff 观测区间越界")
            if not all(key in segment for key in ("mode", "channel", "left_censored",
                                                  "right_censored", "observed_span_ms")):
                raise ValueError("Buff 观测字段不完整")
    if segments > 100000:
        raise ValueError("Buff 区间数量过多")
    result = deepcopy(report)
    windows = result.get("encounter_windows", [])
    if not isinstance(windows, list) or len(windows) > 16:
        raise ValueError("战斗区间无效")
    capture_duration = result.get("capture_duration_ms", duration)
    sliced = isinstance(result.get("selected_encounter"), dict) and bool(result.get("capture_origin"))
    if type(capture_duration) not in (int, float) or not math.isfinite(capture_duration) or not 0 <= capture_duration <= 605000:
        raise ValueError("采集时长无效")
    for window in windows:
        if not isinstance(window, dict):
            raise ValueError("战斗区间无效")
        start, end = window.get("start_ms"), window.get("end_ms")
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end)
                or not 0 <= start < end <= capture_duration):
            raise ValueError("战斗区间越界")
        if window.get("scene") != scene or window.get("own_entity_id") != report["own_entity_id"]:
            raise ValueError("战斗区间身份不匹配")
    if sliced:
        selected = result["selected_encounter"]
        if selected not in windows:
            raise ValueError("战斗快照所选区间不匹配")
        capture_origin = datetime.fromisoformat(result["capture_origin"])
        if (capture_origin.utcoffset() is None
                or abs((datetime.fromisoformat(result["origin"])-capture_origin).total_seconds()*1000
                       - selected["start_ms"]) > .01
                or abs(duration - (selected["end_ms"]-selected["start_ms"])) > .01):
            raise ValueError("战斗快照时间坐标不匹配")
    # Snapshot boundaries only filter Buff display; original evidence is still
    # required for damage timing and independently validated encounter windows.
    result.update(snapshot_filter_windows=windows, encounter_windows=[],
                  encounter_start_verified=False, encounter_end_verified=False,
                  complete_encounter_verified=False,
                  imported_snapshot_only=True, coverage_calculated=False, rdps_supported=False)
    if windows:
        result.setdefault("warnings", []).append("导入快照沿用已保存的战斗边界，仅用于本地筛选和显示。")
    result.setdefault("observations", [])
    result.setdefault("counts", {"initial_native_instances": 0})
    result.setdefault("warnings", [])
    return result


def matching_capture(report, store):
    """Recover only a same-workspace original whose saved hashes all match."""
    source = report.get("source_capture")
    hashes = report.get("source_sha256")
    if not isinstance(source, str) or not isinstance(hashes, dict) or not hashes:
        return None
    base = Path(store).resolve().parent.parent / "manual_captures"
    folder = Path(source)
    try:
        if (folder.is_symlink() or (hasattr(folder, "is_junction") and folder.is_junction())
                or folder.resolve().parent != base or not folder.is_dir()):
            return None
        if not {"capture_result.json", "buff_observations.jsonl", "self_buff_state_snapshots.jsonl"} <= hashes.keys():
            return None
        for name, digest in hashes.items():
            if name not in FILES or not isinstance(digest, str):
                return None
            path = folder / name
            if (path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE
                    or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
                return None
        return folder.resolve()
    except (OSError, ValueError):
        return None


def _verified_payload(folder):
    folder = Path(folder)
    raw = (folder / "manifest.json").read_bytes()
    if len(raw) > 65536:
        raise ValueError("战斗记录清单过大")
    manifest = _json(raw)
    payload = _read_payload(folder)
    if manifest != _manifest(payload) or folder.name != _record_id(manifest):
        raise ValueError("战斗记录文件校验失败")
    return raw, payload


def load_record(folder):
    folder = Path(folder)
    raw, payload = _verified_payload(folder)
    if 'packet_snapshot.json' in payload:
        from yylogs.packet_records import load_packet_record, recover_packet_observations, recover_packet_settlement
        snapshot = _json(payload['packet_snapshot.json'])
        recovered = recover_packet_observations(snapshot, folder)
        observations_recovered = recovered is not snapshot
        with_settlement = recover_packet_settlement(recovered, folder)
        settlement_recovered = with_settlement is not recovered
        recovered = with_settlement
        report = load_packet_record(recovered, folder)
        report['packet_observations_recovered'] = observations_recovered
        report['packet_settlement_recovered'] = settlement_recovered
        return report
    snapshot = "timeline_snapshot.json" in payload
    timeline = validate_snapshot(_json(payload["timeline_snapshot.json"])) if snapshot else build_timeline(folder)
    analysis_folder = folder
    linked = matching_capture(timeline, folder.parent) if snapshot else None
    if linked:
        recovered = build_timeline(linked)
        if recovered["scene"] == timeline["scene"] and recovered["own_entity_id"] == timeline["own_entity_id"]:
            timeline, analysis_folder = recovered, linked
            timeline["recovered_from_matching_original"] = True
    if not snapshot:
        state = _json(payload["capture_state.json"])
        if state.get("active") is not False:
            raise ValueError("采集尚未停止")
    windows = observation_windows(timeline)
    selected_window=windows[0] if windows else None
    if 'settlement_link.json' in payload:
        from yylogs.settlement_accounting import read_link
        from yylogs.settlement_summary import load_settlement_summary
        link=read_link(folder,timeline,load_settlement_summary(folder,timeline),windows)
        if link['status']=='confirmed':selected_window=link['window']
    # The linked original can recover raw hits, never newer settlement snapshots.
    summary = summarize(analysis_folder, timeline, selected_window,
                        settlement_folder=folder)
    total = summary["total_damage"]
    members = []
    for ordinal, member in enumerate(summary["members"], 1):
        members.append({**member, "name": member["name"] or ("本人 · 名称未解析" if member["local"] else f"人物 {ordinal}"),
                        "level": None, "damage": float(member["damage"]), "damage_exact": str(member["damage"]),
                        "dps": float(member["estimated_dps"] if summary.get("event_timing") else member["dps"])
                        if (member["estimated_dps"] if summary.get("event_timing") else member["dps"]) is not None else None,
                        "taken": None, "healing": None,
                        "rdps": None,
                        "damage_share": float(member["share"] or 0)})
    healing = summary['healing_summary']
    by_id = {member['id']: member for member in members}
    for actor in healing['members']:
        if actor['id'] not in by_id:
            member = {**actor, 'name': actor['name'] or actor['id'], 'level': None,
                'damage': 0.0, 'damage_exact': '0', 'dps': None, 'taken': None,
                'rdps': None, 'damage_share': 0.0}
            members.append(member)
            by_id[actor['id']] = member
        by_id[actor['id']].update(healing=float(actor['healing']),
            healing_exact=str(actor['healing']), hps=float(actor['hps']) if actor['hps'] is not None else None)
    seconds = summary["duration_seconds"]
    estimated = summary.get("event_timing")
    displayed_seconds = estimated["duration_seconds"] if estimated else seconds
    meta = {}
    meta_path = folder / "record_meta.json"
    if meta_path.is_file() and not meta_path.is_symlink() and meta_path.stat().st_size <= 65536:
        try:
            candidate = _json(meta_path.read_bytes())
            if isinstance(candidate, dict) and isinstance(candidate.get("name"), str):
                name = candidate["name"].strip()
                if name:
                    meta = {"name": name}
        except (ValueError, OSError, TypeError):
            meta = {}
    encounter = resolve_dungeon_identity(timeline["scene"].get("dungeon_sid"))
    default_name = encounter["label"] + " " + datetime.fromisoformat(timeline["origin"]).strftime("%m-%d %H:%M")
    return {
        "id": folder.name, "record_kind": "saved_manual_capture", "account": "当前角色",
        "display_name": meta.get("name", default_name),
        "display_name_is_custom": bool(meta), "encounter": encounter,
        "encounter_label": encounter["label"], "dungeon_name": encounter["dungeon_name"],
        "boss_name": encounter["boss_name"], "boss_order": encounter["boss_order"],
        "timestamp": timeline["origin"], "dungeon_sid": timeline["scene"]["dungeon_sid"],
        "duration": float(displayed_seconds) if displayed_seconds else None,
        "duration_label": "事件估计用时" if estimated else "观测用时",
        "duration_kind": "observed_capture", "team_damage": float(total), "team_damage_exact": str(total),
        "team_dps": float(total / displayed_seconds) if displayed_seconds else None,
        "team_taken": None,
        "team_healing": float(healing['total_healing']) if healing['members'] else None,
        "healing_summary": healing, "rdps": None, "rdps_supported": False,
        "rdps_estimate_supported": False, "rd_estimate": None,
        "members": members, "buffs": [], "checks_passed": len(payload), "checks_total": len(payload),
        "verified": False, "numeric_account_verified": False, "cache_stat_type": None,
        "source_path": str(folder / "manifest.json"), "source_sha256": hashlib.sha256(raw).hexdigest(),
        "capture_folder": str(analysis_folder), "record_folder": str(folder),
        "buff_timeline": timeline, "capture_summary": summary,
        "skill_details": {}, "capture_active": False, "imported_snapshot_only": snapshot,
        "event_count": summary["record_count"], "validation_scope": "local_capture_observations_only",
    }


def load_record_index(folder):
    """Read only small identity files for the history list; full hashes are checked on open."""
    folder = Path(folder)
    manifest_path = folder / "manifest.json"
    if folder.is_symlink() or manifest_path.is_symlink():
        raise ValueError("战斗记录目录无效")
    raw = manifest_path.read_bytes()
    if len(raw) > 65536:
        raise ValueError("战斗记录清单过大")
    manifest = _json(raw)
    if (manifest.get("schema") != SCHEMA or not isinstance(manifest.get("files"), dict)
            or folder.name != _record_id(manifest)):
        raise ValueError("战斗记录清单无效")

    sid = None
    timestamp = None
    member_count = None
    for filename in ("packet_snapshot.json", "timeline_snapshot.json"):
        path = folder / filename
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 1024 * 1024:
            continue
        value = _json(path.read_bytes())
        if filename == "packet_snapshot.json":
            if sid is None:sid = value.get("dungeon_sid")
            timestamp = value.get("start")
            names = value.get("names")
            member_count = len(names) if isinstance(names, dict) else None
        else:
            scene = value.get("scene")
            sid = scene.get("dungeon_sid") if isinstance(scene, dict) else None
            timestamp = value.get("origin")
        break

    packet_snapshot = folder / "packet_snapshot.json"
    if (sid is None or timestamp is None) and packet_snapshot.is_file() and not packet_snapshot.is_symlink():
        with packet_snapshot.open("rb") as stream:
            prefix = stream.read(8192).decode("utf-8-sig", errors="ignore")
        if sid is None:
            match = re.search(r'"dungeon_sid"\s*:\s*(-?\d+)', prefix)
            if match:sid = int(match.group(1))
        if timestamp is None:
            match = re.search(r'"start"\s*:\s*"([^"\\]{1,80})"', prefix)
            if match:timestamp = match.group(1)

    if sid is None:
        for filename in ("live_status.json", "capture_result.json"):
            path = folder / filename
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 128 * 1024:
                continue
            value = _json(path.read_bytes())
            sid = value.get("dungeon_sid")
            if sid is None and isinstance(value.get("damage_channel"), dict):
                channel = value["damage_channel"]
                sid = channel.get("dungeon_sid")
                observed = channel.get("first_boss_candidate_observed_unix_ms")
                if isinstance(observed, (int, float)):
                    from datetime import timezone
                    timestamp = datetime.fromtimestamp(observed / 1000, timezone.utc).isoformat()
            timestamp = timestamp or value.get("finished_at") or value.get("start")
            if sid is not None:
                break

    if not isinstance(timestamp, str):
        timestamp = datetime.fromtimestamp(folder.stat().st_mtime).astimezone().isoformat()
    datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    meta = {}
    meta_path = folder / "record_meta.json"
    if meta_path.is_file() and not meta_path.is_symlink() and meta_path.stat().st_size <= 65536:
        try:
            candidate = _json(meta_path.read_bytes())
            if isinstance(candidate, dict) and isinstance(candidate.get("name"), str):
                meta = candidate
        except (ValueError, OSError, TypeError):
            pass
    encounter = resolve_dungeon_identity(sid)
    return {"id": folder.name, "record_kind": "saved_battle_index", "account": "抓包记录",
            "display_name": meta.get("name") or encounter["label"],
            "display_name_is_custom": bool(meta.get("name")), "timestamp": timestamp,
            "dungeon_sid": sid, "encounter": encounter, "encounter_label": encounter["label"],
            "dungeon_name": encounter["dungeon_name"], "duration": None,
            "member_count": member_count, "members": [], "record_folder": str(folder),
            "source_path": str(manifest_path), "_lazy_record": True}


def _managed_folder(folder, store):
    original = Path(folder).absolute()
    if original.is_symlink() or (hasattr(original, "is_junction") and original.is_junction()):
        raise ValueError("不能管理符号链接或目录联接")
    folder = original.resolve()
    store = Path(store).resolve()
    if folder.parent != store or not folder.name.startswith("battle_") or not folder.is_dir():
        raise ValueError("只能管理当前战斗记录目录")
    if any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) for p in folder.rglob("*")):
        raise ValueError("记录含符号链接或目录联接")
    _verified_payload(folder)
    return folder


def rename_record(folder, name, store):
    folder = _managed_folder(folder, store)
    if not isinstance(name, str):
        raise ValueError("记录名称无效")
    name = " ".join(name.split())
    if not name:
        raise ValueError("记录名称不能为空")
    if len(name) > 80:
        raise ValueError("记录名称不能超过 80 个字符")
    temporary = folder / ".record_meta.json.tmp"
    temporary.write_text(json.dumps({"schema": 1, "name": name},
                                    ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    temporary.replace(folder / "record_meta.json")
    return name


def delete_record(folder, store):
    folder = _managed_folder(folder, store)
    shutil.rmtree(folder)
    return folder


def link_settlement(folder, source, store, window, *, expected_sha256,
                    confirmed=False, cancelled=lambda:False):
    """Create a linked record copy only after explicit same-fight confirmation."""
    from yylogs.settlement_summary import load_settlement_summary,MAX_FILE as MAX_SETTLEMENT
    from yylogs.settlement_accounting import LINK_FILE,SCHEMA as LINK_SCHEMA,payload_identity
    if confirmed is not True:raise ValueError('必须明确确认结算日志属于当前战斗')
    folder=_managed_folder(folder,store)
    original=load_record(folder)
    report=original['buff_timeline']
    if window not in observation_windows(report):raise ValueError('请选择当前记录有效的战斗区间')
    source=Path(source)
    if source.is_symlink() or not source.is_file() or source.stat().st_size>MAX_SETTLEMENT:
        raise ValueError('结算文件无效或超限')
    raw=source.read_bytes()
    if len(raw)>MAX_SETTLEMENT or hashlib.sha256(raw).hexdigest()!=expected_sha256:
        raise ValueError('结算来源在确认后发生变化，请重新打开')
    payload=_read_payload(folder)
    with tempfile.TemporaryDirectory(prefix='yylogs-settlement-link-') as temporary:
        staged=Path(temporary)
        (staged/'hook_events.jsonl').write_bytes(raw)
        settlement=load_settlement_summary(staged,report,cancelled=cancelled)
    if settlement['status']!='available':raise ValueError('结算文件未通过当前人物及场景校验')
    latest={s['actor_entity_id']:s for s in settlement['snapshots']}
    actors={actor:s['sequence'] for actor,s in latest.items() if s['skills']}
    if not actors:raise ValueError('没有可合并的结算技能')
    if len(actors)>32:raise ValueError('结算人物数量超限')
    payload['hook_events.jsonl']=raw
    payload[LINK_FILE]=json.dumps({'schema':LINK_SCHEMA,'basis':'user_confirmed_same_encounter',
        'record_payload_sha256':payload_identity(payload),'source_sha256':expected_sha256,
        'own_entity_id':report['own_entity_id'],
        'scene':{k:report['scene'][k] for k in ('scene_id','dungeon_sid')},
        'window':window,'actors':actors},ensure_ascii=False,sort_keys=True).encode('utf-8')
    with tempfile.TemporaryDirectory(prefix='yylogs-settlement-check-') as temporary:
        staged=Path(temporary)
        for name,content in payload.items():(staged/name).write_bytes(content)
        preview=summarize(original['capture_folder'],report,window,settlement_folder=staged,cancelled=cancelled)
        audit=preview['settlement_accounting']
        if audit['status']!='merged' or audit['rejected']:
            raise ValueError('结算技能与已采技能存在冲突，未发布关联记录；原记录未修改')
    if cancelled():raise InterruptedError('结算关联已取消')
    linked=_save_payload(payload,store)
    if original.get('display_name'):
        name=original['display_name']
        if not name.endswith(' · 含结算'):name=name[:72]+' · 含结算'
        rename_record(linked['record_folder'],name,store)
        linked['display_name']=name
    return linked


def _record_staging(store):
    # Windows mkdtemp uses a protected owner-only ACL; an elevated worker's
    # directory then stays unreadable by the unelevated UI after publication.
    for _ in range(8):
        folder = store / ('.import-' + uuid4().hex)
        try:
            folder.mkdir(mode=0o777 if os.name == 'nt' else 0o700)
            return folder
        except FileExistsError:
            continue
    raise FileExistsError('无法创建唯一的战斗记录暂存目录')


def _save_payload(payload, store, *, packet_logs=None):
    manifest = _manifest(payload)
    store = Path(store)
    store.mkdir(parents=True, exist_ok=True)
    destination = store / _record_id(manifest)
    if destination.exists():
        report = load_record(destination)
        if packet_logs:
            from yylogs.packet_battle_logs import copy_logs, finish_transfer
            if not (destination / 'packet_logs').exists():
                staged_logs = destination / ('.packet-logs-' + uuid4().hex)
                try:
                    copy_logs(packet_logs, staged_logs)
                    staged_logs.replace(destination / 'packet_logs')
                finally:
                    if staged_logs.exists():shutil.rmtree(staged_logs)
            finish_transfer(packet_logs, destination)
        return report
    temporary = _record_staging(store)
    try:
        for name, raw in payload.items():
            (temporary / name).write_bytes(raw)
        (temporary / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf8")
        # Validate all parsers before publishing to the records list.
        validation_root = temporary / destination.name
        validation_root.mkdir()
        for path in list(temporary.iterdir()):
            if path.is_file():
                path.replace(validation_root / path.name)
        report = load_record(validation_root)
        if packet_logs:
            from yylogs.packet_battle_logs import copy_logs
            copy_logs(packet_logs, validation_root / 'packet_logs')
        validation_root.replace(destination)
        report = load_record(destination)
        if packet_logs:
            from yylogs.packet_battle_logs import finish_transfer
            finish_transfer(packet_logs, destination)
        return report
    finally:
        shutil.rmtree(temporary)


def import_capture(folder, store, *, include_settlement=True):
    if (Path(folder) / 'packet_capture_result.json').is_file():
        from yylogs.packet_records import import_packet_sessions
        result = _json((Path(folder) / 'packet_capture_result.json').read_bytes())
        if result.get('cleanup_verified') is not True:
            raise ValueError('抓包采集尚未停止或清理未确认')
        ids = import_packet_sessions(Path(folder) / 'packet', store)
        if not ids:
            raise ValueError('未找到有战斗边界的抓包记录')
        return load_record(Path(store) / ids[-1])
    payload = _read_payload(folder)
    state = _json(payload.get("capture_state.json", b"{}"))
    if state.get("active") is not False:
        raise ValueError("请先停止并保存采集")
    controller = _json(payload.get("controller_status.json", b"{}"))
    if controller.get("state") in ("preparing", "recording", "stopping", "worker_lost", "cleanup_unverified"):
        raise ValueError("采集仍在运行或清理尚未核验")
    if not include_settlement:
        payload.pop('hook_events.jsonl', None)
        payload.pop('settlement_link.json', None)
    return _save_payload(payload, store)


def import_file(path, store):
    path = Path(path)
    if path.is_dir():
        return import_capture(path, store)
    if path.stat().st_size > MAX_TOTAL:
        raise ValueError("导入文件超过大小限制")
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            names = [item.filename for item in entries]
            if (len(names) != len(set(names)) or "manifest.json" not in names
                    or not set(names) <= FILES | {"manifest.json"}
                    or sum(item.file_size for item in entries) > MAX_TOTAL
                    or any(item.file_size > MAX_FILE for item in entries)):
                raise ValueError("日志包文件清单无效")
            manifest = _json(archive.read("manifest.json"))
            payload = {name: archive.read(name) for name in names if name != "manifest.json"}
    else:
        value = _json(path.read_bytes())
        if value.get("schema") == TIMELINE_SCHEMA:
            validate_snapshot(value)
            linked = matching_capture(value, store)
            if linked:
                return import_capture(linked, store,
                    include_settlement='hook_events.jsonl' in value['source_sha256'])
            payload = {"timeline_snapshot.json": path.read_bytes()}
            manifest = _manifest(payload)
        elif value.get("schema") == SCHEMA:
            payload = {name: text.encode("utf8") for name, text in value["payload"].items()}
            manifest = {"schema": SCHEMA, "files": value["files"]}
        else:
            raise ValueError("不支持的日志格式；请使用战斗日志包或时间轴 JSON")
    if manifest != _manifest(payload):
        raise ValueError("日志包内容与 SHA256 不一致")
    return _save_payload(payload, store)


def export_record(folder, destination):
    folder, destination = Path(folder), Path(destination)
    payload = _read_payload(folder)
    if 'packet_snapshot.json' in payload:
        report = load_record(folder)
        timeline = report['buff_timeline']
        from yylogs.packet_records import recover_packet_actions
        snapshot = recover_packet_actions(timeline['packet_snapshot'], folder)
        if report.get('packet_observations_recovered') or snapshot is not timeline['packet_snapshot']:
            payload['packet_snapshot.json'] = json.dumps(
                snapshot, ensure_ascii=False, allow_nan=False).encode('utf8')
    else:
        timeline = (validate_snapshot(_json(payload["timeline_snapshot.json"]))
                    if "timeline_snapshot.json" in payload else build_timeline(folder))
    encounter = resolve_dungeon_identity(timeline["scene"].get("dungeon_sid"))
    manifest = _manifest(payload)
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        if destination.suffix.lower() == ".json":
            envelope = {**manifest, "encounter": encounter,
                        "payload": {name: raw.decode("utf8") for name, raw in payload.items()}}
            temporary.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf8")
        else:
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest))
                for name, raw in payload.items():
                    archive.writestr(name, raw)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
