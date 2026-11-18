"""Quarantined cumulative skill amounts in the existing character dialog."""
from datetime import datetime, timedelta, timezone
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTableWidgetItem
from yylogs.settlement_summary import SKILL_NAMES


def selected_snapshot(dialog):
    data = dialog.settlement_data()
    index = dialog.settlement_snapshots.currentData()
    snapshots = data.get('snapshots', [])
    if type(index) is not int or not 0 <= index < len(snapshots):
        return None
    snapshot = snapshots[index]
    actor = snapshot.get('actor_entity_id', snapshot['own_entity_id'])
    return snapshot if dialog.selected_entity is None or dialog.selected_entity == actor else None


def render_settlement(dialog):
    from yylogs.damage_candidate_views import number
    data = dialog.settlement_data()
    snapshots = data.get('snapshots', [])
    selector = dialog.settlement_snapshots
    eligible = [(i, s) for i, s in enumerate(snapshots) if dialog.selected_entity is None
                or s.get('actor_entity_id', s['own_entity_id']) == dialog.selected_entity]
    signature = (data.get('source_sha256'), tuple(i for i, _ in eligible))
    if getattr(dialog, '_settlement_signature', None) != signature:
        dialog._settlement_signature = signature
        blocked = selector.blockSignals(True)
        selector.clear()
        for index, snapshot in eligible:
            try:
                at = datetime.fromtimestamp(snapshot['observed_unix_ms'] / 1000,
                    timezone(timedelta(hours=8))).strftime('%m-%d %H:%M:%S')
            except (ValueError, OSError, OverflowError):
                at = str(snapshot['observed_unix_ms']) + ' ms'
            actor = snapshot.get('actor_entity_id', snapshot['own_entity_id'])
            selector.addItem(f"#{snapshot['sequence']} · {actor} · {at}", index)
        selector.setCurrentIndex(len(eligible) - 1)
        selector.blockSignals(blocked)
    wrong_person = bool(snapshots) and dialog.selected_entity is not None and not eligible
    snapshot = None if wrong_person else selected_snapshot(dialog)
    dialog.heading.setText('结算伤害')
    dialog.setWindowTitle('yylogs · 结算伤害候选')
    dialog.back.setVisible(dialog.selected_entity is not None)
    dialog.query.setPlaceholderText('搜索结算技能名称或 ID')
    dialog.notice.setText('累计快照 · 未核验本场。按实际人物展示结算技能候选，不与已采逐次伤害相加，不计算 DPS / RD，也不生成命中时间轴。重复快照单独查看，不累加。')
    dialog.table.setAccessibleName('按人物归属的结算伤害候选；场次未核验；未计入伤害')
    dialog.table.setHorizontalHeaderLabels(['结算技能', '技能 ID', '累计候选', '数据状态', '计入伤害'])
    dialog.member_ids = []
    dialog.settlement_reset.setEnabled(data.get('temporary_file') is True and not dialog.settlement_loading)
    if snapshot:
        actor = snapshot.get('actor_entity_id', snapshot['own_entity_id'])
        dialog.summary.setToolTip(f"伤害人物实体：{actor}\n采集本人实体：{snapshot['own_entity_id']}\n场景：{snapshot['scene']}\nSHA256：{data.get('source_sha256')}\n回调观测 Unix 毫秒：{snapshot['observed_unix_ms']}（不是命中时间）\n同场景同人物不等于同一场战斗，场次仍未核验。")
        source = '人物详情面板' if snapshot['source'] == 'natural_damage_statistic_panel' else '自然统计回调'
        dialog.metrics.setText(f"{source} · 快照 #{snapshot['sequence']} · 本场未核验 · 未计入伤害")
        dialog.summary.setText(f"面板总伤候选 {number(snapshot['total_damage_candidate'])} · 候选用时 {number(snapshot['duration_candidate'], 3)} 秒。与当前伤害统计区间无关，不据此补差额。来源文件：{data['source_file']}。" + (' 临时查看，未写入当前战斗记录。' if data.get('temporary_file') else ''))
    else:
        dialog.metrics.setText('未取得结算金额' if not wrong_person else '未取得该人物结算快照')
        states = {'missing': '此记录未保存结算快照。', 'empty': '日志没有金额快照；就绪或停止通知不代表取得伤害。',
                  'invalid': '结算文件校验失败，未展示其中金额：' + str(data.get('error') or '')}
        dialog.summary.setText(('没有当前人物的快照，不使用其他人物的金额代替。' if wrong_person else
            states.get(data.get('status'), '尚未选择可用快照。')) + ' 未取得不等于零；不会搜索或借用其他场记录。')
    amounts = {s['skill_id']: s['damage'] for s in snapshot['skills']} if snapshot else {}
    query = dialog.query.text().strip().casefold()
    rows = [(skill, name) for skill, name in SKILL_NAMES.items()
            if not query or query in f'{name} {skill}'.casefold()]
    dialog.table.setRowCount(len(rows))
    for i, (skill, name) in enumerate(rows):
        found = skill in amounts
        values = [name, skill, number(amounts.get(skill)), '累计候选' if found else '未取得', '否']
        for j, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            item.setToolTip('仅该次快照的原始技能累计金额；不是一次命中，未核验本场，未计入伤害。' if found else '未取得该技能金额，不代表未施放或伤害为零。')
            if j in (1, 2):
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            dialog.table.setItem(i, j, item)
        dialog.table.setRowHeight(i, 46)
    selector.setEnabled(bool(eligible))
    dialog.export_button.setEnabled(bool(snapshot and snapshot['skills']))
    if dialog.settlement_loading:
        dialog.metrics.setText('正在后台读取本地结算快照…')
        dialog.summary.setText('正在校验人物归属、场景和金额字段；未启动游戏采集。')
        selector.setEnabled(False);dialog.export_button.setEnabled(False)
