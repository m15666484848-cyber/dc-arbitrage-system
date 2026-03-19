// DC套利监控系统 - 核心代码片段

// 1. 全局配置
const config = {
    autoRefresh: false,
    refreshInterval: 3000,
    profitThreshold: 0.5,
    feeRate: 0.35,
    krwUsdRate: 1330,
    currentTab: 'binance-bithumb',
    currentMarket: 'spot',
    minOrderAmount: 50,
    feishuWebhook: '',
    webhookEnabled: false,
    notificationThreshold: 1.0,
    notificationCooldown: 30000
};

// 2. 实盘数据获取
async function fetchRealPrices() {
    // 获取币安价格
    const binanceRes = await fetch('https://api.binance.com/api/v3/ticker/price');
    const binanceData = await binanceRes.json();
    
    // 获取火币价格
    const huobiRes = await fetch('https://api.huobi.pro/market/tickers');
    const huobiData = await huobiRes.json();
    
    // 获取OKX价格
    const okxRes = await fetch('https://www.okx.com/api/v5/market/tickers?instType=SPOT');
    const okxData = await okxRes.json();
    
    // 处理价格数据...
}

// 3. 套利计算逻辑
function calculateArbitrage() {
    allOpportunities = [];
    const markets = config.currentMarket === 'all' ? ['spot', 'contract'] : [config.currentMarket];
    
    markets.forEach(market => {
        coinList.forEach(coin => {
            const binance = priceData.binance[market][coin];
            const huobi = priceData.huobi[market][coin];
            const okx = priceData.okx[market][coin];
            
            // 过滤挂单量不足的币种
            if (!binance || binance.bidAmount < config.minOrderAmount || binance.askAmount < config.minOrderAmount) return;
            
            // 币安 ↔ 火币 套利
            if (huobi) {
                // 币安买，火币卖
                const spread1 = ((huobi.bid - binance.ask) / binance.ask) * 100;
                const profit1 = spread1 - config.feeRate;
                if (profit1 > config.profitThreshold) {
                    allOpportunities.push({
                        symbol: coin,
                        type: 'binance-huobi',
                        market: market,
                        profit: profit1,
                        direction: '币安→火币'
                    });
                }
                
                // 火币买，币安卖
                const spread2 = ((binance.bid - huobi.ask) / huobi.ask) * 100;
                const profit2 = spread2 - config.feeRate;
                if (profit2 > config.profitThreshold) {
                    allOpportunities.push({
                        symbol: coin,
                        type: 'binance-huobi',
                        market: market,
                        profit: profit2,
                        direction: '火币→币安'
                    });
                }
            }
            
            // 币安 ↔ OKX 套利
            if (okx) {
                // 币安买，OKX卖
                const spread3 = ((okx.bid - binance.ask) / binance.ask) * 100;
                const profit3 = spread3 - config.feeRate;
                if (profit3 > config.profitThreshold) {
                    allOpportunities.push({
                        symbol: coin,
                        type: 'binance-okx',
                        market: market,
                        profit: profit3,
                        direction: '币安→OKX'
                    });
                }
                
                // OKX买，币安卖
                const spread4 = ((binance.bid - okx.ask) / okx.ask) * 100;
                const profit4 = spread4 - config.feeRate;
                if (profit4 > config.profitThreshold) {
                    allOpportunities.push({
                        symbol: coin,
                        type: 'binance-okx',
                        market: market,
                        profit: profit4,
                        direction: 'OKX→币安'
                    });
                }
            }
        });
    });
    
    // 按利润排序
    allOpportunities.sort((a, b) => b.profit - a.profit);
}

// 4. 飞书通知
async function sendFeishuNotification(opportunities) {
    if (!config.webhookEnabled || !config.feishuWebhook) return;
    
    // 过滤高利润机会
    const highProfit = opportunities.filter(item => item.profit >= config.notificationThreshold);
    if (highProfit.length === 0) return;
    
    // 构造飞书卡片消息
    const message = {
        msg_type: "interactive",
        card: {
            header: {
                title: {
                    tag: "plain_text",
                    content: `🔥 发现${highProfit.length}个高利润套利机会！`
                },
                template: "red"
            },
            elements: [
                {
                    tag: "div",
                    text: {
                        tag: "lark_md",
                        content: highProfit.map(item => 
                            `**${item.symbol} ${item.market === 'spot' ? '现货' : '合约'}**\n` +
                            `💹 利润: +${item.profit.toFixed(2)}%\n` +
                            `🔄 方向: ${item.direction}`
                        ).join('\n\n')
                    }
                }
            ]
        }
    };
    
    // 发送通知
    await fetch(config.feishuWebhook, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(message)
    });
}

// 5. 配置持久化
function saveConfig() {
    localStorage.setItem('dc_arbitrage_config', JSON.stringify(config));
}

function loadConfig() {
    const saved = localStorage.getItem('dc_arbitrage_config');
    if (saved) Object.assign(config, JSON.parse(saved));
}
