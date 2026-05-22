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
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

PORT      = 8888
HTML_FILE = Path(__file__).parent / "gold_trading.html"

OKX_WS    = "wss://ws.okx.com:8443/ws/v5/public"
OKX_REST  = "https://www.okx.com/api/v5"
INST_ID   = "XAU-USDT-SWAP"   # 黄金永续合约
KLINE_ID  = "XAU-USDT-SWAP"

# ── 全局状态 ──────────────────────────────────────────────
state = {
    "price": None, "prev_close": None, "open": None,
    "high": None,  "low": None,
    "change": None, "change_pct": None,
    "bid": None,   "ask": None,
    "ts": None,    "tick_count": 0,
}
state_lock = threading.Lock()

history = {"1m": [], "5m": []}
hist_lock = threading.Lock()

sse_clients = []
sse_lock = threading.Lock()


# ── 工具 ─────────────────────────────────────────────────
def now_str():
    return datetime.now().strftime("%H:%M:%S")


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
                "t": int(row[0]) // 1000,
                "o": float(row[1]),
                "h": float(row[2]),
                "l": float(row[3]),
                "c": float(row[4]),
                "v": float(row[5]),
            })
        return result
    except Exception as e:
        print(f"[{now_str()}] K线拉取失败 {bar}: {e}")
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
        print(f"[{now_str()}] 24h统计失败: {e}")
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
    print(f"[{now_str()}] ✅ 已连接 OKX WebSocket")
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
            print(f"[{now_str()}] WS事件: {msg}")
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
            print(f"[{now_str()}] [{ch}] XAU={price:.2f}  "
                  f"bid={snap['bid']:.2f} ask={snap['ask']:.2f}  "
                  f"tick#{snap['tick_count']}")

    except Exception as e:
        print(f"[{now_str()}] on_message 错误: {e}")


def on_error(ws, error):
    print(f"[{now_str()}] WebSocket 错误: {error}")


def on_close(ws, code, msg):
    print(f"[{now_str()}] WebSocket 断开，5秒后重连...")
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


# ── K 线定时刷新 ──────────────────────────────────────────
def hist_loop():
    while True:
        time.sleep(30)
        k1 = fetch_klines("1m", 300)
        k5 = fetch_klines("5m", 300)
        with hist_lock:
            if k1: history["1m"] = k1
            if k5: history["5m"] = k5
        print(f"[{now_str()}] K线刷新  1m:{len(k1)}  5m:{len(k5)}")


# ── HTTP Handler ──────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):

    def log_message(self, *a): pass

    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Cache-Control", "no-cache")

    def do_OPTIONS(self):
        self.send_response(200); self.cors(); self.end_headers()

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
            iv = "1m" if "interval=1m" in self.path else "5m"
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

    k1 = fetch_klines("1m", 300)
    k5 = fetch_klines("5m", 300)
    with hist_lock:
        if k1: history["1m"] = k1
        if k5: history["5m"] = k5
    print(f"  K线就绪: 1m:{len(k1)}  5m:{len(k5)}")

    start_ws()
    threading.Thread(target=hist_loop, daemon=True).start()

    server = ThreadedServer(("localhost", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
