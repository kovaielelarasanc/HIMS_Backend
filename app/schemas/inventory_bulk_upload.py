from __future__ import annotations
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class BulkUploadErrorOut(BaseModel):
    row: int = Field(..., description="Row number in file (header is row 1)")
    code: Optional[str] = None
    column: Optional[str] = None
    message: str


class BulkUploadPreviewOut(BaseModel):
    file_type: str
    total_rows: int
    valid_rows: int
    error_rows: int
    required_columns: List[str]
    optional_columns: List[str]
    sample_rows: List[Dict[str, Any]] = Field(default_factory=list)
    errors: List[BulkUploadErrorOut] = Field(default_factory=list)


class BulkUploadCommitOut(BaseModel):
    created: int
    updated: int
    skipped: int
    errors: List[BulkUploadErrorOut] = Field(default_factory=list)
