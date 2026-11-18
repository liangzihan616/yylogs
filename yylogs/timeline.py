"""Pure offline sampled-state timeline. Unknown intervals are never absent states."""
from datetime import datetime
import math

def epoch(value):return datetime.fromisoformat(value).timestamp()
def build_timeline(report,records):
    if not records:return {'available':False,'origin':None,'duration':None,'fight_start_verified':False,'samples':[],'targets':[],'reason':'本场未保存逐次 Boss 观测'}
    if len(records)>10000:raise ValueError('Timeline sample limit')
    if any(r.get('dungeon_sid')!=25 for r in records):raise ValueError('Timeline PVE scope mismatch')
    stamps=[epoch(r['timestamp']) for r in records]
    if any(b<a for a,b in zip(stamps,stamps[1:])):raise ValueError('Timeline timestamps are not ordered')
    spaces={r.get('space_id') for r in records if r.get('space_id')}
    if len(spaces)>1:raise ValueError('Cannot merge different PVE spaces')
    origin=stamps[0];targets=sorted({r['target_id'] for r in records if r.get('status')=='pve_locked_npc_sample'})
    known_keys={(b['id'],b['target'],b['source']) for b in report['buffs']}
    samples=[]
    for i,(r,t) in enumerate(zip(records,stamps)):
        valid=r.get('status')=='pve_locked_npc_sample';effects=[]
        for b in r.get('buffs',[]) if valid else []:
            if b.get('owner_id')!=r.get('target_id'):raise ValueError('Timeline owner mismatch')
            key=(b['buff_no'],b['owner_id'],b['fromer_id'])
            if key not in known_keys:raise ValueError('Unverified effect appeared in timeline')
            effects.append({'id':key[0],'target':key[1],'source':key[2]})
        samples.append({'index':i,'timestamp':r['timestamp'],'t':t-origin,'target':r.get('target_id') if valid else None,'valid':valid,'effects':effects})
    for buff in report['buffs']:
        key=(buff['id'],buff['target'],buff['source']);valid=[x for x in samples if x['valid'] and x['target']==buff['target']];positive=[x for x in valid if any((b['id'],b['target'],b['source'])==key for b in x['effects'])]
        buff['timeline_key']='|'.join(str(x) for x in key);buff['sample_times']=[x['t'] for x in positive];buff['valid_sample_times']=[x['t'] for x in valid];buff['sample_hit_ratio']=len(positive)/len(valid) if valid else None;buff['sample_positive_count']=len(positive);buff['valid_sample_count']=len(valid);buff['applications']=None;buff['max_stacks']=None;buff['coverage']=None
    return {'available':True,'origin':records[0]['timestamp'],'end':records[-1]['timestamp'],'duration':stamps[-1]-origin,'fight_start_verified':False,'axis_kind':'saved_capture_elapsed','samples':samples,'targets':targets,'reason':'零点为首个保存样本；不冒充开战时间。整段观测含战前/战后，脱锁区间未知。'}

def skill_breakdown(report,member_id):
    member=next(m for m in report['members'] if m['id']==member_id)
    unavailable={'available':False,'rows':[],'member':member,'total':None,'unattributed':member['damage'],'complete':False,'reason':'此历史记录只保存了角色总伤害，未保存逐技能明细；无法从总伤害倒推出技能。'}
    payload=report.get('skill_details',{}).get(member_id)
    if not payload or payload.get('status')!='verified':return unavailable
    try:
        rows=payload['rows']
        if not isinstance(rows,list) or not rows or len(rows)>10000:raise ValueError('invalid rows')
        seen=set()
        for row in rows:
            sid=row['skill_id']
            packet_observed=(report.get('capture_backend')=='packet' and payload.get('basis') in
                             ('classified_packet_hits','classified_packet_healing'))
            if packet_observed:
                # Observed effects can have a negative/missing raw ID or share
                # a statistic owner; the exact display group keeps them distinct.
                key=row.get('display_key')
                if (not isinstance(key,list) or not 1<=len(key)<=4
                        or any(type(v) not in (str,int,type(None)) for v in key)
                        or type(sid) not in (int,type(None))):raise ValueError('invalid packet skill identity')
                identity=tuple(key)
            else:
                if isinstance(sid,bool) or not isinstance(sid,(int,str)) or not str(sid).isdigit() or int(sid)<=0:raise ValueError('invalid skill ID')
                identity=str(sid)
            if identity in seen:raise ValueError('duplicate skill identity')
            seen.add(identity)
            if row.get('name') is not None and not isinstance(row['name'],str):raise ValueError('invalid name')
            for key in ['damage','uses','hits','average','critical_percent','insight_percent','graze_percent','maximum']:
                value=row.get(key)
                if key=='damage' and value is None:raise ValueError('missing damage')
                if value is None:continue
                if isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value) or value<0:raise ValueError('invalid number')
                if key in ('uses','hits') and int(value)!=value:raise ValueError('invalid count')
                if key.endswith('_percent') and value>100:raise ValueError('invalid percentage')
        total=sum(row['damage'] for row in rows);tolerance=max(1e-6,member['damage']*1e-9)
        if total>member['damage']+tolerance:raise ValueError('skill damage exceeds personal total')
        missing=max(0,member['damage']-total)
        if 'total' in payload and (not isinstance(payload['total'],(float,int)) or not math.isfinite(payload['total']) or abs(payload['total']-total)>tolerance):raise ValueError('total mismatch')
        return {'available':True,'rows':rows,'member':member,'total':total,'unattributed':missing,'complete':missing<=tolerance,'reason':'仅显示已验证技能明细，不补算缺失次数或属性。'}
    except (ValueError,TypeError,KeyError):
        return {**unavailable,'reason':'技能明细未通过字段 / 总量校验，已阻止展示；原始角色总伤害保留。'}
