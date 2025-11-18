# app/schemas/files.py
from pydantic import BaseModel


class FileUploadOut(BaseModel):
    url: str
    filename: str
    content_type: str
    size: int
