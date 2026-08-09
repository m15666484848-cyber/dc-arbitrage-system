"""认证与用户/客户/授权 schemas。"""
from datetime import datetime

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    user_id: int
    username: str
    display_name: str = ""


class UserOut(BaseModel):
    id: int
    username: str
    is_active: bool
    last_login_at: datetime | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class UserCreate(BaseModel):
    username: str
    password: str


class CustomerOut(BaseModel):
    id: int
    username: str
    display_name: str
    is_active: bool
    last_login_at: datetime | None = None
    note: str = ""
    created_at: datetime
    # 防共用控制
    multi_exchange_allowed: bool = False
    max_order_usdt: float = 5000.0
    # 授权状态摘要
    authorized: bool = False
    auth_expires_at: datetime | None = None

    class Config:
        from_attributes = True


class CustomerCreate(BaseModel):
    username: str
    password: str
    display_name: str = ""
    note: str = ""


class CustomerUpdate(BaseModel):
    display_name: str | None = None
    password: str | None = None
    is_active: bool | None = None
    note: str | None = None
    # 防共用控制(管理员可改)
    multi_exchange_allowed: bool | None = None
    max_order_usdt: float | None = None


class AuthorizationOut(BaseModel):
    id: int
    customer_id: int
    exchange: str
    starts_at: datetime
    expires_at: datetime
    active: bool
    note: str = ""
    created_at: datetime

    class Config:
        from_attributes = True


class AuthorizationCreate(BaseModel):
    customer_id: int
    exchange: str = "all"
    starts_at: datetime
    expires_at: datetime
    active: bool = True
    note: str = ""
