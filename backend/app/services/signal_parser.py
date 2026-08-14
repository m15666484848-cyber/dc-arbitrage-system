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
    "GOLD": "XAU", "XAUUSD": "XAU",
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
    "黄金": "XAU",
    "恒星币": "XLM", "恒星": "XLM", "eos": "EOS", "以太经典": "ETC",
    "经典币": "ETC", "小蚁": "NEO", "本体": "ONT", "量子": "QTUM",
    "小蚁币": "NEO", "本体币": "ONT", "量子币": "QTUM",
    # KOL 常用中文币种俗称(2024-2025 高频)
    "大饼": "BTC", "以太": "ETH", "索拉纳": "SOL", "索尔": "SOL",
    "波卡": "DOT", "柴犬币": "SHIB", "柴犬": "SHIB", "狗币": "DOGE",
    "弗洛基": "FLOKI", "佩佩": "PEPE", "奥迪": "ORDI", "林克": "LINK",
    "萨维亚": "AVAX", "阿普": "APT", "泰波": "TIA", "修": "SEI",
    "皇上": "BTC", "姨太": "ETH", "二饼": "ETH", "蛤蟆鬼": "PEPE",
    "狗庄币": "DOGE", "辣条": "LTC", "太子": "BCH", "小姨太": "ETC",
    " pupper": "WIF",
    # P1-6: 补充 KOL 俗称
    "大阳": "BTC", "大阴": "BTC", "饼子": "BTC", "以太王": "ETH",
    "芝麻开门": "SEI", "太阳": "SOL", "安银": "XMR",
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
    "清仓", "减仓", "减半", "获利了结", "获利平仓",
    "微利出局", "保本出局", "全部止盈出局", "止盈出局", "触发止损出局",
    "全部出局", "全都出局", "止损出局", "打损出局", "成本出局",
    "止损就离场", "止损直接出局", "可以平仓", "可以出来",
    "可以平仓出来", "触发止损价直接出局",  # Round 3: 补充平仓变体
    "close position", "close order", "close all", "close trade",
    "exit", "take profit", "tp hit",
    # P0-1: 英文平仓关键词
    "stopped out", "triggered", "stopped", "flat", "flattened",
    "close long", "close short", "exit position", "exit trade",
    "square up", "out of position", "out of trade", "out of market",
    "tp1", "tp2", "tp3",
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
    r"继续持有到",
    r"持有到",
    r"持有等待",
    r"先别(?:睡觉|动|急|出|跑|平)",
    r"继续拿(?:住|着)",
    r"拿着(?:就行|别动|不动)?",
    r"(?:目前|当前|现在|现|已经|已).{0,12}(?:持有|持仓|拿着|保留).{0,30}(?:多单|空单)",
    r"(?:目前|当前|现在|现|已经|已).{0,12}[一二两三四五六七八九十\d]+\s*(?:个|笔|单|批)?\s*(?:多单|空单).{0,12}(?:在手|持有|持仓|拿着|保留)",
    r"[一二两三四五六七八九十\d]+\s*(?:个|笔|单|批)?\s*(?:多单|空单).{0,12}(?:在手|持有|持仓|拿着|保留)",
    r"(?:持有|持仓|拿着|保留).{0,20}(?:[一二两三四五六七八九十\d]+)?\s*(?:个|笔|单|批)?\s*(?:多单|空单)",
    r"(?:多单|空单).{0,20}(?:持有|持仓|拿着|保留|还在)",
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
    # 第5步: 持仓状态汇报/收益汇报,不是交易指令
    r"(?:当前|目前|现目前|现在).{0,12}(?:[一二两三四五六七八九十\d]+)\s*(?:个|笔|单|批)?\s*(?:多单|空单).{0,20}(?:在手|持仓|持有)",
    r"(?:持仓)?收益(?:分别)?(?:为|是|达到|到了)?.{0,20}(?:%|％|个点|点)",
    r"(?:现目前|目前|当前|现在).{0,12}(?:分别)?(?:盈利|获利|浮盈).{0,20}(?:%|％|个点|点)",
    r"(?:多单|空单).{0,30}(?:分别)?(?:盈利|获利|浮盈).{0,20}(?:%|％|个点|点)",
    r"成本价附近",
    r"当前价格在成本",
    # Round 2: "持仓过夜"+"设置好止盈止损"是提醒性质,非新指令
    r"持仓过夜",
    r"过夜单",
    # Round 3: "持仓观察"/"持仓观望"是建议非指令,即使含"设置好止盈止损"也优先忽略
    r"持仓观察",
    r"持仓观望",
    r"先持仓",
    # Round 3: "均价拉到X"是持仓描述非新开仓
    r"均价(?:应该)?(?:都)?拉到",
    # P0-1: 英文持仓描述/非信号模式
    r"(?i)still\s+in\s+(?:the\s+)?(?:long|short|trade|position)",
    r"(?i)not\s+(?:taken|taking)\s+(?:any\s+)?(?:longs?|shorts?|trades?)",
    r"(?i)not\s+(?:longing|shorting|entering)\s+(?:it\s+)?yet",
    r"(?i)(?:don'?t|do\s+not)\s+open\s+(?:second|another|a\s+new)\s+position",
    r"(?i)shorting\s+isn'?t\s+wise",
    r"(?i)(?:l|s)\s+or\s+(?:l|s)\s+now",  # "L or S now?"
    r"(?i)na[, ]\s*not\s+taken",
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
    r"(?:微利|小赚|小盈|保本|成本|全部止盈|止盈|触发止损|止损|打损)\s*(?:出局|离场|出场|走人|走|平|撤)",
    r"(?:全部|全都|都|直接)?\s*(?:止盈|止损)\s*(?:出局|离场|走人|平仓)",
    # 不玩了/放弃
    r"(?:不玩了|不跟了|放弃|到此为止|到此结束)",
    # 暂时退出 (注意: "观望"单独出现不等于平仓,它表示"等待时机")
    r"暂时\s*(?:退出|离场|撤出)",
    # 保本/止损离场
    r"(?:保本\s*(?:走|出|撤|离场|平)|止损\s*(?:离场|出场|走))",
    # 部分退出/减仓: 统一标记为退出类信号,后续可按仓位比例细化。
    r"(?:减仓|减半|减掉|减去|减持|先减|部分止盈|部分平仓).{0,12}(?:一半|半仓|部分|[一二两三四五六七八九十\d]+成|[0-9]+%)?",
    # "这单/这波 + 动词"
    r"这(?:单|波|次)\s*(?:走|出|跑|撤|平|收|完)",
    # "出来了/走出去了" 等完成态(不匹配"走出区间/走出趋势"等)
    r"(?:出来了|走出来|走出去|跑出来|撤出来)",
    # "先X为敬" 网络用语
    r"先\s*(?:走|撤|跑|出)\s*(?:为敬|一步)",
    # "走一波/撤一波"
    r"(?:走|撤|跑)\s*(?:一波|一下)",
    # P0-2: 触发止损直接出局/止损就离场/可以平仓出来
    # Round 2: 修复逗号/emoji间隔变体 "触发止损，直接出局"
    r"(?:触发止损|止损)[,，\s]*(?:直接|就)?[,，\s]*(?:出局|离场|出场)",
    r"(?:触发止损价)[,，\s]*直接?(?:出局|离场|出场)",
    r"(?:止损)\s*(?:就)?\s*(?:离场|出场|走人|走)",
    r"(?:可以)\s*(?:平仓|出来|出仓|走人|收)",
    r"(?:平仓)\s*(?:出来|出|走)",
    # 英文平仓关键词
    r"(?i)\b(?:stopped\s+out|triggered|take\s+profit|tp\d?|close(?:d)?\s+(?:position|trade|long|short)|exit(?:ed)?\s+(?:position|trade|long|short))\b",
    r"(?i)\b(?:flat(?:ten)?(?:ed)?|square(?:d)?\s+up|out\s+of\s+(?:the\s+)?(?:position|trade|market))\b",
]


def _detect_cn_direction_priority(text: str) -> str:
    """中文方向词最高优先级，用于避免 short-term/stop loss 等英文片段干扰方向。"""
    if not text:
        return ""

    patterns: list[tuple[str, str]] = [
        ("short", r"反弹\s*(?:到|至)?\s*阻力\s*(?:位|附近)?\s*(?:空|做空|开空)?"),
        ("short", r"阻力\s*(?:位|附近)?.{0,8}(?:空|做空|开空)"),
        ("long", r"做多\s*(?:go\s*long)?"),
        ("short", r"做空\s*(?:go\s*short)?"),
        ("long", r"(?:方向\s*[:：]?\s*)?(?:做多|开多|进多|接多|挂多|多单|低多|低吸|逢低做多|逢低接|抄底)"),
        ("short", r"(?:方向\s*[:：]?\s*)?(?:做空|开空|进空|接空|挂空|空单|高空|逢高做空|反弹空|阻力空)"),
        ("long", r"1\s*倍\s*(?:多|做多)"),
        ("short", r"1\s*倍\s*(?:空|做空)"),
    ]
    hits: list[tuple[int, str]] = []
    for side, pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            hits.append((match.start(), side))
    if not hits:
        return ""
    hits.sort(key=lambda item: item[0])
    return hits[-1][1]


def _apply_cn_direction_override(text: str, parsed: ParsedSignal) -> ParsedSignal:
    """LLM 返回后按中文明确方向词修正方向，避免做多 go long 被误判为空。"""
    if parsed.is_exit_signal or parsed.is_update_signal:
        return parsed
    forced_side = _detect_cn_direction_priority(text)
    if forced_side in ("long", "short") and parsed.side and parsed.side != forced_side:
        logger.warning(
            f"中文方向优先覆盖 LLM 方向: {parsed.side} -> {forced_side}, text={text[:80]}"
        )
        parsed.side = forced_side
        parsed.reason = (parsed.reason + "; " if parsed.reason else "") + "中文方向词优先覆盖"
        parsed.actions = [f"open_{forced_side}"]
        parsed.action = parsed.actions[0]
    return parsed


def detect_side(text: str) -> str:
    """识别交易方向(long/short)。

    匹配策略:
    1. 优先匹配完整词组(做多/做空/多单/空单等)
    2. LONG 和 SHORT 同时检查,取更精确的匹配(词组优先于单词)
    3. 如果同时匹配到多和空,取最后出现的方向(通常"建议空单"中空单是最终建议)
    """
    forced_side = _detect_cn_direction_priority(text)
    if forced_side:
        return forced_side

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
            r"(?:^|[\s，,。:：])多(?!空)\s*(?=(?:第?\s*[一二两三四五六七八九十\d]+\s*(?:批|笔|次)|分批|建仓|入场|进场|挂单|开仓|止损|止盈|目标|\d))",
            text,
        )
        single_short = re.search(
            r"(?:^|[\s，,。:：])空(?!单军头)\s*(?=(?:第?\s*[一二两三四五六七八九十\d]+\s*(?:批|笔|次)|分批|建仓|入场|进场|挂单|开仓|止损|止盈|目标|\d))",
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

    # Round 5: 已触及 TP 后让剩余仓位继续持有，是持仓描述/复盘，不是新的平仓指令。
    if re.search(r"(?i)\bi\s+hit\s+tp\s*1?\b.{0,80}\blet\s+the\s+rest\b", text):
        return False, "Round5英文TP1持仓描述: let the rest"
    # Round 5 标注修正: 上周挂单/策略已触发 + 今日另一个方向仍有效，是历史触发描述，不是新平仓。
    if re.search(r"(?i)\bshort\s+from\s+last\s+week\b.{0,80}\btriggered\b.{0,80}\blong\s+is\s+still\s+valid\b", text):
        return False, "Round5英文历史触发描述: last week triggered"

    # Round 4: 明确平仓/部分止盈动作不受复盘、恭喜、收益汇报语气影响。
    # 例如“恭喜...止盈出局”“获利1540点...止盈离场”“移动止盈30%”
    # 都是对已有仓位的实际退出/减仓指令，不能被 REVIEW_INDICATORS 误过滤。
    round4_forced_exit_patterns = [
        r"(?:多单|空单|比特|以太|BTC|ETH).{0,12}(?:出掉|出局)",
        r"(?:多单|空单).{0,8}(?:全部)?止盈(?:吧|了|掉|出局)?",
        r"(?:多单|空单).{0,8}止损(?!\s*(?:上移|下移|改|设|设置|放|移|保护|到|至|推到|拉到))\s*\d",
        r"(?:全部|全都|短线全部)\s*止盈(?:吧|了|出局)?",
        r"💰.{0,12}(?:多单|空单).{0,8}(?:出掉|止盈)💰",
        r"止盈\s*出局",
        r"止盈\s*离场",
        r"止损\s*出局",
        r"触发\s*止损价?\s*直接\s*出局",
        r"移动\s*止盈\s*\d+(?:\.\d+)?\s*[%％]",
        r"(?:^|[，,。\n\s])止盈\s*\d+(?:\.\d+)?\s*[%％]",
        r"剩余\s*持仓\s*止盈\s*(?:还是)?\s*看\s*\d",
        r"止盈\s*\d+(?:\.\d+)?\s*[%％].{0,40}止损位\s*(?:移至|移动至|移到|移)",
    ]
    for pattern in round4_forced_exit_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True, f"Round4明确平仓/部分止盈命中: {pattern}"

    # 第零层:复盘/分析语境排除 (优先级最高)
    # 如果文本包含复盘/回顾性表达,则不视为平仓信号
    for pattern in REVIEW_INDICATORS:
        if re.search(pattern, text):
            return False, f"复盘/分析语境排除: {pattern}"

    # 第一层:精确关键词
    for w in EXIT_WORDS:
        if w.isascii():
            if re.search(rf"\b{re.escape(w)}\b", low):
                return True, f"检测到平仓关键词: {w}"
        else:
            if w in low:
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
    rf"(?:止盈位|止盈点位|止盈价|目标位|目标价).{{0,12}}(?:重设|重置|重新设置|改为|改成|调为|调到|调整为|修改为|更新为)",
    rf"修改\s*止损", rf"调整\s*止损", rf"止损\s*{UPDATE_VERBS}",
    rf"(?:止损位|止损点位|止损价|止损线)\s*{UPDATE_VERBS}",
    rf"(?:止损|止损位|止损点位|止损价|止损线).{{0,12}}(?:重设|重置|重新设置)\s*(?:为|到)?",
    rf"修改\s*止盈止损", rf"调整\s*止盈止损", rf"更新\s*止盈止损",
    rf"止盈止损\s*{UPDATE_VERBS}",
    # 新增: 目标/TP/SL 更新模式
    rf"修改\s*目标", rf"调整\s*目标", rf"目标\s*{UPDATE_VERBS}",
    rf"移\s*止损", rf"移\s*止盈",
    rf"上移\s*止损", rf"下移\s*止损", rf"上移\s*止盈", rf"下移\s*止盈",
    rf"推\s*止损", rf"拉\s*止损", rf"止损\s*保护",
    rf"推\s*保护价|推\s*保护|保护价(?:下来|上去|到|至)?|保护成本|保护利润",
    rf"保本(?:止损)?", rf"保护(?:利润|本金)",
    rf"止盈(?:先看|看到|看至|看向)",
    rf"\bTP\s*{UPDATE_VERBS}", rf"\bSL\s*{UPDATE_VERBS}",
    # Round 2: 补充更新信号变体
    rf"移动\s*止损", rf"移动\s*止盈",  # "移动止损至开仓价"
    rf"设置好?\s*止损", rf"设置好?\s*止盈",  # "设置好止损价：1830"
    rf"止损\s*(?:至|到)\s*开仓价",  # "止损至开仓价"=保本止损
    # Round 3: "止损，我改成X" / "止损改为X" / "止损位下移X点，重设为Y"
    rf"止损\s*[,，]\s*(?:我)?\s*(?:改成|改为|改到)",  # "止损，我改成0.506"
    rf"止损位\s*下移.*重设",  # "止损位下移500点，重设为63300"
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
# 第7步: 信号分类与优先级排序。
# 说明: 一条 KOL 消息可能同时包含平仓/撤单/更新/开仓。
# 主操作按风险从高到低处理,避免把风险更高的退出/撤单类信号误排到开仓后面。
ACTION_PRIORITY = {
    "close_position": 10,
    "cancel_order": 20,
    "update_tp_sl": 30,
    "refresh_pending": 40,
    "hold_pending": 50,
    "open_short": 60,
    "open_long": 60,
}


def _sort_signal_actions(actions: list[str]) -> list[str]:
    """按统一动作优先级排序并去重,保证 parsed.action 是最高优先级主操作。"""
    deduped: list[str] = []
    for action in actions:
        if action and action not in deduped:
            deduped.append(action)
    return sorted(deduped, key=lambda a: ACTION_PRIORITY.get(a, 999))


# 撤挂单关键词: 只代表取消未成交挂单,不等于反向开仓。
CANCEL_ORDER_PATTERNS = [
    r"\bcancel\s+(?:order|orders|limit|limits)\b",
    r"\bcancel\s+pending\b",
    r"(?i)\bcancel(?:led)?\s+(?:the\s+)?(?:order|trade|position|limit)\b",
    r"(?i)\bb(?:order|trade)\s+cancel(?:led)?\b",
    r"^\s*撤[了掉]?\s*$",
    r"撤单",
    r"撤\s*不挂了",
    r"撤销(?:挂单|订单|委托)?",
    r"取消(?:挂单|订单|委托)",
    r"不挂了",
    r"没必要挂了|没必要再挂|不必要挂了|无需挂了",
    r"别挂了|不用挂了|先不挂|暂不挂",
    r"(?:这单|这个单|这个订单|这个策略|订单|策略).{0,8}(?:不要了|作废|取消)",
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
    r"\b(?:longed|shorted|longing|shorting)\b",
    r"\b(?:Short|Long)\s*#?\s*\d+\b",  # "Short #1" / "Long 1"
    r"开多|开空|进多|进空|做多|做空|买入|卖出",
    r"入场|进场|下单|建仓|上车|上车了|搞一波|搞一下|干一波",
    r"挂多|挂空|挂入|挂单|委托|埋伏|埋伏单|抄底单|摸顶单",
    r"挂(?:一个|个|一笔|一单)?(?:反弹|回踩)?",
    r"挂\s*\d+(?:\.\d+)?\s*(?:附近|一线|位置)?",
    r"重新挂|再挂|新挂|补挂|重新进|再次进",
    r"换手\s*(?:做多|做空)",  # P1-5: "换手做多"短信号
]

# 撤单消息里只有出现这些词,才允许"撤旧单后重新开/重新挂"。
# 普通"建仓/方向/止损止盈"在撤单消息中通常是在描述要取消的旧挂单参数。
REOPEN_AFTER_CANCEL_PATTERNS = [
    r"重新挂|再挂|新挂|补挂",
    r"重新进|再次进|再开|重新开",
    r"重新改(?:这个|一下|个)?",  # P0-4: "重新改这个"=新开仓,非更新
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
    """是否包含交易参数块:方向 + 建仓/入场 + 风控字段或明确价格。"""
    if not text:
        return False
    has_side = bool(re.search(r"方向\s*[:：]\s*(?:多|空)|做多|做空|开多|开空|多单|空单|long|short", text, re.IGNORECASE))
    has_entry = bool(re.search(r"建仓|入场|进场|挂单|entry|buy\s*zone|sell\s*zone", text, re.IGNORECASE))
    has_risk = bool(re.search(r"止损|止盈|\bSL\b|\bTP\b", text, re.IGNORECASE))
    has_price = bool(re.search(r"\d+(?:[.,]\d+)?(?:\s*[-~—至到]\s*\d+(?:[.,]\d+)?)?", text))
    return has_side and has_entry and (has_risk or has_price)


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
    if re.search(r"(?i)\bfrom\s+last\s+week\b.{0,100}\btriggered\b.{0,100}\bstill\s+valid\b", text):
        return "analysis", "Round5历史触发/旧策略仍有效描述"
    # Round 4: 文章标题/新闻评论类内容优先过滤，避免“交易思路/能否幸免”
    # 这类分析文中出现“多/空/突破/支撑”等词被误当成开仓。
    if re.search(r"(?:交易思路|盘面分析|行情分析|走势分析)", text) and re.search(
        r"(?:\d+\s*月\s*\d+\s*日|4小时级别|上升通道|箱体震荡|盘面分析|交易思路)",
        text,
        re.IGNORECASE,
    ):
        if not re.search(r"(?:建仓|进场|入场|委托|挂单|现价\s*(?:做多|做空)|直接\s*(?:做多|做空|进场))", text):
            return "analysis", "Round4分析文章标题/盘面分析过滤"
    if re.search(r"(?:能否幸免|咱们来看下|大炮一响|华尔街晚上即将开盘|美股盘前|黄金.*涨到)", text):
        if not re.search(r"(?:建仓|进场|入场|委托|挂单|现价\s*(?:做多|做空)|直接\s*(?:做多|做空|进场))", text):
            return "analysis", "Round4新闻评论/宏观分析过滤"
    if re.search(r"[A-Za-z]{2,12}|[\u4e00-\u9fff]{1,12}", text) and re.search(r"一个不错的做(?:多|空)时机", text):
        if not re.search(r"(?:建仓|进场|入场|委托|挂单|止盈|止损|\d{2,})", text):
            return "analysis", "Round5做多/做空时机观点过滤"
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


def detect_recent_cancel_context(recent_texts: list[str] | None) -> tuple[bool, str]:
    """检测最近消息中是否存在短期撤单上下文。

    用于处理 KOL 分开发消息的场景:
    ① "撤，不挂了"
    ② 紧接着复制原策略参数

    第二条的完整策略参数应作为旧挂单定位信息,不能被当成新开仓。
    recent_texts 由调用方保证只传短时间窗口内的同 KOL 历史消息。
    """
    for raw in reversed(recent_texts or []):
        text = strip_analysis_sections(raw or "").strip()
        if not text:
            continue
        if _has_any_pattern(text, CANCEL_ORDER_PATTERNS) and not _has_any_pattern(text, REOPEN_AFTER_CANCEL_PATTERNS):
            return True, f"近期撤单消息: {text[:80]}"
    return False, ""


def apply_cancel_context_if_needed(
    text: str,
    parsed: ParsedSignal,
    recent_texts: list[str] | None = None,
) -> ParsedSignal:
    """在撤单上下文中,把后续完整策略单改判为 cancel_order。

    保守原则:
    - 当前消息已经明确"重新挂/再挂/新挂"时,不拦截新开仓。
    - 只有当前解析结果本来会开仓时,才改判为撤单定位信息。
    - 保留 symbol/side/entry/tp/sl,方便下游精确匹配并取消 pending 单。
    """
    if not recent_texts:
        return parsed
    if _has_any_pattern(text, REOPEN_AFTER_CANCEL_PATTERNS):
        return parsed
    if "cancel_order" in (parsed.actions or []):
        return parsed

    would_open = any(a.startswith("open_") for a in (parsed.actions or []))
    if not would_open:
        return parsed

    has_cancel_context, context_reason = detect_recent_cancel_context(recent_texts)
    if not has_cancel_context:
        return parsed

    parsed.actions = ["cancel_order"]
    parsed.action = "cancel_order"
    parsed.is_exit_signal = False
    parsed.is_update_signal = False
    parsed.reason = (
        f"撤单上下文命中: {context_reason}; "
        "当前完整策略作为旧挂单定位参数,未执行新开仓"
    )
    parsed.confidence = max(parsed.confidence, 0.75)
    return parsed


POSITION_CONTEXT_PATTERNS = [
    r"(?:目前|当前|现在|现|已经|已).{0,12}(?:持有|持仓|拿着|保留).{0,40}(?:多单|空单|long|short)",
    r"(?:持有|持仓|拿着|保留).{0,24}(?:[一二两三四五六七八九十\d]+)?\s*(?:个|笔|单|批)?\s*(?:多单|空单|long|short)",
    r"(?:多单|空单|long|short).{0,24}(?:持有|持仓|拿着|保留|还在)",
]

POSITION_FOLLOW_UP_PATTERNS = [
    r"(?:没|未|还没|没有).{0,10}(?:进|入场|上车|跟|跟上|布局).{0,18}(?:可以|现在|现价|这里|直接)?.{0,10}(?:跟进|进|入场|上车|跟|补)",
    r"(?:可以|现在|现价|这里|直接).{0,10}(?:跟进|进场|入场|上车|跟上|补进)",
    r"(?:跟进|上车|进场|入场|补进)(?:即可|就行|吧)?\s*$",
    r"(?:前面|之前|刚才|所长给的|给的).{0,24}(?:多单|空单|单子|策略).{0,24}(?:再开一次|再开|重新开|再次开|再进一次|重新进)",
    r"(?:再开一次|再开|重新开|再次开|再进一次|重新进)(?:即可|就行|吧)?\s*$",
]


def _extract_position_context_prices(text: str) -> list[float]:
    """从持仓说明中提取入场/分批价格,过滤批次数、杠杆、百分比等非价格数字。"""
    if not text:
        return []

    # 持仓上下文里如果同时提到 TP/SL,只取其之前的价格作为入场/持仓成本。
    segment = re.split(r"(?:止盈|止损|\btp\b|\bsl\b|目标)", text, maxsplit=1, flags=re.IGNORECASE)[0]
    prices: list[float] = []
    for m in re.finditer(PRICE_RE, segment):
        raw = m.group(1)
        p = _to_float(raw)
        if p is None or p <= 0:
            continue

        before = segment[max(0, m.start() - 4):m.start()]
        after = segment[m.end():m.end() + 4]
        if m.end() < len(segment) and segment[m.end()] == "万":
            p *= 10000

        # "三个空单/3笔/第2批/5x/10%" 不是价格。
        if re.search(r"(?:第\s*)?$", before) and re.match(r"\s*(?:个|笔|单|批|层)", after):
            continue
        if re.match(r"\s*(?:x|X|倍|%)", after):
            continue
        if p <= 20 and re.search(r"(?:个|笔|单|批|层|第)\s*$", before + after):
            continue

        if p not in prices:
            prices.append(p)
    return prices


def extract_position_context(text: str) -> ParsedSignal | None:
    """提取"当前持有/目前持仓"类消息中的可跟进上下文。

    这类消息本身不触发下单,仅供紧随其后的"没进可跟进"等短消息补全。
    """
    cleaned = strip_analysis_sections(text or "").strip()
    if not cleaned or not _has_any_pattern(cleaned, POSITION_CONTEXT_PATTERNS):
        return None

    symbol = extract_symbol(cleaned)
    side = detect_side(cleaned)
    if not side:
        if re.search(r"多单|long", cleaned, re.IGNORECASE):
            side = "long"
        elif re.search(r"空单|short", cleaned, re.IGNORECASE):
            side = "short"

    entry, entry_prices = extract_entry(cleaned)
    if not entry_prices:
        entry_prices = _extract_position_context_prices(cleaned)
        entry = entry_prices[0] if entry_prices else entry

    if not symbol or side not in ("long", "short"):
        return None

    return ParsedSignal(
        symbol=symbol,
        side=side,
        entry_price=entry,
        entry_prices=entry_prices,
        take_profits=extract_take_profits(cleaned),
        stop_loss=extract_stop_loss(cleaned),
        raw_text=text or "",
        confidence=0.0,
        reason="持仓上下文,等待后续跟进话术确认",
    )


def is_position_follow_up_text(text: str) -> bool:
    """识别"没进的可以跟进/现价跟进/上车"这类补跟指令。"""
    cleaned = strip_analysis_sections(text or "").strip()
    if not cleaned:
        return False
    if _has_any_pattern(cleaned, CANCEL_ORDER_PATTERNS):
        return False
    if _has_any_pattern(cleaned, UPDATE_KEYWORDS):
        return False
    is_exit, _ = check_exit_intent(cleaned)
    if is_exit:
        return False
    return _has_any_pattern(cleaned, POSITION_FOLLOW_UP_PATTERNS)


def apply_position_context_if_needed(
    text: str,
    parsed: ParsedSignal,
    recent_texts: list[str] | None = None,
) -> ParsedSignal:
    """用近期持仓说明补全后续跟进短消息。

    保守原则:
    - 只有当前消息明确出现"跟进/上车/没进可进"时才补全。
    - 只使用调用方传入的同 KOL 短时间窗口 recent_texts。
    - 上下文消息本身仍是非交易信号,不会直接开仓。
    """
    if not recent_texts or not is_position_follow_up_text(text):
        return parsed
    if parsed.is_exit_signal or parsed.is_update_signal or "cancel_order" in (parsed.actions or []):
        return parsed

    context_signal: ParsedSignal | None = None
    context_text = ""
    for raw in reversed(recent_texts or []):
        context_signal = extract_position_context(raw)
        if not context_signal:
            # "再开一次"常引用前面完整策略单,而不是"当前持有"说明。
            # 这里保守复用最近同 KOL 的明确开仓信号参数,补齐价格/止损/止盈。
            candidate = parse_text(raw or "")
            if (
                candidate
                and candidate.action in ("open_long", "open_short")
                and candidate.symbol
                and candidate.side in ("long", "short")
                and (candidate.entry_price is not None or candidate.entry_prices)
            ):
                context_signal = candidate
        if context_signal:
            context_text = strip_analysis_sections(raw or "").strip()
            break
    if not context_signal:
        return parsed

    symbol = parsed.symbol or context_signal.symbol
    side = parsed.side or context_signal.side
    if not symbol or side not in ("long", "short"):
        return parsed

    entry_price = parsed.entry_price if parsed.entry_price is not None else context_signal.entry_price
    entry_prices = parsed.entry_prices or context_signal.entry_prices
    parsed.symbol = symbol
    parsed.side = side
    parsed.entry_price = entry_price
    parsed.entry_prices = entry_prices
    parsed.take_profits = parsed.take_profits or context_signal.take_profits
    parsed.stop_loss = parsed.stop_loss if parsed.stop_loss is not None else context_signal.stop_loss
    parsed.actions = [f"open_{side}"]
    parsed.action = parsed.actions[0]
    parsed.is_exit_signal = False
    parsed.is_update_signal = False
    parsed.reason = f"持仓上下文跟进命中: {context_text[:80]}"
    parsed.confidence = max(parsed.confidence, 0.75 if entry_price or entry_prices else 0.65)
    return parsed



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

    # 撤挂单语境下,"多单撤了/空单撤了"通常是取消未成交挂单,不是平仓。
    # 但如果文本同时有明确平仓词,仍允许 close_position 通过后续优先级排序成为主操作。
    if has_cancel:
        explicit_exit, _ = check_exit_intent(text)
        if not explicit_exit:
            is_exit = False

    if has_cancel:
        actions.append("cancel_order")
    if is_exit:
        actions.append("close_position")
    if is_update:
        actions.append("update_tp_sl")
    # P0-3/P0-4: 有明确开仓词+完整交易参数块时,优先判 open(即使同时有撤单)
    # 这解决"撤，不挂了 + Btc方向：多 建仓：64700"混合信号和"方向：空 建仓：65600"被误判refresh_pending的问题
    if side in ("long", "short") and (
        (has_explicit_open and (has_trade_block or not has_cancel or has_reopen_after_cancel))
        or (has_cancel and has_trade_block)
    ):
        actions.append(f"open_{side}")
    # refresh_pending 仅用于:有挂单状态描述+开仓词,但无完整交易参数块(只是刷新旧单状态)
    elif not actions and side in ("long", "short") and has_pending_status and has_explicit_open and not has_trade_block:
        actions.append("refresh_pending")
    elif not actions and has_pending_status and not has_explicit_open and not has_cancel:
        actions.append("hold_pending")

    return _sort_signal_actions(actions)


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
    actions = _sort_signal_actions(actions)
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
    tps = _extract_prices_after(text, [r"\btp\b", r"tp\s*位", r"take\s*profit"])
    if tps:
        return tps

    # 2.2. 更新型止盈价: "止盈位重设为64900" / "目标价改为64900"。
    reset_tp_patterns = [
        rf"(?:止盈点位|止盈位|止盈价|止盈线|止盈|目标位|目标价|目标|\bTP\b).{{0,30}}(?:重设|重置|重新设置|设置|改为|改成|调为|调到|调整为|修改为|更新为)\s*(?:为|到)?\s*{PRICE_RE}\s*(?:万)?",
        rf"(?:重设|重置|重新设置|设置|改为|改成|调为|调到|调整为|修改为|更新为)\s*(?:止盈点位|止盈位|止盈价|止盈线|止盈|目标位|目标价|目标|\bTP\b)\s*(?:为|到)?\s*{PRICE_RE}\s*(?:万)?",
    ]
    for pat in reset_tp_patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            p = _to_float(m.group(1))
            if p and p > 0:
                if "万" in m.group(0):
                    p *= 10000
                tps.append(p)
    if tps:
        return tps

    # 3.6. 中文编号目标: 目标1/目标2/目标3 (KOL 常用格式)
    for i in range(1, 6):
        # 避免把 "目标 150" 误识别为 "目标1: 50"。
        # 编号目标要求数字编号后不能紧跟其它数字。
        for m in re.finditer(rf"目标\s*{i}(?!\d)\s*[:：]?\s*{PRICE_RE}\s*(?:万)?", text):
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
    tp_match = re.search(r"(?:止盈点位|止盈位|止盈价|止盈|目标点位|目标位|目标价|目标)\s*[:：]?\s*(.*?)(?=\n|$|备注|提示)", text)
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
    tps = _extract_prices_after(text, [r"止盈点位", r"止盈位", r"止盈价", r"止盈", r"目标点位", r"目标价", r"目标位", r"目标", r"target\s*price", r"price\s*target"])
    return tps


def extract_stop_loss(text: str) -> float | None:
    # 关键词列表
    keywords = [
        r"止损点位", r"止损位", r"止损价", r"止损线", r"止损", r"防守位", r"防守",
        r"stop\s*loss", r"cut\s*loss", r"\bsl\b", r"\bstop\b",
        r"invalidation", r"invalid",
    ]

    # 更新型绝对止损价优先: "止损位下移500点，重设为63300" 应取 63300,
    # 不能把中间的 "500点" 当成新的止损价。
    reset_sl_patterns = [
        rf"(?:止损点位|止损位|止损价|止损线|止损|\bSL\b).{{0,30}}(?:重设|重置|重新设置|设置|改为|改成|调为|调到|调整为|修改为|更新为)\s*(?:为|到)?\s*{PRICE_RE}\s*(?:万)?",
        rf"(?:重设|重置|重新设置|设置|改为|改成|调为|调到|调整为|修改为|更新为)\s*(?:止损点位|止损位|止损价|止损线|止损|\bSL\b)\s*(?:为|到)?\s*{PRICE_RE}\s*(?:万)?",
    ]
    for pat in reset_sl_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            p = _to_float(m.group(1))
            if p and p > 0:
                if "万" in m.group(0):
                    p *= 10000
                return p

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
        r"现价", r"当前价", r"市价",
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

    # 特殊模式: "X做多" / "X开多" / "X 多 止损..." / "X 空 目标..." 等 (价格 + 方向)
    # KOL 常写 "1870做多" "65000 多 止损63000" 这种格式,前面没有入场关键词。
    price_action_pat = rf"(?<!止盈)(?<!止损)(?<!目标)(?<!止盈点位)(?<!止损点位){PRICE_RE}\s*(?:万)?\s*(?:做多|做空|开多|开空|买入|卖出|做多单|做空单|做多|做空|多(?!空)|空(?!单军头))"
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
    m = re.search(r"(\d+)\s*[xX倍](?=\s|杠杆|仓|倍|做多|做空|多|空|$)", text)
    if m:
        return max(1, min(int(m.group(1)), 125))
    return 1


def extract_position_pct(text: str) -> float:
    m = re.search(r"仓位\s*[:：]?\s*(\d+(?:\.\d+)?)\s*[%％]|(\d+(?:\.\d+)?)\s*[%％]\s*仓位", text)
    if m:
        return max(0.0, min(float(m.group(1) or m.group(2)), 100.0))
    m = re.search(r"position\s*[:：]?\s*(\d+(?:\.\d+)?)\s*[%％]", text, re.IGNORECASE)
    if m:
        return max(0.0, min(float(m.group(1)), 100.0))

    # 部分平仓/部分止盈比例:
    # "止盈80%"、"分批止盈80%"、"先出30%"、"减仓50%" 都是平仓比例,
    # 不能被当作止盈价格 80。
    partial_patterns = [
        r"(?:止盈|止盈出|分批止盈|部分止盈|tp|take\s*profit).{0,8}?(\d+(?:\.\d+)?)\s*[%％]",
        r"(?:减仓|减持|先出|出掉|出|平掉|平仓|平).{0,8}?(\d+(?:\.\d+)?)\s*[%％]",
        r"(\d+(?:\.\d+)?)\s*[%％].{0,8}(?:止盈|减仓|先出|出掉|平掉|平仓)",
    ]
    for pat in partial_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return max(0.0, min(float(m.group(1)), 100.0))

    if re.search(r"(?:止盈|先出|出掉|减仓|平掉|平仓).{0,8}(?:一半|半仓|50[%％])|(?:减半|出一半|止盈一半)", text):
        return 50.0

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
        "XAU",
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

    # 下载图片 (L-4修复: 添加大小限制和内容类型验证)
    MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB 上限
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, max_redirects=3) as client:
            resp = await client.get(image_url)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if not content_type.startswith("image/"):
                logger.warning(f"图片 URL 返回非图片内容类型: {content_type}")
                return """
            if len(resp.content) > MAX_IMAGE_SIZE:
                logger.warning(f"图片过大 ({len(resp.content)} bytes), 跳过 OCR")
                return """
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

            # 提取所有识别到的文本 (L-4修复: 更安全的空值处理)
            texts = []
            if result and len(result) > 0 and result[0]:
                for line in result[0]:
                    try:
                        if line and len(line) >= 2 and line[1]:
                            texts.append(line[1][0])
                    except (IndexError, TypeError):
                        continue

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
def _is_pure_discord_cdn_url(text: str) -> bool:
    """Round 7: 纯 Discord/CDN 附件 URL 不是交易信号。"""
    if not text:
        return False
    stripped = text.strip()
    if not re.fullmatch(r"https?://\S+", stripped, re.IGNORECASE):
        return False
    return bool(re.search(
        r"(?:cdn\.discordapp\.com|media\.discordapp\.net|discord(?:app)?\.com/(?:attachments|channels))",
        stripped,
        re.IGNORECASE,
    ))


def _parse_round7_english_sl_be(text: str, raw_text: str) -> ParsedSignal | None:
    """Round 7: 英文 SL/BE 止损保本与止盈/保本选择。"""
    if not text:
        return None
    if re.search(r"(?i)\b(?:take|book)\b.{0,30}\bor\b.{0,30}\bmove\s+sl\s+to\s+be\b", text):
        return ParsedSignal(
            raw_text=raw_text,
            confidence=0.75,
            is_exit_signal=True,
            exit_reason="Round7 English Take or move SL to BE",
            actions=["close_position", "update_tp_sl"],
            action="close_position",
        )
    if re.search(r"(?i)\b(?:sl|stop\s*loss)\s+to\s+(?:be|breakeven)\b|\bmove\s+sl\s+(?:to\s+be|to\s+breakeven|up)\b|\bsl\s+to\s+be\s+please\b", text):
        return ParsedSignal(
            raw_text=raw_text,
            confidence=0.75,
            is_update_signal=True,
            update_reason="Round7 English SL moved to breakeven",
            actions=["update_tp_sl"],
            action="update_tp_sl",
        )
    if re.search(r"(?i)\bsl\s+to\s+be\s+or\s+book\b", text):
        return ParsedSignal(
            raw_text=raw_text,
            confidence=0.75,
            is_update_signal=True,
            update_reason="Round7 English SL to BE or Book",
            actions=["update_tp_sl"],
            action="update_tp_sl",
        )
    return None


def _extract_round7_open_entry(text: str) -> tuple[float | None, list[float]]:
    """Round 7: 提取英文/中英混合开仓短句中的入场价。"""
    patterns = [
        rf"(?i)\bentry\s*[:：]?\s*{PRICE_RE}(?:\s*[-~至到]\s*{PRICE_RE})?",
        rf"(?i)go\s+(?:long|short)\s*[:：]?\s*{PRICE_RE}(?:\s*[-~至到]\s*{PRICE_RE})?",
        rf"(?i)\bready\s+to\s+short\b.*?{PRICE_RE}",
        rf"{PRICE_RE}\s*(?:直接空|直接多)",
        rf"(?i){PRICE_RE}\s*break\s*down\s*=?\s*ready\s+to\s+short",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if not m:
            continue
        matched = m.group(0)
        prices: list[float] = []
        for pm in re.finditer(PRICE_RE, matched):
            p = _to_float(pm.group(1))
            if p and p > 0:
                if "万" in matched:
                    p *= 10000
                if p not in prices:
                    prices.append(p)
        if prices:
            return prices[0], prices
    return extract_entry(text)


def _parse_round7_open_priority(text: str, raw_text: str) -> ParsedSignal | None:
    """Round 7: 开仓结构优先于止盈/止损/TP 等平仓词。"""
    if not text:
        return None
    has_entry_structure = bool(re.search(r"(?i)\bentry\b|go\s+(?:long|short)|swing\s+movement|进场点位|入场点位|建仓|直接空|直接多|ready\s+to\s+short|break\s*down", text))
    has_risk_params = bool(re.search(r"(?i)\btp\b|\bsl\b|take\s*profit|stop\s*loss|止盈|止损", text))
    has_direction = bool(re.search(r"(?i)go\s+short|\bready\s+to\s+short\b|\bshort\b|做空|直接空|空单|做多|直接多|go\s+long|\blong\b|swing\s+movement", text))
    if not (has_entry_structure and has_direction and (has_risk_params or re.search(PRICE_RE, text))):
        return None

    side = detect_side(text)
    if not side:
        if re.search(r"(?i)\bshort\b|做空|直接空|ready\s+to\s+short|break\s*down", text):
            side = "short"
        elif re.search(r"(?i)\blong\b|go\s+long|做多|直接多|swing\s+movement", text):
            side = "long"
    if side not in ("long", "short"):
        return None

    entry, entry_prices = _extract_round7_open_entry(text)
    if entry is None and not entry_prices:
        return None
    symbol = extract_symbol(text)
    if symbol == "BREAK/USDT" and re.search(r"(?i)break\s*down|ready\s+to\s+short", text):
        symbol = ""
    tps = extract_take_profits(text)
    sl = extract_stop_loss(text)
    confidence = 0.75
    if symbol:
        confidence += 0.1
    if sl or tps:
        confidence += 0.1
    return ParsedSignal(
        symbol=symbol,
        side=side,
        entry_price=entry,
        entry_prices=entry_prices,
        take_profits=tps,
        stop_loss=sl,
        raw_text=raw_text,
        confidence=min(confidence, 0.95),
        actions=[f"open_{side}"],
        action=f"open_{side}",
        reason="Round7开仓结构优先于TP/SL平仓词",
    )


def _parse_round4_add_more(text: str, raw_text: str) -> ParsedSignal | None:
    """Round 4: 处理英文 Add more / 加仓短消息。

    规则:
    - "#XXX Add more" + TP/SL 目标: 加仓后的止盈止损目标,不是平仓。
    - "#XXX Add more / 继续加仓": 明确继续加仓,默认按做多加仓。
    - "#XXX ADD MORE" 但无价格、无方向、无 TP/SL: 信息不足,忽略。
    """
    if not re.search(r"(?i)\badd\s+more\b|继续加仓|加仓更多", text):
        return None

    tag_match = re.search(r"#\s*([A-Za-z0-9]{2,20})\b", text)
    if not tag_match:
        return ParsedSignal(raw_text=raw_text, confidence=0.0, reason="Add more 无明确品种名")

    symbol = normalize_symbol(tag_match.group(1))
    has_tp_sl = bool(re.search(r"(?i)\b(?:tp|sl)\s*\d*\s*[:：]?\s*\d", text)) or bool(re.search(r"(?:止盈|止损)\s*\d", text))
    has_price = bool(re.search(PRICE_RE, re.sub(r"#\s*[A-Za-z0-9]{2,20}\b", "", text)))
    has_explicit_continue = bool(re.search(r"继续加仓", text))
    has_side = bool(re.search(r"(?i)\b(?:long|buy|short|sell|bullish|bearish)\b|做多|做空|多单|空单|看涨|看跌", text))

    # "#AKE ADD MORE / 加仓更多" 这类只有加仓口号,没有价格/方向/TP/SL,不可执行。
    if not (has_tp_sl or has_price or has_explicit_continue or has_side):
        return ParsedSignal(
            symbol=symbol,
            raw_text=raw_text,
            confidence=0.0,
            reason="Add more 缺少方向、价格或 TP/SL,信息不足",
        )

    side = detect_side(text) or "long"
    entry, entry_prices = extract_entry(text)
    tps = extract_take_profits(text)
    sl = extract_stop_loss(text)
    return ParsedSignal(
        symbol=symbol,
        side=side,
        entry_price=entry,
        entry_prices=entry_prices,
        take_profits=tps,
        stop_loss=sl,
        raw_text=raw_text,
        confidence=0.75 if has_tp_sl or has_explicit_continue else 0.65,
        actions=[f"open_{side}"],
        action=f"open_{side}",
        reason="Round4 Add more 加仓规则",
    )


def parse_text(text: str) -> ParsedSignal:
    text = (text or "").strip()
    if not text:
        return ParsedSignal()
    raw_text = text
    if _is_pure_discord_cdn_url(text):
        return ParsedSignal(raw_text=raw_text, confidence=0.0, reason="Round7纯Discord/CDN链接")
    add_more_parsed = _parse_round4_add_more(text, raw_text)
    if add_more_parsed is not None:
        return add_more_parsed
    sl_be_parsed = _parse_round7_english_sl_be(text, raw_text)
    if sl_be_parsed is not None:
        return sl_be_parsed
    open_priority_parsed = _parse_round7_open_priority(text, raw_text)
    if open_priority_parsed is not None:
        return open_priority_parsed
    effective_text = strip_analysis_sections(text).strip()
    scene, scene_reason = classify_signal_scene(effective_text)
    # Round 2: 明确平仓信号优先于“等待/观望”类场景判断。
    # 例如“触发止损，直接出局！等待新一笔策略”中后半句有“等待”，
    # 但前半句已明确要求出局，不能被 conditional_observe 误拦截。
    pre_scene_exit, _pre_scene_exit_reason = check_exit_intent(effective_text)

    if scene in ("analysis", "conditional_observe", "narrative", "noise") and not pre_scene_exit:
        logger.info(f"检测到非交易场景({scene}),标记为忽略: {scene_reason}")
        return ParsedSignal(
            raw_text=raw_text,
            confidence=0.0,
            reason=scene_reason,
        )

    text = effective_text

    # 0. 检查是否为"继续持有"类消息 (不是有效信号)。
    # 如果同时包含止盈/止损调整语义,优先按更新信号处理,避免
    # "止损上移到成本价,继续持有" 被成本价/继续持有误拦截。
    pre_is_update, _pre_update_reason = check_update_intent(text)
    _holding_match = any(re.search(p, text) for p in HOLDING_INDICATORS)
    # Round 5: “当前三笔空单在手 + 分别盈利/持仓收益分别 + 分批止盈/推保护”
    # 是对已有仓位状态和已执行管理动作的描述，不是新的平仓指令。
    if re.search(r"(?:当前|目前|现目前|现在).{0,20}[一二两三四五六七八九十\d]+\s*(?:个|笔|单|批)?\s*(?:多单|空单).{0,12}(?:在手|持仓|持有)", text) and re.search(r"(?:分别盈利|分别获利|持仓收益分别|现目前分别盈利)", text):
        logger.info(f"检测到已有仓位收益汇报,标记为非有效信号: {text[:80]}")
        return ParsedSignal(raw_text=raw_text, confidence=0.0, reason="已有仓位收益汇报,非新的平仓指令")
    # Round 2: “持仓过夜/过夜单 + 设置好/注意 + 止盈止损”是持仓提醒，
    # 不是新的止盈止损更新指令；只有“改为/移动/上移/下移/调整至”等才按更新处理。
    if _holding_match and re.search(r"(?:持仓过夜|过夜单)", text) and re.search(r"(?:设置好|注意).{0,20}(?:止盈|止损)", text):
        logger.info(f"检测到持仓过夜提醒,标记为非有效信号: {text[:80]}")
        return ParsedSignal(raw_text=raw_text, confidence=0.0, reason="持仓过夜提醒,非新的止盈止损更新")
    if _holding_match and not pre_is_update and not pre_scene_exit:
        logger.info(f"检测到持仓继续/维持消息,标记为非有效信号: {text[:80]}")
        return ParsedSignal(raw_text=raw_text, confidence=0.0, reason="持仓继续/维持消息,非交易信号")

    # Round 3: "持仓观察/持仓观望/先持仓"是建议语气,即使含"设置好止盈止损"也优先忽略
    if re.search(r"(?:持仓观察|持仓观望|先持仓)", text):
        logger.info(f"检测到持仓观察建议,标记为非有效信号: {text[:80]}")
        return ParsedSignal(raw_text=raw_text, confidence=0.0, reason="持仓观察建议,非交易信号")
    # Round 3: "均价拉到X"是持仓描述非新开仓
    if re.search(r"均价(?:应该)?(?:都)?拉到", text):
        logger.info(f"检测到持仓均价描述,标记为非有效信号: {text[:80]}")
        return ParsedSignal(raw_text=raw_text, confidence=0.0, reason="持仓均价描述,非新开仓信号")
    # Round 3: "可以移动止损...防止/防"是建议语气非更新指令
    if re.search(r"可以移动止损.*(?:防止|防|以防)", text):
        logger.info(f"检测到建议语气(可以移动止损+防止),标记为非有效信号: {text[:80]}")
        return ParsedSignal(raw_text=raw_text, confidence=0.0, reason="建议语气,非更新指令")

    # 1. 检查是否为平仓信号
    is_exit, exit_reason = pre_scene_exit, _pre_scene_exit_reason
    if not is_exit:
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
        pos_pct = extract_position_pct(text)
        if pos_pct <= 0:
            pos_pct = 100.0
        # 平仓信号仍需提取品种和方向
        return ParsedSignal(
            symbol=symbol,
            side=side,
            raw_text=raw_text,
            confidence=0.7 if symbol else 0.3,  # 有品种信息时置信度较高
            position_pct=pos_pct,
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
                    # 无论做多做空,如果入场价>1000但TP<1000,很可能是省略了"万"单位
                    if entry > 1000:
                        new_tps.append(tp * 10000)
                    else:
                        new_tps.append(tp)
                else:
                    new_tps.append(tp)
            tps = new_tps
        if sl and sl < 1000:
            # 做空时SL高于入场价是正常的，不推断为"万"单位
            if side == "short" and sl > entry:
                pass  # 保持原值
            else:
                sl = sl * 10000
        if entry_prices:
            new_eps = [ep * 10000 if ep < 1000 else ep for ep in entry_prices]
            entry_prices = new_eps

    # 5.4 部分止盈/分批止盈优先按部分平仓处理。
    # 只在带百分比或明确"分批/部分止盈"时触发,避免把"止盈67200"误当平仓。
    partial_exit_pct = extract_position_pct(text)
    if (
        partial_exit_pct > 0
        and re.search(r"(?:止盈|分批|部分|先出|减仓|平掉|平仓|推保护|保护价)", text, re.IGNORECASE)
        and not any(a.startswith("open_") for a in pre_actions)
    ):
        return ParsedSignal(
            symbol=symbol,
            side=side,
            raw_text=raw_text,
            confidence=0.7 if symbol else 0.5,
            position_pct=partial_exit_pct,
            is_exit_signal=True,
            exit_reason=f"部分平仓/止盈比例命中: {partial_exit_pct}%",
            actions=["close_position"],
            action="close_position",
        )

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
    recent_texts: list[str] | None = None,
) -> ParsedSignal:
    """解析一条 Discord 消息(文本 + 可选图片)。

    Args:
        raw_text: 原始文本
        image_url: 图片 URL
        image_base64: 图片 base64
        kol_config: KOL 级别 LLM 配置（为 None 则使用全局设置）
        kol_name: KOL 名称（用于日志）
        recent_texts: 同 KOL 短时间窗口内的历史原文,用于撤单上下文锁和持仓跟进补全

    流程:
    1. 先用 LLM (DEEPSEEK V3) 解析文本信号
    2. 如果有图片且 KOL 配置了多模态分析，优先使用 LLM 分析图片
    3. LLM 解析失败时，回退到规则解析(正则表达式)
    """
    combined = raw_text or ""
    ocr_text = ""  # Fix: 初始化OCR文本变量
    parsed = ParsedSignal()
    has_image = bool(image_url or image_base64)

    if _is_pure_discord_cdn_url(combined):
        logger.info(f"[{kol_name}] 纯 Discord/CDN 链接,标记为非交易信号")
        return ParsedSignal(raw_text=combined, confidence=0.0, reason="Round7纯Discord/CDN链接")

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
        round4_add_more = _parse_round4_add_more(combined, combined)
        if round4_add_more is not None:
            return round4_add_more
        round7_sl_be = _parse_round7_english_sl_be(combined, combined)
        if round7_sl_be is not None:
            return round7_sl_be
        round7_open = _parse_round7_open_priority(combined, combined)
        if round7_open is not None:
            return round7_open
        pre_hold_exit, _ = check_exit_intent(combined)
        pre_hold_update, _ = check_update_intent(combined)
        _holding_match = any(re.search(p, combined) for p in HOLDING_INDICATORS)
        if _holding_match and not pre_hold_exit and not pre_hold_update:
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
            # L-5修复: OCR 文本存入独立字段,不混入 raw_text 影响去重
            combined = (combined + "\n" + ocr_text).strip()
            _ocr_success = True
        else:
            ocr_text = ""
            _ocr_success = False  # Fix: OCR无结果应为False

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
                llm_parsed.ocr_text = ocr_text
                return llm_parsed
        except Exception as e:
            logger.warning(f"[{kol_name}] 图片 LLM (OCR fallback) 解析失败: {e}")

    # ============ 阶段 1.8: 场景分类 + 分析段过滤 ============
    combined_for_parse = strip_analysis_sections(combined).strip()
    scene, scene_reason = classify_signal_scene(combined_for_parse)
    pre_scene_exit, _pre_scene_exit_reason = check_exit_intent(combined_for_parse)
    if scene in ("analysis", "conditional_observe", "narrative", "noise") and not pre_scene_exit:
        logger.info(f"[{kol_name}] 检测到非交易场景({scene}),跳过: {scene_reason}")
        return ParsedSignal(raw_text=combined, confidence=0.0, reason=scene_reason, ocr_text=ocr_text)
    if combined_for_parse and combined_for_parse != combined:
        logger.info(
            f"[{kol_name}] 已截掉盘面分析段,仅解析交易操作段: "
            f"{len(combined)} -> {len(combined_for_parse)} 字符"
        )

    # ============ 阶段 1.85: 撤单上下文锁优先 ============
    # 大镖客等 KOL 常见分开发法:
    # ① "撤，不挂了"
    # ② 紧接着复制原策略单
    # 第二条应作为旧挂单定位信息,不能先交给 LLM 推断成新开仓。
    if recent_texts:
        rule_parsed = parse_text(combined_for_parse)
        rule_parsed.raw_text = combined
        rule_parsed.has_image = has_image
        guarded = apply_cancel_context_if_needed(combined_for_parse, rule_parsed, recent_texts)
        if guarded.action == "cancel_order" and guarded.reason.startswith("撤单上下文命中"):
            logger.info(f"[{kol_name}] 撤单上下文锁命中,当前策略单改判为撤单定位信息: {guarded.reason}")
            guarded.ocr_text = ocr_text
            return guarded

        followed = apply_position_context_if_needed(combined_for_parse, guarded, recent_texts)
        if followed.action in ("open_long", "open_short") and followed.reason.startswith("持仓上下文跟进命中"):
            logger.info(f"[{kol_name}] 持仓上下文跟进命中,当前短消息补全为开仓: {followed.reason}")
            followed.ocr_text = ocr_text
            return followed

    # ============ 阶段 1.9: 明确止盈止损更新优先走规则 ============
    # "止损位下移500点，重设为63300" 这类消息是修改已有仓位风控,
    # 不能先交给 LLM 推断方向,否则可能被误判为新开多/开空。
    if scene == "update_tp_sl":
        rule_parsed = parse_text(combined_for_parse)
        rule_parsed.raw_text = combined
        rule_parsed.has_image = has_image
        if rule_parsed.is_update_signal or "update_tp_sl" in rule_parsed.actions:
            logger.info(f"[{kol_name}] 明确更新场景命中规则优先: {rule_parsed.update_reason}")
            rule_parsed.ocr_text = ocr_text
            return rule_parsed

    # ============ 阶段 2: LLM 文本解析（主要方式） ============
    llm_parsed = None  # 初始化,防止未进入LLM分支时引用未定义变量
    if use_llm and combined_for_parse:
        logger.info(f"[{kol_name}] 使用文本 LLM (DEEPSEEK V3) 解析信号")
        try:
            llm_parsed, usage = await parse_with_llm(
                combined_for_parse,
                context=context,
                min_confidence=llm_min_confidence,
                kol_name=kol_name,
            )
            if llm_parsed is not None:
                logger.info(
                    f"[{kol_name}] LLM 解析成功: symbol={llm_parsed.symbol}, "
                    f"side={llm_parsed.side}, confidence={llm_parsed.confidence:.2f}, "
                    f"tokens={usage.get('total_tokens', 0)}"
                )
                llm_parsed.has_image = has_image

                # ★ 撤单指令规则优先: 无论 LLM 返回什么结果(confidence 高低),
                # 只要规则解析器明确检测到撤单指令且不包含"重新挂/再挂"等重开意图,
                # 就以规则解析器的 cancel_order 结果为准。
                # 这防止 LLM 将 "撤掉""撤单" 等指令误判为平仓/开仓/其他动作
                rule_cancel_check = parse_text(combined_for_parse)
                rule_cancel_check.raw_text = combined
                rule_cancel_check.has_image = has_image
                if rule_cancel_check.action == "cancel_order" or "cancel_order" in rule_cancel_check.actions:
                    has_reopen = _has_any_pattern(combined_for_parse, REOPEN_AFTER_CANCEL_PATTERNS)
                    if not has_reopen:
                        logger.info(
                            f"[{kol_name}] 规则解析器检测到撤单指令,优先于 LLM 结果: {rule_cancel_check.reason}"
                        )
                        rule_cancel_check.ocr_text = ocr_text
                        return rule_cancel_check

                # ★ 关键修复: LLM 判定为无效信号(confidence<=0)时,不运行后续补充检测
                # 这防止叙事/故事类长文本被误判为平仓信号
                # (例如: KOL 发了一篇生活感悟,文中出现"卖了"被模糊模式误匹配)
                if llm_parsed.confidence <= 0:
                    # 规则保底: 撤单已在上面统一处理,这里直接跳过补充检测
                    logger.info(
                        f"[{kol_name}] LLM 判定为无效信号(confidence={llm_parsed.confidence:.2f}),"
                        f"跳过平仓/更新信号补充检测"
                    )
                    llm_parsed.ocr_text = ocr_text
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
                llm_parsed = _apply_cn_direction_override(combined_for_parse, llm_parsed)
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
                llm_parsed.ocr_text = ocr_text
                return llm_parsed
            else:
                logger.debug(f"[{kol_name}] LLM 未能识别有效信号，回退到规则解析")
        except Exception as e:
            logger.warning(f"[{kol_name}] LLM 解析异常: {e}, 回退到规则解析")


    # ============ 阶段 3: 规则解析（兜底） ============
    if use_llm_fallback or not use_llm:
        parsed = parse_text(combined)
        parsed.has_image = has_image
        parsed = apply_position_context_if_needed(combined, parsed, recent_texts)

        # 最终检查
        if (
            not parsed.symbol
            and not parsed.side
            and not parsed.is_exit_signal
            and not parsed.is_update_signal
            and "cancel_order" not in parsed.actions
        ):
            parsed.confidence = 0.0

        parsed.ocr_text = ocr_text
        return parsed

    # LLM 启用但回退禁用,且 LLM 解析失败 → 返回空结果
    parsed.has_image = has_image
    parsed.confidence = 0.0
    parsed.ocr_text = ocr_text
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
    parsed.ocr_text = ""
    return parsed
