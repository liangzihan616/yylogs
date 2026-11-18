"""Source-traceable display labels. No game access or combat inference."""
from copy import deepcopy
from functools import lru_cache
import json,re
from pathlib import Path
CATALOG=Path(__file__).parent/'resources/game_names.json'
SKILL_FALLBACK_CATALOG=Path(__file__).parent/'resources/skill_display_names.json'
KINDS={'buff','skill'}

def identifier(value):
 if type(value) is int:return value
 if isinstance(value,str) and re.fullmatch(r'-?[0-9]{1,19}',value):return int(value)
 return None

def validate_catalog(data):
 if data.get('schema')!='yylogs.game-names/1':raise ValueError('Unsupported name catalog')
 out={}
 for row in data.get('entries',[]):
  kind=row.get('kind');no=row.get('id');name=row.get('name')
  if kind not in KINDS or type(no) is not int or no<=0 or not isinstance(name,str) or not name.strip() or len(name)>160:raise ValueError('Invalid name entry')
  if row.get('source') not in ('user_confirmed','game_config_locale','public_exact_id'):raise ValueError('Unknown name provenance')
  if not row.get('evidence') or type(row.get('configuration_verified')) is not bool:raise ValueError('Missing name evidence')
  key=(kind,no)
  if key in out:raise ValueError('Duplicate or conflicting name key')
  out[key]=dict(row)
 return out

@lru_cache(maxsize=1)
def catalog():return validate_catalog(json.loads(CATALOG.read_text('utf-8-sig')))

def metadata(kind,no):
 row=catalog().get((kind,identifier(no)))
 return _label_metadata(row)

def _label_metadata(row):
 return {'name':row['name'] if row else None,'name_source':row['source'] if row else 'unmapped',
         'name_configuration_verified':row['configuration_verified'] if row else False,
         'name_evidence':row.get('evidence') if row else None,'name_locale':row.get('locale','zh_cn') if row else None,
         'name_text_id':row.get('text_id') if row else None,'name_evidence_sha256':row.get('evidence_sha256') if row else None,
         'name_table_variant':row.get('table_variant') if row else None,'name_config_id':row.get('config_id') if row else None,
         'name_config_level':row.get('config_level') if row else None,
         'config_buff_estimate':row.get('buff_estimate') if row else None,
         'config_buff_show_flag':row.get('buff_show_flag') if row else None}

@lru_cache(maxsize=1)
def skill_fallback_catalog():
 data=validate_catalog(json.loads(SKILL_FALLBACK_CATALOG.read_text('utf-8-sig')))
 if any(kind!='skill' or row.get('name_field') not in ('ui_name','skill_main_id')
        for (kind,no),row in data.items()):raise ValueError('Invalid skill display fallback')
 return data

def skill_metadata(no):
 """Resolve exact IDs first, then explicit configured statistic aliases."""
 data=metadata('skill',no)
 skill=identifier(no)
 if data['name'] or skill is None or skill<=0:return data
 from yylogs.final_count_ownership import skill_root
 owner=skill_root(skill)
 if owner!=skill:
  label=metadata('skill',owner)
  if label['name']:
   return {**label,'name_raw_skill_id':skill,'name_resolved_skill_id':owner,
           'name_alias_evidence':'yylogs/resources/final_count_ownership.json'}
 fallback=skill_fallback_catalog().get(('skill',skill))
 if fallback:
  return {**_label_metadata(fallback),'name_raw_skill_id':skill,
          'name_resolved_skill_id':fallback.get('alias_skill_id',skill),
          'name_alias_evidence':fallback['evidence']+' · '+fallback['name_field']}
 return data

def apply_label(row,kind,no,*,replace=False):
 data=skill_metadata(no) if kind=='skill' else metadata(kind,no)
 if data['name'] and (replace or not row.get('name') or row.get('name_source') in ('user_confirmed','game_config_locale','public_exact_id','unmapped')):row.update(data)
 return row

def refresh_timeline(report, *, metadata_only=False):
 """Enrich a private display copy; raw saved reports and source hashes unchanged."""
 if metadata_only:
  # Display labels change only track metadata; large evidence arrays stay read-only.
  result=dict(report)
  result['tracks']=[dict(track) for track in report.get('tracks',[])]
  if report.get('boss_debuff'):
   result['boss_debuff']={**report['boss_debuff'],'tracks':[dict(track) for track in report['boss_debuff'].get('tracks',[])]}
 else:result=deepcopy(report)
 for track in result.get('tracks',[]):apply_label(track,'buff',track.get('buff_no'))
 for track in result.get('boss_debuff',{}).get('tracks',[]):apply_label(track,'buff',track.get('buff_no'))
 return result

def tooltip(row):
 labels={'user_confirmed':'用户确认','game_config_locale':'本机配置与中文文本对齐','public_exact_id':'公开数据库同 ID（未核对本机版本）','unmapped':'名称待核实'}
 parts=[labels.get(row.get('name_source'),'原记录名称')]
 if row.get('name_table_variant')=='ordinary':parts.append('普通配置快照；未假定灰度或运行时热修复已生效')
 if row.get('name_config_id') is not None:parts.append('配置行内 ID：'+str(row['name_config_id']))
 if row.get('name_config_level') is not None:parts.append('配置等级：'+str(row['name_config_level']))
 if row.get('name_text_id') is not None:parts.append('名称文本 ID：'+str(row['name_text_id']))
 if row.get('name_alias_evidence'):parts.append('名称归属：'+str(row['name_alias_evidence']))
 if row.get('name_evidence'):parts.append('证据：'+str(row['name_evidence']))
 return '\n'.join(parts)


def display_name(row,fallback):
 name=row.get('name')
 if not name:return fallback
 if row.get('name_source')=='public_exact_id':return name+('（英文·公开资料）' if row.get('name_locale')=='en' else '（公开资料）')
 return name


@lru_cache(maxsize=1)
def skill_display_groups():
 data=json.loads(SKILL_FALLBACK_CATALOG.read_text('utf-8-sig'))
 group=data.get('display_grouping',{})
 if (group.get('field')!='skill_main_id' or
     data.get('snapshot',{}).get('files',{}).get(group.get('source'))!=group.get('source_sha256')):
  raise ValueError('Invalid display grouping evidence')
 mapping=group.get('skills',{})
 if any(not key.isdecimal() or int(key)<=0 or type(value) is not int or value<=0
        for key,value in mapping.items()):raise ValueError('Invalid main skill identity')
 result={int(key):value for key,value in mapping.items()}
 for skill in result:
  seen=set()
  while skill in result and result[skill]!=skill:
   if skill in seen:raise ValueError('Cyclic main skill grouping')
   seen.add(skill);skill=result[skill]
 return result


def skill_display_root(skill):
 mapping=skill_display_groups()
 while skill in mapping and mapping[skill]!=skill:skill=mapping[skill]
 return skill
