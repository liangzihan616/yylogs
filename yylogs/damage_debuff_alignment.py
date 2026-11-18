"""Offline eligibility checks; matching observations never proves damage completeness/RD.

All identities/times must be supplied by the capture controller, not inferred from
relative chart positions. Does not import any process reader or instrumentation.
"""
from .runtime_scope import SUPPORTED_PVE_SIDS
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class CaptureScope:
    session_id: str
    game_pid: int
    process_creation_ticks: int
    scene_id: str
    dungeon_sid: int
    own_entity_id: str
    target_id: str
    clock: str
    start_unix_ms: float
    end_unix_ms: float

    def __post_init__(self):
        for key in ('session_id', 'scene_id', 'own_entity_id', 'target_id'):
            if not isinstance(getattr(self, key), str) or not getattr(self, key).strip():
                raise ValueError('missing_' + key)
        for key in ('game_pid', 'process_creation_ticks'):
            value = getattr(self, key)
            if type(value) is not int or value <= 0:
                raise ValueError('invalid_' + key)
        if type(self.dungeon_sid) is not int or self.dungeon_sid not in SUPPORTED_PVE_SIDS:
            raise ValueError('unsupported_pve_scene')
        if self.clock != 'host_unix_epoch_ms':
            raise ValueError('unverified_clock_domain')
        for key in ('start_unix_ms', 'end_unix_ms'):
            value = getattr(self, key)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError('invalid_' + key)
        if self.end_unix_ms <= self.start_unix_ms:
            raise ValueError('unordered_capture_window')


def compare_scopes(buff, damage):
    """Report mismatches without filling missing identity or scaling clocks."""
    reasons = []
    if not isinstance(buff, CaptureScope):
        reasons.append('buff_scope_unverified')
    if not isinstance(damage, CaptureScope):
        reasons.append('damage_scope_unverified')
    if reasons:
        return {'eligible': False, 'reasons': reasons, 'rdps_supported': False}
    for key in ('session_id', 'game_pid', 'process_creation_ticks', 'scene_id',
                'dungeon_sid', 'own_entity_id', 'target_id', 'clock'):
        if getattr(buff, key) != getattr(damage, key):
            reasons.append(key + '_mismatch')
    lower = max(buff.start_unix_ms, damage.start_unix_ms)
    upper = min(buff.end_unix_ms, damage.end_unix_ms)
    if lower >= upper:
        reasons.append('no_shared_observation_window')
    return {
        'eligible': not reasons,
        'reasons': reasons,
        'overlap_unix_ms': [lower, upper] if not reasons else None,
        'covers_buff_window': not reasons and damage.start_unix_ms <= buff.start_unix_ms
            and damage.end_unix_ms >= buff.end_unix_ms,
        'exact_hit_timing_verified': False,
        'complete_damage_capture_verified': False,
        'rdps_supported': False,
    }
