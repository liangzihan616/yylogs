"""Local battle records and presentation preferences."""
from pathlib import Path
import json
def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))

def enrich_report(report,workspace=None):
    if workspace:
        report['_ui_preferences_path'] = str(Path(workspace) / 'logs_research/ui_preferences.json')
    from yylogs.dungeon_identity import resolve_dungeon_identity
    encounter=resolve_dungeon_identity(report.get('dungeon_sid'),report.get('boss_display_name'))
    report['encounter']=encounter
    report['encounter_label']=encounter['label']
    report['dungeon_name']=encounter['dungeon_name']
    report['boss_name']=encounter['boss_name']
    report['boss_order']=encounter['boss_order']
    report.setdefault('timeline',{'available':False,'origin':None,'duration':None,'fight_start_verified':False,'samples':[],'targets':[],'reason':'本场未保存逐次 Boss 观测'})
    report.setdefault('skill_details',{})
    from yylogs.game_names import apply_label
    for detail in report['skill_details'].values():
        for row in detail.get('rows',[]):apply_label(row,'skill',row.get('skill_id'))
    for buff in report.get('buffs',[]):apply_label(buff,'buff',buff.get('id'))
    report.setdefault('boss_qi',{'available':False,'samples':[],'exhausted_intervals':[],'reason':'预留面板，尚未采集 Boss 真气与气竭状态'})
    members={m['id']:m for m in report['members']}
    for b in report['buffs']:
        source=members.get(b['source']);b['source_label']=source['name'] if source else ('Boss 自身' if b['source']==b['target'] else '未解析实体')
        b['source_is_party_member']=source is not None;b['applications']=None;b['max_stacks']=None;b['coverage']=None
    return report

def apply_record_preferences(reports, preferences):
    """Apply saved display name and favorite flag; favorites stay above newer records."""
    for report in reports:
        if not isinstance(preferences, dict):
            preferences = {}
        pref = preferences.get(report.get("id"))
        pref = pref if isinstance(pref, dict) else {}
        name = pref.get("name")
        if isinstance(name, str) and name.strip():
            report["display_name"] = name
            report["display_name_is_custom"] = True
        report["favorite"] = pref.get("favorite") is True
    reports.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    reports.sort(key=lambda r: not r.get("favorite"))
    return reports


class Repository:
    def __init__(self,workspace=None):
        self.workspace=Path(workspace) if workspace else None
    @property
    def battle_store(self):
        from yylogs.battle_records import store_path
        return store_path(self.workspace)
    @property
    def record_preferences(self):
        return self.battle_store.parent / "record_preferences.json"
    def _preferences(self):
        if not self.record_preferences.exists():
            return {}
        data=load_json(self.record_preferences)
        if not isinstance(data,dict):
            raise ValueError("战斗记录设置无效")
        return data
    def _save_preference(self,report_id,**values):
        from yylogs.local_files import atomic_json
        data=self._preferences()
        data[report_id]={**data.get(report_id,{}),**values}
        self.record_preferences.parent.mkdir(parents=True,exist_ok=True)
        atomic_json(self.record_preferences,data)
    def rename_record(self,report,name):
        from yylogs.battle_records import rename_record
        if report.get("record_folder"):
            return rename_record(report["record_folder"],name,self.battle_store)
        if not isinstance(name,str) or not name.strip() or len(name.strip())>80:
            raise ValueError("名称需为 1 到 80 个字符")
        self._save_preference(report["id"],name=name.strip())
        return name.strip()
    def set_favorite(self,report,favorite):
        """Persistent per-record favorite flag; display only, never touches capture data."""
        self._save_preference(report["id"],favorite=bool(favorite))
        return bool(favorite)
    def delete_record(self,report):
        from yylogs.battle_records import delete_record
        if report.get("record_folder"):
            delete_record(report["record_folder"],self.battle_store)
            preferences=self._preferences()
            if report['id'] in preferences:
                from yylogs.local_files import atomic_json
                preferences.pop(report['id'])
                atomic_json(self.record_preferences,preferences)
        else:
            self._save_preference(report["id"],deleted=True)
    def load(self,*,lazy_battles=False):
        reports=[];warnings=[]
        seen=set()
        from yylogs.battle_records import load_record,load_record_index
        if self.battle_store.is_dir():
            for folder in sorted(self.battle_store.glob("battle_*")):
                try:
                    report=load_record_index(folder) if lazy_battles else load_record(folder)
                    if report["id"] not in seen:
                        reports.append(report if report.get("_lazy_record") else enrich_report(report));seen.add(report["id"])
                except (OSError,ValueError,KeyError,TypeError,AttributeError) as e:
                    warnings.append(f"{folder.name}: {e}")
        preferences=self._preferences()
        reports=[r for r in reports
                 if not (preferences.get(r["id"]) or {}).get("deleted")]
        apply_record_preferences(reports,preferences)
        return reports,warnings

    def load_record_detail(self,record_id):
        from yylogs.battle_records import load_record
        if not isinstance(record_id,str) or not record_id.startswith("battle_"):
            raise ValueError("战斗记录标识无效")
        folder=self.battle_store/record_id
        report=enrich_report(load_record(folder),self.workspace)
        apply_record_preferences([report],self._preferences())
        return report
