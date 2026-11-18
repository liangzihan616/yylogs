"""Pure-data Boss-end observations, never whole-dungeon success or combat DPS.

A validated, persistent replicated death property may close a *health window*.
Unavailable living properties are never normalized into an invented state zero.
No process access, game execution, or trust in caller-supplied kill assertions.
"""
from copy import deepcopy
from datetime import datetime
import math

VALIDATED_PROPERTY_CONTRACT = (
    "dead-info-v1:11fe6a50f2683cbba955c6c672b637e181c4461cfc3ad2a5a1d40744532b5e58:"
    "f748718bfff83b0202b9583257c820d6dad22c1b8fb6bbb8824d5afe6af490a1"
)
# Real replicated-death observations currently exist only for SID25.
VALIDATED_DEATH_SIDS = (25,)
MIN_DEAD_SAMPLES = 3
MIN_DEAD_SECONDS = 1.0
MAX_POLL_GAP = 2.5


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


class BossEndObserver:
    def __init__(self, scope):
        self.scope = deepcopy(scope)
        self.previous = None
        self.last_sample = None
        self.last_timestamp = None
        self.candidate = None
        self.living_samples = 0
        self.full_baseline_seen = False
        self.disqualified = False
        self.status = {
            "kind": "boss_end_observation_not_dungeon_clear",
            "classification": "awaiting_observations",
            "kill_verified": False, "dungeon_complete_verified": False,
            "live_validation_required": True, "confirmed_dead_samples": 0,
            "boss_end_observed": False,
        }

    def _clear_candidate(self):
        self.candidate = None
        self.status.update(confirmed_dead_samples=0, boss_end_observed=False)
        self.status.pop("confirmed_at", None)

    def observe(self, sample, *, recording_started):
        moment = datetime.fromisoformat(sample["timestamp"])
        if moment.utcoffset() is None:
            raise ValueError("end observation requires timezone")
        if self.last_timestamp and moment <= self.last_timestamp:
            raise ValueError("non-increasing end timestamp")
        for key in ("dungeon_sid", "space_id", "target_id"):
            if sample.get(key) != self.scope[key]:
                self.disqualified = True
                self._clear_candidate()
                raise ValueError("end observation scope mismatch")
        for key in ("game_pid", "process_creation_ticks"):
            if key in sample and key in self.scope and sample[key] != self.scope[key]:
                self.disqualified = True
                self._clear_candidate()
                raise ValueError("end observation process mismatch")
        current = sample.get("death_properties")
        previous = self.previous
        gap = (moment-self.last_timestamp).total_seconds() if self.last_timestamp else None
        contiguous = gap is not None and 0 < gap <= MAX_POLL_GAP
        hp, maximum = sample.get("hp"), sample.get("hp_max")
        health_valid = (sample.get("boss_identity_verified") is True
                        and sample.get("attribute_representation_verified") is True
                        and sample.get("game_function_called") is not True
                        and number(hp) and number(maximum) and 0 <= hp <= maximum and maximum > 0)
        phase_change = bool(self.last_sample and maximum != self.last_sample.get("hp_max"))
        reset = bool(self.last_sample and recording_started and health_valid
                     and 0 < self.last_sample.get("hp", 0) < maximum and hp == maximum)
        if phase_change or reset:
            self.disqualified = True
        if not contiguous or not health_valid or phase_change or reset:
            self.living_samples = 0
            self.full_baseline_seen = False
            self._clear_candidate()
        valid = (isinstance(current, dict) and current.get("runtime_verified") is True
                 and current.get("status") == "replicated_fields_observed_not_kill_verification"
                 and isinstance(current.get("contract_id"), str) and bool(current["contract_id"])
                 and all(current.get(k) == self.scope[k] for k in ("dungeon_sid", "space_id", "target_id"))
                 and type(current.get("dead_state")) is int and current["dead_state"] in (0, 1, 2)
                 and number(current.get("dead_timestamp")) and current["dead_timestamp"] >= 0)
        # Only an explicitly unavailable, same-contract property can form a lazy
        # appearance edge. Malformed/untrusted fields are not 'living defaults'.
        def lazy_unavailable(fields):
            return (isinstance(fields, dict) and fields.get("status") == "unavailable"
                    and fields.get("runtime_verified") is False
                    and fields.get("dead_state") is None and fields.get("dead_timestamp") is None
                    and isinstance(fields.get("contract_id"), str) and bool(fields["contract_id"])
                    and all(fields.get(k) == self.scope[k] for k in ("dungeon_sid", "space_id", "target_id")))
        category = "death_fields_unavailable"
        if not valid:
            self.previous = None
            self._clear_candidate()
        else:
            state, stamp = current["dead_state"], current["dead_timestamp"]
            same = bool(previous and previous["contract_id"] == current["contract_id"])
            prior_fields = (self.last_sample or {}).get("death_properties")
            lazy_edge = (contiguous and self.living_samples >= 2 and self.full_baseline_seen
                         and lazy_unavailable(prior_fields)
                         and prior_fields["contract_id"] == current["contract_id"]
                         and self.last_sample["hp"] > 0 and health_valid and hp == 0)
            explicit_edge = (same and contiguous and previous["dead_state"] == 0
                             and previous["dead_timestamp"] == 0)
            if state == 0:
                category = "alive_or_cleared_not_kill"
                self._clear_candidate()
            elif state == 2:
                category = "reviving_not_verified_kill"
                self.disqualified = True
                self._clear_candidate()
            elif stamp <= 0:
                category = "dead_state_without_positive_timestamp_not_verified"
                self._clear_candidate()
            elif recording_started and not self.disqualified and (explicit_edge or lazy_edge):
                self.candidate = {
                    "lower": self.last_timestamp.isoformat(), "upper": sample["timestamp"],
                    "contract_id": current["contract_id"], "dead_state": 1, "dead_timestamp": stamp,
                    "kind": ("replicated_dead_property_appearance_after_living_hp_candidate" if lazy_edge
                             else "replicated_dead_state_transition_candidate"),
                    "living_state_zero_observed": not lazy_edge,
                    "living_positive_hp_samples": self.living_samples,
                    "full_health_baseline_observed": self.full_baseline_seen,
                    "timestamp_units_verified": False,
                    "timing": "poll_interval_not_exact_death_time",
                    "schema_confirmed_samples": 0,
                }
                self.status.update(confirmed_dead_samples=1)
                category = ("death_property_appearance_candidate_needs_persistence" if lazy_edge
                            else "death_transition_candidate_needs_live_validation")
            elif (recording_started and not self.disqualified and same and contiguous and self.candidate
                  and stamp == self.candidate["dead_timestamp"] and previous["dead_state"] == 1):
                self.status["confirmed_dead_samples"] += 1
                category = "persistent_death_candidate_needs_live_validation"
            else:
                category = "dead_seen_without_contiguous_alive_baseline_not_verified"
                self._clear_candidate()
            self.previous = deepcopy(current)
            if self.candidate:
                proof = sample.get("death_contract_observation") or {}
                schema_ok = (health_valid and hp == 0 and current["contract_id"] == VALIDATED_PROPERTY_CONTRACT
                             and proof.get("contract_id") == current["contract_id"]
                             and proof.get("server_id") == self.scope["target_id"]
                             and all(proof.get(k) is True for k in ("compiler_verified", "runtime_verified", "property_schema_verified"))
                             and proof.get("game_function_called") is False)
                self.candidate["schema_confirmed_samples"] = (
                    self.candidate["schema_confirmed_samples"] + 1 if schema_ok else 0)
                persistence = (moment-datetime.fromisoformat(self.candidate["upper"])).total_seconds()
                observed = (schema_ok and self.full_baseline_seen and self.candidate["living_positive_hp_samples"] >= 2
                            and self.status["confirmed_dead_samples"] >= MIN_DEAD_SAMPLES
                            and self.candidate["schema_confirmed_samples"] >= MIN_DEAD_SAMPLES
                            and persistence >= MIN_DEAD_SECONDS)
                self.status["boss_end_observed"] = bool(observed)
                if observed:
                    category = "boss_dead_properties_persisted_health_window_only"
                    self.status.setdefault("confirmed_at", sample["timestamp"])
                else:
                    self.status.pop("confirmed_at", None)
        # Remember positive-HP observations without inventing death-property zeros.
        alive_fields = lazy_unavailable(current) or (valid and current["dead_state"] == 0 and current["dead_timestamp"] == 0)
        if health_valid and hp > 0 and alive_fields and not self.disqualified:
            if self.last_sample:
                prior = self.last_sample.get("death_properties") or {}
                if prior.get("contract_id") != current["contract_id"]:
                    self.living_samples = 0
                    self.full_baseline_seen = False
            self.living_samples += 1
            if hp == maximum:
                self.full_baseline_seen = True
        elif not (valid and current["dead_state"] == 1 and self.candidate):
            self.living_samples = 0
            self.full_baseline_seen = False
        self.last_timestamp = moment
        self.last_sample = deepcopy(sample)
        self.status.update(classification=category, observed_at=sample["timestamp"],
                           candidate=deepcopy(self.candidate), hp_zero_observed=hp == 0,
                           kill_verified=False, dungeon_complete_verified=False,
                           live_validation_required=not self.status["boss_end_observed"])
        return deepcopy(self.status)

    def stop(self, reason):
        reason = str(reason)
        if "returned_to_full" in reason:
            category = "possible_reset_not_kill"
        elif "phase" in reason:
            category = "phase_change_not_kill"
        elif "process_identity" in reason:
            category = "process_identity_changed_not_kill"
        elif "scene" in reason or "dungeon" in reason:
            category = "scene_exit_or_scope_change_not_dungeon_clear"
        elif "identity_missing" in reason or "death_runtime_npc_identity" in reason:
            category = "boss_unavailable_not_kill"
        elif "expir" in reason or "timeout" in reason:
            category = "observation_timeout_not_kill"
        else:
            category = "observation_stopped_without_verified_kill"
        self.status.update(stop_classification=category, stop_reason=reason,
                           kill_verified=False, dungeon_complete_verified=False)
        return deepcopy(self.status)
