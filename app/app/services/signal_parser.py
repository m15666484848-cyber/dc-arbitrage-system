"""信号解析服务:从 Discord 文本/图片消息提取结构化交易信号。

支持中英文 KOL 常见格式,提取:符号、方向、入场价(可分批)、多级止盈、止损、杠杆、仓位%。
对图片走 Tesseract OCR 后复用同一解析管线。
规则解析失败时自动降级到 LLM 解析（可选，按 KOL 配置触发）。
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger
from pydantic import BaseModel

from app.core.config import settings
from app.schemas.signal import ParsedSignal

# LLM 解析器（可选导入）
try:
    from app.services.signal_llm_parser import parse_with_llm, parse_image_with_llm
    _LLM_AVAILABLE = True
except ImportError:
    _LLM_AVAILABLE = False
    logger.debug("LLM 解析器不可用，将仅使用规则解析")


@dataclass
class KolLLMConfig:
    """KOL 级别 LLM 配置（覆盖全局设置）。"""
    enabled: bool = False  # 是否启用 LLM（覆盖全局）
    vision_enabled: bool = False  # 是否对该 KOL 启用图片 LLM 分析(GLM-4V)
    fallback: bool = True  # 规则解析失败时是否降级到 LLM
    min_confidence: float = 0.4  # 低于此置信度触发 LLM 兜底

    @classmethod
    def from_kol(cls, kol) -> "KolLLMConfig":
        """从 Kol 模型实例创建配置。"""
        return cls(
            enabled=getattr(kol, 'llm_enabled', False),
            vision_enabled=getattr(kol, 'vision_llm_enabled', False),
            fallback=getattr(kol, 'llm_fallback', True),
            min_confidence=getattr(kol, 'llm_min_confidence', 0.4),
        )


# ---------- 信号意图识别 ----------
# 致命/禁用关键词 (复盘、假设、分析)
BLOCKED_KEYWORDS = [
    r"\bclosed\b", r"\bhit\s*tp\b", r"\bclosed\s*at\b",
    r"\byesterday\b", r"\blast\s*week\b", r"\blast\s*time\b",
    r"\bhypothetical\b", r"\bif\s*i\s*had\b", r"\bshould\s*have\b",
    r"\banalysis\b", r"\bprediction\b", r"\bforecast\b",
    r"\b在?过去", r"复盘", r"回顾", r"上周", r"昨天", r"刚才",
    r"假设", r"假如", r"如果我", r"本可以", r"本应该",
    r"分析", r"预测", r"技术面", r"基本面",
]
# 安全/交易动作关键词
ACTION_KEYWORDS = [
    r"\bsetup\b", r"\benter\b", r"\bopen\b", r"\baction\b", r"\bnow\b",
    r"\bbuy\s*now\b", r"\bsell\s*now\b", r"\bgo\s*long\b", r"\bgo\s*short\b",
    r"立即开", r"现在进", r"建仓", r"开仓", r"进多", r"进空",
    r"多单(走|准备)", r"空单(走|准备)", r"开始买入", r"开始卖出",
]

# 完全无关的噪音关键词 (直接跳过)
NOISE_KEYWORDS = [
    r"\bannouncement\b", r"\bupdate\b", r"\bnews\b", r"\bmaintenance\b",
    r"\bairdrop\b", r"\bstaking\b", r"\bpartnership\b", r"\blisting\b",
    r"公告", r"通知", r"维护", r"升级", r"空投", r"质押", r"合作", r"上币",
]


def classify_signal_intent(text: str) -> tuple[str, str]:
    """
    对消息进行意图分类:
    - 'trade': 明确的交易指令
    - 'analysis': 分析/预测/复盘 (不执行)
    - 'noise': 公告/维护等噪音 (不执行)
    - 'unknown': 无法判断,交给后续解析流程

    返回 (intent, reason)
    """
    if not text:
        return "noise", "empty message"

    low = text.lower()

    # 1. 噪音/公告检测 (优先级最高)
    for kw in NOISE_KEYWORDS:
        if re.search(kw, low):
            return "noise", f"matched noise keyword: {kw}"

    # 2. 致命关键词 (复盘/假设/分析)
    blocked_found = []
    for kw in BLOCKED_KEYWORDS:
        if re.search(kw, low):
            blocked_found.append(kw)

    # 3. 交易动作关键词
    action_found = []
    for kw in ACTION_KEYWORDS:
        if re.search(kw, low):
            action_found.append(kw)

    # 判断逻辑:
    if blocked_found and action_found:
        # 既有复盘词又有交易词 → 模糊,优先判定为分析,但标注出来
        return "analysis", f"conflicting signals: blocked={blocked_found}, action={action_found}"

    if blocked_found and not action_found:
        return "analysis", f"matched blocked keywords: {blocked_found}"

    if action_found:
        return "trade", f"matched action keywords: {action_found}"

    # 没有任何关键词命中
    return "unknown", "no intent keywords matched"


# ---------- 符号标准化 ----------
STABLECOINS = {"USDT", "USDC", "BUSD", "DAI", "TUSD", "FDUSD"}
SYMBOL_ALIASES = {
    # 常见错别字/大小写修正
    "SOl": "SOL", "s0l": "SOL", "ETHH": "ETH", "BTCC": "BTC",
    "PEPEE": "PEPE", "SHIBB": "SHIB", "WIFW": "WIF",
}


def normalize_symbol(raw: str) -> str:
    """$SOL / SOLUSDT / SOL/USDT / SOL-USDT → SOL/USDT。"""
    if not raw:
        return ""
    s = raw.strip().lstrip("$").upper().strip()
    s = SYMBOL_ALIASES.get(s, s)
    # 去除常见后缀
    for suffix in ("USDT", "USDC", "BUSD", "-USDT", "_USDT", "/USDT", "-PERP", ".P"):
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[: -len(suffix)]
            break
    if "/" in s:
        s = s.split("/")[0]
    if "-" in s:
        s = s.split("-")[0]
    s = s.strip()
    if s in STABLECOINS:
        return ""
    if not s or not s.isalnum():
        return ""
    return f"{s}/USDT"


# ---------- 方向识别 ----------
LONG_WORDS = {"long", "buy", "做多", "多单", "开多", "买入", "bull", "bullish", "多", "进行方向：做多", "进行方向:做多"}
SHORT_WORDS = {"short", "sell", "做空", "空单", "开空", "卖出", "bear", "bearish", "空", "进行方向：做空", "进行方向:做空"}

# 离场/平仓关键词 (用于识别非开仓指令)
EXIT_WORDS = {"出局", "离场", "平仓", "平", "关闭", "close", "exit", "take profit", "tp hit"}


def detect_side(text: str) -> str:
    low = text.lower()
    for w in SHORT_WORDS:
        if re.search(rf"\b{re.escape(w)}\b", low) or w in low:
            return "short"
    for w in LONG_WORDS:
        if re.search(rf"\b{re.escape(w)}\b", low) or w in low:
            return "long"
    return ""


def check_exit_intent(text: str) -> tuple[bool, str]:
    """检查是否为离场/平仓指令。返回 (是否离场, 原因)。"""
    low = text.lower()
    for w in EXIT_WORDS:
        if re.search(rf"\b{re.escape(w)}\b", low) or w in low:
            return True, f"检测到平仓关键词: {w}"
    return False, ""


# ---------- 数字提取 ----------
PRICE_RE = r"(\d+(?:[.,]\d+)?)"


def _to_float(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _extract_prices_after(text: str, keywords: list[str]) -> list[float]:
    """提取关键词后的价格列表,如 TP 155/160/165 或 止盈 155 160 165。"""
    for kw in keywords:
        # 关键词后跟价格(支持 空格 / , ， | 分隔)
        pat = rf"{kw}\s*[:：]?\s*({PRICE_RE}(?:\s*[/,，|]?\s*{PRICE_RE})*)"
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            nums = re.findall(PRICE_RE, m.group(1))
            prices = [p for p in (_to_float(n) for n in nums) if p and p > 0]
            if prices:
                return prices
    return []


def extract_take_profits(text: str) -> list[float]:
    tps = []

    # 1. 多级 TP1/TP2/TP3 (英文格式)
    for i in range(1, 6):
        for m in re.finditer(rf"\btp\s*{i}\b\s*[:：]?\s*{PRICE_RE}", text, re.IGNORECASE):
            p = _to_float(m.group(1))
            if p and p > 0:
                tps.append(p)
    if tps:
        return tps

    # 2. 通用 TP / take profit (英文格式)
    tps = _extract_prices_after(text, [r"\btp\b", r"take\s*profit"])
    if tps:
        return tps

    # 3. 止盈点位 / 止盈 / 目标 (中文格式,支持 KOL 多行格式)
    # 先检查是否为 "待定", "暂无" 等
    tp_match = re.search(r"(?:止盈点位|止盈|目标)\s*[:：]?\s*(.*?)(?=\n|$|备注|提示)", text)
    if tp_match:
        tp_content = tp_match.group(1).strip()
        if re.search(r"(待定|暂无|无|空|未定|tbd|n/a|none)", tp_content, re.IGNORECASE):
            return []  # 明确表示无止盈

    # 4. 通用中文止盈提取
    tps = _extract_prices_after(text, [r"止盈点位", r"止盈", r"目标"])
    return tps


def extract_stop_loss(text: str) -> float | None:
    # 关键词列表
    keywords = [
        r"止损点位", r"止损", r"stop\s*loss", r"sl", r"stop",
    ]

    for kw in keywords:
        # 检查是否有 "待定" / "无" / "空" 等表示无止损的词
        # 查找 "止损" 后面的内容是否为否定词
        sl_pattern = rf"{kw}\s*[:：]?\s*(.*?)(?=\n|$|止盈|进场|具体|进行)"
        m = re.search(sl_pattern, text, re.IGNORECASE)
        if m:
            sl_content = m.group(1).strip()
            # 如果是 "待定", "暂无", "无" 等,返回 None
            if re.search(r"(待定|暂无|无|空|未定|tbd|n/a|none)", sl_content, re.IGNORECASE):
                return None

        # 正常提取价格
        m = re.search(rf"{kw}\s*[:：]?\s*{PRICE_RE}", text, re.IGNORECASE)
        if m:
            p = _to_float(m.group(1))
            if p and p > 0:
                return p

    return None


def extract_entry(text: str) -> tuple[float | None, list[float]]:
    """返回 (首个入场价, 分批入场价列表)。"""
    # 关键词列表 (按优先级排序)
    keywords = [
        r"进场点位", r"入场点位", r"进场", r"入场",
        r"开仓", r"entry", r"buy\s*@?", r"@\s*", r"点位",
    ]

    for kw in keywords:
        # 支持多种价格格式:
        # 1869-1873 (范围)
        # 63000附近 (约数)
        # 150-152 / 150~152 / 150/152
        # 单个价格
        m = re.search(
            rf"{kw}\s*[:：]?\s*{PRICE_RE}\s*(?:[-~至到]\s*{PRICE_RE})?\s*(?:附近|左右)?",
            text, re.IGNORECASE
        )
        if m:
            # 提取所有匹配的价格
            prices = []
            for g in m.groups():
                if g:
                    p = _to_float(g)
                    if p and p > 0:
                        prices.append(p)

            # 如果有 "附近" 或 "左右",取单个价格作为中心点
            if re.search(r"(附近|左右)", text) and len(prices) == 1:
                # 保持原样,直接用这个价格
                pass

            if prices:
                return prices[0], prices

    return None, []


def extract_leverage(text: str) -> int:
    m = re.search(r"(\d+)\s*[xX倍]\b", text)
    if m:
        return max(1, min(int(m.group(1)), 125))
    return 1


def extract_position_pct(text: str) -> float:
    m = re.search(r"仓位\s*[:：]?\s*(\d+(?:\.\d+)?)\s*[%%]|(\d+(?:\.\d+)?)\s*%\s*仓位", text)
    if m:
        return float(m.group(1) or m.group(2))
    m = re.search(r"position\s*[:：]?\s*(\d+(?:\.\d+)?)\s*%", text, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return 0.0


def extract_symbol(text: str) -> str:
    # 1. $TICKER 格式
    m = re.search(r"\$([A-Za-z0-9]{2,12})", text)
    if m:
        s = normalize_symbol(m.group(1))
        if s:
            return s

    # 2. "XXX多单"、"XXX空单"、"XXX多"、"XXX空" 格式 (KOL 平仓常用)
    # 不需要后缀匹配，只要出现这个模式就提取
    m = re.search(r"\b([A-Za-z]{2,10})\s*(多单|空单|多|空)", text)
    if m:
        s = normalize_symbol(m.group(1))
        if s:
            return s

    # 3. 具体产品: XXX / 具体产品：XXX 格式 (KOL 常用)
    m = re.search(r"(?:具体产品|币种|产品)\s*[:：]?\s*([A-Za-z0-9]{2,10})", text)
    if m:
        s = normalize_symbol(m.group(1))
        if s:
            return s

    # 4. TICKER/USDT 或 TICKER-USDT 格式
    m = re.search(r"\b([A-Za-z]{2,10})\s*[/\-_]\s*(USDT|USDC|BUSD)\b", text)
    if m:
        return normalize_symbol(m.group(1))

    # 5. TICKERUSDT 连写格式
    m = re.search(r"\b([A-Z]{2,10})USDT\b", text)
    if m:
        return normalize_symbol(m.group(1))

    return ""


# ---------- OCR ----------
async def ocr_image(image_url: str) -> str:
    """OCR 图片识别:优先使用 PaddleOCR,回退到 Tesseract。"""
    if not settings.ocr_enabled or not image_url:
        return ""

    # 下载图片
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(image_url)
            resp.raise_for_status()
        img_bytes = resp.content
    except Exception as e:
        logger.warning(f"下载图片失败: {e}")
        return ""

    # 方案 1: PaddleOCR (优先)
    try:
        from paddleocr import PaddleOCR

        ocr = PaddleOCR(use_angle_cls=True, lang="ch", use_gpu=False)
        import numpy as np
        from PIL import Image

        img = Image.open(io.BytesIO(img_bytes))
        img_array = np.array(img)
        result = ocr.ocr(img_array, cls=True)

        # 提取所有识别到的文本
        texts = []
        if result and result[0]:
            for line in result[0]:
                if line and len(line) >= 2:
                    texts.append(line[1][0])  # line[1][0] 是识别文本

        ocr_text = "\n".join(texts)
        if ocr_text.strip():
            logger.debug(f"PaddleOCR 识别成功: {len(ocr_text)} 字符")
            return ocr_text
    except ImportError:
        logger.debug("PaddleOCR 未安装,回退到 Tesseract")
    except Exception as e:
        logger.warning(f"PaddleOCR 识别失败: {e},回退到 Tesseract")

    # 方案 2: Tesseract (回退)
    try:
        import pytesseract
        from PIL import Image, ImageEnhance, ImageFilter

        img = Image.open(io.BytesIO(img_bytes))

        # 图片预处理:灰度化、增强对比度、去噪
        if img.mode != "L":
            img = img.convert("L")  # 灰度
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(2.0)  # 增强对比度
        img = img.filter(ImageFilter.MedianFilter())  # 去噪

        # 中英文识别
        ocr_text = pytesseract.image_to_string(img, lang="eng+chi_sim")
        if ocr_text.strip():
            logger.debug(f"Tesseract 识别成功: {len(ocr_text)} 字符")
            return ocr_text
    except ImportError:
        logger.warning("Tesseract 未安装,无法进行 OCR")
    except Exception as e:
        logger.warning(f"Tesseract 识别失败: {e}")

    return ""


# ---------- 主解析入口 ----------
def parse_text(text: str) -> ParsedSignal:
    text = (text or "").strip()
    if not text:
        return ParsedSignal()

    # 1. 检查是否为平仓信号
    is_exit, exit_reason = check_exit_intent(text)

    # 2. 提取基本信息
    symbol = extract_symbol(text)
    side = detect_side(text)

    # 3. 如果是平仓信号
    if is_exit:
        logger.info(f"检测到平仓信号: {exit_reason}")
        # 平仓信号仍需提取品种和方向
        return ParsedSignal(
            symbol=symbol,
            side=side,
            raw_text=text,
            confidence=0.7 if symbol else 0.3,  # 有品种信息时置信度较高
            is_exit_signal=True,
            exit_reason=exit_reason,
        )

    # 4. 正常开仓信号解析
    entry, entry_prices = extract_entry(text)
    tps = extract_take_profits(text)
    sl = extract_stop_loss(text)
    leverage = extract_leverage(text)
    pos_pct = extract_position_pct(text)

    # 置信度评分
    confidence = 0.0
    if symbol:
        confidence += 0.4
    if side:
        confidence += 0.3
    if entry or entry_prices:
        confidence += 0.2
    if tps or sl:
        confidence += 0.1
    confidence = min(confidence, 1.0)

    return ParsedSignal(
        symbol=symbol,
        side=side,
        entry_price=entry,
        entry_prices=entry_prices,
        take_profits=tps,
        stop_loss=sl,
        leverage=leverage,
        position_pct=pos_pct,
        raw_text=text,
        confidence=confidence,
        has_image=False,
    )


async def parse_message(
    raw_text: str,
    image_url: str = "",
    image_base64: str = "",
    kol_config: KolLLMConfig | None = None,
    kol_name: str = "",
) -> ParsedSignal:
    """解析一条 Discord 消息(文本 + 可选图片)。

    Args:
        raw_text: 原始文本
        image_url: 图片 URL
        image_base64: 图片 base64
        kol_config: KOL 级别 LLM 配置（为 None 则使用全局设置）
        kol_name: KOL 名称（用于日志）

    流程:
    1. 根据 KOL 配置决定是否使用 LLM
    2. 如果有图片且 KOL 配置了多模态分析，优先使用 LLM 分析图片
    3. 否则使用 OCR + 规则解析
    4. 规则解析失败时，根据 KOL 配置降级到 LLM 文本解析
    """
    combined = raw_text or ""
    parsed = ParsedSignal()
    has_image = bool(image_url or image_base64)

    # ============ 决定是否使用 LLM ============
    # 优先级：KOL 配置 > 全局配置
    use_llm = False
    use_vision = False  # 图片 LLM(GLM-4V) - 仅对 KOL.vision_llm_enabled=True 生效
    use_llm_fallback = True  # 默认启用 fallback
    llm_min_confidence = 0.4

    if kol_config:
        # KOL 级别配置
        if kol_config.enabled:
            use_llm = True
            use_vision = kol_config.vision_enabled  # 该 KOL 是否启用图片 LLM
            use_llm_fallback = kol_config.fallback
            llm_min_confidence = kol_config.min_confidence
    elif settings.llm_enabled:
        # 全局配置：默认启用文本 fallback,图片需 KOL 单独开启
        use_llm = True
        use_llm_fallback = True

    # 如果 LLM 不可用，直接走规则解析
    if not _LLM_AVAILABLE:
        use_llm = False
        use_llm_fallback = False

    # ============ 阶段 1: 图片处理 ============
    if has_image and use_llm and use_vision:
        # 1a. KOL 启用了图片 LLM → 用 GLM-4V 直接分析图片
        logger.info(f"[{kol_name}] 使用图片 LLM (GLM-4V) 分析图片")
        try:
            llm_parsed, usage = await parse_image_with_llm(
                image_urls=[image_url] if image_url else None,
                image_base64_list=[image_base64] if image_base64 else None,
                kol_vision_enabled=True,  # 已确认 KOL 启用
            )
            if llm_parsed and llm_parsed.confidence >= 0.5:
                logger.info(
                    f"[{kol_name}] 图片 LLM 解析成功: confidence={llm_parsed.confidence}, "
                    f"tokens={usage.get('total_tokens', 0)}"
                )
                llm_parsed.has_image = True
                return llm_parsed
        except Exception as e:
            logger.warning(f"[{kol_name}] 图片 LLM 解析失败: {e}, 回退到 OCR")

    # 1b. OCR 识别图片内容（规则路径）
    if image_url:
        ocr_text = await ocr_image(image_url)
        if ocr_text:
            combined = (combined + "\n" + ocr_text).strip()

    # ============ 阶段 2: 规则解析 ============
    parsed = parse_text(combined)
    parsed.has_image = has_image

    # ============ 阶段 3: LLM 兜底 ============
    # 当规则解析失败（置信度低或关键信息缺失）时，根据配置降级到 LLM 文本解析
    if use_llm and use_llm_fallback:
        should_use_llm_fallback = False

        # 判断是否需要 LLM 兜底
        if parsed.confidence < llm_min_confidence:
            should_use_llm_fallback = True
            logger.debug(f"[{kol_name}] 规则解析置信度低 ({parsed.confidence:.2f} < {llm_min_confidence}), 尝试 LLM 兜底")

        if not parsed.symbol and not parsed.is_exit_signal:
            should_use_llm_fallback = True
            logger.debug(f"[{kol_name}] 规则解析未识别到品种, 尝试 LLM 兜底")

        # 执行 LLM 兜底
        if should_use_llm_fallback and combined:
            logger.info(f"[{kol_name}] 规则解析失败，调用 LLM 兜底")
            try:
                llm_parsed, usage = await parse_with_llm(combined)
                if llm_parsed and llm_parsed.confidence > parsed.confidence:
                    logger.info(
                        f"[{kol_name}] LLM 兜底解析成功: confidence {parsed.confidence:.2f} -> "
                        f"{llm_parsed.confidence:.2f}, tokens={usage.get('total_tokens', 0)}"
                    )
                    llm_parsed.has_image = has_image
                    return llm_parsed
                else:
                    logger.debug(
                        f"[{kol_name}] LLM 兜底结果未优于规则解析 "
                        f"({llm_parsed.confidence if llm_parsed else 0:.2f} vs {parsed.confidence:.2f})"
                    )
            except Exception as e:
                logger.warning(f"[{kol_name}] LLM 兜底解析失败: {e}")

    # ============ 阶段 4: 最终检查 ============
    if not parsed.symbol and not parsed.side and not parsed.is_exit_signal:
        parsed.confidence = 0.0

    return parsed


def parse_message_sync(
    raw_text: str,
    image_url: str = "",
    kol_config: KolLLMConfig | None = None,
) -> ParsedSignal:
    """同步版本的消息解析（仅规则解析，不调用 LLM）。"""
    combined = raw_text or ""
    if image_url:
        logger.warning("同步解析不支持 OCR，仅解析文本")
    parsed = parse_text(combined)
    parsed.has_image = bool(image_url)
    return parsed
