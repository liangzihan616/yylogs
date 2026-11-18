"""Standalone offline self Buff observation viewer; no capture controls."""
from copy import deepcopy
from collections import OrderedDict,defaultdict
from bisect import bisect_right
from pathlib import Path
import json
import math
from PySide6.QtCore import Qt,QRectF,Signal
from PySide6.QtGui import QColor,QPainter,QPen,QBrush,QPixmap
from PySide6.QtWidgets import (QDialog,QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QLineEdit,QComboBox,QScrollArea,QTableWidget,QTableWidgetItem,QHeaderView,QAbstractItemView,QFileDialog,QMessageBox,QToolTip,QLayout)
from yylogs.self_buff_timeline import write_csv
from yylogs.encounter_timeline import slice_encounter
from yylogs.boss_debuff_timeline import presence_tracks
from yylogs.buff_view_filters import combat_acquired_track
from yylogs.game_names import refresh_timeline,tooltip as name_tooltip,display_name
from yylogs.actor_names import source_name_map

MODE={"persistent_display":"常驻显示","finite_display":"限时显示","unspecified_display":"显示状态","replicated_state":"原生 Buff 状态","sampled_boss_state":"Boss Debuff 采样","boss_dispatch_state":"Boss Debuff 事件","packet_state":"抓包 Buff 状态"}

def seconds(value):return f"{value/1000:.3f} s"
def segment_time_range(segment):return seconds(segment["start_ms"])+" - "+seconds(segment["end_ms"])
def segment_source_label(segment,names,own_entity_id=None):
    """Display only this span's reported casters, using current-record names."""
    if segment.get("channel")=="gui_display_only":return "来源：未知"
    if segment.get("source_status")=="conflicting_reports":return "来源：冲突，无法确认"
    sources=segment.get("source_entity_ids")
    if sources is None:sources=[segment["source_entity_id"]] if segment.get("source_entity_id") else []
    sources=sorted(set(sources))
    labels=[names.get(source) or ("本人" if source==own_entity_id else "未解析（"+source+"）") for source in sources]
    labels=[label+"（"+source+"）" if labels.count(label)>1 else label for source,label in zip(sources,labels)]
    if segment.get("has_unknown_source") or not sources:labels.append("未知")
    return "来源（上报）："+"、".join(labels)
def provider_name(source,names):
    return names.get(source) or ('未解析角色 · '+source[:8] if source else '未知来源')

def provider_segment_label(segment,names):
    sources=segment.get('source_entity_ids')
    if sources is None:sources=[segment.get('source_entity_id')] if segment.get('source_entity_id') else []
    label='、'.join(provider_name(source,names) for source in sources) or '未知来源'
    if segment.get('has_unknown_source') and sources:label+='、未知来源'

    return label

def title(track):return display_name(track,"未命名 Buff")+" · "+str(track["buff_no"])
def source_text(segment):
    if "source_entity_ids" in segment:
        parts=[s+"（上报，未核验）" for s in segment["source_entity_ids"]]
        if segment.get("has_unknown_source"):parts.append("未知（不默认本人）")
        return "；".join(parts) or "未知（不默认本人）"
    if segment.get("source_status")=="conflicting_reports":return "来源冲突（保留原始证据）"
    return (segment.get("source_entity_id")+"（上报，未核验）") if segment.get("source_entity_id") else "未知（不默认本人）"
def track_source_label(track,own_entity_id=None):
    """Label the single Buff row; caster details stay in spans and the table."""
    if track.get('mirrored_from_self_buff'):return '本人增益 · 来源见明细'
    if track.get("channel")=="gui_display_only":return "显示通道 · 来源未知"
    if track.get("channel") not in ("boss_sampled","boss_events","boss_combined"):return "原生通道 · 来源见明细"
    if track.get("presence_aggregated") and not track.get("source_entity_id"):
        count=len(track.get("source_entity_ids",[]))
        channels=" / ".join({"boss_sampled":"状态采样","boss_events":"分发事件"}.get(value,value)
                            for value in track.get("source_channels",[]))
        channel_note=f" · {channels}" if channels else ""
        return f"合并时间轴 · {count} 个已报来源"+(" / 含未知来源" if track.get("has_unknown_source") else "")+channel_note+" · 明细见下表"
    source=track.get("source_entity_id")
    if source:
        return ("本人" if own_entity_id and source==own_entity_id else "来源实体")+" · "+source
    return "来源未知 · 实例 "+str(track.get("instance_id", "未知"))

def segment_note(segment):
    evidence=segment.get("state_observation_segments",[])
    if len({s.get("channel") for s in evidence})>1:
        return "同一效果的多通道观测；非服务器精确时刻；原始区间：\n"+"\n".join(
            MODE.get(s.get("mode"),s.get("mode","观测"))+" "+
            seconds(s["start_ms"])+" → "+seconds(s["end_ms"])+"；"+source_text(s)
            for s in evidence)
    if segment.get("channel")=="boss_events":return "客户端分发事件；非服务器生效时刻；"+("起点未知；" if segment["left_censored"] else "")+("终点未知；" if segment["right_censored"] else "")+"全场完整性未验证"
    return "；".join(x for x in ("区间开始前已存在（非重新施加）" if segment.get("inherited_at_window_start") else "","区间截断（非移除）" if segment.get("clipped_at_window_end") else "","起点未知" if segment["left_censored"] else "观测起点","终点未知" if segment["right_censored"] else "观测终点","显示不等于增伤" if segment["channel"]=="gui_display_only" else ("状态采样，可能遗漏短效果；非精确挂上/移除" if segment["channel"]=="boss_sampled" else "上报事件，非服务器时刻")) if x)

def _interval_union(rows):
    output=[]
    for a,b in sorted(rows):
        if output and a<=output[-1][1]:output[-1]=(output[-1][0],max(b,output[-1][1]))
        else:output.append((a,b))
    return output


def _remove_gaps(intervals,gaps):
    result=[];i=0
    for a,b in intervals:
        while i<len(gaps) and gaps[i][1]<=a:i+=1
        j=i
        while j<len(gaps) and gaps[j][0]<b:
            lo,hi=gaps[j]
            if lo>a:result.append((a,min(lo,b)))
            a=max(a,hi)
            if a>=b:break
            j+=1
        if a<b:result.append((a,b))
    return result


def observed_coverage(track,report):
    """Union known native intervals, clipped to this battle; never fill gaps."""
    duration=report.get("coverage_duration_ms",report.get("duration_ms"))
    if type(duration) not in (int,float) or not math.isfinite(duration) or duration<=0:
        return None
    channels=defaultdict(list)
    for segment in track.get("segments",[]):
        evidence=segment.get("state_observation_segments") or [segment]
        for seg in evidence:
            if seg.get("channel",track.get("channel"))=="gui_display_only":continue
            a,b=seg.get("start_ms"),seg.get("end_ms")
            if any(type(v) not in (int,float) or not math.isfinite(v) for v in (a,b)):continue
            a,b=max(0,a),min(duration,b)
            if a>=b:continue
            channel=seg.get("channel",track.get("channel"))
            channels[channel].append((a,b))
    intervals=[]
    for channel,rows in channels.items():
        gaps=(report.get('boss_buff_events',{}).get('gaps',[]) if channel=='boss_events' else
              report.get('boss_debuff',{}).get('unobserved_intervals',[]) if channel=='boss_sampled' else [])
        valid=[]
        for gap in gaps:
            lo,hi=gap.get('start_ms'),gap.get('end_ms')
            if all(type(v) in (int,float) and math.isfinite(v) for v in (lo,hi)) and lo<hi:valid.append((lo,hi))
        intervals.extend(_remove_gaps(_interval_union(rows),_interval_union(valid)))
    total=0.0;end=0.0
    for a,b in sorted(intervals):
        total+=max(0,b-max(a,end));end=max(end,b)
    return {"duration_ms":duration,"observed_ms":total,"fraction":total/duration}


class SelfBuffChart(QWidget):
    cursorChanged=Signal(float)
    LEFT=240;TOP=54;ROW=50;RIGHT=26
    def __init__(self,report,palette,*,source_names=None,show_coverage=False,show_cursor_time=False):
        super().__init__();self.report=report;self.p=palette;self.tracks=[];self.zoom=1;self.source_names={**report.get("actor_names",{}),**(source_names or {})};self.show_coverage=show_coverage;self.coverages=[];self.cursor=None
        self.show_cursor_time=show_cursor_time
        self._row_cache=OrderedDict();self._hover_cache={};self.row_render_count=0
        self.setMouseTracking(True);self.setAccessibleName("自身 Buff 观测时间轴；条形连接观测边界，同一 Boss Debuff 合并一条时间轴，悬停和下表保留来源；不代表已验证覆盖率；完整数值可在下方表格中查阅。")
        if self.show_coverage:self.setAccessibleName("Boss Debuff观测时间轴；每行显示去重观测覆盖率，悬停名称可读已观测秒数与战斗时长；非服务器精确覆盖。")
    def set_tracks(self,tracks,*,coverages=None):
        self._row_cache.clear();self._hover_cache.clear()
        self._provider_sources=sorted({v for t in tracks for seg in t.get('segments',[]) for v in (seg.get('source_entity_ids') or ([seg['source_entity_id']] if seg.get('source_entity_id') else []))})
        self.tracks=tracks;self.coverages=(coverages if coverages is not None else [observed_coverage(t,self.report) for t in tracks]) if self.show_coverage else [];self.setMinimumWidth(900*self.zoom);self.setMinimumHeight(self.TOP+len(tracks)*self.ROW+40);self.update()
    def coverage_text(self,index):
        value=self.coverages[index] if self.show_coverage else None
        return f"覆盖率 {value['fraction']:.1%}" if value is not None else "覆盖率 —"
    def x(self,t):return self.LEFT+t/max(self.report["duration_ms"],1)*max(100,self.width()-self.LEFT-self.RIGHT)
    def time_at(self,x):
        return min(self.report["duration_ms"],max(0,(x-self.LEFT)/max(100,self.width()-self.LEFT-self.RIGHT)*self.report["duration_ms"]))
    def set_cursor(self,time):
        if time==self.cursor:return
        self.cursor=time
        self.update()
    def paintEvent(self,event):
        q=QPainter(self);q.setRenderHint(QPainter.Antialiasing);p=self.p
        q.setPen(QColor(p["muted"]));q.drawText(8,19,"效果 / 观测通道")
        if not self.report["duration_ms"]:
            q.setPen(QColor(p["text"]));q.drawText(QRectF(self.LEFT,70,max(100,self.width()-self.LEFT-self.RIGHT),90),Qt.AlignCenter|Qt.TextWordWrap,"暂无可验证的战斗区间\n可切换到「原始采集诊断」核查准备期数据")
            return
        for i in range(7):
            t=self.report["duration_ms"]*i/6;x=int(self.x(t));q.setPen(QPen(QColor(p["line"]),1));q.drawLine(x,32,x,self.height()-25);q.setPen(QColor(p["muted"]));q.drawText(x-16,23,f"{t/1000:.0f}s")
        first=max(0,(event.rect().top()-self.TOP)//self.ROW)
        last=min(len(self.tracks),(event.rect().bottom()-self.TOP)//self.ROW+1)
        for index in range(first,last):
            q.drawPixmap(0,self.TOP+index*self.ROW,self._row_image(index))
        if self.cursor is not None:
            x=self.x(self.cursor);q.setPen(QPen(QColor(p["muted"]),1,Qt.DashLine))
            q.drawLine(int(x),32,int(x),self.height()-25)
        footer="\u6309 Buff \u4e0e\u65bd\u52a0\u8005\u5206\u884c \u00b7 \u659c\u7eb9\uff1a\u672a\u89c2\u6d4b / \u5e38\u9a7b\u663e\u793a \u00b7 \u5b9e\u8272\uff1a\u5df2\u89c2\u6d4b \u00b7 \u865a\u7ebf\uff1a\u8fb9\u754c\u672a\u77e5" if self.show_coverage else "\u540c Buff / \u540c\u6765\u6e90\u5408\u5e76 \u00b7 \u659c\u7eb9\uff1a\u672a\u89c2\u6d4b / \u5e38\u9a7b\u663e\u793a \u00b7 \u5b9e\u8272\uff1a\u5df2\u89c2\u6d4b \u00b7 \u865a\u7ebf\uff1a\u8fb9\u754c\u672a\u77e5"; q.setPen(QColor(p["muted"]));q.drawText(8,self.height()-7,"按提供者与刷新分段 · 点击查看时间与名称 · 空白为未观测" if any(t.get("provider_segmented") for t in self.tracks) else footer)
    def _row_image(self,index):
        key=(index,self.width(),self.devicePixelRatioF(),self.font().toString(),tuple(self.p.items()),
             self.LEFT,self.RIGHT,self.TOP,self.ROW,self.report['duration_ms'])
        cached=self._row_cache.get(key)
        if cached is not None:
            self._row_cache.move_to_end(key)
            return cached
        dpr=self.devicePixelRatioF()
        image=QPixmap(round(self.width()*dpr),round(self.ROW*dpr))
        image.setDevicePixelRatio(dpr);image.fill(Qt.transparent)
        q=QPainter(image);q.setRenderHint(QPainter.Antialiasing);q.setFont(self.font())
        q.translate(0,-self.TOP-index*self.ROW);p=self.p
        track=self.tracks[index]
        y=self.TOP+index*self.ROW;q.setPen(QColor(p["text"]));q.drawText(8,y+12,q.fontMetrics().elidedText(title(track),Qt.ElideRight,self.LEFT-20));q.setPen(QColor(p["muted"]))
        subtitle=track_source_label(track,self.report.get("own_entity_id"))
        if track.get("provider_segmented"):
            subtitle="、".join(provider_name(v,self.source_names) for v in track.get("source_entity_ids",[])) or "未知来源"
        if track.get("source_entity_id"):
            source=track["source_entity_id"]
            caster=self.source_names.get(source)
            if caster:subtitle=f"{caster} · {source}"
        if self.show_coverage:subtitle=self.coverage_text(index)+" · "+subtitle
        q.drawText(8,y+30,q.fontMetrics().elidedText(subtitle,Qt.ElideRight,self.LEFT-20))
        q.fillRect(QRectF(self.LEFT,y,max(100,self.width()-self.LEFT-self.RIGHT),32),QColor(p["raised"]))
        if track.get("channel") in ("boss_sampled","boss_combined"):
            for gap in self.report.get("boss_debuff",{}).get("unobserved_intervals",[]):
                rect=QRectF(self.x(gap["start_ms"]),y+3,max(1,self.x(gap["end_ms"])-self.x(gap["start_ms"])),26)
                color=QColor(p["muted"]);color.setAlpha(75)
                q.fillRect(rect,QBrush(color,Qt.BDiagPattern))
                if rect.width()>95:q.setPen(QColor(p["muted"]));q.drawText(rect,Qt.AlignCenter,"未观测")
        if track.get("channel") in ("boss_events","boss_combined"):
            for gap in self.report.get("boss_buff_events",{}).get("gaps",[]):
                rect=QRectF(self.x(gap["start_ms"]),y+3,max(1,self.x(gap["end_ms"])-self.x(gap["start_ms"])),26)
                color=QColor(p["muted"]);color.setAlpha(75);q.fillRect(rect,QBrush(color,Qt.BDiagPattern))
        for seg in track["segments"]:
            rect=QRectF(self.x(seg["start_ms"]),y+3,max(3,self.x(seg["end_ms"])-self.x(seg["start_ms"])),26)
            color=QColor(p["muted"] if seg["mode"]=="persistent_display" else p["good"])
            if track.get('provider_segmented'):
                source=seg.get('source_entity_id')
                if source in self._provider_sources:
                    number=self._provider_sources.index(source)
                    if number:color=QColor.fromHsl((color.hue()+number*47)%360,max(100,color.hslSaturation()),min(155,color.lightness()+number%2*20))
            q.fillRect(rect,QBrush(color,Qt.BDiagPattern if seg["mode"]=="persistent_display" else Qt.SolidPattern))
            q.setPen(QPen(QColor(p["text"]),1,Qt.DashLine if seg["left_censored"] or seg["right_censored"] else Qt.SolidLine));q.drawRect(rect)
            label=provider_segment_label(seg,self.source_names) if track.get('provider_segmented') else segment_time_range(seg)
            if track.get('provider_segmented') and rect.width()>30:
                q.setPen(QColor('#142b24' if color.lightnessF()>.65 else '#ffffff'))
                q.drawText(rect.adjusted(5,0,-5,0),Qt.AlignVCenter|Qt.AlignLeft,q.fontMetrics().elidedText(label,Qt.ElideRight,max(1,int(rect.width()-10))))
            elif q.fontMetrics().horizontalAdvance(label)+12<=rect.width():q.setPen(QColor(p["bg"]));q.drawText(rect,Qt.AlignCenter,label)
        q.end();self.row_render_count+=1
        self._row_cache[key]=image
        while len(self._row_cache)>32:self._row_cache.popitem(last=False)
        return image
    def mouseMoveEvent(self,event):
        in_plot=self.LEFT<=event.position().x()<=self.width()-self.RIGHT
        cursor_text=''
        if in_plot:
            self.cursor=self.time_at(event.position().x());self.update()
            self.cursorChanged.emit(self.cursor)
            if self.show_cursor_time and self.report.get('duration_ms',0)>0:
                cursor_text='当前时间 '+seconds(self.cursor)
        index=int((event.position().y()-self.TOP)//self.ROW)
        if 0<=index<len(self.tracks):
            track=self.tracks[index]
            if event.position().x()<self.LEFT:
                text=title(track)
                if self.show_coverage:
                    value=self.coverages[index]
                    text+="\n"+self.coverage_text(index)
                    if value:text+=f"（已观测 {value['observed_ms']/1000:.3f} 秒 / 战斗 {value['duration_ms']/1000:.3f} 秒）"
                    text+="\n重叠区间去重；未观测缺口不补齐，不代表服务器精确覆盖。\n"+track_source_label(track,self.report.get("own_entity_id"))
                QToolTip.showText(event.globalPosition().toPoint(),text,self)
                return
            for seg in self._hover_segments(track,self.time_at(event.position().x())):
                if self.x(seg["start_ms"])<=event.position().x()<=max(self.x(seg["end_ms"]),self.x(seg["start_ms"])+3):
                    text=segment_time_range(seg)+"\n"+segment_source_label(seg,self.source_names,self.report.get("own_entity_id"))
                    if track.get("provider_segmented"):text+="\n"+provider_segment_label(seg,self.source_names)+"\n点击查看时间与提供者名称"
                    if seg.get("handoff_kind")=="same_sample_source_handoff":
                        previous=seg["handoff_from_source_id"]
                        previous_name=self.source_names.get(previous) or previous
                        text+=f"\n\u89c2\u6d4b\u5230\u63a5\u66ff\uff0c\u524d\u4e00\u6765\u6e90 {previous_name}\uff1b\u7cbe\u786e\u8986\u76d6\u65f6\u523b\u672a\u77e5\u3002"
                    QToolTip.showText(event.globalPosition().toPoint(),(cursor_text+'\n' if cursor_text else '')+text,self);return
        if cursor_text:
            QToolTip.showText(event.globalPosition().toPoint(),cursor_text,self);return
        QToolTip.hideText()

    def mousePressEvent(self,event):
        index=int((event.position().y()-self.TOP)//self.ROW)
        if event.button()==Qt.LeftButton and 0<=index<len(self.tracks) and self.tracks[index].get('provider_segmented'):
            self.show_provider_details(self.tracks[index])
            event.accept();return
        super().mousePressEvent(event)

    def show_provider_details(self,track):
        dialog=QDialog(self);dialog.setAttribute(Qt.WA_DeleteOnClose);dialog.setWindowTitle(title(track)+' · 提供者与覆盖明细')
        dialog.resize(900,460);layout=QVBoxLayout(dialog)
        heading=QLabel('各时间区间对应的提供者；空白区间不补齐。')
        heading.setWordWrap(True);layout.addWidget(heading)
        rows=track.get('segments',[]);table=QTableWidget(len(rows),2)
        table.setObjectName('bossProviderDetails')
        table.setHorizontalHeaderLabels(['开始 / 结束（秒）','提供者'])
        table.setEditTriggers(QAbstractItemView.NoEditTriggers);table.setSelectionBehavior(QAbstractItemView.SelectRows)
        for i,segment in enumerate(rows):
            sources=segment.get('source_entity_ids',[])
            cells=[f"{segment['start_ms']/1000:.3f} — {segment['end_ms']/1000:.3f}",
                   ('、'.join(provider_name(v,self.source_names) for v in sources) or '未知来源')+('、未知来源' if sources and segment.get('has_unknown_source') else '')]
            for j,text in enumerate(cells):
                item=QTableWidgetItem(text);item.setToolTip(text);table.setItem(i,j,item)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1,QHeaderView.Stretch)
        layout.addWidget(table);close=QPushButton('关闭');close.clicked.connect(dialog.close);layout.addWidget(close)
        self._provider_dialog=dialog;dialog.open()
        return dialog

    def _hover_segments(self,track,time):
        key=id(track)
        if key not in self._hover_cache:
            rows=sorted(enumerate(track['segments']),key=lambda r:r[1]['start_ms'])
            starts=[];ends=[];maximum=-math.inf
            for _,s in rows:
                starts.append(s['start_ms']);maximum=max(maximum,s['end_ms']);ends.append(maximum)
            self._hover_cache[key]=(rows,starts,ends)
        rows,starts,ends=self._hover_cache[key]
        padding=3/max(100,self.width()-self.LEFT-self.RIGHT)*max(1,self.report['duration_ms'])
        i=bisect_right(starts,time)-1;candidates=[]
        while i>=0 and ends[i]+padding>=time:
            order,seg=rows[i]
            if time<=max(seg['end_ms'],seg['start_ms']+padding):candidates.append((order,seg))
            i-=1
        return [s for _,s in sorted(candidates,key=lambda r:r[0])]

    def leaveEvent(self,event):
        QToolTip.hideText()
        super().leaveEvent(event)

class SelfBuffTimelineDialog(QDialog):
    linked_record=Signal(object)
    def __init__(self,report,palette,parent=None,*,capture_folder=None,record_store=None):
        self.capture_folder=Path(capture_folder) if capture_folder is not None else None
        self.record_store=Path(record_store) if record_store is not None else None
        super().__init__(parent);report=refresh_timeline(report);self.report=report;self.window_views={};self.p=palette;self.setWindowTitle("yylogs · Buff / Boss Debuff 时间轴");self.resize(1180,820);self.setMinimumSize(860,620)
        # Keep warnings/details reachable in compact windows rather than
        # letting fixed-height chart content overlap the summary/table.
        outer=QVBoxLayout(self);outer.setContentsMargins(0,0,0,0)
        self.content_scroll=QScrollArea();self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QScrollArea.NoFrame)
        self.content_body=QWidget();self.content_body.setMinimumHeight(780)
        box=QVBoxLayout(self.content_body);box.setSizeConstraint(QLayout.SetMinimumSize)
        box.setContentsMargins(24,22,24,22);box.setSpacing(12)
        self.content_scroll.setWidget(self.content_body);outer.addWidget(self.content_scroll)
        head=QHBoxLayout();self.heading=QLabel("自身 Buff · 战斗时间轴");heading=self.heading;heading.setProperty("kind","heading");head.addWidget(heading);head.addStretch()
        self.damage_button=QPushButton("伤害明细");self.damage_button.setEnabled(self.capture_folder is not None);self.damage_button.clicked.connect(self.open_damage_details);head.addWidget(self.damage_button)
        self.json_button=QPushButton("导出 JSON");self.json_button.clicked.connect(lambda:self.export("json"));head.addWidget(self.json_button)
        self.csv_button=QPushButton("导出区间 CSV");self.csv_button.clicked.connect(lambda:self.export("csv"));head.addWidget(self.csv_button);box.addLayout(head)
        self.note=QLabel();self.note.setWordWrap(True);self.note.setProperty("kind","muted");box.addWidget(self.note)
        self.boundary_note=QLabel("缺少同场战斗的开战 / 结束证据：暂不生成战斗时间轴，也不计算覆盖率。准备期、脱战尾段仅在原始采集诊断中查看。")
        self.boundary_note.setWordWrap(True);box.addWidget(self.boundary_note)
        if report.get("display_channel_available") is False:
            self.channel_warning=QLabel("显示专用 Buff 通道未启用：函数元数据未展开；本场保留已校验通道，不能据此认为显示类 Buff 不存在。")
            self.channel_warning.setWordWrap(True);box.addWidget(self.channel_warning)
        boss=report.get("boss_debuff") or {}
        if boss.get("sample_count"):
            last=boss.get("last_success_ms")
            detail=("最后成功：采集 +"+seconds(last)) if last is not None else "没有成功的 Boss 状态快照"
            reasons=list(dict.fromkeys(g.get("reason","未知") for g in (boss.get("gaps",[])+boss.get("partial_observations",[]))))
            status=QLabel(f"Boss 状态观测：{boss.get('successful_snapshots',0)} 次完整快照 / {boss.get('partial_snapshots',0)} 次部分快照 / {len(boss.get('gaps',[]))} 处缺口 · "+detail+("\n缺口原因："+"；".join(reasons) if reasons else ""))
            status.setWordWrap(True);status.setTextFormat(Qt.PlainText);box.addWidget(status)
        event_info=report.get("boss_buff_events")
        if event_info is not None:
            message=f"Boss 事件通道：{event_info.get('event_count',0)} 条事件；完整性待实战核验，不与状态采样补齐。"
            if event_info.get("unavailable_reason"):message+=" 通道未就绪："+str(event_info["unavailable_reason"])
            if event_info.get("error"):message+=" 导入失败："+str(event_info["error"])
            if event_info.get("channel_disabled"):message+=" 本场通道异常后停止，缺口保留。"
            event_status=QLabel(message);event_status.setWordWrap(True);event_status.setTextFormat(Qt.PlainText);box.addWidget(event_status)
        self.view_mode=QComboBox();self.view_mode.addItem("战斗视图 · 等待边界证据", "encounter");self.view_mode.addItem("原始采集诊断 · 含准备期和结束尾段", "capture");self.view_mode.setAccessibleName("战斗与原始采集视图区分");box.addWidget(self.view_mode)
        if report.get("encounter_windows"):
            self.view_mode.removeItem(0)
            for index,window in enumerate(report["encounter_windows"]):
                reason=window.get("end_evidence",{}).get("evidence",{}).get("reason")
                partial_labels={"target_unavailable_not_verified_death":"目标不可读",
                                "settlement_final_observed":"最终结算自动保存",
                                "boss_boundary_observer_failed":"Boss 身份观测失败",
                                "manual_stop_not_boss_death":"手动停止"}
                label=(partial_labels.get(reason,"观测中断")+" · 开战后片段（结束未验证） ") if window.get("partial_observation") else "战斗观测区间 "
                if window.get("hp_depletion"):label="HP 归零观测（击杀未确认） "
                self.view_mode.insertItem(index,label+str(index+1), "encounter:"+str(index))
            self.view_mode.setCurrentIndex(0)
        controls=QHBoxLayout();self.query=QLineEdit();self.query.setPlaceholderText("筛选名称、Buff ID 或来源实体");self.query.setAccessibleName("筛选自身 Buff 名称编号或来源");controls.addWidget(self.query,2)
        self.channel=QComboBox();self.channel.addItem("全部通道",None);self.channel.addItem("显示状态","gui_display_only");self.channel.addItem("原生 Buff","replicated");self.channel.addItem("Boss Debuff（状态采样）","boss_sampled");self.channel.setAccessibleName("筛选观测通道");controls.addWidget(self.channel)
        self.channel.addItem("Boss Debuff 事件（待实测）","boss_events")
        self.scope=QComboBox();self.scope.addItem("进战后获得 / 刷新（默认）","combat");self.scope.addItem("已命名 Buff（含常驻）","named");self.scope.addItem("本轮变化 / 已命名",True);self.scope.addItem("全部 Buff / 未知 ID",False);self.scope.addItem("进战后 · 明确技能来源","skill");self.scope.setAccessibleName("是否显示全部初始 Buff");controls.addWidget(self.scope)
        if not report.get("encounter_windows"):
            self.view_mode.setCurrentIndex(self.view_mode.findData("capture"))
            self.scope.setCurrentIndex(self.scope.findData(False))
        self.recipient=QComboBox();self.recipient.addItem("自身 Buff","self");self.recipient.addItem("Boss Debuff（含未知 ID）","boss");self.recipient.setAccessibleName("选择自身或 Boss 效果时间轴");controls.addWidget(self.recipient)
        self.zoom=QComboBox();self.zoom.addItem("缩放 1×",1);self.zoom.addItem("缩放 2×",2);self.zoom.addItem("缩放 4×",4);self.zoom.setAccessibleName("调整时间轴缩放");controls.addWidget(self.zoom);box.addLayout(controls)
        self.chart=SelfBuffChart(report,palette,source_names=source_name_map(self.capture_folder) if self.capture_folder else {});scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(self.chart);scroll.setMinimumHeight(210);box.addWidget(scroll,2)
        self.table=QTableWidget(0,8);self.table.setHorizontalHeaderLabels(["效果 / ID","通道","状态","起点（采集秒）","终点（采集秒）","观测跨度","来源实体 ID","边界 / 说明"]);self.table.setAccessibleName("自身 Buff 观测区间明细，可键盘选择；不是覆盖率统计")
        self.table.verticalHeader().hide();self.table.setShowGrid(False);self.table.setEditTriggers(QAbstractItemView.NoEditTriggers);self.table.setSelectionBehavior(QAbstractItemView.SelectRows);self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents);self.table.horizontalHeader().setSectionResizeMode(0,QHeaderView.Interactive);self.table.setColumnWidth(0,175);box.addWidget(self.table,2)
        self.summary=QLabel();self.summary.setWordWrap(True);self.summary.setProperty("kind","muted");box.addWidget(self.summary)
        if report.get("warnings"):
            warning=QLabel("注意："+"；".join(report["warnings"]));warning.setWordWrap(True);box.addWidget(warning)
        footer=QLabel("名称来自本机配置快照，保留原始 ID；同名不同 ID 不合并，悬停查看名称证据。来源为运行时实体 ID，尚未关联数字角色 ID / 昵称；缺失保持未知。开始前已存在 / 结束仍存在的区间保留未知边界；不重复合并显示与原生记录。");footer.setWordWrap(True);footer.setProperty("kind","small");box.addWidget(footer)
        self.view_mode.currentIndexChanged.connect(self.refresh)
        for signal in (self.query.textChanged,self.recipient.currentIndexChanged,self.channel.currentIndexChanged,self.scope.currentIndexChanged,self.zoom.currentIndexChanged):signal.connect(self.refresh)
        self.chart.cursorChanged.connect(self.select_cursor_time)
        self.refresh()
    def select_cursor_time(self,time):
        self.chart.set_cursor(time)
        if not self.table.rowCount():return
        start_col=3;end_col=4
        best=None
        for row in range(self.table.rowCount()):
            try:
                start=float((self.table.item(row,start_col) or QTableWidgetItem()).text().replace(" s",""))
                end=float((self.table.item(row,end_col) or QTableWidgetItem()).text().replace(" s",""))
            except (TypeError,ValueError):continue
            if start*1000<=time<=end*1000:
                best=row;break
            distance=min(abs(time-start*1000),abs(time-end*1000))
            if best is None or distance<best[0]:best=(distance,row)
        row=best if isinstance(best,int) else best[1] if best else -1
        if row>=0:
            self.table.selectRow(row);self.table.scrollToItem(self.table.item(row,0))
    def open_damage_details(self):
        from yylogs.damage_candidate_views import DamageCandidateDialog
        try:
            dialog=DamageCandidateDialog(self.capture_folder,self.report,self.p,self,
                                         record_store=self.record_store)
            dialog.linked_record.connect(self.linked_record.emit)
            dialog.setAttribute(Qt.WA_DeleteOnClose);dialog.show();self.damage_dialog=dialog
        except (OSError,ValueError,KeyError,TypeError) as exc:
            QMessageBox.warning(self,"伤害明细暂不可用",str(exc))
    def refresh(self,*args):
        query=self.query.text().strip().casefold();channel=self.channel.currentData();focused=self.scope.currentData()
        raw_mode=self.view_mode.currentData()=="capture"
        view=self.view_mode.currentData()
        bounded=not raw_mode and view.startswith("encounter:")
        if view not in self.window_views:
            self.window_views[view]=self.report if raw_mode else (slice_encounter(self.report,self.report["encounter_windows"][int(view.split(":")[1])]) if bounded else {**self.report,"tracks":[],"observations":[],"duration_ms":0,"timeline_kind":"encounter_unavailable"})
        self.view_report=self.window_views[view]
        self.note.setText(("原始采集诊断 · "+self.report["origin"].replace("T"," ")+" · "+seconds(self.report["duration_ms"])+"\n包含准备期和尾段；零点不是开战时刻。" if raw_mode else ("战斗观测区间 · "+self.view_report["origin"].replace("T"," ")+" · "+seconds(self.view_report["duration_ms"])+"\n边界来自同场观测范围，不是精确服务器时间。" if bounded else "本次采集："+seconds(self.report["duration_ms"])+"；战斗时长未验证，不将采集时长当成战斗时长。"))+" 显示不等于增伤生效；rDPS 未计算。")
        self.heading.setText(("Boss Debuff" if self.recipient.currentData()=="boss" else "自身 Buff")+(" · 原始采集诊断（非战斗用时）" if raw_mode else " · 战斗时间轴"))
        self.chart.report=self.view_report
        no_encounter=not self.report.get("encounter_windows")
        self.boundary_note.setVisible(not raw_mode or no_encounter)
        self.boundary_note.setText("战斗零点按开战观测区间下界对齐；结束按结束观测区间上界裁切。边界非精确服务器时间，仍不计算增伤覆盖率或 rDPS。" if bounded else ("没有可验证的战斗边界，当前自动显示全部原始 Buff 采集（含准备期和尾段）；这些数据不代表战斗内覆盖率。" if raw_mode and no_encounter else "缺少同场战斗的开战 / 结束证据：暂不生成战斗时间轴，也不计算覆盖率。准备期、脱战尾段仅在原始采集诊断中查看。"))
        if bounded and self.view_report.get("partial_observation"):
            reason=self.view_report["selected_encounter"].get("end_evidence",{}).get("evidence",{}).get("reason")
            target_missing=reason=="target_unavailable_not_verified_death"
            label={"target_unavailable_not_verified_death":"目标不可读",
                   "boss_boundary_observer_failed":"Boss 身份观测失败",
                   "manual_stop_not_boss_death":"手动停止"}.get(reason,"观测中断")
            self.note.setText("开战后观测片段 · "+seconds(self.view_report["duration_ms"])+"\n"+label+"；终点取最后一次成功的 Boss 观测，Boss 死亡和完整战斗用时未验证。")
            self.heading.setText(self.heading.text()+" · "+label+"片段")
            self.boundary_note.setText("已观测开战，结束未验证。可查看片段内的自身 Buff 和 Boss Debuff；不计算全场覆盖率或 rDPS。")
            if self.view_report["selected_encounter"].get("hp_depletion"):
                self.note.setText("HP 归零观测片段 · "+seconds(self.view_report["duration_ms"])+"\n终点取持续归零段的首次采样上界；不含随后归零尾段，击杀和游戏结算用时未确认。")
                self.heading.setText(("Boss Debuff" if self.recipient.currentData()=="boss" else "自身 Buff")+" · HP 归零观测")
        self.json_button.setEnabled(raw_mode or bounded);self.csv_button.setEnabled(raw_mode or bounded)
        self.table.setHorizontalHeaderLabels(["效果 / ID","通道","状态","起点（采集秒）" if raw_mode else "起点（战斗秒）","终点（采集秒）" if raw_mode else "终点（战斗秒）","观测跨度","来源实体 ID","边界 / 说明"])
        selected=[]
        boss_mode=self.recipient.currentData()=="boss"
        self.scope.setEnabled(not boss_mode)
        self.scope.setToolTip("Boss 采样阶段显示全部已确认负面效果 ID，不受自身名称筛选影响。" if boss_mode else "自身 Buff 名称筛选")
        if boss_mode and not channel:
            available_events=self.view_report.get("boss_buff_events",{})
            channel="boss_events" if available_events.get("event_count",0)>0 and not available_events.get("error") else "boss_sampled"
        for track in self.view_report["tracks"]:
            if (track["channel"] in ("boss_sampled","boss_events")) != boss_mode:continue
            if channel and track["channel"]!=channel:continue
            original_track=next((item for item in self.report["tracks"] if item.get("key")==track.get("key")),track)
            if focused in ("combat","skill") and not boss_mode:
                track=combat_acquired_track(track,self.report,strict_skill=focused=="skill")
                if track is None:continue
            if focused=="named" and not boss_mode and not track.get("name"):continue
            if focused is True and not boss_mode and not track.get("name") and all(s["left_censored"] and s["right_censored"] for s in original_track["segments"]):continue
            terms=title(track)+" "+" ".join(s.get("source_entity_id") or "未知" for s in track["segments"])
            if query and query not in terms.casefold():continue
            selected.append(track)
        self.visible_tracks=selected;self.chart.zoom=self.zoom.currentData();chart_tracks=presence_tracks(selected) if boss_mode else selected;self.chart.set_tracks(chart_tracks);rows=[(t,s) for t in selected for s in t["segments"]];self.table.setRowCount(len(rows))
        for index,(track,seg) in enumerate(rows):
            values=[title(track),"显示" if track["channel"]=="gui_display_only" else ("Boss 状态采样" if track["channel"]=="boss_sampled" else ("Boss 事件（待实测）" if track["channel"]=="boss_events" else "原生")),MODE.get(seg["mode"],seg["mode"]),seconds(seg["start_ms"]),seconds(seg["end_ms"]),seconds(seg["observed_span_ms"]),source_text(seg),segment_note(seg)]
            for col,value in enumerate(values):
                item=QTableWidgetItem(value);item.setToolTip(value+("\n"+name_tooltip(track) if col==0 else ""));self.table.setItem(index,col,item)
            self.table.setRowHeight(index,36)
        self.summary.setText(f"显示 {len(chart_tracks)} 条时间轴 / {len(rows)} 个原始观测区间 · 初始原生 Buff {self.report['counts']['initial_native_instances']} 个 · "+("采集已停止，清理已核验" if self.report["cleanup_verified"] else "清理状态需核验")+(" · 无匹配记录" if not rows else ""))
        if focused in ("combat","skill") and not boss_mode:
            self.summary.setText(self.summary.text()+" · 已隐藏开战前已有且未刷新的状态、仅移除状态和已知负面效果；技能施加因果未核验。")
            if not self.report.get("encounter_windows"):self.summary.setText(self.summary.text()+" 缺少可用进战区间，暂不推定；可切换全部查看。")
        if focused=="named" and not boss_mode:self.summary.setText(self.summary.text()+" · 未命名 ID 已隐藏，可切换筛选查看")
        if boss_mode:
            info=self.view_report.get("boss_buff_events" if channel=="boss_events" else "boss_debuff",{})
            self.summary.setText(self.summary.text()+" · 同一 Buff ID 合并一条时间轴，不重复计算重叠区间；悬停与下表保留各施加者和原始分段，不补齐采集缺口。斜纹为未观测，不计算覆盖率或 RD。" if info.get("available") else "本次没有可用的 Boss Debuff 采样；历史记录不能事后补采。")
        if boss_mode and channel=="boss_events":
            self.summary.setText(self.summary.text()+f" · Boss 事件 {info.get('event_count',0)} 条；客户端分发观测，完整性未验证；不与状态采样补齐。")
        if boss_mode and info.get("terminal_reason"):
            self.summary.setText(self.summary.text()+" · Boss 通道提前停止："+str(info["terminal_reason"])+"；后续未采集，不能补全。")
        if boss_mode and info.get("gaps"):
            self.summary.setText(self.summary.text()+" · 存在 "+str(len(info["gaps"]))+" 处观测缺口；空白不能当作未生效。")
        if boss_mode and info.get("boundary_gap_observations"):
            self.summary.setText(self.summary.text()+" · 末尾有跨边界读取缺口；不推定移除时间。")
        if boss_mode and info.get("error"):
            self.summary.setText(self.summary.text()+" · "+str(info["error"]))
        if not raw_mode and not bounded:self.summary.setText("未生成战斗区间：历史采集缺少同步开战与结束证据，不由 Buff 的出现 / 消失反推。原始采集记录保持完整。")
    def export(self,kind):
        if self.view_report.get("timeline_kind")=="encounter_unavailable":return
        path,_=QFileDialog.getSaveFileName(self,"导出 Buff / Boss Debuff 时间轴","self_buff_timeline."+kind,"JSON (*.json)" if kind=="json" else "CSV (*.csv)")
        if not path:return
        try:
            if kind=="json":Path(path).write_text(json.dumps(self.view_report,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")
            else:write_csv(self.view_report,path)
        except (OSError,ValueError) as exc:QMessageBox.warning(self,"导出失败",str(exc))
