"""Delete only a manually selected, stopped and cleanup-verified local session."""
import json
import os
from pathlib import Path
import re
import shutil
import stat
from yylogs.local_files import read_json


def _object(path):
    value=read_json(path)
    if value is None:return {}
    if not isinstance(value,dict):raise ValueError("采集状态格式无效，不能删除")
    return value


def _plain(path):
    st = path.lstat()
    if path.is_symlink() or getattr(st, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
        raise ValueError('不能删除含符号链接或目录联接的采集目录')


def validate_deletion(workspace, folder, *, verify_tree=True):
    workspace = Path(workspace).absolute()
    base = workspace / 'logs_research' / 'manual_captures'
    folder = Path(folder).absolute()
    if folder.parent != base or not re.fullmatch(r'\d{8}_\d{6}_(?:packet_)?[0-9a-f]{8}', folder.name):
        raise ValueError('只能删除当前工作区内明确选中的采集会话')
    for path in (workspace, workspace / 'logs_research', base, folder):
        _plain(path)
    if not folder.is_dir() or folder.resolve().parent != base.resolve():
        raise ValueError('采集路径无效')
    state = _object(folder / 'capture_state.json')
    controller = _object(folder / 'controller_status.json')
    packet = '_packet_' in folder.name
    result = _object(folder / ('packet_capture_result.json' if packet else 'capture_result.json'))
    if packet and controller.get('backend')!='packet' and result.get('schema')!='yylogs.packet-result/1':
        raise ValueError('缺少抓包会话身份，不能删除')
    locks = [base / 'active_session.json']
    if packet:
        shared = Path(os.environ.get('LOCALAPPDATA', Path.home()/'AppData/Local'))/'YYLOGS/logs_research/manual_captures/active_session.json'
        locks.append(shared)
    for lock in locks:
        pointer = _object(lock)
        if pointer.get('folder') and Path(pointer['folder']).resolve() == folder.resolve():
            raise ValueError('该会话仍标记为采集中，不能删除')
    if state.get('active') is True or controller.get('state') in ('preparing', 'recording', 'stopping', 'cleanup_unverified', 'worker_lost'):
        raise ValueError('采集未停止或后台清理未核验，不能删除')
    if controller.get('cleanup_verified') is False or result.get('cleanup_verified') is False:
        raise ValueError('清理状态有冲突或未成功，不能删除')
    if controller.get('cleanup_verified') is not True and result.get('cleanup_verified') is not True:
        raise ValueError('缺少清理成功证据，暂不允许删除')
    if result.get('background_collection_active') is True:
        raise ValueError('后台采集仍在运行，不能删除')
    if verify_tree:
        # Check before rmtree: never follow a link to a game/source/other session.
        for directory, dirs, files in os.walk(folder, followlinks=False):
            for name in dirs + files:
                _plain(Path(directory) / name)
    return folder


def delete_capture(workspace, folder):
    folder = validate_deletion(workspace, folder)
    return _delete_validated_capture(workspace, folder)


def _delete_validated_capture(workspace, folder):
    """Delete a folder just fully validated by the batch caller."""
    folder = Path(folder)
    base = folder.parent
    for name in ('latest_saved.json','latest_joint_probe.json'):
        value=_object(base/name)
        if value.get('folder'):Path(value['folder']).resolve()
    from yylogs.packet_action_archive import retain_actions_before_delete
    retain_actions_before_delete(workspace, folder)
    # Exact path only. Caller must obtain explicit UI confirmation first.
    shutil.rmtree(folder)
    for name in ('latest_saved.json', 'latest_joint_probe.json'):
        pointer = base / name
        value = _object(pointer)
        if value.get('folder') and Path(value['folder']).resolve() == folder.resolve():
            pointer.unlink()
    return folder.name
