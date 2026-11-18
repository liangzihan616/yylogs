"""Member DPS/HP observation on synchronized, selectable battle timelines."""
from bisect import bisect_left, bisect_right
from PySide6.QtCore import Qt, QPointF, QRectF, Signal, QThread
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QDoubleSpinBox, QPushButton, QScrollArea, QFrame, QLayout, QToolTip)

from yylogs.member_status import (damage_curve, healing_curve, health_curve, interval_stats,
    load_member_health, boss_sample_at, health_observation_summary, blood_sacrifice_segments,
    amount_in_segments, health_level_key)


def health_value_text(row):
    if row.get('hp_max') is None:
        return f"{row['hp']:,.0f}（上限未采集）"
    return f"{row['hp']:,.0f}/{row['hp_max']:,.0f}（{row['ratio']:.1%}）"


class HealthLoadWorker(QThread):
    loaded = Signal(object)
    def __init__(self, folder, parent=None, *, record=None):
        super().__init__(parent)
        self.folder = folder
        self.record=record
    def run(self):
        try:
            value = load_member_health(self.folder,record=self.record,cancelled=self.isInterruptionRequested)
            if self.record and not self.isInterruptionRequested():
                from yylogs.boss_status_views import load_boss_curves
                timeline=(self.record.get("capture_summary") or {}).get("member_status") or {}
                if timeline.get("available"):
                    try:value["boss_curves"]=load_boss_curves(self.record,
                        origin_ms=timeline["origin_unix_ms"],duration_ms=timeline["duration_ms"])
                    except (OSError,ValueError,TypeError,KeyError):
                        value["boss_curves"]={"hp":[],"qi":[],"reason":"本场Boss状态读取失败"}
            if not self.isInterruptionRequested():self.loaded.emit(value)
        except InterruptedError:pass


class MemberCurveChart(QWidget):
    selectionChanged = Signal(float, float)
    cursorChanged = Signal(float)
    LEFT, RIGHT, TOP, BOTTOM = 220, 56, 32, 42
    BLOOD_BAND_HEIGHT = 20
    BLOOD_BAND_MARGIN = 0
    BLOOD_BAND_COLOR = "#c62828"
    def __init__(self, palette, kind):
        super().__init__()
        self.p, self.kind = palette, kind
        self.rows, self.times, self.starts = [], [], []
        self.hp_absolute=False
        self.duration = 0
        self.selection = (0, 0)
        self.cursor = None
        self.anchor = None
        self.boss_curves={"hp":[],"qi":[]}
        self.buff_segments=[]
        self.rate_label=None
        self.amount_label=None
        self.overlays=()
        self.setMinimumSize(360, 240)
        self.setMouseTracking(True)
        self.setAccessibleName("队员"+kind.upper()+"曲线" if kind in ("dps","hps") else "队员血量百分比曲线")

    def set_data(self, rows, duration):
        self._scene_cache = None
        self.rows, self.duration = rows, duration
        self.hp_absolute=self.kind=='hp' and health_level_key(rows)=='hp'
        if self.kind=='hp':
            self.setAccessibleName('队员血量数值曲线' if self.hp_absolute else '队员血量百分比曲线')
        self.times = [r["elapsed_ms"] for r in rows]
        self.starts = [r.get("start_ms",r["elapsed_ms"]) for r in rows]
        self.update()

    def x(self, time):
        return self.LEFT + time / max(1, self.duration) * max(1, self.width()-self.LEFT-self.RIGHT)

    def time_at(self, x):
        return min(self.duration, max(0, (x-self.LEFT) / max(1,self.width()-self.LEFT-self.RIGHT) * self.duration))

    def set_selection(self, start, end):
        self.selection = (start, end)
        self.update()

    def set_cursor(self, time):
        self.cursor = time
        self.update()

    def blood_band_rects(self):
        bottom=self.height()-self.BOTTOM
        band_top=bottom-self.BLOOD_BAND_MARGIN-self.BLOOD_BAND_HEIGHT
        result=[]
        for segment in self.buff_segments:
            left=max(self.LEFT,self.x(segment["start_ms"]))
            right=min(self.width()-self.RIGHT,self.x(segment["end_ms"]))
            if right>left:
                result.append((QRectF(left,band_top,right-left,self.BLOOD_BAND_HEIGHT),segment))
        return result

    def set_overlay(self,curves,keys):
        self._scene_cache = None
        self.boss_curves=curves
        self.overlays=tuple(keys) if self.kind in ("dps","hps") else ()
        self.update()

    def set_buff_segments(self,segments):
        self.buff_segments=list(segments or [])
        self.update()

    def boss_text_at(self,time):
        parts=[]
        for key in self.overlays:
            label="Boss血量" if key=="hp" else "Boss真气"
            sample=boss_sample_at(self.boss_curves.get(key,[]),time)
            parts.append(f"{label} {sample['ratio']:.1%}（{sample['elapsed_ms']/1000:.3f}s采样）" if sample else f"{label} 未观测")
        return " · ".join(parts)

    def health_text_at(self,time):
        row=boss_sample_at(self.rows,time)
        if row is None:return f"{time/1000:.2f}s · 未观测血量（不补值）"
        text=f"{time/1000:.2f}s · 血量 {health_value_text(row)}"
        if row.get('state'):
            observed=row.get('observed_unix_ms',row['unix_ms'])-row['unix_ms']+row['elapsed_ms']
            text+=f"\n{observed/1000:.2f}s 收到的血量状态"
        return text

    def rate_text_at(self, time):
        if not self.rows and self.buff_segments:
            active=any(row['start_ms']<=time<row['end_ms'] for row in self.buff_segments)
            ids=sorted({buff_no for row in self.buff_segments for buff_no in row.get('buff_nos',())})
            return "血祭 Buff：" + ("观测活跃" if active else "未观测到活跃区间") + (
                " · ID " + "、".join(map(str,ids)) if ids else " · 本段未采集到受到治疗事件")
        if not self.rows or self.kind not in ("dps", "hps"):
            return ""
        index = min(len(self.rows)-1, max(0, bisect_right(self.starts, time)-1))
        row = self.rows[index]
        amount_key = 'healing' if self.kind == 'hps' else 'damage'
        amount_label = self.amount_label or ('治疗' if self.kind == 'hps' else '伤害')
        rate_label = self.rate_label or self.kind.upper()
        text = f"{row['start_ms']/1000:.2f}s - {row['end_ms']/1000:.2f}s\n{rate_label} {row[self.kind]:,.1f} · {amount_label} {row[amount_key]:,.1f}"
        skills = row.get('skills') or []
        if skills and amount_key == 'damage':
            text += "\n\n技能明细（秒均 DPS / 伤害 / 该秒占比）" if row['end_ms']-row['start_ms']<=1000 else "\n\n技能明细（秒均 DPS / 伤害 / 本段占比）"
            limit = 12
            # Bound the popup for unusually dense seconds; totals remain exact.
            for skill in skills[:limit]:
                text += f"\n{skill['name']}  {skill['dps']:,.1f} DPS · {skill['damage']:,.1f} 伤害"
                if skill.get('share') is not None:
                    text += f" · {skill['share']:.1%}"
            if len(skills) > limit:
                text += f"\n另有 {len(skills)-limit} 项，可在详细伤害面板查看完整技能表"
        elif amount_key == 'damage' and row[amount_key]:
            text += "\n\n本段技能明细未保存"
        if self.overlays:
            text += "\n\n" + f"游标 {time/1000:.3f}s\n" + self.boss_text_at(time)
        if self.buff_segments:
            active=any(row['start_ms']<=time<row['end_ms'] for row in self.buff_segments)
            text += "\n\n血祭 Buff：" + ("观测活跃" if active else "未观测到活跃区间")
        return text

    def paintEvent(self, event):
        q=QPainter(self)
        key=(self.width(),self.height(),self.devicePixelRatioF(),id(self.rows),
             self.duration,id(self.boss_curves),self.overlays,id(self.buff_segments),
             self.hp_absolute,self.rate_label,self.kind,tuple(sorted(self.p.items())))
        cached=getattr(self,'_scene_cache',None)
        if cached is None or cached[0]!=key:
            scale=self.devicePixelRatioF()
            image=QPixmap(round(self.width()*scale),round(self.height()*scale))
            image.setDevicePixelRatio(scale);image.fill(Qt.transparent)
            static=QPainter(image)
            try:self._paint_static(static)
            finally:static.end()
            self._scene_cache=(key,image)
        q.drawPixmap(0,0,self._scene_cache[1])
        top,bottom=self.TOP,self.height()-self.BOTTOM
        if self.selection!=(0,self.duration) and self.selection[1]>self.selection[0]:
            shade=QColor(self.p['accent']);shade.setAlpha(35)
            q.fillRect(QRectF(self.x(self.selection[0]),top,self.x(self.selection[1])-self.x(self.selection[0]),bottom-top),shade)
        if self.cursor is not None:
            q.setPen(QPen(QColor(self.p['muted']),1,Qt.DashLine))
            q.drawLine(QPointF(self.x(self.cursor),top),QPointF(self.x(self.cursor),bottom))

    def _paint_static(self,q):
        q.setRenderHint(QPainter.Antialiasing)
        top,bottom=self.TOP,self.height()-self.BOTTOM
        width=self.width()-self.LEFT-self.RIGHT
        q.fillRect(QRectF(self.LEFT,top,width,bottom-top),QColor(self.p["raised"]))
        rate=self.kind in ("dps","hps")
        color=QColor(self.p["good"] if self.overlays else "#2f78c8" if rate else "#d9485f")
        high=(max((r[self.kind] for r in self.rows),default=1) if rate else
              max((r['hp'] for r in self.rows if not r.get('gap')),default=1) if self.hp_absolute else 1)
        high=max(high,1)
        for fraction in (0,.5,1):
            y=bottom-fraction*(bottom-top)
            q.setPen(QPen(QColor(self.p["line"]),1))
            q.drawLine(QPointF(self.LEFT,y),QPointF(self.width()-self.RIGHT,y))
            q.setPen(QColor(self.p["muted"]))
            label=f"{fraction*high:,.0f}" if rate or self.hp_absolute else f"{fraction*100:.0f}%"
            q.drawText(8,int(y+4),label)
            if self.overlays:
                q.drawText(QRectF(self.width()-self.RIGHT+5,y-9,self.RIGHT-7,20),Qt.AlignRight|Qt.AlignVCenter,f"{fraction*100:.0f}%")
        for i in range(7):
            time=self.duration*i/6
            x=self.x(time)
            q.setPen(QPen(QColor(self.p["line"]),1))
            q.drawLine(QPointF(x,top),QPointF(x,bottom))
            q.setPen(QColor(self.p["muted"]))
            time_label=f"{time/1000:.1f}s" if self.duration<6000 else f"{time/1000:.0f}s"
            q.drawText(int(x-12),bottom+23,time_label)
        q.setPen(QColor(self.p["text"]))
        q.drawText(8,20,self.rate_label+" · 分段受到治疗／秒" if self.rate_label=="受疗HPS" else
                   "HPS · 分段治疗／秒" if self.kind=="hps" else
                   "DPS · 分段伤害／秒" if self.kind=="dps" else
                   "血量 · 当前值" if self.hp_absolute else "血量 · 当前／上限")
        blood_bands=self.blood_band_rects()
        for band,_segment in blood_bands:
            q.fillRect(band,QColor(self.BLOOD_BAND_COLOR))
        if self.buff_segments:
            q.drawText(QRectF(self.LEFT,0,width,25),Qt.AlignLeft|Qt.AlignVCenter,"红色区段 · 已观测血祭")
        if self.overlays:
            q.drawText(QRectF(self.LEFT,0,width,25),Qt.AlignRight|Qt.AlignVCenter,"右轴 · Boss状态%")
            for key in self.overlays:
                q.setPen(QPen(QColor("#d9485f" if key=="hp" else "#2f78c8"),1.7,
                              Qt.SolidLine if key=="hp" else Qt.DashLine))
                previous=None
                for row in self.boss_curves.get(key,[]):
                    if row.get("gap"):
                        previous=None
                        continue
                    if not 0<=row["elapsed_ms"]<=self.duration:continue
                    point=QPointF(self.x(row["elapsed_ms"]),bottom-row["ratio"]*(bottom-top))
                    if previous and (previous[2] or 0<=row["elapsed_ms"]-previous[0]<=2500):
                        if row.get('state'):
                            corner=QPointF(point.x(),previous[1].y())
                            q.drawLine(previous[1],corner);q.drawLine(corner,point)
                        else:q.drawLine(previous[1],point)
                    q.drawEllipse(point,1.5,1.5)
                    if row.get('state') and row.get('valid_until_ms',row['elapsed_ms'])>row['elapsed_ms']:
                        q.drawLine(point,QPointF(self.x(min(self.duration,row['valid_until_ms'])),point.y()))
                    previous=(row["elapsed_ms"],point,row.get('state') is True)
        q.setPen(QPen(color,2))
        previous=None
        for row in self.rows:
            if row.get("gap"):
                previous=None
                continue
            value=row[self.kind] if rate else row['hp'] if self.hp_absolute else row["ratio"]
            point=QPointF(self.x(row["elapsed_ms"]),bottom-value/high*(bottom-top))
            if rate:
                left=QPointF(self.x(row["start_ms"]),point.y())
                right=QPointF(self.x(row["end_ms"]),point.y())
                if previous:q.drawLine(previous[1],left)
                q.drawLine(left,right)
                previous=(row,right)
            else:
                if row.get('state') and 'valid_until_ms' in row:
                    if previous and previous[0].get('valid_until_ms')==row['elapsed_ms']:
                        q.drawLine(previous[1],point)
                    right=QPointF(self.x(row['valid_until_ms']),point.y())
                    q.drawLine(point,right)
                elif previous and row.get("continuous", True):
                    q.drawLine(previous[1],point)
                q.drawEllipse(point,2,2)
                previous=(row,right if row.get('state') and 'valid_until_ms' in row else point)
        q.setPen(QPen(QColor("#ffffff"),1))
        for band,segment in blood_bands:
            q.drawRect(band)
        q.setPen(QColor("#ffffff"))
        for band,segment in blood_bands:
            if band.width()<24:continue
            label=f"{segment['start_ms']/1000:.2f}s - {segment['end_ms']/1000:.2f}s"
            if q.fontMetrics().horizontalAdvance(label)+10>band.width():label="血祭"
            q.drawText(band,Qt.AlignCenter,label)
        if not any(not r.get("gap") for r in self.rows):
            q.setPen(QColor(self.p["muted"]))
            q.drawText(QRectF(self.LEFT,top,width,bottom-top),Qt.AlignCenter|Qt.TextWordWrap,
                       "本场未保存此角色的治疗时间数据" if self.kind=="hps" else "本场未保存此角色的伤害时间数据" if self.kind=="dps" else "本场未保存此角色的血量采样")

    def mousePressEvent(self,event):
        if event.button()==Qt.LeftButton and self.LEFT<=event.position().x()<=self.width()-self.RIGHT:
            self.anchor=self.time_at(event.position().x())

    def mouseMoveEvent(self,event):
        time=self.time_at(event.position().x())
        if event.position().x()<self.LEFT:
            QToolTip.hideText()
            return
        self.cursorChanged.emit(time)
        if self.anchor is not None:
            self.selectionChanged.emit(min(time,self.anchor),max(time,self.anchor))
        if self.rows:
            if self.kind in ("dps","hps"):
                text = self.rate_text_at(time)
            else:
                text = self.health_text_at(time)
            QToolTip.showText(event.globalPosition().toPoint(),text,self)

    def mouseReleaseEvent(self,event):
        if self.anchor is not None:
            time=self.time_at(event.position().x())
            if abs(time-self.anchor)>=1:
                self.selectionChanged.emit(min(time,self.anchor),max(time,self.anchor))
            self.anchor=None

    def leaveEvent(self,event):
        QToolTip.hideText()


class MemberStatusPage(QWidget):
    def __init__(self,record,palette,*,prepared=None):
        super().__init__()
        self.setObjectName("memberStatusObservation")
        self.record,self.p=record,palette
        self.timeline=(record.get("capture_summary") or {}).get("member_status") or {}
        self.healing_summary=(record.get('healing_summary') or
            (record.get('capture_summary') or {}).get('healing_summary') or {})
        self.healing_timeline=self.healing_summary.get('timeline') or {}
        self.received_healing_timeline=self.healing_summary.get('received_timeline') or {}
        self.received_record_count=self.healing_summary.get('received_record_count')
        self.received_by_recipient=self.healing_summary.get('received_by_recipient')
        self.blood_ids=[]
        self.buff_timeline=record.get('buff_timeline') or {}
        self.rate='dps'
        self.duration=self.timeline.get("duration_ms",0)
        self.health={"available":False,"samples":[],"reason":"正在读取本场血量采样…"}
        self.boss_curves={"hp":[],"qi":[]}
        self.worker=None
        self.dps_rows,self.hp_rows=[],[]
        outer=QVBoxLayout(self);outer.setContentsMargins(0,0,0,0)
        self.scroll=QScrollArea();self.scroll.setWidgetResizable(True);self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content=QWidget();box=QVBoxLayout(content);box.setSizeConstraint(QLayout.SetMinimumSize)
        box.setContentsMargins(0,16,0,12);box.setSpacing(12)
        self.scroll.setWidget(content);outer.addWidget(self.scroll)
        heading=QLabel("队员状态观测");heading.setProperty("kind","heading");box.addWidget(heading)
        controls=QHBoxLayout()
        controls.addWidget(QLabel("角色"))
        self.member=QComboBox();self.member.setAccessibleName("选择观测角色")
        from yylogs.observation_preferences import own_actor, observation_members, remember_own
        own = own_actor(record)
        if own is None and record.get('capture_backend') == 'packet':
            self.member.addItem('请选择本人角色', None)
        for m in observation_members(record):self.member.addItem((m.get("name") or "名称未解析")+(" · 本人" if m['id'] == own else ""),m["id"])
        existing={self.member.itemData(i) for i in range(self.member.count())}
        for entity,member in self.timeline.get("members",{}).items():
            if entity not in existing:
                self.member.addItem((member.get("name") or "名称未解析")+" · 无已采伤害",entity)
                existing.add(entity)
        for entity,member in self.healing_timeline.get("members",{}).items():
            if entity not in existing:
                self.member.addItem((member.get("name") or "名称未解析")+" · 治疗",entity)
                existing.add(entity)
        for entity,member in self.received_healing_timeline.get("members",{}).items():
            if entity not in existing:
                self.member.addItem((member.get("name") or "名称未解析")+" · 受疗",entity)
                existing.add(entity)
        controls.addWidget(self.member,1)
        self.set_own = QPushButton('设为本人')
        self.set_own.setToolTip('记住本人角色，队员 Buff 和队员状态观测默认选择本人')
        self.set_own.setVisible(record.get('capture_backend') == 'packet')
        def set_own():
            actor = self.member.currentData()
            if actor is None:return
            try:
                remember_own(record, actor)
                self.set_own.setToolTip('已记住本人角色')
                self.sync_own_selection()
            except (OSError, ValueError) as exc:self.set_own.setToolTip(f'保存失败：{exc}')
        self.set_own.clicked.connect(set_own)
        controls.addWidget(self.set_own)
        controls.addWidget(QLabel("曲线分段"))
        self.interval=QComboBox();self.interval.setAccessibleName("伤害或治疗曲线分段秒数")
        for seconds in (1,3,5):self.interval.addItem(f"{seconds} 秒",seconds*1000)
        controls.addWidget(self.interval);box.addLayout(controls)
        modes=QHBoxLayout();modes.addWidget(QLabel("曲线对照"))
        self.boss_mode=QComboBox();self.boss_mode.setAccessibleName("选择队友状态曲线模式")
        for name,keys in (("DPS + Boss 血量 / 真气",("hp","qi")),
                          ("HPS + Boss 真气 + Boss 血量",("hps","qi","hp")),
                          ("血祭 + HPS",("blood","received"))):
            self.boss_mode.addItem(name,keys)
        modes.addWidget(self.boss_mode,1);box.addLayout(modes)
        self.overlay_note=QLabel();self.overlay_note.setWordWrap(True);self.overlay_note.setProperty("kind","small")
        box.addWidget(self.overlay_note)
        ranges=QHBoxLayout();ranges.addWidget(QLabel("区间"))
        self.start=QDoubleSpinBox();self.end=QDoubleSpinBox()
        for spin,name in ((self.start,"区间开始秒"),(self.end,"区间结束秒")):
            spin.setDecimals(3);spin.setRange(0,self.duration/1000);spin.setSuffix(" s");spin.setAccessibleName(name)
            spin.setStyleSheet(f"QDoubleSpinBox {{background:{palette['side']};color:{palette['text']};border:1px solid {palette['line']};padding:4px;}} QDoubleSpinBox QLineEdit {{background:{palette['side']};color:{palette['text']};}}")
        self.end.setValue(self.duration/1000)
        ranges.addWidget(self.start);ranges.addWidget(QLabel("至"));ranges.addWidget(self.end)
        self.reset=QPushButton("全场");self.reset.clicked.connect(lambda:self.select_range(0,self.duration))
        ranges.addWidget(self.reset);ranges.addStretch();box.addLayout(ranges)
        self.extrema=QComboBox();self.extrema.setAccessibleName("选择DPS峰值或低谷区间")
        self.extrema.addItem("定位高低区间",None)
        for name,key in (("DPS最高区间","peak"),("DPS最低区间","low"),("血量最高时段","hp_high"),("血量最低时段","hp_low")):
            self.extrema.addItem(name,key)
        self.extrema.currentIndexChanged.connect(self.select_extreme)
        ranges.addWidget(self.extrema)
        self.dps_stats=QLabel();self.dps_stats.setWordWrap(True);self.dps_stats.setProperty("kind","muted");box.addWidget(self.dps_stats)
        self.dps_chart=MemberCurveChart(palette,"dps");box.addWidget(self.dps_chart)
        self.hp_stats=QLabel();self.hp_stats.setWordWrap(True);self.hp_stats.setProperty("kind","muted");box.addWidget(self.hp_stats)
        self.hp_chart=MemberCurveChart(palette,"hp");box.addWidget(self.hp_chart)
        explanation="DPS/HPS为已采事件分段值；HPS是已采治疗，不等于有效治疗。血祭模式显示自身 Buff 的观测区间与该角色受到的已采治疗/秒，不代表增伤已触发或达到阈值；缺失数据不补0。Boss和队员血量缺口不连线。最终累计技能表不补造分段伤害，分段合计可与最终统计不同。"
        if (record.get("capture_summary") or {}).get("known_truncated"):
            explanation='本场记录已截断，后段曲线可能不完整。\n'+explanation
        self.note=QLabel(explanation)
        self.note.setWordWrap(True);self.note.setProperty("kind","small")
        self.note.setAccessibleName("队员状态观测说明")
        self.note.setAccessibleDescription(explanation)
        box.addWidget(self.note)
        # Keep the explanation as a fixed page-bottom note. A widget tooltip
        # here competes with the data tooltip shown by the chart itself.
        self.dps_chart.setAccessibleDescription("HPS 为已采治疗，不等于有效治疗；血祭模式显示该角色受到的已采治疗，缺失数据不补0。")
        for chart in (self.dps_chart,self.hp_chart):
            chart.selectionChanged.connect(self.select_range)
            chart.cursorChanged.connect(self.sync_cursor)
        self.start.valueChanged.connect(self.range_changed);self.end.valueChanged.connect(self.range_changed)
        self.member.currentIndexChanged.connect(self.refresh);self.interval.currentIndexChanged.connect(self.refresh)
        self.boss_mode.currentIndexChanged.connect(self.overlay_changed)
        self.refresh()
        if prepared is not None:self.health_loaded(prepared)
        elif record.get("capture_folder") or record.get("buff_timeline") or record.get("boss_health") or record.get("boss_qi"):
            self.worker=HealthLoadWorker(record.get("capture_folder"),self,record=record)
            self.worker.loaded.connect(self.health_loaded)
            self.worker.start()
        else:self.health_loaded(load_member_health(None))

    def sync_own_selection(self):
        from yylogs.observation_preferences import own_actor
        actor = own_actor(self.record)
        members = {m['id']: m for m in self.record.get('members', [])}
        for i in range(self.member.count()):
            member = members.get(self.member.itemData(i))
            if member:
                self.member.setItemText(i, (member.get('name') or '名称未解析')
                                        + (' · 本人' if member['id'] == actor else ''))
        if actor is not None:
            index = self.member.findData(actor)
            if index >= 0:self.member.setCurrentIndex(index)

    def health_loaded(self,value):
        self.health=value
        if "boss_curves" in value:
            self.boss_curves=value["boss_curves"]
            if self.boss_curves.get('qi_absolute'):
                self.boss_curves={**self.boss_curves,'qi':[],'qi_reason':'真气仅有原值，请在 Boss 状态页查看'}
        self.refresh()

    def refresh(self):
        entity=self.member.currentData()
        self.dps_rows=damage_curve(self.timeline,entity,self.interval.currentData() or 1000)
        self.hp_rows=health_curve(self.health,entity,self.timeline.get("origin_unix_ms",0),self.duration)
        self.dps_chart.set_data(self.dps_rows,self.duration)
        self.hp_chart.set_data(self.hp_rows,self.duration)
        self.overlay_changed()
        self.range_changed()

    def overlay_changed(self):
        selection=tuple(self.boss_mode.currentData() or ())
        self.blood_mode='blood' in selection
        keys=tuple(key for key in selection if key in ('hp','qi'))
        self.rate='hps' if ('hps' in selection or self.blood_mode) else 'dps'
        self.dps_chart.kind=self.rate
        self.dps_chart.setAccessibleName('队员'+('受疗HPS' if self.blood_mode else self.rate.upper())+'与Boss状态对照曲线')
        entity=self.member.currentData()
        interval=self.interval.currentData() or 1000
        if self.blood_mode:
            self.rate_rows=healing_curve(self.received_healing_timeline,entity,interval)
        elif self.rate=='hps':
            self.rate_rows=healing_curve(self.healing_timeline,entity,interval)
        else:
            self.rate_rows=self.dps_rows
        self.buff_segments=(blood_sacrifice_segments(self.buff_timeline,entity,
            self.timeline.get('origin_unix_ms',0),self.duration) if self.blood_mode else [])
        self.blood_ids=sorted({buff_no for row in self.buff_segments
                               for buff_no in row.get('buff_nos',())})
        self.dps_chart.rate_label='受疗HPS' if self.blood_mode else self.rate.upper()
        self.dps_chart.amount_label='受到治疗' if self.blood_mode else ('治疗' if self.rate=='hps' else '伤害')
        has_received=any(row.get('healing',0)>0 for row in self.rate_rows) if self.blood_mode else False
        received_count=(self.received_by_recipient.get(entity,0) if isinstance(self.received_by_recipient,dict)
                        else self.received_record_count)
        if received_count is None:received_count=1 if has_received else 0
        if self.blood_mode and not has_received and received_count==0:
            self.rate_rows=[]
        self.dps_chart.set_data(self.rate_rows,self.duration)
        self.dps_chart.set_buff_segments(self.buff_segments)
        rate_label='受疗HPS' if self.blood_mode else self.rate.upper()
        self.extrema.setItemText(1,rate_label+'最高区间')
        self.extrema.setItemText(2,rate_label+'最低区间')
        self.extrema.setAccessibleName('选择'+rate_label+'峰值或低谷区间')
        self.dps_chart.set_overlay(self.boss_curves,keys)
        if self.blood_mode:
            missing=[]
            if not self.rate_rows:missing.append('受到治疗时间数据')
            if not self.buff_segments:missing.append('血祭 Buff')
            ids=("；ID "+"、".join(map(str,self.blood_ids))) if self.blood_ids else ""
            if self.buff_segments and not self.rate_rows:
                self.overlay_note.setText("已观测到血祭（红色自身 Buff"+ids+"） · 本场未采集到受到治疗事件")
            else:
                self.overlay_note.setText("血祭 红色区段（自身 Buff"+ids+"） · 受疗HPS 蓝实线（左轴）"+("；未保存："+"、".join(missing) if missing else ""))
        elif keys:
            missing=["Boss血量" if key=="hp" else "Boss真气" for key in keys
                     if not any(not row.get("gap") for row in self.boss_curves.get(key,[]))]
            labels=[rate_label+" 绿实线（左轴）"]
            if "hp" in keys:labels.append("Boss血量 红实线（右轴）")
            if "qi" in keys:labels.append("Boss真气 蓝虚线（右轴）")
            if self.rate=='hps' and not self.rate_rows:missing.append('治疗时间数据')
            self.overlay_note.setText(" · ".join(labels)+("；未保存："+"、".join(missing) if missing else ""))
        else:self.overlay_note.setText("")
        self.overlay_note.setVisible(bool(keys) or self.blood_mode)
        self.range_changed()

    def select_range(self,start,end):
        for spin,value in ((self.start,start),(self.end,end)):
            spin.blockSignals(True);spin.setValue(value/1000);spin.blockSignals(False)
        self.range_changed()

    def select_extreme(self,index):
        key=self.extrema.currentData()
        if not key:return
        if key.startswith("hp_"):
            rows=[r for r in self.hp_rows if not r.get("gap")]
            if rows:
                hp_key=health_level_key(rows)
                row=(max if key=="hp_high" else min)(rows,key=lambda r:r[hp_key])
                self.select_range(max(0,row["elapsed_ms"]-500),min(self.duration,row["elapsed_ms"]+500))
        elif self.rate_rows:
            row=(max if key=="peak" else min)(self.rate_rows,key=lambda r:r[self.rate])
            self.select_range(row["start_ms"],row["end_ms"])
        self.extrema.blockSignals(True);self.extrema.setCurrentIndex(0);self.extrema.blockSignals(False)

    def sync_cursor(self,time):
        self.dps_chart.set_cursor(time);self.hp_chart.set_cursor(time)

    def range_changed(self):
        start,end=self.start.value()*1000,min(self.duration,self.end.value()*1000)
        for chart in (self.dps_chart,self.hp_chart):chart.set_selection(start,end)
        if start>=end:
            self.dps_stats.setText("请选择结束晚于开始的时间区间")
            self.hp_stats.setText("血量：—")
            return
        stats=interval_stats(self.rate_rows,self.hp_rows,start,end,rate=self.rate)
        span=f"{start/1000:.2f}s - {end/1000:.2f}s"
        rate_label='受疗HPS' if self.blood_mode else self.rate.upper()
        if self.blood_mode and not self.rate_rows:
            ids=(" · ID "+"、".join(map(str,self.blood_ids))) if self.blood_ids else ""
            observed="已观测到血祭"+ids if self.buff_segments else "未观测到血祭 Buff"
            self.dps_stats.setText(f"{span} · {observed} · 本场未采集到受到治疗事件")
            self.dps_stats.setToolTip("血祭 Buff 与受疗事件分别观测；没有治疗事件时不补造HPS或累计受疗。")
            if stats["hp_low"]:
                low,high=stats["hp_low"],stats["hp_high"]
                self.hp_stats.setText(f"区间血量 · 最低 {health_value_text(low)} · 最高 {health_value_text(high)}")
            else:self.hp_stats.setText(self.health.get("reason") or "所选区间没有此角色的血量采样（不视为0血）")
            return
        if stats['average_'+self.rate] is None:self.dps_stats.setText("本场没有可用的队员"+('受到治疗' if self.blood_mode else '治疗' if self.rate=='hps' else '伤害')+"时间数据")
        else:
            peak,low=stats["peak"],stats["low"]
            self.dps_stats.setText(f"{span} · 平均{rate_label} {stats['average_'+self.rate]:,.1f} · 峰值 {peak[self.rate]:,.1f}（{peak['start_ms']/1000:.1f}-{peak['end_ms']/1000:.1f}s） · 最低 {low[self.rate]:,.1f}（{low['start_ms']/1000:.1f}-{low['end_ms']/1000:.1f}s）")
            if self.blood_mode:
                self.dps_stats.setText(self.dps_stats.text()+f" · 区间累计受疗 {stats['healing']:,.1f}")
                if self.buff_segments:
                    blood_amount=amount_in_segments(self.rate_rows,self.buff_segments,start,end)
                    self.dps_stats.setText(self.dps_stats.text()+f" · 血祭内累计受疗 {blood_amount:,.1f}")
            if self.dps_chart.overlays:
                status=self.dps_chart.boss_text_at(peak["elapsed_ms"])
                self.dps_stats.setText(self.dps_stats.text()+"\n峰值段中点对照："+status)
            self.dps_stats.setToolTip("区间跨分段边界时，边缘分段按相交时长均匀折算；不用于替换原总览DPS。")
        if stats["hp_low"]:
            low,high=stats["hp_low"],stats["hp_high"]
            self.hp_stats.setText(f"区间血量 · 最低 {health_value_text(low)}（{max(start,low['elapsed_ms'])/1000:.2f}s） · 最高 {health_value_text(high)}（{max(start,high['elapsed_ms'])/1000:.2f}s）")
            observation=health_observation_summary(self.hp_rows,start,end)
            if self.health.get('basis')=='packet_property_state':
                self.hp_stats.setText(self.hp_stats.text()+f"\n血量状态覆盖 {observation['observed_ms']/1000:.2f}s / {(end-start)/1000:.2f}s")
            else:
                self.hp_stats.setText(self.hp_stats.text()+f"\n有效采样 {observation['sample_count']} · 读取失败 {observation['gap_count']} · 曲线断点 {observation['line_break_count']} · 单角色最大间隔 {observation['max_interval_ms']/1000:.2f}s · 连续观测 {observation['observed_ms']/1000:.2f}s / {(end-start)/1000:.2f}s")
        else:self.hp_stats.setText(self.health.get("reason") or "所选区间没有此角色的血量采样（不视为0血）")

    def stop_worker(self):
        if self.worker and self.worker.isRunning():
            self.worker.requestInterruption()
            return self.worker.wait(2000)
        return True

    def closeEvent(self,event):
        if self.stop_worker():event.accept()
        else:event.ignore()
