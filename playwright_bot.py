#!/usr/bin/env python3
"""
MT5 网页端自动交易机器人（多账户并行版）
同时监听多个交易账户，收到信号后同步下单
"""

import time
import threading
import requests
from datetime import datetime
from playwright.sync_api import sync_playwright

# ── 全局配置 ──────────────────────────────────────────────────
SIGNAL_URL   = "http://localhost:8888/api/latest-signal"
POLL_SEC     = 5
STRATEGY     = "LB"     # "LB" / "L" / "all"
COOLDOWN_SEC = 60       # 每次下单后冷却时间（秒）

# ── 账户配置（在此添加/修改账户）────────────────────────────────
ACCOUNTS = [
    {
        "name":         "模拟账户",
        "url":          "https://web.metatrader.app/terminal?mode=demo&lang=zh",
        "login":        "5050465588",
        "password":     "8eWzDy!n",
        "lot_size":     "0.02",
        "enable_trade": True,
    },
    # {   # ← 暂停真实账户，测试完模拟盘后取消注释
    #     "name":         "真实账户",
    #     "url":          "https://webtrader.ic-cn-asia.com/",
    #     "login":        "15024243",
    #     "password":     "Ss@0985864281",
    #     "lot_size":     "0.02",
    #     "enable_trade": True,
    #     "server":       "ICMarketsSC-MT5-6",
    #     "profile_dir":  "/Users/jamie/.mt5_real_profile",
    # },
]

# ── 工具 ──────────────────────────────────────────────────────
def log(account_name, msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}][{account_name}] {msg}", flush=True)

def get_signal():
    try:
        return requests.get(SIGNAL_URL, timeout=5).json()
    except Exception as e:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 获取信号失败: {e}", flush=True)
        return None

def get_mt5_price(page, direction: str, name: str) -> float:
    try:
        idx = 1 if direction == "buy" else 0
        page.wait_for_selector('div.price-column', timeout=10000)
        el = page.locator('div.price-column').nth(idx)
        text = el.inner_text().strip().replace(' ', '').replace(',', '.')
        return float(text)
    except Exception as e:
        log(name, f"  ⚠ 读取价格失败: {e}")
        return 0.0

def calc_tp_sl(direction: str, mt5_price: float, atr: float, tp_mult: float = 3.0):
    if direction == "buy":
        tp = mt5_price + tp_mult * atr
        sl = mt5_price - 1 * atr
    else:
        tp = mt5_price - tp_mult * atr
        sl = mt5_price + 1 * atr
    return round(tp, 2), round(sl, 2)

def fill_input(page, selector, value):
    el = page.locator(selector).first
    el.click(click_count=3)
    page.keyboard.type(str(value))
    page.wait_for_timeout(150)

# ── 点击连接账户按钮（Continue to MT5 之后）──────────────────────────
def _fill_login_form(page, cfg):
    """Continue to MT5 后：若已自动登录则跳过，否则点击「连接到账户」"""
    name = cfg["name"]
    page.wait_for_timeout(3000)  # 等页面渲染
    # 持久化 session 可能直接进图表，无需点按钮
    if _is_logged_in(page, timeout=3000):
        log(name, "✅ session 已自动恢复，无需点击连接按钮")
        return
    for label in ["连接到账户", "連接到帳戶", "Connect", "Sign in"]:
        # 方式1: button 过滤文本
        try:
            btn = page.locator('button').filter(has_text=label).first
            if btn.is_visible(timeout=3000):
                btn.click()
                log(name, f"点击连接按钮: {label}")
                return
        except:
            pass
        # 方式2: get_by_role
        try:
            btn = page.get_by_role("button", name=label).first
            if btn.is_visible(timeout=2000):
                btn.click()
                log(name, f"点击连接按钮(role): {label}")
                return
        except:
            pass
        # 方式3: get_by_text（匹配任意元素）
        try:
            btn = page.get_by_text(label, exact=True).last  # last 取右下角的按钮
            if btn.is_visible(timeout=2000):
                btn.click()
                log(name, f"点击连接按钮(text): {label}")
                return
        except:
            pass
    log(name, "⚠ 未找到连接按钮，等待手动操作...")


# ── 服务器选择（IC Markets 等需要先选服务器的平台）─────────────────
def select_server_if_needed(page, cfg):
    """若配置了 server 字段，在登录前先选择 MT5 服务器"""
    server = cfg.get("server")
    if not server:
        return
    name = cfg["name"]

    # 判断服务器选择页面是否存在
    try:
        page.wait_for_selector('text=Continue to MT5', timeout=6000)
    except:
        return  # 不是服务器选择页面，跳过

    log(name, "检测到服务器选择页面")

    # 点击 MetaTrader5 按钮
    try:
        btn = page.get_by_text("MetaTrader5", exact=True).first
        if btn.is_visible(timeout=3000):
            btn.click()
            log(name, "点击 MetaTrader5")
            page.wait_for_timeout(600)
    except:
        pass

    # 选择服务器（原生 <select> 下拉框）
    try:
        sel_el = page.locator('select').first
        sel_el.wait_for(state="visible", timeout=5000)
        # 用 JS 强制找到匹配的 option 并选中，最可靠
        result = page.evaluate(f"""(server) => {{
            const sel = document.querySelector('select');
            if (!sel) return 'no select';
            const opts = Array.from(sel.options);
            const opt = opts.find(o => o.text.includes(server) || o.value.includes(server));
            if (!opt) return 'not found: ' + opts.map(o=>o.text).join(',');
            sel.value = opt.value;
            sel.dispatchEvent(new Event('change', {{bubbles: true}}));
            return 'ok: ' + opt.text;
        }}""", server)
        log(name, f"服务器选择结果: {result}")
        page.wait_for_timeout(800)
    except Exception as e:
        log(name, f"⚠ 服务器下拉选择失败: {e}")

    # 点击 Continue to MT5
    try:
        cont = page.get_by_text("Continue to MT5", exact=True).first
        cont.wait_for(state="visible", timeout=5000)
        cont.click()
        log(name, "点击 Continue to MT5")
        page.wait_for_timeout(3000)
    except Exception as e:
        log(name, f"⚠ Continue to MT5 点击失败: {e}")
        return

    # Continue 之后填写登录表单（兼容 iframe）
    _fill_login_form(page, cfg)


# ── 已登录检测（canvas 或其他交易界面元素）────────────────────────
def _is_logged_in(page, timeout=5000) -> bool:
    """等待 canvas（MT5 图表）出现，出现即视为已登录"""
    try:
        page.wait_for_selector('canvas', timeout=timeout)
        return True
    except:
        return False

# ── 登录 ──────────────────────────────────────────────────────
def login(page, cfg):
    name = cfg["name"]
    log(name, "等待页面加载...")

    # 持久化 session 恢复较慢给 25 秒；普通账户 5 秒判断未登录即走登录流程
    init_timeout = 25000 if cfg.get("profile_dir") else 5000
    if _is_logged_in(page, timeout=init_timeout):
        log(name, "✅ 已登录，跳过登录步骤")
        return

    log(name, "未检测到图表，尝试登录...")

    # 有 server 配置的账户（如 IC Markets），服务器选择+连接账户完成后直接等 canvas
    if cfg.get("server"):
        select_server_if_needed(page, cfg)
        if _is_logged_in(page, timeout=20000):
            log(name, "✅ 登录成功")
        else:
            log(name, "⚠ 登录超时，请手动完成登录")
        return

    select_server_if_needed(page, cfg)

    # ① 优先点击左侧已保存账户
    try:
        saved = page.locator(f'text={cfg["login"]}').first
        if saved.is_visible(timeout=3000):
            saved.click()
            log(name, f"点击已保存账户: {cfg['login']}")
            page.wait_for_timeout(1500)
            if _is_logged_in(page, timeout=10000):
                log(name, "✅ 已登录（已保存账户）")
                return
    except:
        pass

    # ② 点击「连接到账户」入口
    for label in ["连接到账户", "連接到帳戶", "Sign in to account", "Connect to account"]:
        try:
            el = page.locator(f'text={label}').first
            if el.is_visible(timeout=2000):
                el.click()
                log(name, f"点击: {label}")
                break
        except:
            continue
    page.wait_for_timeout(1000)

    # ③ 填写账号密码
    login_sel_list = [
        'input[name="login"]', 'input[placeholder="输入登录名"]',
        'input[placeholder*="登录"]', 'input[placeholder*="Login"]',
        'input[type="text"]', 'input[type="number"]',
    ]
    for sel in login_sel_list:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=1500):
                el.click(click_count=3)
                page.keyboard.type(cfg["login"])
                log(name, f"填写账号 [{sel}]")
                break
        except:
            continue

    pwd_sel_list = [
        'input[name="password"]', 'input[placeholder="输入密码"]',
        'input[placeholder*="密码"]', 'input[placeholder*="Password"]',
        'input[type="password"]',
    ]
    for sel in pwd_sel_list:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=1500):
                el.click(click_count=3)
                page.keyboard.type(cfg["password"])
                log(name, f"填写密码 [{sel}]")
                break
        except:
            continue

    # ④ 点击登录按钮
    connected = False
    for label in ["连接到账户", "連接到帳戶", "Connect", "Sign in"]:
        try:
            btn = page.locator('button').filter(has_text=label).first
            if btn.is_visible(timeout=2000):
                btn.click()
                connected = True
                log(name, f"点击登录按钮: {label}")
                break
        except:
            continue
    if not connected:
        try:
            page.locator('button.button').first.click(timeout=5000)
            log(name, "点击登录按钮（兜底）")
        except:
            log(name, "⚠ 未找到登录按钮，等待手动操作...")

    if _is_logged_in(page, timeout=30000):
        log(name, "✅ 登录成功")
    else:
        log(name, "⚠ 登录超时，请手动完成登录")

# ── 会话检测 ──────────────────────────────────────────────────
def ensure_logged_in(page, cfg) -> bool:
    """只检查 canvas 是否存在，不做任何页面跳转，避免破坏已登录状态"""
    return _is_logged_in(page, timeout=15000)

# ── 下单 ──────────────────────────────────────────────────────
def place_order(page, cfg, direction: str, atr: float, tp_mult: float = 3.0):
    name     = cfg["name"]
    lot_size = cfg["lot_size"]
    enable   = cfg["enable_trade"]
    try:
        if not ensure_logged_in(page, cfg):
            log(name, "❌ session 无效，跳过本次下单")
            return False
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        page.get_by_text("新订单", exact=True).click()
        page.wait_for_timeout(2000)

        mt5_price = get_mt5_price(page, direction, name)
        if mt5_price == 0:
            log(name, "  ❌ 无法读取价格，跳过本次下单")
            page.keyboard.press("Escape")
            return False

        tp, sl = calc_tp_sl(direction, mt5_price, atr, tp_mult)
        log(name, f"下单: {direction.upper()}  价={mt5_price:.2f}  ATR={atr:.2f}  TP={tp:.2f}  SL={sl:.2f}")

        fill_input(page, 'div.volume input', lot_size)
        log(name, f"  ✓ 交易量: {lot_size}")
        fill_input(page, 'div.sl input', f"{sl:.2f}")
        log(name, f"  ✓ 止损(SL): {sl:.2f}")
        fill_input(page, 'div.tp input', f"{tp:.2f}")
        log(name, f"  ✓ 止盈(TP): {tp:.2f}")
        page.wait_for_timeout(300)

        if not enable:
            page.screenshot(path=f"/tmp/mt5_{name}_{direction}_{int(time.time())}.png")
            log(name, f"  ⚠ enable_trade=False，截图已存 /tmp/，不实际下单")
            page.keyboard.press("Escape")
            return True

        if direction == "buy":
            page.locator('button.trade-button:not(.red)').first.click()
            log(name, "  ✓ 点击 Buy by Market")
        else:
            page.locator('button.trade-button.red').first.click()
            log(name, "  ✓ 点击 Sell by Market")

        page.wait_for_timeout(2000)
        log(name, f"✅ 下单完成: {direction.upper()} {lot_size}手")

        for ok_label in ["好的", "好", "OK", "Ok"]:
            try:
                btn = page.locator('button').filter(has_text=ok_label).first
                if btn.is_visible(timeout=3000):
                    btn.click()
                    log(name, f"  ✓ 关闭确认弹窗: {ok_label}")
                    page.wait_for_timeout(500)
                    break
            except:
                continue

        page.wait_for_timeout(500)
        try:
            page.locator('canvas').first.click(position={"x": 400, "y": 300})
        except:
            pass
        page.wait_for_timeout(500)
        return True

    except Exception as e:
        log(name, f"❌ 下单失败: {e}")
        try:
            page.screenshot(path=f"/tmp/mt5_error_{name}_{int(time.time())}.png")
        except: pass
        return False

# ── 单账户主循环 ───────────────────────────────────────────────
def run_account(cfg):
    """每个账户在自己的线程里创建独立的 sync_playwright 实例"""
    name        = cfg["name"]
    profile_dir = cfg.get("profile_dir")
    with sync_playwright() as playwright:
        if profile_dir:
            # 持久化 context：session/cookie 保存到本地，登录一次即可
            import os; os.makedirs(profile_dir, exist_ok=True)
            context = playwright.chromium.launch_persistent_context(
                profile_dir,
                headless=False,
                args=["--start-maximized"],
                viewport={"width": 1440, "height": 900},
            )
            page = context.pages[0] if context.pages else context.new_page()
        else:
            browser = playwright.chromium.launch(headless=False, args=["--start-maximized"])
            page    = browser.new_page(viewport={"width": 1440, "height": 900})

        log(name, f"打开交易页面: {cfg['url']}")
        page.goto(cfg["url"], wait_until="networkidle")
        login(page, cfg)

        # 选中 XAUUSD（仅模拟账户需要，真实账户默认已是 XAUUSD）
        if not cfg.get("profile_dir"):
            try:
                search = page.locator('input[placeholder*="搜索"]').first
                search.fill("XAUUSD")
                page.wait_for_timeout(1000)
                page.locator('button.item').filter(has_text="XAUUSD").first.click()
                page.wait_for_timeout(1000)
                log(name, "✅ 已选中 XAUUSD")
            except:
                log(name, "⚠ 自动选择 XAUUSD 失败，请手动点击")

        try:
            page.get_by_text("新订单", exact=True).wait_for(timeout=5000)
            log(name, "✅ 新订单按钮已就绪")
        except:
            log(name, "⚠ 新订单按钮未找到，请确认图表已打开")

        log(name, f"开始监听  策略={STRATEGY}  手数={cfg['lot_size']}  下单={'✅开启' if cfg['enable_trade'] else '⚠模拟'}")
        log(name, "=" * 40)

        # 启动时先获取当前信号 ts，跳过历史信号，只响应新信号
        init_sig = get_signal()
        last_ts  = init_sig.get("ts", "0") if init_sig else "0"
        if last_ts != "0":
            log(name, f"⏭ 跳过启动时已有信号 ts={last_ts}，等待新信号...")

        last_order_ts      = 0
        last_session_check = time.time()

        while True:
            try:
                if time.time() - last_session_check > 1800:
                    if not _is_logged_in(page, timeout=5000):
                        log(name, "⚠ 定期检查：canvas 消失，尝试重新登录...")
                        page.goto(cfg["url"], wait_until="networkidle", timeout=30000)
                        page.wait_for_timeout(2000)
                        login(page, cfg)
                    last_session_check = time.time()

                sig = get_signal()
                if not sig or not sig.get("direction") or sig.get("ts", "0") == "0":
                    time.sleep(POLL_SEC); continue

                ts        = sig["ts"]
                direction = sig["direction"]
                strategy  = sig.get("strategy", "original")
                price     = float(sig.get("price") or 0)
                tf        = sig.get("tf", "?")
                atr       = float(sig.get("atr") or 0)

                if ts == last_ts:
                    time.sleep(POLL_SEC); continue

                age = time.time() - int(ts)
                if age > 300:
                    last_ts = ts; time.sleep(POLL_SEC); continue

                if STRATEGY != "all" and strategy != STRATEGY:
                    last_ts = ts; time.sleep(POLL_SEC); continue

                if atr == 0:
                    log(name, "⚠ ATR 为0，跳过")
                    last_ts = ts; time.sleep(POLL_SEC); continue

                since_last = time.time() - last_order_ts
                if since_last < COOLDOWN_SEC:
                    log(name, f"⏳ 冷却中 {int(since_last)}s，跳过")
                    last_ts = ts; time.sleep(POLL_SEC); continue

                tp_mult = 3.0 if strategy == "LB" else (2.5 if strategy == "L" else 3.0)
                log(name, f"🔔 {direction.upper()} [{tf}] 策略={strategy}  价={price:.2f}  ATR={atr:.2f}  ({int(age)}秒前)")
                ok = place_order(page, cfg, direction, atr, tp_mult)
                last_ts = ts
                if ok:
                    last_order_ts = time.time()

            except Exception as e:
                log(name, f"主循环异常: {e}")

            time.sleep(POLL_SEC)

# ── 主入口 ─────────────────────────────────────────────────────
def main():
    threads = []
    for cfg in ACCOUNTS:
        t = threading.Thread(
            target=run_account,
            args=(cfg,),
            name=cfg["name"],
            daemon=True
        )
        threads.append(t)
        t.start()
        time.sleep(3)   # 错开启动时间，避免同时抢占资源

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 已启动 {len(ACCOUNTS)} 个账户线程", flush=True)
    for t in threads:
        t.join()

if __name__ == "__main__":
    main()
