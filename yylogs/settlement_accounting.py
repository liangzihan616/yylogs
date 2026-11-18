"""Same-capture or explicitly confirmed links and cumulative replacement."""
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path

LINK_FILE='settlement_link.json'
SCHEMA='yylogs.settlement-link/1'
AUTO_BASIS='same_capture_final_rpc'


def automatic_link(folder, report, settlement, windows):
    """Use only a clean joint capture stopped by its own final RPC batch."""
    missing={'status':'missing','actors':{},'basis':None}
    try:
        if settlement.get('status')!='available' or report.get('cleanup_verified') is not True or len(windows)!=1:
            return missing
        from yylogs.encounter_timeline import unix_ms,window_from_recorder
        def read(name):
            path=Path(folder)/name
            if path.is_symlink() or path.stat().st_size>262144:raise ValueError('invalid capture evidence')
            return json.loads(path.read_text('utf-8-sig'))
        controller=read('controller_status.json')
        state=read('live_status.json')
        scope=read('joint_capture_scope.json')
        damage=read('damage_capture_result.json')
        channel=controller.get('settlement_channel') or {}
        if (read('capture_state.json').get('active') is not False
                or controller.get('state')!='saved' or controller.get('cleanup_verified') is not True
                or controller.get('host_stop_reason')!='settlement_final_observed'
                or state.get('reason')!='settlement_final_observed'
                or scope.get('schema')!='yylogs.joint-capture-scope/1'
                or scope.get('manual_joint_capture') is not True
                or not isinstance(scope.get('session_id'),str) or not scope['session_id']
                or channel.get('schema')!='yylogs.manual-settlement-channel/1'
                or channel.get('phase')!='finished'
                or channel.get('postcombat_end')!='settlement_final_received'
                or any(channel.get(k) is not True for k in ('cleanup_verified','script_unloaded',
                    'parent_session_detached','shared_dispatch_verified_by_parent'))):
            return missing
        window=windows[0]
        if window_from_recorder(report,state,controller)!=window:return missing
        expected={'game_pid':report['capture_identity']['game_pid'],
            'process_creation_ticks':report['capture_identity']['process_creation_ticks'],
            'own_entity_id':report['own_entity_id'],'scene_id':report['scene']['scene_id'],
            'dungeon_sid':report['scene']['dungeon_sid'],'target_id':window['target_id'],
            'session_id':scope['session_id']}
        if any(source.get(k)!=v for source in (scope,damage,controller.get('damage_channel') or {})
               for k,v in expected.items()):return missing
        if (controller.get('own_entity_id')!=report['own_entity_id']
                or controller.get('dungeon_sid')!=report['scene']['dungeon_sid']):return missing
        for name,digest in report['source_sha256'].items():
            if hashlib.sha256((Path(folder)/name).read_bytes()).hexdigest()!=digest:return missing
        snapshots=settlement['snapshots']
        finals=[s for s in snapshots if s.get('final_settlement') is True]
        if (not finals or channel.get('snapshots')!=len(snapshots)
                or channel.get('final_settlement_snapshots')!=len(finals)):return missing
        origin=unix_ms(report['origin'])
        lower=origin+window['end_ms']
        upper=unix_ms(report['end'])
        times=[s['observed_unix_ms'] for s in finals]
        if (any(not lower<=t<=upper for t in times) or max(times)-min(times)>3000
                or len({s['duration_candidate'] for s in finals})!=1):return missing
        duration=finals[0]['duration_candidate']
        if duration is None or not 0<duration<=600:return missing
        start_low,start_high=window['start_interval_ms']
        if any(not origin+start_low-2500<=t-float(duration)*1000<=origin+start_high+2500
               for t in times):return missing
        actors={}
        for snapshot in finals:
            actor=snapshot['actor_entity_id']
            if actor in actors and actors[actor]['all_damage_skills']!=snapshot['all_damage_skills']:return missing
            actors[actor]=snapshot
        return {'status':'confirmed','basis':AUTO_BASIS,'actors':actors,'window':window,
                'independently_verified':False}
    except (OSError,ValueError,TypeError,KeyError,AttributeError,RecursionError):
        return missing


def payload_identity(payload):
    files={name:hashlib.sha256(raw).hexdigest() for name,raw in payload.items()
           if name not in ('hook_events.jsonl',LINK_FILE)}
    return hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def read_link(folder, report, settlement, windows):
    path=Path(folder)/LINK_FILE
    result={'status':'missing','actors':{},'basis':None}
    if not path.exists():return automatic_link(folder,report,settlement,windows)
    try:
        from yylogs.battle_records import _read_payload
        if path.is_symlink() or path.stat().st_size>65536:raise ValueError('关联凭据文件无效')
        value=json.loads(path.read_text('utf-8-sig'))
        if (value.get('schema')!=SCHEMA or value.get('basis')!='user_confirmed_same_encounter'
                or value.get('source_sha256')!=settlement.get('source_sha256')
                or settlement.get('status')!='available'
                or value.get('record_payload_sha256')!=payload_identity(_read_payload(folder))
                or value.get('own_entity_id')!=report.get('own_entity_id')
                or value.get('scene')!={k:report.get('scene',{}).get(k) for k in ('scene_id','dungeon_sid')}
                or value.get('window') not in windows):
            raise ValueError('结算关联与当前记录、来源或区间不匹配')
        actors=value.get('actors')
        if not isinstance(actors,dict) or not 1<=len(actors)<=32:raise ValueError('关联人物列表无效')
        selected={}
        for actor,sequence in actors.items():
            if not isinstance(actor,str) or type(sequence) is not int:raise ValueError('关联快照无效')
            found=[s for s in settlement['snapshots'] if s['sequence']==sequence
                   and s.get('actor_entity_id',s['own_entity_id'])==actor]
            if len(found)!=1:raise ValueError('关联人物快照不存在')
            from yylogs.encounter_timeline import unix_ms
            if found[0]['observed_unix_ms']<unix_ms(report['origin'])+value['window']['end_ms']:
                raise ValueError('结算快照早于所选观测区间结束')
            selected[actor]=found[0]
        return {'status':'confirmed','basis':value['basis'],'actors':selected,'window':value['window'],
                'independently_verified':False}
    except (OSError,ValueError,TypeError,KeyError,AttributeError,RecursionError) as exc:
        return {'status':'invalid','actors':{},'basis':None,'error':str(exc)}


def merge(summary, link, window, players):
    """Replace an exact actor/skill subtotal; do not append cumulative amounts."""
    summary['settlement_accounting']={'status':link['status'],'basis':link.get('basis'),
        'applied':[],'rejected':[],'included_in_damage':False,
        'independently_verified':False,'error':link.get('error')}
    audit=summary['settlement_accounting']
    if link['status']!='confirmed':return summary
    if window!=link['window'] or summary['target_mode']!='all':
        audit['status']='outside_confirmed_scope'
        return summary
    result=deepcopy(summary)
    audit=result['settlement_accounting']
    estimated_seconds=result['event_timing']['duration_seconds'] if result.get('event_timing') else None
    for actor,snapshot in link['actors'].items():
        if link.get('basis')==AUTO_BASIS:
            _replace_final_actor(result,actor,snapshot,players,audit)
            continue
        for item in snapshot['skills']:
            skill_id=item['skill_id']
            member=next((m for m in result['members'] if m['id']==actor),None)
            overlaps=[s for s in member['skills'] if skill_id in s['skill_ids'] and not s['buff_ids']] if member else []
            if any(s['skill_ids']!=[skill_id] for s in overlaps):
                audit['rejected'].append({'actor_entity_id':actor,'skill_id':skill_id,'reason':'ambiguous_multi_id_group'})
                continue
            if member is None:
                member={**players.get(actor,{'id':actor,'name':None,'local':actor==snapshot['own_entity_id'],
                    'party_membership_verified':False}),'damage':Decimal(0),'record_count':0,'skills':[],
                    'dps':None,'estimated_dps':None,'share':None,
                    'rd_provided_dps':None,'rd_given_dps':None,'rd_taken_dps':None}
                result['members'].append(member)
            observed=sum((s['damage'] for s in overlaps),Decimal(0))
            if overlaps and item['damage']<observed:
                audit['rejected'].append({'actor_entity_id':actor,'skill_id':skill_id,'reason':'snapshot_lower_than_observed'})
                continue
            delta=item['damage']-observed
            member['skills']=[s for s in member['skills'] if s not in overlaps]
            count=sum(s['record_count'] for s in overlaps)
            member['skills'].append({'name':item['name'],'skill_ids':[skill_id],
                'calcpoint_ids':[],'buff_ids':[],'name_sources':['confirmed_settlement_snapshot'],
                'name_evidence':[result['settlement_summary']['source_sha256']],
                'record_count':count,'positive_count':sum(s['positive_count'] for s in overlaps),
                'zero_count':sum(s['zero_count'] for s in overlaps),
                'negative_count':sum(s['negative_count'] for s in overlaps),
                'damage':item['damage'],'settlement_sequence':snapshot['sequence'],
                'settlement_observed_damage':observed,'settlement_added_damage':delta,
                'settlement_basis':link.get('basis','user_confirmed_same_encounter'),'hit_timing_available':False})
            member['damage']+=delta
            result['total_damage']+=delta
            audit['applied'].append({'actor_entity_id':actor,'skill_id':skill_id,
                'sequence':snapshot['sequence'],'observed_damage':observed,
                'cumulative_damage':item['damage'],'added_damage':delta})
    audit['included_in_damage']=bool(audit['applied'])
    audit['status']='merged' if audit['applied'] else 'not_applied'
    total=result['total_damage']
    for member in result['members']:
        member['share']=member['damage']/total if total>0 else None
        member['dps']=member['damage']/result['duration_seconds'] if result['duration_seconds'] else None
        member['estimated_dps']=member['damage']/estimated_seconds if estimated_seconds else None
        for skill in member['skills']:
            skill['share']=skill['damage']/member['damage'] if member['damage']>0 else None
            skill['dps']=skill['damage']/result['duration_seconds'] if result['duration_seconds'] else None
            skill['estimated_dps']=skill['damage']/estimated_seconds if estimated_seconds else None
        member['skills'].sort(key=lambda s:s['damage'],reverse=True)
    result['members'].sort(key=lambda m:m['damage'],reverse=True)
    return result


def _replace_final_actor(result, actor, snapshot, players, audit):
    """The complete final skill table replaces, never supplements, this actor."""
    items=snapshot.get('all_damage_skills')
    if not items:
        audit['rejected'].append({'actor_entity_id':actor,'reason':'empty_final_skill_table'})
        return
    member=next((m for m in result['members'] if m['id']==actor),None)
    if member is None:
        member={**players.get(actor,{'id':actor,'name':None,'local':actor==snapshot['own_entity_id'],
            'party_membership_verified':False}),'damage':Decimal(0),'record_count':0,'skills':[],
            'dps':None,'estimated_dps':None,'share':None,
            'rd_provided_dps':None,'rd_given_dps':None,'rd_taken_dps':None}
        result['members'].append(member)
    observed=member['damage']
    previous=member['skills']
    groups={}
    for item in items:
        key=item['name']
        group=groups.setdefault(key,{'name':key,'skill_ids':[],'calcpoint_ids':[],'buff_ids':[],
            'name_sources':['same_capture_final_damage_table'],
            'name_evidence':[result['settlement_summary']['source_sha256']],
            'record_count':0,'positive_count':0,'zero_count':0,'negative_count':0,
            'damage':Decimal(0),'settlement_sequence':snapshot['sequence'],
            'settlement_observed_damage':Decimal(0),'settlement_added_damage':Decimal(0),
            'settlement_basis':AUTO_BASIS,'hit_timing_available':False,
            'game_final_skill_table':True,'observed_record_count_available':False})
        group['skill_ids'].append(item['skill_id']);group['damage']+=item['damage']
        exact=sum((s['damage'] for s in previous if s['skill_ids']==[item['skill_id']]
                   and not s['buff_ids']),Decimal(0))
        audit['applied'].append({'actor_entity_id':actor,'skill_id':item['skill_id'],
            'sequence':snapshot['sequence'],'observed_damage':exact,
            'cumulative_damage':item['damage'],'added_damage':None,
            'accounting_mode':'complete_actor_final_table_replacement'})
    from yylogs.final_count_ownership import final_count_group
    by_id={skill_id:g for g in groups.values() for skill_id in g['skill_ids']}
    for counts in member.get('observed_count_groups',[]):
        identity=counts['identity']
        group=final_count_group(identity,by_id,groups)
        if group is None:continue
        for field in ('record_count','positive_count','zero_count','negative_count'):
            group[field]+=counts[field]
        group['observed_record_count_available']=True
        group['record_count_basis']='same_capture_accepted_damage_events'
        group['calcpoint_ids']=sorted(set(group['calcpoint_ids'])|set(counts['calcpoint_ids']))
    audit.setdefault('observed_count_attribution',[]).append({'actor_entity_id':actor,
        'assigned_records':sum(g['record_count'] for g in groups.values()),
        'unassigned_records':member['record_count']-sum(g['record_count'] for g in groups.values())})
    cumulative=sum((s['damage'] for s in groups.values()),Decimal(0))
    member['observed_skills']=previous
    member['observed_damage']=observed
    member['skills']=list(groups.values());member['damage']=cumulative
    member['damage_basis']='same_capture_final_damage_table'
    delta=cumulative-observed
    result['total_damage']+=delta
    audit.setdefault('actor_replacements',[]).append({'actor_entity_id':actor,
        'observed_damage':observed,'final_damage':cumulative,'net_change':delta,
        'final_skill_count':len(items),'display_skill_count':len(groups)})
    audit['full_skill_table_used']=True
