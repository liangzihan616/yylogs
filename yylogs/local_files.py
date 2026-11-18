import json
import os
import time
from pathlib import Path
from uuid import uuid4


def read_json(path):
    path=Path(path)
    if not path.exists():return None
    if path.stat().st_size>2*1024*1024:raise ValueError("capture status file too large")
    return json.loads(path.read_text(encoding="utf-8-sig"))

def atomic_json(path,value):
    path=Path(path)
    temporary=path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x",encoding="utf-8") as stream:
            json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False)
            stream.flush();os.fsync(stream.fileno())
        for attempt,delay in enumerate((0.05,0.1,0.2,0.3)):
            try:
                os.replace(temporary,path)
                break
            except OSError as exc:
                if getattr(exc,"winerror",None) not in (5,32) or attempt==3:raise
                time.sleep(delay)
    finally:
        temporary.unlink(missing_ok=True)
