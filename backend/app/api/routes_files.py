# app/api/routes_files.py
from __future__ import annotations
from pathlib import Path
from datetime import datetime
import shutil, uuid

from fastapi import APIRouter, UploadFile, File, HTTPException
from app.core.config import settings

router = APIRouter()

ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".pdf", ".csv", ".txt"}


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    root = Path(settings.STORAGE_DIR).resolve()
    date_dir = datetime.utcnow().strftime("uploads/%Y/%m/%d")
    dest_dir = root / date_dir
    dest_dir.mkdir(parents=True, exist_ok=True)

    ext = (Path(file.filename).suffix or "").lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"Unsupported file type: {ext}")

    name = f"{uuid.uuid4().hex}{ext}"
    dest = dest_dir / name
    with dest.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    rel_posix = f"{date_dir}/{name}"
    file_url_rel = f"{settings.MEDIA_URL.rstrip('/')}/{rel_posix}"
    file_url_abs = f"{settings.SITE_URL.rstrip('/')}{file_url_rel}"

    return {
        "file_url": file_url_rel,  # preferred for HTML (has /media prefix)
        "file_url_abs": file_url_abs,  # absolute (if you need)
        "original_name": file.filename,
        "size": dest.stat().st_size,
        "ext": ext,
    }


# from __future__ import annotations
# from fastapi import APIRouter, UploadFile, File, HTTPException
# from pathlib import Path
# from datetime import datetime
# import secrets
# from app.api.deps import get_db, current_user
# from app.core.config import settings
# from app.models.user import User
# from fastapi import APIRouter, Depends
# import uuid, shutil

# MEDIA_ROOT = Path("./media").resolve()

# @router.post("/upload")
# async def upload_file(file: UploadFile = File(...)):
#     ext = Path(file.filename or "").suffix.lower()
#     if ext not in ALLOWED_EXT:
#         raise HTTPException(status_code=400,
#                             detail=f"Unsupported file type: {ext}")

#     # folders /media/uploads/YYYY/MM/DD/<random>.<ext>
#     subdir = datetime.utcnow().strftime("uploads/%Y/%m/%d")
#     dest_dir = MEDIA_ROOT / subdir
#     dest_dir.mkdir(parents=True, exist_ok=True)

#     rand = secrets.token_hex(8)
#     dest_path = dest_dir / f"{rand}{ext}"

#     data = await file.read()
#     with dest_path.open("wb") as f:
#         f.write(data)

#     rel = dest_path.relative_to(MEDIA_ROOT).as_posix()  # uploads/....
#     file_url = f"/media/{rel}"
#     return {
#         "file_url": file_url,
#         "original_name": file.filename,
#         "size": len(data),
#         "ext": ext,
#     }
