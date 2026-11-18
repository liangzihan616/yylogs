"""Record-scoped Buff pages; preserves separate self and Boss evidence channels."""
from pathlib import Path
from PySide6.QtCore import Qt, QSize, QAbstractTableModel, QModelIndex
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QLineEdit, QComboBox, QScrollArea, QFrame,
    QHeaderView, QAbstractItemView, QTableView, QPushButton, QDialog,
    QDialogButtonBox, QListWidget, QListWidgetItem, QMessageBox)
from yylogs.icons import icon
from yylogs.self_buff_views import SelfBuffChart, title, source_text, segment_note, MODE
from yylogs.game_names import refresh_timeline, tooltip as name_tooltip
from yylogs.encounter_timeline import slice_encounter
from yylogs.buff_view_filters import combat_acquired_track
from yylogs.boss_debuff_timeline import presence_tracks
from yylogs.boss_status_views import BossStatusPage
from yylogs.actor_names import source_name_map

TAB_NAMES = ("总览", "队员buff观测", "boss状态观测", "队员状态观测", "玩家动作观测")
TAB_ICONS = ("swords", "shield-check", "chart-no-axes-combined", "users", "list-checks")
TAB_RECIPIENTS = {1: 'self', 2: 'boss', 3: 'members', 4: 'actions'}


def text(value, kind=None):
    label = QLabel(value)
    label.setTextFormat(Qt.PlainText)
    if kind:
        label.setProperty("kind", kind)
    return label


def empty_page(message, palette, symbol="shield-check"):
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.addStretch()
    image = QLabel()
    image.setPixmap(icon(symbol, palette["subtle"], 36, page.devicePixelRatioF()).pixmap(QSize(36, 36)))
    layout.addWidget(image, 0, Qt.AlignHCenter)
    layout.addWidget(text(message, "muted"), 0, Qt.AlignHCenter)
    layout.addStretch()
    return page


def legacy_observation_record(record):
    """Adapt the older Boss-only report shape to the new observation shell."""
    timeline = record.get("timeline") or {}
    origin = timeline.get("origin") or record.get("timestamp")
    end = timeline.get("end") or origin
    duration_ms = max(0.0, float(timeline.get("duration") or 0) * 1000)
    tracks = []
    for buff in record.get("buffs", []):
        times = list(buff.get("sample_times") or [])
        if not times:
            times = [
                (float(item["timestamp"] and
                       __import__("datetime").datetime.fromisoformat(item["timestamp"]).timestamp()) -
                 __import__("datetime").datetime.fromisoformat(origin).timestamp()) * 1000
                for item in buff.get("observations", [])
                if item.get("timestamp")
            ]
        segments = []
        for index, value in enumerate(times):
            start_ms = max(0.0, float(value) * 1000 if float(value) < duration_ms / 1000 + 1 else float(value))
            next_value = times[index + 1] if index + 1 < len(times) else times[index]
            end_ms = max(start_ms, float(next_value) * 1000 if float(next_value) < duration_ms / 1000 + 1 else float(next_value))
            segments.append({
                "start_ms": min(start_ms, duration_ms),
                "end_ms": min(end_ms, duration_ms),
                "observed_span_ms": max(0.0, end_ms - start_ms),
                "mode": "sampled_boss_state",
                "channel": "boss_sampled",
                "source_entity_id": buff.get("source"),
                "source_status": "reported",
                "left_censored": index == 0,
                "right_censored": index == len(times) - 1,
                "inherited_at_window_start": False,
                "clipped_at_window_end": False,
                "close_reason": "legacy_sample_boundary",
                "fields": {},
            })
        tracks.append({
            "key": f"legacy:{buff.get('id')}:{buff.get('target')}:{buff.get('source')}",
            "buff_no": buff.get("id"),
            "name": buff.get("name"),
            "name_source": "legacy_report",
            "channel": "boss_sampled",
            "category": "boss_debuff",
            "instance_id": None,
            "segments": segments,
        })
    adapted = dict(record)
    adapted["buff_timeline"] = {
        "schema": "yylogs.self-buff-timeline/1",
        "timeline_kind": "legacy_boss_observation_adapter",
        "origin": origin,
        "end": end,
        "duration_ms": duration_ms,
        "own_entity_id": None,
        "scene": {"scene_id": "legacy", "dungeon_sid": record.get("dungeon_sid"),
                  "pve_verified": True, "training_verified": False},
        "tracks": tracks,
        "encounter_windows": [],
        "boss_debuff": {"available": bool(tracks), "gaps": [], "unobserved_intervals": []},
        "boss_buff_events": {"event_count": 0},
        "warnings": ["旧版 Boss 观测报告已适配到新页签；原始采样边界保留，不推定连续覆盖。"],
        "counts": {"tracks": len(tracks), "segments": sum(len(t["segments"]) for t in tracks),
                   "initial_native_instances": 0},
        "cleanup_verified": True,
        "coverage_calculated": False,
        "rdps_supported": False,
    }
    return adapted


def prepare_self_status(record,cancelled=lambda:False):
    report=refresh_timeline(record["buff_timeline"], metadata_only=True)
    views={None:report}
    for index,window in enumerate(report.get("encounter_windows",[])):
        if cancelled():raise InterruptedError()
        views[index]=slice_encounter(report,window)
    filters={}
    for index,view in views.items():
        for scope in ("combat","named","all"):
            candidates=[]
            for track in view.get("tracks",[]):
                if cancelled():raise InterruptedError()
                if track["channel"] in ("boss_events","boss_sampled","packet_boss"):continue
                if scope=="combat":track=combat_acquired_track(track,report)
                elif scope=="named" and not track.get("name"):continue
                if track is None:continue
                candidates.append((track,(title(track)+" "+" ".join(source_text(seg) for seg in track["segments"])).casefold()))
            for channel in (None,"gui_display_only","replicated","packet_self"):
                filters[(index,channel,scope)]=[(track,terms) for track,terms in candidates
                    if channel is None or track["channel"]==channel]
    return {"report":report,"views":views,"filters":filters,
        "source_names":source_name_map(record["capture_folder"]) if record.get("capture_folder") else report.get('actor_names',{})}


class SegmentTableModel(QAbstractTableModel):
    HEADERS=("效果 / ID","起点 (秒)","终点 (秒)","观测通道","来源")
    def __init__(self,parent=None):
        super().__init__(parent);self.rows=[]
    def set_rows(self,rows):
        self.beginResetModel();self.rows=rows;self.endResetModel()
    def rowCount(self,parent=QModelIndex()):return 0 if parent.isValid() else len(self.rows)
    def columnCount(self,parent=QModelIndex()):return 0 if parent.isValid() else 5
    def headerData(self,section,orientation,role=Qt.DisplayRole):
        if role==Qt.DisplayRole and orientation==Qt.Horizontal:return self.HEADERS[section]
    def data(self,index,role=Qt.DisplayRole):
        if not index.isValid() or not 0<=index.row()<len(self.rows):return None
        track,seg=self.rows[index.row()];column=index.column()
        if role==Qt.TextAlignmentRole and column in (1,2):return Qt.AlignRight|Qt.AlignVCenter
        if role==Qt.ToolTipRole:return name_tooltip(track)+"\n"+segment_note(seg)
        if role!=Qt.DisplayRole:return None
        if column==0:return title(track)
        if column in (1,2):return f"{seg['start_ms' if column==1 else 'end_ms']/1000:.3f}"
        if column==3:return {"replicated":"原生 Buff","gui_display_only":"显示状态",
            "boss_sampled":"状态采样","boss_events":"分发事件"}.get(track["channel"],track["channel"])
        return source_text(seg)


class SegmentTable(QTableView):
    def rowCount(self):return self.model().rowCount()


class ObservationPage(QWidget):
    def __init__(self, record, palette, recipient, *, prepared=None, preference_record=None):
        super().__init__()
        self.record, self.p, self.recipient_kind = record, palette, recipient
        self.preference_record = record if preference_record is None else preference_record
        from yylogs.observation_preferences import preferences
        hidden = preferences(self.preference_record).get('member_hidden_combat_buff_ids', [])
        self.hidden_buff_ids = {n for n in hidden if type(n) is int and n > 0} if isinstance(hidden, list) else set()
        data=prepared if prepared is not None else prepare_self_status(record)
        self.report=data["report"]
        self.window_views=data["views"]
        self._filter_cache=dict(data.get("filters",{})) if recipient=="self" else {}
        self.visible_tracks = []
        self.setObjectName("selfObservations" if recipient == "self" else "bossObservations")
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 16, 0, 0)
        box.setSpacing(12)
        header = QHBoxLayout()
        header.addWidget(text("队员 Buff 观测" if recipient == "self" else "Boss 状态", "heading"))
        header.addStretch()
        self.count = text("", "muted")
        header.addWidget(self.count)
        box.addLayout(header)
        if recipient == "boss":
            resources = QHBoxLayout()
            folder = Path(record.get("capture_folder", ""))
            hp = bool(record.get("capture_folder")) and (folder / "samples.jsonl").is_file()
            resources.addWidget(text("血量 · 已保存观测" if hp else "血量 · 暂无观测", "muted"))
            resources.addSpacing(20)
            resources.addWidget(text("真气 · 本场未采集", "muted"))
            resources.addStretch()
            box.addLayout(resources)
        filters = QGridLayout()
        filters.setHorizontalSpacing(8)
        filters.setVerticalSpacing(8)
        self.query = QLineEdit()
        self.query.setPlaceholderText("搜索效果名称、Buff ID 或来源")
        self.query.setAccessibleName("筛选队员 Buff" if recipient == "self" else "筛选 Boss Debuff")
        filters.addWidget(self.query, 0, 0, 1, 2 if recipient == 'self' else 3)
        self.customize_button = QPushButton('自定义展示')
        self.customize_button.setObjectName('memberBuffDisplayButton')
        self.customize_button.setToolTip('选择进战后获得或刷新的 Buff；设置适用于各队员，其他显示范围不受影响')
        from yylogs.icons import decorate
        decorate(self.customize_button, 'settings-2')
        self.customize_button.setIcon(icon('settings-2', palette['text'], dpr=self.devicePixelRatioF()))
        self.customize_button.clicked.connect(self.choose_buffs)
        if recipient == 'self':
            filters.addWidget(self.customize_button, 0, 2)
        else:
            self.customize_button.hide()
        self.window = QComboBox()
        self.window.setAccessibleName("观测时间范围")
        for i, window in enumerate(self.report.get("encounter_windows", [])):
            self.window.addItem(("战斗观测" if window.get("end_verified") else "战斗片段")+f" {i+1}", i)
        self.window.addItem("战斗快照" if self.report.get("snapshot_filter_windows") and self.report.get("capture_origin")
                            else "全部采集", None)
        filters.addWidget(self.window, 1, 0)
        self.channel = QComboBox()
        self.channel.setAccessibleName("观测通道")
        if recipient == "self":
            self.channel.addItem("全部 Buff 通道", None)
            self.channel.addItem("显示状态", "gui_display_only")
            self.channel.addItem("原生 Buff", "replicated")
            if record.get('capture_backend')=='packet':
                self.channel.clear();self.channel.addItem('抓包 Buff 状态','packet_self')
        else:
            self.channel.addItem("Debuff 状态采样", "boss_sampled")
            self.channel.addItem("Debuff 事件", "boss_events")
            if self.report.get("boss_buff_events", {}).get("event_count", 0):
                self.channel.setCurrentIndex(1)
        filters.addWidget(self.channel, 1, 1)
        self.scope = QComboBox()
        self.scope.setAccessibleName("队员 Buff 显示范围")
        self.scope.addItem("进战后获得 / 刷新", "combat")
        self.scope.addItem("已命名效果", "named")
        self.scope.addItem("全部效果", "all")
        self.scope.setCurrentIndex(0)
        if recipient == "self":
            filters.addWidget(self.scope, 1, 2)
        else:
            self.scope.hide()
        for control in (self.window, self.channel, self.scope):
            control.setMinimumWidth(0)
            control.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            control.setMinimumContentsLength(8)
        box.addLayout(filters)
        self.chart = SelfBuffChart(self.report, palette, source_names=data["source_names"])
        if recipient == 'self':
            self.chart.setAccessibleName('队员 Buff 观测时间轴，悬停可查看效果来源和观测区间')
        self.chart.LEFT = 210
        self.chart_scroll = QScrollArea()
        self.chart_scroll.setFrameShape(QFrame.NoFrame)
        self.chart_scroll.setWidgetResizable(True)
        self.chart_scroll.setWidget(self.chart)
        self.chart_scroll.setMinimumHeight(160)
        box.addWidget(self.chart_scroll, 2)
        self.empty = text("暂无匹配的观测", "muted")
        box.addWidget(self.empty, 0, Qt.AlignHCenter)
        self.table = SegmentTable()
        self.table_model=SegmentTableModel(self.table);self.table.setModel(self.table_model)
        self.table.setAccessibleName("队员 Buff 观测明细" if recipient == "self" else "Boss Debuff 观测明细")
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        for col in (1, 2):
            self.table.setColumnWidth(col, 85)
        self.table.setColumnWidth(3, 110)
        box.addWidget(self.table, 2)
        self.status = text("", "small")
        self.status.setWordWrap(True)
        box.addWidget(self.status)
        self.query.textChanged.connect(self.refresh)
        for control in (self.window, self.channel, self.scope):
            control.currentIndexChanged.connect(self.refresh)
        self.refresh()

    def choose_buffs(self):
        if self.recipient_kind != 'self' or self.scope.currentData() != 'combat':
            return
        cached = self._filter_cache.get((self.window.currentData(), self.channel.currentData(), 'combat'), [])
        tracks = {}
        for track, _ in cached:
            number = track.get('buff_no')
            if type(number) is int and number > 0:
                tracks.setdefault(number, track)
        dialog = QDialog(self)
        dialog.setWindowTitle('队员 Buff 自定义展示')
        dialog.resize(480, 520)
        layout = QVBoxLayout(dialog)
        search = QLineEdit()
        search.setPlaceholderText('搜索名称或 Buff ID')
        search.setAccessibleName('搜索进战后获得或刷新的 Buff')
        layout.addWidget(search)
        choices = QListWidget()
        choices.setAccessibleName('勾选需要展示的队员 Buff')
        layout.addWidget(choices, 1)
        for number, track in sorted(tracks.items(), key=lambda row: (title(row[1]).casefold(), row[0])):
            item = QListWidgetItem(title(track), choices)
            item.setData(Qt.UserRole, number)
            item.setToolTip(name_tooltip(track))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked if number in self.hidden_buff_ids else Qt.Checked)
        def filter_items(value):
            for i in range(choices.count()):
                item = choices.item(i)
                item.setHidden(value.casefold() not in item.text().casefold())
        search.textChanged.connect(filter_items)
        actions = QHBoxLayout()
        reset_requested = [False]
        def select_all(checked, *, reset=False):
            reset_requested[0] = reset_requested[0] or reset
            for i in range(choices.count()):
                choices.item(i).setCheckState(Qt.Checked if checked else Qt.Unchecked)
        for label, checked in (('全选', True), ('全不选', False), ('恢复默认', True)):
            button = QPushButton(label)
            button.clicked.connect(lambda _, checked=checked, reset=label == '恢复默认': select_all(checked, reset=reset))
            actions.addWidget(button)
        layout.addLayout(actions)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText('保存')
        buttons.button(QDialogButtonBox.Cancel).setText('取消')
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        accepted = dialog.exec() == QDialog.Accepted
        dialog.deleteLater()
        if not accepted:
            return
        hidden = set() if reset_requested[0] else self.hidden_buff_ids - tracks.keys()
        hidden.update(choices.item(i).data(Qt.UserRole) for i in range(choices.count())
                      if choices.item(i).checkState() != Qt.Checked)
        from yylogs.observation_preferences import save_preference
        try:
            save_preference(self.preference_record, 'member_hidden_combat_buff_ids', sorted(hidden))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, '展示设置未保存', str(exc))
            return
        self.hidden_buff_ids = hidden
        self.refresh()

    def refresh(self):
        index = self.window.currentData()
        if index not in self.window_views:
            self.window_views[index] = (
                slice_encounter(self.report, self.report["encounter_windows"][index])
                if index is not None else self.report)
        self.view_report = self.window_views[index]
        query, channel = self.query.text().strip().casefold(), self.channel.currentData()
        cache_key=(index,channel,self.scope.currentData())
        cached=self._filter_cache.get(cache_key)
        tracks = []
        for track in ([] if cached is not None else self.view_report["tracks"]):
            boss = track["channel"] in ("boss_events", "boss_sampled", "packet_boss")
            if boss != (self.recipient_kind == "boss") or (channel and track["channel"] != channel):
                continue
            if not boss and self.scope.currentData() == "combat":
                track = combat_acquired_track(track, self.report)
                if track is None:
                    continue
            if not boss and self.scope.currentData() == "named" and not track.get("name"):
                continue
            tracks.append(track)
        if cached is None:
            cached=[(track,(title(track)+" "+" ".join(source_text(s) for s in track["segments"])).casefold()) for track in tracks]
            self._filter_cache[cache_key]=cached
        custom = self.recipient_kind == 'self' and self.scope.currentData() == 'combat'
        self.customize_button.setEnabled(custom)
        tracks=[track for track,terms in cached if (not query or query in terms)
                and (not custom or track.get('buff_no') not in self.hidden_buff_ids)]
        self.visible_tracks = tracks
        chart_tracks = presence_tracks(tracks) if self.recipient_kind == "boss" else tracks
        self.chart.report = self.view_report
        self.chart.set_tracks(chart_tracks)
        self.chart.setMinimumWidth(0)
        self.empty.setVisible(not tracks)
        self.chart_scroll.setVisible(bool(tracks))
        rows = [(track, seg) for track in tracks for seg in track["segments"]]
        self.table_model.set_rows(rows)
        self.count.setText(f"{len(chart_tracks)} 种效果 · {len(rows)} 个区间")
        info = self.view_report.get("boss_buff_events" if self.channel.currentData() == "boss_events" else "boss_debuff", {})
        gaps = len(info.get("gaps", [])) if self.recipient_kind == "boss" else 0
        scope_note = (" · 已隐藏进战前已有且未刷新的效果" if self.scope.currentData()=="combat"
                      and (self.report.get("encounter_windows") or self.report.get("snapshot_filter_windows")) else
                      " · 缺少战斗边界，无法判定进战 Buff；可切换全部效果" if self.scope.currentData()=="combat" else "")
        warning=f'{gaps} 处数据缺口' if gaps else ''
        if '缺少战斗边界' in scope_note:warning+='；缺少战斗边界，无法筛选进战 Buff'
        self.status.setText(warning.lstrip('；'))
        self.status.setVisible(bool(warning))
        self.status.setToolTip("\n".join(self.report.get("warnings", [])))
        self.scope.setToolTip('仅显示战斗中获得或刷新的效果。'+scope_note)


def observation_page(record, palette, recipient, *, prepared=None):
    if recipient == 'actions':
        from yylogs.player_action_views import PlayerActionPage
        return PlayerActionPage(record, palette, prepared=prepared)
    if recipient=='self' and record.get('capture_backend')=='packet':
        from yylogs.packet_observations import packet_self_page
        return packet_self_page(record,palette,prepared=prepared)
    if recipient == "members":
        from yylogs.member_status_views import MemberStatusPage
        return MemberStatusPage(record,palette,prepared=prepared)
    if recipient == "boss":
        if record.get("buff_timeline"):
            return BossStatusPage(record, palette,prepared=prepared)
        if record.get("timeline") or record.get("buffs"):
            return BossStatusPage(legacy_observation_record(record), palette,prepared=prepared)
        return empty_page("本场没有 Boss 状态观测", palette, "chart-no-axes-combined")
    if record.get("buff_timeline"):
        return ObservationPage(record, palette, recipient,prepared=prepared)
    if record.get("timeline") or record.get("buffs"):
        return ObservationPage(legacy_observation_record(record), palette, recipient,prepared=prepared)
    return empty_page("本场没有队员 Buff 观测" if recipient == "self" else "本场没有 Boss 状态观测",
                      palette, "shield-check" if recipient == "self" else "chart-no-axes-combined")
