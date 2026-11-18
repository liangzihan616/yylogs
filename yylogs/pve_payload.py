"""Offline candidate adapter. Never promotes candidates to verified UI damage.
Consumes exact named PVE fields only. Requires a separate independently checked
fight binding and runtime schema before yylogs.live.extract_skills may publish.
"""
from copy import deepcopy
from yylogs.live import plain,finite


def detail_candidate(path_result):
    result={'status':'unavailable','source':path_result.get('source'),
            'runtime_schema_verified':False,'fight_link_verified':False,
            'duration_candidate':None,'players':{},'reason':path_result.get('status','missing')}
    fields=path_result.get('fields',{})
    duration=fields.get('time_cost',{})
    if duration.get('status')=='candidate_scalar' and finite(duration.get('snapshot')):
        result['duration_candidate']=duration['snapshot']
    data=fields.get('combat_stat',{})
    if data.get('status')!='candidate_snapshot':return result
    try:
        players=plain(data['snapshot'])
        if not isinstance(players,dict):raise ValueError('combat_stat must be a player map')
        if len(players)>64:raise ValueError('player bound exceeded')
        for entity,value in players.items():
            if not isinstance(entity,str) or not entity:raise ValueError('invalid entity key')
            if not isinstance(value,dict):raise ValueError('player payload is not a map')
            if value.get('unresolved_player_instance') is True:raise ValueError('unresolved native player properties')
            selected={}
            for key in ('combat_stat_detail','own_skill_counter','own_skill_detail'):
                if key not in value:continue
                if not isinstance(value[key],dict) or 'unexpanded_type' in value[key]:raise ValueError('unresolved player field: '+key)
                selected[key]=deepcopy(value[key])
            if selected:result['players'][entity]=selected
        result.update(status='candidate_unverified',reason='仅结构候选；尚未验证运行时字段和本场关联，禁止用于可见技能伤害')
    except (KeyError,TypeError,ValueError) as error:
        result.update(status='invalid_candidate',players={},reason=str(error))
    return result
