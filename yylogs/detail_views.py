"""PVE buff/source timelines and personal skill-detail views. No game access."""
from datetime import datetime
from pathlib import Path
import csv,json
from PySide6.QtCore import Qt,Signal,QRectF
from PySide6.QtGui import QColor,QPainter,QPen,QBrush,QFont
from PySide6.QtWidgets import (QWidget,QFrame,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QComboBox,QLineEdit,QSlider,QScrollArea,QTableWidget,QTableWidgetItem,QHeaderView,QAbstractItemView,QDialog,QFileDialog,QMessageBox,QToolTip,QStyledItemDelegate)
from yylogs.timeline import skill_breakdown
from yylogs.settlement_summary import SKILL_NAMES as SETTLEMENT_SKILL_NAMES
from yylogs.game_names import display_name,tooltip as name_tooltip,skill_display_root

def text(value,kind=None):
    l=QLabel(str(value));l.setTextFormat(Qt.PlainText)
    if kind:l.setProperty('kind',kind)
    return l

def fmt_time(t):
    minute=int(t)//60;second=t-minute*60
    return f'{minute:02d}:{second:05.2f}'


def skill_stat_text(row, key, decimals=0):
    value = row.get(key)
    if value is None:
        return '—'
    result = f'{value:,.{decimals}f}'
    if row.get(key + '_basis') == 'observed' and not row.get('observed_statistics_reconciled'):
        result += '（已采）'
    return result


def skill_stat_tooltip(row, key):
    basis = row.get(key + '_basis')
    if basis == 'settlement':
        return {'hits': '游戏结算计数；不等同于技能施放次数。',
                'average': '按游戏结算总伤害与结算计数计算。',
                'maximum': '游戏结算中的单次最高伤害。'}.get(key, '')
    if basis == 'observed':
        scope = ('已采伤害与该技能结算总伤一致。' if row.get('observed_statistics_reconciled')
                 else '仅统计已采部分，可能未覆盖全场。')
        return {'hits': '已采伤害通知条数；不等同于施放次数。',
                'average': '已采伤害合计除以已采通知条数。',
                'maximum': '已采通知中的单次最高伤害。'}.get(key, '') + '\n' + scope
    return ''

class BuffTimeline(QWidget):
    inspected=Signal(str)
    LEFT=205;RIGHT=210;TOP=72;ROW=34
    def __init__(self,report,palette):
        super().__init__();self.report=report;self.p=palette;self.buffs=list(report['buffs']);self.zoom=1;self.setMouseTracking(True);self.setAccessibleName('Boss Buff 全程采样时间轴。每行按 Buff、目标、施加者区分；未知区段不计覆盖率。');self.size_chart()
    def size_chart(self):
        self.setMinimumWidth(960*self.zoom);self.setMinimumHeight(112+len(self.buffs)*self.ROW);self.update()
    def set_buffs(self,buffs):self.buffs=buffs;self.size_chart()
    def set_zoom(self,value):self.zoom=value;self.size_chart()
    def plot(self):return self.LEFT,max(60,self.width()-self.LEFT-self.RIGHT)
    def time_x(self,t):
        left,width=self.plot();return left+t/max(self.report['timeline']['duration'] or 1,1)*width
    def paintEvent(self,event):
        q=QPainter(self);q.setRenderHint(QPainter.Antialiasing);p=self.p;timeline=self.report['timeline'];duration=max(timeline['duration'] or 1,1);left,width=self.plot();bottom=self.TOP+len(self.buffs)*self.ROW
        q.setFont(QFont('Microsoft YaHei UI',9));q.setPen(QColor(p['muted']));q.drawText(8,20,'效果 / Buff ID');q.drawText(int(left+width+18),20,'施加者 / 来源实体')
        for i in range(7):
            t=duration*i/6;x=self.time_x(t);q.setPen(QPen(QColor(p['line']),1));q.drawLine(int(x),32,int(x),bottom+12);q.setPen(QColor(p['muted']));q.drawText(int(x)-19,50,fmt_time(t)[:5])
        # A full-width unknown track with discrete valid-poll markers; never extrapolate uptime.
        q.fillRect(QRectF(left,60,width,8),QBrush(QColor(p['line']),Qt.BDiagPattern))
        for sample in timeline['samples']:
            if sample['valid']:q.fillRect(QRectF(self.time_x(sample['t'])-1,60,2,8),QColor(p['muted']))
        for i,b in enumerate(self.buffs):
            y=self.TOP+i*self.ROW;key=(b['id'],b['target'],b['source']);q.fillRect(QRectF(left,y+8,width,22),QBrush(QColor(p['raised']),Qt.BDiagPattern))
            q.setPen(QColor(p['text']));name=(b['name']+' · '+str(b['id'])) if b['name'] else f"Buff {b['id']}";q.drawText(8,y+19,name)
            q.setPen(QColor(p['muted']));q.drawText(int(left+width+18),y+13,b.get('source_label',b['source']));q.setFont(QFont('Segoe UI',8));q.drawText(int(left+width+18),y+29,b['source']);q.setFont(QFont('Microsoft YaHei UI',9))
            for sample in timeline['samples']:
                if not sample['valid'] or sample['target']!=b['target']:continue
                x=self.time_x(sample['t']);present=any((e['id'],e['target'],e['source'])==key for e in sample['effects']);q.setPen(Qt.NoPen)
                if present:
                    q.setBrush(QColor(p['good'] if b['local'] else p['accent']));q.drawRoundedRect(QRectF(x-4,y+10,8,18),2,2)
                else:
                    q.setPen(QPen(QColor(p['subtle']),1));q.drawLine(int(x)-2,y+19,int(x)+2,y+19)
        q.setPen(QColor(p['muted']));q.drawText(8,bottom+38,'短竖条：实际观测到存在   短横：该来源效果未出现在有效快照   斜纹：未观测 / 未锁定目标')
    def sample_at(self,pos):
        left,width=self.plot()
        if not left<=pos.x()<=left+width:return None
        t=(pos.x()-left)/width*(self.report['timeline']['duration'] or 1);row=int((pos.y()-self.TOP)//self.ROW)
        if not 0<=row<len(self.buffs):return None
        buff=self.buffs[row];samples=self.report['timeline']['samples'];nearest=min(samples,key=lambda x:abs(x['t']-t)) if samples else None
        if not nearest:return None
        exact=abs(nearest['t']-t)<=0.35
        valid=exact and nearest['valid'] and nearest['target']==buff['target'];key=(buff['id'],buff['target'],buff['source'])
        present=valid and any((b['id'],b['target'],b['source'])==key for b in nearest['effects'])
        state='实际快照：存在' if present else ('实际快照：未见此来源的效果' if valid else '未观测，不能判断存在或消失')
        return f"{fmt_time(t)}  ·  {buff['name'] or 'Buff '+str(buff['id'])}\n来源：{buff.get('source_label','未解析')} / {buff['source']}\n目标：{buff['target']}\n{state}"
    def mouseMoveEvent(self,event):
        info=self.sample_at(event.position())
        if info:QToolTip.showText(event.globalPosition().toPoint(),info,self)
        else:QToolTip.hideText()
    def mousePressEvent(self,event):
        if event.button()==Qt.LeftButton:
            info=self.sample_at(event.position())
            if info:self.inspected.emit(info)
        super().mousePressEvent(event)

class BuffPage(QWidget):
    def __init__(self,report,palette):
        super().__init__();self.report=report;self.p=palette;box=QVBoxLayout(self);box.setContentsMargins(0,12,0,8);box.setSpacing(10)
        head=QHBoxLayout();head.addWidget(text('Boss 效果 · 全程观测','heading'));head.addStretch();head.addWidget(text('按 Buff + 施加者分别记录','muted'));box.addLayout(head)
        controls=QHBoxLayout();controls.addWidget(text('目标','muted'));self.target=QComboBox();self.target.addItem('全部 Boss',None);self.target.setMinimumWidth(0);self.target.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon);self.target.setMinimumContentsLength(10)
        for target in sorted({b['target'] for b in report['buffs']}):self.target.addItem('Boss · '+target,target)
        controls.addWidget(self.target,2);controls.addWidget(text('施加者','muted'));self.source=QComboBox();self.source.addItem('全部来源',None);self.source.setMinimumWidth(0);self.source.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon);self.source.setMinimumContentsLength(8)
        names={b['source']:b.get('source_label','未解析实体') for b in report['buffs']}
        for source,name in names.items():self.source.addItem(name+' · '+source[-8:],source)
        controls.addWidget(self.source,1);self.query=QLineEdit();self.query.setPlaceholderText('Buff 名称或 ID');self.query.setAccessibleName('筛选 Buff 名称或编号');self.query.setMinimumWidth(80);controls.addWidget(self.query,1);box.addLayout(controls)
        self.source.setAccessibleName('按 Buff 施加者筛选');self.target.setAccessibleName('按 Buff 目标筛选')
        note=text('','small');note.setWordWrap(True);timeline=report['timeline']
        if timeline['available']:
            start=datetime.fromisoformat(timeline['origin']);end=datetime.fromisoformat(timeline['end']);note.setText(f"已保存整段 {fmt_time(timeline['duration'])} · {start:%H:%M:%S}—{end:%H:%M:%S} · 战斗结算 {report['duration']:.2f} 秒。零点是采集首样本，不是已验证的开战时刻；战前、战后和脱锁区间保留。")
        else:note.setText('本场没有逐次观测样本，不能补出时间轴；不代表没有 Buff。')
        box.addWidget(note)
        self.table=QTableWidget(0,7);self.table.setHorizontalHeaderLabels(['效果 / Buff ID','施加者','存在样本','施加次数','最高层','覆盖率','首次 / 最后观测']);self.table.verticalHeader().hide();self.table.setEditTriggers(QAbstractItemView.NoEditTriggers);self.table.setSelectionBehavior(QAbstractItemView.SelectRows);self.table.setShowGrid(False);self.table.horizontalHeader().setSectionResizeMode(0,QHeaderView.Stretch)
        for c in range(1,7):self.table.horizontalHeader().setSectionResizeMode(c,QHeaderView.ResizeToContents)
        self.table.setMaximumHeight(210);self.table.setMinimumHeight(140);self.table.setAccessibleName('Boss Buff 来源统计表，未知施加次数、层数和覆盖率保留为空');self.table.hide()
        self.chart=None
        if timeline['available']:
            zoom=QHBoxLayout();zoom.addWidget(text('时间轴缩放','muted'));self.zoom=QSlider(Qt.Horizontal);self.zoom.setRange(1,6);self.zoom.setValue(1);self.zoom.setFixedWidth(190);self.zoom.setAccessibleName('时间轴缩放，一到六倍');zoom.addWidget(self.zoom);self.zoom_text=text('1×','small');zoom.addWidget(self.zoom_text);zoom.addStretch();reset=QPushButton('查看整段');reset.clicked.connect(lambda:self.zoom.setValue(1));zoom.addWidget(reset);box.addLayout(zoom)
            self.chart=BuffTimeline(report,palette);scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(self.chart);scroll.setMinimumHeight(190);scroll.setAccessibleName('可横向滚动的完整 Buff 时间轴');box.addWidget(scroll,1);self.inspector=text('悬停查看时刻；点击时间轴固定当前 Buff、施加者和观测状态。','small');self.inspector.setWordWrap(True);box.addWidget(self.inspector);self.chart.inspected.connect(self.inspector.setText);self.zoom.valueChanged.connect(self.change_zoom)
        self.summary_toggle=QPushButton('展开样本明细 · 次数 / 层数 / 覆盖率');self.summary_toggle.setCheckable(True);self.summary_toggle.toggled.connect(self.table.setVisible);self.summary_toggle.toggled.connect(lambda checked:self.summary_toggle.setText('收起样本明细' if checked else '展开样本明细 · 次数 / 层数 / 覆盖率'));box.addWidget(self.summary_toggle);box.addWidget(self.table)
        caveat=text('“存在样本”不是施加次数或覆盖率；精确 Buff 事件接入前，不生成连续覆盖条。','small');caveat.setWordWrap(True);box.addWidget(caveat)
        self.target.currentIndexChanged.connect(self.filter);self.source.currentIndexChanged.connect(self.filter);self.query.textChanged.connect(self.filter);self.filter()
    def change_zoom(self,value):
        self.zoom_text.setText(f'{value}×');self.chart.set_zoom(value)
    def filter(self,*args):
        target=self.target.currentData();source=self.source.currentData();query=self.query.text().strip().casefold();buffs=[b for b in self.report['buffs'] if (not target or b['target']==target) and (not source or b['source']==source) and (not query or query in f"{b['name'] or ''} {b['id']}".casefold())];self.visible_buffs=buffs
        self.table.setRowCount(len(buffs))
        for i,b in enumerate(buffs):
            a=b.get('sample_positive_count',b['count']);n=b.get('valid_sample_count');seen=f'{a}/{n}' if n is not None else str(a);span='—'
            if b['first'] and b['last']:span=datetime.fromisoformat(b['first']).strftime('%H:%M:%S')+' / '+datetime.fromisoformat(b['last']).strftime('%H:%M:%S')
            values=[(b['name']+' · '+str(b['id'])) if b['name'] else f"Buff {b['id']}",b.get('source_label','未解析实体')+(' · 当前角色' if b['local'] else ''),seen,'—','—','—',span]
            self.table.setRowHeight(i,39)
            for j,v in enumerate(values):
                item=QTableWidgetItem(v);item.setToolTip(f"Buff ID {b['id']}\n来源实体：{b['source']}\n目标实体：{b['target']}\n施加次数、最高层和覆盖率尚未采集。");self.table.setItem(i,j,item)
        if self.chart:self.chart.set_buffs(buffs)

class QiPlaceholder(QWidget):
    def __init__(self,palette):
        super().__init__();self.p=palette;self.setMinimumHeight(235);self.setAccessibleName('Boss 真气与气竭时间轴预留，尚无数据，不绘制伪造曲线')
    def paintEvent(self,event):
        q=QPainter(self);p=self.p;q.setPen(QColor(p['line']));left=65;right=self.width()-25
        for i in range(5):y=25+i*40;q.drawLine(left,y,right,y)
        q.setPen(QColor(p['muted']));q.drawText(8,30,'真气');q.drawText(left,222,'时间 / 气竭区间：待采集');q.drawText(self.rect(),Qt.AlignCenter,'尚未采集 Boss 真气 / 气竭状态\n接入后与 Buff 时间轴同步，不以 0 代替缺失值')

def qi_page(palette):
    page=QWidget();box=QVBoxLayout(page);box.setContentsMargins(0,22,0,12);box.setSpacing(18);box.addWidget(text('Boss 真气与气竭','heading'));note=text('此处预留真气曲线、气竭开始 / 结束和同步效果区间。当前未采集，暂不绘图。','muted');note.setWordWrap(True);box.addWidget(note);box.addWidget(QiPlaceholder(palette));box.addStretch();return page

class SkillValueItem(QTableWidgetItem):
    def __init__(self,display,value=None):
        super().__init__(display);self.setData(Qt.UserRole,value);self.setTextAlignment(Qt.AlignRight|Qt.AlignVCenter)
    def __lt__(self,other):
        a=self.data(Qt.UserRole);b=other.data(Qt.UserRole)
        return (-1 if a is None else a)<(-1 if b is None else b)

class DamageShareDelegate(QStyledItemDelegate):
    def paint(self,painter,option,index):
        share=index.data(Qt.UserRole)
        if share is not None:
            painter.save();painter.setPen(Qt.NoPen);painter.setBrush(QColor(128,150,139,45));rect=QRectF(option.rect).adjusted(3,7,-3,-7);rect.setWidth(rect.width()*max(0,min(1,share/100)));painter.drawRoundedRect(rect,3,3);painter.restore()
        super().paint(painter,option,index)

def group_settlement_skill_rows(rows):
    """Group proven same-name segments for display; preserve original record rows."""
    groups = {}
    for index, row in enumerate(rows):
        verified = (row.get('name') and row.get('name_source') == 'game_config_locale'
                    and row.get('name_configuration_verified') is True)
        key = (skill_display_root(int(row['skill_id'])), row['name']) if verified else ('row', index)
        groups.setdefault(key, []).append(row)
    result = []
    for key, parts in groups.items():
        if len(parts) == 1:
            result.append(parts[0])
            continue
        root = key[0]
        first = next((row for row in parts if int(row['skill_id']) == root), parts[0])
        merged = {**first, 'damage': sum(row['damage'] for row in parts),
                  'component_skill_ids': sorted(int(row['skill_id']) for row in parts),
                  'display_main_skill_id': root, 'uses': None,
                  'critical_percent': None, 'insight_percent': None, 'graze_percent': None}
        for field in ('raw_skill_ids', 'calcpoint_ids'):
            values = {value for row in parts for value in row.get(field, [])}
            if field == 'raw_skill_ids':values.update(merged['component_skill_ids'])
            merged[field] = sorted(values)
        for field, aggregate in (('observed_damage', sum), ('observed_hits', sum), ('observed_maximum', max)):
            merged.pop(field, None)
            if all(row.get(field) is not None for row in parts):
                merged[field] = aggregate(row[field] for row in parts)
        merged['observed_statistics_reconciled'] = all(row.get('observed_statistics_reconciled') is True for row in parts)
        for field, aggregate in (('hits', sum), ('maximum', max)):
            basis = {row.get(field + '_basis') for row in parts}
            known = (len(basis) == 1 and next(iter(basis)) in ('settlement', 'observed')
                     and all(row.get(field) is not None for row in parts))
            merged[field] = aggregate(row[field] for row in parts) if known else None
            merged[field + '_basis'] = next(iter(basis)) if known else None
        merged['average'], merged['average_basis'] = None, None
        if merged['hits']:
            if merged['hits_basis'] == 'settlement' or merged['observed_statistics_reconciled']:
                merged['average'] = merged['damage'] / merged['hits']
            elif merged.get('observed_damage') is not None:
                merged['average'] = merged['observed_damage'] / merged['hits']
            if merged['average'] is not None:merged['average_basis'] = merged['hits_basis']
        result.append(merged)
    return result


def settlement_skill_name(row):
    sid = row.get('skill_id')
    if type(sid) is str and sid.isdecimal():
        sid = int(sid)
    return SETTLEMENT_SKILL_NAMES.get(sid) if type(sid) is int else None


class SkillDialog(QDialog):
    def __init__(self,report,member_id,parent=None,*,metric='damage'):
        super().__init__(parent);self.report=report;self.detail=skill_breakdown(report,member_id);m=self.detail['member'];self.setWindowTitle(m['name']+' · 技能伤害明细 — yylogs');self.resize(1180,710);self.setMinimumSize(760,460);box=QVBoxLayout(self);box.setContentsMargins(24,22,24,22);box.setSpacing(16)
        if (metric == 'damage' and self.detail['available'] and report.get('capture_backend') == 'packet'
                and report.get('skill_details', {}).get(member_id, {}).get('basis') == 'packet_settlement'):
            self.detail = {**self.detail, 'rows': group_settlement_skill_rows(self.detail['rows'])}
        head=QHBoxLayout();head.addWidget(text(m['name']+' · 技能伤害','heading'));head.addStretch();head.addWidget(text(f"Lv.{m['level']} · {'当前角色' if m['local'] else '队伍成员'}",'muted'));box.addLayout(head)
        postcombat=report.get('record_kind')=='saved_postcombat_skill_snapshot'
        self.summary=text(f"个人总伤害 {m['damage']:,.1f}   /   {'详情口径 DPS' if postcombat else '平均 DPS'} {m['dps']:,.1f}   /   {report.get('duration_label','战斗')} {report['duration']:.2f} 秒",'muted');self.summary.setWordWrap(True);box.addWidget(self.summary)
        if postcombat:
            provenance=text(f"战后截图核对 · 结算界面另示 {report['settlement_duration']:.0f} 秒；当前 DPS 仅以个人详情用时计算，非完整战斗计时。次数为游戏计数器，不宣称施放次数。",'small');provenance.setWordWrap(True);box.addWidget(provenance)
        self.damage_categories = metric == 'damage'
        self.settlement_summary = text('', 'muted')
        self.settlement_summary.setWordWrap(True)
        amounts = {}
        for row in self.detail['rows']:
            name = settlement_skill_name(row)
            if name:
                amounts[name] = amounts.get(name, 0) + row['damage']
        total = sum(amounts.values())
        share = total / m['damage'] * 100 if m['damage'] else 0
        if amounts:
            self.settlement_summary.setText(
                f'结算类伤害 {total:,.1f} · 占个人伤害 {share:.2f}%\n' +
                '；'.join(f'{name} {amounts[name]:,.1f}' for name in SETTLEMENT_SKILL_NAMES.values() if name in amounts))
        else:
            self.settlement_summary.setText('当前明细中暂无结算类伤害记录。' if self.detail['available']
                                            else '本记录缺少可用技能明细，无法判断结算类伤害。')
        self.settlement_summary.setToolTip('结算类按技能 ID 识别：醉杀状态、失魂落魄、鼠鼠追命、天威伤害。汇总已包含在个人总伤害中。')
        box.addWidget(self.settlement_summary)
        self.settlement_summary.setVisible(self.damage_categories)
        filters = QHBoxLayout()
        self.category_label = text('伤害类型')
        self.category_filter = QComboBox()
        self.category_filter.addItems(['全部技能', '仅看结算类', '其他技能'])
        self.category_filter.setAccessibleName('筛选技能伤害类型')
        self.category_label.setBuddy(self.category_filter)
        self.filter_status = text('', 'small')
        filters.addWidget(self.category_label);filters.addWidget(self.category_filter)
        filters.addWidget(self.filter_status);filters.addStretch()
        box.addLayout(filters)
        for widget in (self.category_label, self.category_filter, self.filter_status):
            widget.setVisible(self.damage_categories)
        note=text(('结算明细中，同名分段按配置主技能合并，原始 ID 见悬停与 CSV。' if self.damage_categories else '') + '技能名未解析时显示 Skill ID；缺失次数或属性显示“—”。','small');note.setWordWrap(True);box.addWidget(note)
        self.table=QTableWidget(0,12);self.table.setHorizontalHeaderLabels(['技能 / Skill ID','总伤害','占个人伤害','次数','命中','平均','会心%','会意%','擦伤%','最高','DPS','伤害类型']);self.table.verticalHeader().hide();self.table.setEditTriggers(QAbstractItemView.NoEditTriggers);self.table.setShowGrid(False);self.table.setSelectionBehavior(QAbstractItemView.SelectRows);self.table.horizontalHeader().setSectionResizeMode(0,QHeaderView.Stretch)
        for c in range(1,12):self.table.horizontalHeader().setSectionResizeMode(c,QHeaderView.ResizeToContents)
        self.table.horizontalHeader().moveSection(11, 1)
        self.table.setColumnHidden(11, not self.damage_categories)
        if report.get('capture_backend') == 'packet':
            self.table.horizontalHeaderItem(3).setToolTip('未收到可靠的技能施放次数，不能用命中次数代替。')
            for column in (6, 7, 8):
                self.table.horizontalHeaderItem(column).setToolTip('本场尚无经过验证的逐命中结果分类，缺失值不会填成 0%。')
            for column in (5, 9):
                self.table.horizontalHeaderItem(column).setToolTip('根据本场已记录的该技能命中统计；不代表缺失命中的数值。')
        if postcombat:self.table.horizontalHeaderItem(3).setText('次数(计数)');self.table.horizontalHeaderItem(10).setText('详情 DPS')
        self.table.setAccessibleName('个人逐技能伤害表；未采集时为空，不显示虚构技能');self.table.setItemDelegateForColumn(2,DamageShareDelegate(self.table));box.addWidget(self.table,1)
        self.empty=text('','muted');self.empty.setWordWrap(True);box.addWidget(self.empty)
        if not self.detail['available']:self.empty.setText(self.detail['reason']+f"\n尚未归因到技能的伤害：{m['damage']:,.1f}。这些不是名为“未知”的真实技能。")
        else:
            rows=self.detail['rows'];self.table.setRowCount(len(rows))
            for i,row in enumerate(rows):
                def val(k,dec=0):return skill_stat_text(row,k,dec)
                share=row['damage']/m['damage']*100 if m['damage'] else 0;values=[(str(row['name'])+' · '+str(row['skill_id'])) if row.get('name') else f"Skill {row['skill_id']}",val('damage',1),f'{share:.2f}%',val('uses'),val('hits'),val('average',1),val('critical_percent',2),val('insight_percent',2),val('graze_percent',2),val('maximum',1),f"{row['damage']/report['duration']:,.1f}"]
                for c,value in enumerate(values):
                    numeric=[None,row['damage'],share,row.get('uses'),row.get('hits'),row.get('average'),row.get('critical_percent'),row.get('insight_percent'),row.get('graze_percent'),row.get('maximum'),row['damage']/report['duration']];item=QTableWidgetItem(value) if c==0 else SkillValueItem(value,numeric[c]);item.setToolTip('Skill ID '+str(row['skill_id'])+'\n'+name_tooltip(row));self.table.setItem(i,c,item)
                    if c == 0 and row.get('component_skill_ids'):
                        item.setToolTip(item.toolTip() + '\n按配置主技能合并分段；原始结算 ID：' +
                                        '、'.join(map(str, row['component_skill_ids'])))
                    if c in (4,5,9):
                        explanation=skill_stat_tooltip(row,{4:'hits',5:'average',9:'maximum'}[c])
                        if explanation:item.setToolTip(explanation)
                category = '结算类' if settlement_skill_name(row) else '其他技能'
                item = QTableWidgetItem(category)
                item.setToolTip('按技能 ID 识别伤害类型；结算类伤害已计入总伤害，不另行叠加。')
                self.table.setItem(i, 11, item)
            self.empty.setText(f"已归因 {self.detail['total']:,.1f} / {m['damage']:,.1f}；未归因 {self.detail['unattributed']:,.1f}。")
        self.table.setSortingEnabled(True);self.table.sortItems(1,Qt.DescendingOrder)
        self.category_filter.currentIndexChanged.connect(self.filter_skills)
        self.table.horizontalHeader().sortIndicatorChanged.connect(self.filter_skills)
        self.filter_skills()
        footer=QHBoxLayout();footer.addWidget(text('rDPS 尚未支持','small'));footer.addStretch();export=QPushButton('导出技能 CSV');self.export_button=export;export.setEnabled(self.detail['available']);export.setToolTip('导出全部技能及伤害类型；界面筛选不影响导出范围。');export.clicked.connect(self.export);footer.addWidget(export);close=QPushButton('返回总览');close.clicked.connect(self.accept);footer.addWidget(close);box.addLayout(footer)
    def filter_skills(self, *args):
        selected = self.category_filter.currentIndex() if self.damage_categories else 0
        visible = 0
        for i in range(self.table.rowCount()):
            settlement = self.table.item(i, 11).text() == '结算类'
            hide = (selected == 1 and not settlement) or (selected == 2 and settlement)
            self.table.setRowHidden(i, hide)
            visible += not hide
        self.filter_status.setText(f'显示 {visible} / {self.table.rowCount()} 项' +
                                   (' · 当前筛选无记录' if not visible else ''))

    def export(self):
        target,_=QFileDialog.getSaveFileName(self,'导出技能伤害','yylogs_skills.csv','CSV (*.csv)')
        if not target:return
        try:
            with Path(target).open('w',encoding='utf-8-sig',newline='') as f:
                keys=['skill_id','name','name_source','name_configuration_verified','damage','uses','hits','average','critical_percent','insight_percent','graze_percent','maximum','hits_basis','average_basis','maximum_basis','observed_statistics_reconciled','observed_hits','observed_damage','observed_maximum','原始技能ID','计算点ID','副本名称','Boss序号','Boss名称','遭遇标签','副本SID','伤害类型','合并结算技能ID','配置主技能ID'];writer=csv.DictWriter(f,keys,extrasaction='ignore');writer.writeheader()
                encounter=self.report.get('encounter') or {}
                writer.writerows({**row,'合并结算技能ID':';'.join(map(str,row.get('component_skill_ids',[]))),'配置主技能ID':row.get('display_main_skill_id',''),'伤害类型':'结算类' if settlement_skill_name(row) else '其他技能','原始技能ID':';'.join(map(str,row.get('raw_skill_ids',[]))),
                    '计算点ID':';'.join(map(str,row.get('calcpoint_ids',[]))),
                    '副本名称':encounter.get('dungeon_name') or encounter.get('catalog_name') or '',
                    'Boss序号':encounter.get('boss_order') or '', 'Boss名称':encounter.get('boss_name') or '',
                    '遭遇标签':encounter.get('label') or self.report.get('encounter_label',''),
                    '副本SID':encounter.get('dungeon_sid',self.report.get('dungeon_sid',''))}
                    for row in self.detail['rows'])
        except OSError as e:QMessageBox.warning(self,'导出失败',str(e))
