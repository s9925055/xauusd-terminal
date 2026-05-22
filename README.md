# XAUUSD 黄金实时看盘终端

基于 OKX 公开行情数据的黄金期货超短线看盘系统，完全免费，无需任何 API Key。

## 功能

- **实时行情**：OKX XAU-USDT-SWAP 永续合约，WebSocket 毫秒级推送
- **K线图表**：lightweight-charts，支持 1m / 5m / 15m / 1H
- **技术指标**：EMA20 / EMA50 / EMA200 / 布林带 / Supertrend
- **买卖信号**：基于 EMA 金死叉、K线形态、布林带的评分制信号标注
- **音效提醒**：信号出现时自动播放提示音（Web Audio API）
- **关键价位**：自动标注整数关口压力/支撑线
- **入场清单**：黄金区域入场把握度评分

## 启动方法

```bash
# 安装依赖
pip3 install requests websocket-client

# 启动本地服务
python3 gold_server.py

# 浏览器打开
open http://localhost:8888
```

## 数据来源

| 数据 | 来源 | 延迟 |
|------|------|------|
| 实时价格 | OKX WebSocket books5 | 毫秒级 |
| K线数据 | OKX REST API | 实时 |
| 费用 | 完全免费，无需注册 | — |

## 免责声明

本工具仅供学习和参考，不构成任何投资建议。期货交易存在风险，入市需谨慎。
