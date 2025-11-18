from pydantic import BaseModel
from typing import List


class RoleBase(BaseModel):
    name: str
    description: str | None = None


class RoleCreate(RoleBase):
    permission_ids: List[int] = []


class RoleOut(RoleBase):
    id: int
    permission_ids: List[int]
    class Config:
        from_attributes = True