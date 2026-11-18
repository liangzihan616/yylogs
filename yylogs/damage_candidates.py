"""Offline damage candidates; identities and coverage remain unverified."""
from pathlib import Path
import csv,json,math
from yylogs.game_names import skill_metadata
from yylogs.damage_effects import candidate_label
from yylogs.damage_kinds import event_kind
from yylogs.runtime_scope import is_supported_pve_sid
from yylogs.actor_names import actor_rows as _actor_names, source_name_map

FIELDS=('fromer_id','target_id','skill_id','calcpoint_id','damage','damage_type','special_hit','hurt','absorbed_heal','flag')
BINDING_COLUMNS=('event_identity_bound','receiver_method','receiver_event_id','receiver_event_name','receiver_payload_kind','receiver_frame_depth','receiver_entity_id','receiver_identity_verified','receiver_identity_status')
COLUMNS=('sequence','observed_unix_ms','received_at','dungeon_sid','scene_id','source_role','source_name')+BINDING_COLUMNS+FIELDS+('skill_name','skill_name_source','skill_name_configuration_verified','damage_name','damage_name_source','damage_name_evidence','damage_buff_ids','event_kind','event_kind_source')

def iter_candidates(lines):
    """Validate/yield one record at a time; memory does not grow with hits."""
    sequence=0;labels={}
    for line in lines:
        if len(line)>32768:raise ValueError('record_byte_bound')
        envelope=json.loads(line);message=envelope.get('message',{})
        if message.get('type')!='send':continue
        p=message.get('payload',{})
        if p.get('kind')!='damage_candidate':continue
        if p.get('semantics_verified') is not False:raise ValueError('unverified_semantic_claim')
        seq=p.get('sequence')
        if type(seq) is not int or seq!=sequence+1:raise ValueError('collector_delivery_sequence_gap')
        sequence=seq;f=p.get('fields',{});scene=p.get('scene',{})
        if not is_supported_pve_sid(scene.get('dungeon_sid')) or not isinstance(scene.get('scene_id'),str):raise ValueError('unverified_pve')
        if not isinstance(f.get('fromer_id'),str) or type(f.get('skill_id')) is not int or type(f.get('damage')) not in (int,float) or not math.isfinite(f['damage']):raise ValueError('candidate_field_types')
        for key in ('hurt','absorbed_heal','flag'):
            value=f.get(key)
            if value is not None and (type(value) not in (int,float) or not math.isfinite(value) or (key=='flag' and (type(value) is not int or abs(value)>9007199254740991))):raise ValueError('optional_damage_numeric_field')
        role=p.get('source_role')
        if role not in ('own','other_unverified_not_automatically_teammate'):raise ValueError('unverified_party_membership')
        bound=p.get('event_identity_bound',False);receiver=p.get('receiver_context')
        if type(bound) is not bool:raise ValueError('receiver_binding_flag_type')
        if bound:
            if not isinstance(receiver,dict) or receiver.get('method') not in ('client_sync','client_sync_by_eid','client_sync_local') or type(receiver.get('event_id')) is not int or receiver['event_id']<=0 or receiver.get('payload_kind') not in ('args','kwargs') or type(receiver.get('frame_depth')) is not int or not 0<=receiver['frame_depth']<6:raise ValueError('receiver_context_contract')
        elif receiver is not None:raise ValueError('unbound_receiver_context')
        receiver=receiver or {}
        identity_verified=receiver.get('receiver_identity_verified',False)
        entity_id=receiver.get('receiver_entity_id')
        if type(identity_verified) is not bool:raise ValueError('receiver_target_verified_flag')
        if identity_verified:
            if not bound or not isinstance(entity_id,str) or not entity_id or receiver.get('receiver_identity_status')!='verified_caller_receiver_instance_id' or receiver.get('damage_target_semantics_verified') is not False:raise ValueError('receiver_target_identity_contract')
        elif entity_id is not None:raise ValueError('unverified_receiver_entity_id')
        if receiver.get('event_name') is not None and receiver['event_name'] not in ('Behit','BehitWithoutAnim','HpHarmText'):raise ValueError('receiver_event_name')
        row=dict(sequence=seq,observed_unix_ms=p.get('observed_unix_ms'),received_at=envelope.get('received_at'),
            dungeon_sid=scene['dungeon_sid'],scene_id=scene['scene_id'],source_role=role,event_identity_bound=bound,
            receiver_method=receiver.get('method'),receiver_event_id=receiver.get('event_id'),receiver_event_name=receiver.get('event_name'),receiver_payload_kind=receiver.get('payload_kind'),receiver_frame_depth=receiver.get('frame_depth'),receiver_entity_id=entity_id,receiver_identity_verified=identity_verified,receiver_identity_status=receiver.get('receiver_identity_status','not_recorded'),**{k:f.get(k) for k in FIELDS})
        if row['skill_id'] not in labels:labels[row['skill_id']]=skill_metadata(row['skill_id'])
        name=labels[row['skill_id']]
        row.update(skill_name=name['name'],skill_name_source=name['name_source'],skill_name_configuration_verified=name['name_configuration_verified'])
        row.update(candidate_label(row))
        row['event_kind'],row['event_kind_source']=event_kind(row)
        yield row


def extract(lines):
    return list(iter_candidates(lines))


def iter_saved_candidates(folder, *, journal_digest=None):
    path=Path(folder)/"entry_events.jsonl"
    if not path.is_file():return
    names=source_name_map(folder)
    with path.open("rb") as stream:
        def lines():
            for line in stream:
                if journal_digest is not None:journal_digest.update(line)
                yield line.decode("utf-8-sig")
        for row in iter_candidates(lines()):
            row['source_name']=names.get(row['fromer_id'])
            yield row


def load_candidates(folder):
    path=Path(folder)/"entry_events.jsonl"
    if not path.is_file():
        return []
    if path.stat().st_size>32*1024*1024:
        raise ValueError("伤害候选文件超过读取上限")
    with path.open(encoding="utf-8-sig") as stream:
        rows=extract(stream)
    names=source_name_map(folder)
    for row in rows:row['source_name']=names.get(row['fromer_id'])
    return rows


def write_candidate_csv(path,rows):
    with Path(path).open("w",encoding="utf-8-sig",newline="") as stream:
        writer=csv.DictWriter(stream,fieldnames=COLUMNS)
        writer.writeheader();writer.writerows(rows)
