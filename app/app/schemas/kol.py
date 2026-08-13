"""KOL schemas。"""
from datetime import datetime

from pydantic import Field, BaseModel, ConfigDict


class KolOut(BaseModel):
    id: int
    name: str
    discord_account_id: int | None = None
    discord_channel_id: str
    discord_user_id: str = ""
    enabled: bool
    avatar: str = ""
    description: str = ""
    # LLM 配置
    llm_enabled: bool = False
    vision_llm_enabled: bool = False
    llm_fallback: bool = True
    llm_min_confidence: float = Field(0.4, ge=0, le=1)
    # 统计
    cached_win_rate: float = 0.0
    cached_pnl: float = 0.0
    cached_signal_count: int = 0
    llm_calls_total: int = 0
    llm_calls_success: int = 0
    llm_tokens_used: int = 0
    followed: bool = False  # 当前客户是否关注(客户视图)
    follow_settings: dict | None = None  # {strategy_id, notional_usdt}
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class KolCreate(BaseModel):
    name: str
    discord_account_id: int | None = None
    discord_channel_id: str
    discord_user_id: str = ""
    enabled: bool = True
    avatar: str = ""
    description: str = ""
    llm_enabled: bool = False
    vision_llm_enabled: bool = False
    llm_fallback: bool = True
    llm_min_confidence: float = 0.4


class KolUpdate(BaseModel):
    name: str | None = None
    discord_account_id: int | None = None
    discord_channel_id: str | None = None
    discord_user_id: str | None = None
    enabled: bool | None = None
    avatar: str | None = None
    description: str | None = None
    llm_enabled: bool | None = None
    vision_llm_enabled: bool | None = None
    llm_fallback: bool | None = None
    llm_min_confidence: float | None = None


class KolFollowItem(BaseModel):
    """单个 KOL 的关注设置。"""
    kol_id: int
    strategy_id: int | None = None
    notional_usdt: float | None = Field(None, ge=0)


class KolFollowUpdate(BaseModel):
    """客户批量设置关注的 KOL(多选/全选),支持每 KOL 独立策略和跟单金额。

    两种提交方式:
    1. 简化版: {kol_ids: [1,2,3], strategy_id?: 5, notional_usdt?: 100} — 所有 KOL 统一设置
    2. 精细版: {kol_settings: [{kol_id:1, strategy_id:5, notional_usdt:200}, ...]} — 每 KOL 独立设置
    """

    kol_ids: list[int] | None = None
    strategy_id: int | None = None
    notional_usdt: float | None = None
    kol_settings: list[KolFollowItem] | None = None
