"""Boss status observation page: HP/Qi curves and prioritized debuffs.

This module only reads saved report data. It never starts collection and never
turns missing samples into zeroes or interpolated values.
"""
from datetime import datetime
import json
import math
from pathlib import Path

from PySide6.QtCore import Qt, QPointF, QRectF, QSize, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QBrush, QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QFrame,
    QPushButton, QToolButton, QLayout, QDialog, QDialogButtonBox, QLineEdit,
    QListWidget, QListWidgetItem, QToolTip, QComboBox,
)

from yylogs.boss_debuff_timeline import presence_tracks, provider_tracks
from yylogs.game_names import display_name, refresh_timeline
from yylogs.icons import icon
from yylogs.self_buff_views import SelfBuffChart, source_text, title, observed_coverage
from yylogs.encounter_timeline import slice_encounter
from yylogs.actor_names import source_name_map


PRIORITY_NAMES = (
    "幻鸣",
    "青衣魅",
    "易伤",
    "气竭层数标记",
    "真气失衡",
    "惧刃",
    "胆寒",
    "离猎",
    "步乱",
    "烁鼓",
)
SHUOGU_BUFF_IDS = frozenset((200216, 200228, 200229, 200230, 240000094))
PRIORITY_SHUOGU_BUFF_ID = 200228
PRIORITY_BUFF_IDS = {200176: "惧刃", PRIORITY_SHUOGU_BUFF_ID: "烁鼓"}
LIN_BUFF_IDS = frozenset((1058032,))
QI_MISSING = "本场未保存真气观测"
MAX_JSONL_ROWS = 100000
MAX_JSONL_BYTES = 64 * 1024 * 1024
MAX_CURVE_GAP_MS = 2500


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.timestamp() * 1000


def _row_elapsed(row, base_ms):
    for key in ("elapsed_ms", "time_ms", "relative_ms"):
        value = row.get(key)
        if _number(value):
            return float(value)
    if _number(row.get("timestamp_ms")) and base_ms is not None:
        return float(row["timestamp_ms"]) - base_ms
    stamp = _timestamp(row.get("timestamp") or row.get("observed_at") or row.get("observed_at_local"))
    return None if stamp is None or base_ms is None else stamp - base_ms


def _read_jsonl(path):
    if not path.is_file() or path.stat().st_size > MAX_JSONL_BYTES:
        return []
    rows = []
    with path.open(encoding="utf-8-sig") as stream:
        for index, line in enumerate(stream):
            if index >= MAX_JSONL_ROWS:
                break
            if line.strip():
                try:
                    value = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    return rows


def _resource_values(row):
    resource = row.get("resource") if isinstance(row.get("resource"), dict) else row
    current = None
    maximum = None
    for key in ("current", "qi", "value", "amount"):
        if _number(resource.get(key)):
            current = float(resource[key])
            break
    for key in ("maximum", "max", "qi_max", "max_value"):
        if _number(resource.get(key)):
            maximum = float(resource[key])
            break
    return current, maximum


def _load_hp_rows(record):
    rows = []
    timeline = record.get("buff_timeline") or record.get("timeline") or {}
    embedded = timeline.get("boss_curves", {}).get("hp", []) if isinstance(timeline, dict) else []
    for row in embedded:
        if isinstance(row, dict):
            if row.get("gap"):
                rows.append({"timestamp_ms": _timestamp(row.get("timestamp")), "gap": True})
                continue
            hp, maximum = row.get("hp"), row.get("hp_max")
            if _number(hp) and _number(maximum) and maximum > 0 and 0 <= hp <= maximum:
                rows.append({"timestamp_ms": _timestamp(row.get("timestamp")),
                             "hp": float(hp), "maximum": float(maximum),
                             "gap": bool(row.get("gap")), "state": row.get('state') is True})
    folder = Path(record.get("capture_folder", "")) if record.get("capture_folder") else None
    for folder in ([] if rows else [folder]):
        if not folder or not (folder / "samples.jsonl").is_file():
            continue
        for row in _read_jsonl(folder / "samples.jsonl"):
            health = row.get("boss_health") if isinstance(row.get("boss_health"), dict) else row
            if row.get("status") == "gap" or health.get("gap"):
                rows.append({"timestamp_ms": _timestamp(row.get("timestamp")), "gap": True})
                continue
            hp, maximum = health.get("hp"), health.get("hp_max")
            if not (_number(hp) and _number(maximum) and maximum > 0 and 0 <= hp <= maximum):
                continue
            rows.append({
                "timestamp_ms": _timestamp(row.get("timestamp")),
                "hp": float(hp),
                "maximum": float(maximum),
                "gap": row.get("status") == "gap",
            })
        for row in _read_jsonl(folder / "boss_health_sample_gaps.jsonl"):
            rows.append({"timestamp_ms": _timestamp(row.get("timestamp")), "gap": True})
    for row in timeline.get("samples", []) if isinstance(timeline.get("samples"), list) else []:
        hp, maximum = row.get("hp"), row.get("hp_max")
        if _number(hp) and _number(maximum) and maximum > 0:
            rows.append({
                "timestamp_ms": _timestamp(row.get("timestamp")),
                "elapsed_ms": row.get("elapsed_ms", row.get("elapsed")),
                "hp": float(hp),
                "maximum": float(maximum),
                "gap": bool(row.get("gap")),
            })
    return rows


def _load_qi_rows(record):
    boss_qi = record.get("boss_qi")
    rows = list(boss_qi.get("samples", [])) if isinstance(boss_qi, dict) else []
    timeline = record.get("buff_timeline") or record.get("timeline") or {}
    embedded = timeline.get("boss_curves", {}).get("qi", []) if isinstance(timeline, dict) else []
    if not rows:
        rows = embedded
    folder = Path(record.get("capture_folder", "")) if record.get("capture_folder") else None
    if not rows:
        for folder in [folder]:
            if not folder:
                continue
            for name in ("boss_qi.jsonl", "boss_qi_observations.jsonl",
                         "boss_resource_observations.jsonl", "boss_qi_candidates.jsonl"):
                rows = _read_jsonl(folder / name)
                if rows:
                    break
            if rows:
                break
    if folder:
        gap_report = folder / "boss_qi_read_gaps.json"
        if gap_report.is_file() and gap_report.stat().st_size <= 1024 * 1024:
            try:
                gaps = json.loads(gap_report.read_text("utf-8-sig")).get("gaps", [])
            except (OSError, TypeError, ValueError):
                gaps = []
            rows.extend({"timestamp": gap.get("timestamp"), "gap": True}
                        for gap in gaps if isinstance(gap, dict) and _timestamp(gap.get("timestamp")) is not None)
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("status") == "gap" or row.get("gap"):
            result.append({"gap": True, "timestamp_ms": _timestamp(row.get("timestamp")) if isinstance(row, dict) else None})
            continue
        if (row.get('scale')=='absolute' and _number(row.get('qi')) and row['qi']>=0
                and not (_number(row.get('qi_max')) and row['qi_max'] > 0)):
            result.append({'timestamp_ms':_timestamp(row.get('timestamp')),'qi':row['qi'],
                           'absolute':True,'gap':False,'state':row.get('state') is True})
            continue
        current, maximum = _resource_values(row)
        resource = row.get("resource") if isinstance(row.get("resource"), dict) else row
        if not (_number(current) and _number(maximum) and maximum > 0 and 0 <= current <= maximum
                and resource.get("numeric_range_consistent") is not False):
            result.append({"gap": True, "timestamp_ms": _timestamp(row.get("timestamp"))})
            continue
        result.append({
            "timestamp_ms": _timestamp(row.get("timestamp") or row.get("observed_at")),
            "elapsed_ms": row.get("elapsed_ms", row.get("time_ms")),
            "qi": current,
            "maximum": maximum,
            "gap": False,
            "state": row.get('state') is True,
        })
    return result


def load_boss_curves(record, *, origin_ms=None, duration_ms=None):
    """Return normalized HP and Qi samples on the report's local time axis."""
    hp_rows = _load_hp_rows(record)
    qi_rows = _load_qi_rows(record)
    timestamps = [
        row["timestamp_ms"] for row in hp_rows + qi_rows
        if _number(row.get("timestamp_ms"))
    ]
    base_ms = origin_ms if _number(origin_ms) else (min(timestamps) if timestamps else None)

    def normalize(rows, value_key):
        output = []
        absolute_max=max([row.get(value_key,0) for row in rows if row.get('absolute')] or [0])
        for row in rows:
            elapsed = row.get("elapsed_ms")
            if not _number(elapsed):
                elapsed = _row_elapsed(row, base_ms)
            if not _number(elapsed) or elapsed < 0 or (_number(duration_ms) and elapsed > duration_ms):
                continue
            if row.get("gap"):
                output.append({"elapsed_ms": float(elapsed), "gap": True})
                continue
            maximum = absolute_max if row.get('absolute') else row.get("maximum")
            if row.get('absolute') and not maximum:maximum=1
            value = row.get(value_key)
            if not (_number(value) and _number(maximum) and maximum > 0):
                continue
            output.append({
                "elapsed_ms": float(elapsed),
                "value": float(value),
                "maximum": float(maximum),
                "absolute":bool(row.get('absolute')),
                "state": row.get('state') is True,
                "ratio": min(1.0, max(0.0, float(value) / float(maximum))),
                "gap": bool(row.get("gap")),
            })
        output.sort(key=lambda item: (item["elapsed_ms"], 0 if item.get("gap") else 1))
        deduplicated = []
        for row in output:
            if (row.get("gap") and deduplicated and deduplicated[-1].get("gap")
                    and row["elapsed_ms"] == deduplicated[-1]["elapsed_ms"]):
                continue
            deduplicated.append(row)
        output = deduplicated
        connected = []
        previous = None
        for row in output:
            if (previous is not None and not previous.get('state') and not row.get("gap")
                    and row["elapsed_ms"] - previous["elapsed_ms"] > MAX_CURVE_GAP_MS):
                connected.append({"elapsed_ms": row["elapsed_ms"], "gap": True})
            connected.append(row)
            previous = None if row.get("gap") else row
        for index, row in enumerate(connected):
            if row.get('state') and not row.get('gap'):
                until = connected[index + 1]['elapsed_ms'] if index + 1 < len(connected) else duration_ms
                if _number(until):row['valid_until_ms'] = max(row['elapsed_ms'], float(until))
        return connected

    hp = normalize(hp_rows, "hp")
    qi = normalize(qi_rows, "qi")
    duration = max([row["elapsed_ms"] for row in hp + qi] or [0.0])
    boss_qi = record.get("boss_qi")
    reason = (boss_qi or {}).get("reason") if isinstance(boss_qi, dict) else None
    return {
        "hp": hp,
        "qi": qi,
        "duration_ms": duration,
        "qi_available": any(not row.get("gap") for row in qi),
        "qi_absolute":any(row.get('absolute') for row in qi),
        "qi_state":any(row.get('state') for row in qi),
        "qi_axis_max":max([row.get('value',0) for row in qi if row.get('absolute')] or [0]),
        "qi_reason": reason or QI_MISSING,
    }


def _is_self_track(track):
    return track.get("channel") in ("gui_display_only", "replicated")


def _mirrored_self_buff_tracks(report):
    """Copy selected self damage buffs into Boss display without moving originals."""
    mirrored = []
    for track in report.get("tracks", []):
        name = track.get("name") or ""
        is_mirrored_buff = ("霖" in name or "烁鼓" in name or
                           track.get("buff_no") in SHUOGU_BUFF_IDS | LIN_BUFF_IDS)
        own = report.get('_display_own_entity_id') or report.get('own_entity_id')
        packet_self = (track.get('channel') == 'packet_self' and own is not None
                       and track.get('target_entity_id') == own)
        if not (_is_self_track(track) or packet_self) or not is_mirrored_buff:
            continue
        item = dict(track)
        item["key"] = "boss_mirror:" + str(track.get("key", track.get("buff_no")))
        item["channel"] = "boss_sampled"
        item["category"] = "boss_debuff"
        item["mirrored_from_self_buff"] = True
        item["name"] = name or ("烁鼓" if track.get("buff_no") in SHUOGU_BUFF_IDS else "增伤·霖")
        mirrored.append(item)
    return mirrored


def boss_display_tracks(report, *, refresh_names=True):
    """Build the Boss display set and return priority and collapsed tracks."""
    if refresh_names:
        report = refresh_timeline(report, metadata_only=True)
    source = [
        track for track in report.get("tracks", [])
        if track.get("channel") in ("boss_sampled", "boss_events", "packet_boss")
    ]
    source.extend(_mirrored_self_buff_tracks(report))
    grouped = provider_tracks(source)
    priority = {name: index for index, name in enumerate(PRIORITY_NAMES)}

    def rank(track):
        name = display_name(track, "")
        buff_no = track.get("buff_no")
        if (buff_no in SHUOGU_BUFF_IDS or "烁鼓" in name) and buff_no != PRIORITY_SHUOGU_BUFF_ID:
            return len(PRIORITY_NAMES) + 1, name, str(buff_no)
        priority_name = PRIORITY_BUFF_IDS.get(buff_no, name)
        if priority_name in priority:
            return priority[priority_name], name, str(track.get("buff_no"))
        if "霖" in name or "烁鼓" in name:
            return len(PRIORITY_NAMES), name, str(track.get("buff_no"))
        return len(PRIORITY_NAMES) + 1, name or "未命名 Buff", str(track.get("buff_no"))

    ordered = sorted(grouped, key=rank)
    fixed = [track for track in ordered if rank(track)[0] <= len(PRIORITY_NAMES)]
    other = [track for track in ordered if rank(track)[0] > len(PRIORITY_NAMES)]
    return fixed, other


class BossCurveChart(QWidget):
    cursorChanged=Signal(float)
    LEFT, TOP, RIGHT, BOTTOM = 220, 30, 26, 62
    HP_COLOR = "#d9485f"
    QI_COLOR = "#2f78c8"

    def __init__(self, curves, palette):
        super().__init__()
        self.curves, self.p = curves, palette
        self._geometry_cache = {}
        self._base_cache=None;self.base_render_count=0
        self._has_samples=any(not row.get('gap') for key in ('hp','qi') for row in curves.get(key,[]))
        self.cursor=None
        self.setMouseTracking(True)
        self.setMinimumSize(360, 268)
        self.setAccessibleName("Boss 血量和真气归一化曲线；红色为血量，蓝色为真气，绿色为真气归零；空白表示没有连续采样")

    def set_curves(self, curves):
        self.curves = curves
        self._base_cache=None
        self._has_samples=any(not row.get('gap') for key in ('hp','qi') for row in curves.get(key,[]))
        self._geometry_cache.clear()
        self.update()
    def set_cursor(self,time):
        if self.cursor==time:return
        self.cursor=time
        self.update()

    def _x(self, elapsed, duration, right):
        return self.LEFT + elapsed / max(duration, 1.0) * (right - self.LEFT)

    def _draw_curve(self, painter, rows, color, duration, right, top, bottom):
        if not rows:
            return
        pen = QPen(QColor(color), 2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        key = (id(rows), duration, right, top, bottom)
        cached = self._geometry_cache.get(key)
        if cached is not None:
            path, markers = cached
            painter.drawPath(path)
            painter.setPen(Qt.NoPen);painter.setBrush(QColor(color))
            for point in markers:painter.drawEllipse(point,2.0,2.0)
            painter.setBrush(Qt.NoBrush)
            return
        path = QPainterPath()
        markers = []
        previous = None
        last_marker = None
        for row in rows:
            if row.get("gap"):
                if previous is not None:
                    markers.append(previous[1])
                previous = None
                last_marker = None
                continue
            point = QPointF(self._x(row["elapsed_ms"], duration, right),
                            bottom - row["ratio"] * (bottom - top))
            if previous is None or not (previous[2] or 0 <= row["elapsed_ms"] - previous[0] <= MAX_CURVE_GAP_MS):
                if previous is not None:
                    markers.append(previous[1])
                path.moveTo(point)
                markers.append(point)
                last_marker = point
            else:
                if row.get('state'):
                    path.lineTo(QPointF(point.x(), previous[1].y()))
                path.lineTo(point)
                # Retain every observed vertex; limit only marker density.
                if (abs(point.x() - last_marker.x()) >= 4
                        or abs(point.y() - last_marker.y()) >= 4):
                    markers.append(point)
                    last_marker = point
            if row.get('state') and row.get('valid_until_ms', row['elapsed_ms']) > row['elapsed_ms']:
                path.lineTo(QPointF(self._x(row['valid_until_ms'], duration, right), point.y()))
            previous = (row["elapsed_ms"], point, row.get('state') is True)
        if previous is not None:
            markers.append(previous[1])
        if len(self._geometry_cache)>=4:self._geometry_cache.clear()
        self._geometry_cache[key]=(path,markers)
        painter.drawPath(path)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color))
        for point in markers:
            painter.drawEllipse(point, 2.0, 2.0)
        painter.setBrush(Qt.NoBrush)

    def _draw_zero_qi(self, painter, rows, duration, right, top, bottom):
        """Recolor observed zero Qi only; leave recovery edges and gaps intact."""
        key = ('qi-zero', id(rows), duration, right, top, bottom)
        cached = self._geometry_cache.get(key)
        if cached is None:
            path, markers = QPainterPath(), []
            previous = last_marker = None
            for row in rows:
                if row.get('gap'):
                    previous = last_marker = None
                    continue
                elapsed, zero = row['elapsed_ms'], row['ratio'] == 0
                point = QPointF(self._x(elapsed, duration, right),
                                bottom - row['ratio'] * (bottom - top))
                if (previous is not None and previous[3]
                        and (previous[2] or 0 <= elapsed - previous[0] <= MAX_CURVE_GAP_MS)
                        and (row.get('state') or zero)):
                    path.moveTo(previous[1])
                    path.lineTo(QPointF(point.x(), bottom))
                if zero:
                    if last_marker is None or abs(point.x() - last_marker.x()) >= 4:
                        markers.append(point)
                        last_marker = point
                    until = row.get('valid_until_ms', elapsed)
                    if row.get('state') and until > elapsed:
                        path.moveTo(point)
                        path.lineTo(QPointF(self._x(until, duration, right), bottom))
                else:
                    last_marker = None
                previous = (elapsed, point, row.get('state') is True, zero)
            if previous is not None and previous[3]:
                markers.append(previous[1])
            if len(self._geometry_cache) >= 4:
                self._geometry_cache.clear()
            self._geometry_cache[key] = cached = (path, markers)
        color = QColor(self.p['good'])
        pen = QPen(color, 2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(cached[0])
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        for point in cached[1]:
            painter.drawEllipse(point, 2.0, 2.0)
        painter.setBrush(Qt.NoBrush)

    def _draw_cursor(self,painter,duration,right,top,bottom):
        if self.cursor is None:return
        x=self._x(self.cursor,duration,right)
        painter.setPen(QPen(QColor(self.p["muted"]),1,Qt.DashLine))
        painter.drawLine(QPointF(x,top),QPointF(x,bottom))

    def paintEvent(self, event):
        key=(self.width(),self.height(),self.devicePixelRatioF(),self.font().toString(),tuple(self.p.items()),
             self.LEFT,self.RIGHT,self.TOP,self.BOTTOM)
        if self._base_cache is None or self._base_cache[0]!=key:
            dpr=self.devicePixelRatioF();image=QPixmap(round(self.width()*dpr),round(self.height()*dpr))
            image.setDevicePixelRatio(dpr);image.fill(Qt.transparent)
            base=QPainter(image);base.setFont(self.font());self._paint_base(base);base.end()
            self._base_cache=(key,image);self.base_render_count+=1
        painter=QPainter(self);painter.drawPixmap(0,0,self._base_cache[1])
        self._draw_cursor(painter,max(1000.0,float(self.curves.get('duration_ms') or 0)),
                          float(max(self.LEFT+120,self.width()-self.RIGHT)),float(self.TOP),
                          float(max(self.TOP+100,self.height()-self.BOTTOM)))

    def _paint_base(self,painter):
        painter.setRenderHint(QPainter.Antialiasing)
        right = float(max(self.LEFT + 120, self.width() - self.RIGHT))
        top = float(self.TOP)
        bottom = float(max(top + 100, self.height() - self.BOTTOM))
        has_samples = self._has_samples
        duration = max(1000.0, float(self.curves.get("duration_ms") or 0.0))
        painter.fillRect(QRectF(self.LEFT, top, right - self.LEFT, bottom - top),
                         QColor(self.p["raised"]))
        painter.setPen(QPen(QColor(self.p["line"]), 1))
        for fraction in (0.0, 0.5, 1.0):
            y = bottom - fraction * (bottom - top)
            painter.drawLine(QPointF(self.LEFT, y), QPointF(right, y))
            painter.setPen(QColor(self.p["muted"]))
            painter.drawText(4, int(y + 5), f"{fraction * 100:.0f}%")
            painter.setPen(QPen(QColor(self.p["line"]), 1))
        for index in range(7):
            elapsed = duration * index / 6
            x = self._x(elapsed, duration, right)
            painter.drawLine(QPointF(x, top), QPointF(x, bottom))
            painter.setPen(QColor(self.p["muted"]))
            painter.drawText(int(x - 15), int(bottom + 25), f"{elapsed / 1000:.0f}s")
            painter.setPen(QPen(QColor(self.p["line"]), 1))

        painter.setPen(QColor(self.p["text"]))
        painter.drawText(8, 18, "Boss 血量 / 真气 · 归一化采样曲线")
        legend_x = max(self.LEFT + 150, int(right - 230))
        for offset, color, name in ((0, self.HP_COLOR, "血量"), (116, self.QI_COLOR, "真气")):
            painter.setPen(QPen(QColor(color), 3))
            painter.drawLine(legend_x + offset, 15, legend_x + offset + 18, 15)
            painter.setPen(QColor(self.p["text"]))
            painter.drawText(legend_x + offset + 24, 19, name)

        if not has_samples:
            painter.setPen(QColor(self.p["muted"]))
            painter.drawText(QRectF(self.LEFT, top, right - self.LEFT, bottom - top),
                             Qt.AlignCenter | Qt.TextWordWrap,
                             "本场没有可绘制的 Boss 血量 / 真气观测")
            painter.setPen(QColor(self.p["subtle"]))
            painter.drawText(8, self.height() - 10, "没有保存采样，不以 0 代替缺失数据")
            return

        self._draw_curve(painter, self.curves.get("hp", []), self.HP_COLOR,
                         duration, right, top, bottom)
        self._draw_curve(painter, self.curves.get("qi", []), self.QI_COLOR,
                         duration, right, top, bottom)
        self._draw_zero_qi(painter, self.curves.get('qi', []),
                           duration, right, top, bottom)
        if not self.curves.get("qi_available"):
            painter.setPen(QColor(self.p["muted"]))
            painter.drawText(QRectF(self.LEFT, bottom + 28, right - self.LEFT, 18),
                             Qt.AlignRight, self.curves.get("qi_reason", QI_MISSING))
        painter.setPen(QColor(self.p["subtle"]))
        note=('真气以本场观测峰值为 100%（上限未知）；空白表示采样缺口'
              if self.curves.get('qi_absolute') else '真气按收到的状态保持；空白表示观测缺口' if self.curves.get('qi_state')
              else '空白表示采样缺口；曲线不补点、不代表服务器精确时间')
        painter.drawText(8, self.height() - 10, note)

    def mouseMoveEvent(self,event):
        right=float(max(self.LEFT+120,self.width()-self.RIGHT))
        duration=max(1000.0,float(self.curves.get("duration_ms") or 0.0))
        if self.LEFT<=event.position().x()<=right and self.curves.get('duration_ms',0)>0:
            time=min(duration,max(0.0,(event.position().x()-self.LEFT)/max(1.0,right-self.LEFT)*duration))
            self.cursor=time;self.update();self.cursorChanged.emit(time)
            QToolTip.showText(event.globalPosition().toPoint(),f'当前时间 {time/1000:.3f} s',self)
        else:QToolTip.hideText()

    def leaveEvent(self,event):
        QToolTip.hideText()
        super().leaveEvent(event)


def prepare_boss_status(record,cancelled=lambda:False):
    from yylogs.observation_preferences import own_actor
    report=refresh_timeline(record.get("buff_timeline") or {}, metadata_only=True)
    report['_display_own_entity_id']=own_actor(record)
    if cancelled():raise InterruptedError()
    if report.get("encounter_windows"):
        report=slice_encounter(report,report["encounter_windows"][0])
    if cancelled():raise InterruptedError()
    report["coverage_duration_ms"]=report.get("duration_ms",0)
    curves=load_boss_curves(record,origin_ms=_timestamp(report.get("origin")),
        duration_ms=report.get("duration_ms"))
    duration=max(float(report.get("duration_ms") or 0),float(curves.get("duration_ms") or 0))
    report["duration_ms"]=curves["duration_ms"]=duration
    fixed,other=boss_display_tracks(report,refresh_names=False)
    if cancelled():raise InterruptedError()
    return {"report":report,"curves":curves,"duration":duration,"fixed":fixed,"other":other,
        "coverages":{id(track):observed_coverage(track,report) for track in fixed+other},
        "source_names":{**report.get("actor_names",{}), **(source_name_map(record["capture_folder"]) if record.get("capture_folder") else {})}}


class BossStatusPage(QWidget):
    targetRequested = Signal(object)

    def __init__(self, record, palette, *, prepared=None):
        super().__init__()
        self.record = record
        self.p = palette
        data=prepared if prepared is not None else prepare_boss_status(record)
        self.report,self.curves=data["report"],data["curves"]
        self._coverages=data["coverages"]
        # Both charts use this same capture-relative axis. If an imported
        # resource journal extends beyond the Buff report, retain that tail
        # instead of clipping it or silently compressing the Debuff timeline.
        self.axis_duration_ms=data["duration"]
        self.fixed_tracks,self.other_tracks=data["fixed"],data["other"]
        from yylogs.observation_preferences import preferences
        pins = preferences(record).get('boss_pinned_buff_ids', [])
        self.pinned_ids = {n for n in pins if type(n) is int and n > 0} if isinstance(pins, list) else set()
        self.expanded = False
        self.setObjectName("bossStatusObservation")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0,0,0,0)
        self.page_scroll = QScrollArea()
        self.page_scroll.setFrameShape(QFrame.NoFrame)
        self.page_scroll.setWidgetResizable(True)
        self.page_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        self.page_scroll.setWidget(content)
        outer.addWidget(self.page_scroll)
        box = QVBoxLayout(content)
        box.setSizeConstraint(QLayout.SetMinimumSize)
        box.setContentsMargins(0, 16, 0, 12)
        box.setSpacing(10)

        header = QHBoxLayout()
        heading = QLabel("Boss 状态观测")
        heading.setProperty("kind", "heading")
        header.addWidget(heading)
        header.addStretch()
        self.count = QLabel()
        self.count.setProperty("kind", "muted")
        header.addWidget(self.count)
        box.addLayout(header)

        options = record.get('boss_entity_options', [])
        if options:
            selection = QHBoxLayout()
            caption = QLabel('观测实体')
            self.entity_selector = QComboBox()
            self.entity_selector.setObjectName('bossEntitySelector')
            self.entity_selector.setAccessibleName('选择血量、真气、Debuff 和 RD 的观测实体')
            self.entity_selector.setMinimumHeight(36)
            self.entity_selector.setMaxVisibleItems(12)
            self.entity_selector.setStyleSheet('QComboBox { combobox-popup: 0; }')
            self.entity_selector.setMinimumContentsLength(24)
            self.entity_selector.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            self.entity_selector.addItem('自动选择（Boss 名称优先，其次最大血量）', None)
            for entity in options:
                name = entity.get('name') or f"实体 {entity.get('template_no') or entity['id'][:8]}"
                maximum = f"最大血量 {entity['max_hp']:,.0f}" if entity['max_hp'] else '未采集血量上限'
                mark = ' · Boss' if entity.get('is_boss') else ''
                self.entity_selector.addItem(f"{name}{mark} · {maximum} · {entity['id'][-6:]}", entity['id'])
            if record.get('boss_entity_mode') == 'manual':
                self.entity_selector.setCurrentIndex(self.entity_selector.findData(record.get('boss_entity_id')))
            caption.setBuddy(self.entity_selector)
            selection.addWidget(caption)
            selection.addWidget(self.entity_selector, 1)
            box.addLayout(selection)
            active = next((row for row in options if row['id'] == record.get('boss_entity_id')), {})
            name = active.get('name') or f"实体 {active.get('template_no') or active.get('id', '')[:8]}"
            current = QLabel(f'当前：{name} · 血量、真气、Debuff 和 RD 按此实体展示')
            current.setTextFormat(Qt.PlainText)
            current.setProperty('kind', 'small')
            current.setWordWrap(True)
            box.addWidget(current)
            self.entity_selector.currentIndexChanged.connect(
                lambda _: self.targetRequested.emit(self.entity_selector.currentData()))

        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setProperty("kind", "small")
        box.addWidget(self.note)
        self.curve_scroll = QScrollArea()
        self.curve_scroll.setFrameShape(QFrame.NoFrame)
        self.curve_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.curve_scroll.setWidgetResizable(True)
        self.chart = BossCurveChart(self.curves, palette)
        self.curve_scroll.setWidget(self.chart)
        self.curve_scroll.setFixedHeight(268)
        box.addWidget(self.curve_scroll)

        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        divider.setFrameShadow(QFrame.Plain)
        box.addWidget(divider)

        debuff_header = QHBoxLayout()
        debuff_title = QLabel("Boss Debuff")
        debuff_title.setProperty("kind", "heading")
        debuff_header.addWidget(debuff_title)
        debuff_header.addStretch()
        self.pin_button = QPushButton('置顶 Buff')
        self.pin_button.setMinimumHeight(36)
        self.pin_button.setToolTip('选择额外置顶的 Buff，默认优先效果继续保留')
        self.pin_button.clicked.connect(self.choose_pins)
        debuff_header.addWidget(self.pin_button)
        self.expand = QPushButton()
        self.expand.setCheckable(True)
        self.expand.setMinimumHeight(36)
        self.expand.setAccessibleName("展开或收起其余 Boss Debuff")
        self.expand.toggled.connect(self.toggle_other_tracks)
        debuff_header.addWidget(self.expand)
        box.addLayout(debuff_header)

        self.debuff_note = QLabel()
        self.debuff_note.setWordWrap(True)
        self.debuff_note.setProperty("kind", "small")
        box.addWidget(self.debuff_note)
        self.debuff_scroll = QScrollArea()
        self.debuff_scroll.setFrameShape(QFrame.NoFrame)
        self.debuff_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.debuff_scroll.setWidgetResizable(True)
        self.debuff_chart = SelfBuffChart(self.report, palette, show_coverage=True,
            source_names=data["source_names"], show_cursor_time=True)
        self.debuff_chart.LEFT = 220
        self.debuff_scroll.setWidget(self.debuff_chart)
        self.debuff_scroll.setMinimumHeight(150)
        box.addWidget(self.debuff_scroll, 1)
        self.curve_scroll.horizontalScrollBar().valueChanged.connect(
            self.debuff_scroll.horizontalScrollBar().setValue
        )
        self.debuff_scroll.horizontalScrollBar().valueChanged.connect(
            self.curve_scroll.horizontalScrollBar().setValue
        )
        self.chart.cursorChanged.connect(self.sync_cursor)
        self.debuff_chart.cursorChanged.connect(self.sync_cursor)
        self.refresh()
        self.sync_own_selection()

    def sync_own_selection(self):
        from yylogs.observation_preferences import own_actor
        own = own_actor(self.record)
        if self.report.get('_display_own_entity_id') == own:return
        self.report = {**self.report, '_display_own_entity_id': own}
        self.fixed_tracks,self.other_tracks=boss_display_tracks(self.report,refresh_names=False)
        self._coverages={id(track):observed_coverage(track,self.report)
                         for track in self.fixed_tracks+self.other_tracks}
        self.refresh()

    def sync_cursor(self,time):
        self.chart.set_cursor(time)
        self.debuff_chart.set_cursor(time)

    def _display_tracks(self):
        pinned = [t for t in self.other_tracks if t.get('buff_no') in self.pinned_ids]
        rest = [t for t in self.other_tracks if t.get('buff_no') not in self.pinned_ids]
        return self.fixed_tracks + pinned + (rest if self.expanded else [])

    def choose_pins(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('置顶 Boss Buff')
        dialog.resize(440, 480)
        layout = QVBoxLayout(dialog)
        search = QLineEdit()
        search.setPlaceholderText('搜索名称或 Buff ID')
        search.setAccessibleName('搜索可置顶的 Boss Buff')
        layout.addWidget(search)
        choices = QListWidget()
        choices.setAccessibleName('勾选需要置顶的 Buff')
        layout.addWidget(choices, 1)
        defaults = {t.get('buff_no') for t in self.fixed_tracks}
        seen = set()
        for track in self.fixed_tracks + self.other_tracks:
            buff = track.get('buff_no')
            if buff in seen:continue
            seen.add(buff)
            item = QListWidgetItem(f'{display_name(track, "未命名 Buff")} · {buff}', choices)
            item.setData(Qt.UserRole, buff)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if buff in defaults or buff in self.pinned_ids else Qt.Unchecked)
            if buff in defaults:
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
                item.setToolTip('默认优先效果，始终保留')
        def filter_items(text):
            for index in range(choices.count()):
                item = choices.item(index)
                item.setHidden(text.casefold() not in item.text().casefold())
        search.textChanged.connect(filter_items)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText('保存')
        buttons.button(QDialogButtonBox.Cancel).setText('取消')
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:return
        pins = self.pinned_ids - seen
        pins.update(choices.item(i).data(Qt.UserRole) for i in range(choices.count())
                    if choices.item(i).checkState() == Qt.Checked
                    and choices.item(i).data(Qt.UserRole) not in defaults)
        from yylogs.observation_preferences import save_preference
        try:save_preference(self.record, 'boss_pinned_buff_ids', sorted(pins))
        except (OSError, ValueError) as exc:
            self.pin_button.setToolTip(f'本次置顶已生效，保存失败：{exc}')
        self.pinned_ids = pins
        self.refresh()

    def refresh(self):
        hp_count = sum(not row.get("gap") for row in self.curves.get("hp", []))
        qi_count = sum(not row.get("gap") for row in self.curves.get("qi", []))
        self.count.setText(f"血量 {hp_count} 点 · 真气 {qi_count} 点")
        self.note.setToolTip('')
        missing=[]
        if not hp_count:missing.append('未采集到 Boss 血量')
        if not qi_count:missing.append(str(self.curves.get('qi_reason', QI_MISSING)))
        self.note.setText('；'.join(missing))
        self.note.setVisible(bool(missing))
        self.curve_scroll.setToolTip('')
        self.count.setToolTip('')
        visible = self._display_tracks()
        self.debuff_chart.report = self.report
        self.debuff_chart.set_tracks(visible,coverages=[self._coverages[id(track)] for track in visible])
        self.debuff_chart.setMinimumWidth(360)
        self.debuff_note.setToolTip(
            "优先显示：" + "、".join(PRIORITY_NAMES)
            + "；本人增伤霖、烁鼓同步显示在这里，队员 Buff 观测页面仍保留。"
            + " 覆盖率为本场已观测去重时长占比，缺口不补齐。"
            + (f" 自选置顶 {sum(t.get('buff_no') in self.pinned_ids for t in self.other_tracks)} 项。")
            + (" 当前没有可用的 Boss Debuff 采样。" if not visible else "")
        )
        self.debuff_note.setText('按提供者与刷新分段 · 点击条形查看时间与名称' if visible else '其余效果已收起，可展开查看' if self.other_tracks else '未采集到 Boss Debuff')
        self.debuff_note.setVisible(True)
        self.expand.setToolTip(self.debuff_note.toolTip())
        remaining = sum(t.get('buff_no') not in self.pinned_ids for t in self.other_tracks)
        self.expand.setVisible(bool(remaining))
        self.expand.setText(
            f"收起其余 {remaining} 项" if self.expanded
            else f"展开其余 {remaining} 项"
        )
        self.expand.setIcon(icon("chevron-up" if self.expanded else "chevron-down",
                                 self.p["text"], 16, self.expand.devicePixelRatioF()))
        self.expand.setIconSize(QSize(16, 16))

    def toggle_other_tracks(self, expanded):
        self.expanded = bool(expanded)
        self.refresh()
