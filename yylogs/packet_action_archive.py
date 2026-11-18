"""Keep verified legacy action observations independent of capture cleanup."""
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

SCHEMA = 'yylogs.packet-action-archive/1'
NAME = 'packet_actions_archive.json'
MAX_BYTES = 64 * 1024 * 1024


def _encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(',', ':')).encode('utf8')


def _scope(snapshot):
    return {key: snapshot.get(key) for key in
            ('session', 'battle_id', 'source_sha256', 'start', 'end')}


def _path(folder):
    if not folder:return None
    folder = Path(folder)
    if not folder.is_dir() or folder.is_symlink() or folder.is_junction():return None
    path = folder / NAME
    if path.is_symlink() or path.is_junction():return None
    return path


def archived_actions(snapshot, folder):
    """Only accept observations tied to the exact saved encounter identity."""
    if 'actions' in snapshot:return snapshot
    path = _path(folder)
    if path is None or not path.is_file():return snapshot
    try:
        if path.stat().st_size > MAX_BYTES:return snapshot
        value = json.loads(path.read_text('utf8'))
        if (not isinstance(value, dict) or value.get('schema') != SCHEMA
                or value.get('scope') != _scope(snapshot)
                or not snapshot.get('source_sha256')):return snapshot
        actions = value.get('actions')
        if hashlib.sha256(_encode(actions)).hexdigest() != value.get('actions_sha256'):
            return snapshot
        from yylogs.player_actions import validate_actions
        validate_actions(actions, snapshot['start'], snapshot['end'],
                         {row['id'] for row in snapshot['actors']})
        return {**snapshot, 'actions': actions}
    except (OSError, ValueError, TypeError, KeyError):
        return snapshot


def archive_actions(snapshot, folder, actions):
    """Called only after original session, battle bytes and clock were verified."""
    path = _path(folder)
    if path is None or not snapshot.get('source_sha256'):return False
    from yylogs.player_actions import validate_actions
    validate_actions(actions, snapshot['start'], snapshot['end'],
                     {row['id'] for row in snapshot['actors']})
    value = {'schema': SCHEMA, 'scope': _scope(snapshot), 'actions': actions,
             'actions_sha256': hashlib.sha256(_encode(actions)).hexdigest()}
    raw = _encode(value)
    if len(raw) > MAX_BYTES:raise ValueError('动作副本超过大小限制')
    temporary = path.with_name('.' + NAME + '.' + uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def retain_actions_before_delete(workspace, capture):
    """Save dependent legacy actions before the user's confirmed raw deletion."""
    capture = Path(capture).resolve()
    if '_packet_' not in capture.name:return
    from yylogs.battle_records import _verified_payload
    from yylogs.packet_records import _matching_battle, recover_packet_actions
    store = Path(workspace) / 'logs_research/yylogs_imported/battles'
    _matching_battle.cache_clear()
    for folder in store.glob('battle_*'):
        if folder.is_symlink() or folder.is_junction() or not (folder/'packet_snapshot.json').is_file():
            continue
        _, payload = _verified_payload(folder)
        snapshot = json.loads(payload['packet_snapshot.json'])
        if 'actions' in archived_actions(snapshot, folder):continue
        path = _matching_battle(str(store), snapshot.get('source_sha256'),
                                snapshot.get('session'), snapshot.get('battle_id'))
        if not path or not Path(path).resolve().is_relative_to(capture):continue
        ledger = Path(path).parents[2]
        if not any((ledger/name).is_file() for name in ('action-events.jsonl', 'action-events.jsonl.ylz')):
            continue
        recover_packet_actions(snapshot, folder)
        if 'actions' not in archived_actions(snapshot, folder):
            raise ValueError('关联战斗的动作数据尚未保存，已取消删除原始采集')
