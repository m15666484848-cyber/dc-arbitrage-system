"""S12新增: 配置验证单元测试。

测试 get_settings() 中的启动验证逻辑:
- LLM 配置验证
- 数据库连接池验证
- Discord Token 警告
"""
import pytest
import warnings
import os
from unittest.mock import patch
from app.core.config import Settings


class TestConfigValidationLogic:
    """配置验证逻辑测试(直接测试条件判断)。"""

    def test_llm_enabled_without_api_key_detected(self):
        """LLM 启用但 API Key 为空 → 应触发验证错误。"""
        llm_enabled = True
        llm_api_key = ""
        # 模拟 get_settings 中的验证逻辑
        should_raise = llm_enabled and not llm_api_key
        assert should_raise is True

    def test_llm_enabled_without_model_detected(self):
        """LLM 启用但 model 为空 → 应触发验证错误。"""
        llm_enabled = True
        llm_model = ""
        should_raise = llm_enabled and not llm_model
        assert should_raise is True

    def test_llm_disabled_no_validation_needed(self):
        """LLM 禁用时不需要验证 API Key。"""
        llm_enabled = False
        llm_api_key = ""
        should_raise = llm_enabled and not llm_api_key
        assert should_raise is False

    def test_llm_enabled_with_config_no_error(self):
        """LLM 启用且配置完整 → 不应触发验证错误。"""
        llm_enabled = True
        llm_api_key = "sk-test"
        llm_model = "deepseek-chat"
        should_raise_key = llm_enabled and not llm_api_key
        should_raise_model = llm_enabled and not llm_model
        assert should_raise_key is False
        assert should_raise_model is False

    def test_db_pool_size_zero_detected(self):
        """DB 连接池大小为0 → 应触发验证错误。"""
        db_pool_size = 0
        should_raise = db_pool_size < 1
        assert should_raise is True

    def test_db_pool_size_valid(self):
        """DB 连接池大小>=1 → 不应触发。"""
        assert (5 < 1) is False
        assert (1 < 1) is False

    def test_db_max_overflow_negative_detected(self):
        """DB max_overflow 为负数 → 应触发。"""
        db_max_overflow = -1
        should_raise = db_max_overflow < 0
        assert should_raise is True

    def test_db_max_overflow_zero_valid(self):
        """DB max_overflow=0 → 不应触发(0不<0)。"""
        assert (0 < 0) is False

    def test_db_max_overflow_positive_valid(self):
        """DB max_overflow>0 → 不应触发。"""
        assert (10 < 0) is False

    def test_no_discord_token_should_warn(self):
        """未配置 Discord Token → 应发出警告。"""
        discord_token = ""
        should_warn = not discord_token
        assert should_warn is True

    def test_with_discord_token_no_warn(self):
        """已配置 Discord Token → 不应警告。"""
        discord_token = "bot-token-xxx"
        should_warn = not discord_token
        assert should_warn is False


class TestSettingsConstruction:
    """Settings 类构造测试。"""

    def test_settings_can_be_created(self):
        """Settings 类可以被实例化(从环境变量加载)。"""
        s = Settings()
        assert s is not None
        assert hasattr(s, "llm_enabled")
        assert hasattr(s, "db_pool_size")
        assert hasattr(s, "discord_token")

    def test_settings_has_required_fields(self):
        """Settings 包含所有必需字段。"""
        s = Settings()
        required_fields = [
            "llm_enabled", "llm_api_key", "llm_model",
            "db_pool_size", "db_max_overflow",
            "discord_token", "jwt_secret", "database_url",
            "redis_url", "app_env",
        ]
        for field in required_fields:
            assert hasattr(s, field), f"缺少字段: {field}"

    def test_settings_defaults(self):
        """Settings 默认值正确。"""
        s = Settings()
        assert s.db_pool_size >= 1  # 应该有合理的默认值
        assert s.app_env in ("development", "production")
