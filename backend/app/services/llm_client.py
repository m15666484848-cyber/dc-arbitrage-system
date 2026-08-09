"""LLM 客户端封装 - 支持 DeepSeek V3 和 GLM-4.5-V。"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger

from app.core.config import settings

# SSRF 防护: 图片下载白名单域名
_ALLOWED_IMAGE_DOMAINS = {
    "cdn.discordapp.com", "media.discordapp.net",
    "images-ext-1.discordapp.net", "images-ext-2.discordapp.net",
}

# 图片下载大小上限 (10MB)
_MAX_IMAGE_SIZE = 10 * 1024 * 1024


def _is_safe_image_url(url: str) -> bool:
    """检查 URL 是否在允许的域名白名单内且使用 http/https 协议。"""
    try:
        parsed = urlparse(url)
        return parsed.hostname in _ALLOWED_IMAGE_DOMAINS and parsed.scheme in ("https", "http")
    except Exception:
        return False

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


# ★ P1 修复: 添加 close_httpx_client() 函数,在应用关闭时清理 httpx 客户端
async def close_httpx_client() -> None:
    """关闭共享的 httpx 异步客户端,释放连接池资源。

    应在应用关闭时(lifespan shutdown)调用,避免连接泄漏。
    """
    global _shared_httpx_client
    if _shared_httpx_client is not None:
        try:
            await _shared_httpx_client.aclose()
            logger.info("httpx 共享客户端已关闭")
        except Exception as e:
            logger.warning(f"关闭 httpx 客户端时出错: {e}")
        finally:
            _shared_httpx_client = None


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
        _provider_cfg = PROVIDER_CONFIG.get(self.provider, {})
        self.model = model or settings.llm_model or _provider_cfg.get("default_model", "")
        self.api_base = api_base or settings.llm_api_base or _provider_cfg.get("api_base", "")
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

    async def _download_urls_to_base64(self, image_urls: list[str]) -> list[str]:
        """将图片 URL 列表下载并转为 base64 编码。

        SiliconFlow / GLM-4.5V 等视觉 LLM API 无法直接访问 Discord CDN 等私有 URL,
        必须先将图片下载到本地,转成 base64 data URL 再发送给 API。

        Args:
            image_urls: 图片 URL 列表

        Returns:
            base64 编码的图片列表(不含 data: 前缀)
        """
        base64_list = []
        client = get_httpx_client()
        for url in image_urls:
            try:
                # SSRF 防护: 校验 URL 域名白名单
                if not _is_safe_image_url(url):
                    logger.warning(f"图片 URL 不在白名单内,拒绝下载: {url[:80]}")
                    continue
                resp = await client.get(url, timeout=20)
                resp.raise_for_status()
                # 图片大小限制: 超过 10MB 拒绝
                content_length = resp.headers.get("content-length")
                if content_length and int(content_length) > _MAX_IMAGE_SIZE:
                    logger.warning(f"图片过大(Content-Length={content_length}),跳过: {url[:80]}")
                    continue
                if len(resp.content) > _MAX_IMAGE_SIZE:
                    logger.warning(f"图片过大({len(resp.content)} bytes),跳过: {url[:80]}")
                    continue
                b64 = base64.b64encode(resp.content).decode("utf-8")
                base64_list.append(b64)
                logger.debug(f"图片下载成功: {url[:80]}... ({len(resp.content)} bytes)")
            except Exception as e:
                logger.warning(f"图片下载失败: {url[:80]}... -> {e}")
        return base64_list

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
            image_urls: 图片 URL 列表（用于多模态模型，会自动下载转 base64）
            image_base64_list: 图片 base64 列表

        Returns:
            LLMResponse 对象
        """
        if not self.is_available:
            raise ValueError("LLM 未启用或 API Key 未配置")

        # 视觉 LLM API(如 SiliconFlow/GLM-4.5V)无法直接访问 Discord CDN 等 URL,
        # 必须先将图片 URL 下载转 base64 再发送。
        all_base64 = list(image_base64_list or [])
        if image_urls:
            downloaded_b64 = await self._download_urls_to_base64(image_urls)
            all_base64.extend(downloaded_b64)

        # 构建请求体
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        # 如果有图片(全部已转为 base64），添加到最后一条用户消息
        if all_base64:
            payload["messages"] = self._add_images_to_messages(messages, None, all_base64)

        # DeepSeek: disable Thinking mode for fast signal parsing (15-30s -> 3-8s)
        if self.provider == "deepseek":
            payload["thinking"] = {"type": "disabled"}

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
        choices = data.get("choices") or []
        if not choices:
            raise ValueError(f"API返回无choices: {data}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if content is None:
            raise ValueError(f"API返回无content: {data}")
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
        except (json.JSONDecodeError, ValueError) as e:
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

## 币种识别
BTC/比特币/大饼/皇上 → BTC/USDT
ETH/以太/以太坊 → ETH/USDT
SOL/索尔 → SOL/USDT
DOGE/狗狗 → DOGE/USDT
其他代币: XXX → XXX/USDT
如果消息中没有明确币种名称或代号，symbol设为空 → 不要猜测

## 价格格式转换
- 64k → 64000, 6.4w → 64000, 64K → 64000
- "64000附近/一线/位置" → 64000
- "64000-63500附近" → 范围价，取中间值63750作为entry_price

## 解析规则

### 开仓识别
1. 开仓关键词：接/开/进/多/空/做多/做空/挂单/委托/埋伏/抄底/摸顶/上车/搞/干/操作/布局/进场/入场/建仓
2. 平仓关键词：跑了/平了/出了/不格局了/落袋/落袋为安/跑路/撤退/撤/止盈了/止损了/平仓/全平/清仓/走人/下车/出局/兑现/获利了结
3. "拿着/持有/继续拿着/不用管/继续格局/保持仓位" = 继续持有，不是新开仓 → is_valid_signal=false
4. "关注/观察/留意/看看/等待/等一等/不急/再看看" = 观望，不是即时交易 → is_valid_signal=false
5. "想试试/咱就开" = 明确开仓意图 → is_valid_signal=true

### 入场价格
- 有明确入场价（如"64000接一手"）→ entry_price=64000
- 无明确入场价（如"现价做空/直接开空/市价空/咱就开个空单"）→ entry_price=null
- "现价63600做多"中的63600是描述当前市场价，不是挂单价 → entry_price=null

### 条件触发与两阶段入场
当信号包含"如果...才..."、"先...再..."、"跌破...反弹..."、"涨到...回踩..."等条件语句时：
- "如果在63750附近反弹 在64300附近 空" → condition_price=63750, entry_price=64300
- "跌破65000以后 反弹到65500空" → condition_price=65000, entry_price=65500
- 普通限价单（无前提条件）: condition_price=null
- condition_price不为null时，系统会先监控条件价，触及后才入场
- 条件触发单语义：先触及 condition_price，再等待 entry_price 入场；不要把条件价误当入场价
- “跌破/插针到/杀到/踩到/回踩到 X 后反弹 Y 空” → condition_price=X, entry_price=Y, side=short
- “涨破/冲到/打到/站上 X 后回踩 Y 多” → condition_price=X, entry_price=Y, side=long
- 如果只有“挂 X”“X 接”“X 空/多”，没有先决条件，则 condition_price=null, entry_price=X

### 分批建仓与仓位
- 多个入场价如“64000/63500/63000 分批接” → entry_prices=[64000,63500,63000], entry_price取第一个入场价
- “半仓/五成仓” → position_pct=50；“三成/轻仓” → position_pct=30；“重仓/七成” → position_pct=70
- “减半/出一半/先出30%”属于部分平仓信号，is_exit_signal=true，并在 position_pct 中返回平仓比例

### TP1 后保本移动
- “TP1后保本/到一止盈后保本/先止盈一半剩下保本/止盈后止损推到成本” → breakeven_after_tp=entry_price
- “TP1后止损推到 X / 到第一目标后保护 X / 保本价 X” → breakeven_after_tp=X
- 未提及 TP 后保本规则 → breakeven_after_tp=null

### 止盈止损
- 止盈关键词：目标/看/压力位/止盈/减仓位/出局位/落袋位
- 止损关键词：止损/破位/跌破/突破/防守位/保护位/底线/不能破/破了走/站稳
- "X级别站稳算破" → stop_loss为该价格
- "止损：小时级别站稳这个M头 站稳65100" → stop_loss=65100
- 多个止盈：止盈：64000-63650-63100-62850 → take_profits=[64000,63650,63100,62850]
- 如果没有提到止损 → stop_loss=null（系统会自动补设）
- 如果没有提到止盈 → take_profits=[]（系统会自动补设）

### 平仓信号识别（非常重要）
如果文本包含以下表达，识别为平仓信号(is_exit_signal=true, is_valid_signal=true)：
a) 明确平仓词："出局"、"离场"、"平仓"、"平多"、"平空"、"全平"、"清仓"、"获利了结"
b) 口语化平仓："先走吧"、"差不多了可以出了"、"先撤了"、"落袋为安"、"可以跑了"、
   "不玩了"、"走人"、"撤了"、"出了"、"跑了"、"抛了"、"卖了"
c) 上下文暗示平仓：短消息(<50字)包含"走/出/撤/跑/抛/收/关"等动词，
   且不包含入场价、方向、止盈止损等开仓要素

### 非交易内容（is_valid_signal=false）
- 复盘/分析："顺利空到1890"、"昨夜重头戏"、"吃了个短线反弹"
- 市场观望建议："观望观望"、"到时候再看"、"求稳可以不做"
- 宣传/推广内容："每天实时操作"、"机会来了赚钱就是这么快"
- 纯链接/URL、纯表情/emoji
- "反复震荡都不知道该不该做了" = 犹豫不决，非明确交易指令 → is_valid_signal=false
- 举例式描述：含"比如"/"例如" + 无明确即时入场指令

### 黑话/俚语
- "接一手/开一单/搞一波/想试试/咱就开" = 开仓
- "跑了/平了/出了/不格局了/落袋" = 平仓
- "开孔/开空" = 做空
- "抄底/摸顶" = 分别做多/做空
- "上车/下车" = 开仓/平仓
- "格局" = 持有（"继续格局"=持有, "不格局了"=平仓）
- "反手" = 平掉当前仓位反方向开仓
- "大饼/皇上" = BTC；"姨太/二饼" = ETH；"山寨"需要结合具体代币，不明确则 symbol 为空
- "防守/保护/底线"通常是 stop_loss；"压力/压制/上方目标"常用于空单入场或止盈；"支撑/下方接"常用于多单入场
- "站稳/有效突破/收上去"偏向向上确认；"跌破/破位/收下去"偏向向下确认
- "插针/打针/扫到/踩到"表示价格触及某个价位，可作为条件价或入场价，按上下文判断

## 重要原则
- 只解析明确的交易指令，不含糊的观望/分析/感慨归为无效
- 没有明确价格的"做多/做空/开空" → 仍然解析为有效信号，entry_price=null
- "反弹以后考虑继续开空" = 观望建议，不是即时信号 → is_valid_signal=false
- 对KOL消息要有判断力：分析市场观点≠交易指令
- 所有价格以 USDT 计价

返回严格的 JSON 格式，不要添加其他文字。"""

    def _get_user_prompt(self, text: str) -> str:
        """获取用户提示词。"""
        return f"""请解析以下 KOL 交易策略，返回 JSON 格式结果：

文本内容：
{text}

请返回以下格式的 JSON：
{{
    "is_valid_signal": true/false,
    "is_exit_signal": true/false,
    "symbol": "BTC/USDT",
    "side": "long" 或 "short",
    "entry_price": 63000,
    "entry_prices": [],
    "condition_price": null,
    "breakeven_after_tp": null,
    "position_pct": 0,
    "take_profits": [65000, 68000],
    "stop_loss": 62000,
    "confidence": 0.95,
    "reasoning": "解析说明"
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

    @staticmethod
    def _image_base64_to_data_url(b64: str) -> str:
        """根据图片魔数识别 MIME,构造 data URL。"""
        if b64.startswith("data:image/"):
            return b64
        try:
            raw = base64.b64decode(b64[:128], validate=False)
        except Exception:
            raw = b""
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif raw.startswith(b"GIF87a") or raw.startswith(b"GIF89a"):
            mime = "image/gif"
        elif raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
            mime = "image/webp"
        elif raw.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        else:
            mime = "image/jpeg"
        return f"data:{mime};base64,{b64}"

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
                content.append({
                    "type": "image_url",
                    "image_url": {"url": self._image_base64_to_data_url(b64)},
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

        start = text.find("{")
        if start == -1:
            raise ValueError("未找到 JSON 对象")

        depth = 0
        end = -1
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == '\\':
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break

        if end == -1:
            raise ValueError("未找到完整 JSON 对象(括号不匹配)")

        return text[start:end]


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
