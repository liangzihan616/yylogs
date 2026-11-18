"""Saved-report preparation off the GUI thread; no collector or game access."""
import os
import time
import weakref
from PySide6.QtCore import Qt,QThread,QTimer,QPropertyAnimation,QEasingCurve
from PySide6.QtGui import QPainter,QColor,QPen
from PySide6.QtWidgets import QWidget,QVBoxLayout,QLabel,QPushButton,QGraphicsOpacityEffect


def motion_enabled():
    if os.environ.get("YYLOGS_REDUCED_MOTION")=="1":return False
    if os.name=="nt":
        try:
            import ctypes
            enabled=ctypes.c_int(1)
            if ctypes.windll.user32.SystemParametersInfoW(0x1042,0,ctypes.byref(enabled),0):
                return bool(enabled.value)
        except (OSError,AttributeError):pass
    return True


class LoadingIndicator(QWidget):
    def __init__(self,palette,parent=None):
        super().__init__(parent);self.p=palette;self.angle=0
        self.setFixedSize(28,28)
        self.timer=QTimer(self);self.timer.setInterval(45);self.timer.timeout.connect(self.advance)
    def advance(self):self.angle=(self.angle+18)%360;self.update()
    def showEvent(self,event):
        if motion_enabled():self.timer.start()
        super().showEvent(event)
    def hideEvent(self,event):self.timer.stop();super().hideEvent(event)
    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(self.p["line"]),2.5));painter.drawEllipse(3,3,22,22)
        painter.setPen(QPen(QColor(self.p["accent"]),2.5,Qt.SolidLine,Qt.RoundCap))
        painter.drawArc(3,3,22,22,-self.angle*16,110*16)


class AnalysisLoadingPage(QWidget):
    def __init__(self,palette,title,retry=None):
        super().__init__();self.p=palette;self.setObjectName("analysisLoading")
        box=QVBoxLayout(self);box.setContentsMargins(24,32,24,24);box.setSpacing(12)
        box.addStretch()
        self.indicator=LoadingIndicator(palette,self);box.addWidget(self.indicator,0,Qt.AlignHCenter)
        self.label=QLabel("正在准备"+title+"…");self.label.setProperty("kind","heading")
        self.label.setAlignment(Qt.AlignCenter);self.label.setAccessibleName(self.label.text())
        box.addWidget(self.label)
        self.retry=QPushButton("重试");self.retry.setVisible(False)
        if retry:self.retry.clicked.connect(retry)
        box.addWidget(self.retry,0,Qt.AlignHCenter);box.addStretch()
    def failed(self,message,title="观测数据暂不可用"):
        self.indicator.hide();self.label.setText(title)
        self.label.setToolTip(message);self.retry.show()


def reveal_page(page):
    """A short opacity transition; never animate chart values or geometry."""
    if not motion_enabled():return
    old=getattr(page,"_reveal_animation",None)
    if old:old.stop();old.deleteLater()
    effect=QGraphicsOpacityEffect(page);effect.setOpacity(.72);page.setGraphicsEffect(effect)
    animation=QPropertyAnimation(effect,b"opacity",page)
    animation.setDuration(120);animation.setStartValue(.72);animation.setEndValue(1)
    animation.setEasingCurve(QEasingCurve.OutCubic)
    page._reveal_animation=animation
    target=weakref.ref(page)
    def finish():
        from shiboken6 import isValid
        widget=target()
        if widget is not None and isValid(widget):
            widget.setGraphicsEffect(None)
    animation.finished.connect(finish)
    animation.start()


class AnalysisPreparation(QThread):
    def __init__(self,record,recipient,generation,parent=None):
        super().__init__(parent);self.record=record;self.recipient=recipient
        self.generation=generation;self.result=None;self.error=None;self.elapsed_ms=0
    def run(self):
        started=time.perf_counter()
        try:
            from yylogs.observation_views import legacy_observation_record,prepare_self_status
            from yylogs.boss_status_views import prepare_boss_status,load_boss_curves
            from yylogs.member_status import load_member_health
            record=self.record
            updated = None
            if self.recipient == 'boss' and record.get('_rebuild_boss_target'):
                from yylogs.packet_records import load_packet_record
                from yylogs.packet_entities import preferred_entity
                snapshot = record['buff_timeline']['packet_snapshot']
                rebuilt = load_packet_record(snapshot, record['record_folder'], boss_target_id=preferred_entity(record))
                updated = {**record, **rebuilt}
                updated.pop('_rebuild_boss_target', None)
                record = updated
                if self.isInterruptionRequested():raise InterruptedError()
            if self.recipient == 'actions':
                from yylogs.player_actions import prepare_player_actions
                data = prepare_player_actions(record, self.isInterruptionRequested)
            elif self.recipient=="members":
                data=load_member_health(record.get("capture_folder"),record=record,cancelled=self.isInterruptionRequested)
                timeline=(record.get("capture_summary") or {}).get("member_status") or {}
                if timeline.get("available") and not self.isInterruptionRequested():
                    data["boss_curves"]=load_boss_curves(record,origin_ms=timeline["origin_unix_ms"],
                        duration_ms=timeline["duration_ms"])
            elif self.recipient=='self' and record.get('capture_backend')=='packet':
                from yylogs.packet_observations import prepare_packet_self
                data=prepare_packet_self(record,self.isInterruptionRequested)
            else:
                if not record.get("buff_timeline") and (record.get("timeline") or record.get("buffs")):
                    record=legacy_observation_record(record)
                if record.get("buff_timeline"):
                    data=(prepare_boss_status if self.recipient=="boss" else prepare_self_status)(
                        record,self.isInterruptionRequested)
                else:data=None
            if updated is not None and data is not None:data['_record_update'] = updated
            if not self.isInterruptionRequested():self.result=data
        except InterruptedError:pass
        except Exception as exc:self.error=f"{type(exc).__name__}: {exc}"
        finally:self.elapsed_ms=(time.perf_counter()-started)*1000
