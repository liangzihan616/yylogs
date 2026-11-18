"""Quarantined presentation evidence, never imported as independent damage hits."""
import json
import math
from pathlib import Path
from yylogs.runtime_scope import is_supported_pve_sid

SKILLS = frozenset((23700043, 20901210, 23700033, 2050310511))
FIELDS = frozenset(("fromer_id", "target_id", "skill_id", "calcpoint_id", "damage",
                    "damage_type", "harmtext_damage", "harmtext_id"))


def validate_diagnostic(payload, sequence, *, scene=None):
    if (payload.get("kind") != "settlement_diagnostic"
            or type(payload.get("diagnostic_sequence")) is not int
            or payload["diagnostic_sequence"] != sequence or not 1 <= sequence <= 512
            or payload.get("event_identity_bound") is not True
            or payload.get("semantics_verified") is not False
            or payload.get("included_in_damage") is not False
            or payload.get("amount_is_independent_damage_verified") is not False):
        raise ValueError("settlement_diagnostic_contract")
    fields = payload.get("fields")
    if (not isinstance(fields, dict) or set(fields) - FIELDS
            or not {"skill_id", "fromer_id", "target_id"} <= set(fields)
            or not {"damage", "harmtext_damage"}.intersection(fields)
            or type(fields.get("skill_id")) is not int or fields["skill_id"] not in SKILLS):
        raise ValueError("settlement_diagnostic_fields")
    for key, value in fields.items():
        if key in ("fromer_id", "target_id"):
            valid = isinstance(value, str) and 0 < len(value) <= 128
        else:
            valid = type(value) in (int, float) and math.isfinite(value)
        if not valid:
            raise ValueError("settlement_diagnostic_field_type")
    context = payload.get("receiver_context")
    if (not isinstance(context, dict) or context.get("event_name") != "HarmText"
            or context.get("diagnostic_only") is not True
            or context.get("method") not in ("client_sync", "client_sync_by_eid", "client_sync_local")
            or type(context.get("event_id")) is not int or context["event_id"] <= 0
            or context.get("payload_kind") != "kwargs"
            or type(context.get("frame_depth")) is not int or not 0 <= context["frame_depth"] < 6
            or context.get("receiver_identity_verified") is not True
            or not isinstance(context.get("receiver_entity_id"), str)
            or not context["receiver_entity_id"]
            or context.get("receiver_identity_status") != "verified_caller_receiver_instance_id"
            or context.get("damage_target_semantics_verified") is not False):
        raise ValueError("settlement_diagnostic_receiver")
    observed = payload.get("scene")
    if (not isinstance(observed, dict) or set(observed) != {"scene_id", "dungeon_sid"}
            or not isinstance(observed["scene_id"], str) or not observed["scene_id"]
            or not is_supported_pve_sid(observed["dungeon_sid"])
            or (scene is not None and observed != scene)):
        raise ValueError("settlement_diagnostic_scene")
    timestamp = payload.get("observed_unix_ms")
    if type(timestamp) not in (int, float) or not math.isfinite(timestamp) or timestamp < 0:
        raise ValueError("settlement_diagnostic_time")
    return payload


def iter_saved_diagnostics(folder):
    path = Path(folder) / "entry_events.jsonl"
    if not path.is_file():
        return
    sequence = 0
    scope = None
    with path.open(encoding="utf-8-sig") as stream:
        for line in stream:
            if len(line) > 32768:
                raise ValueError("record_byte_bound")
            envelope = json.loads(line)
            message = envelope.get("message", {})
            payload = message.get("payload", {})
            if message.get("type") != "send" or payload.get("kind") != "settlement_diagnostic":
                continue
            sequence += 1
            validate_diagnostic(payload, sequence, scene=scope)
            if scope is None:
                scope = payload["scene"]
            yield payload
