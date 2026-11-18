"""Native, cached action lanes and a virtualized sequence/interval table."""
from collections import defaultdict
from PySide6.QtCore import Qt, QRectF, QModelIndex, QAbstractTableModel, Signal, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap, QFontMetrics
from PySide6.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QHBoxLayout, QComboBox, QLineEdit, QPushButton,
    QDoubleSpinBox, QScrollArea, QFrame, QTableView, QHeaderView, QAbstractItemView,
    QToolTip, QSizePolicy,
)
from yylogs.icons import decorate, refresh_icons
from yylogs.observation_preferences import own_actor, observation_members


REASONS = {
    'queue-removed': '技能同步队列移除', 'server-skill-end': '收到技能结束通知',
    'queue-replaced': '被后续动作替换，结束未确认', 'next-action': '收到后续动作，结束未确认',
    'range-end': '记录结束，动作结束未确认', 'visibility': '观测范围改变',
    'observed-aoi-leave': '角色离开观测范围', 'space-data-reset': '场景状态重置',
    'entity-snapshot': '角色状态重建',
}
BASIS = ('使用服务器同步队列的收包时间，表示观测到的技能占用区间；'
         '不等同客户端动画或前后摇。斜纹表示开始或结束未确认。'
         '待复查间隔仅由已观测结束与下一次开始计算，不能直接判定为走位或浪费时间。')


def seconds(value):
    return '—' if value is None else f'{value:.3f} s'


def stamp(value):
    minutes, secs = divmod(max(0, value), 60)
    return f'{int(minutes):02d}:{secs:06.3f}'


def span_tooltip(row, names):
    who = names.get(row['actor'], row['actor'])
    if 'skillId' not in row:
        return (f"{who}\n{stamp(row['start'])} - {stamp(row['end'])}\n"
                f"待复查间隔 {seconds(row['duration'])}\n"
                f"{row['previousName']} → {row['nextName']}")
    start = stamp(row['start']) + ('' if row['startKnown'] else '（开始未确认）')
    end = stamp(row['end']) + ('' if row['endKnown'] else '（结束未确认）')
    target = row.get('target')
    phases = '、'.join(str(p['segment']) for p in row.get('phases', []))
    return (f"{who} · 第 {row['order']} 个动作\n{row['name']} · ID {row['skillId']}\n"
            f"{start} - {end}\n完整动作时长 {seconds(row['duration'])}\n"
            f"已观测区间 {seconds(row['observedSeconds'])}\n前次间隔 {seconds(row['gapBefore'])}\n"
            f"{REASONS.get(row['endReason'], '观测边界改变')}"
            + (f"\n目标 {names.get(target, target)}" if target else '')
            + (f"\n技能分段 {phases}" if phases else ''))


class ActionTableModel(QAbstractTableModel):
    def __init__(self, names, parent=None):
        super().__init__(parent)
        self.names, self.rows, self.gaps = names, [], False

    def replace(self, rows, gaps):
        self.beginResetModel()
        self.rows, self.gaps = rows, gaps
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else 7

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return ('角色', '顺序', '动作 / 技能', '开始', '结束',
                    '间隔' if self.gaps else '动作时长', '后续动作' if self.gaps else '前次间隔')[section]

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self.rows):
            return None
        row = self.rows[index.row()]
        if role == Qt.ToolTipRole:
            return span_tooltip(row, self.names)
        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignVCenter | (Qt.AlignRight if index.column() in (1, 3, 4, 5) else Qt.AlignLeft))
        if role != Qt.DisplayRole:
            return None
        if self.gaps:
            values = (self.names.get(row['actor'], row['actor']), '—', row['previousName'],
                      stamp(row['start']), stamp(row['end']), seconds(row['duration']), row['nextName'])
        else:
            values = (self.names.get(row['actor'], row['actor']), str(row['order']), row['name'],
                      stamp(row['start']) if row['startKnown'] else stamp(row['start']) + ' *',
                      stamp(row['end']) if row['endKnown'] else '未确认',
                      seconds(row['duration']), seconds(row['gapBefore']))
        return values[index.column()]


class ActionChart(QWidget):
    selected = Signal(object)
    lane_height = 40
    left = 106
    top = 28

    def __init__(self, palette, names, parent=None):
        super().__init__(parent)
        self.p, self.names = palette, names
        self.spans, self.gaps, self.actors = [], [], []
        self.start, self.end = 0, 1
        self.cache, self.highlight, self.hit_bins = None, None, {}
        self.setMouseTracking(True)
        self.setAccessibleName('玩家动作时间轴；可在下方表格用键盘选择动作')
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_data(self, spans, gaps, actors, start, end):
        self.spans, self.gaps, self.actors = spans, gaps, actors
        self.start, self.end = start, end
        self.cache, self.highlight = None, None
        self.setFixedHeight(max(112, self.top + len(actors)*self.lane_height + 16))
        self.update()

    def resizeEvent(self, event):
        self.cache = None
        super().resizeEvent(event)

    def _x(self, at):
        return self.left + (min(self.end, max(self.start, at))-self.start) / max(.001, self.end-self.start) * max(1, self.width()-self.left-16)

    def _color(self, skill):
        # Stable skill hue, with different lightness for each application theme.
        dark = QColor(self.p['bg']).lightnessF() < .5
        return QColor.fromHsv((skill*67) % 360, 115 if dark else 135, 215 if dark else 130)

    def _build_cache(self):
        ratio = self.devicePixelRatioF()
        self.cache = QPixmap(max(1, round(self.width()*ratio)), max(1, round(self.height()*ratio)))
        self.cache.setDevicePixelRatio(ratio)
        self.cache.fill(QColor(self.p['panel']))
        painter = QPainter(self.cache)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setFont(self.font())
        painter.setPen(QColor(self.p['muted']))
        fm = QFontMetrics(self.font())
        for tick in range(6):
            at = self.start + (self.end-self.start)*tick/5
            x = self._x(at)
            painter.setPen(QColor(self.p['line']))
            painter.drawLine(int(x), self.top, int(x), self.height()-8)
            painter.setPen(QColor(self.p['muted']))
            text_x = min(max(0, self.width()-68), max(0, x-32))
            painter.drawText(QRectF(text_x, 0, 64, 24), Qt.AlignCenter, f'{at:.1f}s')
        positions = {actor: n for n, actor in enumerate(self.actors)}
        for actor, n in positions.items():
            y = self.top + n*self.lane_height
            painter.setPen(QColor(self.p['muted']))
            name = fm.elidedText(self.names.get(actor, actor), Qt.ElideRight, self.left-16)
            painter.drawText(QRectF(6, y, self.left-16, self.lane_height), Qt.AlignVCenter, name)
            painter.setPen(QColor(self.p['line']))
            painter.drawLine(self.left, y+self.lane_height, self.width()-16, y+self.lane_height)
        self.hit_bins = defaultdict(list)
        for row in self.gaps + self.spans:
            if row['actor'] not in positions:
                continue
            lane = positions[row['actor']]
            rect = QRectF(self._x(row['start']), self.top+lane*self.lane_height+8,
                          max(2, self._x(row['end'])-self._x(row['start'])), 24)
            if 'skillId' not in row:
                painter.setPen(QPen(QColor(self.p['muted']), 1, Qt.DashLine))
                painter.setBrush(QColor(self.p['raised']))
                painter.drawRect(rect)
            else:
                color = self._color(row['skillId'])
                fill = QColor(color)
                fill.setAlpha(60 if row['duration'] is None else 205)
                painter.setPen(QPen(color, 1))
                painter.setBrush(fill)
                painter.drawRoundedRect(rect, 2, 2)
                if row['duration'] is None:
                    painter.save()
                    painter.setClipRect(rect)
                    painter.setPen(QPen(color, 1, Qt.SolidLine))
                    for dx in range(int(rect.left())-24, int(rect.right())+1, 8):
                        painter.drawLine(dx, int(rect.bottom()), dx+24, int(rect.top()))
                    painter.restore()
                if rect.width() > 64:
                    painter.save()
                    painter.setClipRect(rect.adjusted(4, 0, -4, 0))
                    text_color = self.p['text'] if row['duration'] is None else (
                        self.p['side'] if QColor(self.p['bg']).lightnessF() < .5 else '#ffffff')
                    painter.setPen(QColor(text_color))
                    painter.drawText(rect.adjusted(5, 0, -4, 0), Qt.AlignVCenter,
                                     fm.elidedText(row['name'], Qt.ElideRight, int(rect.width())-10))
                    painter.restore()
            for cell in range(int(rect.left())//12, int(rect.right())//12+1):
                self.hit_bins[(lane, cell)].append((rect, row))
        if not self.actors:
            painter.setPen(QColor(self.p['muted']))
            painter.drawText(self.rect(), Qt.AlignCenter, '本区间没有匹配的玩家动作')
        painter.end()

    def paintEvent(self, event):
        if self.cache is None:
            self._build_cache()
        painter = QPainter(self)
        painter.drawPixmap(0, 0, self.cache)
        row = self.highlight
        if row and row['actor'] in self.actors:
            lane = self.actors.index(row['actor'])
            painter.setPen(QPen(QColor(self.p['text']), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(QRectF(self._x(row['start']), self.top+lane*self.lane_height+6,
                                   max(2, self._x(row['end'])-self._x(row['start'])), 28))
        painter.end()

    def _hit(self, point):
        lane = int((point.y()-self.top)//self.lane_height)
        for rect, row in reversed(self.hit_bins.get((lane, int(point.x())//12), [])):
            if rect.contains(point):
                return row
        return None

    def mouseMoveEvent(self, event):
        row = self._hit(event.position())
        if row:
            QToolTip.showText(event.globalPosition().toPoint(), span_tooltip(row, self.names), self)
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        QToolTip.hideText()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            row = self._hit(event.position())
            if row:
                self.selected.emit(row)
        super().mousePressEvent(event)


class PlayerActionPage(QWidget):
    def __init__(self, record, palette, *, prepared=None):
        super().__init__()
        from yylogs.player_actions import prepare_player_actions
        self.record, self.p = record, palette
        self.data = prepared if prepared is not None else prepare_player_actions(record)
        self.names = {m['id']: m.get('name') or m['id'] for m in record.get('members', [])}
        self.names.update({key: value for key, value in (record.get('buff_timeline') or {}).get('actor_names', {}).items()
                           if key not in self.names})
        self.duration = self.data['duration']
        self.setObjectName('playerActionObservation')
        self.setStyleSheet(f"""
            QDoubleSpinBox {{background:{palette['side']};color:{palette['text']};
                border:1px solid {palette['line']};border-radius:6px;padding:6px;min-height:22px;}}
            QDoubleSpinBox QLineEdit {{background:transparent;color:{palette['text']};border:none;padding:0;}}
            QDoubleSpinBox:focus {{border-color:{palette['muted']};}}
            QTableView {{alternate-background-color:{palette['panel']};color:{palette['text']};}}
        """)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 16, 0, 12)
        box.setSpacing(12)
        heading = QHBoxLayout()
        title = QLabel('玩家动作观测');title.setProperty('kind', 'heading')
        heading.addWidget(title)
        info = decorate(QPushButton('观测口径'), 'info', icon_only=True)
        info.setToolTip(BASIS)
        info.clicked.connect(lambda: QToolTip.showText(info.mapToGlobal(info.rect().bottomLeft()), BASIS, info))
        heading.addWidget(info);heading.addStretch()
        box.addLayout(heading)
        controls = QHBoxLayout()
        controls.addWidget(QLabel('角色'))
        self.member = QComboBox();self.member.setAccessibleName('选择动作观测角色')
        self.member.addItem('全队', None)
        own = own_actor(record)
        for member in observation_members(record):
            self.member.addItem(self.names[member['id']] + (' · 本人' if member['id'] == own else ''), member['id'])
        if own is not None:self.member.setCurrentIndex(self.member.findData(own))
        self._last_own = own
        controls.addWidget(self.member, 1)
        self.search = QLineEdit();self.search.setPlaceholderText('搜索技能名称 / ID');self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName('搜索观测动作的技能名称或编号')
        controls.addWidget(self.search, 1)
        self.kind = QComboBox();self.kind.addItems(['动作顺序', '待复查间隔'])
        self.kind.setAccessibleName('选择动作或待复查间隔列表')
        controls.addWidget(self.kind)
        box.addLayout(controls)
        ranges = QHBoxLayout();ranges.addWidget(QLabel('区间'))
        self.start, self.end = QDoubleSpinBox(), QDoubleSpinBox()
        for spin, name in ((self.start, '动作区间开始秒'), (self.end, '动作区间结束秒')):
            spin.setRange(0, max(.001, self.duration));spin.setDecimals(3);spin.setSuffix(' s')
            spin.setMinimumWidth(116)
            spin.setSingleStep(1);spin.setAccessibleName(name);spin.setKeyboardTracking(False)
            ranges.addWidget(spin)
        self.end.setValue(self.duration)
        self.reset = decorate(QPushButton('全场'), 'refresh-cw');self.reset.clicked.connect(self.reset_range)
        ranges.addWidget(self.reset);ranges.addStretch()
        self.gap_label = QLabel('最小间隔')
        self.minimum = QDoubleSpinBox();self.minimum.setRange(0, max(1, self.duration))
        self.minimum.setDecimals(2);self.minimum.setSuffix(' s');self.minimum.setSingleStep(.1)
        self.minimum.setAccessibleName('待复查间隔最小秒数');self.minimum.setKeyboardTracking(False)
        ranges.addWidget(self.gap_label);ranges.addWidget(self.minimum)
        box.addLayout(ranges)
        self.summary = QLabel();self.summary.setTextFormat(Qt.PlainText)
        self.summary.setWordWrap(True);self.summary.setProperty('kind', 'muted');box.addWidget(self.summary)
        self.chart = ActionChart(palette, self.names)
        self.chart_scroll = QScrollArea();self.chart_scroll.setWidgetResizable(True)
        self.chart_scroll.setFrameShape(QFrame.NoFrame);self.chart_scroll.setMaximumHeight(288)
        self.chart_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);self.chart_scroll.setWidget(self.chart)
        box.addWidget(self.chart_scroll)
        legend = QLabel('实色：起止完整    斜纹：起止不完整    虚线：待复查间隔')
        legend.setProperty('kind', 'muted');legend.setToolTip(BASIS);box.addWidget(legend)
        self.table = QTableView();self.model = ActionTableModel(self.names, self.table)
        self.table.setModel(self.model);self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(False);self.table.setAlternatingRowColors(True)
        self.table.setAccessibleName('玩家动作明细；双击或按查看附近缩小观测区间')
        self.table.verticalHeader().hide();self.table.verticalHeader().setDefaultSectionSize(38)
        header = self.table.horizontalHeader();header.setMinimumSectionSize(34)
        header.setSectionResizeMode(QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Fixed);self.table.setColumnWidth(1, 36)
        self.table.setMinimumHeight(140);box.addWidget(self.table, 1)
        bottom = QHBoxLayout()
        self.detail = QLabel('');self.detail.setTextFormat(Qt.PlainText);self.detail.setWordWrap(True)
        bottom.addWidget(self.detail, 1)
        self.near = decorate(QPushButton('查看附近'), 'search');self.near.setEnabled(False)
        bottom.addWidget(self.near);box.addLayout(bottom)
        self.debounce = QTimer(self);self.debounce.setSingleShot(True);self.debounce.setInterval(100)
        self.debounce.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda: self.debounce.start())
        for control in (self.start, self.end, self.minimum):control.valueChanged.connect(self.refresh)
        self.member.currentIndexChanged.connect(self.refresh);self.kind.currentIndexChanged.connect(self.refresh)
        self.chart.selected.connect(self.select_row)
        self.table.selectionModel().currentRowChanged.connect(self.table_selected)
        self.near.clicked.connect(self.view_near)
        self.table.doubleClicked.connect(lambda index: self.view_near())
        refresh_icons(self, palette)
        self.refresh()

    def sync_own_selection(self):
        own = own_actor(self.record)
        if own == self._last_own:return
        self._last_own = own
        for index in range(1, self.member.count()):
            actor = self.member.itemData(index)
            self.member.setItemText(index, self.names[actor]+(' · 本人' if actor == own else ''))
        self.member.setCurrentIndex(max(0, self.member.findData(own)))

    def refresh(self, *args):
        low, high = self.start.value(), self.end.value()
        if high <= low:
            low, high = max(0, min(low, self.duration-.001)), min(self.duration, low+1)
            for spin, value in ((self.start, low), (self.end, high)):
                spin.blockSignals(True);spin.setValue(value);spin.blockSignals(False)
        actor, query = self.member.currentData(), self.search.text().strip().casefold()
        def matches(row):
            return (actor is None or row['actor'] == actor) and row['end'] >= low and row['start'] <= high
        spans = [row for row in self.data['spans'] if matches(row) and
                 (not query or query in f"{row['name']} {row['skillId']}".casefold())]
        # Only show gaps fully contained in the range; no prorated gap duration.
        gaps = [row for row in self.data['gaps'] if matches(row) and row['start'] >= low and row['end'] <= high
                and row['duration'] >= self.minimum.value() and (not query or query in
                f"{row['previousName']} {row['previousSkill']} {row['nextName']} {row['nextSkill']}".casefold())]
        gap_mode = self.kind.currentIndex() == 1
        self.minimum.setVisible(gap_mode);self.gap_label.setVisible(gap_mode)
        self.model.replace(sorted(gaps, key=lambda r: -r['duration']) if gap_mode else spans, gap_mode)
        present = {row['actor'] for row in spans + gaps}
        actors = [self.member.itemData(i) for i in range(1, self.member.count()) if self.member.itemData(i) in present]
        self.chart.set_data(spans, gaps, actors, low, max(low+.001, high))
        self.chart_scroll.setMinimumHeight(min(250, max(112, self.chart.height()+6)))
        complete = sum(row['duration'] is not None for row in spans)
        self.summary.setText((f'{len(spans)} 个动作 · {complete} 个起止完整 · '
                              f"{len(gaps)} 段待复查间隔 / {sum(r['duration'] for r in gaps):.3f} s")
                             if self.data['available'] else self.data['message'])
        self.detail.setText('');self.near.setEnabled(False)

    def reset_range(self):
        self.start.blockSignals(True);self.end.blockSignals(True)
        self.start.setValue(0);self.end.setValue(self.duration)
        self.start.blockSignals(False);self.end.blockSignals(False);self.refresh()

    def select_row(self, row):
        for index, candidate in enumerate(self.model.rows):
            if candidate is row:
                self.table.selectRow(index);self.table.scrollTo(self.model.index(index, 0));return
        self.show_selected(row)

    def table_selected(self, index, previous):
        if index.isValid() and index.row() < len(self.model.rows):
            self.show_selected(self.model.rows[index.row()])

    def show_selected(self, row):
        self.chart.highlight = row;self.chart.update()
        name = row.get('name') or f"{row['previousName']} → {row['nextName']}"
        self.detail.setText(f"{self.names.get(row['actor'], row['actor'])} · {name} · {seconds(row['duration'])}")
        self.detail.setToolTip(span_tooltip(row, self.names));self.near.setEnabled(True)

    def view_near(self):
        row = self.chart.highlight
        if row is None:return
        self.start.blockSignals(True);self.end.blockSignals(True)
        self.start.setValue(max(0, row['start']-2));self.end.setValue(min(self.duration, row['end']+2))
        self.start.blockSignals(False);self.end.blockSignals(False);self.refresh();self.select_row(row)
