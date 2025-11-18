from pydantic import BaseModel, EmailStr


class RegisterAdminIn(BaseModel):
    name: str
    email: EmailStr
    password: str
    confirm_password: str


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class OtpVerifyIn(BaseModel):
    email: EmailStr
    otp: str


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"