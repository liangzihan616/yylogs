"""Task-based desktop shell. Presentation only; never starts game collection."""
from pathlib import Path
from datetime import datetime
import json
from PySide6.QtCore import Qt,QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QWidget,QFrame,QLabel,QPushButton,QVBoxLayout,QHBoxLayout,QGridLayout,QScrollArea,QTableWidget,QTableWidgetItem,QHeaderView,QAbstractItemView,QTabWidget,QLineEdit,QMenu,QButtonGroup,QStackedWidget,QSizePolicy,QCheckBox,QSlider)
from yylogs import __version__
from yylogs.icons import decorate

SECTIONS={'records':('战斗记录','选择一场记录，查看队伍与个人表现。'),'archive':('采集档案','浏览本地已保存的采集档案。'),'settings':('设置与说明','外观与数据位置。')}
AUTHOR_DOUYIN_URL='https://www.douyin.com/user/MS4wLjABAAAAyDTqXxSQWWGXLgfAsSVjYMTMdprCLfgnKtdrigJzx9Ih15HtR5hrm4jlm5nstUV2?from_tab_name=main'


class FeedbackLabel(QLabel):
    def __init__(self,parent=None):
        super().__init__(parent)
        self.setTextFormat(Qt.PlainText)

    def setText(self,value):
        super().setText(value)
        self.setVisible(bool(value.strip()))


def text(value,kind=None):
    widget=QLabel(value);widget.setTextFormat(Qt.PlainText);widget.setWordWrap(True)
    if kind:widget.setProperty('kind',kind)
    return widget


def button(value,slot):
    item=QPushButton(value);item.setMinimumHeight(42);item.setCursor(Qt.PointingHandCursor);item.clicked.connect(slot);return item


def scroll_page():
    scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setFrameShape(QFrame.NoFrame)
    content=QWidget();box=QVBoxLayout(content);box.setContentsMargins(0,0,4,8);box.setSpacing(20);scroll.setWidget(content)
    return scroll,box


def panel(title,description):
    widget=QFrame();widget.setProperty('card',True);box=QVBoxLayout(widget);box.setContentsMargins(22,20,22,20);box.setSpacing(12)
    box.addWidget(text(title,'heading'))
    if description:box.addWidget(text(description,'muted'))
    return widget,box


def build_shell(w):
    central=QWidget();w.setCentralWidget(central);root=QHBoxLayout(central);root.setContentsMargins(0,0,0,0);root.setSpacing(0)
    w.sidebar=QFrame();w.sidebar.setObjectName('sidebar');w.sidebar.setFixedWidth(204);root.addWidget(w.sidebar)
    side=QVBoxLayout(w.sidebar);side.setContentsMargins(18,26,18,20);side.setSpacing(8)
    side.addWidget(text('yylogs','brand'));side.addWidget(text('离线复盘 · PVE 战斗分析','small'));side.addSpacing(28)
    w.section_group=QButtonGroup(w);w.section_group.setExclusive(True);w.section_buttons={}
    for key,title,icon_name in [('records','战斗记录','swords'),('archive','采集档案','archive')]:
        item=button(title,lambda checked=False,k=key:w.navigate(k));item.setProperty('sectionNav',True);item.setCheckable(True);item.setAccessibleName('导航：'+title);w.section_group.addButton(item);w.section_buttons[key]=item;side.addWidget(item);decorate(item,icon_name,label=title)
    side.addStretch();settings=button('设置与说明',lambda:w.navigate('settings'));settings.setProperty('sectionNav',True);settings.setCheckable(True);w.section_group.addButton(settings);w.section_buttons['settings']=settings;side.addWidget(settings);side.addSpacing(12);decorate(settings,'settings-2',label='设置与说明')
    w.side_status=text('','small');side.addWidget(w.side_status);w.side_status.hide();side.addWidget(text('v'+__version__,'small'))
    w.nav=w.section_buttons['records'];w.self_buff_nav=w.section_buttons['archive']
    body=QWidget();root.addWidget(body,1);layout=QVBoxLayout(body);layout.setContentsMargins(28,24,28,14);layout.setSpacing(20)
    top=QHBoxLayout();heading=QVBoxLayout();heading.setSpacing(6);w.section_title=text('战斗记录','title');w.section_subtitle=text(SECTIONS['records'][1],'muted');heading.addWidget(w.section_title);heading.addWidget(w.section_subtitle);top.addLayout(heading,1)
    w.refresh_button=button('刷新',w.refresh_active);w.refresh_button.setToolTip('重新读取本地文件，不连接游戏（Ctrl+R）');decorate(w.refresh_button,'refresh-cw');top.addWidget(w.refresh_button)
    w.import_button=button('导入日志包',w.import_damage);w.import_button.setToolTip('一键导入已停止采集并生成战斗记录');decorate(w.import_button,'file-input');top.addWidget(w.import_button)
    w.export_button=button('导出报告',lambda:None);menu=QMenu(w.export_button);menu.addAction('战斗记录包 · ZIP',lambda:w.export('battle'));menu.addAction('当前报告 · JSON',lambda:w.export('json'));menu.addAction('队伍统计 · CSV',lambda:w.export('csv'));w.export_button.setMenu(menu);decorate(w.export_button,'file-output');top.addWidget(w.export_button);layout.addLayout(top)
    w.sections=QStackedWidget();layout.addWidget(w.sections,1);w.section_indices={}
    # Records: local history is a secondary pane, never mixed into global navigation.
    records=QWidget();record_layout=QHBoxLayout(records);record_layout.setContentsMargins(0,0,0,0);record_layout.setSpacing(24)
    w.record_list_panel=QFrame();w.record_list_panel.setObjectName('recordList');w.record_list_panel.setFixedWidth(214);left=QVBoxLayout(w.record_list_panel);left.setContentsMargins(0,0,0,0);left.setSpacing(12)
    left.addWidget(text('已保存记录','heading'));w.search=QLineEdit();w.search.setPlaceholderText('搜索副本或记录名称');w.search.setAccessibleName('搜索本地战斗记录');w.search.textChanged.connect(w.filter_reports);left.addWidget(w.search)
    record_actions=QHBoxLayout()
    w.record_manage_button=button('多选',lambda:w.set_record_manage_mode(not w.record_manage_mode))
    decorate(w.record_manage_button,'list-checks',label='多选战斗记录')
    record_actions.addWidget(w.record_manage_button,1)
    w.record_favorite_button=button('收藏',w.toggle_record_favorite)
    decorate(w.record_favorite_button,'star',icon_only=True,label='收藏当前记录')
    record_actions.addWidget(w.record_favorite_button)
    w.record_rename_button=button('重命名',w.rename_selected_record)
    decorate(w.record_rename_button,'settings-2',icon_only=True,label='重命名当前记录')
    record_actions.addWidget(w.record_rename_button)
    w.record_delete_button=button('删除当前',lambda:w.delete_selected_records(single=True))
    decorate(w.record_delete_button,'trash-2',icon_only=True,label='删除当前记录')
    record_actions.addWidget(w.record_delete_button)
    left.addLayout(record_actions)
    w.record_bulk_host=QWidget();w.record_bulk_actions=QVBoxLayout(w.record_bulk_host);w.record_bulk_actions.setContentsMargins(0,0,0,0);w.record_bulk_actions.setSpacing(6)
    bulk_row=QHBoxLayout()
    w.record_select_all_button=button('全选',w.select_all_records);bulk_row.addWidget(w.record_select_all_button)
    w.record_clear_button=button('清除',lambda:w.select_all_records(True));bulk_row.addWidget(w.record_clear_button)
    w.record_bulk_actions.addLayout(bulk_row)
    bulk_row=QHBoxLayout()
    w.record_selection_count=text('','small');bulk_row.addWidget(w.record_selection_count,1)
    w.record_bulk_delete_button=button('删除选中',w.delete_selected_records);bulk_row.addWidget(w.record_bulk_delete_button)
    w.record_bulk_actions.addLayout(bulk_row)
    left.addWidget(w.record_bulk_host)
    history_content=QWidget();w.history=QVBoxLayout(history_content);w.history.setContentsMargins(0,0,4,0);w.history.setSpacing(6);w.history.setAlignment(Qt.AlignTop)
    w.history_scroll=QScrollArea();w.history_scroll.setWidgetResizable(True);w.history_scroll.setFrameShape(QFrame.NoFrame);w.history_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff);w.history_scroll.setWidget(history_content);left.addWidget(w.history_scroll,1);record_layout.addWidget(w.record_list_panel)
    w.main=QWidget();analysis=QVBoxLayout(w.main);analysis.setContentsMargins(0,0,0,0);analysis.setSpacing(16)
    w.caption=text('','small');w.caption.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Fixed);analysis.addWidget(w.caption)
    w.metric_container=QWidget(w.main);w.metric_container.hide();w.metrics=QGridLayout(w.metric_container);w.metrics.setContentsMargins(0,0,0,0);w.metrics.setSpacing(12);w.metric_cards=[]
    w.tabs=QTabWidget();w.tabs.setDocumentMode(True);w.tabs.tabBar().setDrawBase(False);w.tabs.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Expanding)
    from yylogs.observation_views import TAB_NAMES
    for name in TAB_NAMES:w.tabs.addTab(QWidget(),name)
    analysis.addWidget(w.tabs,1);record_layout.addWidget(w.main,1)
    w.section_indices['records']=w.sections.addWidget(records)
    # Archive: real session metadata only. No inferred combat duration or fabricated DPS.
    archive=QWidget();archive_box=QVBoxLayout(archive);archive_box.setContentsMargins(0,0,0,0);archive_box.setSpacing(16)
    actions=QHBoxLayout();w.archive_search=QLineEdit();w.archive_search.setPlaceholderText('搜索采集会话');w.archive_search.setAccessibleName('筛选采集档案');w.archive_search.textChanged.connect(w.filter_archives);actions.addWidget(w.archive_search,1);w.latest_timeline=button('最近时间轴',w.show_self_buff_timeline);decorate(w.latest_timeline,'chart-no-axes-combined');actions.addWidget(w.latest_timeline);archive_box.addLayout(actions)
    w.archive_table=QTableWidget(0,6);w.archive_table.setAccessibleName('本地采集会话，双击打开时间轴');w.archive_table.setHorizontalHeaderLabels(['选择','采集时间','场景 / 范围','保存状态','时间轴','会话 ID']);w.archive_table.verticalHeader().hide();w.archive_table.setShowGrid(False);w.archive_table.setEditTriggers(QAbstractItemView.NoEditTriggers);w.archive_table.setSelectionBehavior(QAbstractItemView.SelectRows);w.archive_table.setSelectionMode(QAbstractItemView.SingleSelection);w.archive_table.horizontalHeader().setSectionResizeMode(5,QHeaderView.Stretch);w.archive_table.setColumnHidden(0,True);w.archive_table.itemChanged.connect(lambda item:w.archive_item_changed(item));w.archive_table.itemSelectionChanged.connect(w.update_archive_selection);w.archive_table.activated.connect(lambda *_:w.open_selected_archive());archive_box.addWidget(w.archive_table,1)
    for c in range(1,5):w.archive_table.horizontalHeader().setSectionResizeMode(c,QHeaderView.ResizeToContents)
    w.archive_note=text('会话保存时间不是战斗时长。缺少开战 / 结束证据的记录仅供原始诊断，不计算覆盖率或 rDPS。','small');w.archive_note.hide()
    archive_actions=QHBoxLayout();w.archive_count=text('','muted');archive_actions.addWidget(w.archive_count,1);w.archive_select_mode=button('多选删除',lambda:w.set_archive_delete_mode(True));decorate(w.archive_select_mode,'list-checks');archive_actions.addWidget(w.archive_select_mode);w.archive_select_all=button('全选',lambda:w.select_all_archives());archive_actions.addWidget(w.archive_select_all);w.archive_clear_selection=button('清除选择',lambda:w.select_all_archives(True));archive_actions.addWidget(w.archive_clear_selection);w.archive_selection_count=text('','muted');archive_actions.addWidget(w.archive_selection_count);w.archive_import_button=button('导入战斗记录',w.import_selected_archive);decorate(w.archive_import_button,'file-check-2');archive_actions.addWidget(w.archive_import_button);w.archive_folder_button=button('打开会话目录',w.open_archive_folder);decorate(w.archive_folder_button,'folder-open');archive_actions.addWidget(w.archive_folder_button);w.archive_open_button=button('查看时间轴',w.open_selected_archive);decorate(w.archive_open_button,'chart-no-axes-combined');archive_actions.addWidget(w.archive_open_button);w.archive_delete_button=button('删除选中',w.delete_selected_archive);decorate(w.archive_delete_button,'trash-2');w.archive_delete_button.setToolTip('仅删除已停止并完成清理的会话；需要再次确认');archive_actions.addWidget(w.archive_delete_button);archive_box.addLayout(archive_actions);w.section_indices['archive']=w.sections.addWidget(archive)
    settings,settings_box=scroll_page();appearance,appearance_box=panel('外观','中性灰主题。仅切换显示，不影响采集或数据。');row=QHBoxLayout();row.addWidget(text('界面主题','muted'),1);w.theme=button('切换深色',w.toggle_theme);row.addWidget(w.theme);appearance_box.addLayout(row);settings_box.addWidget(appearance)
    storage,storage_box=panel('数据与版本',None);storage_box.addWidget(text('yylogs '+__version__+' · 离线复盘','muted'));path=str(w.repo.workspace) if w.repo.workspace else '默认数据目录';path_label=text(path,'small');path_label.setTextInteractionFlags(Qt.TextSelectableByMouse);storage_box.addWidget(path_label)
    path_label.setWordWrap(True)
    if w.repo.workspace:storage_box.addWidget(button('打开数据目录',lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(w.repo.workspace)/'logs_research')))),0,Qt.AlignLeft)
    storage_actions=QHBoxLayout()
    w.storage_choose_button=button('选择保存文件夹',w.choose_storage_folder);decorate(w.storage_choose_button,'folder-open');storage_actions.addWidget(w.storage_choose_button)
    w.storage_reset_button=button('恢复默认目录',w.reset_storage_folder);storage_actions.addWidget(w.storage_reset_button);storage_actions.addStretch();storage_box.addLayout(storage_actions)
    w.storage_pending_label=text('','small');w.storage_pending_label.setWordWrap(True);w.storage_pending_label.setTextInteractionFlags(Qt.TextSelectableByMouse);w.storage_pending_label.hide();storage_box.addWidget(w.storage_pending_label)
    from yylogs.runtime_paths import configured_workspace
    pending=configured_workspace()
    if pending is not None and (not w.repo.workspace or pending!=Path(w.repo.workspace).resolve()):
        w.storage_pending_label.setText('重启后保存到：'+str(pending));w.storage_pending_label.show()
    settings_box.addWidget(storage)
    author,author_box=panel('\u4f5c\u8005',None);w.author_douyin_button=button('\u8bbf\u95ee\u4f5c\u8005\u6296\u97f3',lambda:QDesktopServices.openUrl(QUrl(AUTHOR_DOUYIN_URL)));w.author_douyin_button.setAccessibleName('\u6253\u5f00\u4f5c\u8005\u6296\u97f3\u4e3b\u9875');w.author_douyin_button.setToolTip('\u5728\u9ed8\u8ba4\u6d4f\u89c8\u5668\u4e2d\u6253\u5f00\u4f5c\u8005\u6296\u97f3\u4e3b\u9875');author_box.addWidget(w.author_douyin_button,0,Qt.AlignLeft);settings_box.addWidget(author)
    settings_box.addStretch();w.section_indices['settings']=w.sections.addWidget(settings)
    w.footer=FeedbackLabel();w.footer.setProperty('kind','small');w.footer.setWordWrap(True);layout.addWidget(w.footer);w.footer.hide();w.section_subtitle.hide();w.active_section='records';w.section_buttons['records'].setChecked(True)


def navigate(w,key):
    if key not in SECTIONS:raise ValueError('Unknown navigation section')
    w.active_section=key;w.sections.setCurrentIndex(w.section_indices[key]);w.section_buttons[key].setChecked(True)
    title,subtitle=SECTIONS[key];w.section_title.setText(title);w.section_subtitle.setText(subtitle)
    w.import_button.setVisible(key=='records');w.export_button.setVisible(key=='records');w.export_button.setEnabled(key=='records' and w.current is not None)
    w.refresh_button.setVisible(key!='settings')
    if key=='archive':w.refresh_archives()
    w.update_tab_layout()


def read_object(path):
    if not path.is_file():return {}
    try:
        value=json.loads(path.read_text('utf-8-sig'));return value if isinstance(value,dict) else {}
    except (ValueError,OSError):return {}


def refresh_archives(w):
    table=w.archive_table;selected=w.selected_archive();previous=selected['folder'] if selected else None;rows=[]
    if w.repo.workspace:
        base=Path(w.repo.workspace)/'logs_research/manual_captures'
        if base.is_dir():
            for folder in sorted(base.iterdir(),key=lambda p:p.name,reverse=True):
                if not folder.is_dir() or folder.is_symlink() or folder.resolve().parent!=base.resolve():continue
                if not any((folder/name).is_file() for name in ('capture_state.json','controller_status.json','capture_result.json','self_buff_timeline.json')):continue
                status=read_object(folder/'controller_status.json');capture=read_object(folder/'capture_state.json');result=read_object(folder/'capture_result.json')
                state=status.get('state');ready=(folder/'self_buff_timeline.json').is_file();scope=capture.get('scope') or result.get('scope')
                if status.get('backend')=='packet':
                    ready=(folder/'packet_records.json').is_file() and status.get('cleanup_verified') is True
                    scope='副本 PVE · 抓包'
                if status.get('backend')=='packet':pass
                elif status.get('joint_manual_capture') or result.get('joint_manual_capture'):scope='副本 · 联合采集'
                else:scope={'self_only':'自身 Buff','pve':'副本 PVE','training':'木桩训练'}.get(scope,'原始采集')
                title={'saved':'已保存','failed':'已中断','recording':'记录中','preparing':'准备中','stopping':'停止中','cleanup_unverified':'清理待核验','worker_lost':'异常待核验'}.get(state,'已结束' if capture.get('active') is False else '状态待核实')
                if status.get('cleanup_verified') is True:title+=' · 已清理'
                try:when=datetime.strptime(folder.name[:15],'%Y%m%d_%H%M%S').strftime('%m-%d  %H:%M:%S')
                except ValueError:when='时间未标记'
                rows.append({'folder':folder,'values':[when,scope,title,'可查看' if ready else '无可用时间轴',folder.name],'ready':ready})
    w.archive_rows=rows;table.setRowCount(len(rows));pick=0
    for i,row in enumerate(rows):
        if row['folder']==previous:pick=i
        check=QTableWidgetItem();check.setFlags(Qt.ItemIsUserCheckable|Qt.ItemIsEnabled);check.setCheckState(Qt.Checked if row['folder'] in getattr(w,'archive_checked',set()) else Qt.Unchecked);table.setItem(i,0,check)
        for j,value in enumerate(row['values']):
            item=QTableWidgetItem(value);item.setToolTip(str(row['folder']));table.setItem(i,j+1,item)
        table.setRowHeight(i,52)
    w.filter_archives(w.archive_search.text())
    if rows:table.selectRow(pick)
    w.update_archive_selection()
