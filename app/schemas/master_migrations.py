# app/schemas/master_migrations.py
from __future__ import annotations
from typing import List, Optional, Union, Annotated, Literal, Dict, Any
from pydantic import BaseModel, Field, ConfigDict
from datetime import datetime

class ColumnSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    type: str
    nullable: bool = True
    default: Optional[str] = None      # allowed: NULL, CURRENT_TIMESTAMP, numbers, or quoted strings like 'abc'
    comment: Optional[str] = None
    after: Optional[str] = None

class OpCreateDatabase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["create_database"]
    db_name: str

class OpDropDatabase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["drop_database"]
    db_name: str

class OpCreateTable(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["create_table"]
    table: str
    columns: List[ColumnSpec]

class OpDropTable(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["drop_table"]
    table: str

class OpAddColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["add_column"]
    table: str
    column: ColumnSpec

class OpModifyColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["modify_column"]
    table: str
    column: ColumnSpec

class OpDropColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["drop_column"]
    table: str
    column_name: str

class OpRenameColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["rename_column"]
    table: str
    old_column_name: str
    new_column_name: str

MigrationOp = Annotated[
    Union[
        OpCreateDatabase,
        OpDropDatabase,
        OpCreateTable,
        OpDropTable,
        OpAddColumn,
        OpModifyColumn,
        OpDropColumn,
        OpRenameColumn,
    ],
    Field(discriminator="op")
]

class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = "Migration"
    description: Optional[str] = None

    apply_all: bool = False
    tenant_ids: List[int] = Field(default_factory=list)

    dry_run: bool = True
    allow_destructive: bool = False
    confirm_phrase: Optional[str] = None

    client_request_id: Optional[str] = None
    ops: List[MigrationOp]

class TargetSql(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: int
    db_name: str
    sql: List[str]

class PlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    destructive_detected: bool
    global_sql: List[str] = Field(default_factory=list)  # create/drop db etc (run once)
    targets: List[TargetSql]

class ApplyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: int

class JobRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    name: str
    status: str
    created_at: str

class JobTargetRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: int
    status: str
    started_at: Optional[datetime] = None   # ✅ FIX
    finished_at: Optional[datetime] = None
    error: Optional[str] = None

class JobDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job: Dict[str, Any]
    targets: List[JobTargetRow]
