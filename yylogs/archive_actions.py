"""Selection and confirmed batch deletion for the local capture archive."""
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QDialog, QMessageBox
from yylogs.capture_delete import validate_deletion, delete_capture


def selected_rows(w):
    if not getattr(w, "archive_delete_mode_enabled", False):
        row = w.selected_archive()
        return [row] if row else []
    return [row for i, row in enumerate(w.archive_rows)
            if row["folder"] in w.archive_checked and not w.archive_table.isRowHidden(i)]


def can_delete(w, row):
    try:
        if w.recording_controls.controller.busy:
            return False
        validate_deletion(w.repo.workspace, row["folder"], verify_tree=False)
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def set_mode(w, enabled):
    w.archive_delete_mode_enabled = enabled
    w.archive_checked.clear()
    table = w.archive_table
    table.setColumnHidden(0, not enabled)
    table.setSelectionMode(QAbstractItemView.ExtendedSelection if enabled else QAbstractItemView.SingleSelection)
    for control in (w.archive_select_all, w.archive_clear_selection, w.archive_selection_count):
        control.setVisible(enabled)
    previous = table.blockSignals(True)
    for i in range(table.rowCount()):
        table.item(i, 0).setCheckState(Qt.Unchecked)
    table.blockSignals(previous)
    w.update_archive_selection()


def check_changed(w, item):
    if item.column() != 0 or not w.archive_delete_mode_enabled:
        return
    row = w.archive_rows[item.row()]
    if item.checkState() == Qt.Checked and can_delete(w, row):
        w.archive_checked.add(row["folder"])
    else:
        w.archive_checked.discard(row["folder"])
    w.update_archive_selection()


def select_all(w, clear=False):
    table = w.archive_table
    previous = table.blockSignals(True)
    for i, row in enumerate(w.archive_rows):
        if clear or not table.isRowHidden(i):
            checked = not clear and can_delete(w, row)
            table.item(i, 0).setCheckState(Qt.Checked if checked else Qt.Unchecked)
            if checked:
                w.archive_checked.add(row["folder"])
            else:
                w.archive_checked.discard(row["folder"])
    table.blockSignals(previous)
    w.update_archive_selection()


def delete_selected(w):
    rows = selected_rows(w)
    if not rows:
        return
    if w.recording_controls.controller.busy:
        QMessageBox.warning(w, "不能删除", "请先停止采集并核验清理。")
        return
    folders = {row["folder"] for row in rows}
    for dialog in w.findChildren(QDialog):
        if getattr(dialog, "capture_folder", None) in folders or getattr(dialog, "folder", None) in folders:
            QMessageBox.warning(w, "不能删除", "请先关闭所选采集的时间轴和伤害统计窗口。")
            return
    try:
        for row in rows:
            validate_deletion(w.repo.workspace, row["folder"], verify_tree=False)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        QMessageBox.warning(w, "不能删除", str(exc))
        return
    names = "\n".join(row["folder"].name for row in rows[:10])
    if len(rows) > 10:
        names += f"\n另 {len(rows)-10} 次采集"
    answer = QMessageBox.question(
        w, f"删除选中的 {len(rows)} 次采集？",
        "将永久删除这些采集的原始日志和导出文件，无法撤销。已导入战斗记录的独立副本会保留。\n\n"+names,
        QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
    if answer != QMessageBox.Yes:
        return
    deleted = []
    try:
        # Perform the expensive recursive link check once, after confirmation.
        for row in rows:
            validate_deletion(w.repo.workspace, row["folder"])
        from yylogs.capture_delete import _delete_validated_capture
        for row in rows:
            deleted.append(_delete_validated_capture(w.repo.workspace, row["folder"]))
            controller = w.recording_controls.controller
            if controller.folder == row["folder"] and controller.finalized:
                controller.folder = None
                controller.status = {"state": "idle"}
                w.recording_controls.refresh()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        QMessageBox.warning(w, "删除未全部完成", f"已删除 {len(deleted)} 次采集。\n"+str(exc))
    w.archive_checked.clear()
    w.refresh_archives()
    w.archive_note.setText(f"已删除 {len(deleted)} 次采集")
    w.footer.setText(f"已删除 {len(deleted)} 条采集记录")
