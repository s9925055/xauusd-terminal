#!/usr/bin/env python3
"""
XAUUSD 职业策略回测 v3
基于专业超短线体系：多周期共振 + 量价确认 + 结构止损

核心逻辑提炼：
  大方向：5m EMA20方向 + SuperTrend
  偏向过滤：VWAP位置（上方偏多 / 下方偏空）
  排列确认：EMA9 > EMA20 > EMA60（多头）或反之（空头）
  动能确认：QQE > 50 金叉（多）/ QQE < 50 死叉（空）
  量价确认：回踩缩量 → 放量突破
  时间过滤：欧美盘活跃时段（北京时间 21:30-00:00 / 15:00-18:00）
  止损：1.2×ATR（近似结构外）
  止盈：2.4×ATR（盈亏比 1:2）

IC Markets 手续费：
  Raw Spread 账户：$3.50/手/单边 → 0.05手双边 = $0.35/笔
  点差：XAUUSD ~0.2点 → 约 $0.01/0.05手（忽略不计）
  实际每笔费用：$0.35

仓位：0.05手 = 5oz，价格每动$1 → 盈亏$5
总资金：$440
"""

import requests, time
from datetime import datetime, timezone, timedelta

OKX_REST = "https://www.okx.com/api/v5"
INST_ID  = "XAU-USDT-SWAP"
CST      = timezone(timedelta(hours=8))  # 北京/新加坡时间

LOT_OZ       = 5.0    # 0.05手 × 100oz/手 = 5oz
COMMISSION   = 0.35   # IC Markets，0.05手双边手续费
CAPITAL      = 440.0  # 总资金
SL_MULT      = 1.2    # 止损 = 1.2×ATR
TP_MULT      = 2.4    # 止盈 = 2.4×ATR（盈亏比 1:2）

# ── 数据拉取 ──────────────────────────────────────────────
def fetch_klines(bar, days=5):
    limit, target = 300, days * 24 * (60 if bar == "1m" else 12)
    candles, after = [], ""
    print(f"  拉取 {bar}（目标 {target} 根）...", end="", flush=True)
    while len(candles) < target:
        url = f"{OKX_REST}/market/candles?instId={INST_ID}&bar={bar}&limit={limit}"
        if after: url += f"&after={after}"
        try:
            data = requests.get(url, timeout=10).json().get("data", [])
        except: break
        if not data: break
        for row in data:
            candles.append({
                "t": int(row[0]) // 1000,
                "o": float(row[1]), "h": float(row[2]),
                "l": float(row[3]), "c": float(row[4]),
                "v": float(row[5]), "confirm": int(row[8]),
            })
        after = data[-1][0]
        time.sleep(0.2)
        print(".", end="", flush=True)
    candles.sort(key=lambda x: x["t"])
    candles = [c for c in candles if c["confirm"] == 1]
    print(f" {len(candles)} 根")
    return candles

# ── 指标计算 ──────────────────────────────────────────────
def calc_ema(candles, p):
    k, ema, out = 2/(p+1), None, []
    for c in candles:
        ema = c["c"] if ema is None else c["c"]*k + ema*(1-k)
        out.append(ema)
    return out

def calc_atr(candles, p=14):
    tr = [candles[0]["h"]-candles[0]["l"]]
    for i in range(1, len(candles)):
        d, prev = candles[i], candles[i-1]
        tr.append(max(d["h"]-d["l"], abs(d["h"]-prev["c"]), abs(d["l"]-prev["c"])))
    atr, v = [], sum(tr[:p])/p
    for i in range(len(tr)):
        if i < p: atr.append(v); continue
        v = (v*(p-1)+tr[i])/p
        atr.append(v)
    return atr

def calc_supertrend(candles, p=10, mult=3.0):
    """ATR period=10, Multiplier=3（职业稳健型参数）"""
    atr = calc_atr(candles, p)
    trends, up, dn, trend = [], 0, 0, 1
    prev_up = prev_dn = 0
    for i, d in enumerate(candles):
        hl2 = (d["h"]+d["l"])/2
        ru, rd = hl2+mult*atr[i], hl2-mult*atr[i]
        if i == 0:
            up, dn = ru, rd
        else:
            up = ru if (ru < prev_up or candles[i-1]["c"] > prev_up) else prev_up
            dn = rd if (rd > prev_dn or candles[i-1]["c"] < prev_dn) else prev_dn
        if trend == -1 and d["c"] > up: trend = 1
        elif trend == 1  and d["c"] < dn: trend = -1
        trends.append(trend)
        prev_up, prev_dn = up, dn
    return trends

def calc_vwap(candles):
    """VWAP：每个自然日重置"""
    vwap = []
    cum_pv = cum_v = 0.0
    cur_day = None
    for c in candles:
        day = datetime.fromtimestamp(c["t"], tz=CST).date()
        if day != cur_day:
            cum_pv = cum_v = 0.0
            cur_day = day
        tp = (c["h"]+c["l"]+c["c"])/3
        cum_pv += tp * c["v"]
        cum_v  += c["v"]
        vwap.append(cum_pv/cum_v if cum_v else c["c"])
    return vwap

def calc_rsi(candles, p=14):
    gains, losses = [0.0], [0.0]
    for i in range(1, len(candles)):
        d = candles[i]["c"] - candles[i-1]["c"]
        gains.append(max(d,0)); losses.append(max(-d,0))
    rsi, ag = [], sum(gains[1:p+1])/p
    al = sum(losses[1:p+1])/p
    for i in range(len(candles)):
        if i < p: rsi.append(50.0); continue
        ag = (ag*(p-1)+gains[i])/p
        al = (al*(p-1)+losses[i])/p
        rs = ag/al if al else 100
        rsi.append(100-100/(1+rs))
    return rsi

def calc_qqe(candles, rsi_p=14, smooth=5):
    """QQE：平滑RSI，判断动能与金叉/死叉"""
    rsi  = calc_rsi(candles, rsi_p)
    k    = 2/(smooth+1)
    srsi, ema = [], None
    for r in rsi:
        ema = r if ema is None else r*k + ema*(1-k)
        srsi.append(ema)
    return srsi   # smoothed RSI，>50多头，<50空头

def calc_vol_ma(candles, p=10):
    vols = [c["v"] for c in candles]
    out  = []
    for i in range(len(vols)):
        sl = vols[max(0,i-p+1):i+1]
        out.append(sum(sl)/len(sl))
    return out

def calc_ema_slope(series, lookback=3):
    """EMA斜率：当前值 vs N根前，正=向上，负=向下"""
    slopes = [0.0]*lookback
    for i in range(lookback, len(series)):
        slopes.append(series[i] - series[i-lookback])
    return slopes

# ── 信号生成 ──────────────────────────────────────────────
def generate_pro_signals(k1m, k5m):
    """
    职业策略信号生成
    1m进场 + 5m方向 + 多指标共振
    """
    # 1m 指标
    e9   = calc_ema(k1m, 9)
    e20  = calc_ema(k1m, 20)
    e60  = calc_ema(k1m, 60)
    st1  = calc_supertrend(k1m, 10, 3)
    vwap = calc_vwap(k1m)
    qqe  = calc_qqe(k1m, 14, 5)
    atr  = calc_atr(k1m, 14)
    vma  = calc_vol_ma(k1m, 10)

    # 5m 指标（方向过滤）
    e20_5m   = calc_ema(k5m, 20)
    st5      = calc_supertrend(k5m, 10, 3)
    slope_5m = calc_ema_slope(e20_5m, 3)

    # 5m 时间戳 → 趋势/斜率 映射
    st5_map    = {k5m[i]["t"]: st5[i]     for i in range(len(k5m))}
    slope5_map = {k5m[i]["t"]: slope_5m[i] for i in range(len(k5m))}
    def get5(t1m, mp):
        t5 = (t1m//300)*300
        return mp.get(t5, mp.get(t5-300, list(mp.values())[-1] if mp else 0))

    signals = []
    last_sig_t = 0
    interval   = k1m[1]["t"] - k1m[0]["t"]
    min_i      = 65

    for i in range(min_i, len(k1m)):
        c   = k1m[i]
        t   = c["t"]
        too_close = (t - last_sig_t) < 5 * interval   # 至少5根K线间隔

        # 时间过滤：北京时间 21:30-次日00:00 或 15:00-18:00
        hour_cst = datetime.fromtimestamp(t, tz=CST).hour
        min_cst  = datetime.fromtimestamp(t, tz=CST).minute
        in_us    = (hour_cst == 21 and min_cst >= 30) or (hour_cst == 22) or (hour_cst == 23)
        in_eu    = 15 <= hour_cst < 18
        if not (in_us or in_eu):
            continue
        if too_close:
            continue

        # 5m方向
        st5_trend   = get5(t, st5_map)
        slope5      = get5(t, slope5_map)

        # 公共条件
        atr_v = atr[i]
        vol_v = c["v"]
        vol_avg = vma[i]

        # ── 做多条件 ──
        long_conds = [
            st5_trend == 1,                          # 5m ST多头
            slope5 > 0,                              # 5m EMA20向上
            st1[i] == 1,                             # 1m ST多头
            c["c"] > vwap[i],                        # 价格在VWAP上方
            e9[i] > e20[i] > e60[i],                 # EMA多头排列
            qqe[i] > 50,                             # QQE动能偏多
            qqe[i] > qqe[i-1],                       # QQE上升（近似金叉）
            c["c"] > c["o"],                         # 阳线
            vol_v > vol_avg * 1.3,                   # 放量突破
            min(k1m[j]["l"] for j in range(max(0,i-5),i)) <= e20[i]*1.001,  # 近期曾回踩EMA20
        ]

        # ── 做空条件 ──
        short_conds = [
            st5_trend == -1,                         # 5m ST空头
            slope5 < 0,                              # 5m EMA20向下
            st1[i] == -1,                            # 1m ST空头
            c["c"] < vwap[i],                        # 价格在VWAP下方
            e9[i] < e20[i] < e60[i],                 # EMA空头排列
            qqe[i] < 50,                             # QQE动能偏空
            qqe[i] < qqe[i-1],                       # QQE下降（近似死叉）
            c["c"] < c["o"],                         # 阴线
            vol_v > vol_avg * 1.3,                   # 放量跌破
            max(k1m[j]["h"] for j in range(max(0,i-5),i)) >= e20[i]*0.999,  # 近期曾回抽EMA20
        ]

        n_long  = sum(long_conds)
        n_short = sum(short_conds)
        total   = len(long_conds)

        direction = None
        if n_long == total:       # A+：全部满足
            direction = "buy"
        elif n_short == total:
            direction = "sell"

        if direction is None:
            continue

        last_sig_t = t
        signals.append({
            "i": i, "t": t,
            "direction": direction,
            "price": c["c"],
            "atr": atr_v,
            "n_conds": total,
        })
    return signals

# ── 模拟交易 ──────────────────────────────────────────────
def simulate(k1m, signals, sl_mult=SL_MULT, tp_mult=TP_MULT,
             lot_oz=LOT_OZ, commission=COMMISSION):
    trades = []
    for sig in signals:
        entry = sig["price"]
        atr_v = sig["atr"]
        sl_d  = atr_v * sl_mult
        tp_d  = atr_v * tp_mult

        if sig["direction"] == "buy":
            sl, tp = entry - sl_d, entry + tp_d
        else:
            sl, tp = entry + sl_d, entry - tp_d

        result = "timeout"
        exit_p = k1m[min(sig["i"]+60, len(k1m)-1)]["c"]

        for j in range(sig["i"]+1, min(sig["i"]+61, len(k1m))):
            c = k1m[j]
            if sig["direction"] == "buy":
                if c["l"] <= sl: exit_p = sl; result = "sl"; break
                if c["h"] >= tp: exit_p = tp; result = "tp"; break
            else:
                if c["h"] >= sl: exit_p = sl; result = "sl"; break
                if c["l"] <= tp: exit_p = tp; result = "tp"; break

        gross = ((exit_p - entry) if sig["direction"] == "buy" else (entry - exit_p)) * lot_oz
        net   = gross - commission
        trades.append({
            "entry_t":   datetime.fromtimestamp(sig["t"], tz=CST).strftime("%m-%d %H:%M"),
            "dir":       sig["direction"],
            "entry":     entry,
            "exit":      exit_p,
            "sl_d":      round(sl_d, 2),
            "tp_d":      round(tp_d, 2),
            "gross":     round(gross, 2),
            "fee":       commission,
            "net":       round(net, 2),
            "result":    result,
            "risk_pct":  round(sl_d * lot_oz / CAPITAL * 100, 2),
        })
    return trades

def summarize(trades, label, capital=CAPITAL):
    if not trades:
        print(f"\n{label}: 无交易信号（条件过严）")
        return
    wins  = [t for t in trades if t["net"] > 0]
    loss  = [t for t in trades if t["net"] <= 0]
    gross = sum(t["gross"] for t in trades)
    fees  = sum(t["fee"] for t in trades)
    net   = sum(t["net"] for t in trades)
    wr    = len(wins)/len(trades)*100

    # 最大回撤
    equity, peak, max_dd = capital, capital, 0
    for t in trades:
        equity += t["net"]
        peak    = max(peak, equity)
        max_dd  = max(max_dd, peak - equity)

    avg_risk = sum(t["risk_pct"] for t in trades) / len(trades)
    pf = abs(sum(t["net"] for t in wins) / sum(t["net"] for t in loss)) if loss and sum(t["net"] for t in loss) != 0 else 999

    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  交易次数  : {len(trades)}  (买:{sum(1 for t in trades if t['dir']=='buy')}  卖:{sum(1 for t in trades if t['dir']=='sell')})")
    print(f"  胜率      : {wr:.1f}%  ({len(wins)}胜 / {len(loss)}负)")
    print(f"  毛盈亏    : ${gross:+.2f}")
    print(f"  总手续费  : ${-fees:.2f}  (每笔 ${fees/len(trades):.2f})")
    print(f"  净盈亏    : ${net:+.2f}   ({net/capital*100:+.2f}% 资金)")
    print(f"  每日净盈  : ${net/5:+.2f}")
    print(f"  盈亏比    : {pf:.2f}")
    print(f"  最大回撤  : ${max_dd:.2f}  ({max_dd/capital*100:.1f}% 资金)")
    print(f"  平均单笔风险: {avg_risk:.1f}% 资金")
    print(f"  TP触发: {sum(1 for t in trades if t['result']=='tp')}  "
          f"SL触发: {sum(1 for t in trades if t['result']=='sl')}  "
          f"超时: {sum(1 for t in trades if t['result']=='timeout')}")

    print(f"\n  交易明细（全部）:")
    print(f"  {'时间(CST)':<14} {'方向':<4} {'入场':>8} {'出场':>8} {'SL距':>5} {'TP距':>5} {'净盈亏':>8} {'风险%':>6} {'结果'}")
    for t in trades:
        d = "买▲" if t["dir"] == "buy" else "卖▼"
        print(f"  {t['entry_t']:<14} {d:<4} {t['entry']:>8.2f} {t['exit']:>8.2f} "
              f"{t['sl_d']:>5.2f} {t['tp_d']:>5.2f} {t['net']:>+8.2f} {t['risk_pct']:>5.1f}%  {t['result']}")


# ── 主程序 ────────────────────────────────────────────────
if __name__ == "__main__":
    print("="*60)
    print("  XAUUSD 职业策略回测 v3（最近5天）")
    print(f"  仓位: 0.05手=5oz | 资金: ${CAPITAL} | IC Markets费: $0.35/笔")
    print("="*60)

    print("\n[1] 拉取历史数据...")
    k1m = fetch_klines("1m", days=5)
    k5m = fetch_klines("5m", days=5)

    if len(k1m) < 100:
        print("数据不足"); exit(1)

    print("\n[2] 生成职业策略信号（10条件全满足）...")
    sigs = generate_pro_signals(k1m, k5m)
    print(f"  共发现 {len(sigs)} 个A+信号")

    trades = simulate(k1m, sigs)
    summarize(trades, "职业策略（10条件全满足 + 欧美盘 + 结构止损）")

    # 对比：放宽到8条件（允许2个不满足）
    print("\n[3] 宽松版：8/10条件满足（允许放量或回踩其一不满足）")

    def generate_relaxed(k1m, k5m, min_conds=8):
        e9   = calc_ema(k1m, 9);  e20 = calc_ema(k1m, 20);  e60 = calc_ema(k1m, 60)
        st1  = calc_supertrend(k1m, 10, 3)
        vwap = calc_vwap(k1m);    qqe = calc_qqe(k1m, 14, 5)
        atr  = calc_atr(k1m, 14); vma = calc_vol_ma(k1m, 10)
        e20_5m   = calc_ema(k5m, 20)
        st5      = calc_supertrend(k5m, 10, 3)
        slope_5m = calc_ema_slope(e20_5m, 3)
        st5_map    = {k5m[i]["t"]: st5[i]      for i in range(len(k5m))}
        slope5_map = {k5m[i]["t"]: slope_5m[i] for i in range(len(k5m))}
        def get5(t1m, mp):
            t5 = (t1m//300)*300
            return mp.get(t5, mp.get(t5-300, list(mp.values())[-1] if mp else 0))

        signals, last_sig_t = [], 0
        interval = k1m[1]["t"] - k1m[0]["t"]
        for i in range(65, len(k1m)):
            c = k1m[i]; t = c["t"]
            if (t - last_sig_t) < 5 * interval: continue
            hour_cst = datetime.fromtimestamp(t, tz=CST).hour
            min_cst  = datetime.fromtimestamp(t, tz=CST).minute
            in_us = (hour_cst == 21 and min_cst >= 30) or hour_cst in (22, 23)
            in_eu = 15 <= hour_cst < 18
            if not (in_us or in_eu): continue

            st5_trend = get5(t, st5_map); slope5 = get5(t, slope5_map)
            atr_v = atr[i]; vol_v = c["v"]; vol_avg = vma[i]

            long_conds = [
                st5_trend == 1, slope5 > 0, st1[i] == 1,
                c["c"] > vwap[i], e9[i] > e20[i] > e60[i],
                qqe[i] > 50, qqe[i] > qqe[i-1], c["c"] > c["o"],
                vol_v > vol_avg * 1.3,
                min(k1m[j]["l"] for j in range(max(0,i-5),i)) <= e20[i]*1.001,
            ]
            short_conds = [
                st5_trend == -1, slope5 < 0, st1[i] == -1,
                c["c"] < vwap[i], e9[i] < e20[i] < e60[i],
                qqe[i] < 50, qqe[i] < qqe[i-1], c["c"] < c["o"],
                vol_v > vol_avg * 1.3,
                max(k1m[j]["h"] for j in range(max(0,i-5),i)) >= e20[i]*0.999,
            ]
            direction = None
            if sum(long_conds) >= min_conds:  direction = "buy"
            elif sum(short_conds) >= min_conds: direction = "sell"
            if direction is None: continue
            last_sig_t = t
            signals.append({"i":i,"t":t,"direction":direction,"price":c["c"],"atr":atr_v,"n_conds":10})
        return signals

    sigs8 = generate_relaxed(k1m, k5m, min_conds=8)
    print(f"  共发现 {len(sigs8)} 个信号")
    trades8 = simulate(k1m, sigs8)
    summarize(trades8, "宽松版（8/10条件 + 欧美盘）")

    # 汇总
    print(f"\n{'='*60}")
    print(f"  汇总对比（资金 ${CAPITAL}，0.05手，IC Markets $0.35/笔）")
    print(f"{'='*60}")
    print(f"  {'方案':<30} {'次数':>5} {'胜率':>7} {'净盈亏':>10} {'占资金':>8} {'盈亏比':>7} {'最大回撤':>10}")
    print(f"  {'-'*30} {'-'*5} {'-'*7} {'-'*10} {'-'*8} {'-'*7} {'-'*10}")
    for label, tds in [("严格版(10/10条件)", trades), ("宽松版(8/10条件)", trades8)]:
        if not tds:
            print(f"  {label:<30} {'无信号':>5}"); continue
        wins = [t for t in tds if t["net"] > 0]
        loss = [t for t in tds if t["net"] <= 0]
        net  = sum(t["net"] for t in tds)
        wr   = len(wins)/len(tds)*100
        pf   = abs(sum(t["net"] for t in wins)/sum(t["net"] for t in loss)) if loss and sum(t["net"] for t in loss)!=0 else 999
        equity, peak, md = CAPITAL, CAPITAL, 0
        for t in tds:
            equity += t["net"]; peak = max(peak, equity); md = max(md, peak-equity)
        print(f"  {label:<30} {len(tds):>5} {wr:>6.1f}% {net:>+10.2f} {net/CAPITAL*100:>+7.1f}% {pf:>7.2f} {md:>8.2f}({md/CAPITAL*100:.1f}%)")
    print()
