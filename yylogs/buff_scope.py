"""Offline selection contract for the future Buff collector, not a live hook.
Source attribution is independent of recipient filtering. Only an upstream,
verified entity identity may populate own_entity_id or boss_entity_ids.
"""
from dataclasses import dataclass

@dataclass(frozen=True)
class BuffScopeDecision:
    collect: bool
    category: str | None
    reason: str
    source_entity_id: str | None = None
    source_status: str = "unknown"

@dataclass(frozen=True)
class BuffCaptureScope:
    own_entity_id: str
    boss_entity_ids: frozenset[str]
    scene_id: str

    def __post_init__(self):
        if any(not isinstance(x, str) or not x.strip() for x in (self.own_entity_id, self.scene_id)):
            raise ValueError("own entity and scene must be explicit nonempty IDs")
        if not isinstance(self.boss_entity_ids, frozenset):
            raise ValueError("boss IDs must be an immutable explicit set")
        if any(not isinstance(x, str) or not x.strip() for x in self.boss_entity_ids):
            raise ValueError("invalid boss entity ID")
        if self.own_entity_id in self.boss_entity_ids:
            raise ValueError("local player cannot be a boss")

    def select(self, *, scene_id, pve_verified, target_entity_id, buff_estimate=None, source_entity_id=None):
        """Apply before journaling any payload; do not filter by the caster.

        self_buff intentionally includes every status on self. Boss records need
        an explicitly decoded/static-mapped buff_estimate; an unknown/absent
        value is not proof of a debuff. Caller retains the source ID unchanged,
        including a missing source. A reported source is not independently verified.
        This helper does not verify game identities,
        decode native payloads, or establish any runtime coverage.
        """
        if source_entity_id is not None and (not isinstance(source_entity_id, str) or not source_entity_id.strip()):
            raise ValueError("source must be an explicit nonempty entity ID or None")
        source_status = "reported_unverified" if source_entity_id is not None else "unknown"
        if pve_verified is not True:
            return BuffScopeDecision(False, None, "pve_not_verified")
        if scene_id != self.scene_id:
            return BuffScopeDecision(False, None, "scene_changed")
        if not isinstance(target_entity_id, str) or not target_entity_id:
            return BuffScopeDecision(False, None, "target_unresolved")
        if target_entity_id == self.own_entity_id:
            return BuffScopeDecision(True, "self_buff", "local_player_recipient", source_entity_id, source_status)
        if target_entity_id not in self.boss_entity_ids:
            return BuffScopeDecision(False, None, "recipient_out_of_scope")
        if type(buff_estimate) is not int:
            return BuffScopeDecision(False, None, "boss_effect_classification_unknown")
        if buff_estimate != 2:
            return BuffScopeDecision(False, None, "boss_effect_not_debuff")
        return BuffScopeDecision(True, "boss_debuff", "verified_boss_debuff", source_entity_id, source_status)
