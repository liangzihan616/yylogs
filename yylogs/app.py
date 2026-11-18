"""yylogs desktop: offline battle review."""
import argparse,csv,json,os,sys
from pathlib import Path
from datetime import datetime
from PySide6.QtCore import Qt,QTimer,QUrl,QThread,Signal
from PySide6.QtGui import QColor,QFont,QIcon,QPainter,QPen,QPixmap,QPalette,QDesktopServices,QShortcut,QKeySequence
from PySide6.QtWidgets import (QApplication,QMainWindow,QWidget,QFrame,QLabel,QPushButton,QVBoxLayout,QHBoxLayout,QGridLayout,QScrollArea,QTableWidget,QTableWidgetItem,QHeaderView,QAbstractItemView,QTabWidget,QFileDialog,QMessageBox,QLineEdit,QSizePolicy,QMenu,QButtonGroup,QDialog,QInputDialog,QCheckBox,QSystemTrayIcon)
from yylogs import __version__
from yylogs.data import Repository
from yylogs.detail_views import BuffPage,SkillDialog,qi_page
from yylogs.battle_records import import_file, import_capture, export_record
from yylogs.dungeon_identity import safe_encounter_filename
from yylogs.observation_views import observation_page, empty_page, TAB_NAMES, TAB_ICONS, TAB_RECIPIENTS
from yylogs.icons import icon, refresh_icons
from yylogs.archive_actions import selected_rows, delete_selected
DARK={"bg":"#151b1a","side":"#101514","panel":"#1d2825","raised":"#273631","hover":"#30423d","line":"#3b4c47","text":"#e7efec","muted":"#a9bab4","subtle":"#899b94","accent":"#6dd6b4","good":"#78d8a9","track":"#394a45","white":"#f4faf7"}
LIGHT={"bg":"#f6faf8","side":"#edf4f1","panel":"#ffffff","raised":"#e7f0eb","hover":"#dcebe4","line":"#cbdad3","text":"#1b2d27","muted":"#52675e","subtle":"#687c73","accent":"#176f5b","good":"#23764f","track":"#d6e3dc","white":"#17241f"}

def number(value,decimals=0):return "—" if value is None else f"{value:,.{decimals}f}"
def clock(value):return datetime.fromisoformat(value).strftime("%H:%M:%S")
def label(text,kind=None):
    w=QLabel(text);w.setTextFormat(Qt.PlainText)
    if kind:w.setProperty("kind",kind)
    return w

def app_icon():
    path=Path(__file__).parent/"resources/icons/yylogs.svg"
    return QIcon(str(path))

class NumericItem(QTableWidgetItem):
    def __init__(self,value,decimals=0):
        super().__init__(number(value,decimals));self.setData(Qt.UserRole,float(value) if value is not None else None);self.setTextAlignment(Qt.AlignRight|Qt.AlignVCenter)
    def __lt__(self,other):
        a=self.data(Qt.UserRole);b=other.data(Qt.UserRole)
        return a<b if a is not None and b is not None else super().__lt__(other)


class RecordButton(QPushButton):
    def __init__(self, title, subtitle):
        super().__init__()
        self.title, self.subtitle = title, subtitle
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setAccessibleName(title+" "+subtitle)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        width = max(20, self.width()-24)
        self.setText(self.fontMetrics().elidedText(self.title, Qt.ElideRight, width)+"\n"+
                     self.fontMetrics().elidedText(self.subtitle, Qt.ElideRight, width))


class RepositoryLoadWorker(QThread):
    loaded=Signal(object,object)
    def __init__(self,repository,parent=None):
        super().__init__(parent);self.repository=repository
    def run(self):
        try:
            reports,warnings=self.repository.load(lazy_battles=True)
            self.loaded.emit(reports,warnings)
        except Exception as exc:
            self.loaded.emit(None,exc)


class SavedRecordDetailWorker(QThread):
    loaded=Signal(str,object,object)
    def __init__(self,repository,record_id,parent=None):
        super().__init__(parent);self.repository=repository;self.record_id=record_id
    def run(self):
        try:self.loaded.emit(self.record_id,self.repository.load_record_detail(self.record_id),None)
        except Exception as exc:self.loaded.emit(self.record_id,None,exc)


class Window(QMainWindow):
    def __init__(self,workspace,*,close_to_tray=False,defer_initial_load=False):
        super().__init__();self.repo=Repository(workspace);self.reports=[];self.current=None;self.dark=False;self.p=LIGHT;self.report_buttons=[];self.skill_dialogs=[];self.record_manage_mode=False;self.record_checked=set()
        self.lazy_records=bool(defer_initial_load)
        self.close_to_tray=close_to_tray;self._quit_requested=False;self.tray=None
        self._analysis_generation=0;self._analysis_worker=None;self._analysis_pending={}
        self._analysis_cache={};self._analysis_record=None;self._analysis_closing=False
        self._startup_worker=None
        self._record_detail_worker=None;self._record_detail_workers=set();self._record_detail_generation=0
        self.analysis_timings=[]
        self.setWindowTitle("yylogs · 离线复盘");self.setWindowIcon(app_icon());self.resize(1360,880);self.setMinimumSize(1040,720)
        from yylogs.ui_shell import build_shell
        build_shell(self)
        self.record_bulk_host.setVisible(False)
        self.archive_delete_mode_enabled=False
        self.archive_checked=set()
        for index, name in enumerate(TAB_ICONS):
            self.tabs.setTabIcon(index, icon(name))
        self.setup_tray()
        QShortcut(QKeySequence("Ctrl+R"),self,activated=self.refresh_active)
        QShortcut(QKeySequence("Ctrl+E"),self,activated=lambda:self.export('json') if self.active_section=='records' else None)
        self.tabs.currentChanged.connect(self.update_tab_layout)
        self.apply_theme();refresh_icons(self,self.p)
        if defer_initial_load:
            self.footer.setText('正在读取战斗记录…')
            self._startup_worker=RepositoryLoadWorker(self.repo,self)
            self._startup_worker.loaded.connect(self._initial_load_finished)
            self._startup_worker.start()
            self.reload(from_disk=False)
            self.footer.setText('正在读取战斗记录…')
        else:
            self.reload()
        self.navigate('records')
    def _initial_load_finished(self,reports,error):
        self._startup_worker=None
        if self._analysis_closing:return
        if reports is None:
            QMessageBox.warning(self,'读取失败',f'未能读取已验证报告，原始文件没有改动。\n{error}')
            self.footer.setText('读取战斗记录失败，可点击刷新重试。')
            return
        # Preserve rows published by packet capture while this older startup
        # scan was still running.
        merged={row['id']:row for row in reports}
        for row in self.reports:
            current=merged.get(row['id'])
            if current is None or current.get('_lazy_record') and not row.get('_lazy_record'):
                merged[row['id']]=row
        from yylogs.data import apply_record_preferences
        self.reports=list(merged.values())
        apply_record_preferences(self.reports,self.repo._preferences())
        self.report_warnings=list(error or [])+list(getattr(self,'report_warnings',[]))
        self.reload(from_disk=False)
    def navigate(self,key):
        from yylogs.ui_shell import navigate
        navigate(self,key)
    def choose_storage_folder(self):
        target=QFileDialog.getExistingDirectory(self,'选择数据保存文件夹',str(self.repo.workspace or Path.home()))
        if target:self.change_storage_folder(Path(target))
    def reset_storage_folder(self):
        self.change_storage_folder(None)
    def change_storage_folder(self,target):
        from yylogs.runtime_paths import save_workspace
        try:
            destination=save_workspace(target)
        except (OSError,ValueError,TypeError) as exc:
            QMessageBox.warning(self,'保存目录失败',str(exc));return
        self.storage_pending_label.setText('重启后保存到：'+str(destination))
        self.storage_pending_label.show()
        QMessageBox.information(self,'保存目录已更新',
            '重启 yylogs 后生效。采集档案和战斗记录将保存到所选文件夹。\n'
            '旧数据保留在原位置，不自动搬移；切回原目录可查看，也可通过导出、导入转移战斗记录。')
    def setup_tray(self):
        if not self.close_to_tray or not QSystemTrayIcon.isSystemTrayAvailable():return
        self.tray=QSystemTrayIcon(self.windowIcon(),self)
        self.tray.setToolTip("yylogs · PVE 战斗分析")
        menu=QMenu()
        self.tray_open_action=menu.addAction("打开主界面");self.tray_open_action.triggered.connect(self.open_main_window)
        menu.addSeparator()
        self.tray_quit_action=menu.addAction("退出 yylogs");self.tray_quit_action.triggered.connect(self.request_quit)
        self.tray_menu=menu;self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.tray_activated)
        self.tray.show()
    def tray_activated(self,reason):
        if reason in (QSystemTrayIcon.Trigger,QSystemTrayIcon.DoubleClick):self.open_main_window()
    def hide_to_tray(self):
        if not self.tray or not self.tray.isVisible():return False
        self.hide();return True
    def request_quit(self):
        self._quit_requested=True
        self.close()
    def finish_quit(self):
        app=QApplication.instance()
        if app is not None:app.quit()
    def open_main_window(self):
        if self.isMinimized():self.showNormal()
        else:self.show()
        self.raise_();self.activateWindow()
    def refresh_active(self):
        if self._startup_worker is not None and self._startup_worker.isRunning():
            return
        if self.active_section=='archive':self.refresh_archives()
        else:self.reload()
    def refresh_archives(self):
        from yylogs.ui_shell import refresh_archives
        refresh_archives(self)
    def filter_archives(self,query):
        query=query.strip().casefold();visible=0
        for index,row in enumerate(getattr(self,'archive_rows',[])):
            match=not query or query in ' '.join(row['values']).casefold()
            self.archive_table.setRowHidden(index,not match);visible+=bool(match)
        self.archive_count.setText(f'{visible} 条记录')
        self.update_archive_selection()
    def set_archive_delete_mode(self,enabled):
        from yylogs.archive_actions import set_mode
        set_mode(self,enabled)
        self.archive_select_mode.setText('退出多选' if enabled else '多选删除')
        if enabled:
            self.archive_select_mode.clicked.disconnect()
            self.archive_select_mode.clicked.connect(lambda:self.set_archive_delete_mode(False))
        else:
            self.archive_select_mode.clicked.disconnect()
            self.archive_select_mode.clicked.connect(lambda:self.set_archive_delete_mode(True))
    def archive_item_changed(self,item):
        from yylogs.archive_actions import check_changed
        check_changed(self,item)
    def select_all_archives(self,clear=False):
        from yylogs.archive_actions import select_all
        select_all(self,clear)
    def selected_archive(self):
        row=self.archive_table.currentRow()
        entries=getattr(self,'archive_rows',[])
        return entries[row] if 0<=row<len(entries) and not self.archive_table.isRowHidden(row) else None
    def update_archive_selection(self):
        row=self.selected_archive()
        rows=selected_rows(self)
        self.archive_folder_button.setEnabled(row is not None and not self.archive_delete_mode_enabled)
        self.archive_open_button.setEnabled(bool(row and row['ready']) and not self.archive_delete_mode_enabled)
        self.archive_import_button.setEnabled(bool(row) and not self.archive_delete_mode_enabled)
        self.archive_delete_button.setEnabled(bool(rows) and all(__import__('yylogs.archive_actions',fromlist=['can_delete']).can_delete(self,r) for r in rows))
        self.archive_selection_count.setText(f'已选 {len(rows)} 次' if self.archive_delete_mode_enabled else '')
    def delete_selected_archive(self):
        delete_selected(self)
    def open_selected_archive(self):
        row=self.selected_archive()
        if row and row['ready']:self.show_manual_capture(row['folder'])
    def import_selected_archive(self):
        row=self.selected_archive()
        if not row:
            return
        try:
            report=import_capture(row["folder"],self.repo.battle_store)
            self.reload()
            self.select(next(r for r in self.reports if r["id"]==report["id"]))
            self.navigate("records")
            self.footer.setText("已导入战斗记录：原始采集保留在采集档案。")
        except (OSError,ValueError,KeyError,TypeError) as exc:
            QMessageBox.warning(self,"导入失败","本次采集没有改动。\n"+str(exc))
    def open_archive_folder(self):
        row=self.selected_archive()
        if row:QDesktopServices.openUrl(QUrl.fromLocalFile(str(row['folder'])))
    def show_manual_capture(self,folder):
        try:
            folder=Path(folder).resolve();base=(Path(self.repo.workspace)/"logs_research/manual_captures").resolve()
            if folder.parent!=base:raise ValueError("采集目录不属于当前工作区")
            if (folder/'packet_records.json').is_file():
                from yylogs.local_files import read_json
                from yylogs.battle_records import load_record
                ids=(read_json(folder/'packet_records.json') or {}).get('record_ids') or []
                if not ids:raise ValueError('尚无已保存的战斗记录')
                last=ids[-1]
                if not isinstance(last,str) or len(last)!=31 or not last.startswith('battle_') or any(c not in '0123456789abcdef' for c in last[7:]):
                    raise ValueError('战斗记录标识无效')
                report=load_record(self.repo.battle_store/last)
                self.refresh_saved_packet_records(ids)
                self.select(next(r for r in self.reports if r['id']==report['id']))
                self.navigate('records')
                return
            if (folder/'packet_capture_result.json').is_file():
                report=import_capture(folder,self.repo.battle_store)
                self.reload();self.select(next(r for r in self.reports if r['id']==report['id']))
                self.navigate('records')
                return
            from yylogs.self_buff_timeline import build_timeline
            from yylogs.self_buff_views import SelfBuffTimelineDialog
            folder=Path(folder).resolve();base=(Path(self.repo.workspace)/"logs_research/manual_captures").resolve()
            if folder.parent!=base:raise ValueError("采集目录不属于当前工作区")
            report=build_timeline(folder);dialog=SelfBuffTimelineDialog(report,self.p,self,capture_folder=folder,
                                                                      record_store=self.repo.battle_store)
            dialog.linked_record.connect(self.settlement_record_added)
            # A saved manual session opens the verified battle view, never
            # silently promotes preparation/tail diagnostics into combat time.
            dialog.setAttribute(Qt.WA_DeleteOnClose);dialog.show();self.self_buff_dialog=dialog
        except (OSError,ValueError,KeyError,TypeError) as exc:QMessageBox.warning(self,"本次记录暂不可用",str(exc))
    def closeEvent(self,event):
        if self.close_to_tray and self.tray and not self._quit_requested and self.hide_to_tray():
            event.ignore();return
        self._analysis_closing=True
        startup_worker=self._startup_worker
        if startup_worker and startup_worker.isRunning():
            if not startup_worker.wait(2000):
                event.ignore();QTimer.singleShot(100,self.close);return
            self._startup_worker=None
        for detail in list(self._record_detail_workers):
            if detail.isRunning() and not detail.wait(2000):
                event.ignore();QTimer.singleShot(100,self.close);return
        worker=self._analysis_worker
        if worker and worker.isRunning():
            worker.requestInterruption()
            if not worker.wait(2000):
                event.ignore();QTimer.singleShot(100,self.close);return
        # Closing the parent destroys child dialogs without delivering their
        # closeEvent. Stop local readers before their QThreads can be destroyed.
        from yylogs.damage_candidate_views import DamageCandidateDialog
        readers=self.findChildren(DamageCandidateDialog)
        from yylogs.member_status_views import MemberStatusPage
        for page in self.findChildren(MemberStatusPage):
            if not page.stop_worker():
                event.ignore();QTimer.singleShot(100,self.close);return
        detail_workers=[worker for dialog in readers for worker in
                        (dialog.worker,dialog.settlement_worker,dialog.link_worker) if worker is not None]
        for worker in detail_workers:
            if worker.isRunning():worker.requestInterruption()
        for worker in detail_workers:
            if worker.isRunning() and not worker.wait(2000):
                event.ignore();QTimer.singleShot(100,self.close);return
        if self.tray:self.tray.hide()
        event.accept()
        if self._quit_requested:QTimer.singleShot(0,self.finish_quit)
    def show_self_buff_timeline(self):
        if self.repo.workspace:
            try:
                base=(Path(self.repo.workspace)/"logs_research/manual_captures").resolve()
                pointer=base/"latest_saved.json"
                if pointer.is_file():
                    item=json.loads(pointer.read_text(encoding="utf-8-sig"));folder=Path(item["folder"]).resolve()
                    if folder.parent!=base or not (folder/"self_buff_timeline.json").is_file():raise ValueError("最近采集指针无效")
                    self.show_manual_capture(folder);return
            except (OSError,ValueError,KeyError,TypeError) as exc:
                QMessageBox.warning(self,"最近手动采集暂不可用",str(exc));return
        self.footer.setText("没有可用的本地采集时间轴。")
    def update_tab_layout(self,*args):
        records=getattr(self,'active_section','records')=='records'
        loading_record=bool(records and self.current and self.current.get('_lazy_record'))
        if records and self.current and not loading_record:self.ensure_analysis_tab(self.tabs.currentIndex())
        if records and self.tabs.currentIndex() in getattr(self,'loaded_analysis_tabs',set()):
            page=self.tabs.currentWidget()
            if getattr(self,'_last_analysis_visible',None) is not page:
                from yylogs.analysis_loading import reveal_page
                reveal_page(page);self._last_analysis_visible=page
        else:self._last_analysis_visible=None
        if self.current and not loading_record:self.caption.setText(getattr(self,'history_caption',''))
        self.export_button.setEnabled(records and self.current is not None)
        self.export_button.setToolTip('导出当前选中的记录；不导出其他会话或历史诊断')
    def apply_theme(self):
        p=self.p
        self.setStyleSheet(f"""
        QWidget {{color:{p['text']};font-family:'Segoe UI','Microsoft YaHei UI';font-size:13px;}}
        QMainWindow, QDialog, QWidget#qt_tabwidget_stackedwidget {{background:{p['bg']};}}
        #sidebar {{background:{p['side']};border-right:1px solid {p['line']};}}
        QLabel {{background:transparent;}}
        QLabel[kind='brand'] {{font-size:26px;font-weight:650;letter-spacing:-1px;}}
        QLabel[kind='title'] {{font-size:26px;font-weight:600;}}
        QLabel[kind='heading'] {{font-size:17px;font-weight:600;}}
        QLabel[kind='metric'] {{font-size:24px;font-weight:600;}}
        QLabel[kind='muted'] {{color:{p['muted']};}}
        QLabel[kind='small'] {{color:{p['subtle']};font-size:12px;}}
        QLabel[kind='pill'] {{background:{p['raised']};border-radius:6px;padding:5px 8px;font-size:11px;font-weight:600;}}
        QLabel[kind='verified'] {{color:{p['good']};}}
        QPushButton {{background:transparent;border:1px solid {p['line']};border-radius:8px;padding:10px 13px;min-height:19px;}}
        QPushButton:hover {{background:{p['hover']};}}
        QPushButton:pressed {{background:{p['raised']};}}
        QPushButton:focus,QLineEdit:focus,QComboBox:focus,QSlider:focus {{border:1px solid {p['muted']};}}
        QPushButton:disabled {{color:{p['subtle']};}}
        QPushButton#iconButton {{padding:6px;min-height:0;}}
        QPushButton#primary {{background:{p['text']};color:{p['side']};border-color:{p['text']};font-weight:600;}}
        QPushButton#primary:hover {{background:{p['accent']};}}
        QPushButton[nav='true'] {{text-align:left;background:{p['raised']};border-color:transparent;}}
        QPushButton[sectionNav='true'] {{text-align:left;border:1px solid transparent;padding:12px 14px;background:transparent;font-weight:500;}}
        QPushButton[sectionNav='true']:checked {{background:{p['raised']};border-color:{p['line']};font-weight:600;}}
        QPushButton[sectionNav='true']:hover {{background:{p['hover']};}}
        QPushButton:disabled {{border-color:{p['line']};color:{p['subtle']};}}
        QPushButton#primary:disabled {{background:{p['raised']};border-color:{p['line']};color:{p['subtle']};}}
        #recordList {{background:transparent;}}
        QPushButton[history='true'] {{text-align:left;border:1px solid transparent;padding:12px 10px;min-height:48px;line-height:1.7;}}
        QPushButton[history='true']:checked {{background:{p['raised']};border-color:{p['line']};}}
        QLineEdit {{background:{p['side']};border:1px solid {p['line']};border-radius:8px;padding:10px;}}
        QComboBox {{background:{p['side']};border:1px solid {p['line']};border-radius:7px;padding:8px;min-height:22px;}}
        QComboBox QAbstractItemView {{background:{p['panel']};selection-background-color:{p['raised']};}}
        QSlider::groove:horizontal {{height:4px;background:{p['track']};border-radius:2px;}}
        QSlider::handle:horizontal {{width:14px;margin:-5px 0;background:{p['accent']};border-radius:7px;}}
        QToolTip {{background:{p['panel']};color:{p['text']};border:1px solid {p['line']};padding:6px;}}
        QFrame[card='true'] {{background:{p['panel']};border:1px solid {p['line']};border-radius:10px;}}
        QTabWidget::pane {{border:none;}}
        QTabBar {{border-bottom:1px solid {p['line']};}}
        QTabBar::tab {{color:{p['muted']};background:transparent;padding:13px 8px;border-bottom:2px solid transparent;}}
        QTabBar::tab:selected {{color:{p['text']};border-bottom:2px solid {p['text']};}}
        QTabBar::tab:hover {{color:{p['text']};}}
        QScrollArea,QScrollArea>QWidget>QWidget {{background:{p['bg']};border:none;}}
        QTableWidget,QTableView {{background:{p['bg']};gridline-color:{p['line']};border:none;selection-background-color:{p['raised']};selection-color:{p['text']};outline:0;}}
        QHeaderView::section {{background:{p['bg']};color:{p['muted']};border:none;border-bottom:1px solid {p['line']};padding:12px 4px;font-weight:400;}}
        QTableWidget::item {{padding:10px 4px;border-bottom:1px solid {p['line']};}}
        QScrollBar:vertical {{background:{p['bg']};width:10px;}}QScrollBar::handle:vertical {{background:{p['line']};border-radius:4px;min-height:28px;}}QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {{height:0;}}
        QScrollBar:horizontal {{background:{p["bg"]};height:10px;}}QScrollBar::handle:horizontal {{background:{p["line"]};border-radius:4px;min-width:28px;}}QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal {{width:0;}}
        QMenu {{background:{p['panel']};border:1px solid {p['line']};padding:6px;}}QMenu::item {{padding:9px 22px;}}QMenu::item:selected {{background:{p['hover']};}}
        """)
        palette=self.palette();palette.setColor(QPalette.ColorRole.Window,QColor(p['bg']));palette.setColor(QPalette.ColorRole.Base,QColor(p['bg']));palette.setColor(QPalette.ColorRole.Text,QColor(p['text']));palette.setColor(QPalette.ColorRole.WindowText,QColor(p['text']));palette.setColor(QPalette.ColorRole.Button,QColor(p['panel']));palette.setColor(QPalette.ColorRole.ButtonText,QColor(p['text']));self.setPalette(palette)
        if hasattr(self,"theme"):
            self.theme.setText("切换深色" if not self.dark else "切换浅色")
    def arrange_metrics(self):
        if not hasattr(self,'metric_cards'):return
        columns=2 if self.width()<1240 else 4
        for i,card in enumerate(self.metric_cards):self.metrics.addWidget(card,i//columns,i%columns)
        for i in range(4):self.metrics.setColumnStretch(i,1 if i<columns else 0)
        self.sidebar.setFixedWidth(180 if self.width()<1180 else 204)
        if hasattr(self,"record_list_panel"):self.record_list_panel.setFixedWidth(182 if self.width()<1180 else 214)
    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'metrics'):self.arrange_metrics()
    def toggle_theme(self):
        self.dark=not self.dark;self.p=DARK if self.dark else LIGHT;self.theme.setText("切换浅色" if self.dark else "切换深色");self.apply_theme();refresh_icons(self,self.p)
        self.sync_favorite_button()
        if self.current:
            self.show_record_loading() if self.current.get("_lazy_record") else self.render()
    def clear_layout(self,layout):
        while layout.count():
            item=layout.takeAt(0)
            if item.widget():item.widget().hide();item.widget().deleteLater()
    def reload(self,*,from_disk=True):
        old=self.current['id'] if self.current else None
        try:
            if from_disk:self.reports,self.report_warnings=self.repo.load(lazy_battles=self.lazy_records)
            warnings=getattr(self,'report_warnings',[])
        except (OSError,ValueError,KeyError,TypeError) as e:
            QMessageBox.warning(self,"读取失败",f"未能读取已验证报告，原始文件没有改动。\n{e}");return
        self.clear_layout(self.history);self.report_buttons=[];self.record_rows={}
        if hasattr(self,'group'):self.group.deleteLater()
        self.group=QButtonGroup(self);self.group.setExclusive(True)
        self.record_checked.intersection_update(r["id"] for r in self.reports)
        for r in self.reports:
            dt=datetime.fromisoformat(r['timestamp'])
            title=(r.get("display_name") if r.get("display_name_is_custom") else None) or ("木桩事件" if r.get("record_kind")=="saved_damage_journal" else ("战后技能" if r.get("record_kind")=="saved_postcombat_skill_snapshot" else (r.get("encounter_label") or r.get("display_name") or "副本记录")))
            period=(str(r["event_count"])+"条") if r.get("record_kind")=="saved_damage_journal" else ("点击加载" if r.get("_lazy_record") else number(r["duration"],1)+" 秒")
            encounter_label=r.get("encounter_label") or "副本名称未解析"
            row=QWidget();row_layout=QHBoxLayout(row);row_layout.setContentsMargins(0,0,0,0);row_layout.setSpacing(4)
            check=QCheckBox();check.setAccessibleName("选择战斗记录 "+title)
            check.blockSignals(True);check.setChecked(r["id"] in self.record_checked);check.blockSignals(False)
            check.stateChanged.connect(lambda state,record=r:self.record_checked_changed(record,state))
            favorite=bool(r.get("favorite"))
            display_title=("★ " if favorite else "")+title
            member_count=r.get("member_count",len(r["members"]))
            member_label=f" · {member_count} 人" if member_count is not None else ""
            button=RecordButton(display_title,f"{encounter_label} · {period} · {dt:%m月%d日  %H:%M}{member_label}")
            button.setCheckable(True);button.setProperty("history",True);button.setMinimumHeight(72);button.setMaximumHeight(84)
            button.setText(display_title+"\n"+button.subtitle)
            button.setToolTip(("★ 已收藏\n" if favorite else "")+title+f"\n账号 {r['account']} / {encounter_label} / SID {r['dungeon_sid']}"+(f"；Boss 名称来自截图，时间为保存快照时间" if r.get("boss_display_name") else ""))
            button.clicked.connect(lambda checked=False,record_id=r["id"]:self.select_record_id(record_id))
            row_layout.addWidget(check);row_layout.addWidget(button,1)
            self.group.addButton(button);self.history.addWidget(row)
            self.report_buttons.append((r,button));self.record_rows[r["id"]]=(row,check)
        self.empty=label("暂无匹配的已验证记录","muted");self.empty.setWordWrap(True);self.history.addWidget(self.empty)
        self.filter_reports(self.search.text())
        selected=next((r for r in self.reports if r['id']==old),None)
        if selected is None:selected=next((r for r in self.reports if not r.get("_lazy_record")),None)
        if selected:self.select(selected)
        else:
            self._analysis_generation+=1;self._analysis_pending.clear();self._analysis_cache.clear()
            self._analysis_record=None
            if self._analysis_worker:self._analysis_worker.requestInterruption()
            self.metric_container.setParent(self.main);self.metric_container.hide()
            self.current=None;self.caption.setText("选择一场战斗记录以加载详情" if self.reports else "暂无战斗记录")
            self.loaded_analysis_tabs=set()
            self.clear_layout(self.metrics);self.metric_cards=[]
            self.tabs.blockSignals(True)
            for i in range(len(TAB_NAMES)):self.replace_page(i,empty_page("选择一场战斗记录以加载详情" if self.reports else "暂无战斗记录",self.p,"swords"))
            self.tabs.blockSignals(False);self.update_tab_layout()
        self.footer.setText(f'{len(warnings)} 条记录读取失败，请查看诊断' if warnings else '')
        self.update_record_actions()
    def filter_reports(self,text):
        query=text.strip().casefold();count=0
        for r,b in self.report_buttons:
            row,_=self.record_rows[r["id"]]
            haystack=" ".join([r.get("display_name",""),r.get("encounter_label",""),b.text(),r.get('account',''),str(r.get('dungeon_sid',''))]+[m['name'] for m in r.get('members',[])]).casefold();visible=not query or query in haystack;row.setVisible(visible);b.setVisible(visible);count+=visible
        if hasattr(self,'empty'):self.empty.setVisible(count==0)
        self.update_record_actions()
    def select(self,report):
        report=next((row for row in self.reports if row["id"]==report["id"]),report)
        if report.get("_lazy_record"):
            self.current=report
            for row,button in self.report_buttons:button.setChecked(row['id']==report['id'])
            self.show_record_loading()
            if self._record_detail_worker and self._record_detail_worker.isRunning() and self._record_detail_worker.record_id==report['id']:
                return
            self._record_detail_generation+=1;generation=self._record_detail_generation
            worker=SavedRecordDetailWorker(self.repo,report['id'],self)
            self._record_detail_worker=worker;self._record_detail_workers.add(worker)
            worker.loaded.connect(lambda rid,value,error,w=worker,g=generation:self._record_detail_loaded(w,g,rid,value,error))
            worker.start();self.footer.setText('正在读取本场战斗记录…');self.update_record_actions();return
        changed=self.current is not report
        self.current=report
        for r,b in self.report_buttons:b.setChecked(r['id']==report['id'])
        if changed:
            if self.footer.text()=='正在读取本场战斗记录…':self.footer.setText('')
            self.render()
        self.update_record_actions()
    def select_record_id(self,record_id):
        report=next((row for row in self.reports if row['id']==record_id),None)
        if report:self.select(report)
    def show_record_loading(self,error=None):
        self._analysis_generation+=1;self._analysis_pending.clear();self._analysis_cache.clear();self._analysis_record=None
        if self._analysis_worker:self._analysis_worker.requestInterruption()
        self.metric_container.setParent(self.main);self.metric_container.hide();self.clear_layout(self.metrics);self.metric_cards=[]
        encounter=self.current.get('encounter_label') or '战斗记录'
        self.caption.setText(("读取失败 · " if error else "正在加载 · ")+encounter)
        self.loaded_analysis_tabs=set();self.tabs.blockSignals(True)
        for i in range(len(TAB_NAMES)):
            from yylogs.analysis_loading import AnalysisLoadingPage
            page=AnalysisLoadingPage(self.p,'本场战斗记录',
                lambda checked=False,record_id=self.current['id']:self.select_record_id(record_id))
            if error:page.failed(str(error),'本场战斗记录读取失败')
            self.replace_page(i,page)
        self.tabs.blockSignals(False);self.update_tab_layout()
        if error:self.footer.setText(f"战斗记录读取失败：{error}")
    def _record_detail_loaded(self,worker,generation,record_id,report,error):
        self._record_detail_workers.discard(worker)
        if self._record_detail_worker is worker:self._record_detail_worker=None
        if self._analysis_closing:return
        if report is None:
            if self.current and self.current['id']==record_id:self.show_record_loading(error)
            elif not self._record_detail_workers and self.footer.text()=='正在读取本场战斗记录…':self.footer.setText('')
            return
        index=next((i for i,row in enumerate(self.reports) if row['id']==record_id),None)
        if index is not None:self.reports[index]=report
        self.report_buttons=[(report if row['id']==record_id else row,button) for row,button in self.report_buttons]
        if not self.current or self.current['id']!=record_id:
            if not self._record_detail_workers and self.footer.text()=='正在读取本场战斗记录…':self.footer.setText('')
            return
        self.current=report;self.footer.setText('');self.render();self.update_record_actions()
    def can_manage_record(self,report):
        if not report.get("record_folder"):
            return False
        try:
            folder=Path(report["record_folder"]).resolve()
            store=Path(self.repo.battle_store).resolve()
            return folder.parent==store and folder.name.startswith("battle_") and folder.is_dir()
        except (OSError,ValueError):
            return False
    def record_checked_changed(self,report,state):
        if not self.record_manage_mode:
            return
        if state:
            self.record_checked.add(report["id"])
        else:
            self.record_checked.discard(report["id"])
        self.update_record_actions()
    def set_record_manage_mode(self,enabled):
        self.record_manage_mode=bool(enabled)
        self.record_bulk_host.setVisible(self.record_manage_mode)
        self.record_manage_button.setText("完成" if self.record_manage_mode else "多选")
        if not self.record_manage_mode:
            self.record_checked.clear()
            for _,check in self.record_rows.values():
                check.blockSignals(True);check.setChecked(False);check.blockSignals(False)
        self.update_record_actions()
    def select_all_records(self,clear=False):
        if not self.record_manage_mode:
            self.set_record_manage_mode(True)
        visible_ids=set()
        for report,_ in self.report_buttons:
            row,check=self.record_rows[report["id"]]
            if not row.isHidden() and self.can_manage_record(report):
                visible_ids.add(report["id"])
                check.blockSignals(True);check.setChecked(not clear);check.blockSignals(False)
        if clear:
            self.record_checked.difference_update(visible_ids)
        else:
            self.record_checked.update(visible_ids)
        self.update_record_actions()
    def update_record_actions(self):
        if not hasattr(self,"record_bulk_host"):
            return
        selected=[r for r in self.reports if r["id"] in self.record_checked and self.can_manage_record(r)]
        current_manage=self.current is not None and self.can_manage_record(self.current)
        for report,_ in getattr(self,"report_buttons",[]):
            _,check=self.record_rows[report["id"]]
            check.setVisible(self.record_manage_mode)
            check.setEnabled(self.can_manage_record(report))
        self.record_selection_count.setText(
            f"已选 {len(selected)} 条" if self.record_manage_mode else "")
        self.record_rename_button.setEnabled(current_manage)
        self.record_delete_button.setEnabled(current_manage)
        self.sync_favorite_button()
        self.record_bulk_delete_button.setEnabled(bool(selected))
        self.record_select_all_button.setEnabled(any(
            not self.record_rows[report["id"]][0].isHidden() and self.can_manage_record(report)
            for report,_ in getattr(self,"report_buttons",[])))
        self.record_clear_button.setEnabled(bool(self.record_checked))
    def toggle_record_favorite(self):
        report=self.current
        if report is None:
            return
        try:
            favorite=self.repo.set_favorite(report,not report.get("favorite"))
        except (OSError,ValueError,KeyError,TypeError) as exc:
            QMessageBox.warning(self,"收藏未保存",str(exc))
            return
        report["favorite"]=favorite
        self.reports.sort(key=lambda r:r.get("timestamp") or "",reverse=True)
        self.reports.sort(key=lambda r:not r.get("favorite"))
        self.reload(from_disk=False)
        self.footer.setText("已收藏该战斗记录，收藏的记录排在最前。" if favorite else "已取消收藏。")
    def sync_favorite_button(self):
        button=getattr(self,"record_favorite_button",None)
        if button is None:
            return
        report=self.current
        favorite=bool(report and report.get("favorite"))
        button.setEnabled(report is not None)
        button.setText("")
        label="取消收藏当前记录" if favorite else "收藏当前记录（置顶显示）"
        button.setAccessibleName(label)
        button.setToolTip(label)
        from yylogs.icons import icon
        button.setIcon(icon("star-filled" if favorite else "star",self.p["text"],18,button.devicePixelRatioF()))

    def rename_selected_record(self):
        report=self.current
        if report is None or not self.can_manage_record(report):
            return
        value,accepted=QInputDialog.getText(self,"重命名战斗记录","记录名称：",text=report.get("display_name",""))
        if not accepted:
            return
        try:
            report["display_name"]=self.repo.rename_record(report,value)
            self.reload(from_disk=False)
            self.footer.setText("战斗记录名称已更新。")
        except (OSError,ValueError,KeyError,TypeError) as exc:
            QMessageBox.warning(self,"重命名失败",str(exc))
    def delete_selected_records(self,*,single=False):
        if self.record_manage_mode and not single:
            targets=[r for r in self.reports if r["id"] in self.record_checked and self.can_manage_record(r)]
        else:
            targets=[self.current] if self.current is not None and self.can_manage_record(self.current) else []
        if not targets:
            return
        names="\n".join("· "+(r.get("display_name") or r["id"]) for r in targets)
        answer=QMessageBox.question(
            self,"删除战斗记录",
            f"确定永久删除 {len(targets)} 条战斗记录？\n对应的独立战斗日志将一并删除；旧版共享采集档案保留。\n\n{names}",
            QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
        if answer!=QMessageBox.Yes:
            return
        deleted=0
        try:
            for report in targets:
                self.repo.delete_record(report)
                self.reports=[r for r in self.reports if r["id"]!=report["id"]]
                deleted+=1
        except (OSError,ValueError,KeyError,TypeError) as exc:
            QMessageBox.warning(self,"删除未全部完成",f"已删除 {deleted} 条。\n{exc}")
        self.record_checked.clear()
        self.reload(from_disk=False)
        self.record_checked.clear()
        self.update_record_actions()
        self.footer.setText(f"已删除 {deleted} 条战斗记录及对应独立日志；旧版共享采集档案已保留。")
    def card(self,title,value,note):
        card=QFrame();card.setProperty("card",True);box=QVBoxLayout(card);box.setContentsMargins(14,13,14,13);box.setSpacing(6);box.addWidget(label(title,"muted"));box.addWidget(label(value,"metric"));box.addWidget(label(note,"small"));return card
    def page(self):
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setFrameShape(QFrame.NoFrame);content=QWidget();box=QVBoxLayout(content);box.setContentsMargins(0,22,0,12);box.setSpacing(18);scroll.setWidget(content);return scroll,box
    def replace_page(self,index,page):
        old=self.tabs.widget(index)
        selected_index=self.tabs.currentIndex()
        from yylogs.member_status_views import MemberStatusPage
        members=([old] if isinstance(old,MemberStatusPage) else old.findChildren(MemberStatusPage))
        for member_page in members:
            if not member_page.stop_worker():
                # Retain the object until its bounded local reader has finished.
                old.hide();self.tabs.removeTab(index);self.tabs.insertTab(index,page,TAB_NAMES[index])
                self.tabs.setCurrentIndex(selected_index)
                member_page.worker.finished.connect(old.deleteLater)
                self.tabs.setTabIcon(index,icon(TAB_ICONS[index],self.p["text"]))
                return
        self.tabs.removeTab(index);self.tabs.insertTab(index,page,TAB_NAMES[index]);self.tabs.setCurrentIndex(selected_index);old.hide();old.deleteLater()
        self.tabs.setTabIcon(index, icon(TAB_ICONS[index], self.p["text"]))
    def ensure_analysis_tab(self,index):
        if index not in TAB_RECIPIENTS:return
        if index in getattr(self,'loaded_analysis_tabs',set()):
            page = self.tabs.widget(index)
            if hasattr(page, 'sync_own_selection'):page.sync_own_selection()
            return
        if self.current is None:return
        if index in self._analysis_pending:return
        recipient=TAB_RECIPIENTS[index]
        if recipient in self._analysis_cache:
            self._install_analysis_page(index,self._analysis_cache[recipient]);return
        from yylogs.analysis_loading import AnalysisLoadingPage
        page=AnalysisLoadingPage(self.p,TAB_NAMES[index],lambda:self._retry_analysis(index))
        blocked=self.tabs.blockSignals(True)
        try:
            self.replace_page(index,page)
            self.tabs.setCurrentIndex(index)
        finally:self.tabs.blockSignals(blocked)
        self._analysis_pending[index]=page
        QTimer.singleShot(0,self._pump_analysis)
    def _retry_analysis(self,index):
        self._analysis_pending.pop(index,None)
        self.ensure_analysis_tab(index)
    def _pump_analysis(self):
        if self._analysis_closing or self._analysis_worker is not None or not self._analysis_pending:return
        index=self.tabs.currentIndex()
        if index not in self._analysis_pending:index=next(iter(self._analysis_pending))
        from yylogs.analysis_loading import AnalysisPreparation
        worker=AnalysisPreparation(self.current,TAB_RECIPIENTS[index],
            self._analysis_generation,self)
        worker.tab_index=index;self._analysis_worker=worker
        worker.finished.connect(self._analysis_finished);worker.start()
    def _analysis_finished(self):
        worker=self.sender()
        if worker is not self._analysis_worker:return
        self._analysis_worker=None
        try:
            if (self._analysis_closing or worker.generation!=self._analysis_generation
                    or worker.record is not self.current):return
            index=worker.tab_index
            loading=self._analysis_pending.get(index)
            if loading is None:return
            if worker.error:
                loading.failed(worker.error);self._analysis_pending.pop(index,None);return
            updated = (worker.result or {}).get('_record_update') if isinstance(worker.result, dict) else None
            if updated is not None:
                self.current.update(updated)
                self.current.pop('_rebuild_boss_target', None)
                self.replace_page(0, self.overview())
            self._analysis_cache[worker.recipient]=worker.result
            self._analysis_pending.pop(index,None)
            self.analysis_timings.append({'tab':index,'prepare_ms':worker.elapsed_ms})
            try:self._install_analysis_page(index,worker.result)
            except (OSError,ValueError,KeyError,TypeError) as exc:
                self._analysis_cache.pop(worker.recipient,None)
                loading.failed(str(exc))
        finally:
            worker.deleteLater()
            QTimer.singleShot(0,self._pump_analysis)
    def _install_analysis_page(self,index,prepared):
        import time
        started=time.perf_counter()
        page=observation_page(self.current,self.p,TAB_RECIPIENTS[index],prepared=prepared)
        if hasattr(page, 'targetRequested'):
            page.targetRequested.connect(self.change_boss_target)
        selected=self.tabs.currentIndex();blocked=self.tabs.blockSignals(True)
        try:
            self.replace_page(index,page);self.tabs.setCurrentIndex(selected)
            self.loaded_analysis_tabs.add(index)
        finally:self.tabs.blockSignals(blocked)
        self.analysis_timings.append({'tab':index,'gui_build_ms':(time.perf_counter()-started)*1000})
        if selected==index:
            from yylogs.analysis_loading import reveal_page
            reveal_page(page);self._last_analysis_visible=page
    def change_boss_target(self, actor):
        from yylogs.packet_entities import remember_entity
        try:
            remember_entity(self.current, actor)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, '实体选择未保存', str(exc))
            return
        self.current['_rebuild_boss_target'] = True
        self._analysis_cache.clear()
        self.render()
        self.ensure_analysis_tab(self.tabs.currentIndex())
    def render(self):
        # Keep overview-only metrics below the stable shared tab bar.
        self.metric_container.setParent(self.main);self.metric_container.hide()
        self._analysis_generation+=1;self._analysis_pending.clear()
        if self._analysis_worker:self._analysis_worker.requestInterruption()
        if self._analysis_record is not self.current:
            self._analysis_cache.clear();self._analysis_record=self.current
        r=self.current;selected=self.tabs.currentIndex();dt=datetime.fromisoformat(r['timestamp']);encounter_label=r.get('encounter_label') or '副本名称未解析';self.caption.setText(f"{dt:%Y年%m月%d日  %H:%M:%S}   /   {encounter_label}   /   已保存战斗记录")
        postcombat=r.get('record_kind')=='saved_postcombat_skill_snapshot'
        if postcombat:self.caption.setText(f"{dt:%Y年%m月%d日  %H:%M:%S} 保存快照 / {encounter_label} / 战后明细，非全程记录")
        self.history_caption=self.caption.text()
        self.clear_layout(self.metrics);self.metric_cards=[]
        duration_title=r.get('duration_label','战斗时长')
        duration_note=f"结算界面另示 {r['settlement_duration']:.0f} 秒" if postcombat else "使用缓存原始时长"
        for title,value,note in [("队伍总伤害",number(r['team_damage']),"战后截图与技能总量核对" if postcombat else "已与游戏结算核对"),(duration_title,number(r['duration'],2)+" s",duration_note),("详情口径 DPS" if postcombat else "队伍平均 DPS",number(r['team_dps'],1),"总伤害 ÷ 个人详情用时" if postcombat else "总伤害 ÷ 战斗时长"),("队伍成员",str(len(r['members'])),"当前选定账号的副本样本")]:self.metric_cards.append(self.card(title,value,note))
        if r.get('record_kind')=='saved_damage_journal':
            self.caption.setText(f"{dt:%Y年%m月%d日 %H:%M:%S} / 木桩训练 · 双人受限样本 / 已停止采集的原始事件")
            self.history_caption=self.caption.text();self.clear_layout(self.metrics);self.metric_cards=[]
            for title,value,note in [("观测伤害总量",number(r['team_damage'],3),"精确原值见明细与导出"),("伤害记录",str(r['event_count']),f"{r['positive_event_count']}条正伤害 + {r['zero_event_count']}条零伤害"),("战斗时长 / DPS","未验证 / —","不把首末伤害间隔当战斗时长"),("大号界面对照","163,084 / 秒","截图01:00；仅对照，不作为采集计时")]:self.metric_cards.append(self.card(title,value,note))
        if r.get('capture_backend')=='packet':
            self.clear_layout(self.metrics);self.metric_cards=[]
            for title,value,note in [('队伍总伤害',number(r['team_damage']),'服务器累计统计，不叠加命中通知'),
                    (duration_title,number(r['duration'],2)+' s','同场服务器计时' if r.get('duration_kind')=='server_battle_timer' else '接收观测区间，非精确开战计时'),
                    ('队伍平均 DPS',number(r['team_dps'],1),'总伤害 ÷ 当前计时口径'),
                    ('队伍成员',str(len(r['members'])),'本场服务器队员统计')]:
                self.metric_cards.append(self.card(title,value,note))
        self.arrange_metrics()
        self.tabs.blockSignals(True)
        self.loaded_analysis_tabs=set()
        self.replace_page(0,self.overview())
        for index in TAB_RECIPIENTS:self.replace_page(index,QWidget())
        self.tabs.setCurrentIndex(min(max(selected,0),len(TAB_NAMES)-1));self.tabs.blockSignals(False);self.update_tab_layout()
    def overview(self):
        r=self.current;page,box=self.page();box.addWidget(self.metric_container);self.metric_container.show()
        for card in self.metric_cards:card.show()
        journal=r.get("record_kind")=="saved_damage_journal";row=QHBoxLayout();row.addWidget(label("观测角色" if journal else "队伍表现","heading"));row.addStretch();row.addWidget(label('抓包观测' if r.get('capture_backend')=='packet' else "样本总量 / 可见行已对照" if journal else "战后截图已核对" if r.get("validation_scope")=="screenshot_correlated_postcombat_only" else "结算已核对","verified"));box.addLayout(row)
        if r.get('record_kind')=='saved_manual_capture':
            settlement_button=QPushButton('结算伤害');settlement_button.setObjectName('openSettlementButton')
            settlement_button.setIcon(icon('swords',self.p['text']));settlement_button.setMinimumHeight(38)
            settlement_button.setToolTip('查看本人结算累计候选；本场未核验，不加入逐次伤害')
            settlement_button.clicked.connect(lambda checked=False,report=r:self.open_member(report['buff_timeline']['own_entity_id'],report,metric='settlement'))
            row.insertWidget(1,settlement_button)
        table=QTableWidget(len(r['members']),8);table.setHorizontalHeaderLabels(["角色","总伤害","详情口径 DPS" if r.get("duration_kind")=="personal_detail_cache" else "平均 DPS","RDPS（估算）","承伤","治疗","伤害占比","流派"]);table.verticalHeader().hide();table.setShowGrid(False);table.setEditTriggers(QAbstractItemView.NoEditTriggers);table.setSelectionBehavior(QAbstractItemView.SelectRows);table.setSelectionMode(QAbstractItemView.SingleSelection);table.setAccessibleName("队伍伤害与承伤统计表");table.horizontalHeader().setSectionResizeMode(0,QHeaderView.Stretch)
        for c in range(1,8):table.horizontalHeader().setSectionResizeMode(c,QHeaderView.ResizeToContents)
        for i,m in enumerate(r['members']):
            table.setRowHeight(i,66);name=QTableWidgetItem(m['name']+("  ·  当前角色" if m['local'] else "")+f"\nLv.{m['level']}");name.setData(Qt.UserRole,m['id']);table.setItem(i,0,name)
            if r.get('capture_backend')=='packet' and m['level'] is None:name.setText(m['name'])
            details=r.get('skill_details',{}).get(m['id'],{}).get('rows',[])
            from yylogs.skill_styles import infer_style, style_tooltip
            style=m.get('style_observation') or infer_style(details)
            style_item=QTableWidgetItem(style['name']);style_item.setData(Qt.UserRole,style['name'])
            style_item.setTextAlignment(Qt.AlignCenter)
            table.horizontalHeaderItem(7).setTextAlignment(Qt.AlignCenter)
            style_item.setToolTip(style_tooltip(style))
            table.setItem(i,1,NumericItem(m['damage'],1));table.setItem(i,2,NumericItem(m['dps'],1))
            rd_item=NumericItem(m.get('rdps_estimate'),1)
            estimate=r.get('rd_estimate') or {}
            from yylogs.rdps_estimate import estimate_tooltip
            rd_item.setToolTip(estimate_tooltip(m, estimate))
            table.setItem(i,3,rd_item);table.setItem(i,7,style_item)
            for c,key,dec in [(4,'taken',0),(5,'healing',0)]:table.setItem(i,c,NumericItem(m[key],dec))


            share=NumericItem(m['damage_share']*100,2);share.setText(f"{m['damage_share']:.2%}");table.setItem(i,6,share)
        table.setSortingEnabled(True);table.sortItems(1,Qt.DescendingOrder)
        table.setObjectName('membersTable');table.setCursor(Qt.PointingHandCursor);table.setToolTip('点击角色所在行查看个人技能伤害；也可选中后按 Enter')
        def open_row(row, column, table=table, report=r):
            member_id = table.item(row, 0).data(Qt.UserRole)
            person = next((m for m in report['members'] if m['id'] == member_id), {})
            metric = 'healing' if column == 5 or (not person.get('damage') and person.get('healing')) else 'damage'
            self.open_member(member_id, report, metric=metric)
        table.cellClicked.connect(open_row)
        table.activated.connect(lambda index,open_row=open_row:open_row(index.row(),index.column()))
        if r.get('record_kind')=='saved_manual_capture':
            table.horizontalHeaderItem(5).setText('已采治疗')
            table.horizontalHeaderItem(5).setToolTip('治疗候选合计，非已验证有效治疗；点击查看治疗技能和 HPS。')
        box.addWidget(label('点击任意成员 → 查看个人技能伤害明细（支持 Enter）','muted'))
        table.setMinimumHeight(190);table.setMaximumHeight(220);box.addWidget(table)



        box.addSpacing(4);box.addWidget(label("伤害贡献","heading"))
        for m in sorted(r['members'], key=lambda member: member.get('damage') or 0, reverse=True):
            head=QHBoxLayout();head.addWidget(label(m['name'],"muted"));head.addStretch();head.addWidget(label(f"{number(m['damage'],1)}  /  {m['damage_share']:.2%}","muted"));box.addLayout(head)
            track=QFrame();track.setFixedHeight(6);track.setStyleSheet(f"background:{self.p['track']};border-radius:3px;");bar=QFrame(track);bar.setStyleSheet(f"background:{self.p['accent']};border-radius:3px;");bar.setGeometry(0,0,max(2,int(650*m['damage_share'])),6)
            def resize(e,track=track,bar=bar,share=m['damage_share']):bar.setGeometry(0,0,max(2,int(track.width()*share)),6)
            track.resizeEvent=resize;box.addWidget(track)
        box.addSpacing(6);box.addWidget(label("本场 Buff","heading"));known=[b for b in r['buffs'] if b['name']]
        mechanisms=(r.get('rd_estimate') or {}).get('mechanic_contributions',{})
        if mechanisms:
            effects=QTableWidget(len(mechanisms),3)
            effects.setObjectName('mechanicBuffTable');effects.setAccessibleName('本场机制 Buff 估算贡献')
            effects.setHorizontalHeaderLabels(['机制效果','增益伤害（估算）','每秒贡献（估算）'])
            effects.verticalHeader().hide();effects.setShowGrid(False)
            effects.setEditTriggers(QAbstractItemView.NoEditTriggers);effects.setSelectionMode(QAbstractItemView.NoSelection)
            effects.horizontalHeader().setSectionResizeMode(0,QHeaderView.Stretch)
            for column in (1,2):effects.horizontalHeader().setSectionResizeMode(column,QHeaderView.ResizeToContents)
            statuses={'unobserved':'未观测','trigger_unconfirmed':'未确认触发','damage_unobserved':'缺少命中数据'}
            for index,entry in enumerate(mechanisms.values()):
                title=QTableWidgetItem(entry['name']);effects.setItem(index,0,title)
                for column,key in ((1,'damage'),(2,'dps')):
                    item=NumericItem(entry.get(key),1)
                    if entry.get('status')!='estimated':item.setText(statuses.get(entry.get('status'),'未观测'))
                    item.setToolTip('机制贡献单独统计，不计入队员提供 RDPS；估算值不改变实际总伤害。')
                    effects.setItem(index,column,item)
                effects.setRowHeight(index,38)
            effects.setFixedHeight(effects.horizontalHeader().height()+38*len(mechanisms)+4)
            box.addWidget(effects)
        if known:
            b=known[0];panel=QFrame();panel.setProperty('card',True);v=QVBoxLayout(panel);v.setContentsMargins(18,16,18,16);v.setSpacing(8);h=QHBoxLayout();h.addWidget(label(b['name'],"heading"));h.addWidget(label(f"ID {b['id']}","pill"));h.addStretch();h.addWidget(label("名称 / 来源已核实","verified"));v.addLayout(h);v.addWidget(label("当前角色 → Boss  ·  "+str(b['count'])+" 次存在观测","muted"));t=label(b['description'],"muted");t.setWordWrap(True);v.addWidget(t);button=QPushButton("查看 Debuff 观测");button.clicked.connect(lambda:self.tabs.setCurrentIndex(2));v.addWidget(button,0,Qt.AlignLeft);box.addWidget(panel)
        elif not mechanisms:box.addWidget(label("本场未采集 Boss Buff，不能据此判断没有增伤效果。","muted"))
        box.addStretch();return page
    def debuffs(self):
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setFrameShape(QFrame.NoFrame);page=BuffPage(self.current,self.p);page.setMinimumHeight(610);scroll.setWidget(page);return scroll
    def open_member(self,member_id,report=None,*,metric='damage'):
        report=report or self.current
        if report.get('record_kind')=='saved_packet_capture':
            from yylogs.packet_observations import packet_skill_dialog
            dialog=packet_skill_dialog(report,member_id,metric,self)
        elif report.get("record_kind")=="saved_manual_capture":
            from yylogs.damage_candidate_views import DamageCandidateDialog
            dialog=DamageCandidateDialog(report["capture_folder"],report["buff_timeline"],self.p,self,
                                         initial_summary=report["capture_summary"],member_id=member_id,initial_metric=metric,
                                         settlement_folder=report.get("record_folder",report["capture_folder"]),
                                         record_store=self.repo.battle_store if report.get('record_folder') else None)
            dialog.linked_record.connect(self.settlement_record_added)
        else:
            dialog=SkillDialog(report,member_id,self)
        dialog.setAttribute(Qt.WA_DeleteOnClose);self.skill_dialogs.append(dialog)
        dialog.finished.connect(lambda result,d=dialog:self.skill_dialogs.remove(d) if d in self.skill_dialogs else None)
        dialog.show();return dialog
    def settlement_record_added(self,report):
        from yylogs.data import enrich_report
        report=enrich_report(report,self.repo.workspace)
        self.reports=[r for r in self.reports if r['id']!=report['id']]
        self.reports.insert(0,report)
        self.reload(from_disk=False);self.select(report)
        self.footer.setText('已保存含结算记录；原记录保留，重复快照不累加。')
    def import_damage(self):
        target,_=QFileDialog.getOpenFileName(self,"导入战斗日志包",str(self.repo.workspace or Path.home()),"yylogs 战斗记录 (*.yylogs *.zip *.json);;采集目录 (目录)")
        if not target:return
        try:
            report=import_file(target,self.repo.battle_store);self.reload()
            if self.lazy_records:
                self.reports=[report if row['id']==report['id'] else row for row in self.reports]
                self.reload(from_disk=False);self.select(report)
            else:self.select(next(r for r in self.reports if r['id']==report['id']))
            self.tabs.setCurrentIndex(0);self.footer.setText("战斗记录导入完成：已生成本地记录，未启动游戏采集。")
        except (OSError,ValueError,KeyError,TypeError) as e:QMessageBox.warning(self,"导入失败","请使用 yylogs 导出的战斗日志包或战斗日志 JSON；CSV 和诊断压缩包不能导入。\n"+str(e))
    def export(self,kind):
        if self.current and self.current.get("_lazy_record"):
            self.footer.setText("请等待本场战斗记录加载完成后再导出。")
            return
        if kind=="battle":
            folder=(self.current or {}).get('record_folder') or (self.current or {}).get('capture_folder')
            if not folder:
                self.footer.setText("当前记录没有可导出的采集原始包。");return
            dt=datetime.fromisoformat(self.current['timestamp']);tag=safe_encounter_filename(self.current.get('encounter_label') or f"副本SID{self.current.get('dungeon_sid','unknown')}");filename=f"yylogs_{tag}_{dt:%Y%m%d_%H%M%S}.yylogs"
            target,_=QFileDialog.getSaveFileName(self,"导出战斗记录包",str(Path.home()/"Desktop"/filename),"yylogs 战斗记录 (*.yylogs)")
            if target:
                try:export_record(folder,target);self.footer.setText("已导出："+target)
                except (OSError,ValueError,KeyError,TypeError) as e:QMessageBox.warning(self,"导出失败",str(e))
            return
        if not self.current:return
        r=self.current;dt=datetime.fromisoformat(r['timestamp']);tag=safe_encounter_filename(r.get('encounter_label') or f"副本SID{r.get('dungeon_sid','unknown')}");name=f"yylogs_{tag}_{dt:%Y%m%d_%H%M%S}.{kind}"
        target,_=QFileDialog.getSaveFileName(self,"导出当前报告",str(Path.home()/"Desktop"/name),"JSON (*.json)" if kind=='json' else "CSV (*.csv)")
        if not target:return
        try:
            if r.get('record_kind') in ('saved_manual_capture','saved_packet_capture') and kind=='json':
                export_record(r.get('record_folder') or r['capture_folder'],target)
            elif kind=='json':Path(target).write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8')
            else:
                with Path(target).open('w',encoding='utf-8-sig',newline='') as f:
                    w=csv.writer(f);w.writerow(['副本名称','Boss序号','Boss名称','遭遇标签','副本SID','角色','等级','当前角色','总伤害','平均DPS_报告计时口径','承伤','治疗','伤害占比','报告用时秒','观测HPS','治疗观测用时秒','有效治疗','过量治疗','治疗统计口径'])
                    encounter=r.get('encounter') or {};dungeon_name=encounter.get('dungeon_name') or encounter.get('catalog_name') or '';boss_order=encounter.get('boss_order') or '';boss_name=encounter.get('boss_name') or '';encounter_label=encounter.get('label') or r.get('encounter_label','')
                    for m in r['members']:w.writerow([dungeon_name,boss_order,boss_name,encounter_label,r.get('dungeon_sid',''),m['name'],m['level'],m['local'],m.get('damage_exact',m['damage']),m['dps'],m['taken'],m.get('healing_exact',m['healing']),m['damage_share'],r['duration'],m.get('hps'),r.get('healing_summary',{}).get('duration_seconds'),'','','已采治疗候选；有效/过量未知；覆盖与去重未核验' if r.get('healing_summary') else '历史报告原值'])
            self.footer.setText("已导出："+target)
        except OSError as e:QMessageBox.warning(self,"导出失败",str(e))


def main():
    from yylogs.runtime_paths import user_workspace

    parser=argparse.ArgumentParser(description='yylogs offline battle review')
    parser.add_argument('--workspace',type=Path,help='Directory for local records')
    args=parser.parse_args()
    workspace=args.workspace.resolve() if args.workspace else user_workspace()
    app=QApplication(sys.argv[:1])
    app.setApplicationName('yylogs')
    app.setApplicationVersion(__version__)
    app.setOrganizationName('yylogs')
    app.setStyle('Fusion')
    app.setFont(QFont('Microsoft YaHei UI',10))
    window=Window(workspace,defer_initial_load=True)
    window.show()
    return app.exec()


if __name__=='__main__':raise SystemExit(main())
