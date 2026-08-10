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
from urllib.parse import urlparse

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
    r"\blong\s*now\b", r"\bshort\s*now\b",
    r"立即开", r"现在进", r"建仓", r"开仓", r"进多", r"进空",
    r"进场", r"入场", r"下单", r"挂单",
    r"准备做多", r"准备做空", r"准备进场", r"准备入场",
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
NON_SYMBOL_WORDS = {
    "KOL", "VIP", "群", "消息", "工作室", "币姐", "昨天", "今天", "明天",
}
SYMBOL_ALIASES = {
    # 常见错别字/大小写修正
    "SOl": "SOL", "s0l": "SOL", "ETHH": "ETH", "BTCC": "BTC",
    "PEPEE": "PEPE", "SHIBB": "SHIB", "WIFW": "WIF",
}

# 中文币种名映射(按优先级排序,避免子串误匹配)
CN_COIN_NAMES = {
    # 3字币种名(优先匹配,避免被2字币种名截获)
    "狗狗币": "DOGE", "瑞波币": "XRP", "币安币": "BNB", "以太币": "ETH",
    # 2字币种名
    "比特币": "BTC", "以太坊": "ETH", "币安": "BNB", "SOL币": "SOL",
    "瑞波": "XRP", "艾达币": "ADA", "艾达": "ADA", "波场": "TRX",
    "抹茶": "MX", "柚子": "EOS", "莱特币": "LTC", "莱特": "LTC",
    "比特币现金": "BCH", "比特现金": "BCH", "门罗币": "XMR",
    "恒星币": "XLM", "恒星": "XLM", "eos": "EOS", "以太经典": "ETC",
    "经典币": "ETC", "小蚁": "NEO", "本体": "ONT", "量子": "QTUM",
    "小蚁币": "NEO", "本体币": "ONT", "量子币": "QTUM",
    # KOL 常用中文币种俗称(2024-2025 高频)
    "大饼": "BTC", "以太": "ETH", "索拉纳": "SOL", "索尔": "SOL",
    "波卡": "DOT", "柴犬币": "SHIB", "柴犬": "SHIB", "狗币": "DOGE",
    "弗洛基": "FLOKI", "佩佩": "PEPE", "奥迪": "ORDI", "林克": "LINK",
    "萨维亚": "AVAX", "阿普": "APT", "泰波": "TIA", "修": "SEI",
    " pupper": "WIF",
}


def normalize_symbol(raw: str) -> str:
    """$SOL / SOLUSDT / SOL/USDT / SOL-USDT → SOL/USDT。"""
    if not raw:
        return ""
    s = raw.strip().lstrip("$").upper().strip()
    s = SYMBOL_ALIASES.get(s, s)
    # 去除常见后缀:优先匹配带分隔符的后缀,避免 "BTC-USDT" 被先匹配到 "USDT" 后留下 "BTC-"
    for suffix in ("-USDT", "_USDT", "/USDT", "-USDC", "_USDC", "/USDC", "-PERP", ".P", "USDT", "USDC", "BUSD"):
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[: -len(suffix)]
            break
    if "/" in s:
        s = s.split("/")[0]
    if "-" in s:
        s = s.split("-")[0]
    s = s.strip()
    if s in STABLECOINS or s in NON_SYMBOL_WORDS:
        return ""
    if not s or not s.isalnum():
        return ""
    return f"{s}/USDT"


# ---------- 方向识别 ----------
# 注意:不包含单字 "多"/"空",因为 "多空博弈"、"多空较量" 等常见词会导致误判。
# 单字匹配过于宽泛,必须使用完整词组(做多/做空/多单/空单等)。
# 包含常见交易方向 emoji(🈳=空/short、🈵=满/long、📈📉🐻🐂 等),KOL 常用 emoji 表达方向。
LONG_WORDS = {
    # 核心做多词组
    "long", "buy", "做多", "多单", "开多", "买入", "看多", "做反弹", "反弹", "抄底",
    "bull", "bullish",
    "进行方向：做多", "进行方向:做多", "方向：多", "方向:多",
    # KOL 常用做多术语(不含已有词的子串)
    "挂多", "进多", "接多", "加多", "低多", "看涨", "低吸", "多军", "起涨",
    "逢低接", "逢低买入", "逢低做多",
    # Emoji 方向标识
    "📈", "🐂", "🈵", "🚀", "🟢",
}
SHORT_WORDS = {
    # 核心做空词组
    "short", "sell", "做空", "空单", "开空", "卖出", "看空",
    "bear", "bearish",
    "进行方向：做空", "进行方向:做空", "方向：空", "方向:空",
    # KOL 常用做空术语(不含已有词的子串)
    "挂空", "进空", "接空", "加空", "高空", "看跌", "高抛", "空军", "起跌",
    "逢高抛", "逢高做空", "逢高卖出",
    # Emoji 方向标识
    "📉", "🐻", "🈳", "🔻", "🔴",
}

# 离场/平仓关键词 (用于识别非开仓指令)
# 注意:不包含单字 "平" 和单词 "close",因为:
#   - "平" 会误匹配 "平均/平移/水平" 等非交易词
#   - "close" 会通过子串匹配误命中 "closed/closely" (复盘语境)
# 必须使用完整词组(平仓/平多/平空/close position 等)。
EXIT_WORDS = {
    # 明确平仓词
    "出局", "离场", "平仓", "平多", "平空", "全平", "关闭",
    "清仓", "获利了结", "获利平仓",
    "close position", "close order", "close all", "close trade",
    "exit", "take profit", "tp hit",
}

# 复盘/分析语境关键词 (出现这些词时,不应判定为平仓信号)
# 这些词表示KOL在回顾过去的操作或分析市场,不是在发新的交易指令
REVIEW_INDICATORS = [
    r"顺利(?:空到|多到|做到|涨到|跌到)",  # "顺利空到1890" 回顾已完成的交易
    r"恭喜",  # "恭喜大家" 庆祝过去的盈利
    r"昨夜|昨日|前天",  # 回顾过去时间点
    r"重头戏",  # "昨夜重头戏谷歌了" 回顾性叙述
    r"吃了?个.*(?:反弹|利润|利润)",  # "吃了个短线反弹" 回顾已完成交易
    r"到时候再看",  # "到时候再看" 未来不确定,不是平仓
    r"复盘|回顾",  # 明确复盘
    r"让大家.*(?:做多|做空|进场|入场)",  # "让大家在359做多了进去" 回顾给过的建议
    r"舒琴.*(?:有讲过|讲过)",  # "舒琴有讲过" 引用过去观点
    r"甚至.*(?:两遍|三次)",  # "甚至空了两遍" 回顾性描述
    r"每天实时操作",  # 宣传性文字
    r"求稳.*可以不做",  # "求稳这两天可以不做" 建议观望,不是平仓
    r"留意",  # "大家可以留意下" 建议,不是指令
    # ★ 新增: 叙事/故事/感悟类文本检测 (KOL 发生活感悟而非交易信号)
    # 这些模式匹配叙事性文本,避免文中出现的"卖了/退出"等词被误判为平仓信号
    r"那天晚上|那天",  # 叙事开头 "那天晚上，他给我发了一条消息"
    r"他给我发",  # 叙事 "他给我发了一条消息"
    r"他说[:：]?[\"""'']",  # 叙事对话 "他说: ..."
    r"第[一二三四五]桶金",  # "赚到第一桶金" 人生经历叙述
    r"财富自由",  # 哲学感悟
    r"真正的.*不是.*而是",  # 哲学反思 "真正的财富自由不是...而是..."
    r"人生最重要",  # 哲学感悟
    r"交易可以让人赚钱",  # 哲学感悟
    r"把钱.*退出市场|资金.*退出市场",  # "把一部分资金彻底退出市场" 叙事语境
    r"换车|换房",  # 生活叙事 "换车、换房、旅行"
    r"朋友圈",  # 生活叙事
    r"舒服的生活|真正的.*生活",  # 哲学感悟
]

# 持仓继续/维持信号 (KOL告诉用户继续持有,不是新的交易指令)
# 这些消息只是状态更新或心理安抚,不包含可执行的交易操作
HOLDING_INDICATORS = [
    r"继续持有",
    r"继续拿(?:住|着)",
    r"拿着(?:就行|别动|不动)?",
    r"持仓(?:没退|未退|还在|不动|别动|继续)",
    r"持仓是值得的",
    r"值得持有",
    r"保持持仓",
    r"保持仓位",
    r"仓位(?:没退|未退|还在|不动|别动|继续)",
    r"单子(?:没动|不动|还在)",
    r"耐心持有",
    r"继续等待",
    r"等待下探",
    r"等待回调",
    r"浮亏.*继续",
    r"浮盈.*继续",
    r"略微浮亏",
    r"略微浮盈",
    r"成本价附近",
    r"当前价格在成本",
]


# 模糊平仓/离场表达模式 (KOL 口语化表达)
# 使用正则匹配,覆盖以下场景:
#   "这个单子先走吧" / "差不多了可以出了" / "先撤了" / "落袋为安"
#   "可以跑了" / "止盈离场" / "暂时退出" / "不玩了"
#   "走人" / "撤了" / "出了" / "跑了" / "抛了" / "卖了"
#   "这个位置可以出" / "差不多就收" / "保本走" / "先走一步"
FUZZY_EXIT_PATTERNS = [
    # "X走"/"X出"/"X跑"/"X撤" 模式 (单字动词在句末)
    r"(?:先走|先撤|先跑|先出|先平)",
    r"(?:可以走|可以出|可以跑|可以撤|可以平)",
    r"(?:走了|出了|跑了|撤了|抛了|卖了|撤了)",
    r"(?:走人|撤人|跑路)",
    # "单子/仓位 + 动词" 模式
    r"单子\s*(?:先走|先撤|走|撤|出|跑|平|关)",
    r"仓位\s*(?:先走|先撤|走|撤|出|跑|平|关)",
    r"这个\s*(?:单|单子|仓位)\s*(?:先走|走|撤|出|跑|平|关|收)",
    # "差不多/差不多就" 模式
    r"差不多\s*(?:可以|就)\s*(?:出|走|跑|撤|收|平)",
    r"差不多了?\s*(?:可以)?\s*(?:出|走|跑|撤|收|平)",
    # 获利/止盈 + 离场
    r"(?:落袋|落袋为安|见好就收|收工|收摊)",
    r"(?:止盈|获利)\s*(?:离场|出场|走人|撤)",
    # 不玩了/放弃
    r"(?:不玩了|不跟了|放弃|到此为止|到此结束)",
    # 暂时退出 (注意: "观望"单独出现不等于平仓,它表示"等待时机")
    r"暂时\s*(?:退出|离场|撤出)",
    # 保本/止损离场
    r"(?:保本\s*(?:走|出|撤|离场|平)|止损\s*(?:离场|出场|走))",
    # "这单/这波 + 动词"
    r"这(?:单|波|次)\s*(?:走|出|跑|撤|平|收|完)",
    # "出来了/走出去了" 等完成态(不匹配"走出区间/走出趋势"等)
    r"(?:出来了|走出来|走出去|跑出来|撤出来)",
    # "先X为敬" 网络用语
    r"先\s*(?:走|撤|跑|出)\s*(?:为敬|一步)",
    # "走一波/撤一波"
    r"(?:走|撤|跑)\s*(?:一波|一下)",
]


def detect_side(text: str) -> str:
    """识别交易方向(long/short)。

    匹配策略:
    1. 优先匹配完整词组(做多/做空/多单/空单等)
    2. LONG 和 SHORT 同时检查,取更精确的匹配(词组优先于单词)
    3. 如果同时匹配到多和空,取最后出现的方向(通常"建议空单"中空单是最终建议)
    """
    low = text.lower()

    # 收集所有匹配到的方向词及其位置
    long_hits = []
    short_hits = []
    for w in LONG_WORDS:
        idx = low.rfind(w.lower())
        if idx >= 0:
            long_hits.append((idx, w))
    for w in SHORT_WORDS:
        idx = low.rfind(w.lower())
        if idx >= 0:
            short_hits.append((idx, w))

    if not long_hits and not short_hits:
        # 保守单字方向: 只在"多/空"后面紧跟批次、建仓、入场、挂单或价格时启用。
        # 避免把"多空分界线/多空博弈"这类分析词误判为方向。
        single_long = re.search(
            r"(?:^|[\s，,。:：])多(?!空)\s*(?=(?:第?\s*[一二两三四五六七八九十\d]+\s*(?:批|笔|次)|分批|建仓|入场|进场|挂单|开仓|\d))",
            text,
        )
        single_short = re.search(
            r"(?:^|[\s，,。:：])空(?!单军头)\s*(?=(?:第?\s*[一二两三四五六七八九十\d]+\s*(?:批|笔|次)|分批|建仓|入场|进场|挂单|开仓|\d))",
            text,
        )
        if single_long and not single_short:
            return "long"
        if single_short and not single_long:
            return "short"
        if single_long and single_short:
            return "long" if single_long.start() > single_short.start() else "short"
        return ""

    # 只匹配到一个方向
    if long_hits and not short_hits:
        return "long"
    if short_hits and not long_hits:
        return "short"

    # 两个方向都匹配到:取最后出现的方向词(通常最终的交易建议在句末)
    # 例如 "多空博弈,建议空单" → long 在前(多空),short 在后(空单) → 取 short
    all_hits = [(idx, "long") for idx, _ in long_hits] + [(idx, "short") for idx, _ in short_hits]
    all_hits.sort(key=lambda x: x[0])
    return all_hits[-1][1]


def check_exit_intent(text: str) -> tuple[bool, str]:
    """检查是否为离场/平仓指令。返回 (是否离场, 原因)。

    三层检测:
    1. 精确关键词匹配 (EXIT_WORDS)
    2. 模糊口语化模式匹配 (FUZZY_EXIT_PATTERNS)
    3. 上下文启发式判断 (短消息 + 离场动词暗示)
    """
    low = text.lower()

    # 第零层:复盘/分析语境排除 (优先级最高)
    # 如果文本包含复盘/回顾性表达,则不视为平仓信号
    for pattern in REVIEW_INDICATORS:
        if re.search(pattern, text):
            return False, f"复盘/分析语境排除: {pattern}"

    # 第一层:精确关键词
    for w in EXIT_WORDS:
        if re.search(rf"\b{re.escape(w)}\b", low) or w in low:
            return True, f"检测到平仓关键词: {w}"

    # 第二层:模糊口语化模式
    # ★ 长文本(>200字)保护: 模糊模式中的单字动词(卖了/走了/出了等)
    # 在叙事/故事文本中极易误匹配。对长文本,要求同时存在交易语境关键词
    # (品种名/单子/仓位/多单/空单/止盈/止损/合约等)才允许模糊模式生效。
    _is_long_text = len(text) > 200
    _has_trading_context = bool(
        re.search(r"(?:单子|仓位|多单|空单|止盈|止损|合约|持仓|进场|入场|开仓|平仓|爆仓|杠杆)", text)
        or re.search(r"\b(?:BTC|ETH|SOL|BNB|XRP|DOGE|PEPE|AVAX|LINK|ADA)\b", text, re.IGNORECASE)
    )
    for pattern in FUZZY_EXIT_PATTERNS:
        if re.search(pattern, text):
            if _is_long_text and not _has_trading_context:
                # 长文本 + 无交易语境 → 跳过模糊模式匹配(很可能是叙事/故事)
                continue
            return True, f"检测到模糊平仓表达: {pattern}"

    # 第三层:上下文启发式
    # 短消息(<30字)且包含离场暗示动词,且无入场价/方向/止盈止损等开仓要素
    # 典型场景: "BTC先走吧" / "ETH出了" / "差不多了,走"
    exit_verbs = ["撤", "抛", "收"]
    has_exit_verb = any(v in text for v in exit_verbs)
    has_open_elements = bool(
        re.search(r"\d+\.?\d*\s*(?:附近|左右|一线)", text)  # 入场价
        or re.search(r"(?:做多|做空|开多|开空|多单|空单)", text)  # 方向
        or re.search(r"(?:止盈|止损|目标)", text)  # TP/SL
    )
    # 排除分析语境: "走势"/"出去"/"走过" 等非交易场景
    non_exit_contexts = ["走势", "走出", "出去", "走过", "进行", "走低", "走高", "走强", "走弱", "走出区间", "走出趋势"]
    has_non_exit = any(nc in text for nc in non_exit_contexts)

    if has_exit_verb and not has_open_elements and not has_non_exit and len(text) < 50:
        return True, "启发式检测:短消息含离场动词且无开仓要素"

    return False, ""


# ---------- 止盈止损更新意图识别 ----------
# 显式更新关键词:必须包含"修改/调整/更新/改为/调成/调到"等变更动词
UPDATE_VERBS = r"(?:改为|改成|改到|调为|调到|调成|调整为|修改为|更新为|变更为|变更|更新|调整|修改|上调|下调|上移|下移|移到|提到|推到|拉到|放到|保护到|降到|抬高|降低|带到)"

UPDATE_KEYWORDS = [
    rf"修改\s*止盈", rf"调整\s*止盈", rf"止盈\s*{UPDATE_VERBS}",
    rf"修改\s*止损", rf"调整\s*止损", rf"止损\s*{UPDATE_VERBS}",
    rf"修改\s*止盈止损", rf"调整\s*止盈止损", rf"更新\s*止盈止损",
    rf"止盈止损\s*{UPDATE_VERBS}",
    # 新增: 目标/TP/SL 更新模式
    rf"修改\s*目标", rf"调整\s*目标", rf"目标\s*{UPDATE_VERBS}",
    rf"移\s*止损", rf"移\s*止盈",
    rf"上移\s*止损", rf"下移\s*止损", rf"上移\s*止盈", rf"下移\s*止盈",
    rf"推\s*止损", rf"拉\s*止损", rf"止损\s*保护",
    rf"保本(?:止损)?", rf"保护(?:利润|本金)",
    rf"止盈(?:先看|看到|看至|看向)",
    rf"\bTP\s*{UPDATE_VERBS}", rf"\bSL\s*{UPDATE_VERBS}",
]


def check_update_intent(text: str) -> tuple[bool, str]:
    """检查是否为止盈止损更新信号(显式关键词检测)。

    返回 (是否更新, 原因)。
    隐式检测(有止盈/止损但无入场价和方向)在 parse_text() 中进行。
    """
    if not text:
        return False, ""
    for pat in UPDATE_KEYWORDS:
        if re.search(pat, text, re.IGNORECASE):
            return True, f"显式更新关键词命中: {pat}"
    return False, ""


# ---------- 多动作识别 ----------
# 撤挂单关键词: 只代表取消未成交挂单,不等于反向开仓。
CANCEL_ORDER_PATTERNS = [
    r"\bcancel\s+(?:order|orders|limit|limits)\b",
    r"\bcancel\s+pending\b",
    r"撤单",
    r"撤\s*不挂了",
    r"撤销(?:挂单|订单|委托)?",
    r"取消(?:挂单|订单|委托)",
    r"不挂了",
    r"别挂了|不用挂了|先不挂|暂不挂",
    r"(?:多单|空单|多|空|单子|挂单|委托).{0,8}(?:撤了|撤掉|撤回|取消)",
    r"(?:多单|空单|多|空|单子|挂单|委托).{0,8}不挂",
    r"(?:撤掉|撤回).{0,8}(?:多单|空单|多|空|单子|挂单|委托|点位)",
    r"没挂到.{0,12}(?:撤|不挂|取消)",
    r"撤.*挂",
]

# 旧挂单状态说明: 这些表达只说明以前的单还在挂着,不能当作新开仓。
PENDING_STATUS_PATTERNS = [
    r"(?:多单|空单|单子|挂单|委托).{0,12}(?:挂着|还在|继续挂|保留|有效)",
    r"(?:挂着|还挂着|继续挂着|原单挂着|之前的单还在)",
    r"(?:上面|下面|这些|那些|前面|原来|之前).{0,10}(?:点位|位置|单子|挂单).{0,12}(?:挂着|继续挂|还在|有效)",
    r"(?:点位|位置).{0,8}(?:继续挂|挂着|还在|有效)",
    r"(?:keep|still)\s+(?:pending|open)",
]

# 明确新开仓/新挂单关键词。只有这些词命中,才把方向解析成 open_long/open_short。
EXPLICIT_OPEN_PATTERNS = [
    r"\b(?:buy|sell|long|short)\s+now\b",
    r"\b(?:go\s+long|go\s+short|enter|open)\b",
    r"开多|开空|进多|进空|做多|做空|买入|卖出",
    r"入场|进场|下单|建仓|上车|上车了|搞一波|搞一下|干一波",
    r"挂多|挂空|挂入|挂单|委托|埋伏|埋伏单|抄底单|摸顶单",
    r"挂(?:一个|个|一笔|一单)?(?:反弹|回踩)?",
    r"挂\s*\d+(?:\.\d+)?\s*(?:附近|一线|位置)?",
    r"重新挂|再挂|新挂|补挂|重新进|再次进",
]

# 撤单消息里只有出现这些词,才允许"撤旧单后重新开/重新挂"。
# 普通"建仓/方向/止损止盈"在撤单消息中通常是在描述要取消的旧挂单参数。
REOPEN_AFTER_CANCEL_PATTERNS = [
    r"重新挂|再挂|新挂|补挂",
    r"重新进|再次进|再开|重新开",
]

# ---------- 场景分类与话术库 ----------
# 这些段落通常是盘面分析/关键位说明,不应参与下单参数抽取。
ANALYSIS_SECTION_PATTERNS = [
    r"盘面基调",
    r"盘面分析",
    r"行情分析",
    r"接下来(?:重点|主要)?看",
    r"(?:BTC|ETH|SOL|Btc|Eth)?\s*核心关键位",
    r"(?:BTC|ETH|SOL|Btc|Eth)?\s*日线多空分界线",
    r"上方压力位",
    r"下方支撑位",
    r"压力位",
    r"支撑位",
    r"多空分界线",
]

# 纯分析/预测/观察话术。命中且没有完整交易块时,直接忽略。
ANALYSIS_ONLY_PATTERNS = [
    r"我的判断是",
    r"行情更可能",
    r"更可能(?:向上|向下)",
    r"我认为|个人看法|整体看|大方向|短线看|日内看",
    r"盘面基调",
    r"盘面分析|行情分析|走势分析",
    r"先向上测试|向下测试|测试\d",
    r"收上以后再看|收下以后再看|再看",
    r"反弹升级|假突破处理|判断作废|走势作废",
    r"周线|日线|月线|4小时|小时线|收线|均线|平台",
    r"压力位|支撑位|关键位|分界线|阻力位|防守位",
    r"流动性|放量|缩量|突破确认|回踩确认",
    r"反向风险|上方空间|下方空间|震荡区间|箱体",
]

# 条件观察话术。没有明确"现在开/重新挂"时,不能直接开仓。
CONDITIONAL_OBSERVE_PATTERNS = [
    r"收上.*再看",
    r"收下.*再看",
    r"站上.*再",
    r"站稳.*再",
    r"跌破.*再",
    r"突破.*再",
    r"有效(?:突破|跌破|站上|站稳).*再",
    r"确认(?:突破|跌破|站上|站稳).*再",
    r"等.*(?:确认|站稳|突破|跌破|回踩|反弹)",
    r"(?:先|等).{0,12}(?:守住|站稳|收上|收下|突破|跌破)",
    r"如果.*(?:再|就)",
    r"若.*(?:再|就)",
    r"等待|观察|留意|不急|先看|再等等|等一下",
]

# 生活吐槽/叙事内容。常包含"KOL/止损/100次"等词,必须避免误识别为币种或止损。
NON_TRADE_NARRATIVE_PATTERNS = [
    r"其他KOL",
    r"恶意评价|别有用心|跳出来闹",
    r"工作室.*牛鬼蛇神",
    r"不想带单|可以带着赚\d+次",
    r"嘴巴都说干了|不承认",
    r"如果不想看",
    r"为什么.*这么看|这么看的原因|逻辑是|思路是",
    r"之前(?:说过|讲过|提醒过|给过)",
    r"昨天.*(?:说过|讲过|提醒|判断)",
    r"不是马后炮|马后炮",
    r"复盘一下|解释一下|回头看",
]


def _has_trade_param_block(text: str) -> bool:
    """是否包含完整交易参数块:方向 + 建仓/入场 + 风控字段。"""
    if not text:
        return False
    has_side = bool(re.search(r"方向\s*[:：]\s*(?:多|空)|做多|做空|开多|开空|多单|空单|long|short", text, re.IGNORECASE))
    has_entry = bool(re.search(r"建仓|入场|进场|挂单|entry|buy\s*zone|sell\s*zone", text, re.IGNORECASE))
    has_risk = bool(re.search(r"止损|止盈|\bSL\b|\bTP\b", text, re.IGNORECASE))
    return has_side and has_entry and has_risk


def strip_analysis_sections(text: str) -> str:
    """截掉盘面分析/压力支撑位段,只保留前面的交易操作段。

    大镖客常见格式是:
    交易块(方向/建仓/止损/止盈) + 盘面基调 + 关键位/压力位/支撑位。
    后半段数字不能参与入场/止盈/止损抽取。
    """
    if not text:
        return ""
    cut_pos: int | None = None
    for pat in ANALYSIS_SECTION_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m and (cut_pos is None or m.start() < cut_pos):
            cut_pos = m.start()
    if cut_pos is None:
        return text
    head = text[:cut_pos].strip()
    # 只有前半段确实像交易块/撤单块时才截断;否则保留原文交给分析过滤。
    if _has_trade_param_block(head) or _has_any_pattern(head, CANCEL_ORDER_PATTERNS):
        return head
    return text


def classify_signal_scene(text: str) -> tuple[str, str]:
    """粗分类当前文本场景,用于先挡掉非交易内容。"""
    if not text:
        return "noise", "empty"
    has_trade_block = _has_trade_param_block(text)
    if _has_any_pattern(text, NON_TRADE_NARRATIVE_PATTERNS) and not has_trade_block:
        return "narrative", "生活吐槽/叙事内容"
    if _has_any_pattern(text, CANCEL_ORDER_PATTERNS):
        return "cancel_order", "撤销挂单话术"
    if _has_any_pattern(text, UPDATE_KEYWORDS):
        return "update_tp_sl", "止盈止损更新话术"
    if _has_any_pattern(text, PENDING_STATUS_PATTERNS) and has_trade_block:
        return "refresh_pending", "挂着且包含完整交易参数"
    if _has_any_pattern(text, PENDING_STATUS_PATTERNS):
        return "hold_pending", "仅说明挂单/持仓状态"
    if _has_any_pattern(text, CONDITIONAL_OBSERVE_PATTERNS) and not has_trade_block:
        return "conditional_observe", "条件观察/等待确认"
    if _has_any_pattern(text, ANALYSIS_ONLY_PATTERNS) and not has_trade_block:
        return "analysis", "行情分析/关键位说明"
    if has_trade_block:
        return "trade_block", "完整交易参数块"
    return "unknown", "未命中特定场景"


def _has_any_pattern(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)



def detect_signal_actions(
    text: str,
    *,
    side: str = "",
    is_exit: bool = False,
    is_update: bool = False,
) -> list[str]:
    """识别一条消息包含的标准化动作。

    保守原则:
    - "撤不挂了/撤单" 只生成 cancel_order。
    - 只有"多单挂着/继续挂着"但没有建仓参数时,只说明旧挂单状态,不生成 open_long。
    - "方向+建仓+挂着"表示延续/刷新挂单,先查旧 pending,没有旧 pending 才新挂。
    - 只有明确出现开仓/新挂单词,才生成 open_long/open_short。
    """
    actions: list[str] = []
    has_cancel = _has_any_pattern(text, CANCEL_ORDER_PATTERNS)
    has_pending_status = _has_any_pattern(text, PENDING_STATUS_PATTERNS)
    has_explicit_open = _has_any_pattern(text, EXPLICIT_OPEN_PATTERNS)
    has_reopen_after_cancel = _has_any_pattern(text, REOPEN_AFTER_CANCEL_PATTERNS)
    has_trade_block = _has_trade_param_block(text)

    # 撤挂单优先级高于模糊平仓。"多单撤了/空单撤了"是取消未成交挂单,不是平仓。
    if has_cancel:
        is_exit = False

    if has_cancel:
        actions.append("cancel_order")
    if is_exit:
        actions.append("close_position")
    elif is_update:
        actions.append("update_tp_sl")
    elif side in ("long", "short") and has_pending_status and has_explicit_open and (not has_cancel or has_trade_block):
        actions.append("refresh_pending")
    elif side in ("long", "short") and has_explicit_open and (not has_cancel or has_reopen_after_cancel):
        actions.append(f"open_{side}")
    elif has_pending_status and not has_explicit_open and not has_cancel:
        actions.append("hold_pending")

    # 去重并保持顺序
    out: list[str] = []
    for action in actions:
        if action not in out:
            out.append(action)
    return out


def apply_actions_to_parsed(text: str, parsed: ParsedSignal) -> ParsedSignal:
    """根据解析结果补齐 actions/action,同时兼容旧布尔字段。"""
    actions = detect_signal_actions(
        text,
        side=parsed.side,
        is_exit=parsed.is_exit_signal,
        is_update=parsed.is_update_signal,
    )
    if not actions:
        if parsed.is_exit_signal:
            actions = ["close_position"]
        elif parsed.is_update_signal:
            actions = ["update_tp_sl"]
        elif parsed.symbol and parsed.side in ("long", "short"):
            actions = [f"open_{parsed.side}"]
    parsed.actions = actions
    parsed.action = actions[0] if actions else ""
    return parsed


# ---------- 数字提取 ----------
PRICE_RE = r"(\d+(?:[.,]\d+)?(?:[kK])?)"


def _to_float(s: str) -> float | None:
    """将字符串转为浮点数,支持"万"和"k/K"单位(如 6.41万 → 64100, 65k → 65000)。"""
    try:
        s = s.replace(",", "")
        # 检测并处理"万"单位
        if "万" in s:
            s = s.replace("万", "")
            return float(s) * 10000
        # 检测并处理"k/K"单位 (KOL 常用 65k 表示 65000)
        if s and s[-1] in ("k", "K"):
            s = s[:-1]
            return float(s) * 1000
        return float(s)
    except (ValueError, AttributeError):
        return None


def extract_cancel_order_prices(text: str) -> list[float]:
    """撤挂单消息里的价格表示旧挂单点位,用于精确取消 pending。

    例如:
    - "撤 不挂了 下边的点位 65000 64000"
    - "BTC撤单 65000/64000 多单挂着"
    """
    if not text:
        return []
    if not _has_any_pattern(text, CANCEL_ORDER_PATTERNS):
        return []

    # 如果存在止盈/止损描述,只取其之前的价格,避免误把 TP/SL 当成要撤的挂单点位。
    segment = re.split(r"(?:止盈|止损|\btp\b|\bsl\b)", text, maxsplit=1, flags=re.IGNORECASE)[0]
    prices: list[float] = []
    for m in re.finditer(PRICE_RE, segment):
        p = _to_float(m.group(1))
        if p and p > 0:
            # 逐价格检测其后是否紧跟"万"字
            end_pos = m.end()
            if end_pos < len(segment) and segment[end_pos] == "万":
                p *= 10000
            if p not in prices:
                prices.append(p)
    return prices


def _extract_prices_after(text: str, keywords: list[str]) -> list[float]:
    """提取关键词后的价格列表,如 TP 155/160/165 或 止盈 155 160 165。

    支持"万"单位(如 6.41万 → 64100)。
    支持"改为/调整"等变更动词(如 "止盈改为65000")。
    """
    for kw in keywords:
        # 关键词后跟价格(支持 空格 / , ， | 分隔),并支持"万"单位
        # 允许关键词和价格之间有变更动词(改为/调成/调整为等)
        pat = rf"{kw}\s*[:：]?\s*(?:{UPDATE_VERBS}|先看|看到|看至|看向)?\s*({PRICE_RE}(?:\s*[/,，|、和与]?\s*{PRICE_RE})*)\s*(?:万)?"
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            matched_text = m.group(0)  # 完整匹配文本
            prices: list[float] = []
            for pm in re.finditer(PRICE_RE, matched_text):
                p = _to_float(pm.group(1))
                if p and p > 0:
                    # 逐价格检测其后是否紧跟"万"字
                    end_pos = pm.end()
                    if end_pos < len(matched_text) and matched_text[end_pos] == "万":
                        p *= 10000
                    prices.append(p)
            if prices:
                return prices
    return []


def extract_take_profits(text: str) -> list[float]:
    tps = []

    # 1. 多级 TP1/TP2/TP3 (英文格式,支持"万"单位)
    for i in range(1, 6):
        for m in re.finditer(rf"\btp\s*{i}\b\s*[:：]?\s*{PRICE_RE}\s*(?:万)?", text, re.IGNORECASE):
            p = _to_float(m.group(1))
            if p and p > 0:
                if "万" in m.group(0):
                    p *= 10000
                tps.append(p)
    if tps:
        return tps

    # 2. 通用 TP / take profit (英文格式)
    tps = _extract_prices_after(text, [r"\btp\b", r"take\s*profit"])
    if tps:
        return tps

    # 3.6. 中文编号目标: 目标1/目标2/目标3 (KOL 常用格式)
    for i in range(1, 6):
        for m in re.finditer(rf"目标\s*{i}\s*[:：]?\s*{PRICE_RE}\s*(?:万)?", text):
            p = _to_float(m.group(1))
            if p and p > 0:
                if "万" in m.group(0):
                    p *= 10000
                tps.append(p)
    if tps:
        return tps

    # 3.7. 中文序号目标: 第一目标/第二目标/第三目标
    cn_nums = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
    for cn_num in cn_nums:
        for m in re.finditer(rf"第{cn_num}目标\s*[:：]?\s*{PRICE_RE}\s*(?:万)?", text):
            p = _to_float(m.group(1))
            if p and p > 0:
                if "万" in m.group(0):
                    p *= 10000
                tps.append(p)
    if tps:
        return tps


    # 2.5. 反向格式: 价格 + 止盈 (如 "6.4止盈"、"6.4万止盈")
    #    KOL 常简写为 "X.X止盈" 而非 "止盈：X.X"
    #    排除误匹配:数字若属于"止损/进场/入场/开仓"的值(中间无逗号/句号分隔),则跳过
    #    例: "止损点位：62800 止盈点位：65700" 中 62800 不应被当作止盈
    #        "进场64000 止盈点位：65700" 中 64000 不应被当作止盈
    #        "入场：6.41-6.44万附近 止盈" 中 6.44 是入场范围,不应被当作止盈
    #    但 "分2次入场，6.4止盈" 中 6.4 是独立的止盈价(逗号分隔),不应跳过
    reverse_tp_pattern = PRICE_RE + r"\s*(?:万)?\s*(?:附近|左右)?\s*(?:处|位置)?\s*(?:止盈点位|止盈|目标(?!\d))"
    for m in re.finditer(reverse_tp_pattern, text):
        # 检查数字前面是否属于其他价格关键词的值
        prefix = text[max(0, m.start() - 20):m.start()]
        # 找 prefix 中最后一个"止损/进场/入场/开仓"关键词
        last_non_tp_idx = -1
        for kw in ("止损", "进场", "入场", "开仓"):
            idx = prefix.rfind(kw)
            if idx > last_non_tp_idx:
                last_non_tp_idx = idx
        if last_non_tp_idx >= 0:
            # 检查该关键词到数字之间是否有逗号/句号/分号分隔符
            # 有分隔符 → 独立价格(不跳过);无分隔符 → 属于该关键词的值(跳过)
            between = prefix[last_non_tp_idx:]
            if not re.search(r"[,，。；;]", between):
                continue
        p = _to_float(m.group(1))
        if p and p > 0:
            if "万" in m.group(0):
                p *= 10000
            tps.append(p)
    if tps:
        return tps

    # 3. 止盈点位 / 止盈 / 目标 (中文格式,支持 KOL 多行格式)
    # 先检查是否为 "待定", "暂无" 等
    tp_match = re.search(r"(?:止盈点位|止盈|目标)\s*[:：]?\s*(.*?)(?=\n|$|备注|提示)", text)
    if tp_match:
        tp_content = tp_match.group(1).strip()
        if re.search(r"(待定|暂无|无|空|未定|tbd|n/a|none)", tp_content, re.IGNORECASE):
            return []  # 明确表示无止盈

    # 3.5. 多止盈点格式: 点位1：xxx 点位2：xxx 点位3：xxx
    #    舒琴格式: "止盈：点位1：6.35附近 点位2：6.3 点位3：6.23万"
    multi_tp_pattern = r"点位\s*\d+\s*[:：]?\s*" + PRICE_RE + r"\s*(?:万)?\s*(?:附近|左右)?"
    tp_matches = list(re.finditer(multi_tp_pattern, text))
    if tp_matches:
        for m in tp_matches:
            p = _to_float(m.group(1))
            if p and p > 0:
                if "万" in m.group(0):
                    p *= 10000
                tps.append(p)
        if tps:
            return tps

    # 4. 通用中文止盈提取
    tps = _extract_prices_after(text, [r"止盈点位", r"止盈价", r"止盈", r"目标价", r"目标位", r"目标", r"target\s*price", r"price\s*target"])
    return tps


def extract_stop_loss(text: str) -> float | None:
    # 关键词列表
    keywords = [
        r"止损点位", r"止损价", r"止损线", r"止损", r"防守位", r"防守",
        r"stop\s*loss", r"cut\s*loss", r"\bsl\b", r"\bstop\b",
        r"invalidation", r"invalid",
    ]

    # 1. 先检查正向格式: 止损 + 价格 (如 "止损63000"、"止损：63000")
    #    优先于反向格式,避免 "止盈65000 止损63000" 中 65000 被误提取为 SL
    for kw in keywords:
        # 检查是否有 "待定" / "无" / "空" 等表示无止损的词
        # 查找 "止损" 后面的内容是否为否定词
        sl_pattern = rf"{kw}\s*[:：]?\s*(.*?)(?=\n|$|止盈|进场|具体|进行)"
        m = re.search(sl_pattern, text, re.IGNORECASE)
        if m:
            sl_content = m.group(1).strip()
            # 如果是 "待定", "暂无", "无" 等,返回 None
            if re.search(r"(待定|暂无|无|未定|tbd|n/a|none)", sl_content, re.IGNORECASE) or sl_content.strip() == "空":
                return None

        # 更新型止损: "止损上移到65000" / "SL moved to 65000" / "止损保护到入场价附近"
        m = re.search(rf"{kw}\s*[:：]?\s*(?:{UPDATE_VERBS}|moved?\s+to|move\s+to)?\s*{PRICE_RE}\s*(?:万)?", text, re.IGNORECASE)
        if m:
            p = _to_float(m.group(1))
            if p and p > 0:
                if "万" in m.group(0):
                    p *= 10000
                return p

        # 正常提取价格(支持"万"单位)
        # 支持 "止损：6.47" 和 "止损：小幅涨破6.47" 等格式
        # 先尝试直接跟数字
        m = re.search(rf"{kw}\s*[:：]?\s*{PRICE_RE}\s*(?:万)?", text, re.IGNORECASE)
        if m:
            p = _to_float(m.group(1))
            if p and p > 0:
                if "万" in m.group(0):
                    p *= 10000
                return p

        # 如果直接跟数字失败,尝试跳过描述词(如 "小幅涨破"、"跌破" 等)
        # 格式: "止损：[描述词]xxx"
        m = re.search(rf"{kw}\s*[:：]?\s*[^0-9]{{0,8}}{PRICE_RE}\s*(?:万)?", text, re.IGNORECASE)
        if m:
            p = _to_float(m.group(1))
            if p and p > 0:
                if "万" in m.group(0):
                    p *= 10000
                return p

    # 2. 反向格式兜底: 价格 + 止损 (如 "6.1止损"、"6.53止损")
    #    KOL 常简写为 "X.X止损" 而非 "止损：X.X"
    #    放在正向格式之后,避免误匹配 "止盈65000 止损" 中的 65000
    reverse_sl_pattern = PRICE_RE + r"\s*(?:万)?\s*(?:附近|左右)?\s*(?:处|位置)?\s*(?:突破|跌破|涨破)?\s*(?:止损点位|止损)"
    m = re.search(reverse_sl_pattern, text)
    if m:
        p = _to_float(m.group(1))
        if p and p > 0:
            if "万" in m.group(0):
                p *= 10000
            return p

    return None


def extract_entry(text: str) -> tuple[float | None, list[float]]:
    """返回 (首个入场价, 分批入场价列表)。"""
    # 关键词列表 (按优先级排序)
    # "点位" 放最后,避免优先匹配到 "止盈点位/止损点位" 的价格
    keywords = [
        r"进场点位", r"入场点位", r"进场", r"入场",
        r"开仓", r"开仓价", r"建仓", r"建仓位", r"建仓价",
        r"挂单价", r"触发价", r"委托价",
        r"entry", r"enter\s*at", r"buy\s*@?", r"@\s*",
        r"buy\s*zone", r"sell\s*zone", r"open\s*price",
        r"点位",
    ]

    # - "第一批65000 第二批64500 第三批64000"
    # - "1批 65000 / 2批 64500"
    batch_labeled_prices: list[float] = []
    batch_label_pat = rf"(?:第?\s*[一二三四五六七八九十\d]+\s*(?:批|笔|次)|(?:批|笔|次)\s*[一二三四五六七八九十\d]+)\s*[:：]?\s*{PRICE_RE}\s*(?:万)?"
    for m in re.finditer(batch_label_pat, text, re.IGNORECASE):
        p = _to_float(m.group(1))
        if p and p > 0:
            if "万" in m.group(0):
                p *= 10000
            if p not in batch_labeled_prices:
                batch_labeled_prices.append(p)
    if batch_labeled_prices:
        return batch_labeled_prices[0], batch_labeled_prices

    # "分批建仓/分2次入场/挂单/建仓" 后直接跟多个价格,视为分批入场价。
    batch_then_prices = re.search(
        rf"(?:分\s*[一二两三四五六七八九十\d]+\s*(?:批|笔|次)\s*)?(?:分批|分批建仓|分批入场|分批进场|建仓|挂单|埋伏|接|低吸|补仓)\s*[:：]?\s*(.*?)(?=止损|止盈|目标|\bsl\b|\btp\b|$)",
        text,
        re.IGNORECASE,
    )
    if batch_then_prices:
        segment = batch_then_prices.group(1)
        nums = re.findall(PRICE_RE, segment)
        prices: list[float] = []
        is_wan = "万" in segment
        for n in nums:
            p = _to_float(n)
            if p and p > 0:
                if is_wan:
                    p *= 10000
                if p not in prices:
                    prices.append(p)
        if prices:
            return prices[0], prices

    # 0. 特殊格式: "在X.X和X.X万支撑附近" / "X.X-X.X万附近"
    #    支持 "在6.2和6.3万支撑附近做多" 这种格式
    range_patterns = [
        r"在\s*" + PRICE_RE + r"\s*(?:和|与|到)\s*" + PRICE_RE + r"\s*(?:万)?\s*(?:附近|左右)?\s*(?:支撑|阻力|区间)",
        r"在\s*" + PRICE_RE + r"\s*[-~至到和与]\s*" + PRICE_RE + r"\s*(?:万)?\s*(?:附近|左右)?",
        # 新增: "X-X万附近" 格式(无"在"前缀)
        r"(?<!止盈)(?<!止损)" + PRICE_RE + r"\s*[-~至到]\s*" + PRICE_RE + r"\s*(?:万)?\s*(?:附近|左右)?\s*(?:支撑|阻力|区间|做多|做空|开多|开空)",
        # 新增: "X到X" 简单区间格式
        r"(?<!止盈)(?<!止损)(?<!目标)" + PRICE_RE + r"\s*(?:到|至)\s*" + PRICE_RE + r"\s*(?:万)?\s*(?:附近|左右)?",
    ]
    for pat in range_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            matched_text = m.group(0)
            is_wan = "万" in matched_text
            prices = []
            for g in m.groups():
                if g:
                    p = _to_float(g)
                    if p and p > 0:
                        if is_wan:
                            p *= 10000
                        prices.append(p)
            if prices:
                return prices[0], prices

    # 特殊模式: "开多 65000/64000" / "重新挂多 65000 64000"。
    # 方向/挂单动作后直接跟多个价格时,这些价格就是分批入场价。
    action_then_prices = re.search(
        rf"(?:重新挂|再挂|新挂|补挂|分批建仓|分批入场|分批进场|挂多|挂空|挂单|建仓|开多|开空|进多|进空|做多|做空|买入|卖出|入场|进场|entry)\s*[:：]?\s*(.*?)(?=止损|止盈|目标|\bsl\b|\btp\b|$)",
        text,
        re.IGNORECASE,
    )
    if action_then_prices:
        segment = action_then_prices.group(1)
        nums = re.findall(PRICE_RE, segment)
        prices = []
        is_wan = "万" in segment
        for n in nums:
            p = _to_float(n)
            if p and p > 0:
                if is_wan:
                    p *= 10000
                prices.append(p)
        if prices:
            return prices[0], prices

    # 特殊模式: "X附近做反弹" / "X左右做多" 等无入场关键词的格式
    # 例如 "1870附近 做反弹" 中 extract_entry 应识别 1870 为入场价
    bare_entry_pat = rf"(?<!止盈)(?<!止损)(?<!到)(?<!至)(?<!位){PRICE_RE}\s*(?:附近|左右|一线|位置|一线附近)"
    m_bare = re.search(bare_entry_pat, text)
    if m_bare:
        p = _to_float(m_bare.group(1))
        if p and p > 0:
            # 确认这不是止盈/止损价格(检查上下文)
            bare_context = text[max(0, m_bare.start()-10):m_bare.start()]
            if "止盈" not in bare_context and "止损" not in bare_context:
                return p, [p]

    # 特殊模式: "X做多" / "X开多" / "X买入" / "X做空" 等 (价格 + 方向动词)
    # KOL 常写 "1870做多" "65000做空" 这种格式,前面没有入场关键词
    price_action_pat = rf"(?<!止盈)(?<!止损)(?<!目标)(?<!止盈点位)(?<!止损点位){PRICE_RE}\s*(?:万)?\s*(?:做多|做空|开多|开空|买入|卖出|做多单|做空单|做多|做空)"
    m_pa = re.search(price_action_pat, text)
    if m_pa:
        p = _to_float(m_pa.group(1))
        if p and p > 0:
            matched_text = m_pa.group(0)
            if "万" in matched_text:
                p *= 10000
            return p, [p]

    for kw in keywords:
        # 负向回顾断言:排除 "止盈点位/止损点位" 被当作入场价
        # 支持多种价格格式:
        # 1869-1873 (范围)
        # 63000附近 (约数)
        # 150-152 / 150~152 / 150/152
        # 单个价格
        # 支持"万"单位: 6.41万、6.41-6.44万
        m = re.search(
            rf"(?<!止盈)(?<!止损){kw}\s*[:：]?\s*{PRICE_RE}\s*(?:[-~至到]\s*{PRICE_RE})?\s*(?:万)?\s*(?:附近|左右)?",
            text, re.IGNORECASE
        )
        if m:
            # 检查是否有"万"单位
            matched_text = m.group(0)
            is_wan = "万" in matched_text

            # 提取所有匹配的价格
            prices = []
            for g in m.groups():
                if g:
                    p = _to_float(g)
                    if p and p > 0:
                        if is_wan:
                            p *= 10000
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
        return max(0.0, min(float(m.group(1) or m.group(2)), 100.0))
    m = re.search(r"position\s*[:：]?\s*(\d+(?:\.\d+)?)\s*%", text, re.IGNORECASE)
    if m:
        return max(0.0, min(float(m.group(1)), 100.0))
    # 中文口语仓位词:
    # - 半仓 = 50%
    # - 三成仓 / 3成仓 / 三成 = 30%
    # - 轻仓 = 30%, 重仓 = 70%（保守默认,避免口语词导致过度下单）
    # - 全仓/满仓 = 100%
    normalized = text.replace(" ", "")
    keyword_pct = [
        (r"(半仓|半成仓位|半仓位)", 50.0),
        (r"(轻仓|小仓|小仓位|轻仓位)", 30.0),
        (r"(重仓|大仓|大仓位|重仓位)", 70.0),
        (r"(全仓|满仓|梭哈)", 100.0),
    ]
    for pattern, pct in keyword_pct:
        if re.search(pattern, normalized):
            return pct
    chinese_digits = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    m = re.search(r"([一二两三四五六七八九十1-9])成(?:仓|仓位|操作)?", normalized)
    if m:
        raw = m.group(1)
        n = chinese_digits.get(raw, int(raw) if raw.isdigit() else 0)
        if n:
            return max(0.0, min(float(n * 10), 100.0))
    return 0.0


def extract_symbol(text: str) -> str:
    # 0. 先去除 URL,避免 "https://..." 中的 https 被误识别为币种
    #    KOL 信号常带图文链接(如 https://discord.com/channels/xxx),不处理会得到 "HTTPS/USDT"
    if re.search(r"https?://|www\.", text, re.IGNORECASE):
        text = re.sub(r"https?://\S+|www\.\S+", " ", text, flags=re.IGNORECASE)

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

    # 2.5. 平仓/离场 + 品种 格式 (平仓信号品种提取)
    # 匹配 "平仓 BTC"、"BTC 平仓"、"离场ETH"、"ETH全平" 等,KOL 平仓信号常省略多/空后缀
    m = re.search(r"(?:平仓|离场|出局|全平|平多|平空|关闭)\s*[:：]?\s*([A-Za-z]{2,10})", text)
    if m:
        s = normalize_symbol(m.group(1))
        if s:
            return s
    m = re.search(r"\b([A-Za-z]{2,10})\s*(?:平仓|离场|出局|全平)", text)
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

    # 5.5. 中文币种名映射(如"比特币"→"BTC","以太坊"→"ETH")
    #    按 key 长度降序匹配,避免 "狗狗币" 被 "币" 单独截获
    for cn_name in sorted(CN_COIN_NAMES, key=len, reverse=True):
        if cn_name in text:
            return normalize_symbol(CN_COIN_NAMES[cn_name])

    # 6. 中文语境兜底:匹配前后是中文/标点/空格/边界的英文币种
    #    解决 "买入BTC吧"、"BTC怎么样" 等 \b 无法匹配的问题
    #    使用 (?<![A-Za-z]) 和 (?![A-Za-z]) 替代 \b
    #    常见币种优先匹配(避免误匹配 "ABC" 等非币种词)
    common_coins = [
        "BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "DOT", "MATIC",
        "LINK", "LTC", "AVAX", "UNI", "ATOM", "ETC", "BCH", "FIL", "APT",
        "ARB", "OP", "DYDX", "AAVE", "MKR", "COMP", "CRV", "SNX", "INJ",
        "SUI", "TIA", "SEI", "ORDI", "PEPE", "WIF", "FLOKI", "JUP", "BONK",
    ]
    for coin in common_coins:
        # 匹配: 前面是中文/标点/空格/边界,后面是中文/标点/空格/边界
        m = re.search(rf"(?<![A-Za-z]){coin}(?![A-Za-z])", text, re.IGNORECASE)
        if m:
            return normalize_symbol(coin)

    # 7. 通用中文语境匹配:英文2-10字母,前后是中文或标点
    #    放在最后作为兜底,优先级低于常见币种
    m = re.search(r"(?<=[\u4e00-\u9fff\s：:，,。！!？?（()）])?([A-Za-z]{2,10})(?=[\u4e00-\u9fff\s：:，,。！!？?（()）]|$)", text)
    if m:
        s = normalize_symbol(m.group(1))
        if s:
            return s

    return ""


# ---------- OCR ----------
# PaddleOCR 模块级单例(惰性初始化,避免每次调用都重新加载模型)
_paddle_ocr = None
_paddle_ocr_init_failed = False


def _get_paddle_ocr():
    """获取 PaddleOCR 单例。首次调用时初始化,之后复用。

    PaddleOCR 不支持重复初始化(底层 PaddleX 会报错),必须用单例。
    初始化失败后标记 _paddle_ocr_init_failed,避免反复尝试。
    """
    global _paddle_ocr, _paddle_ocr_init_failed
    if _paddle_ocr_init_failed:
        return None
    if _paddle_ocr is None:
        try:
            from paddleocr import PaddleOCR
            import paddleocr as _paddleocr_mod
            _paddle_ver = getattr(_paddleocr_mod, "__version__", "0")
            # PaddleOCR 3.x 存在后端兼容性 bug (NotImplementedError:
            # ConvertPirAttribute2RuntimeAttribute), 无法正常执行 OCR。
            # 直接跳过 PaddleOCR, 使用 Tesseract 作为主 OCR 引擎。
            if int(_paddle_ver.split(".")[0]) >= 3:
                logger.warning(
                    f"PaddleOCR {_paddle_ver} 存在后端兼容性 bug, "
                    f"跳过 PaddleOCR, 使用 Tesseract 作为 OCR 引擎"
                )
                _paddle_ocr_init_failed = True
                return None
            _paddle_ocr = PaddleOCR(use_textline_orientation=True, lang="ch")
            logger.info("PaddleOCR 单例初始化成功")
        except ImportError:
            logger.debug("PaddleOCR 未安装,回退到 Tesseract")
            _paddle_ocr_init_failed = True
            return None
        except Exception as e:
            logger.warning(f"PaddleOCR 初始化失败: {e},回退到 Tesseract")
            _paddle_ocr_init_failed = True
            return None
    return _paddle_ocr


# SSRF 防护: 图片下载白名单域名
_ALLOWED_IMAGE_DOMAINS = {
    "cdn.discordapp.com", "media.discordapp.net",
    "images-ext-1.discordapp.net", "images-ext-2.discordapp.net",
}


def _is_safe_url(url: str) -> bool:
    """检查 URL 是否在允许的域名白名单内且使用 http/https 协议。"""
    try:
        parsed = urlparse(url)
        return parsed.hostname in _ALLOWED_IMAGE_DOMAINS and parsed.scheme in ("https", "http")
    except Exception:
        return False


async def ocr_image(image_url: str) -> str:
    """OCR 图片识别:优先使用 PaddleOCR,回退到 Tesseract。"""
    if not settings.ocr_enabled or not image_url:
        return ""

    # SSRF 防护: 校验图片 URL 域名白名单
    if not _is_safe_url(image_url):
        logger.warning(f"图片 URL 不在白名单内,拒绝下载: {image_url[:100]}")
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

    # 方案 1: PaddleOCR (优先,使用单例)
    ocr = _get_paddle_ocr()
    if ocr is not None:
        try:
            import numpy as np
            from PIL import Image

            img = Image.open(io.BytesIO(img_bytes))
            img_array = np.array(img)
            result = ocr.ocr(img_array)

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
    raw_text = text
    effective_text = strip_analysis_sections(text).strip()
    scene, scene_reason = classify_signal_scene(effective_text)

    if scene in ("analysis", "conditional_observe", "narrative", "noise"):
        logger.info(f"检测到非交易场景({scene}),标记为忽略: {scene_reason}")
        return ParsedSignal(
            raw_text=raw_text,
            confidence=0.0,
            reason=scene_reason,
        )

    text = effective_text

    # 0. 检查是否为"继续持有"类消息 (不是有效信号)
    _holding_match = any(re.search(p, text) for p in HOLDING_INDICATORS)
    if _holding_match:
        logger.info(f"检测到持仓继续/维持消息,标记为非有效信号: {text[:80]}")
        return ParsedSignal(raw_text=raw_text, confidence=0.0, reason="持仓继续/维持消息,非交易信号")

    # 1. 检查是否为平仓信号
    is_exit, exit_reason = check_exit_intent(text)

    # 2. 提取基本信息
    symbol = extract_symbol(text)
    side = detect_side(text)
    pre_actions = detect_signal_actions(text, side=side)
    if "cancel_order" in pre_actions:
        is_exit = False
        exit_reason = ""

    # 2.5 旧挂单状态说明: 如 "多单挂着/继续挂着"。
    # 没有明确新开仓词时,不能误判成 open_long/open_short。
    if pre_actions == ["hold_pending"]:
        logger.info(f"检测到旧挂单状态说明,不作为新交易信号: {text[:80]}")
        return ParsedSignal(
            symbol=symbol,
            side=side,
            raw_text=raw_text,
            confidence=0.0,
            actions=pre_actions,
            action=pre_actions[0],
            reason="旧挂单状态说明,非新开仓信号",
        )

    # 2.6 纯撤挂单信号: 如 "撤不挂了/撤单/取消挂单"。
    # 只撤销未成交挂单,不因为文本里出现"多单/空单"而新开仓。
    if (
        "cancel_order" in pre_actions
        and "refresh_pending" not in pre_actions
        and not any(a.startswith("open_") for a in pre_actions)
    ):
        cancel_prices = extract_cancel_order_prices(text)
        return ParsedSignal(
            symbol=symbol,
            side=side,
            entry_price=cancel_prices[0] if cancel_prices else None,
            entry_prices=cancel_prices,
            raw_text=raw_text,
            confidence=0.7 if symbol else 0.3,
            actions=pre_actions,
            action=pre_actions[0],
            reason="撤销未成交挂单",
        )

    # 3. 如果是平仓信号
    if is_exit:
        logger.info(f"检测到平仓信号: {exit_reason}")
        # 平仓信号仍需提取品种和方向
        return ParsedSignal(
            symbol=symbol,
            side=side,
            raw_text=raw_text,
            confidence=0.7 if symbol else 0.3,  # 有品种信息时置信度较高
            is_exit_signal=True,
            exit_reason=exit_reason,
            actions=["close_position"],
            action="close_position",
        )

    # 4. 正常开仓信号解析
    entry, entry_prices = extract_entry(text)
    tps = extract_take_profits(text)
    sl = extract_stop_loss(text)
    leverage = extract_leverage(text)
    pos_pct = extract_position_pct(text)

    # 5. 智能价格单位推断
    # 如果入场价是大额(>1000),但止盈/止损价格看起来太小(<1000),
    # 说明 KOL 可能省略了"万"字,自动补全
    if entry and entry > 1000:
        # 入场价是万单位
        if tps:
            new_tps = []
            for tp in tps:
                if tp < 1000:
                    new_tps.append(tp * 10000)
                else:
                    new_tps.append(tp)
            tps = new_tps
        if sl and sl < 1000:
            sl = sl * 10000
        if entry_prices:
            new_eps = [ep * 10000 if ep < 1000 else ep for ep in entry_prices]
            entry_prices = new_eps

    # 5.5 止盈止损更新信号检测
    is_update, update_reason = check_update_intent(text)
    if not is_update:
        # 隐式检测:有止盈/止损但无入场价且无方向 → 更新信号
        has_tp_or_sl = bool(tps) or sl is not None
        has_no_entry = entry is None and not entry_prices
        has_no_side = not side
        if has_tp_or_sl and has_no_entry and has_no_side:
            is_update = True
            update_reason = "隐式更新信号: 有止盈/止损但无入场价和方向"

    if is_update:
        logger.info(f"检测到止盈止损更新信号: {update_reason}")
        confidence = 0.5
        if symbol:
            confidence += 0.3
        if tps or sl:
            confidence += 0.2
        return ParsedSignal(
            symbol=symbol,
            side=side,
            take_profits=tps,
            stop_loss=sl,
            leverage=leverage,
            raw_text=raw_text,
            confidence=min(confidence, 1.0),
            is_update_signal=True,
            update_reason=update_reason,
            actions=["update_tp_sl"],
            action="update_tp_sl",
        )

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

    parsed = ParsedSignal(
        symbol=symbol,
        side=side,
        entry_price=entry,
        entry_prices=entry_prices,
        take_profits=tps,
        stop_loss=sl,
        leverage=leverage,
        position_pct=pos_pct,
        raw_text=raw_text,
        confidence=confidence,
        has_image=False,
    )
    return apply_actions_to_parsed(text, parsed)


async def parse_message(
    raw_text: str,
    image_url: str = "",
    image_base64: str = "",
    kol_config: KolLLMConfig | None = None,
    kol_name: str = "",
    context: str = "",
) -> ParsedSignal:
    """解析一条 Discord 消息(文本 + 可选图片)。

    Args:
        raw_text: 原始文本
        image_url: 图片 URL
        image_base64: 图片 base64
        kol_config: KOL 级别 LLM 配置（为 None 则使用全局设置）
        kol_name: KOL 名称（用于日志）

    流程:
    1. 先用 LLM (DEEPSEEK V3) 解析文本信号
    2. 如果有图片且 KOL 配置了多模态分析，优先使用 LLM 分析图片
    3. LLM 解析失败时，回退到规则解析(正则表达式)
    """
    combined = raw_text or ""
    parsed = ParsedSignal()
    has_image = bool(image_url or image_base64)

    # 自动检测: 如果没有显式传入 image_url,但 raw_text 中包含图片 URL,
    # 自动提取作为 image_url (KOL 有时只发图片链接,Discord 不总是将其放入 attachments)
    if not image_url and not image_base64 and combined:
        import re as _re
        _img_url_pat = _re.compile(
            r'(https?://\S+\.(?:jpg|jpeg|png|gif|webp|bmp)(?:\?\S*)?)',
            _re.IGNORECASE
        )
        _m = _img_url_pat.search(combined)
        if _m:
            image_url = _m.group(1)
            has_image = True
            # 从 combined 中移除图片 URL,避免 LLM 将其当作文本信号
            combined = _img_url_pat.sub("", combined).strip()
            logger.info(f"自动检测到图片 URL: {image_url[:100]}")

    # ============ 决定是否使用 LLM ============
    # 优先级：KOL 配置 > 全局配置
    use_llm = False
    use_vision = False  # 图片 LLM(GLM-4V) - 仅对 KOL.vision_llm_enabled=True 生效
    use_llm_fallback = True  # 规则解析作为 fallback
    llm_min_confidence = 0.4

    if kol_config:
        # KOL 级别配置
        if kol_config.enabled:
            use_llm = True
            use_vision = kol_config.vision_enabled  # 该 KOL 是否启用图片 LLM
            use_llm_fallback = kol_config.fallback
            llm_min_confidence = kol_config.min_confidence
    elif settings.llm_enabled:
        # 全局配置：默认启用文本 LLM 解析
        use_llm = True
        use_llm_fallback = True

    # 如果 LLM 不可用，直接走规则解析
    if not _LLM_AVAILABLE:
        use_llm = False
        use_llm_fallback = False

    # ============ 阶段 0: 持仓继续/维持消息检测 ============
    # 检查是否为"继续持有"类消息 (不是有效信号,无需告警)
    if combined:
        _holding_match = any(re.search(p, combined) for p in HOLDING_INDICATORS)
        if _holding_match:
            logger.info(f"[{kol_name}] 检测到持仓继续/维持消息,标记为非有效信号")
            return ParsedSignal(raw_text=combined, confidence=0.0, reason="持仓继续/维持消息,非交易信号")

    # ============ 阶段 1: 图片处理 ============
    if has_image and use_llm and use_vision:
        # 1a. KOL 启用了图片 LLM → 用 GLM-4V 直接分析图片
        logger.info(f"[{kol_name}] 使用图片 LLM (GLM-4V) 分析图片")
        try:
            llm_parsed, usage = await parse_image_with_llm(
                image_urls=[image_url] if image_url else None,
                image_base64_list=[image_base64] if image_base64 else None,
                kol_vision_enabled=True,  # 已确认 KOL 启用
                min_confidence=llm_min_confidence,
            )
            if llm_parsed and llm_parsed.confidence >= llm_min_confidence:
                logger.info(
                    f"[{kol_name}] 图片 LLM 解析成功: confidence={llm_parsed.confidence}, "
                    f"tokens={usage.get('total_tokens', 0)}"
                )
                llm_parsed.has_image = True
                llm_parsed = apply_actions_to_parsed(combined, llm_parsed)
                return llm_parsed
        except Exception as e:
            logger.warning(f"[{kol_name}] 图片 LLM 解析失败: {e}, 回退到 OCR")

    # 1b. OCR 识别图片内容（补充文本）
    _ocr_success = False
    if image_url:
        ocr_text = await ocr_image(image_url)
        if ocr_text:
            combined = (combined + "\n" + ocr_text).strip()
            _ocr_success = True

    # 1c. 如果 OCR 失败但启用了 vision LLM,尝试用 GLM-4V 分析图片
    #     这解决了 OCR 不可用时的图片信号丢失问题
    if image_url and not _ocr_success and use_llm and use_vision:
        logger.info(f"[{kol_name}] OCR 无结果,尝试使用图片 LLM (GLM-4V) 分析图片")
        try:
            llm_parsed, usage = await parse_image_with_llm(
                image_urls=[image_url],
                image_base64_list=[image_base64] if image_base64 else None,
                kol_vision_enabled=True,
                min_confidence=llm_min_confidence,
            )
            if llm_parsed and llm_parsed.confidence >= llm_min_confidence:
                logger.info(
                    f"[{kol_name}] 图片 LLM (OCR fallback) 解析成功: confidence={llm_parsed.confidence}, "
                    f"tokens={usage.get('total_tokens', 0)}"
                )
                llm_parsed.has_image = True
                llm_parsed = apply_actions_to_parsed(combined, llm_parsed)
                return llm_parsed
        except Exception as e:
            logger.warning(f"[{kol_name}] 图片 LLM (OCR fallback) 解析失败: {e}")

    # ============ 阶段 1.8: 场景分类 + 分析段过滤 ============
    combined_for_parse = strip_analysis_sections(combined).strip()
    scene, scene_reason = classify_signal_scene(combined_for_parse)
    if scene in ("analysis", "conditional_observe", "narrative", "noise"):
        logger.info(f"[{kol_name}] 检测到非交易场景({scene}),跳过: {scene_reason}")
        return ParsedSignal(raw_text=combined, confidence=0.0, reason=scene_reason)
    if combined_for_parse and combined_for_parse != combined:
        logger.info(
            f"[{kol_name}] 已截掉盘面分析段,仅解析交易操作段: "
            f"{len(combined)} -> {len(combined_for_parse)} 字符"
        )

    # ============ 阶段 2: LLM 文本解析（主要方式） ============
    llm_parsed = None  # 初始化,防止未进入LLM分支时引用未定义变量
    if use_llm and combined_for_parse:
        logger.info(f"[{kol_name}] 使用文本 LLM (DEEPSEEK V3) 解析信号")
        try:
            llm_parsed, usage = await parse_with_llm(
                combined_for_parse,
                context=context,
                min_confidence=llm_min_confidence,
            )
            if llm_parsed is not None:
                logger.info(
                    f"[{kol_name}] LLM 解析成功: symbol={llm_parsed.symbol}, "
                    f"side={llm_parsed.side}, confidence={llm_parsed.confidence:.2f}, "
                    f"tokens={usage.get('total_tokens', 0)}"
                )
                llm_parsed.has_image = has_image

                # ★ 关键修复: LLM 判定为无效信号(confidence<=0)时,不运行后续补充检测
                # 这防止叙事/故事类长文本被误判为平仓信号
                # (例如: KOL 发了一篇生活感悟,文中出现"卖了"被模糊模式误匹配)
                if llm_parsed.confidence <= 0:
                    logger.info(
                        f"[{kol_name}] LLM 判定为无效信号(confidence={llm_parsed.confidence:.2f}),"
                        f"跳过平仓/更新信号补充检测"
                    )
                    return llm_parsed

                # LLM 解析后,补充检查是否为平仓信号(LLM prompt 可能未识别模糊表达)
                # 注意: 此检查是保守的,仅在以下条件满足时才覆盖 LLM 的判断:
                #   0. LLM 置信度 > 0 (LLM 判定为有效信号时才补充检测)
                #   1. 文本较短(<200字) — 长文本很可能是复盘/分析
                #   2. 不包含复盘/分析语境关键词
                #   3. check_exit_intent 返回 True
                if not llm_parsed.is_exit_signal and len(combined_for_parse) < 200:
                    is_exit, exit_reason = check_exit_intent(combined_for_parse)
                    if is_exit:
                        llm_parsed.is_exit_signal = True
                        llm_parsed.exit_reason = f"LLM后补充检测: {exit_reason}"
                        # 平仓信号不需要方向/入场价/止盈止损
                        llm_parsed.side = ""
                        llm_parsed.entry_price = None
                        llm_parsed.take_profits = []
                        llm_parsed.stop_loss = None
                        logger.info(f"[{kol_name}] LLM 结果补充标记为平仓信号: {exit_reason}")
                elif not llm_parsed.is_exit_signal and len(combined_for_parse) >= 200:
                    # 长文本仍检查复盘语境,但仅在不包含复盘词时才检测平仓
                    has_review = any(re.search(p, combined_for_parse) for p in REVIEW_INDICATORS)
                    if not has_review:
                        is_exit, exit_reason = check_exit_intent(combined_for_parse)
                        if is_exit:
                            llm_parsed.is_exit_signal = True
                            llm_parsed.exit_reason = f"LLM后补充检测(长文本): {exit_reason}"
                            llm_parsed.side = ""
                            llm_parsed.entry_price = None
                            llm_parsed.take_profits = []
                            llm_parsed.stop_loss = None
                            logger.info(f"[{kol_name}] LLM 结果补充标记为平仓信号(长文本): {exit_reason}")

                # LLM 解析后,补充检查是否为更新信号(LLM prompt 可能未识别)
                # 注意:检查原始文本是否有显式方向/入场价,而非 LLM 推断的结果
                if not llm_parsed.is_exit_signal:
                    is_update, update_reason = check_update_intent(combined_for_parse)
                    if not is_update:
                        # 隐式检测:有止盈/止损但 LLM 未识别出入场价和方向 → 更新信号
                        # 注意:优先信任 LLM 的解析结果,不使用规则解析器覆盖
                        # LLM 能理解自然语言(如"做反弹"=做多),规则解析器可能漏判
                        has_tp_or_sl = bool(llm_parsed.take_profits) or llm_parsed.stop_loss is not None
                        llm_has_no_entry = llm_parsed.entry_price is None and not llm_parsed.entry_prices
                        llm_has_no_side = not llm_parsed.side
                        if has_tp_or_sl and llm_has_no_entry and llm_has_no_side:
                            is_update = True
                            update_reason = "隐式更新信号: 有止盈/止损但无入场价和方向"
                    if is_update:
                        llm_parsed.is_update_signal = True
                        llm_parsed.update_reason = update_reason
                        # 清除 LLM 推断的方向和入场价(更新信号不需要这些)
                        llm_parsed.side = ""
                        llm_parsed.entry_price = None
                        llm_parsed.entry_prices = []
                        logger.info(f"[{kol_name}] LLM 结果补充标记为更新信号: {update_reason}")
                llm_parsed.raw_text = combined
                llm_parsed = apply_actions_to_parsed(combined_for_parse, llm_parsed)
                if llm_parsed.actions == ["hold_pending"]:
                    llm_parsed.confidence = 0.0
                    llm_parsed.reason = "旧挂单状态说明,非新开仓信号"
                if (
                    "cancel_order" in llm_parsed.actions
                    and "refresh_pending" not in llm_parsed.actions
                    and not any(a.startswith("open_") for a in llm_parsed.actions)
                ):
                    llm_parsed.is_exit_signal = False
                    llm_parsed.is_update_signal = False
                    llm_parsed.entry_price = None
                    llm_parsed.entry_prices = []
                    llm_parsed.take_profits = []
                    llm_parsed.stop_loss = None
                    llm_parsed.reason = "撤销未成交挂单"
                return llm_parsed
            else:
                logger.debug(f"[{kol_name}] LLM 未能识别有效信号，回退到规则解析")
        except Exception as e:
            logger.warning(f"[{kol_name}] LLM 解析异常: {e}, 回退到规则解析")


    # ============ 阶段 3: 规则解析（兜底） ============
    if use_llm_fallback or not use_llm:
        parsed = parse_text(combined)
        parsed.has_image = has_image

        # 最终检查
        if (
            not parsed.symbol
            and not parsed.side
            and not parsed.is_exit_signal
            and not parsed.is_update_signal
            and "cancel_order" not in parsed.actions
        ):
            parsed.confidence = 0.0

        return parsed

    # LLM 启用但回退禁用,且 LLM 解析失败 → 返回空结果
    parsed.has_image = has_image
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
