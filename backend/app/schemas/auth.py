"""认证与用户/客户/授权 schemas。"""
from datetime import datetime

from pydantic import BaseModel, Field, field_validator, ConfigDict


class LoginRequest(BaseModel):
    username: str
    password: str


class AuthorizationInfo(BaseModel):
    authorized: bool
    expires_at: datetime | None = None
    exchanges: list[str] = []


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    user_id: int
    username: str
    display_name: str = ""
    authorization: AuthorizationInfo | None = None
    show_signal_summary: bool = False
    emergency_stop: bool = False


class UserOut(BaseModel):
    id: int
    username: str
    is_active: bool
    last_login_at: datetime | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class UserCreate(BaseModel):
    username: str
    password: str


class CustomerOut(BaseModel):
    id: int
    username: str
    display_name: str
    is_active: bool
    status: str = "pending"
    register_source: str = "self"
    reject_reason: str = ""
    last_login_at: datetime | None = None
    note: str = ""
    created_at: datetime
    multi_exchange_allowed: bool = False
    max_order_usdt: float = 5000.0
    show_signal_summary: bool = False
    authorized: bool = False
    auth_expires_at: datetime | None = None
    # 客户分类与邀请系统
    customer_type: str = "normal"
    invite_code: str = ""
    invited_by: int | None = None

    @field_validator("note", "reject_reason", "display_name", "invite_code", "customer_type", mode="before")
    @classmethod
    def handle_none_str(cls, v):
        return v or ""

    model_config = ConfigDict(from_attributes=True)


class CustomerCreate(BaseModel):
    username: str
    password: str
    display_name: str = ""
    note: str = ""


class CustomerRegister(BaseModel):
    """客户自助注册请求 (默认 pending,等待管理员审批)。"""
    username: str
    password: str
    display_name: str = ""
    # 邀请码(可选),有效则设置 invited_by 与 register_source="invite"
    invite_code: str | None = None


class CustomerApprove(BaseModel):
    """管理员审批客户 (激活账号,默认授权所有交易所)。"""
    max_order_usdt: float = 5000.0
    multi_exchange_allowed: bool = False
    note: str = ""


class CustomerReject(BaseModel):
    """管理员拒绝客户注册。"""
    reject_reason: str = ""


class CustomerUpdate(BaseModel):
    status: str | None = None
    display_name: str | None = None
    password: str | None = None
    is_active: bool | None = None
    note: str | None = None
    # 防共用控制(管理员可改)
    multi_exchange_allowed: bool | None = None
    max_order_usdt: float | None = None
    # 页面权限(管理员可改):是否向该客户开放信号汇总
    show_signal_summary: bool | None = None


class AuthorizationOut(BaseModel):
    id: int
    customer_id: int
    exchange: str
    starts_at: datetime
    expires_at: datetime
    active: bool
    note: str = ""
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AuthorizationCreate(BaseModel):
    customer_id: int
    exchange: str = "all"
    starts_at: datetime
    expires_at: datetime
    active: bool = True
    note: str = ""
