"""Durable offline PVE encounter recorder. No process access in this module.
Only explicit observations can confirm combat transitions or death. Target loss,
scene exit, polling timeout and unchanged totals NEVER mean a boss was killed.
"""
from pathlib import Path
from datetime import datetime
from copy import deepcopy
import hashlib,json,math,os
SID=25
from .runtime_scope import SUPPORTED_PVE_SIDS, entity_id as validate_entity_id

def finite(v):return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and v>=0

def plain(value):
    if isinstance(value,dict):
        if value.get('kind') in ('dict','table','attribute_map') and 'entries' in value or 'entries' in value and 'array' in value:
            result={}
            for entry in value['entries']:
                key=str(entry['key'])
                if key in result:raise ValueError('duplicate snapshot key')
                result[key]=plain(entry['value'])
            for entry in value.get('array',[]):
                key=str(entry['index'])
                if key in result:raise ValueError('duplicate snapshot key')
                result[key]=plain(entry['value'])
            return result
        if value.get('kind')=='list':return [plain(x) for x in value['items']]
        return {k:plain(v) for k,v in value.items()}
    if isinstance(value,list):return [plain(v) for v in value]
    return value

def extract_skills(player,total,*,fight_link_verified=False,schema_verified=False,source=None):
    """Exact saved PVE skill maps, never total-derived fabricated skills.
    Schema evidenced by imp_damage_statistuc.lua main.3/4: combat_stat_detail
    [target].damage[skill_id]; own_skill_detail[target].damage[skill_id].max/count;
    own_skill_counter[skill_id]. Real runtime shape must pass these checks too.
    """
    unavailable={'status':'unavailable','rows':[],'total':None,'unattributed':total,'reason':'未取得与本场关联的逐技能原始字段'}
    if not fight_link_verified or not schema_verified or not finite(total) or not isinstance(player,dict):return unavailable
    detail=player.get('combat_stat_detail')
    if not isinstance(detail,dict) or not detail:return unavailable
    try:
        by_skill={};counter=player.get('own_skill_counter',{});ext=player.get('own_skill_detail',{})
        if not isinstance(counter,dict) or not isinstance(ext,dict):raise ValueError('unsupported counter/detail map')
        for target,stat in detail.items():
            if not isinstance(stat,dict):raise ValueError('invalid target stat')
            damages=stat.get('damage',{})
            if not isinstance(damages,dict):raise ValueError('invalid skill damage map')
            for sid,damage in damages.items():
                if sid in ('CRI_DMG','BASH_DMG','ORD_DMG','ABR_DMG','CRI_HEAL'):
                    if not finite(damage):raise ValueError('invalid damage structure total')
                    continue
                # Type/structure buckets are NOT silently treated as skills.
                if not str(sid).isdigit() or int(sid)<=0 or not finite(damage):raise ValueError('unverified skill damage key/value')
                sid=int(sid);row=by_skill.setdefault(sid,{'skill_id':sid,'damage':0,'_counts':[],'_maxima':[],'_ext_complete':True});row['damage']+=damage
                observed=ext.get(str(target),{}).get('damage',{}).get(str(sid))
                if not isinstance(observed,dict):row['_ext_complete']=False;continue
                count=observed.get('count');maximum=observed.get('max')
                if count is None:row['_ext_complete']=False
                elif not finite(count) or int(count)!=count:raise ValueError('invalid hit count')
                else:row['_counts'].append(count)
                if maximum is not None:
                    if not finite(maximum):raise ValueError('invalid maximum')
                    row['_maxima'].append(maximum)
                else:row['_ext_complete']=False
        if not by_skill:return unavailable
        rows=[]
        for sid,row in sorted(by_skill.items()):
            counts=row.pop('_counts');maxima=row.pop('_maxima');complete=row.pop('_ext_complete')
            uses=counter.get(str(sid))
            if uses is not None and (not finite(uses) or int(uses)!=uses):raise ValueError('invalid skill use counter')
            row['uses']=uses;row['hits']=sum(counts) if complete else None;row['maximum']=max(maxima) if complete and maxima else None
            row['average']=row['damage']/row['hits'] if row['hits'] else None
            for key in ('critical_percent','insight_percent','graze_percent'):row[key]=None
            rows.append(row)
        amount=sum(row['damage'] for row in rows);tolerance=max(1e-6,total*1e-9)
        if amount>total+tolerance:raise ValueError('skill sum exceeds independently observed personal total')
        return {'status':'verified','rows':rows,'total':amount,'unattributed':max(0,total-amount),'complete':abs(amount-total)<=tolerance,'source':source,'fight_link_verified':True,'schema':'pve_target_damage_by_skill_v1'}
    except (TypeError,KeyError,ValueError,AttributeError) as error:
        return {**unavailable,'reason':'技能字段结构 / 总量校验失败：'+str(error)}

def verified_boss_damage(frame):
    """Current-encounter TEAM damage to this exact NPC, never a cached total.
    Runtime adapter must verify semantics and encounter binding independently.
    This gate does not discover a source or turn candidates into observations.
    """
    value=frame.get('boss_damage')
    if not isinstance(value,dict) or value.get('schema_verified') is not True or value.get('fight_link_verified') is not True:return None
    if value.get('scope')!='team_to_boss':return None
    if not frame.get('target_observed') or not frame.get('target_id'):return None
    if value.get('target_id')!=frame['target_id'] or value.get('space_id')!=frame['space_id']:return None
    if not isinstance(value.get('encounter_id'),str) or not value['encounter_id']:return None
    if not isinstance(value.get('source'),str) or not value['source']:return None
    if not finite(value.get('total')):raise ValueError('invalid verified boss damage total')
    return value


def boss_damage_start(frame,previous):
    """Damage trigger is independent of HP / in_battle; polling is an interval.
    Only an explicitly verified pre-pull zero for the same team/target/fight
    establishes a full start. A positive first snapshot is mid-fight evidence.
    """
    current=verified_boss_damage(frame)
    if current is None or current['total']<=0:return None
    before=verified_boss_damage(previous) if previous else None
    same=bool(before and all(before[k]==current[k] for k in ('encounter_id','target_id','space_id','source')))
    elapsed=(datetime.fromisoformat(frame['timestamp'])-datetime.fromisoformat(previous['timestamp'])).total_seconds() if previous else None
    observed=bool(same and before['total']==0 and before.get('pre_pull_baseline_verified') is True and 0<elapsed<=2.5)
    return {'observed':observed,'start':{'lower':previous['timestamp'] if observed else None,'upper':frame['timestamp'],
            'kind':'observed_first_boss_damage' if observed else 'boss_damage_seen_in_progress',
            'timing':'poll_interval_not_exact_hit_time','source':current['source'],'encounter_id':current['encounter_id'],
            'first_observed_total':current['total']}}


def verified_boss_health(frame):
    """Adapter-certified current-encounter Boss HP, independent of target lock.
    Raw candidate_hp, NPC class or a selected target alone is insufficient.
    """
    value=frame.get('boss_health')
    if not isinstance(value,dict) or any(value.get(k) is not True for k in ('schema_verified','boss_identity_verified')):return None
    if value.get('fight_link_verified') is not True:
        # A fresh read-only HP stream can bind an observation window, not a
        # server fight or damage cache. Keep that distinction explicit.
        scope=value.get('observation_scope')
        if not isinstance(scope,dict) or scope.get('kind')!='local_boss_health_window':return None
        if value.get('observation_binding_verified') is not True or frame.get('health_only') is not True:return None
        if value.get('encounter_id_kind')!='local_observation_window_not_server_fight':return None
        if scope.get('session_id')!=value.get('encounter_id'):return None
        if any(scope.get(k)!=frame.get(k) for k in ('dungeon_sid','space_id','target_id')):return None
        if type(scope.get('game_pid')) is not int or scope['game_pid']<=0:return None
        if type(scope.get('process_creation_ticks')) is not int or scope['process_creation_ticks']<=0:return None
    if not frame.get('target_observed') or not frame.get('target_id'):return None
    if value.get('target_id')!=frame['target_id'] or value.get('space_id')!=frame['space_id']:return None
    if any(not isinstance(value.get(k),str) or not value[k] for k in ('encounter_id','source')):return None
    hp=value.get('hp');maximum=value.get('hp_max')
    if not finite(hp) or not finite(maximum) or maximum<=0 or hp>maximum:raise ValueError('invalid verified Boss HP')
    return value


def boss_health_start(frame,previous):
    """HP drop is team-independent; no local attack or lock is required.
    Poll timestamps bound detection, not the actual hit. A first already-low
    sample cannot prove a full encounter start. Zero HP alone never proves kill.
    """
    current=verified_boss_health(frame)
    if current is None:return None
    before=verified_boss_health(previous) if previous else None
    same=bool(before and all(before[k]==current[k] for k in ('encounter_id','target_id','space_id','source','hp_max')))
    dropping=bool(same and current['hp']<before['hp'])
    if not dropping and not (not same and current['hp']<current['hp_max']):return None
    elapsed=(datetime.fromisoformat(frame['timestamp'])-datetime.fromisoformat(previous['timestamp'])).total_seconds() if previous else None
    observed=bool(dropping and before['hp']==before['hp_max'] and before.get('pre_pull_baseline_verified') is True and 0<elapsed<=2.5)
    return {'observed':observed,'start':{'lower':previous['timestamp'] if observed else None,'upper':frame['timestamp'],
            'kind':'observed_boss_hp_drop' if observed else 'boss_hp_seen_in_progress',
            'timing':'poll_interval_not_exact_hit_time','source':current['source'],'encounter_id':current['encounter_id'],
            'hp_before':before['hp'] if same else None,'hp_after':current['hp'],'hp_max':current['hp_max']}}


class EncounterRecorder:
    """One encounter: arm before pull, journal every poll and fsync immediately.
    Observed edges are bounded by poll timestamps, never exact event timestamps.
    """
    def __init__(self,directory,*,account,entity,dungeon_sid=SID):
        if type(dungeon_sid) is not int or dungeon_sid not in SUPPORTED_PVE_SIDS:raise ValueError('unsupported PVE SID')
        self.dungeon_sid=dungeon_sid
        self.account=account
        self.entity=validate_entity_id(entity)
        self.root=Path(directory);self.root.mkdir(parents=True,exist_ok=True)
        self.journal=(self.root/'samples.jsonl').open('x',encoding='utf-8',newline='\n')
        self.state={'schema':2,'dungeon_sid':dungeon_sid,'account':self.account,'entity':self.entity,'pve_only':True,'state':'armed','samples':0,'target_id':None,'space_id':None,'start':None,'end':None,'complete':False,'start_observed':False,'start_mode':None,'damage_encounter_id':None,'damage_source':None,'last_boss_damage_total':None,'health_encounter_id':None,'health_source':None,'health_max':None,'last_boss_hp':None,'target_lock_required':False,'kill_verified':False,'last_timestamp':None,'members':{},'skill_details':{},'gaps':[],'rdps':None,'rdps_supported':False,'stream_sha256':None,'reason':'等待有效的 PVE 战斗开始观测'}
        self.last=None;self.hash=hashlib.sha256();self.closed=False;self.publish()
    @property
    def terminal(self):return self.state['state'] in ('finished','interrupted')
    def publish(self):
        tmp=self.root/'live_status.tmp'
        with tmp.open('w',encoding='utf-8') as f:json.dump(self.state,f,ensure_ascii=False,allow_nan=False,indent=2);f.flush();os.fsync(f.fileno())
        os.replace(tmp,self.root/'live_status.json')
    def feed(self,frame):
        if self.closed or self.terminal:raise ValueError('recorder is closed or terminal')
        if frame.get('account')!=self.account or frame.get('entity')!=self.entity:raise ValueError('account/entity scope mismatch')
        if type(frame.get('dungeon_sid')) is not int or frame.get('dungeon_sid')!=self.dungeon_sid:raise ValueError('outside selected PVE; caller must interrupt without appending foreign data')
        ts=datetime.fromisoformat(frame['timestamp']).timestamp()
        if self.last and ts<=datetime.fromisoformat(self.last['timestamp']).timestamp():raise ValueError('non-increasing sample time')
        if not isinstance(frame.get('space_id'),str) or not frame['space_id']:raise ValueError('missing space identity')
        if self.state['space_id'] and self.state['space_id']!=frame['space_id']:
            self.interrupt('scene_changed_before_verified_kill');return
        target=frame.get('target_id')
        if self.state['target_id'] and target and target!=self.state['target_id']:raise ValueError('cannot merge different NPCs')
        for key in ('in_battle','boss_death_verified'):
            if frame.get(key) is not None and not isinstance(frame[key],bool):raise ValueError('invalid tristate '+key)
        if frame.get('boss_death_verified') is True and (not frame.get('target_observed') or not target or not frame.get('death_evidence')):raise ValueError('unanchored death signal')
        damage=verified_boss_damage(frame)
        health=verified_boss_health(frame)
        if self.state['state']=='recording' and self.state.get('start_mode')=='boss_health' and health:
            if health['encounter_id']!=self.state['health_encounter_id'] or health['source']!=self.state['health_source'] or health['hp_max']!=self.state['health_max']:
                self.interrupt('boss_health_encounter_source_or_phase_changed_not_kill');return
        # Reject resets or a different fight rather than merge them into this record.
        if self.state['state']=='recording' and self.state.get('start_mode')=='boss_damage' and damage:
            if damage['encounter_id']!=self.state['damage_encounter_id'] or damage['source']!=self.state['damage_source']:
                self.interrupt('damage_encounter_or_source_changed');return
            if damage['total']<self.state['last_boss_damage_total']:
                self.interrupt('boss_damage_counter_reset_not_kill');return
        damage_start=boss_damage_start(frame,self.last) if self.state['state']=='armed' else None
        health_start=boss_health_start(frame,self.last) if self.state['state']=='armed' else None
        # Validate full serialization before mutating or saving state.
        line=json.dumps(frame,ensure_ascii=False,allow_nan=False,separators=(',',':'))+'\n'
        self.journal.write(line);self.journal.flush();os.fsync(self.journal.fileno());self.hash.update(line.encode('utf-8'))
        state=self.state;state['samples']+=1;state['stream_sha256']=self.hash.hexdigest();state['space_id']=frame['space_id'];state['last_timestamp']=frame['timestamp']
        if target:state['target_id']=target
        if self.last:
            elapsed=ts-datetime.fromisoformat(self.last['timestamp']).timestamp()
            if elapsed>2.5 and state['state']=='recording':state['gaps'].append({'after':self.last['timestamp'],'before':frame['timestamp'],'seconds':elapsed,'reason':'poll_gap'})
        in_battle=frame.get('in_battle') if frame.get('combat_signal_verified') is True and frame.get('target_observed') else None;previous=self.last
        if state['state']=='armed' and damage_start:
            observed=damage_start['observed']
            state.update(state='recording',start_observed=observed,start_mode='boss_damage',start=damage_start['start'],
                         damage_encounter_id=damage['encounter_id'],damage_source=damage['source'],last_boss_damage_total=damage['total'],
                         reason='首笔 Boss 伤害触发记录；起点为采样区间，不依赖血量' if observed else '已观测本场 Boss 伤害；缺少开怪前基线，按中途加入记录')
        elif state['state']=='armed' and health_start:
            observed=health_start['observed']
            state.update(state='recording',start_mode='boss_health',start_observed=observed,start=health_start['start'],
                         health_encounter_id=health['encounter_id'],health_source=health['source'],health_max=health['hp_max'],last_boss_hp=health['hp'],
                         reason='Boss 掉血触发，不要求锁定或本人攻击；边界为采样区间' if observed else 'Boss 已掉血；缺少可靠战前基线，按中途加入保存')
        elif state['state']=='armed' and target and in_battle is True:
            observed=bool(previous and previous.get('target_id')==target and previous.get('in_battle') is False and previous.get('combat_signal_verified') is True and previous.get('target_observed') is True and ts-datetime.fromisoformat(previous['timestamp']).timestamp()<=2.5)
            state.update(state='recording',start_mode='combat_state',start_observed=observed,start={'lower':previous['timestamp'] if observed else None,'upper':frame['timestamp'],'kind':'observed_combat_transition' if observed else 'joined_in_progress'},reason='正在记录；开战边界为采样区间' if observed else '中途加入：缺少开战前基线，不能称为全场')
        if state['state']=='recording':
            if state['start_mode']=='boss_damage':
                if damage is None:state['gaps'].append({'timestamp':frame['timestamp'],'reason':'boss_damage_stream_not_verified'})
                else:state['last_boss_damage_total']=damage['total']
            elif state['start_mode']=='boss_health':
                if health is None:state['gaps'].append({'timestamp':frame['timestamp'],'reason':'boss_health_stream_not_verified'})
                else:state['last_boss_hp']=health['hp']
            elif in_battle is None:state['gaps'].append({'timestamp':frame['timestamp'],'reason':'combat_state_not_verified'})
            if not frame.get('target_observed',False):state['gaps'].append({'timestamp':frame['timestamp'],'reason':'target_not_observed_not_death'})
            if frame.get('cache_link_verified') is True:
                members=frame.get('members',{})
                if isinstance(members,dict):
                    state['members']=deepcopy(members)
                    state['skill_details']={}
                    state['duration']=frame.get('duration') if finite(frame.get('duration')) else None
                    for eid,member in members.items():
                        if not isinstance(member,dict) or not finite(member.get('damage')):continue
                        fields=frame.get('player_skill_fields',{}).get(eid)
                        result=extract_skills(fields,member['damage'],fight_link_verified=True,schema_verified=frame.get('skill_schema_verified') is True,source=frame.get('skill_source'))
                        if result['status']=='verified':state['skill_details'][eid]=result
                        else:state['skill_details'].pop(eid,None)
            if frame.get('boss_death_verified') is True and target==state['target_id'] and frame.get('death_evidence'):
                state.update(state='finished',kill_verified=True,end={'lower':previous['timestamp'] if previous else None,'upper':frame['timestamp'],'evidence':frame['death_evidence']},reason='已观测到独立验证的目标击杀信号')
                state['complete']=state['start_observed'] and not state['gaps']
            elif in_battle is False:
                state['gaps'].append({'timestamp':frame['timestamp'],'reason':'left_battle_without_verified_kill'})
                state['reason']='已观测脱战，但没有独立击杀信号；继续保存，不冒充击杀'
        self.last=deepcopy(frame);self.publish()
    def interrupt(self,reason):
        if self.terminal:return
        self.state.update(state='interrupted',complete=False,reason=reason);self.publish()
    def close(self,reason='collector_stopped_before_verified_kill'):
        if self.closed:return
        if not self.terminal:self.interrupt(reason)
        self.journal.close();self.closed=True


def presentation_report(state):
    """Small live summary for the same personal-detail dialog; never historical backfill."""
    validate_entity_id(state.get('entity'))
    if state.get('pve_only') is not True or type(state.get('dungeon_sid')) is not int or state.get('dungeon_sid') not in SUPPORTED_PVE_SIDS:raise ValueError('live report scope')
    duration=state.get('duration')
    if not finite(duration) or duration<=0:return None
    members=[]
    for eid,member in state.get('members',{}).items():
        if not isinstance(eid,str) or not isinstance(member,dict) or not finite(member.get('damage')):raise ValueError('invalid member total')
        members.append({'id':eid,'name':str(member.get('name') or eid),'level':member.get('level') if isinstance(member.get('level'),int) else '—','local':eid==state.get('entity'),'damage':member['damage'],'dps':member['damage']/duration,'taken':member.get('taken'),'healing':member.get('healing'),'rdps':None})
    if not members:return None
    return {'duration':duration,'members':members,'skill_details':state.get('skill_details',{}),'rdps_supported':False}


def read_saved_journal(path):
    """Recover complete fsynced frames after a crash; ignore ONLY a torn last line."""
    path=Path(path)
    if path.stat().st_size>128*1024*1024:raise ValueError('journal byte limit')
    raw=path.read_bytes();lines=raw.splitlines(keepends=True);frames=[];torn=False;selected_sid=None;selected_identity=None
    if len(lines)>100000:raise ValueError('journal frame limit')
    for index,line in enumerate(lines):
        if not line.endswith(b'\n'):
            if index!=len(lines)-1:raise ValueError('broken interior frame')
            torn=True;break
        frame=json.loads(line.decode('utf-8'))
        if type(frame.get('dungeon_sid')) is not int or frame.get('dungeon_sid') not in SUPPORTED_PVE_SIDS:raise ValueError('journal PVE scope')
        identity=(frame.get('account'),validate_entity_id(frame.get('entity')))
        if selected_identity is None:selected_identity=identity
        elif identity!=selected_identity:raise ValueError('journal cannot mix different account/entities')
        if selected_sid is None:selected_sid=frame['dungeon_sid']
        elif selected_sid!=frame['dungeon_sid']:raise ValueError('journal cannot mix different PVE SIDs')
        frames.append(frame)
    return {'frames':frames,'torn_tail_ignored':torn,'complete':False,'note':'文件可恢复不等于战斗完整；仍需开怪与击杀证据。'}
