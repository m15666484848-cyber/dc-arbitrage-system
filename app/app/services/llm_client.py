"""LLM 客户端封装 - 支持 DeepSeek V3 和 GLM-4.5-V。"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

from app.core.config import settings

_shared_httpx_client: httpx.AsyncClient | None = None


def get_httpx_client() -> httpx.AsyncClient:
    """获取共享的 httpx 异步客户端(连接池复用)。"""
    global _shared_httpx_client
    if _shared_httpx_client is None:
        _shared_httpx_client = httpx.AsyncClient(
            timeout=httpx.Timeout(60),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )
    return _shared_httpx_client


# 预设配置
PROVIDER_CONFIG = {
    "deepseek": {
        "default_model": "deepseek-chat",
        "api_base": "https://api.deepseek.com/v1",
    },
    "zhipu": {
        "default_model": "glm-4v",
        "api_base": "https://open.bigmodel.cn/api/paas/v4",
    },
    "glm": {
        "default_model": "glm-4v",
        "api_base": "https://open.bigmodel.cn/api/paas/v4",
    },
    "siliconflow": {
        "default_model": "zai-org/GLM-4.5V",
        "api_base": "https://api.siliconflow.cn/v1",
    },
}


@dataclass
class LLMResponse:
    """LLM 响应结果。"""
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""


class LLMClient:
    """LLM 客户端，支持文本和多模态（图片）解析。"""

    def __init__(self, provider: str | None = None, api_key: str | None = None,
                 model: str | None = None, api_base: str | None = None,
                 temperature: float | None = None, max_tokens: int | None = None,
                 timeout: int | None = None, enabled: bool | None = None):
        """
        初始化 LLM 客户端。

        Args:
            provider: 提供商 ("deepseek" 或 "zhipu")
            api_key: API Key
            model: 模型名称（可选）
            api_base: API Base URL（可选）
            temperature/max_tokens/timeout/enabled: 覆盖全局配置
        """
        self.provider = provider or settings.llm_provider
        self.api_key = api_key or settings.llm_api_key
        self.model = model or settings.llm_model or PROVIDER_CONFIG[self.provider]["default_model"]
        self.api_base = api_base or settings.llm_api_base or PROVIDER_CONFIG[self.provider]["api_base"]
        self.timeout = timeout if timeout is not None else settings.llm_timeout
        self.temperature = temperature if temperature is not None else settings.llm_temperature
        self.max_tokens = max_tokens if max_tokens is not None else settings.llm_max_tokens
        self._enabled_override = enabled  # None=用 settings.llm_enabled

    @classmethod
    async def create_from_runtime(cls) -> "LLMClient":
        """从 runtime_config 构造文本客户端(数据库优先,回退 .env)。

        已废弃:请改用 get_text_llm_client() / get_vision_llm_client()。
        """
        from app.core.runtime_config import get_text_llm_settings
        cfg = await get_text_llm_settings()
        return cls(
            provider=cfg.provider,
            api_key=cfg.api_key,
            model=cfg.model,
            api_base=cfg.api_base,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            timeout=cfg.timeout,
            enabled=cfg.enabled,
        )

    @classmethod
    async def create_text_client(cls) -> "LLMClient":
        """构造文本 LLM 客户端(DeepSeek V3,用于信号文本解析)。"""
        from app.core.runtime_config import get_text_llm_settings
        cfg = await get_text_llm_settings()
        return cls(
            provider=cfg.provider,
            api_key=cfg.api_key,
            model=cfg.model,
            api_base=cfg.api_base,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            timeout=cfg.timeout,
            enabled=cfg.enabled,
        )

    @classmethod
    async def create_vision_client(cls) -> "LLMClient":
        """构造图片 LLM 客户端(GLM-4V,用于图片信号解析)。"""
        from app.core.runtime_config import get_vision_llm_settings
        cfg = await get_vision_llm_settings()
        return cls(
            provider=cfg.provider,
            api_key=cfg.api_key,
            model=cfg.model,
            api_base=cfg.api_base,
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            timeout=cfg.timeout,
            enabled=cfg.enabled,
        )

    @property
    def is_available(self) -> bool:
        """检查 LLM 是否可用。"""
        enabled = self._enabled_override if self._enabled_override is not None else settings.llm_enabled
        return bool(self.api_key) and enabled

    async def chat(
        self,
        messages: list[dict[str, Any]],
        image_urls: list[str] | None = None,
        image_base64_list: list[str] | None = None,
    ) -> LLMResponse:
        """
        发送聊天请求（支持多模态）。

        Args:
            messages: 消息列表
            image_urls: 图片 URL 列表（用于多模态模型）
            image_base64_list: 图片 base64 列表（用于多模态模型）

        Returns:
            LLMResponse 对象
        """
        if not self.is_available:
            raise ValueError("LLM 未启用或 API Key 未配置")

        # 构建请求体
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        # 如果有图片，添加到最后一条用户消息
        if image_urls or image_base64_list:
            payload["messages"] = self._add_images_to_messages(messages, image_urls, image_base64_list)

        # 发送请求
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        client = get_httpx_client()
        response = await client.post(
            f"{self.api_base}/chat/completions",
            headers=headers,
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()

        # 解析响应
        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)

        return LLMResponse(
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            model=self.model,
        )

    async def analyze_signal(
        self,
        text: str,
        image_urls: list[str] | None = None,
        image_base64_list: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        分析 KOL 信号文本和图片，返回结构化结果。

        Args:
            text: 信号文本
            image_urls: 图片 URL 列表
            image_base64_list: 图片 base64 列表

        Returns:
            {"result": 结构化信号字典, "usage": token 使用信息}
        """
        system_prompt = self._get_system_prompt()
        user_prompt = self._get_user_prompt(text)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        response = await self.chat(messages, image_urls, image_base64_list)

        # 记录 token 使用情况
        logger.debug(
            f"LLM token 使用: prompt={response.prompt_tokens}, "
            f"completion={response.completion_tokens}, total={response.total_tokens}, "
            f"model={response.model}"
        )

        # 尝试解析 JSON
        try:
            # 清理可能的 markdown 代码块
            clean_text = self._extract_json(response.content)
            result = json.loads(clean_text)
            return {
                "result": result,
                "usage": {
                    "prompt_tokens": response.prompt_tokens,
                    "completion_tokens": response.completion_tokens,
                    "total_tokens": response.total_tokens,
                    "model": response.model,
                }
            }
        except json.JSONDecodeError as e:
            logger.warning(f"LLM 返回内容解析失败: {e}, 原始内容: {response.content[:100]}")
            return {
                "result": {
                    "is_valid_signal": False,
                    "error": f"JSON 解析失败: {response.content[:100]}",
                },
                "usage": {
                    "prompt_tokens": response.prompt_tokens,
                    "completion_tokens": response.completion_tokens,
                    "total_tokens": response.total_tokens,
                    "model": response.model,
                }
            }

    def _get_system_prompt(self) -> str:
        """获取系统提示词。"""
        return """你是一个专业的加密货币交易信号解析器。你的任务是从 KOL 发布的交易策略文本中提取结构化的交易信号。

请严格遵循以下规则：
1. 识别交易品种（如 BTC/USDT、ETH/USDT 等）
2. 判断交易方向（long/多 或 short/空）
3. 提取入场价格（可以是具体价格或价格范围）
4. 提取止盈价格列表（TP1, TP2, ...）
5. 提取止损价格（SL）
6. 如果文本包含"出局"、"离场"、"平仓"等词，识别为平仓信号

注意：
- 如果文本是复盘、分析、假设、公告等，is_valid_signal 设为 false
- 如果是平仓信号，is_exit_signal 设为 true
- 价格如果是范围（如 63000-63500），取中间值作为 entry_price
- 所有价格以 USDT 计价

返回严格的 JSON 格式，不要添加其他文字。"""

    def _get_user_prompt(self, text: str) -> str:
        """获取用户提示词。"""
        return f"""请解析以下 KOL 交易策略，返回 JSON 格式结果：

文本内容：
{text}

请返回以下格式的 JSON：
{{
    "is_valid_signal": true/false,  // 是否为有效交易信号
    "is_exit_signal": true/false,  // 是否为平仓信号
    "symbol": "BTC/USDT",  // 交易品种
    "side": "long" 或 "short",  // 交易方向
    "entry_price": 63000,  // 入场价格
    "take_profits": [65000, 68000],  // 止盈价格列表
    "stop_loss": 62000,  // 止损价格
    "confidence": 0.95,  // 置信度 0-1
    "reasoning": "解析说明"  // 解析过程说明
}}"""

    def _add_images_to_messages(
        self,
        messages: list[dict[str, Any]],
        image_urls: list[str] | None,
        image_base64_list: list[str] | None,
    ) -> list[dict[str, Any]]:
        """将图片添加到最后一条用户消息。"""
        if not image_urls and not image_base64_list:
            return messages

        # 复制消息列表
        new_messages = list(messages)

        # 找到最后一条用户消息
        last_user_msg_idx = -1
        for i, msg in enumerate(new_messages):
            if msg["role"] == "user":
                last_user_msg_idx = i

        if last_user_msg_idx == -1:
            # 如果没有用户消息，添加一条
            content = self._build_image_content("请分析这些图片中的交易策略", image_urls, image_base64_list)
            new_messages.append({"role": "user", "content": content})
        else:
            # 更新最后一条用户消息
            old_content = new_messages[last_user_msg_idx]["content"]
            new_content = self._build_image_content(old_content, image_urls, image_base64_list)
            new_messages[last_user_msg_idx]["content"] = new_content

        return new_messages

    def _build_image_content(
        self,
        text: str,
        image_urls: list[str] | None,
        image_base64_list: list[str] | None,
    ) -> list[dict[str, Any]]:
        """构建包含图片的 content 列表。"""
        content = []
        content.append({"type": "text", "text": text})

        # 添加图片 URL
        if image_urls:
            for url in image_urls:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": url},
                })

        # 添加 base64 图片
        if image_base64_list:
            for b64 in image_base64_list:
                # 假设是 JPEG 格式，实际使用时可能需要根据实际格式调整
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                })

        return content

    def _extract_json(self, text: str) -> str:
        """从 LLM 返回中提取 JSON,使用括号配对算法精确定位。"""
        text = text.strip()

        if text.startswith("```"):
            lines = text.split("\n")
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)

        json_start = text.find("{")
        if json_start == -1:
            raise ValueError("未找到 JSON 对象")

        depth = 0
        json_end = -1
        for i in range(json_start, len(text)):
            c = text[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    json_end = i
                    break

        if json_end == -1:
            raise ValueError("未找到完整 JSON 对象(括号不匹配)")

        return text[json_start:json_end + 1]


# 全局 LLM 客户端实例(仅用于向后兼容,新代码请用 get_llm_client 异步版)
_llm_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """获取全局 LLM 客户端实例(同步,从 .env 读)。

    已废弃:请改用 await get_llm_client_async() 以使用数据库配置。
    """
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client


async def get_llm_client_async() -> LLMClient:
    """获取文本 LLM 客户端实例(异步,从 runtime_config 读最新配置)。

    每次调用都读最新配置,确保管理页修改后立即生效。
    已废弃:请改用 get_text_llm_client()。
    """
    return await LLMClient.create_text_client()


async def get_text_llm_client() -> LLMClient:
    """获取文本 LLM 客户端(DeepSeek V3,用于信号文本解析)。"""
    return await LLMClient.create_text_client()


async def get_vision_llm_client() -> LLMClient:
    """获取图片 LLM 客户端(GLM-4V,用于图片信号解析)。

    若 vision_llm 未启用或未配 key,返回的 client.is_available 为 False。
    """
    return await LLMClient.create_vision_client()


def reset_llm_client() -> None:
    """重置全局 LLM 客户端实例（用于配置变更后）。"""
    global _llm_client
    _llm_client = None