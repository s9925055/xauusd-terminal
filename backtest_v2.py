#!/usr/bin/env python3
"""
XAUUSD 信号系统回测 v2
对比 4 个方案：
  A: 原始系统（EMA20/50）+ 固定 TP/SL $8/$8
  B: 原始系统（EMA20/50）+ ATR 动态 TP/SL（1:2）
  C: 快速EMA（EMA5/13）+ 固定 TP/SL $8/$8
  D: 快速EMA + 5m ST多周期过滤 + ATR 动态 TP/SL（完整新系统）

数据源：OKX XAU-USDT-SWAP，拉取最近 3 天 1m K 线
"""

import requests, time
from datetime import datetime

OKX_REST = "https://www.okx.com/api/v5"
INST_ID  = "XAU-USDT-SWAP"
FIXED_TP = 8.0
FIXED_SL = 8.0

# ── 数据拉取 ──────────────────────────────────────────────
def fetch_klines_all(bar, days=3):
    """拉取最近 N 天的 K 线（分页）"""
    limit   = 300
    target  = days * 24 * 60 if bar == "1m" else days * 24 * 12
    candles = []
    after   = ""
    print(f"  拉取 {bar} K线（目标 {target} 根）...", end="", flush=True)
    while len(candles) < target:
        url = f"{OKX_REST}/market/candles?instId={INST_ID}&bar={bar}&limit={limit}"
        if after:
            url += f"&after={after}"
        try:
            r = requests.get(url, timeout=10)
            data = r.json().get("data", [])
        except Exception as e:
            print(f" 请求失败: {e}")
            break
        if not data:
            break
        for row in data:
            candles.append({
                "t": int(row[0]) // 1000,
                "o": float(row[1]),
                "h": float(row[2]),
                "l": float(row[3]),
                "c": float(row[4]),
                "v": float(row[5]),
                "confirm": int(row[8]),
            })
        after = data[-1][0]  # 最旧那根的时间戳
        time.sleep(0.2)
        print(".", end="", flush=True)
    candles.sort(key=lambda x: x["t"])
    # 只保留已收盘 K 线
    candles = [c for c in candles if c["confirm"] == 1]
    print(f" 共 {len(candles)} 根")
    return candles

# ── 指标计算 ──────────────────────────────────────────────
def calc_ema(candles, period):
    k, ema, out = 2 / (period + 1), None, []
    for c in candles:
        ema = c["c"] if ema is None else c["c"] * k + ema * (1 - k)
        out.append(ema)
    return out

def calc_bb(candles, period=20, mult=2):
    upper, lower = [], []
    for i in range(len(candles)):
        if i < period - 1:
            upper.append(None); lower.append(None); continue
        sl   = [c["c"] for c in candles[i - period + 1:i + 1]]
        mean = sum(sl) / period
        std  = (sum((x - mean) ** 2 for x in sl) / period) ** 0.5
        upper.append(mean + mult * std)
        lower.append(mean - mult * std)
    return upper, lower

def calc_atr(candles, period=14):
    tr = []
    for i, d in enumerate(candles):
        if i == 0:
            tr.append(d["h"] - d["l"])
        else:
            prev = candles[i - 1]
            tr.append(max(d["h"] - d["l"], abs(d["h"] - prev["c"]), abs(d["l"] - prev["c"])))
    atr, atr_val = [], sum(tr[:period]) / period
    for i in range(len(candles)):
        if i < period:
            atr.append(atr_val); continue
        atr_val = (atr_val * (period - 1) + tr[i]) / period
        atr.append(atr_val)
    return atr

def calc_supertrend(candles, period=10, mult=2.5):
    """返回每根 K 线的 ST 趋势方向列表（1=多头, -1=空头）"""
    tr = []
    for i, d in enumerate(candles):
        if i == 0:
            tr.append(d["h"] - d["l"])
        else:
            prev = candles[i - 1]
            tr.append(max(d["h"] - d["l"], abs(d["h"] - prev["c"]), abs(d["l"] - prev["c"])))
    atr, atr_val = [], sum(tr[:period]) / period
    for i in range(len(candles)):
        if i < period:
            atr.append(atr_val); continue
        atr_val = (atr_val * (period - 1) + tr[i]) / period
        atr.append(atr_val)

    trends = []
    up_band = dn_band = 0
    trend = 1
    prev_up = prev_dn = 0
    for i, d in enumerate(candles):
        hl2    = (d["h"] + d["l"]) / 2
        raw_up = hl2 + mult * atr[i]
        raw_dn = hl2 - mult * atr[i]
        if i == 0:
            up_band, dn_band = raw_up, raw_dn
        else:
            up_band = raw_up if (raw_up < prev_up or candles[i-1]["c"] > prev_up) else prev_up
            dn_band = raw_dn if (raw_dn > prev_dn or candles[i-1]["c"] < prev_dn) else prev_dn
        if trend == -1 and d["c"] > up_band:
            trend = 1
        elif trend == 1 and d["c"] < dn_band:
            trend = -1
        trends.append(trend)
        prev_up, prev_dn = up_band, dn_band
    return trends

# ── 信号检测 ──────────────────────────────────────────────
def score_candle(i, candles, e_fast, e_slow, bb_upper, bb_lower):
    """计算买/卖得分，返回 (buy_score, sell_score)"""
    d, prev = candles[i], candles[i - 1]
    e_fc, e_fp = e_fast[i], e_fast[i - 1]
    e_sc, e_sp = e_slow[i], e_slow[i - 1]
    bbu, bbl   = bb_upper[i], bb_lower[i]

    # 买入
    bull_engulf = d["c"] > d["o"] and d["o"] <= prev["c"] and d["c"] >= prev["o"] and (d["c"] - d["o"]) > (prev["o"] - prev["c"]) * 0.8
    hammer      = d["c"] > d["o"] and (d["o"] - d["l"]) > (d["c"] - d["o"]) * 1.8 and (d["h"] - d["c"]) < (d["c"] - d["o"])
    ema_cross   = e_fp < e_sp and e_fc > e_sc
    bb_bounce   = bbl and d["l"] <= bbl and d["c"] > bbl and d["c"] > d["o"]
    ema_support = d["c"] > d["o"] and prev["c"] < e_fp and d["c"] > e_fc
    buy_score   = (2 if bull_engulf else 0) + (2 if hammer else 0) + (3 if ema_cross else 0) + (2 if bb_bounce else 0) + (1 if ema_support else 0)

    # 卖出
    bear_engulf = d["c"] < d["o"] and d["o"] >= prev["c"] and d["c"] <= prev["o"] and (d["o"] - d["c"]) > (prev["c"] - prev["o"]) * 0.8
    shoot_star  = d["c"] < d["o"] and (d["h"] - d["o"]) > (d["o"] - d["c"]) * 1.8 and (d["c"] - d["l"]) < (d["o"] - d["c"])
    death_cross = e_fp > e_sp and e_fc < e_sc
    bb_reject   = bbu and d["h"] >= bbu and d["c"] < bbu and d["c"] < d["o"]
    ema_break   = d["c"] < d["o"] and prev["c"] > e_fp and d["c"] < e_fc
    sell_score  = (2 if bear_engulf else 0) + (2 if shoot_star else 0) + (3 if death_cross else 0) + (2 if bb_reject else 0) + (1 if ema_break else 0)

    return buy_score, sell_score

def calc_rsi(candles, period=14):
    gains, losses = [], []
    for i in range(1, len(candles)):
        d = candles[i]["c"] - candles[i-1]["c"]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    rsi = [50.0]  # index 0 placeholder
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        rs = avg_g / avg_l if avg_l != 0 else 100
        rsi.append(100 - 100 / (1 + rs))
    # pad front so len == len(candles)
    while len(rsi) < len(candles):
        rsi.insert(0, 50.0)
    return rsi

def calc_bb_width(candles, period=20, mult=2):
    """返回每根K线的BB宽度百分比"""
    widths = []
    for i in range(len(candles)):
        if i < period - 1:
            widths.append(0.0); continue
        sl   = [c["c"] for c in candles[i - period + 1:i + 1]]
        mean = sum(sl) / period
        std  = (sum((x - mean)**2 for x in sl) / period) ** 0.5
        widths.append((mult * 2 * std / mean * 100) if mean else 0)
    return widths

def calc_adx(candles, period=14):
    """计算 ADX（平均方向性指数），返回每根K线的 ADX 值列表"""
    n = len(candles)
    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(n):
        if i == 0:
            plus_dm.append(0.0); minus_dm.append(0.0)
            tr_list.append(candles[0]["h"] - candles[0]["l"])
            continue
        prev = candles[i - 1]
        cur  = candles[i]
        up   = cur["h"] - prev["h"]
        dn   = prev["l"] - cur["l"]
        plus_dm.append(up if up > dn and up > 0 else 0.0)
        minus_dm.append(dn if dn > up and dn > 0 else 0.0)
        tr_list.append(max(cur["h"] - cur["l"],
                           abs(cur["h"] - prev["c"]),
                           abs(cur["l"] - prev["c"])))

    # Wilder 平滑（前 period 根用简单均值初始化）
    def wilder_smooth(data, p):
        out = [0.0] * n
        out[p] = sum(data[:p])
        for i in range(p + 1, n):
            out[i] = out[i-1] - out[i-1] / p + data[i]
        return out

    atr_w  = wilder_smooth(tr_list, period)
    pdm_w  = wilder_smooth(plus_dm, period)
    mdm_w  = wilder_smooth(minus_dm, period)

    adx = [0.0] * n
    dx_vals = []
    for i in range(period, n):
        pdi = 100 * pdm_w[i] / atr_w[i] if atr_w[i] else 0
        mdi = 100 * mdm_w[i] / atr_w[i] if atr_w[i] else 0
        denom = pdi + mdi
        dx = 100 * abs(pdi - mdi) / denom if denom else 0
        dx_vals.append(dx)
        if len(dx_vals) >= period:
            # ADX = Wilder MA of DX
            if len(dx_vals) == period:
                adx[i] = sum(dx_vals[-period:]) / period
            else:
                adx[i] = (adx[i-1] * (period - 1) + dx) / period
    return adx

def generate_signals(candles, fast_p, slow_p, st_filter=None,
                     min_score=3, rsi_filter=False,
                     bb_width_filter=False, time_filter=False,
                     adx_filter=False, us_session=False):
    """
    生成信号列表，支持多种过滤器：
    min_score      : 最低得分门槛（默认3）
    rsi_filter     : 买入时RSI<65，卖出时RSI>35
    bb_width_filter: BB宽度>0.1%才入场（过滤横盘）
    time_filter    : 只在欧美盘（北京时间14:00-次日02:00）交易
    adx_filter     : ADX>20 才入场（趋势行情过滤）
    us_session     : 只在美盘核心时段（北京时间21:30-次日00:00）
    """
    from datetime import timezone, timedelta
    CST = timezone(timedelta(hours=8))

    min_period = max(fast_p, slow_p, 20) + 5
    e_fast       = calc_ema(candles, fast_p)
    e_slow       = calc_ema(candles, slow_p)
    bb_u, bb_l   = calc_bb(candles, 20, 2)
    atr          = calc_atr(candles, 14)
    rsi          = calc_rsi(candles, 14)
    bb_width     = calc_bb_width(candles, 20, 2)
    adx          = calc_adx(candles, 14) if (adx_filter or us_session) else None
    interval     = candles[1]["t"] - candles[0]["t"]

    signals    = []
    last_sig_t = 0

    for i in range(min_period, len(candles)):
        buy_s, sell_s = score_candle(i, candles, e_fast, e_slow, bb_u, bb_l)
        too_close = (candles[i]["t"] - last_sig_t) < 3 * interval

        direction = None
        score = 0
        if not too_close and buy_s >= min_score and buy_s > sell_s:
            direction = "buy";  score = buy_s
        elif not too_close and sell_s >= min_score and sell_s > buy_s:
            direction = "sell"; score = sell_s

        if direction is None:
            continue

        # 5m ST 多周期过滤
        if st_filter is not None:
            trend = st_filter[i]
            if direction == "buy"  and trend != 1:  continue
            if direction == "sell" and trend != -1: continue

        # RSI 过滤
        if rsi_filter:
            r = rsi[i]
            if direction == "buy"  and r > 65: continue
            if direction == "sell" and r < 35: continue

        # BB 宽度过滤（横盘期间不入场）
        if bb_width_filter and bb_width[i] < 0.1:
            continue

        # ADX 过滤（<20 = 震荡行情，跳过）
        if adx_filter and adx[i] < 20:
            continue

        # 时间过滤（只做欧美盘：北京时间 14:00-次日02:00）
        if time_filter:
            hour = datetime.fromtimestamp(candles[i]["t"], tz=CST).hour
            if not (14 <= hour or hour < 2):
                continue

        # 美盘核心时段（北京时间 21:30-00:00）
        if us_session:
            dt  = datetime.fromtimestamp(candles[i]["t"], tz=CST)
            hm  = dt.hour * 60 + dt.minute
            if not (21 * 60 + 30 <= hm <= 23 * 60 + 59):
                continue

        last_sig_t = candles[i]["t"]
        signals.append({
            "i":         i,
            "t":         candles[i]["t"],
            "direction": direction,
            "price":     candles[i]["c"],
            "score":     score,
            "atr":       atr[i],
        })
    return signals

# ── 模拟交易 ──────────────────────────────────────────────
def simulate(candles, signals, use_atr=False, fixed_tp=FIXED_TP, fixed_sl=FIXED_SL,
             atr_tp_mult=2, lot_size_oz=1.0, flat_fee=0.28, circuit_breaker=0):
    """
    模拟逐笔交易，返回交易记录
    use_atr=True     : TP=atr_tp_mult×ATR, SL=1×ATR
    lot_size_oz      : 仓位大小（oz），0.04手=4oz
    flat_fee         : IC Markets 固定手续费（每笔来回，默认$0.28 for 0.04手）
    circuit_breaker  : >0 时表示当日连续亏损N笔后停止交易（0=不启用）
    """
    from datetime import timezone, timedelta
    CST = timezone(timedelta(hours=8))

    trades = []
    # 连续亏损熔断状态
    day_state = {}   # date -> {"consec_loss": int, "stopped": bool}

    for sig in signals:
        if circuit_breaker > 0:
            dt  = datetime.fromtimestamp(sig["t"], tz=CST)
            day = dt.date()
            if day not in day_state:
                day_state[day] = {"consec_loss": 0, "stopped": False}
            if day_state[day]["stopped"]:
                continue
        entry   = sig["price"]
        atr_val = sig["atr"]
        if use_atr:
            tp_dist = atr_val * atr_tp_mult
            sl_dist = atr_val * 1
        else:
            tp_dist = fixed_tp
            sl_dist = fixed_sl
        # 单次来回手续费（IC Markets：固定佣金，0.04手=$0.28）
        fee = flat_fee

        if sig["direction"] == "buy":
            tp = entry + tp_dist
            sl = entry - sl_dist
        else:
            tp = entry - tp_dist
            sl = entry + sl_dist

        result = "open"
        exit_price = None
        exit_t = None

        for j in range(sig["i"] + 1, min(sig["i"] + 61, len(candles))):
            c = candles[j]
            if sig["direction"] == "buy":
                if c["l"] <= sl:
                    result, exit_price, exit_t = "sl", sl, c["t"]; break
                if c["h"] >= tp:
                    result, exit_price, exit_t = "tp", tp, c["t"]; break
            else:
                if c["h"] >= sl:
                    result, exit_price, exit_t = "sl", sl, c["t"]; break
                if c["l"] <= tp:
                    result, exit_price, exit_t = "tp", tp, c["t"]; break

        if result == "open":
            exit_price = candles[min(sig["i"] + 60, len(candles) - 1)]["c"]
            exit_t     = candles[min(sig["i"] + 60, len(candles) - 1)]["t"]
            result     = "timeout"

        gross_pnl = ((exit_price - entry) if sig["direction"] == "buy" else (entry - exit_price)) * lot_size_oz
        net_pnl   = gross_pnl - fee
        trades.append({
            "entry_t":   datetime.fromtimestamp(sig["t"]).strftime("%m-%d %H:%M"),
            "dir":       sig["direction"],
            "entry":     entry,
            "exit":      exit_price,
            "gross_pnl": round(gross_pnl, 2),
            "fee":       round(fee, 2),
            "pnl":       round(net_pnl, 2),   # 净盈亏（扣手续费后）
            "result":    result,
            "score":     sig["score"],
            "tp":        round(tp_dist, 2),
            "sl":        round(sl_dist, 2),
        })
        # 更新连续亏损计数
        if circuit_breaker > 0:
            if net_pnl <= 0:
                day_state[day]["consec_loss"] += 1
                if day_state[day]["consec_loss"] >= circuit_breaker:
                    day_state[day]["stopped"] = True
            else:
                day_state[day]["consec_loss"] = 0  # 盈利则重置计数
    return trades

def summarize(trades, label):
    if not trades:
        print(f"\n{label}: 无交易")
        return
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross  = sum(t["gross_pnl"] for t in trades)
    fees   = sum(t["fee"] for t in trades)
    net    = sum(t["pnl"] for t in trades)
    win_r  = len(wins) / len(trades) * 100
    avg_w  = sum(t["pnl"] for t in wins)   / len(wins)   if wins   else 0
    avg_l  = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    pf     = abs(sum(t["pnl"] for t in wins) / sum(t["pnl"] for t in losses)) if losses and sum(t["pnl"] for t in losses) != 0 else 999

    print(f"\n{'='*58}")
    print(f"  {label}")
    print(f"{'='*58}")
    print(f"  交易次数: {len(trades)}  (买:{sum(1 for t in trades if t['dir']=='buy')}  卖:{sum(1 for t in trades if t['dir']=='sell')})")
    print(f"  胜率    : {win_r:.1f}%  ({len(wins)}胜 / {len(losses)}负)")
    print(f"  毛盈亏  : ${gross:+.2f}")
    print(f"  总手续费: ${-fees:.2f}  (每笔平均 ${fees/len(trades):.2f})")
    print(f"  净盈亏  : ${net:+.2f}   每日净盈: ${net/7:+.2f}")
    print(f"  平均净盈: ${avg_w:+.2f}  平均净亏: ${avg_l:+.2f}")
    print(f"  盈亏比  : {pf:.2f}")
    print(f"  TP触发  : {sum(1 for t in trades if t['result']=='tp')}  "
          f"SL触发: {sum(1 for t in trades if t['result']=='sl')}  "
          f"超时  : {sum(1 for t in trades if t['result']=='timeout')}")


# ── 主程序 ────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("  XAUUSD 信号系统回测 v2（最近7天）")
    print("=" * 55)

    print("\n[1] 拉取历史数据...")
    k1m = fetch_klines_all("1m", days=7)
    k5m = fetch_klines_all("5m", days=7)

    if len(k1m) < 200:
        print("数据不足，退出")
        exit(1)

    # 计算 5m SuperTrend 并对齐到 1m
    print("\n[2] 计算5m SuperTrend并对齐到1m时间轴...")
    st5 = calc_supertrend(k5m)
    # 建立 5m 时间戳→趋势 映射
    st5_map = {k5m[i]["t"]: st5[i] for i in range(len(k5m))}
    # 每根1m K线：找对应的5m K线（同一个5分钟内最新的5m趋势）
    # 5m candle time = floor(t / 300) * 300
    def get_5m_trend(t1m):
        t5 = (t1m // 300) * 300
        return st5_map.get(t5, st5_map.get(t5 - 300, 1))

    st5_for_1m = [get_5m_trend(c["t"]) for c in k1m]
    bull5 = sum(1 for x in st5_for_1m if x == 1)
    print(f"  1m总计: {len(k1m)} 根  对应5m多头占比: {bull5/len(k1m)*100:.1f}%")

    # 计算 1m SuperTrend（ATR10, mult=2.5）
    st1 = calc_supertrend(k1m, period=10, mult=2.5)
    bull1 = sum(1 for x in st1 if x == 1)
    print(f"  1m ST 多头占比: {bull1/len(k1m)*100:.1f}%")

    LOT = 4.0   # 0.04手 = 4 oz

    # ── 方案 A：原始 EMA20/50 + 固定 TP/SL ──
    print("\n[3] 运行回测方案（仓位 0.04手=4oz，Taker手续费 0.05%）...")
    sigs_a = generate_signals(k1m, fast_p=20, slow_p=50)
    trades_a = simulate(k1m, sigs_a, use_atr=False, lot_size_oz=LOT)
    summarize(trades_a, "方案A：原始系统（EMA20/50）+ 固定TP/SL $8/$8")

    # ── 方案 B：原始 EMA20/50 + ATR TP/SL ──
    trades_b = simulate(k1m, sigs_a, use_atr=True, lot_size_oz=LOT)
    summarize(trades_b, "方案B：原始系统（EMA20/50）+ ATR TP/SL（1:2）")

    # ── 方案 C：快速 EMA5/13 + 固定 TP/SL ──
    sigs_c = generate_signals(k1m, fast_p=5, slow_p=13)
    trades_c = simulate(k1m, sigs_c, use_atr=False, lot_size_oz=LOT)
    summarize(trades_c, "方案C：快速EMA（5/13）+ 固定TP/SL $8/$8")

    # ── 方案 D：快速 EMA5/13 + 5m ST过滤 + ATR TP/SL ──
    sigs_d = generate_signals(k1m, fast_p=5, slow_p=13, st_filter=st5_for_1m)
    trades_d = simulate(k1m, sigs_d, use_atr=True, lot_size_oz=LOT)
    summarize(trades_d, "方案D：快速EMA(5/13) + 5m ST过滤 + ATR TP/SL")

    # ── 方案 E：快速 EMA5/13 + 无5m过滤 + ATR TP/SL ──
    trades_e = simulate(k1m, sigs_c, use_atr=True, lot_size_oz=LOT)
    summarize(trades_e, "方案E：快速EMA(5/13) + ATR TP/SL（基准）")

    # ── 方案 F：快速 EMA5/13 + 有5m过滤 + 固定TP/SL ──
    trades_f = simulate(k1m, sigs_d, use_atr=False, lot_size_oz=LOT)
    summarize(trades_f, "方案F：快速EMA(5/13) + 5m ST过滤 + 固定TP/SL $8/$8")

    # ── 方案 G：E + 信号门槛≥4 ──
    sigs_g   = generate_signals(k1m, fast_p=5, slow_p=13, min_score=4)
    trades_g = simulate(k1m, sigs_g, use_atr=True, lot_size_oz=LOT)
    summarize(trades_g, "方案G：E + 信号门槛≥4分")

    # ── 方案 H：E + RSI 过滤 ──
    sigs_h   = generate_signals(k1m, fast_p=5, slow_p=13, rsi_filter=True)
    trades_h = simulate(k1m, sigs_h, use_atr=True, lot_size_oz=LOT)
    summarize(trades_h, "方案H：E + RSI过滤（买<65 / 卖>35）")

    # ── 方案 I：E + BB宽度过滤 ──
    sigs_i   = generate_signals(k1m, fast_p=5, slow_p=13, bb_width_filter=True)
    trades_i = simulate(k1m, sigs_i, use_atr=True, lot_size_oz=LOT)
    summarize(trades_i, "方案I：E + BB宽度过滤（>0.1%才入场）")

    # ── 方案 J：E + TP=3×ATR ──
    sigs_e_base = generate_signals(k1m, fast_p=5, slow_p=13)
    trades_j    = simulate(k1m, sigs_e_base, use_atr=True, atr_tp_mult=3, lot_size_oz=LOT)
    summarize(trades_j, "方案J：E + TP=3×ATR")

    # ── 方案 K：全部叠加 ──
    sigs_k   = generate_signals(k1m, fast_p=5, slow_p=13,
                                min_score=4, rsi_filter=True, bb_width_filter=True)
    trades_k = simulate(k1m, sigs_k, use_atr=True, atr_tp_mult=3, lot_size_oz=LOT)
    summarize(trades_k, "方案K：全叠加（≥4分+RSI+BB宽度+TP3×ATR）")

    # ── 方案 L：TP=3×ATR + RSI过滤 ──
    sigs_l   = generate_signals(k1m, fast_p=5, slow_p=13, rsi_filter=True)
    trades_l = simulate(k1m, sigs_l, use_atr=True, atr_tp_mult=3, lot_size_oz=LOT)
    summarize(trades_l, "方案L：TP=3×ATR + RSI过滤（最优组合）")

    # ── 方案 M：L + ADX>20 过滤（趋势确认）──
    sigs_m   = generate_signals(k1m, fast_p=5, slow_p=13, rsi_filter=True, adx_filter=True)
    trades_m = simulate(k1m, sigs_m, use_atr=True, atr_tp_mult=3, lot_size_oz=LOT)
    summarize(trades_m, "方案M：L + ADX>20（过滤震荡行情）")

    # ── 方案 N：L + 美盘时间过滤（北京21:30-00:00）──
    sigs_n   = generate_signals(k1m, fast_p=5, slow_p=13, rsi_filter=True, us_session=True)
    trades_n = simulate(k1m, sigs_n, use_atr=True, atr_tp_mult=3, lot_size_oz=LOT)
    summarize(trades_n, "方案N：L + 美盘时段（21:30-00:00 北京）")

    # ── 方案 O：L + ADX>20 + 美盘时间 + 连续3亏停手 ──
    sigs_o   = generate_signals(k1m, fast_p=5, slow_p=13, rsi_filter=True,
                                adx_filter=True, us_session=True)
    trades_o = simulate(k1m, sigs_o, use_atr=True, atr_tp_mult=3, lot_size_oz=LOT,
                        circuit_breaker=3)
    summarize(trades_o, "方案O：L + ADX + 美盘 + 连续3亏熔断")

    # ── ST方向一致系列 ──────────────────────────────────────
    print("\n[4] ST方向一致过滤（买入时ST多头，卖出时ST空头）...")

    # ── 方案 P：L + 1m ST方向一致 ──
    sigs_p   = generate_signals(k1m, fast_p=5, slow_p=13, rsi_filter=True,
                                st_filter=st1)
    trades_p = simulate(k1m, sigs_p, use_atr=True, atr_tp_mult=3, lot_size_oz=LOT)
    summarize(trades_p, "方案P：L + 1m ST方向一致")

    # ── 方案 Q：L + 5m ST方向一致 ──
    sigs_q   = generate_signals(k1m, fast_p=5, slow_p=13, rsi_filter=True,
                                st_filter=st5_for_1m)
    trades_q = simulate(k1m, sigs_q, use_atr=True, atr_tp_mult=3, lot_size_oz=LOT)
    summarize(trades_q, "方案Q：L + 5m ST方向一致")

    # ── 方案 R：L + 1m ST + 5m ST 双重方向一致 ──
    # 两个ST同向才入场
    st_both = [s1 if s1 == s5 else 0 for s1, s5 in zip(st1, st5_for_1m)]
    sigs_r   = generate_signals(k1m, fast_p=5, slow_p=13, rsi_filter=True,
                                st_filter=st_both)
    trades_r = simulate(k1m, sigs_r, use_atr=True, atr_tp_mult=3, lot_size_oz=LOT)
    summarize(trades_r, "方案R：L + 1m&5m ST双重方向一致")

    # ── 方案 S：N（美盘） + 1m ST方向一致 ──
    sigs_s   = generate_signals(k1m, fast_p=5, slow_p=13, rsi_filter=True,
                                us_session=True, st_filter=st1)
    trades_s = simulate(k1m, sigs_s, use_atr=True, atr_tp_mult=3, lot_size_oz=LOT)
    summarize(trades_s, "方案S：美盘时段 + 1m ST方向一致")

    # ── 汇总对比 ──
    def max_drawdown(trades):
        """计算最大回撤（净盈亏序列）"""
        peak = 0.0
        equity = 0.0
        max_dd = 0.0
        for t in trades:
            equity += t["pnl"]
            peak    = max(peak, equity)
            max_dd  = max(max_dd, peak - equity)
        return max_dd

    print(f"\n{'='*82}")
    print(f"  汇总对比（仓位 0.04手=4oz，IC Markets $0.28/笔）")
    print(f"{'='*82}")
    print(f"  {'方案':<32} {'次数':>5} {'胜率':>7} {'净盈亏':>9} {'盈亏比':>7} {'最大回撤':>9}")
    print(f"  {'-'*32} {'-'*5} {'-'*7} {'-'*9} {'-'*7} {'-'*9}")
    for label, trades in [
        ("A: 原始+固定TP/SL", trades_a),
        ("C: 快速EMA+固定TP/SL", trades_c),
        ("E: 快速EMA+ATR  ← 基准", trades_e),
        ("G: E+门槛≥4", trades_g),
        ("H: E+RSI过滤", trades_h),
        ("I: E+BB宽度", trades_i),
        ("J: E+TP×3", trades_j),
        ("K: 全叠加", trades_k),
        ("L: TP×3+RSI（基线）", trades_l),
        ("M: L+ADX>20", trades_m),
        ("N: L+美盘时段", trades_n),
        ("O: L+ADX+美盘+熔断", trades_o),
        ("P: L+1m ST方向一致", trades_p),
        ("Q: L+5m ST方向一致", trades_q),
        ("R: L+1m&5m ST双重", trades_r),
        ("S: 美盘+1m ST ← 新优", trades_s),
    ]:
        if not trades:
            print(f"  {label:<32} {'无交易':>5}")
            continue
        wins  = [t for t in trades if t["pnl"] > 0]
        loss  = [t for t in trades if t["pnl"] <= 0]
        net   = sum(t["pnl"] for t in trades)
        wr    = len(wins) / len(trades) * 100
        pf    = abs(sum(t["pnl"] for t in wins) / sum(t["pnl"] for t in loss)) if loss and sum(t["pnl"] for t in loss) != 0 else 999
        dd    = max_drawdown(trades)
        print(f"  {label:<32} {len(trades):>5} {wr:>6.1f}% {net:>+9.2f} {pf:>7.2f} {-dd:>+9.2f}")
    print()
