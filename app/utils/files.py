from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile, HTTPException

from app.core.config import settings

_safe_module = re.compile(r"^[a-zA-Z0-9_\-]+$")


def save_upload(file: UploadFile, module: str) -> dict:
    """
    Saves file inside: {STORAGE_DIR}/{module}/YYYY/MM/DD/<uuid>.<ext>
    Returns public_url like: {MEDIA_URL}/{module}/YYYY/MM/DD/<uuid>.<ext>
    Example: /files/ris/2025/12/14/abc.csv
    """
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="Invalid file")

    module = (module or "").strip().lower()
    if not _safe_module.match(module):
        raise HTTPException(status_code=400, detail="Invalid module path")

    now = datetime.utcnow()
    y, m, d = now.strftime("%Y"), now.strftime("%m"), now.strftime("%d")

    ext = Path(file.filename).suffix.lower()
    fname = f"{uuid4().hex}{ext}"  # keep extension

    rel_dir = Path(module) / y / m / d
    disk_dir = Path(settings.STORAGE_DIR).resolve() / rel_dir
    disk_dir.mkdir(parents=True, exist_ok=True)

    disk_path = disk_dir / fname

    try:
        with disk_path.open("wb") as out:
            shutil.copyfileobj(file.file, out)
    finally:
        try:
            file.file.close()
        except Exception:
            pass

    public_url = f"{settings.MEDIA_URL}/{rel_dir.as_posix()}/{fname}"

    return {
        "public_url": public_url,
        "relative_path": f"{rel_dir.as_posix()}/{fname}",
        "disk_path": str(disk_path),
        "original_name": file.filename,
        "content_type": file.content_type,
        "size": disk_path.stat().st_size if disk_path.exists() else None,
    }
