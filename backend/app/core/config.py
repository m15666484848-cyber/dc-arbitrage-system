"""应用配置 - 基于 pydantic-settings 从环境变量加载。"""

import warnings

from functools import lru_cache

from typing import Literal



from pydantic import Field, computed_field

from pydantic_settings import BaseSettings, SettingsConfigDict





class Settings(BaseSettings):

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)



    app_env: Literal["development", "production"] = "production"

    app_host: str = "0.0.0.0"

    app_port: int = 8000

    app_name: str = "DC量化跟单系统"



    jwt_secret: str = "change-me"

    jwt_alg: str = "HS256"

    jwt_expire_minutes: int = 1440

    fernet_key: str = "change-me"



    database_url: str = "postgresql+asyncpg://dcquant:dcquant@localhost:5432/dcquant"



    redis_url: str = "redis://localhost:6379/0"



    discord_token: str = ""

    discord_heartbeat_interval: int = 41

    discord_signal_concurrency: int = 10

    discord_process_timeout: int = 120



    ocr_enabled: bool = True

    tesseract_cmd: str = "tesseract"



    llm_enabled: bool = False

    llm_provider: Literal["deepseek", "zhipu", "glm", "siliconflow"] = "deepseek"

    llm_api_key: str = ""

    llm_api_base: str = ""

    llm_model: str = ""

    llm_temperature: float = 0.1

    llm_max_tokens: int = 2000

    llm_timeout: int = 30

    vision_llm_enabled: bool = False

    vision_llm_provider: Literal["deepseek", "zhipu", "glm", "siliconflow"] = "zhipu"

    vision_llm_api_key: str = ""

    vision_llm_api_base: str = ""

    vision_llm_model: str = ""

    vision_llm_temperature: float = 0.1

    vision_llm_max_tokens: int = 2000

    vision_llm_timeout: int = 60



    admin_username: str = "admin"

    admin_password: str = "change-me"



    cors_origins: str = "http://localhost:5173"

    # P2 修复: 默认 TAKER 手续费率,可通过环境变量配置
    default_taker_fee_rate: float = 0.001

    # 架构级能力开关:默认关闭,用于小仓灰度验证后再接管实盘路径
    native_stop_loss_enabled: bool = False
    price_feed_mode: Literal["polling", "websocket"] = "polling"



    @computed_field

    @property

    def cors_list(self) -> list[str]:

        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]



    @property

    def is_dev(self) -> bool:

        return self.app_env == "development"





@lru_cache

def get_settings() -> Settings:

    s = Settings()

    if s.app_env == "production":

        if s.jwt_secret == "change-me":

            raise RuntimeError(

                "SECURITY ERROR: jwt_secret 使用默认值 'change-me', "

                "生产环境必须设置 JWT_SECRET 环境变量!"

            )

        if s.fernet_key == "change-me":

            raise RuntimeError(

                "SECURITY ERROR: fernet_key 使用默认值 'change-me', "

                "生产环境必须设置 FERNET_KEY 环境变量!"

            )

        if s.admin_password in ("change-me", "admin", "password", "123456"):

            raise RuntimeError(

                "SECURITY ERROR: admin_password 使用弱密码, "

                "生产环境必须设置 ADMIN_PASSWORD 环境变量且不能为常见弱密码!"

            )

    return s





settings = get_settings()
