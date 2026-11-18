"""Local ownership of per-battle ledgers; no game or network access."""
import hashlib
import json
from pathlib import Path
import re
import shutil

LEDGERS = ('damage-events', 'buff-events', 'action-events', 'encounter-events',
           'timer-events', 'settlement-details', 'statistics')
MAX_BYTES = 256 * 1024 * 1024


def _plain(path):
    if path.is_symlink() or path.is_junction():
        raise ValueError('独立战斗日志不能包含链接')


def event_folder(battle_path):
    """Old captures keep their original session layout; new captures opt in."""
    battle_path = Path(battle_path)
    layout = battle_path.with_name('event-layout.json')
    if not layout.exists():
        return battle_path.parents[2]
    for item in (battle_path.parent, layout, battle_path.parent / 'events'):
        _plain(item)
    if layout.stat().st_size > 4096:
        raise ValueError('独立战斗日志布局无效')
    value = json.loads(layout.read_text('utf-8-sig'))
    battle = json.loads(battle_path.read_text('utf-8-sig'))
    if (value.get('schema') != 'yylogs.battle-events/1' or value.get('battle') != battle.get('id')
            or value.get('session') != (battle.get('source') or {}).get('sessionId')):
        raise ValueError('独立战斗日志场次不匹配')
    return battle_path.parent / 'events'


def is_independent(battle_path):
    return event_folder(battle_path) == Path(battle_path).parent / 'events'


def _source_files(descriptor):
    source = Path(descriptor['path'])
    _plain(source); _plain(source.parent)
    if source.name != 'battle.json' or source.parent.parent.name != 'battles':
        raise ValueError('独立战斗日志来源路径无效')
    source = source.resolve()
    folder = event_folder(source)
    if folder != source.parent / 'events':
        raise ValueError('仅归档独立战斗日志')
    _plain(folder)
    layout = json.loads(source.with_name('event-layout.json').read_text('utf-8-sig'))
    if layout.get('sealed') is not True and not (source.parents[2] / 'completion.json').is_file():
        raise ValueError('独立战斗日志仍在写入，不能归档')
    expected = descriptor.get('limits')
    allowed = {name + suffix for name in LEDGERS for suffix in ('.jsonl', '.jsonl.ylz')}
    files = {p.name: p.stat().st_size for p in folder.iterdir()} if expected is None else expected
    if not isinstance(files, dict) or set(files) - allowed:
        raise ValueError('独立战斗日志文件无效')
    total = 0
    for name, size in files.items():
        p = folder / name
        _plain(p)
        if type(size) is not int or not 0 <= size <= MAX_BYTES or p.stat().st_size != size:
            raise ValueError('独立战斗日志尚未封存或大小无效')
        total += size
    if total > 7 * MAX_BYTES:
        raise ValueError('独立战斗日志超过大小限制')
    return source, folder, files


def copy_logs(descriptor, destination):
    """Copy into the unpublished record. Keep source intact until publication."""
    source, events, files = _source_files(descriptor)
    destination = Path(destination)
    destination.mkdir()
    target = destination / 'events'
    target.mkdir()
    for name in files:
        shutil.copyfile(events / name, target / name)
    raw = descriptor.get('battle_bytes') or source.read_bytes()
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError('独立战斗元数据过大')
    (destination / 'battle.json').write_bytes(raw)
    shutil.copyfile(source.with_name('event-layout.json'), destination / 'event-layout.json')
    (destination / 'identities.json').write_text(json.dumps(
        {'names': descriptor.get('identities', [])}, ensure_ascii=False), 'utf8')
    checksums = {}
    for p in destination.rglob('*'):
        if p.is_file():
            with p.open('rb') as stream:
                checksums[str(p.relative_to(destination)).replace('\\', '/')] = hashlib.file_digest(stream, 'sha256').hexdigest()
    (destination / 'manifest.json').write_text(json.dumps(
        {'schema': 'yylogs.battle-log-bundle/1', 'files': checksums}, sort_keys=True), 'utf8')


def finish_transfer(descriptor, record_folder):
    """Replace the capture's raw copy with a small receipt, never another fight."""
    from yylogs.local_files import atomic_json
    source = Path(descriptor['path'])
    record_folder = Path(record_folder)
    logs = record_folder / 'packet_logs'
    _plain(record_folder); _plain(logs)
    if not (logs / 'manifest.json').is_file():
        raise ValueError('独立战斗日志归档未完成')
    # This receipt prevents a later session import from resurrecting a deleted
    # record. It contains no combat payload and no arbitrary deletion path.
    atomic_json(source.with_name('record-link.json'), {'schema': 'yylogs.battle-log-receipt/1',
        'battle': source.parent.name, 'record_id': record_folder.name})
    events = source.parent / 'events'
    if events.exists():
        _plain(events)
        if events.resolve().parent != source.parent.resolve():
            raise ValueError('独立战斗日志清理路径无效')
        for p in events.iterdir():
            _plain(p)
            if not p.is_file():
                raise ValueError('独立战斗日志含意外目录')
        shutil.rmtree(events)
    for name in ('battle.json', 'saved-snapshot.json', 'event-layout.json'):
        p = source.with_name(name)
        _plain(p)
        p.unlink(missing_ok=True)


def linked_records(root, store):
    """Only return receipts whose actual local record still exists."""
    root, store = Path(root).resolve(), Path(store).resolve()
    for p in root.glob('sessions/*/capture/battles/*/record-link.json'):
        _plain(p)
        if not p.resolve().is_relative_to(root) or p.stat().st_size > 4096:
            raise ValueError('独立战斗日志回执无效')
        v = json.loads(p.read_text('utf-8-sig'))
        record_id = v.get('record_id')
        if (v.get('schema') != 'yylogs.battle-log-receipt/1' or v.get('battle') != p.parent.name
                or not isinstance(record_id, str) or not re.fullmatch(r'battle_[a-f0-9]{24}', record_id)):
            raise ValueError('独立战斗日志回执身份无效')
        folder = store / record_id
        _plain(folder)
        if folder.is_dir():
            yield record_id
