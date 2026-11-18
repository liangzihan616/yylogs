"""Packet lifecycle display with explicit character selection and provenance."""
from yylogs.game_names import apply_label
from yylogs.packet_records import number, timestamp


def buff_tracks(snapshot, selected=None, *, include_members=False):
    origin, end = timestamp(snapshot['start']), timestamp(snapshot['end'])
    opened, tracks = {}, {}
    def close(key, stop, censored=False):
        state=opened.pop(key, None)
        if not state:return
        row,start=state
        boss=row['targetActorHash'] in snapshot['boss_ids']
        channel='packet_boss' if boss else 'packet_self'
        buff=row['buffNo'];identity=(row['targetActorHash'],buff)
        track=tracks.setdefault(identity,{'key':':'.join(map(str,identity)), 'buff_no':buff,
            'channel':channel,'category':'boss_debuff' if boss else 'self_buff',
            'target_entity_id':row['targetActorHash'],'segments':[], 'name':None,
            'provenance':'LQ decoded packet lifecycle; attribution unverified'})
        track['segments'].append({'start_ms':start-origin,'end_ms':max(start,stop)-origin,
            'observed_span_ms':max(0,stop-start),'mode':'packet_state','channel':channel,
            'source_entity_id':row.get('sourceActorHash'),'source_status':'reported',
            'left_censored':bool(row.get('initial')),'right_censored':censored,
            'close_reason':'capture_end_unverified' if censored else 'packet_lifecycle_change',
            'fields':{'level':row.get('level'),'charge_level':row.get('charge_level')},
            'instance_id':row.get('buffInstanceHash'),
            'change_kind':row.get('_display_change','applied'),
            'previous_source_entity_id':row.get('_previous_source'),
            'transport_record_ordinal':row.get('transportRecordOrdinal'),
            'raw_start_timestamp':row.get('rawStartTimestamp')})
    for row in sorted(snapshot['buffs'],key=lambda item:(timestamp(item['observedAt']),item.get('transportRecordOrdinal') or 0)):
        target=row['targetActorHash']
        at=timestamp(row['observedAt'])
        if not origin<=at<end:continue
        if row.get('kind') == 'invalidation':
            for opened_key in list(opened):
                if target is None or opened_key[0] == target:close(opened_key,at)
            continue
        if target not in snapshot['boss_ids'] and target!=selected and not include_members:continue
        key=(target,row.get('buffInstanceHash') or row['buffNo'])
        kind=row.get('kind')
        if kind in ('remove_candidate','remove','scope_lost','unavailable'):
            close(key,at);continue
        if kind not in ('snapshot','apply_candidate','update','field_patch'):continue
        change='applied';previous=None
        if key in opened:
            before=opened[key][0]
            previous=before.get('sourceActorHash')
            if (kind != 'apply_candidate' and not row.get('sourceChanged')
                    and (before.get('sourceActorHash'),before.get('charge_level'),before.get('level'),before.get('rawStartTimestamp')) == (row.get('sourceActorHash'),row.get('charge_level'),row.get('level'),row.get('rawStartTimestamp'))):
                continue
            change='handoff' if previous != row.get('sourceActorHash') else 'refresh' if kind=='apply_candidate' or before.get('rawStartTimestamp')!=row.get('rawStartTimestamp') else 'state-update'
            close(key,at)
        opened[key]=({**row,'_display_change':change,'_previous_source':previous},at)
    for key in list(opened):close(key,end,True)
    result=[]
    for track in tracks.values():
        apply_label(track,'buff',track['buff_no']);result.append(track)
    return result


def prepare_packet_self(record,cancelled=lambda:False):
    from yylogs.observation_views import prepare_self_status
    timeline=record['buff_timeline'];snapshot=timeline['packet_snapshot']
    grouped={}
    for track in buff_tracks(snapshot,include_members=True):
        grouped.setdefault(track['target_entity_id'],[]).append(track)
    prepared={}
    for actor in [None]+[member['id'] for member in record['members']]:
        if cancelled():raise InterruptedError()
        view={**timeline,'own_entity_id':actor,'tracks':grouped.get(actor,[])}
        prepared[actor]=prepare_self_status({**record,'buff_timeline':view},cancelled)
    return {'actors':prepared}


def packet_self_page(record,palette,*,prepared=None):
    from yylogs.observation_preferences import own_actor, observation_members, remember_own
    from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLabel,QComboBox,QPushButton
    from yylogs.observation_views import ObservationPage
    prepared=prepared if prepared is not None else prepare_packet_self(record)
    host=QWidget();box=QVBoxLayout(host);box.setContentsMargins(0,0,0,0)
    row=QHBoxLayout();row.addWidget(QLabel('观测角色'))
    selector=QComboBox();selector.setAccessibleName('选择需要查看 Buff 的角色')
    selector.addItem('请选择本人角色',None)
    own = own_actor(record)
    for member in observation_members(record):
        selector.addItem(member['name'] + (' · 本人' if member['id'] == own else ''),member['id'])
    selector.setCurrentIndex(max(0, selector.findData(own)))
    row.addWidget(selector,1)
    set_own = QPushButton('设为本人')
    set_own.setToolTip('记住本人角色，队员 Buff 和队员状态观测默认选择本人')
    row.addWidget(set_own);box.addLayout(row)
    host.observation=None
    def change():
        if host.observation:
            box.removeWidget(host.observation);host.observation.deleteLater()
        selected=selector.currentData();data=prepared['actors'][selected]
        copy={**record,'buff_timeline':data['report']}
        host.observation=ObservationPage(copy,palette,'self',prepared=data,preference_record=record);box.addWidget(host.observation,1)
    def remember_selected():
        actor = selector.currentData()
        if actor is not None:
            try:
                remember_own(record, actor)
                for i in range(1, selector.count()):
                    member = next(m for m in record['members'] if m['id'] == selector.itemData(i))
                    selector.setItemText(i, member['name'] + (' · 本人' if member['id'] == actor else ''))
            except (OSError, ValueError) as exc:selector.setToolTip(f'本人选择保存失败：{exc}')
    def selected():
        if own_actor(record) is None:remember_selected()
        change()
    set_own.clicked.connect(remember_selected)
    selector.currentIndexChanged.connect(selected);change()
    def sync_own_selection():
        own = own_actor(record)
        for i in range(1, selector.count()):
            member = next(m for m in record['members'] if m['id'] == selector.itemData(i))
            selector.setItemText(i, member['name'] + (' · 本人' if member['id'] == own else ''))
        index = selector.findData(own)
        if index < 0 or index == selector.currentIndex():return
        blocked = selector.blockSignals(True)
        selector.setCurrentIndex(index)
        selector.blockSignals(blocked)
        change()
    host.sync_own_selection = sync_own_selection
    host.actor_selector=selector
    return host


def packet_skill_dialog(report,member_id,metric,parent):
    from yylogs.detail_views import SkillDialog
    from PySide6.QtWidgets import QLabel,QFileDialog,QMessageBox
    if metric!='healing':
        dialog=SkillDialog(report,member_id,parent)
        for label in dialog.findChildren(QLabel):label.setText(label.text().replace('Lv.None · ',''))
        return dialog
    import csv
    adapted={**report,'members':[{**member,'damage':member['healing'],'dps':member['hps']} for member in report['members']],
             'skill_details':report['healing_skill_details']}
    class HealingDialog(SkillDialog):
        def export(self):
            file,_=QFileDialog.getSaveFileName(self,'导出治疗技能 CSV','healing_skills.csv','CSV (*.csv)')
            if not file:return
            try:
                with open(file,'w',encoding='utf-8-sig',newline='') as stream:
                    writer=csv.writer(stream);writer.writerow(['技能ID','名称','已归因治疗量','命中次数','HPS','统计口径'])
                    for row in self.detail['rows']:
                        writer.writerow([row['skill_id'],row.get('name'),row['damage'],row.get('hits'),
                            row['damage']/report['duration'],'已分类抓包通知；有效治疗/过量未知'])
            except OSError as exc:QMessageBox.warning(self,'导出失败',str(exc))
    dialog=HealingDialog(adapted,member_id,parent,metric='healing')
    dialog.setWindowTitle(dialog.windowTitle().replace('伤害','治疗'))
    for label in dialog.findChildren(QLabel):
        label.setText(label.text().replace('伤害','治疗').replace('DPS','HPS').replace('Lv.None · ',''))
    for index in range(dialog.table.columnCount()):
        item=dialog.table.horizontalHeaderItem(index)
        if item:item.setText(item.text().replace('伤害','治疗').replace('DPS','HPS'))
    return dialog
