# DC 量化系统边缘情况分析与修复方案

## 🔴 高风险问题（必须修复）

### 1. 交易所 API 调用失败无重试机制

**问题**：
- 网络抖动导致 API 调用失败时直接抛异常
- 订单可能成功下单但返回超时，导致重复下单
- 没有指数退避重试

**影响**：
- 信号丢失，错过交易机会
- 可能重复下单导致仓位异常

**修复方案**：
```python
# 使用 tenacity 库实现重试
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True
)
async def place_order_with_retry(ex, symbol, type, side, amount, price=None):
    ...
```

---

### 2. Discord WebSocket 断线无重连

**问题**：
- Discord Gateway 连接断开后无自动重连
- 心跳超时无处理
- 网络恢复后需手动重启服务

**影响**：
- 信号监听中断，错过 KOL 信号
- 系统需要人工干预才能恢复

**修复方案**：
```python
async def discord_listener():
    while True:  # 外层循环：断线重连
        try:
            await _connect_and_listen()
        except Exception as e:
            logger.error(f"Discord 连接断开: {e}, 5 秒后重连...")
            await asyncio.sleep(5)
```

---

### 3. 并发信号处理无锁

**问题**：
- 多个 KOL 同时发信号时，可能同时检查仓位状态
- 可能导致多个信号同时创建"主仓位"
- 违反"同一品种同方向只开一个主仓位"的设计

**影响**：
- 物理上开了多个仓位，违背聚合设计
- 止盈止损逻辑混乱

**修复方案**：
```python
# 使用 Redis 分布式锁
from redis import asyncio as aioredis

async def process_signal_with_lock(signal, customer_id):
    lock_key = f"signal_lock:{customer_id}:{signal.symbol}:{signal.side}"
    redis = await get_redis()
    async with redis.lock(lock_key, timeout=30):
        await _process_signal_internal(signal, customer_id)
```

---

### 4. 订单部分成交处理不当

**问题**：
- ccxt 返回 `partial` 状态时未正确处理
- 可能导致仓位数量与实际不一致

**影响**：
- 仓位追踪错误
- 止盈止损计算错误

**修复方案**：
```python
# 订单状态检查
if order_status in ("open", "partial"):
    # 更新订单的 filled_qty
    filled_qty = float(ex_order.get("filled", 0))
    if filled_qty > 0:
        order.filled_qty = filled_qty
        order.status = "partial"
        # 部分成交也算成功，继续创建仓位
```

---

### 5. 余额不足无友好提示

**问题**：
- 交易所返回 "insufficient balance" 时直接抛异常
- 用户不知道是哪个币种余额不足

**影响**：
- 用户困惑，不知道如何解决
- 无法快速定位问题

**修复方案**：
```python
try:
    order = await ex.create_order(...)
except ccxt.InsufficientFunds as e:
    # 获取余额信息
    balance = await ex.fetch_balance()
    available = balance.get("USDT", {}).get("free", 0)
    raise ValueError(
        f"余额不足: 需要 {amount * price:.2f} USDT, "
        f"可用 {available:.2f} USDT"
    ) from e
```

---

## 🟡 中等风险问题（建议修复）

### 6. 价格精度未校验

**问题**：
- 不同币种价格精度不同（如 DOGE: 4位小数，BTC: 1位小数）
- 用户输入的价格可能不符合交易所要求
- 可能导致下单失败

**修复方案**：
```python
# 使用 ccxt 的 price_to_precision
def adjust_price(ex, symbol, price):
    if hasattr(ex, 'price_to_precision'):
        return float(ex.price_to_precision(symbol, price))
    return price
```

---

### 7. 无入场价信号处理不明确

**问题**：
- KOL 发送"BTC 做多，现价进"时无入场价
- 系统如何获取现价？
- 市价单滑点如何控制？

**修复方案**：
```python
if not entry_price or entry_price == 0:
    # 获取实时市价
    market_price = await fetch_market_price(exchange, symbol)
    if not market_price:
        return {"ok": False, "reason": "无法获取市价"}
    entry_price = market_price
    logger.info(f"使用市价入场: {symbol} @ {entry_price}")
```

---

### 8. 多币种信号未处理

**问题**：
- KOL 发送"BTC 和 ETH 都做多"时只解析第一个
- 可能导致部分信号丢失

**修复方案**：
```python
# 信号解析器返回列表
def parse_text(text) -> list[ParsedSignal]:
    signals = []
    # 尝试提取多个币种
    for match in re.finditer(r"([A-Z]{2,10})\s*(做多|做空|long|short)", text, re.I):
        signals.append(_parse_single_signal(match, text))
    return signals if signals else [parse_text_single(text)]
```

---

### 9. 品种格式未标准化

**问题**：
- KOL 可能发送 "BTC"、"BTCUSDT"、"BTC/USDT"
- 不同格式可能导致仓位匹配失败

**修复方案**：
```python
def normalize_symbol(symbol: str) -> str:
    """标准化品种格式为 BTCUSDT。"""
    symbol = symbol.upper().strip()
    # BTC/USDT -> BTCUSDT
    symbol = symbol.replace("/", "").replace("-", "").replace("_", "")
    # BTC -> BTCUSDT
    if not symbol.endswith("USDT"):
        symbol = symbol + "USDT"
    return symbol
```

---

### 10. 止盈止损比例未指定

**问题**：
- KOL 发送"TP1: 65000, TP2: 66000"但未指定平仓比例
- 系统如何分配？

**修复方案**：
```python
# 默认等分
def calculate_tp_ratios(tp_count: int) -> list[float]:
    if tp_count == 0:
        return []
    # 等分，或自定义策略
    return [1.0 / tp_count] * tp_count

# 例如 TP1, TP2, TP3 -> [0.33, 0.33, 0.34]
```

---

## 🟢 低风险问题（优化建议）

### 11. 缺少系统健康检查接口

**建议**：
添加 `/api/health` 接口检查：
- 数据库连接
- Redis 连接
- Discord 连接状态
- 交易所 API 可用性

---

### 12. 缺少异常信号告警

**建议**：
当出现以下情况时发送飞书告警：
- 同一 KOL 短时间内发送多个相矛盾信号
- 信号解析成功率突然下降
- 交易所 API 连续失败

---

### 13. 缺少仓位同步机制

**建议**：
定期（如每小时）同步交易所仓位与数据库仓位：
- 检测手动在交易所平仓的情况
- 检测异常仓位
- 自动修复数据不一致

---

## 部署检查清单

在部署前，请确认以下事项：

- [ ] 已应用数据库迁移（`alembic upgrade head`）
- [ ] 已配置 LLM API Key（如需使用）
- [ ] 已测试 Discord 连接和断线重连
- [ ] 已测试交易所下单和异常处理
- [ ] 已配置飞书 Webhook（告警）
- [ ] 已设置日志收集和监控
- [ ] 已配置备份策略（数据库、Redis）