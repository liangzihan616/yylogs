"""Offline bridge from the existing encounter recorder to Buff views.

Does not discover live signals or certify adapter assertions. Only a same-process,
same-scene recorder with observed start and a supported end can bound the chart.
Capture timeout, unknown Buff IDs, target lock and inactivity are not boundaries.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import math

class EncounterTimelineError(ValueError):
    pass


TARGET_UNAVAILABLE_OPERATIONS = frozenset((
    "death_runtime_npc_identity",
    "death_runtime_npc_changed",
    "live_boss_local_identity_missing",
    "live_boss_server_identity_missing",
    "live_boss_required_property_unavailable",
    "live_boss_end_identity_changed",
    "live_boss_path_changed",
))

LEGACY_TARGET_UNAVAILABLE_OPERATIONS = frozenset((
    "live_boss_local_identity_missing",
    "live_boss_server_identity_missing",
))


def _controller_target_unavailable(controller):
    """Return a verified post-start target-loss operation, or None."""
    if not isinstance(controller, dict):
        return None
    error = controller.get("boundary_error")
    if error in TARGET_UNAVAILABLE_OPERATIONS:
        return error
    failure = controller.get("boundary_preflight_failure")
    if not isinstance(failure, dict):
        return None
    operation = failure.get("failed_operation")
    if (error == "boss_identity_not_loaded"
            and failure.get("status") == "stopped_on_failure"
            and failure.get("preflight_stage") == "verified_observer"
            and failure.get("handle_closed") is True
            and operation in LEGACY_TARGET_UNAVAILABLE_OPERATIONS):
        return operation
    return None


def _controller_scope_matches(controller, report, state, scene, identity):
    if not (isinstance(controller.get("damage_channel"), dict)
            and isinstance(controller.get("joint_capture_scope"), dict)):
        return False
    joint = controller["joint_capture_scope"]
    damage = controller["damage_channel"]
    expected = {
        "game_pid": identity.get("game_pid"),
        "process_creation_ticks": identity.get("process_creation_ticks"),
        "scene_id": scene["scene_id"],
        "dungeon_sid": scene["dungeon_sid"],
        "own_entity_id": report["own_entity_id"],
        "target_id": state.get("target_id"),
    }
    if (controller.get("own_entity_id") != expected["own_entity_id"]
            or controller.get("dungeon_sid") != expected["dungeon_sid"]
            or joint.get("schema") != "yylogs.joint-capture-scope/1"
            or any(joint.get(key) != value for key, value in expected.items())
            or damage.get("session_id") != joint.get("session_id")
            or any(damage.get(key) != value for key, value in expected.items())):
        return False
    return True


def unix_ms(value):
    if not isinstance(value, str):
        raise EncounterTimelineError("missing boundary timestamp")
    try:
        moment = datetime.fromisoformat(value)
        if moment.utcoffset() is None:
            raise ValueError("timezone required")
        return moment.timestamp() * 1000
    except (ValueError, OverflowError) as exc:
        raise EncounterTimelineError("invalid boundary timestamp") from exc

def window_from_recorder(report, state, controller=None):
    """Use existing recorder evidence, never an unrelated historic fight.

    Bounds are observation intervals, not exact hit/death timestamps. Use the
    outer interval for visualization; retain both uncertainties for inspection.
    This still does not certify complete Buff or damage-event coverage.
    """
    scene = report["scene"]
    scope = state.get("observation_scope") or {}
    identity = report.get("capture_identity") or {}
    if (state.get("schema") != 2 or state.get("pve_only") is not True
            or state.get("entity") != report["own_entity_id"]
            or state.get("space_id") != scene["scene_id"]
            or state.get("dungeon_sid") != scene["dungeon_sid"]):
        raise EncounterTimelineError("encounter recipient or scene mismatch")
    for key in ("game_pid", "process_creation_ticks"):
        if type(identity.get(key)) is not int or identity[key] <= 0 or scope.get(key) != identity[key]:
            raise EncounterTimelineError("encounter process identity mismatch")
    if any(scope.get(k) != state.get(k) for k in ("space_id", "dungeon_sid", "target_id")) or not state.get("target_id"):
        raise EncounterTimelineError("encounter observation scope mismatch")
    partial_reason = state.get("reason") in (
        "manual_stop_not_boss_death",
        "target_unavailable_not_verified_death",
        "settlement_final_observed",
    )
    controller_operation = _controller_target_unavailable(controller)
    record_budget_stop = (
        isinstance(controller, dict)
        and controller.get("state") == "failed"
        and controller.get("host_stop_reason") == "agent_stopped"
        and (controller.get("damage_channel") or {}).get("agent_stop", {}).get("reason") == "record_budget"
    )
    controller_partial_stop = (
        isinstance(controller, dict)
        and controller.get("cleanup_verified") is True
        and ((controller.get("state") == "saved"
              and controller.get("host_stop_reason") == "boss_boundary_failed"
              and controller_operation in TARGET_UNAVAILABLE_OPERATIONS)
             or record_budget_stop)
        and _controller_scope_matches(controller, report, state, scene, identity)
    )
    partial = (state.get("state") == "interrupted"
               and (partial_reason or controller_partial_stop)
               and not state.get("gaps") and not state.get("end")
               and not state.get("kill_verified") and not state.get("boss_end_observed"))
    if (state.get("state") != "finished" and not partial) or state.get("start_observed") is not True:
        raise EncounterTimelineError("encounter start or end unverified")
    start, end = state.get("start") or {}, state.get("end") or {}
    if start.get("kind") not in ("observed_first_boss_damage", "observed_boss_hp_drop", "observed_combat_transition"):
        raise EncounterTimelineError("unsupported encounter start evidence")
    if partial:
        last = state.get("last_timestamp")
        if partial_reason:
            reason = state.get("reason")
        elif (controller.get("damage_channel") or {}).get("agent_stop", {}).get("reason") == "record_budget":
            reason = "damage_candidate_budget_exhausted"
        else:
            reason = "boss_boundary_observer_failed"
        end = {"lower": last, "upper": last, "kind": "last_successful_observation_not_death",
               "evidence": {"reason": reason, "last_sample": last,
                            "boundary_error": controller.get("boundary_error") if controller_partial_stop else None,
                            "boundary_operation": controller_operation if controller_partial_stop else None,
                            "preflight_stage": (
                                (controller.get("boundary_preflight_failure") or {}).get("preflight_stage")
                                if controller_partial_stop else None)}}
    # Reuse the existing validated death observer, never promote hp==0 alone.
    health_end = (state.get("boss_end_observed") is True
                  and end.get("kind") == "boss_health_window_end_not_dungeon_clear"
                  and (state.get("end_observation") or {}).get("boss_end_observed") is True
                  and bool(end.get("evidence")))
    kill_end = state.get("kill_verified") is True and bool(end.get("evidence"))
    if not (health_end or kill_end or partial):
        raise EncounterTimelineError("timeout, manual stop or idle is not a verified end")
    times = [unix_ms(v) for v in (start.get("lower"), start.get("upper"), end.get("lower"), end.get("upper"))]
    origin, capture_end = unix_ms(report["origin"]), unix_ms(report["end"])
    if not origin <= times[0] <= times[1] <= times[2] <= times[3] <= capture_end or times[0] == times[3]:
        raise EncounterTimelineError("encounter outside capture or unordered boundaries")
    return {
        "id": str(scope.get("session_id") or start.get("encounter_id") or state["target_id"]),
        "kind": "partial_manual_observation" if partial else ("boss_health_observation_window" if health_end else "encounter_observation_window"),
        "boundary_source": "existing_encounter_recorder", "start_verified": True, "end_verified": not partial,
        "partial_observation": partial, "observation_end_verified": True,
        "target_id": state["target_id"], "own_entity_id": report["own_entity_id"],
        "scene": deepcopy(scene), "capture_identity": deepcopy(identity),
        "source_sha256": deepcopy(report["source_sha256"]),
        "start_ms": times[0]-origin, "end_ms": times[3]-origin,
        "start_interval_ms": [times[0]-origin, times[1]-origin],
        "end_interval_ms": [times[2]-origin, times[3]-origin],
        "start_evidence": deepcopy(start), "end_evidence": deepcopy(end),
        "kill_verified": bool(kill_end), "dungeon_complete_verified": False,
        "complete_encounter_verified": False, "exact_boundary_timing_verified": False,
        "coverage_calculated": False, "rdps_supported": False,
    }


def refine_partial_health_window(report, state, window, frames):
    """Bound a partial HP observation at persistent zero, without certifying death.

    Verify the complete same-session journal, including later samples: a revive,
    reset, scope change or missing poll must not turn zero HP into a fight end.
    """
    from .live import verified_boss_health
    from .encounter_end import MIN_DEAD_SAMPLES, MIN_DEAD_SECONDS, MAX_POLL_GAP
    if (not window.get("partial_observation") or state.get("start_mode") != "boss_health"
            or report.get("cleanup_verified") is not True or state.get("gaps")):
        return window
    origin = unix_ms(report["origin"])
    start = window["start_interval_ms"]
    scope = state["observation_scope"]
    previous = None
    previous_ms = None
    previous_stamp = None
    candidate = None
    count = zero_count = 0
    start_seen = False
    for frame in frames:
        count += 1
        if not isinstance(frame, dict):
            raise EncounterTimelineError("invalid health journal frame")
        health = verified_boss_health(frame)
        if (health is None or health.get("observation_scope") != scope
                or frame.get("entity") != report["own_entity_id"]
                or health["encounter_id"] != state.get("health_encounter_id")
                or health["source"] != state.get("health_source")
                or health["hp_max"] != state.get("health_max") or health["hp"] < 0):
            raise EncounterTimelineError("health journal scope, source or phase mismatch")
        moment = unix_ms(frame.get("timestamp")) - origin
        if not 0 <= moment <= window["end_ms"]:
            raise EncounterTimelineError("health journal outside observation")
        if previous_ms is not None and not 0 < moment - previous_ms <= MAX_POLL_GAP * 1000:
            raise EncounterTimelineError("health journal has unordered or missing polls")
        if (previous_ms == start[0] and moment == start[1]
                and previous["hp"] == previous["hp_max"]
                and previous.get("pre_pull_baseline_verified") is True
                and health["hp"] < health["hp_max"]):
            start_seen = True
        if (frame.get("death_properties") or {}).get("dead_state") == 2:
            return window
        if candidate and health["hp"] > 0:
            return window
        if health["hp"] == 0 and start_seen:
            if candidate is None and previous is not None and previous["hp"] > 0:
                candidate = {"lower_ms": previous_ms, "upper_ms": moment,
                             "lower": previous_stamp, "upper": frame["timestamp"],
                             "hp_before": previous["hp"]}
            if candidate:
                zero_count += 1
        elif (start_seen and previous and previous["hp"] < health["hp_max"]
              and health["hp"] == health["hp_max"]):
            return window
        previous = health
        previous_ms = moment
        previous_stamp = frame["timestamp"]
    if (previous is None or count != state.get("samples") or previous_stamp != state.get("last_timestamp")
            or previous["hp"] != state.get("last_boss_hp")):
        raise EncounterTimelineError("health journal does not match recorder tail")
    if (not candidate or zero_count < MIN_DEAD_SAMPLES
            or previous_ms - candidate["upper_ms"] < MIN_DEAD_SECONDS * 1000
            or candidate["upper_ms"] >= window["end_ms"]):
        return window
    result = deepcopy(window)
    result["hp_depletion"] = {
        **candidate, "zero_samples": zero_count, "confirmed_at": previous_stamp,
        "original_observation_end_ms": window["end_ms"],
        "excluded_tail_ms": window["end_ms"] - candidate["upper_ms"],
        "kill_verified": False, "game_settlement_timing_verified": False}
    result["end_ms"] = candidate["upper_ms"]
    result["end_interval_ms"] = [candidate["lower_ms"], candidate["upper_ms"]]
    result["end_evidence"] = {
        "lower": candidate["lower"], "upper": candidate["upper"],
        "kind": "persistent_zero_hp_observation_not_death",
        "evidence": {**deepcopy(window["end_evidence"]["evidence"]),
                     "persistent_zero_hp": deepcopy(result["hp_depletion"])}}
    return result


def slice_encounter(report, window):
    """Clip/rebase copies, never alter capture evidence or invent applications."""
    partial_reason = (window.get("end_evidence", {}).get("evidence") or {}).get("reason")
    partial_evidence = window.get("end_evidence", {}).get("evidence") or {}
    partial = (window.get("kind") == "partial_manual_observation"
               and window.get("partial_observation") is True
               and window.get("end_verified") is False
               and window.get("observation_end_verified") is True
               and (window.get("end_evidence") or {}).get("kind") in (
                   "last_successful_observation_not_death", "persistent_zero_hp_observation_not_death")
               and (partial_reason in ("manual_stop_not_boss_death", "target_unavailable_not_verified_death",
                                       "settlement_final_observed")
                    or (partial_reason == "boss_boundary_observer_failed"
                        and (partial_evidence.get("boundary_error") in TARGET_UNAVAILABLE_OPERATIONS
                             or (partial_evidence.get("boundary_error") == "boss_identity_not_loaded"
                                 and partial_evidence.get("boundary_operation") in LEGACY_TARGET_UNAVAILABLE_OPERATIONS
                                 and partial_evidence.get("preflight_stage") == "verified_observer")))
                    or partial_reason == "damage_candidate_budget_exhausted"))
    if window.get("boundary_source") != "existing_encounter_recorder" or window.get("start_verified") is not True or not (window.get("end_verified") is True or partial):
        raise EncounterTimelineError("verified recorder boundaries required")
    if any(window.get(k) != report.get(k) for k in ("scene", "own_entity_id", "capture_identity", "source_sha256")):
        raise EncounterTimelineError("window belongs to another capture")
    lower, upper = window.get("start_ms"), window.get("end_ms")
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in (lower,upper)) or not 0 <= lower < upper <= report["duration_ms"]:
        raise EncounterTimelineError("invalid encounter range")
    # Large event journals are rebuilt below; copy only retained metadata here.
    rebuilt = {"tracks", "observations", "boss_debuff", "boss_buff_events"}
    result = deepcopy({key: value for key, value in report.items() if key not in rebuilt})
    for key in ("boss_debuff", "boss_buff_events"):
        if key in report and not report[key]:
            result[key] = deepcopy(report[key])
    result.update(timeline_kind="partial_manual_observation" if partial else "encounter_observation", duration_ms=upper-lower,
                  capture_origin=report["origin"], capture_duration_ms=report["duration_ms"],
                  selected_encounter=deepcopy(window), encounter_start_verified=True, encounter_end_verified=not partial, partial_observation=partial,
                  complete_encounter_verified=False, coverage_calculated=False, rdps_supported=False)
    origin = datetime.fromisoformat(report["origin"])
    result.update(origin=(origin+timedelta(milliseconds=lower)).isoformat(),
                  end=(origin+timedelta(milliseconds=upper)).isoformat(),
                  limitations=["零点是开战观测区间下界，不是精确服务器开战时间。", "结束按观测边界裁切，不表示 Buff 移除；不计算覆盖率或 rDPS。"]+report.get("limitations",[])[1:])
    if partial:
        reason = window.get("end_evidence", {}).get("evidence", {}).get("reason")
        label = {"target_unavailable_not_verified_death": "目标不可读",
                 "settlement_final_observed": "副本最终结算自动保存",
                 "boss_boundary_observer_failed": "Boss 身份观测失败",
                 "damage_candidate_budget_exhausted": "伤害候选达到安全上限",
                 "manual_stop_not_boss_death": "手动停止"}.get(reason, "观测中断")
        result["limitations"].insert(0, f"{label}观测片段。结束取最后一次成功观测；Boss 死亡和完整战斗用时未验证。")
        if window.get("hp_depletion"):
            result["limitations"][0] = "HP 持续归零观测片段，终点取首次归零采样上界；击杀和游戏结算用时未验证。"
    result["tracks"] = []
    for track in report["tracks"]:
        kept = []
        for segment in track["segments"]:
            a,b = segment["start_ms"],segment["end_ms"]
            if b <= lower or a >= upper:
                continue
            seg = deepcopy(segment)
            seg.update(start_ms=max(a,lower)-lower, end_ms=min(b,upper)-lower,
                       capture_start_ms=a, capture_end_ms=b,
                       inherited_at_window_start=a < lower, clipped_at_window_end=b > upper,
                       original_close_reason=segment["close_reason"])
            seg["observed_span_ms"] = seg["end_ms"]-seg["start_ms"]
            if "last_seen_ms" in seg:
                seg["capture_last_seen_ms"]=seg["last_seen_ms"]
                seg["last_seen_ms"]=seg["last_seen_ms"]-lower
            for name in ("start_observation_range_ms", "end_observation_range_ms"):
                if seg.get(name) is not None:
                    seg[name] = [v-lower for v in seg[name]]
            if a < lower:
                seg.update(left_censored=True,start_sequence=None,start_observation_range_ms=None)
            if b > upper:
                seg.update(right_censored=True,end_sequence=None,end_observation_range_ms=None,
                           close_reason="encounter_window_clipped_not_buff_removed")
            kept.append(seg)
        if kept:
            copied=deepcopy({key:value for key,value in track.items() if key!="segments"})
            copied["segments"]=kept;result["tracks"].append(copied)
    result["observations"] = [dict(deepcopy(row),time_ms=row["time_ms"]-lower,capture_time_ms=row["time_ms"])
                              for row in report["observations"] if lower <= row["time_ms"] <= upper]
    if report.get("boss_debuff"):
        boss=deepcopy({key:value for key,value in report["boss_debuff"].items() if key!="tracks"})
        boss["tracks"]=[deepcopy(t) for t in result["tracks"] if t["channel"]=="boss_sampled"]
        boss["gaps"]=[{**g,"start_ms":max(lower,g["start_ms"])-lower,"end_ms":min(upper,g["end_ms"])-lower} for g in boss.get("gaps",[]) if g["end_ms"]>=lower and g["start_ms"]<=upper]
        boss["unobserved_intervals"]=[{**g,"start_ms":max(lower,g["start_ms"])-lower,"end_ms":min(upper,g["end_ms"])-lower} for g in boss.get("unobserved_intervals",[]) if g["end_ms"]>lower and g["start_ms"]<upper]
        boss["partial_observations"]=[{**g,"capture_observed_ms":g["observed_ms"],
            "observed_ms":g["observed_ms"]-lower} for g in boss.get("partial_observations",[])
            if lower <= g["observed_ms"] <= upper]
        boss["partial_snapshots"]=len(boss["partial_observations"])
        result["boss_debuff"]=boss
    if report.get("boss_buff_events"):
        events=deepcopy({key:value for key,value in report["boss_buff_events"].items()
                         if key not in ("tracks","observations")})
        events["tracks"]=[deepcopy(t) for t in result["tracks"] if t["channel"]=="boss_events"]
        events["observations"]=[deepcopy(r) for r in result["observations"] if r["channel"]=="boss_events"]
        events["gaps"]=[{**g,"start_ms":max(lower,g["start_ms"])-lower,"end_ms":min(upper,g["end_ms"])-lower}
                        for g in events.get("gaps",[]) if g["end_ms"]>lower and g["start_ms"]<upper]
        events["event_count"]=len(events["observations"])
        result["boss_buff_events"]=events
    result["counts"].update(tracks=len(result["tracks"]),segments=sum(len(t["segments"]) for t in result["tracks"]))
    return result
