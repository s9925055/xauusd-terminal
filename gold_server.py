#!/usr/bin/env python3
"""
XAUUSD 黄金期货本地数据服务 v4
数据源：OKX XAU-USDT-SWAP 黄金永续合约（完全免费，无需 API Key）
  - 24h 成交量 ~4400万，价格毫秒级变化
  - WebSocket tickers 频道：价格每次变化立即推送
  - books5 频道：买一/卖一每次变化推送（更频繁）
  - REST 获取 K 线数据
"""

import json
import threading
import time
import socketserver
import requests
import websocket
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# 日志同时写到文件和终端
LOG_FILE = Path(__file__).parent / "gold_server.log"
_log_lock = threading.Lock()

def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with _log_lock:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")

PORT      = 8888
HTML_FILE = Path(__file__).parent / "gold_trading.html"

OKX_WS    = "wss://ws.okx.com:8443/ws/v5/public"
OKX_REST  = "https://www.okx.com/api/v5"
INST_ID   = "XAU-USDT-SWAP"   # 黄金永续合约
KLINE_ID  = "XAU-USDT-SWAP"

FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/0d9a72f4-e58a-4da9-bfe1-e94db4ff07ea"

# ── 全局状态 ──────────────────────────────────────────────
state = {
    "price": None, "prev_close": None, "open": None,
    "high": None,  "low": None,
    "change": None, "change_pct": None,
    "bid": None,   "ask": None,
    "ts": None,    "tick_count": 0,
}
state_lock = threading.Lock()

history = {"1m": [], "5m": [], "15m": [], "1H": []}
hist_lock = threading.Lock()

sse_clients = []
sse_lock = threading.Lock()


# ── 工具 ─────────────────────────────────────────────────
def now_str():
    return datetime.now().strftime("%H:%M:%S")


# ── 飞书推送 ──────────────────────────────────────────────
def send_feishu(signal: str, price: float, score: int, tf: str):
    """signal: 买▲ / 强买▲ / 卖▼ / 强卖▼ / ST转多 / ST转空"""
    emoji_map = {
        "买▲":  "🟢", "强买▲": "🔥",
        "卖▼":  "🔴", "强卖▼": "⚡",
        "ST转多": "⬆️", "ST转空": "⬇️",
    }
    emoji = emoji_map.get(signal, "📊")
    ts    = datetime.now().strftime("%H:%M:%S")
    title = f"{emoji} [{tf}] XAU/USD {signal}"
    content = (
        f"**时间**：{ts}\n"
        f"**价格**：${price:.2f}\n"
        f"**得分**：{score}分"
    ) if score else (
        f"**时间**：{ts}\n"
        f"**价格**：${price:.2f}"
    )
    body = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "green" if "买" in signal or "多" in signal else "red",
            },
            "elements": [{
                "tag": "div",
                "text": {"tag": "lark_md", "content": content},
            }],
        },
    }
    try:
        requests.post(FEISHU_WEBHOOK, json=body, timeout=5)
        log(f" 飞书推送: {title} ${price:.2f}")
    except Exception as e:
        log(f" 飞书推送失败: {e}")


# ── 信号检测（Python 版，与前端逻辑一致）────────────────────
_TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1H": 3600}
def _calc_ema(candles, period):
    k = 2 / (period + 1)
    ema = None
    out = []
    for c in candles:
        ema = c['c'] if ema is None else c['c'] * k + ema * (1 - k)
        out.append(ema)
    return out

def _calc_bb(candles, period=20, mult=2):
    upper, lower = [], []
    for i in range(len(candles)):
        if i < period - 1:
            upper.append(None); lower.append(None); continue
        sl   = [c['c'] for c in candles[i - period + 1:i + 1]]
        mean = sum(sl) / period
        std  = (sum((x - mean) ** 2 for x in sl) / period) ** 0.5
        upper.append(mean + mult * std)
        lower.append(mean - mult * std)
    return upper, lower

def detect_signals(candles, tf="1m"):
    """返回最新一根 K 线上的信号，无信号返回 None"""
    if len(candles) < 56:
        return None
    e20 = _calc_ema(candles, 20)
    e50 = _calc_ema(candles, 50)
    bb_upper, bb_lower = _calc_bb(candles, 20, 2)
    interval = candles[1]['t'] - candles[0]['t']

    last_sig_t = 0
    result = None
    for i in range(55, len(candles)):
        d, prev = candles[i], candles[i - 1]
        e20c, e20p = e20[i], e20[i - 1]
        e50c, e50p = e50[i], e50[i - 1]
        bbu, bbl   = bb_upper[i], bb_lower[i]

        bull_engulf  = d['c'] > d['o'] and d['o'] <= prev['c'] and d['c'] >= prev['o'] and (d['c'] - d['o']) > (prev['o'] - prev['c']) * 0.8
        hammer       = d['c'] > d['o'] and (d['o'] - d['l']) > (d['c'] - d['o']) * 1.8 and (d['h'] - d['c']) < (d['c'] - d['o'])
        ema_cross    = e20p < e50p and e20c > e50c
        bb_bounce    = bbl and d['l'] <= bbl and d['c'] > bbl and d['c'] > d['o']
        ema_support  = d['c'] > d['o'] and prev['c'] < e20p and d['c'] > e20c
        buy_score    = (2 if bull_engulf else 0) + (2 if hammer else 0) + (3 if ema_cross else 0) + (2 if bb_bounce else 0) + (1 if ema_support else 0)

        bear_engulf  = d['c'] < d['o'] and d['o'] >= prev['c'] and d['c'] <= prev['o'] and (d['o'] - d['c']) > (prev['c'] - prev['o']) * 0.8
        shoot_star   = d['c'] < d['o'] and (d['h'] - d['o']) > (d['o'] - d['c']) * 1.8 and (d['c'] - d['l']) < (d['o'] - d['c'])
        death_cross  = e20p > e50p and e20c < e50c
        bb_reject    = bbu and d['h'] >= bbu and d['c'] < bbu and d['c'] < d['o']
        ema_break    = d['c'] < d['o'] and prev['c'] > e20p and d['c'] < e20c
        sell_score   = (2 if bear_engulf else 0) + (2 if shoot_star else 0) + (3 if death_cross else 0) + (2 if bb_reject else 0) + (1 if ema_break else 0)

        too_close = (d['t'] - last_sig_t) < 3 * interval

        if not too_close and buy_score >= 3 and buy_score > sell_score:
            last_sig_t = d['t']
            result = {'t': d['t'], 'dir': '强买▲' if buy_score >= 5 else '买▲', 'score': buy_score, 'price': d['c']}
        elif not too_close and sell_score >= 3 and sell_score > buy_score:
            last_sig_t = d['t']
            result = {'t': d['t'], 'dir': '强卖▼' if sell_score >= 5 else '卖▼', 'score': sell_score, 'price': d['c']}

    # 窗口 = K线周期 + 3分钟缓冲（信号时间戳是开盘时间，收盘后才能检测）
    tf_sec  = _TF_SECONDS.get(tf, 60)
    max_age = tf_sec + 180
    now = time.time()
    if result and (now - result['t']) <= max_age:
        return result
    if result:
        sig_time = datetime.fromtimestamp(result['t']).strftime('%H:%M')
        age_min  = int((now - result['t']) / 60)
        log(f" detect: 有信号 {result['dir']} @ {sig_time}，已过 {age_min} 分钟，跳过")
    return None


# ── SuperTrend 检测（ATR period=10, mult=2.5）────────────────
def detect_st_flip(candles, period=10, mult=2.5):
    """返回最新一根已收盘K线是否发生 ST 翻转，无翻转返回 None"""
    if len(candles) < period + 1:
        return None
    # ATR
    tr = []
    for i, d in enumerate(candles):
        if i == 0:
            tr.append(d['h'] - d['l'])
        else:
            prev = candles[i - 1]
            tr.append(max(d['h'] - d['l'], abs(d['h'] - prev['c']), abs(d['l'] - prev['c'])))
    atr = []
    atr_val = sum(tr[:period]) / period
    for i in range(len(candles)):
        if i < period:
            atr.append(atr_val)
            continue
        atr_val = (atr_val * (period - 1) + tr[i]) / period
        atr.append(atr_val)

    up_band = dn_band = 0
    trend = 1
    prev_up = prev_dn = 0
    prev_trend = 1
    flip = None

    for i, d in enumerate(candles):
        hl2    = (d['h'] + d['l']) / 2
        raw_up = hl2 + mult * atr[i]
        raw_dn = hl2 - mult * atr[i]
        if i == 0:
            up_band = raw_up
            dn_band = raw_dn
        else:
            up_band = raw_up if (raw_up < prev_up or candles[i-1]['c'] > prev_up) else prev_up
            dn_band = raw_dn if (raw_dn > prev_dn or candles[i-1]['c'] < prev_dn) else prev_dn

        prev_trend = trend
        if trend == -1 and d['c'] > up_band:
            trend = 1
        elif trend == 1 and d['c'] < dn_band:
            trend = -1

        if trend != prev_trend:
            flip = {'t': d['t'], 'dir': 'ST转多' if trend == 1 else 'ST转空', 'price': d['c']}

        prev_up = up_band
        prev_dn = dn_band

    if flip and flip['t'] == candles[-1]['t']:
        return flip
    return None


# ── 已推送信号记录（防重复推送）────────────────────────────
_sent_signals = {}   # key: "{tf}_{candle_t}" → True


# ── OKX REST：K 线 ────────────────────────────────────────
def fetch_klines(bar, limit=300):
    """bar: 1m / 5m"""
    try:
        url = f"{OKX_REST}/market/candles?instId={KLINE_ID}&bar={bar}&limit={limit}"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json().get("data", [])
        # OKX 返回 [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        # 最新在前，需要倒序
        result = []
        for row in reversed(data):
            result.append({
                "t":       int(row[0]) // 1000,
                "o":       float(row[1]),
                "h":       float(row[2]),
                "l":       float(row[3]),
                "c":       float(row[4]),
                "v":       float(row[5]),
                "confirm": int(row[8]),   # 1=已收盘, 0=当前未收盘
            })
        return result
    except Exception as e:
        log(f" K线拉取失败 {bar}: {e}")
        return []


# ── OKX REST：24h 统计 ────────────────────────────────────
def fetch_24h():
    try:
        r = requests.get(f"{OKX_REST}/market/ticker?instId={INST_ID}", timeout=5)
        d = r.json()["data"][0]
        return {
            "open":      float(d["open24h"]),
            "high":      float(d["high24h"]),
            "low":       float(d["low24h"]),
            "prev_close": float(d["lastPx"] if "lastPx" in d else d["last"]),
            "last":      float(d["last"]),
            "bid":       float(d["bidPx"]),
            "ask":       float(d["askPx"]),
        }
    except Exception as e:
        log(f" 24h统计失败: {e}")
        return {}


# ── SSE 广播 ─────────────────────────────────────────────
def _broadcast(payload):
    with sse_lock:
        clients = list(sse_clients)
    dead = []
    for q in clients:
        try:
            q.append(payload)
        except Exception:
            dead.append(q)
    if dead:
        with sse_lock:
            for q in dead:
                if q in sse_clients:
                    sse_clients.remove(q)


def _make_payload(price, snap):
    return json.dumps({"type": "price", "data": {
        "price":     round(price, 2),
        "prevClose": round(snap["prev_close"] or price, 2),
        "open":      round(snap["open"] or price, 2),
        "high":      round(snap["high"] or price, 2),
        "low":       round(snap["low"] or price, 2),
        "bid":       round(snap["bid"] or price, 2),
        "ask":       round(snap["ask"] or price, 2),
        "change":    snap["change"] or 0,
        "changePct": snap["change_pct"] or 0,
        "ts":        snap["ts"] or int(time.time() * 1000),
    }})


# ── OKX WebSocket ─────────────────────────────────────────
ws_app = None

def on_open(ws):
    log(f" ✅ 已连接 OKX WebSocket")
    # 订阅 tickers（每次价格变化推送）
    ws.send(json.dumps({
        "op": "subscribe",
        "args": [
            {"channel": "tickers", "instId": INST_ID},
            {"channel": "books5",  "instId": INST_ID},   # 买一/卖一 5档，更频繁
        ]
    }))


def on_message(ws, message):
    try:
        msg = json.loads(message)

        # 忽略订阅确认
        if msg.get("event") in ("subscribe", "error"):
            log(f" WS事件: {msg}")
            return

        arg  = msg.get("arg", {})
        ch   = arg.get("channel", "")
        data = msg.get("data", [])
        if not data:
            return

        price = None
        ts    = int(time.time() * 1000)

        if ch == "tickers":
            d     = data[0]
            price = float(d["last"])
            bid   = float(d["bidPx"]) if d.get("bidPx") else None
            ask   = float(d["askPx"]) if d.get("askPx") else None
            ts    = int(d.get("ts", ts))
            with state_lock:
                if bid: state["bid"] = bid
                if ask: state["ask"] = ask

        elif ch == "books5":
            d = data[0]
            bids = d.get("bids", [])
            asks = d.get("asks", [])
            if bids and asks:
                bid = float(bids[0][0])
                ask = float(asks[0][0])
                price = round((bid + ask) / 2, 2)
                ts    = int(d.get("ts", ts))
                with state_lock:
                    state["bid"] = bid
                    state["ask"] = ask

        if price is None:
            return

        with state_lock:
            if state["prev_close"] is None:
                return  # 等待初始化
            if state["high"] is None or price > state["high"]:
                state["high"] = price
            if state["low"] is None or price < state["low"]:
                state["low"] = price
            prev = state["prev_close"] or price
            state["price"]      = price
            state["change"]     = round(price - prev, 2)
            state["change_pct"] = round((price - prev) / prev * 100, 3) if prev else 0
            state["ts"]         = ts
            state["tick_count"] += 1
            snap = dict(state)

        _broadcast(_make_payload(price, snap))

        if snap["tick_count"] % 200 == 1:
            log(f" [{ch}] XAU={price:.2f}  "
                  f"bid={snap['bid']:.2f} ask={snap['ask']:.2f}  "
                  f"tick#{snap['tick_count']}")

    except Exception as e:
        log(f" on_message 错误: {e}")


def on_error(ws, error):
    log(f" WebSocket 错误: {error}")


def on_close(ws, code, msg):
    log(f" WebSocket 断开，5秒后重连...")
    time.sleep(5)
    start_ws()


def start_ws():
    global ws_app
    ws_app = websocket.WebSocketApp(
        OKX_WS,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )
    t = threading.Thread(
        target=ws_app.run_forever,
        kwargs={"ping_interval": 25, "ping_timeout": 10},
        daemon=True
    )
    t.start()


# ── 24h 统计定时刷新（每5分钟同步一次 OKX 的日内高低/开盘/前收）──
def stats_loop():
    while True:
        time.sleep(300)   # 5 分钟
        stats = fetch_24h()
        if not stats:
            continue
        with state_lock:
            # open / prev_close 固定用 OKX 提供的值（不随价格漂移）
            state["open"]       = stats.get("open")
            state["prev_close"] = stats.get("prev_close")
            # high / low 取 OKX 24h 值与本地追踪值的更优解
            okx_high = stats.get("high")
            okx_low  = stats.get("low")
            if okx_high and (state["high"] is None or okx_high > state["high"]):
                state["high"] = okx_high
            if okx_low and (state["low"] is None or okx_low < state["low"]):
                state["low"] = okx_low
            # 同步更新 change / change_pct
            price = state["price"] or stats.get("last")
            prev  = state["prev_close"]
            if price and prev:
                state["change"]     = round(price - prev, 2)
                state["change_pct"] = round((price - prev) / prev * 100, 3)
        log(f" 24h统计刷新  open:{state['open']}  "
              f"high:{state['high']}  low:{state['low']}  prev:{state['prev_close']}")


# ── K 线定时刷新 ──────────────────────────────────────────
def _push_if_new(sig, tf):
    """推送信号，防重复"""
    key = f"{tf}_{sig['t']}"
    sig_time = datetime.fromtimestamp(sig['t']).strftime('%H:%M')
    if key in _sent_signals:
        log(f" [{tf}] 信号已推过: {sig['dir']} @ {sig_time}，跳过")
        return
    _sent_signals[key] = True
    if len(_sent_signals) > 200:
        oldest = list(_sent_signals.keys())[0]
        del _sent_signals[oldest]
    score = sig.get('score', 0)
    log(f" [{tf}] 新信号: {sig['dir']} @ {sig_time}" + (f"  score={score}" if score else ""))
    send_feishu(sig['dir'], sig['price'], score, tf)

def _check_and_notify(candles, tf):
    """检测技术信号 + ST翻转，推送飞书（防重复）"""
    # 只对已收盘的 K 线检测信号，排除当前未收盘的最后一根
    closed = [c for c in candles if c.get("confirm", 1) == 1]
    if not closed:
        return
    # 技术信号（买▲ / 卖▼ / 强买▲ / 强卖▼）
    sig = detect_signals(closed, tf)
    if sig:
        _push_if_new(sig, tf)
    # ST 翻转（多▲ / 空▼）
    st = detect_st_flip(closed)
    if st:
        _push_if_new(st, tf)


def hist_loop():
    tick = 0
    log(" hist_loop 启动")
    while True:
        time.sleep(15)
        tick += 1
        # 1m 每15秒刷新一次（确保不漏1分钟K线的信号）
        k1 = fetch_klines("1m", 300)
        with hist_lock:
            if k1: history["1m"] = k1
        if k1:
            log(f" [1m] tick#{tick} 拉取 {len(k1)} 根K线，最新:{datetime.fromtimestamp(k1[-1]['t']).strftime('%H:%M')}")
            _check_and_notify(k1, "1m")

        # 5m/15m/1H 每30秒刷新一次
        if tick % 2 == 0:
            k5  = fetch_klines("5m",  300)
            k15 = fetch_klines("15m", 300)
            k1h = fetch_klines("1H",  300)
            with hist_lock:
                if k5:  history["5m"]  = k5
                if k15: history["15m"] = k15
                if k1h: history["1H"]  = k1h
            log(f" K线刷新  1m:{len(k1)}  5m:{len(k5) if k5 else 0}  15m:{len(k15) if k15 else 0}  1H:{len(k1h) if k1h else 0}")
            if k5: _check_and_notify(k5, "5m")


# ── HTTP Handler ──────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def log_message(self, *a): pass

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-cache")

    def do_OPTIONS(self):
        self.send_response(200); self.cors(); self.end_headers()

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/notify":
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length) or b"{}")
            signal = body.get("signal", "")
            price  = float(body.get("price", 0))
            score  = int(body.get("score", 0))
            tf     = body.get("tf", "")
            log(f" /api/notify 收到浏览器信号: {signal} ${price:.2f} [{tf}]")
            threading.Thread(
                target=send_feishu, args=(signal, price, score, tf), daemon=True
            ).start()
            self.send_response(200)
            self.cors(); self.end_headers()
            self.wfile.write(b'{"ok":true}')
        else:
            self.send_response(404); self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/api/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.cors()
            self.end_headers()
            q = []
            with sse_lock:
                sse_clients.append(q)
            # 立刻推送当前价格
            with state_lock:
                if state["price"]:
                    self._sse(_make_payload(state["price"], dict(state)))
            try:
                while True:
                    if q:
                        self._sse(q.pop(0))
                    else:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        time.sleep(0.1)   # 100ms 轮询队列
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with sse_lock:
                    if q in sse_clients:
                        sse_clients.remove(q)

        elif path == "/api/price":
            with state_lock:
                s = dict(state)
            body = json.dumps({"price": {
                "price":     round(s["price"] or 0, 2),
                "prevClose": round(s["prev_close"] or 0, 2),
                "open":      round(s["open"] or 0, 2),
                "high":      round(s["high"] or 0, 2),
                "low":       round(s["low"] or 0, 2),
                "bid":       round(s["bid"] or 0, 2),
                "ask":       round(s["ask"] or 0, 2),
                "change":    s["change"] or 0,
                "changePct": s["change_pct"] or 0,
            }, "error": None}).encode()
            self._json(body)

        elif path == "/api/history":
            iv_map = {"1m": "1m", "5m": "5m", "15m": "15m", "1H": "1H"}
            from urllib.parse import parse_qs, urlparse
            qs = parse_qs(urlparse(self.path).query)
            iv = iv_map.get(qs.get("interval", ["5m"])[0], "5m")
            with hist_lock:
                candles = list(history[iv])
            self._json(json.dumps({"candles": candles}).encode())

        elif path in ("/", "/index.html"):
            if HTML_FILE.exists():
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.cors(); self.end_headers()
                self.wfile.write(HTML_FILE.read_bytes())
            else:
                self.send_response(404); self.end_headers()
        else:
            self.send_response(404); self.end_headers()

    def _sse(self, msg):
        self.wfile.write(f"data: {msg}\n\n".encode())
        self.wfile.flush()

    def _json(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.cors(); self.end_headers()
        self.wfile.write(body)


class ThreadedServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ── 入口 ──────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("  XAUUSD 黄金终端 v4  (OKX XAU-USDT-SWAP)")
    print("=" * 55)
    print(f"  看盘地址 : http://localhost:{PORT}")
    print(f"  数据源   : OKX 黄金永续合约（24h量 ~4400万）")
    print(f"  推送方式 : WebSocket books5 → SSE → 浏览器")
    print(f"  更新延迟 : 毫秒级（< 100ms）")
    print("  停止服务 : Ctrl+C")
    print("=" * 55)

    print("初始化数据...")
    stats = fetch_24h()
    with state_lock:
        state.update({
            "price":      stats.get("last"),
            "prev_close": stats.get("prev_close"),
            "open":       stats.get("open"),
            "high":       stats.get("high"),
            "low":        stats.get("low"),
            "bid":        stats.get("bid"),
            "ask":        stats.get("ask"),
        })
        if state["price"] and state["prev_close"]:
            p, pc = state["price"], state["prev_close"]
            state["change"]     = round(p - pc, 2)
            state["change_pct"] = round((p - pc) / pc * 100, 3)
    print(f"  初始价格: ${state['price']:.2f}")

    k1  = fetch_klines("1m",  300)
    k5  = fetch_klines("5m",  300)
    k15 = fetch_klines("15m", 300)
    k1h = fetch_klines("1H",  300)
    with hist_lock:
        if k1:  history["1m"]  = k1
        if k5:  history["5m"]  = k5
        if k15: history["15m"] = k15
        if k1h: history["1H"]  = k1h
    print(f"  K线就绪: 1m:{len(k1)}  5m:{len(k5)}  15m:{len(k15)}  1H:{len(k1h)}")

    start_ws()
    threading.Thread(target=hist_loop,  daemon=True).start()
    threading.Thread(target=stats_loop, daemon=True).start()

    server = ThreadedServer(("localhost", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
