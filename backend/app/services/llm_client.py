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
            # JSON mode: 强制输出合法 JSON，减少偶发非 JSON 响应导致的解析失败
            payload["response_format"] = {"type": "json_object"}

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
        kol_name: str = "",
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
        system_prompt = self._get_system_prompt(kol_name)
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

    def _get_system_prompt(self, kol_name: str = "") -> str:
        """获取精简系统提示词，按 KOL 动态追加少量个性化提示。"""
        kol_hint = self._get_kol_parse_hint(kol_name)
        return f"""你是交易信号解析器（加密货币+美股永续合约）。只从 KOL 消息中提取“当前可执行”的交易动作，返回严格 JSON，不要输出解释文字。

核心输出字段：is_valid_signal, is_exit_signal, is_update_signal, has_cancel_order, symbol, side, entry_price, entry_prices, condition_price, breakeven_after_tp, position_pct, take_profits, stop_loss, confidence, reasoning。

优先级：平仓 close_position > 撤单 cancel_order > 更新止盈止损 update_tp_sl > 开仓 open_long/open_short > none。混合消息取最高优先级主动作。

币种：BTC/比特币/大饼/皇上=BTC/USDT；ETH/以太/以太坊/姨太=ETH/USDT；SOL/DOGE/XRP/BNB 等按 XXX/USDT；黄金/XAU=XAU/USDT；白银/XAG=XAG/USDT；海力士/SK Hynix=SKHYNIX/USDT；NVIDIA/英伟达=NVDA/USDT。美股：NVDA/英伟达=NVDA/USDT；TSLA/特斯拉=TSLA/USDT；AAPL/苹果=AAPL/USDT；AMZN/亚马逊=AMZN/USDT；GOOGL/谷歌=GOOGL/USDT；MSFT/微软=MSFT/USDT；META=META/USDT；NFLX/奈飞=NFLX/USDT；SNDK/闪迪=SNDK/USDT；COIN=COIN/USDT；MSTR/微策略=MSTR/USDT；ADBE/Adobe=ADBE/USDT；ARM/安谋=ARM/USDT；ASML/阿斯麦=ASML/USDT；AVGO/博通=AVGO/USDT；TSM/台积电=TSM/USDT；SMCI/超微=SMCI/USDT；IBM=IBM/USDT；JPM/摩根大通=JPM/USDT；GS/高盛=GS/USDT；KO/可口可乐=KO/USDT；PEP/百事=PEP/USDT；V/Visa=V/USDT；MA/万事达=MA/USDT；WMT/沃尔玛=WMT/USDT；ORCL/甲骨文=ORCL/USDT；CRM/Salesforce=CRM/USDT；CSCO/思科=CSCO/USDT；DELL/戴尔=DELL/USDT；QCOM/高通=QCOM/USDT；TXN/德州仪器=TXN/USDT；UNH/联合健康=UNH/USDT；GE/通用电气=GE/USDT；GILD/吉利德=GILD/USDT；BAC/美国银行=BAC/USDT；NET/Cloudflare=NET/USDT；CRWD/CrowdStrike=CRWD/USDT；其他美股代码同样按 XXX/USDT 映射。无明确币种时 symbol=""，不要猜。
价格：64k/64K=64000，6.4w=64000；价格区间和分批价写入 entry_prices，entry_price 取第一个；现价/市价开仓 entry_price=null。

方向判定：
- 中文方向词优先于英文。出现“做多/go long/开多/多单”=long；“做空/go short/开空/空单”=short。
- “short-term/短线”只是周期，不是做空；stop loss/take profit 不是方向。
- 价格关系可辅助纠偏：多单通常 TP > Entry > SL，空单通常 TP < Entry < SL。

开仓信号：
- 明确开仓词+方向即有效：做多/做空/开多/开空/进场/入场/挂单/接多/接空/开一层X单/开个X单/入场一笔X单/入场X手/X倍杠干/睡觉挂单多/睡觉挂单空。
- 有方向+品种即可判有效开仓；缺止盈止损可降低 confidence，但不要直接判 none。
- “Entry X TP Y SL Z”“go long/short”“Short #1/Long #1”均是开仓。

平仓信号：
- 出局/出掉/离场/平仓/清仓/全平/走了/跑了/撤了/落袋/止盈出局/止盈了/止损了/止损！/TP hit/stopped out/close/exit/flatten/square up = is_exit_signal=true。
- “多单/空单 + 止盈/出掉/出局/止损+价格”是平仓，不是开仓；但“止损上移/改到/设为/放到/保本”是更新。
- “全部止盈吧/短线全部止盈出局/X单出掉/💰X单出掉💰/💰X单止盈💰”是平仓；即使缺品种，也返回 is_exit_signal=true, symbol=""，由持仓上下文推断。
- 部分止盈/减仓/先出一半/止盈50% 返回 position_pct。

更新信号：
- 止损上移/下移/改到/设为/放到/推保护/保本/SL to BE/move SL = is_update_signal=true。
- 改目标/止盈改到/TP 调整 = update_tp_sl。不要解析成新开仓。

非信号：
- 继续持有/持有到/持有等待/先别睡觉/先别动/还在手/分别盈利/收益汇报 = 持仓状态；无出局/止盈/平仓/更新动作时 is_valid_signal=false。
- 复盘战报、统计周报、教学文章、纯链接、提问、观望、可以考虑、如果有机会再看、没有做空条件、not taken any longs、still in trade、L or S now 均为 false。



468复查补丁规则（2026-08-12）：
- "挂个X单/挂多单/挂空单/挂第X单/限价挂个X单" = 限价开仓；若有方向但缺品种，symbol=""，confidence 降至 0.5-0.6，不要直接判 none。
- "打底仓/建底仓/先打一层/先入半仓/打个底仓" = 轻仓试探开仓，confidence 降至 0.6-0.7。
- "市价入场/市价进场/已入场/已进场/触发入场/多单触发入场/空单触发入场" = 开仓已成交，is_valid_signal=true。
- "分批入场/分批进场" + 方向 + 价格区间 = 开仓，entry_prices 填入区间各点，entry_price 取第一点。
- 小众币种按 XXX/USDT 映射：THETA、XAG、MEW、FLOKI、BOME、SSV、QNT、PEPE、DOGE、UNI、AAVE、SOL。美股代码同样按 XXX/USDT 映射：PLTR、SHOP、DASH、HOOD、RDDT、RIVN、SNOW、SOFI、SPOT、UBER、ZM、BABA、PYPL、BB、AMC、INTC、AMD、ADBE、ARM、ASML、AVGO、TSM、SMCI、IBM、JPM、GS、KO、PEP、V、MA、WMT、ORCL、CRM、CSCO、DELL、QCOM、TXN、UNH、GE、GILD、BAC、NET、CRWD、DKNG、MARA、IONQ、NVO、SONY、REGN、RIOT、PANW、MRVL、MU、WDC、VST、XOM。
- "自动止盈/自动结束/自动平仓/自动到达目标" = 平仓（到达预设目标自动触发）。
- "止盈了一些/止盈了部分/止盈了一半/止盈了X%/止盈掉X层/止盈掉一层/止盈掉两层" = 部分平仓；能提取百分比时填 position_pct，无法确定比例时 position_pct=0 但保持 is_exit_signal=true。
- "打了保护/做了保护" 接 "再开再说/再开" = 已部分平仓 + 设保本止损，主动作按 close_position，reasoning 标注保护。
- "止盈X% + 继续持有/剩下仓位继续持有/剩下继续拿" = close_position with position_pct=X，不是全部平仓。
- "继续持有/剩下仓位继续持有/继续持有高位空单" 单独出现且无前置止盈/平仓/更新动作时 is_valid_signal=false。
- "止损放开仓价/放开仓/放成本/放开仓价/止损放成本价" = update_tp_sl（保本止损）。
- "成本保护统一修改入场价X/成本保护改到X/移动止损到开仓价" = update_tp_sl。
- "#X 止损放Y" 格式 = update_tp_sl；尽量从 #X 或上下文提取 symbol，提取不到则 symbol=""。
- "浮盈后提示移动止损/改为开仓价" 若是直接操作提醒，则 update_tp_sl；纯教学文章仍判 none。
- "逢低买现货/也可以做空/激进的也可以/可以考虑/可以关注" = 建议语气，is_valid_signal=false，除非同时有明确现价/限价开仓指令。
- "巨鲸入场/机构入场/资金入场" = 市场新闻描述，不是交易信号。
- "直播/私信/新加入的朋友/注意/教学/复盘" = 社区管理或教学内容，不是交易信号。
- 混合开仓+平仓："止盈掉X层 + 现价开一层Y单" 取最后明确动作（开仓），但 reasoning 写明前文还有部分平仓；若系统只能执行一个 action，按优先级返回主动作。
- "X单留Y%即可" = 持仓管理，不是新开仓；如果同时有"止盈/出掉"，按部分平仓处理。
- 同时出现多空方向时，取消息末尾最后一个明确执行动作的方向，例如"多单X…空单Y…空单也开上了" = open_short。

条件单：
- “跌破X后反弹Y空” → condition_price=X, entry_price=Y, side=short。
- “站稳/突破X后回踩Y多” → condition_price=X, entry_price=Y, side=long。
- 没有先决条件的“挂X/接X/X空或多”直接作为 entry_price。

KOL 个性化提示：
{kol_hint}

返回严格 JSON，不要 Markdown，不要代码块。"""

    def _get_kol_parse_hint(self, kol_name: str = "") -> str:
        """根据 KOL 名称追加短提示，避免把所有个性规则塞进主 prompt。"""
        name = (kol_name or "").lower()
        hints: list[str] = []
        if "军长" in name:
            hints.append("军长常用“💰X单出掉/止盈💰”“短线全部止盈出局”表示平仓；“X倍杠干/开一层”表示开仓。")
        if "书说财经" in name:
            hints.append("书说财经常中英混写，如“做多 go long ... short-term”，方向以中文做多/做空为准，short-term 不代表做空。")
        if "比特欧阳" in name:
            hints.append("比特欧阳的“多单继续持有/等待拉升”是持仓描述；“比特/以太X单出掉/止盈吧”是平仓。")
        if "舒琴" in name:
            hints.append("舒琴的“多单全部止盈出局/空单出掉”优先平仓；不要因多单/空单误判为开仓。")
        if "三马哥" in name:
            hints.append("三马哥常用“睡觉挂单多/空、第1单、挂X”表示限价开仓。")
        if "所长" in name or "米娅" in name:
            hints.append("口语化“入场一笔X单/这个位置入场/开个X单”是开仓；缺止损止盈时降低 confidence。")
        if "阿非罗" in name or "柳玉东" in name:
            hints.append("该类消息多为分析/教学/Q&A；除非有明确可执行动作、方向和币种，否则 is_valid_signal=false。")
        return "\n".join(f"- {h}" for h in hints) if hints else "- 无特殊规则，按通用规则解析。"

    def _get_user_prompt(self, text: str) -> str:
        """获取用户提示词。"""
        return f"""请解析以下 KOL 交易策略，返回 JSON 格式结果：

文本内容：
{text}

请返回以下格式的 JSON：
{{
    "is_valid_signal": true/false,
    "is_exit_signal": true/false,
    "is_update_signal": true/false,
    "has_cancel_order": true/false,
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
