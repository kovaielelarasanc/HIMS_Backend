# app/utils/files.py
import os, uuid, mimetypes
from pathlib import Path
from datetime import datetime
from fastapi import UploadFile

UPLOAD_ROOT = Path("uploads")


def save_upload(file: UploadFile, subdir: str) -> dict:
    ext = Path(file.filename).suffix or ""
    basename = f"{uuid.uuid4().hex}{ext}"
    dated = datetime.utcnow().strftime("%Y/%m/%d")
    folder = UPLOAD_ROOT / subdir / dated
    folder.mkdir(parents=True, exist_ok=True)
    stored_path = folder / basename
    with stored_path.open("wb") as f:
        f.write(file.file.read())
    content_type = mimetypes.guess_type(
        str(stored_path))[0] or file.content_type or "application/octet-stream"
    public_url = f"/files/{subdir}/{dated}/{basename}"
    size_bytes = stored_path.stat().st_size
    return {
        "stored_path": str(stored_path),
        "public_url": public_url,
        "content_type": content_type,
        "size_bytes": size_bytes,
        "filename": file.filename,
    }
