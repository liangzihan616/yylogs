"""Healing projection for the existing asynchronous character/skill dialog."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTableWidgetItem


def render_healing(dialog):
    from yylogs.damage_candidate_views import number
    data = dialog.data.get('healing_summary', {})
    members = data.get('members', [])
    member = next((m for m in members if m['id'] == dialog.selected_entity), None)
    if member is None:
        dialog.selected_entity = None
    query = dialog.query.text().strip().casefold()
    seconds = data.get('duration_seconds')
    duration = f'观测 {number(seconds, 3)} 秒' if seconds else '战斗用时未验证 · HPS —'
    name = (member['name'] or member['id']) if member else '人物治疗'
    dialog.heading.setText(name)
    dialog.setWindowTitle('yylogs · 人物治疗统计')
    dialog.notice.setText('已采治疗候选 · 非完整结算。只统计已识别人物向已识别人物产生的治疗类型记录，不计入伤害、DPS 或 RD。有效治疗、过量治疗及护盾量未知，不能由血量变化推算；覆盖与去重尚未核验。')
    dialog.table.setAccessibleName('人物治疗列表，点击人物查看治疗技能汇总')
    dialog.back.setVisible(member is not None)
    dialog.query.setPlaceholderText('搜索治疗技能名称或 ID' if member else '搜索人物名称')
    dialog.table.setHorizontalHeaderLabels(['技能' if member else '人物', '治疗记录数',
        '已采治疗合计', '占比', '观测 HPS'])
    amount = member['healing'] if member else data.get('total_healing', 0)
    rate = member['hps'] if member else amount / seconds if seconds else None
    dialog.metrics.setText(f'{name} · 已采治疗 {number(amount)} · 观测 HPS {number(rate, 1)} · {duration}')
    dialog.metrics.setToolTip('HPS 使用与伤害相同的选定观测区间，不使用首末治疗间隔或伤害事件估计用时。')
    rows = []
    dialog.member_ids = []
    if member:
        for skill in member['skills']:
            ids = ', '.join(map(str, skill['skill_ids'] + skill['calcpoint_ids']))
            if query and query not in (skill['name'] + ' ' + ids).casefold():
                continue
            rows.append(([skill['name'], skill['record_count'], number(skill['healing']),
                number(skill['share'] * 100, 1) + '%' if skill['share'] is not None else '—',
                number(skill['hps'], 1)], '原始技能 / 计算点 ID：' + ids + '\n受治疗实体：' +
                ', '.join(skill['recipients']) + '\n记录数不是施放次数；有效/过量治疗未知。'))
    else:
        for actor in members:
            label = actor['name'] or actor['id']
            if query and query not in label.casefold():
                continue
            dialog.member_ids.append(actor['id'])
            rows.append(([label, actor['record_count'], number(actor['healing']),
                number(actor['share'] * 100, 1) + '%' if actor['share'] is not None else '—',
                number(actor['hps'], 1)], '治疗提供者：' + actor['id'] + '\n队伍成员身份未核验。'))
    dialog.table.setRowCount(len(rows))
    for i, (values, tooltip) in enumerate(rows):
        for j, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            item.setToolTip(tooltip)
            if j:
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            dialog.table.setItem(i, j, item)
        dialog.table.setRowHeight(i, 46)
    rejected = data.get('rejected', {})
    reasons = {'different_scene': '其他场景', 'unresolved_source': '来源未识别',
               'unresolved_recipient': '受治疗对象未核验', 'negative_amount': '负值记录'}
    detail = '；'.join(f'{reasons.get(key, key)} {value}' for key, value in rejected.items() if value)
    dialog.summary.setText(f'{duration} · 受治疗对象：本场可识别人物（不受伤害 Boss 筛选限制） · '
        f"已采治疗记录 {data.get('record_count', 0):,} 条。" +
        ('没有可核验来源和受治疗对象的治疗候选，不代表没有治疗。' if not members else
         '同名技能按人物汇总，原始 ID 和受治疗对象保留。') +
        (' 排除：' + detail if detail else '') +
        (' 仅为观测片段，不是完整战斗 HPS。' if dialog.data.get('partial_observation') else '') +
        (' 原记录已触及条数上限，无法恢复缺失治疗。' if dialog.data.get('known_truncated') else ''))
    dialog.export_button.setEnabled(bool(rows))
