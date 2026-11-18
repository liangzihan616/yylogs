"""Character -> merged skill summaries; raw hits remain on disk."""
from pathlib import Path
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,
    QLineEdit,QComboBox,QTableWidget,QTableWidgetItem,QHeaderView,
    QAbstractItemView,QFileDialog,QMessageBox,QWidget)
from yylogs.damage_summary import summarize,observation_windows,write_summary_csv

def number(value,decimals=0):return '—' if value is None else f'{value:,.{decimals}f}'

class SummaryWorker(QThread):
    loaded=Signal(object)
    failed=Signal(str)
    def __init__(self,folder,report,window,target_mode,parent,*,settlement_folder=None):
        super().__init__(parent);self.folder,self.report,self.window,self.target_mode=folder,report,window,target_mode
        self.settlement_folder=settlement_folder
    def run(self):
        try:
            result=summarize(self.folder,self.report,self.window,target_mode=self.target_mode,
                             cancelled=self.isInterruptionRequested,settlement_folder=self.settlement_folder)
            if not self.isInterruptionRequested():self.loaded.emit(result)
        except InterruptedError:pass
        except Exception as exc:self.failed.emit(str(exc))

class SettlementWorker(QThread):
    loaded=Signal(object)
    failed=Signal(str)
    def __init__(self,folder,report,path,parent):
        super().__init__(parent);self.folder,self.report,self.path=folder,report,path
    def run(self):
        from yylogs.settlement_summary import load_settlement_summary
        try:
            result=load_settlement_summary(self.folder,self.report,path=self.path,cancelled=self.isInterruptionRequested)
            if not self.isInterruptionRequested():self.loaded.emit(result)
        except InterruptedError:pass
        except Exception as exc:self.failed.emit(str(exc))

class SettlementLinkWorker(QThread):
    loaded=Signal(object)
    failed=Signal(str)
    def __init__(self,folder,source,store,window,digest,parent):
        super().__init__(parent);self.args=(folder,source,store,window);self.digest=digest
    def run(self):
        from yylogs.battle_records import import_capture,link_settlement
        from yylogs.settlement_summary import MAX_FILE
        try:
            folder,source,store,window=self.args
            if Path(folder).resolve().parent!=Path(store).resolve():
                import hashlib
                if self.isInterruptionRequested():raise InterruptedError()
                if Path(source).is_symlink() or not Path(source).is_file() or Path(source).stat().st_size>MAX_FILE:
                    raise ValueError('结算文件无效或超限')
                if hashlib.sha256(Path(source).read_bytes()).hexdigest()!=self.digest:
                    raise ValueError('结算来源在确认后发生变化，请重新打开')
                record=import_capture(folder,store)
                folder=record['record_folder']
                if self.isInterruptionRequested():raise InterruptedError()
            result=link_settlement(folder,source,store,window,expected_sha256=self.digest,
                confirmed=True,cancelled=self.isInterruptionRequested)
            self.loaded.emit(result)
        except InterruptedError:pass
        except Exception as exc:self.failed.emit(str(exc))

class DamageCandidateDialog(QDialog):
    linked_record=Signal(object)
    def __init__(self,folder,report,palette,parent=None,*,initial_summary=None,member_id=None,initial_metric='damage',settlement_folder=None,record_store=None):
        super().__init__(parent);self.folder,self.report=Path(folder),report
        self.settlement_folder=self.folder if settlement_folder is None else Path(settlement_folder)
        self.data=None;self.selected_entity=member_id;self.worker=None;self.closing=False;self.member_ids=[]
        self.settlement_worker=None;self.external_settlement=None;self.settlement_loading=False
        self.link_worker=None;self.record_store=record_store;self.external_settlement_path=None
        self.setWindowTitle('yylogs · 人物伤害统计');self.resize(1060,760);self.setMinimumSize(880,620)
        box=QVBoxLayout(self);box.setContentsMargins(24,22,24,22);box.setSpacing(14)
        head=QHBoxLayout();self.back=QPushButton('返回人物列表');self.back.setMinimumHeight(42)
        self.back.clicked.connect(self.show_members);head.addWidget(self.back)
        self.heading=QLabel('人物伤害');self.heading.setProperty('kind','heading');self.heading.setTextFormat(Qt.PlainText)
        head.addWidget(self.heading,1);self.export_button=QPushButton('导出技能汇总');self.export_button.setMinimumHeight(42)
        self.export_button.clicked.connect(self.export_csv);head.addWidget(self.export_button);box.addLayout(head)
        self.notice=QLabel('已采伤害 · 非完整结算。仅显示本场可识别的人物来源，队伍身份未核验；NPC 与无法分类来源不计入人物列表。原始逐条记录保存在会话文件中，面板不展示。记录未去重核验，记录数不等于施放次数；观测 DPS 不等于游戏结算 DPS。')
        self.notice.setTextFormat(Qt.PlainText);self.notice.setWordWrap(True);self.notice.setProperty('kind','muted');box.addWidget(self.notice)
        controls=QHBoxLayout();self.metric_selector=QComboBox();self.metric_selector.addItem('伤害','damage');self.metric_selector.addItem('治疗','healing')
        self.metric_selector.addItem('结算伤害','settlement')
        self.metric_selector.setAccessibleName('选择伤害、治疗或结算伤害统计');self.metric_selector.setCurrentIndex(max(0,self.metric_selector.findData(initial_metric)))
        controls.addWidget(QLabel('统计类型'));controls.addWidget(self.metric_selector)
        controls.addWidget(QLabel('统计范围'));self.scope=QComboBox();self.windows=observation_windows(report)
        for i,window in enumerate(self.windows):
            label=('HP 归零观测（击杀未确认）' if window.get('hp_depletion') else
                   ('战斗区间' if window.get('end_verified') else '开战后观测片段（未确认结束）'))
            self.scope.addItem(f'{label} {i+1}',i)
        self.scope.addItem('全部采集 · 不计算 DPS / HPS',None);self.scope.setAccessibleName('选择本场统计区间');controls.addWidget(self.scope)
        if initial_summary and initial_summary.get('settlement_accounting',{}).get('included_in_damage'):
            from yylogs.encounter_timeline import unix_ms
            for i,w in enumerate(self.windows):
                if unix_ms(report['origin'])+w['start_ms']==initial_summary.get('window_start_unix_ms'):
                    self.scope.setCurrentIndex(i);break
        controls.addWidget(QLabel('伤害对象'));self.target_scope=QComboBox()
        self.target_scope.addItem('本场所有目标','all')
        self.target_scope.addItem('选定 Boss','boss')
        self.target_scope.setAccessibleName('选择伤害统计对象')
        self.target_scope.model().item(1).setEnabled(bool(self.windows))
        controls.addWidget(self.target_scope)
        controls.addStretch();box.addLayout(controls)
        self.settlement_controls=QWidget();settlement_row=QHBoxLayout(self.settlement_controls);settlement_row.setContentsMargins(0,0,0,0)
        self.settlement_label=QLabel('累计快照');settlement_row.addWidget(self.settlement_label);self.settlement_snapshots=QComboBox();self.settlement_snapshots.setAccessibleName('选择单次结算累计快照，不累加');settlement_row.addWidget(self.settlement_snapshots,1)
        settlement_row.addStretch()
        self.settlement_open=QPushButton();self.settlement_open.setMinimumHeight(38)
        from yylogs.icons import icon
        self.settlement_open.setIcon(icon('folder-open',palette['text']));self.settlement_open.setFixedWidth(40)
        self.settlement_open.setToolTip('打开本地结算 Hook 日志（临时查看，不修改战斗记录）');self.settlement_open.setAccessibleName('打开本地结算 Hook 日志')
        settlement_row.addWidget(self.settlement_open)
        self.settlement_reset=QPushButton();self.settlement_reset.setMinimumHeight(38);self.settlement_reset.setFixedWidth(40)
        self.settlement_reset.setIcon(icon('x',palette['text']));self.settlement_reset.setToolTip('关闭临时文件，返回记录内快照');self.settlement_reset.setAccessibleName('返回记录内结算快照');settlement_row.addWidget(self.settlement_reset)
        self.settlement_link=QPushButton('确认同场并合并');self.settlement_link.setMinimumHeight(38)
        self.settlement_link.setIcon(icon('file-check-2',palette['text']))
        self.settlement_link.setToolTip('确认日志属于所选战斗，保存含结算记录；原记录保留')
        self.settlement_link.setAccessibleName('确认同场并合并结算伤害')
        settlement_row.addWidget(self.settlement_link)
        box.addWidget(self.settlement_controls);self.settlement_controls.hide()
        self.query=QLineEdit();self.query.setPlaceholderText('搜索人物名称');self.query.setAccessibleName('搜索人物或技能名称');box.addWidget(self.query)
        self.metrics=QLabel('正在读取本地记录…');self.metrics.setProperty('kind','heading');self.metrics.setWordWrap(True);box.addWidget(self.metrics)
        self.summary=QLabel();self.summary.setTextFormat(Qt.PlainText);self.summary.setWordWrap(True);self.summary.setProperty('kind','muted');box.addWidget(self.summary)
        self.table=QTableWidget(0,5);self.table.setAccessibleName('人物伤害列表，点击人物查看同名技能汇总')
        self.table.verticalHeader().hide();self.table.setShowGrid(False);self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows);self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(0,QHeaderView.Stretch)
        for col,width in ((1,110),(2,165),(3,110),(4,150)):self.table.setColumnWidth(col,width)
        self.table.cellClicked.connect(self.open_member);box.addWidget(self.table,1)
        self.scope.currentIndexChanged.connect(self.reload);self.target_scope.currentIndexChanged.connect(self.reload)
        self.query.textChanged.connect(self.render)
        self.metric_selector.currentIndexChanged.connect(self.metric_changed)
        self.settlement_snapshots.currentIndexChanged.connect(self.render)
        self.settlement_open.clicked.connect(self.open_settlement_file)
        self.settlement_reset.clicked.connect(self.reset_settlement_file)
        self.settlement_link.clicked.connect(self.confirm_settlement_link)
        if initial_summary is not None:self.loaded(initial_summary)
        else:self.reload()
    def reload(self):
        if self.link_worker is not None and self.link_worker.isRunning():return
        if self.worker is not None and self.worker.isRunning():return
        self.data=None;self.table.setRowCount(0);self.scope.setEnabled(False);self.target_scope.setEnabled(False);self.export_button.setEnabled(False);self.back.hide()
        self.metrics.setText('正在后台汇总本地记录…');self.summary.setText('按行读取，原始逐条记录不会全部载入内存。')
        index=self.scope.currentData();window=self.windows[index] if index is not None else None
        boss_available=bool(window and isinstance(window.get('target_id'),str) and window['target_id'].strip())
        self.target_scope.model().item(1).setEnabled(boss_available)
        if not boss_available and self.target_scope.currentData()=='boss':
            blocked=self.target_scope.blockSignals(True)
            self.target_scope.setCurrentIndex(0)
            self.target_scope.blockSignals(blocked)
        target_mode=self.target_scope.currentData()
        if self.worker is not None:self.worker.deleteLater()
        self.worker=SummaryWorker(self.folder,self.report,window,target_mode,self,settlement_folder=self.settlement_folder);self.worker.loaded.connect(self.loaded);self.worker.failed.connect(self.failed);self.worker.finished.connect(self.worker_finished);self.worker.start()
    def loaded(self,result):self.data=result;self.render()
    def metric_changed(self,*_):
        self.query.clear();self.render()
    def settlement_data(self):
        return self.external_settlement if self.external_settlement is not None else (self.data or {}).get('settlement_summary',{'status':'missing','snapshots':[]})
    def open_settlement_file(self):
        if self.link_worker and self.link_worker.isRunning():return
        if self.settlement_worker and self.settlement_worker.isRunning():return
        path,_=QFileDialog.getOpenFileName(self,'打开本地结算 Hook 日志','','结算 Hook 日志 (*.jsonl)')
        if not path:return
        self.external_settlement_path=Path(path)
        if self.settlement_worker is not None:self.settlement_worker.deleteLater()
        self.settlement_loading=True;self.settlement_open.setEnabled(False);self.settlement_reset.setEnabled(False)
        self.settlement_worker=SettlementWorker(self.folder,self.report,path,self)
        self.settlement_worker.loaded.connect(self.settlement_loaded);self.settlement_worker.failed.connect(self.settlement_failed)
        self.settlement_worker.finished.connect(self.settlement_finished);self.settlement_worker.start();self.render()
    def settlement_loaded(self,result):
        self.external_settlement={**result,'temporary_file':True};self.settlement_loading=False;self.render()
    def settlement_failed(self,error):
        self.settlement_loaded({'status':'invalid','snapshots':[],'error':error,'source_file':'本地文件'})
    def settlement_finished(self):
        self.settlement_loading=False;self.settlement_open.setEnabled(True);self.settlement_reset.setEnabled(self.external_settlement is not None)
        if self.closing:self.close()
    def reset_settlement_file(self):
        if self.link_worker and self.link_worker.isRunning():return
        if self.settlement_loading:return
        self.external_settlement=None;self.external_settlement_path=None;self._settlement_signature=None;self.render()
    def confirm_settlement_link(self):
        if not self.settlement_link.isEnabled():return
        index=self.scope.currentData()
        if index is None:return
        answer=QMessageBox.question(self,'确认结算属于当前战斗',
            '请确认结算日志与所选区间对应同一场、同一完整统计范围。不要将全场结算关联到残缺记录或局部区间。\n'
            '同副本、同人物不能自动证明同场；关联由你确认，不是自动核验。\n'
            '按人物和技能替换已采金额，不重复相加；不推算命中时间或RD。\n'
            '保存含结算的新记录，原记录保留。是否确认？',
            QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
        if answer!=QMessageBox.Yes:return
        if self.link_worker:self.link_worker.deleteLater()
        source=self.external_settlement_path or self.settlement_folder/'hook_events.jsonl'
        self.link_worker=SettlementLinkWorker(self.settlement_folder,source,self.record_store,
            self.windows[index],self.settlement_data()['source_sha256'],self)
        self.link_worker.loaded.connect(self.settlement_linked)
        self.link_worker.failed.connect(lambda error:QMessageBox.warning(self,'未合并结算',error))
        self.link_worker.finished.connect(self.link_finished)
        self.link_worker.start();self.render()
    def settlement_linked(self,record):
        self.folder=Path(record['capture_folder']);self.settlement_folder=Path(record['record_folder'])
        self.report=record['buff_timeline'];self.external_settlement=None;self.external_settlement_path=None
        self._settlement_signature=None;self.data=record['capture_summary']
        self.linked_record.emit(record)
        if not self.closing:self.render()
    def link_finished(self):
        if self.closing:self.close()
        else:self.render()
    def failed(self,error):self.metrics.setText('统计暂不可用');self.summary.setText(error)
    def worker_finished(self):
        self.scope.setEnabled(self.metric_selector.currentData()!='settlement');self.target_scope.setEnabled(self.metric_selector.currentData()=='damage')
        if self.closing:self.close()
    def show_members(self):self.selected_entity=None;self.query.clear();self.render()
    def open_member(self,row,column=0):
        if self.selected_entity is None and 0<=row<len(self.member_ids):
            self.selected_entity=self.member_ids[row];self.query.clear();self.render()
    def render(self,*_):
        settlement=self.metric_selector.currentData()=='settlement'
        damage=self.metric_selector.currentData()=='damage'
        self.settlement_controls.setVisible(settlement or damage)
        self.settlement_snapshots.setVisible(settlement);self.settlement_label.setVisible(settlement)
        self.settlement_reset.setEnabled(self.external_settlement is not None and not self.settlement_loading)
        self.settlement_label.setText('累计快照' if settlement else '结算明细')
        self.settlement_label.setVisible(settlement or damage)
        if self.data is None:return
        linking=bool(self.link_worker and self.link_worker.isRunning())
        ready=self.settlement_data().get('status')=='available'
        if damage:
            included=self.data.get('settlement_accounting',{}).get('included_in_damage')
            self.settlement_label.setText('结算已合并' if included else '已取得结算 · 尚未合并' if ready else '未取得结算快照')
        self.settlement_link.setToolTip('确认日志属于所选完整战斗范围，保存含结算记录；原记录保留'
            if ready else '本记录未取得结算金额，不能合并。结算可来自自然统计回调；采集期间打开游戏人物伤害详情也可能触发读取。')
        if self.record_store is None:
            self.settlement_link.setToolTip('当前为只读诊断。请从战斗记录打开人物明细，或从采集中心查看本次记录后确认合并。')
        self.settlement_link.setVisible(damage)
        already_merged=bool(self.data.get('settlement_accounting',{}).get('included_in_damage')
            and self.data['settlement_accounting'].get('basis')=='same_capture_final_rpc'
            and self.external_settlement is None)
        self.settlement_link.setEnabled(damage and ready and not already_merged and self.record_store is not None
            and self.scope.currentData() is not None and self.target_scope.currentData()=='all'
            and not self.settlement_loading and not linking and not (self.worker and self.worker.isRunning()))
        if linking:
            self.scope.setEnabled(False);self.target_scope.setEnabled(False)
            self.settlement_open.setEnabled(False);self.settlement_reset.setEnabled(False)
            self.export_button.setEnabled(False)
            self.settlement_link.setText('正在合并…');return
        self.settlement_link.setText('结算已合并' if already_merged else '确认同场并合并')
        self.settlement_open.setEnabled(not self.settlement_loading)
        self.scope.setEnabled(not settlement and not (self.worker and self.worker.isRunning()))
        self.metrics.setToolTip('')
        self.summary.setToolTip('')
        self.export_button.setText('导出选定快照' if settlement else '导出技能汇总')
        if settlement:
            from yylogs.settlement_views import render_settlement
            self.target_scope.setEnabled(False);render_settlement(self);return
        if self.metric_selector.currentData()=='healing':
            from yylogs.healing_views import render_healing
            self.target_scope.setEnabled(False);render_healing(self);return
        self.target_scope.setEnabled(not (self.worker and self.worker.isRunning()))
        self.setWindowTitle('yylogs · 人物伤害统计')
        self.notice.setText('已采伤害 · 非完整结算。只统计可归属人物的伤害候选；NPC、未分类来源及治疗不计入伤害。覆盖与去重未核验，记录数不等于施放次数；观测 DPS 不等于游戏结算 DPS。')
        if not ready:self.notice.setText(self.notice.text()+' 未取得结算快照，结算技能尚未计入。')
        self.table.setAccessibleName('人物伤害列表，点击人物查看同名技能汇总')
        data=self.data;member=next((m for m in data['members'] if m['id']==self.selected_entity),None);query=self.query.text().strip().casefold();duration=data['duration_seconds']
        target_text='本场总伤害' if data['target_mode']=='all' else '选定 Boss 伤害'
        dps_text='观测 DPS' if data['target_mode']=='all' else 'Boss 观测 DPS'
        event_timing=data.get('event_timing')
        dps_key='estimated_dps' if event_timing else 'dps'
        if event_timing:dps_text='事件估计 DPS' if data['target_mode']=='all' else 'Boss 事件估计 DPS'
        from yylogs.settlement_details import detail_rows
        settlement_rows=detail_rows(self.settlement_data(),self.selected_entity)
        accounting=data.get('settlement_accounting',{})
        same_source=self.settlement_data().get('source_sha256')==data.get('settlement_summary',{}).get('source_sha256')
        applied=accounting.get('applied',[]) if same_source else []
        settlement_rows=[s for s in settlement_rows if not any(a['actor_entity_id']==self.selected_entity
            and a['skill_id']==s['skill_id'] for a in applied)]
        if self.selected_entity is not None and member is None and not settlement_rows:self.selected_entity=None
        duration_text=f'观测 {number(duration,3)} 秒' if duration else '战斗用时未验证 · DPS —'
        if event_timing:duration_text=f"事件对照估计 {number(event_timing['duration_seconds'],3)} 秒 · HP 观测 {number(duration,3)} 秒"
        self.back.setVisible(member is not None or bool(settlement_rows));self.heading.setText((member['name'] or ('本人 · 名称未解析' if member['local'] else '人物 · 名称未解析')) if member else ('人物伤害明细' if settlement_rows else '人物伤害'))
        self.query.setPlaceholderText('搜索技能名称或 ID' if member or settlement_rows else '搜索人物名称');rows=[];self.member_ids=[]
        if member or settlement_rows:
            self.table.setHorizontalHeaderLabels(['技能','伤害记录数','伤害金额' if settlement_rows else '已采伤害合计','占比',dps_text])
            self.metrics.setText(f"{target_text}（已采） {number(member['damage'] if member else None)}   ·   {dps_text} {number(member[dps_key] if member else None,1)}")
            replaced_ids={s['skill_id'] for s in settlement_rows}
            for skill in member['skills'] if member else []:
                if len(skill['skill_ids'])==1 and skill['skill_ids'][0] in replaced_ids and not skill['buff_ids']:continue
                ids=skill['skill_ids']+skill['calcpoint_ids']+skill['buff_ids']
                if query and query not in (skill['name']+' '+' '.join(map(str,ids))).casefold():continue
                tooltip='原始技能 ID：'+', '.join(map(str,skill['skill_ids']))
                tooltip+='\n伤害计算点 ID：'+(', '.join(map(str,skill['calcpoint_ids'])) or '未记录')
                if 'settlement_sequence' in skill:
                    tooltip+=(f"\n同场最终技能统计 #{skill['settlement_sequence']}；按游戏最终技能归类，不补造实时ID对应关系。"
                              + ('\n次数：同场已采伤害记录数，按精确计算点/技能统计归属对应；非施放次数/游戏结算次数，可能未覆盖全场。' if skill.get('observed_record_count_available') else '\n金额已纳入本行及总伤；最终快照未提供次数，也没有可对应的实时事件，次数保持未知，不按金额估算。')
                              +'\n最终表未提供命中时间；原始逐条伤害保留，RD和分段曲线仍使用原始事件。') if skill.get('game_final_skill_table') else (f"\n已确认同场结算快照 #{skill['settlement_sequence']}"
                              f"\n原已采金额：{skill['settlement_observed_damage']}"
                              f"\n累计金额：{skill['damage']}；净增加：{skill['settlement_added_damage']}"
                              '\n累计替换，不重复相加；次数为已采记录数，不是结算次数。无命中时间，不计RD。')
                if skill['buff_ids']:
                    tooltip+='\n归类参考 Buff ID：'+', '.join(map(str,skill['buff_ids']))
                    tooltip+='\n按 Buff 计算点配置归类；不代表已识别具体 Buff 实例。'
                    tooltip+='\n名称依据：'+', '.join(skill['name_evidence'])
                rows.append(([skill['name'],('—' if 'settlement_sequence' in skill and not skill['record_count'] else skill['record_count']),number(skill['damage']),number(skill['share']*100,1)+'%' if skill['share'] is not None else '—',number(skill[dps_key],1)],tooltip))
            for skill in settlement_rows:
                if query and query not in f"{skill['name']} {skill['skill_id']}".casefold():continue
                tooltip=(f"技能 ID：{skill['skill_id']}\n人物实体：{skill['actor_entity_id']}\n原始累计金额：{skill['damage']}"
                         f"\n结算累计快照 #{skill['sequence']}；重复读取不累加。"
                         f"\n来源：{skill['source_file']}\nSHA256：{skill['source_sha256']}"
                         '\n本场关联未核验；不与逐次伤害相加，不计算次数、DPS、RD或命中时间。')
                observed=[s for s in member['skills'] if s['skill_ids']==[skill['skill_id']] and not s['buff_ids']] if member else []
                if observed:
                    tooltip+='\n该技能已采逐次伤害：'+number(sum(s['damage'] for s in observed))+ '；面板在同一行显示累计金额，原逐次记录与统计未修改。'
                rows.append(([skill['name'],'—',number(skill['damage']),
                              number(skill['share']*100,1)+'%（面板）' if skill['share'] is not None else '—','—'],tooltip))
            if settlement_rows:
                self.notice.setText('结算技能来自该人物最后一份有效累计快照，重复读取不累加。本场关联尚未核验，暂不加进已采总伤 / DPS / RD；结算行占比以游戏面板总伤为分母，不代表当前筛选区间或所选 Boss。')
        else:
            self.table.setHorizontalHeaderLabels(['人物名称','伤害记录数','已采伤害合计','占比',dps_text])
            self.metrics.setText(f"人物{target_text}（已采） {number(data['total_damage'])}   ·   {duration_text}")
            for ordinal,m in enumerate(data['members'],1):
                name=m['name'] or ('本人 · 名称未解析' if m['local'] else f'人物 {ordinal} · 名称未解析')
                if query and query not in name.casefold():continue
                self.member_ids.append(m['id']);rows.append(([name,m['record_count'],number(m['damage']),number(m['share']*100,1)+'%' if m['share'] is not None else '—',number(m[dps_key],1)],'来源实体 ID：'+m['id']+'\n队伍成员身份未核验；点击查看技能。'))
            seen={m['id'] for m in data['members']}
            actors=dict.fromkeys(s.get('actor_entity_id',s.get('own_entity_id')) for s in self.settlement_data().get('snapshots',[]))
            for actor in actors:
                if not actor or actor in seen or not detail_rows(self.settlement_data(),actor):continue
                name=f'人物 · {actor}（结算）'
                if query and query not in name.casefold():continue
                self.member_ids.append(actor);rows.append(([name,'—','—','—','—'],'仅取得结算累计快照；点击查看该人物技能，不借用其他人物金额。'))
        self.table.setRowCount(len(rows))
        for i,(values,tooltip) in enumerate(rows):
            for j,value in enumerate(values):
                item=QTableWidgetItem(str(value));item.setToolTip(tooltip)
                if j:item.setTextAlignment(Qt.AlignRight|Qt.AlignVCenter)
                self.table.setItem(i,j,item)
            self.table.setRowHeight(i,46)
        target_text='本场所有目标' if data.get('target_mode')=='all' else '选定 Boss'
        exclusions=f"时间范围外 {data['outside_selected_window']:,} 条"
        exclusions+=f"；治疗候选 {data.get('excluded_healing_records',0):,} 条（不计伤害 / DPS）"
        if data.get('excluded_kind_conflict_records'):
            exclusions+=f"；类型冲突 {data['excluded_kind_conflict_records']:,} 条"
        if data.get('target_mode')=='boss':
            exclusions+=f"；非所选 Boss {data['outside_selected_target']:,} 条"
        timing_note=''
        if data.get('dps_timing_basis')=='persistent_zero_hp_observation':
            a,b=data['duration_interval_seconds']
            timing_note=f"\nHP 持续归零计时 · 已剔除归零尾段 {number(data['excluded_zero_hp_tail_seconds'],3)} 秒；起止采样用时范围 {number(a,3)}–{number(b,3)} 秒。击杀及游戏结算用时未确认。"
        if event_timing:
            timing_note+='\n两端伤害扣血量与 HP 变化吻合，按回调时间估计；服务器计时未验证，伤害统计范围不变。'
        settlement_note=''
        settlement_state=self.settlement_data()
        if settlement_rows:
            settlement_note='\n结算技能金额来自人物面板累计快照；本场关联未核验，当前时间 / 目标筛选不适用。'+(' 临时查看，未写入战斗记录。' if settlement_state.get('temporary_file') else '')
        elif damage and settlement_state.get('status')=='invalid':
            settlement_note='\n结算文件校验失败：'+str(settlement_state.get('error') or '')+'；未显示其中金额。'
        elif damage and self.settlement_loading:
            settlement_note='\n正在后台读取结算明细…'
        if accounting.get('included_in_damage'):
            automatic=accounting.get('basis')=='same_capture_final_rpc'
            self.notice.setText(('已合并本次采集内通过会话与最终回调校验的结算技能。' if automatic else '已按用户确认的同场关联合并结算技能。')+'按人物及技能累计替换已采金额，重复快照不相加；总伤和DPS包含结算净增加金额。原始记录保留，结算不伪造命中时间、不加入RD或分段曲线。')
            settlement_note+='\n'+('本次采集最终结算自动合并' if automatic else '同场确认已保存')+' · 结算累计替换已采技能金额 · DPS沿用当前观测用时。'
            if accounting.get('full_skill_table_used'):
                self.notice.setText('金额采用本次采集内校验通过的最终技能表，不与实时伤害重复相加。次数保留可对应的已采伤害记录数，非施放次数或游戏结算次数；无法对应显示横线。原始事件、治疗、RD与分段曲线不变。')
                settlement_note='\n金额与技能分类采用游戏最终统计；DPS仍用当前观测秒数，不承诺等于游戏结算DPS。'
                if member:
                    counts=next((v for v in accounting.get('observed_count_attribution',[])
                        if v['actor_entity_id']==member['id']),None)
                    if counts and counts['unassigned_records']:
                        settlement_note+=f"\n此人物另有 {counts['unassigned_records']:,} 条已采记录尚不能可靠对应最终技能，不按伤害金额猜次数。"
        elif accounting.get('status')=='outside_confirmed_scope':
            settlement_note+='\n结算仅计入已确认的战斗区间和所有目标口径；当前筛选不含结算。'
        elif accounting.get('status')=='invalid':
            settlement_note+='\n结算关联凭据校验失败，未合并：'+str(accounting.get('error') or '')
        self.summary.setText(f"{duration_text} · 统计对象：{target_text} · 读取 {data['record_count']:,} 条原始候选 · 排除 NPC / 未分类来源 {data['excluded_nonplayer_or_unresolved']:,} 条；{exclusions}；时间未知 {data['unknown_timestamp_records']:,} 条。\n"+('没有已采到且可归属人物的伤害；不代表人物没有造成伤害。' if not data['members'] else '点击人物查看技能；同名技能合并，伤害时间与原始 ID 不丢弃。')+(' 旧记录因条数 / 解码上限已截断，无法恢复缺失伤害。' if data.get('known_truncated') else '')+(' 本次仅观测片段，不是完整战斗 DPS。' if data['partial_observation'] else '')+timing_note+settlement_note)
        self.export_button.setEnabled(bool(rows))
    def export_csv(self):
        if self.metric_selector.currentData()=='settlement':
            from yylogs.settlement_views import selected_snapshot
            from yylogs.settlement_summary import write_settlement_csv
            snapshot=selected_snapshot(self)
            if snapshot is None or not snapshot['skills']:return
            path,_=QFileDialog.getSaveFileName(self,'导出选定结算累计快照','settlement_snapshot.csv','CSV (*.csv)')
            if path:
                try:write_settlement_csv(path,snapshot,self.settlement_data())
                except (OSError,ValueError) as exc:QMessageBox.warning(self,'导出失败',str(exc))
            return
        healing=self.metric_selector.currentData()=='healing'
        path,_=QFileDialog.getSaveFileName(self,'导出治疗技能汇总' if healing else '导出人物技能汇总','healing_summary.csv' if healing else 'damage_summary.csv','CSV (*.csv)')
        if path:
            try:
                if healing:
                    from yylogs.healing_summary import write_healing_csv
                    write_healing_csv(path,self.data['healing_summary'],self.selected_entity)
                else:write_summary_csv(path,self.data,self.selected_entity,settlement_summary=self.settlement_data())
            except (OSError,ValueError) as exc:QMessageBox.warning(self,'导出失败',str(exc))
    def closeEvent(self,event):
        for worker in (self.worker,self.settlement_worker,self.link_worker):
            if worker and worker.isRunning():
                worker.requestInterruption()
                if not worker.wait(2000):self.closing=True;event.ignore();return
        event.accept()
