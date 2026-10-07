from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Any


def rollback_rollout(journal: dict[str, Any]) -> dict[str, Any]:
    target = Path(journal["target"])
    backup = Path(journal["backup"])
    if hashlib.sha256(target.read_bytes()).hexdigest().upper() != journal["post_sha256"]:
        raise ValueError("target changed after migration; refusing whole-file rollback")
    if hashlib.sha256(backup.read_bytes()).hexdigest().upper() != journal["pre_sha256"]:
        raise ValueError("backup hash mismatch")
    temporary = target.with_name(target.name + ".rollback.tmp")
    shutil.copy2(backup, temporary)
    os.replace(temporary, target)
    return {"restored_sha256": journal["pre_sha256"], "target": str(target)}
