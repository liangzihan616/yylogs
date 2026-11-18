"""Display-only skill grouping. Preserve all candidate rows and original IDs."""
from collections import OrderedDict
from yylogs.damage_effects import display_identity


def group_key(row, *, by_name=True):
    if by_name:
        skill,_=display_identity(row)
        return (row['fromer_id'],skill)
    return (row['fromer_id'],row['skill_id'],row['calcpoint_id'],row['receiver_entity_id'])


def group_skills(rows, *, by_name=True):
    groups=OrderedDict()
    for row in rows:
        key=group_key(row,by_name=by_name)
        group=groups.setdefault(key,{'key':key,'source_entity_id':row['fromer_id'],
            'name':display_identity(row)[1],
            'skill_ids':set(),'calcpoint_ids':set(),'receiver_ids':set(),
            'record_count':0,'positive_count':0,'zero_count':0,'negative_count':0})
        group['skill_ids'].add(row['skill_id']);group['calcpoint_ids'].add(row.get('calcpoint_id'))
        group['receiver_ids'].add(row.get('receiver_entity_id'))
        group['record_count']+=1
        if row['damage']>0:group['positive_count']+=1
        elif row['damage']==0:group['zero_count']+=1
        else:group['negative_count']+=1
    result=list(groups.values())
    for group in result:
        for key in ('skill_ids','calcpoint_ids','receiver_ids'):
            group[key]=sorted(group[key],key=lambda value:(value is None,str(value)))
    return result
