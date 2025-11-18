from pydantic import BaseModel


class PermissionBase(BaseModel):
    code: str
    label: str
    module: str


class PermissionCreate(PermissionBase):
    pass


class PermissionOut(PermissionBase):
    id: int
    class Config:
        from_attributes = True