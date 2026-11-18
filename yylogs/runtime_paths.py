"""Separate installed application files from writable per-user records."""
import json
import os
from pathlib import Path


def default_workspace():
    directory = 'YYLOGS-Offline'
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local')) / directory


def configured_workspace():
    settings = default_workspace() / 'storage.json'
    if not settings.is_file():return None
    value = json.loads(settings.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict) or value.get('schema') != 'yylogs.storage/1':
        raise ValueError('Invalid data directory settings')
    root = value.get('root')
    if root is None:return None
    if not isinstance(root, str) or not root.strip() or not Path(root).is_absolute():
        raise ValueError('Invalid data directory path')
    return Path(root).resolve()


def save_workspace(root):
    """Save a next-launch preference; never move or delete existing records."""
    import tempfile
    destination = Path(root) if root is not None else default_workspace()
    if not destination.is_absolute():raise ValueError('请选择绝对路径')
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    # Check write access before committing the preference, including ACL errors.
    with tempfile.TemporaryFile(dir=destination) as probe:
        probe.write(b'yylogs');probe.flush()
    settings = default_workspace() / 'storage.json'
    settings.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                dir=settings.parent, prefix='.storage-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({'schema': 'yylogs.storage/1',
                       'root': str(destination) if root is not None else None}, stream, ensure_ascii=False)
        temporary.replace(settings)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)
    return destination


def user_workspace():
    root = configured_workspace() or default_workspace()
    (root / "logs_research").mkdir(parents=True, exist_ok=True)
    return root.resolve()
