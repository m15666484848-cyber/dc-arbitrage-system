"""回放测试: 用 8-26 三马哥误判的 4 条信号验证修复效果。

在 dcquant-backend 容器内执行: python /app/replay_test.py
"""
import asyncio
import sys

sys.path.insert(0, "/app")

from app.services import signal_parser


CASES = [
    {
        "id": 3212,
        "expect": "open_long ETH 挂单2367/2288 止盈2428/2518/2600 止损2200",
        "raw": (
            "ETH  挂单多     仓位思路强平控制U及以下1300以下\n"
            "挂2367 100倍 2%保证金\n"
            "再挂2288补仓    100倍 3%保证金\n"
            "第一止盈 2428 走70%移动保本\n"
            "第二止盈 2518\n"
            "第三止盈2600\n"
            "止损2200"
        ),
    },
    {
        "id": 3213,
        "expect": "open_short BTC 挂单79450/81088 止盈76688/75388 止损82000",
        "raw": (
            "BTC  挂单空     仓位思路强平控制U及以下95000U以上\n"
            "79450  100倍 2%保证金\n"
            "再挂81088 100倍 3%保证金\n"
            "第一止盈 76688止盈70%移动保本\n"
            "第二止盈 75388\n"
            "止损82000"
        ),
    },
    {
        "id": 3238,
        "expect": "cancel_order BTC空单 + open_short ETH 市价",
        "raw": (
            "撤掉BTC的空单，直接头仓市价做空ETH 100倍 2%保证金 "
            "补仓预留3%一会发完整策略 强平控制在3000以上"
        ),
    },
    {
        "id": 3243,
        "expect": "open_short ETH 市价 止盈2432/2408/2367 止损2580",
        "raw": (
            "ETH  做空     仓位思路强平控制在3000U及以上\n"
            "2457附近市价已经进场 100倍 2%保证金\n"
            "不提前发补仓点容易被针对    100倍 3%保证金\n"
            "第一止盈 2432  走50%移动保本\n"
            "第二止盈2408\n"
            "第三止盈2367\n"
            "止损2580"
        ),
    },
]


def _fmt(parsed) -> str:
    return (
        f"actions={parsed.actions} side={parsed.side or '-'} "
        f"symbol={parsed.symbol or '-'} entry={parsed.entry_price} "
        f"entry_prices={parsed.entry_prices} tp={parsed.take_profits} "
        f"sl={parsed.stop_loss} conf={parsed.confidence} "
        f"update_reason={parsed.update_reason or '-'}"
    )


async def main() -> None:
    # 模拟三马哥的 KOL 级 LLM 配置(生产环境 discord_monitor 用 KolLLMConfig.from_kol)
    kol_config = signal_parser.KolLLMConfig(
        enabled=True,
        vision_enabled=False,
        fallback=True,
        min_confidence=0.4,
    )
    print("=" * 90)
    print("[单元验证] 复合指令提取函数")
    cancel_sym, cancel_side = signal_parser.extract_cancel_target(
        "撤掉BTC的空单，直接头仓市价做空ETH 100倍 2%保证金"
    )
    open_sym = signal_parser.extract_compound_open_symbol(
        "撤掉BTC的空单，直接头仓市价做空ETH 100倍 2%保证金"
    )
    print(f"  撤单目标: symbol={cancel_sym} side={cancel_side}  (期望 BTC/USDT short)")
    print(f"  开仓目标: symbol={open_sym}  (期望 ETH/USDT)")
    assert cancel_sym == "BTC/USDT" and cancel_side == "short", "撤单目标提取失败"
    assert open_sym == "ETH/USDT", "开仓目标提取失败"
    print("  PASS")
    print("=" * 90)

    print("[专项验证] check_exit_intent 不应把止盈阶梯判为平仓")
    for case in CASES:
        is_exit, reason = signal_parser.check_exit_intent(case["raw"])
        flag = "OK" if not is_exit else "!!!误判平仓!!!"
        print(f"  #{case['id']}: is_exit={is_exit} ({reason or '-'}) {flag}")
    print("=" * 90)

    print("[专项验证] 规则兜底路径(模拟 LLM 超时,kol_config=None)")
    for case in CASES:
        parsed = await signal_parser.parse_message(case["raw"], kol_name="规则兜底")
        print(f"  #{case['id']}: actions={parsed.actions} conf={parsed.confidence}")
    print("-" * 60)
    print("[回归验证] 真实部分平仓消息不应被开仓策略保护误拦截(应 close_position)")
    real_exits = [
        "止盈70%,剩余仓位挂78888全部止盈",
        "BTC空单挂单成交,止盈70%先走",
        "多单止盈50%,剩余仓位止损上移保本",
    ]
    for raw in real_exits:
        parsed = await signal_parser.parse_message(raw, kol_name="回归")
        flag = "OK" if "close_position" in parsed.actions else "!!!漏判平仓!!!"
        print(f"  {raw[:28]}... → {parsed.actions} {flag}")
    print("=" * 90)

    print("[主流程验证] 生产 LLM 路径(kol_config 启用)")
    for case in CASES:
        print(f"\n[信号 #{case['id']}] 期望: {case['expect']}")
        parsed = await signal_parser.parse_message(
            case["raw"], kol_config=kol_config, kol_name="回放测试"
        )
        print(f"  实际: {_fmt(parsed)}")
        if parsed.reason:
            print(f"  reason: {parsed.reason[:120]}")
        if parsed.exit_reason:
            print(f"  exit_reason: {parsed.exit_reason[:120]}")

    print("\n回放完成")


if __name__ == "__main__":
    asyncio.run(main())
