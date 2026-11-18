"""Offline importer for buff lifecycle rows from decoded local PVE logs.

Keeps producer entity, payload eid, source ID, Buff ID, event and charge level
separate. It deliberately does not infer affected damage, duration, coverage or rDPS.
"""
from __future__ import annotations
import csv, hashlib, json
from pathlib import Path

SUPPORTED_EVENTS = {"add_buff", "remove_buff", "buff_level_change"}
MAX_ROWS = 1_000_000

class BuffEventError(ValueError):
    pass

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def import_buff_events(session_path: str | Path, buffs_path: str | Path) -> dict:
    session_path, buffs_path = Path(session_path), Path(buffs_path)
    try:
        session = json.loads(session_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuffEventError(f"invalid session metadata: {exc}") from exc
    metadata = session.get("metadata") or []
    if not metadata or metadata[0].get("config_name") != "PVE_MULTI":
        raise BuffEventError("only explicitly tagged PVE_MULTI local logs are supported")
    if session.get("privacy") == "PVP" or "PVP" in str(session_path).upper():
        raise BuffEventError("PVP input is not supported")
    expected = {"timestamp", "timestamp_iso", "entity_id", "event", "eid", "fromer_id", "buff_no", "new_charge_level"}
    rows = []
    previous = None
    try:
        with buffs_path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames or not expected.issubset(reader.fieldnames):
                raise BuffEventError("buff CSV is missing required raw columns")
            for index, row in enumerate(reader):
                if index >= MAX_ROWS:
                    raise BuffEventError("buff event row limit exceeded")
                event = row["event"]
                if event not in SUPPORTED_EVENTS:
                    continue
                try:
                    timestamp = float(row["timestamp"])
                    buff_no = int(row["buff_no"])
                    charge = int(row["new_charge_level"]) if row["new_charge_level"] else None
                except (TypeError, ValueError) as exc:
                    raise BuffEventError(f"invalid numeric field on CSV row {index + 2}") from exc
                if not (timestamp >= 0 and buff_no >= 0):
                    raise BuffEventError(f"negative timestamp or Buff ID on CSV row {index + 2}")
                if previous is not None and timestamp < previous:
                    raise BuffEventError("buff rows are not chronologically ordered")
                previous = timestamp
                rows.append({
                    "timestamp": timestamp,
                    "timestamp_iso": row["timestamp_iso"],
                    "logger_entity_id": row["entity_id"],
                    "event": event,
                    "payload_eid": row["eid"],
                    "fromer_id": row["fromer_id"],
                    "buff_no": buff_no,
                    "new_charge_level": charge,
                    "source_row": index + 2,
                })
    except OSError as exc:
        raise BuffEventError(f"unable to read buff CSV: {exc}") from exc
    if not rows:
        raise BuffEventError("no supported Buff lifecycle rows found")
    duration = rows[-1]["timestamp"] - rows[0]["timestamp"]
    from collections import Counter
    event_counts = dict(Counter(row["event"] for row in rows))
    producer_entities = sorted({row["logger_entity_id"] for row in rows})
    payload_entities = sorted({row["payload_eid"] for row in rows})
    return {
        "schema": "yylogs.buff-events/1",
        "status": "offline_raw_lifecycle_import",
        "source": {
            "session_path": str(session_path.resolve()),
            "buffs_path": str(buffs_path.resolve()),
            "session_sha256": _sha256(session_path),
            "buffs_sha256": _sha256(buffs_path),
            "pve_config": metadata[0].get("config_name"),
            "session_id": metadata[0].get("session_id"),
            "claimed_role_id": metadata[0].get("role_id"),
            "role_id_independently_verified": False,
        },
        "observed_event_count": len(rows),
        "event_counts": event_counts,
        "distinct_buff_ids": len({row["buff_no"] for row in rows}),
        "distinct_sources": len({row["fromer_id"] for row in rows}),
        "logger_entity_ids": producer_entities,
        "payload_eids": payload_entities,
        "payload_eid_always_equals_logger_entity": all(row["payload_eid"] == row["logger_entity_id"] for row in rows),
        "observed_span_seconds": duration,
        "encounter_start_verified": False,
        "encounter_end_verified": False,
        "uptime_or_coverage_calculated": False,
        "rdps_supported": False,
        "events": rows,
        "limitations": [
            "Preserves local decoded event rows only; capture completeness and encounter boundaries are not established.",
            "logger_entity_id and payload_eid are kept distinct; payload_eid semantics are not inferred.",
            "No Buff interval, coverage, damage impact, credit assignment, or rDPS is inferred.",
        ],
    }

def write_buff_event_report(session_path: str | Path, buffs_path: str | Path, output_path: str | Path) -> dict:
    report = import_buff_events(session_path, buffs_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp = output_path.with_suffix(output_path.suffix + ".tmp")
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(output_path)
    return report
