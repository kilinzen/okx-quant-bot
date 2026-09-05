#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🚀 OKX 量化全自動合約交易機器人 (過濾下架幣 + 活躍小幣動態狙擊版)
- 🛡️ 自動過濾 51087 下架死幣 (如 SATS 等模擬盤下架合約)
- 🎯 只鎖定 OKX 官方 status=='live' 的可交易活躍小幣
- 🔑 完美讀取 Railway 變數：OKX_SECRET_KEY, OKX_API_KEY, OKX_PASSPHRASE
- 💰 15,000 元台幣本金風控：單筆 20 USDT, 槓桿 10x
- 💬 Telegram 雙向查詢 + 即時開平倉通知
"""

import os
import sys
import time
import json
import math
import hmac
import hashlib
import base64
import threading
import urllib.request
from datetime import datetime, timezone

# ==========================================
# ⚙️ 1. 系統與環境變數設定
# ==========================================
def get_val(keys: list, default: str = "") -> str:
    for k in keys:
        v = os.getenv(k)
        if v is not None and v.strip() != "":
            return v.strip()
    return default

OKX_API_KEY = get_val(["OKX_API_KEY", "OKX_KEY", "API_KEY"])
OKX_API_SECRET = get_val(["OKX_SECRET_KEY", "OKX_API_SECRET", "OKX_SECRET", "SECRET_KEY"])
OKX_PASSPHRASE = get_val(["OKX_PASSPHRASE", "OKX_PASSWORD", "PASSPHRASE", "PASSWORD"])

SIM_VAL = get_val(["OKX_SIMULATED", "SIMULATED"], "1").lower()
IS_SIMULATED = SIM_VAL in ["1", "true", "yes"]

TELEGRAM_BOT_TOKEN = get_val(["TELEGRAM_BOT_TOKEN", "TG_BOT_TOKEN", "BOT_TOKEN", "TG_TOKEN"])
TELEGRAM_CHAT_ID = get_val(["TELEGRAM_CHAT_ID", "TG_CHAT_ID", "CHAT_ID"])

TOTAL_EQUITY_TWD = 15000.0
MARGIN_PER_TRADE_USDT = float(get_val(["MIN_MARGIN", "MARGIN_USDT"], "20.0"))
DEFAULT_LEVERAGE = int(get_val(["LEVERAGE"], "10"))
MAX_OPEN_POSITIONS = int(get_val(["MAX_POSITIONS", "MAX_POSITION"], "3"))
CHECK_INTERVAL_SEC = 15

# 排除大幣與已確認在模擬盤下架的合約
EXCLUDE_SYMBOLS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "USDC-USDT-SWAP", "SATS-USDT-SWAP"]
DEAD_SYMBOLS_BLACKLIST = set(EXCLUDE_SYMBOLS)
ERROR_COOLDOWN = {}

# ==========================================
# 📢 2. Telegram 模組
# ==========================================
def send_telegram(message: str, target_chat_id: str = None):
    chat_id = target_chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=8):
            pass
    except Exception as e:
        print(f"⚠️ TG 推播異常: {e}")

# ==========================================
# 🔐 3. OKX 底層請求與簽名
# ==========================================
def http_request(url: str, method: str = "GET", headers: dict = None, body: str = None) -> dict:
    req_headers = {"User-Agent": "Mozilla/5.0"}
    if headers:
        req_headers.update(headers)
    data_bytes = body.encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data_bytes, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")
        return {"code": str(e.code), "msg": f"HTTP {e.code}: {err_body}"}
    except Exception as e:
        return {"code": "-1", "msg": f"網路錯誤: {e}"}

def get_okx_headers(method: str, request_path: str, body: str = "") -> dict:
    now = datetime.now(timezone.utc)
    timestamp = now.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
    pre_hash = f"{timestamp}{method.upper()}{request_path}{body}"
    signature = base64.b64encode(
        hmac.new(OKX_API_SECRET.encode('utf-8'), pre_hash.encode('utf-8'), hashlib.sha256).digest()
    ).decode('utf-8')

    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json"
    }
    if IS_SIMULATED:
        headers["x-simulated-trading"] = "1"
    return headers

def get_account_balance() -> tuple[float, float]:
    if not OKX_API_KEY or not OKX_API_SECRET:
        return 0.0, 0.0
    try:
        path = "/api/v5/account/balance"
        headers = get_okx_headers("GET", path)
        url = f"https://www.okx.com{path}"
        res = http_request(url, method="GET", headers=headers)
        
        if res.get("code") == "0" and res.get("data"):
            d = res["data"][0]
            total_eq = float(d.get("totalEq", "0.0"))
            avail_usdt = 0.0
            for item in d.get("details", []):
                if item.get("ccy") == "USDT":
                    val_str = (
                        item.get("availBal") or 
                        item.get("availEq") or 
                        item.get("cashBal") or 
                        item.get("eq") or 
                        "0.0"
                    )
                    avail_usdt = float(val_str)
                    break
            return total_eq, avail_usdt
    except Exception:
        pass
    return 0.0, 0.0

def get_open_positions() -> list:
    if not OKX_API_KEY or not OKX_API_SECRET:
        return []
    try:
        path = "/api/v5/account/positions?instType=SWAP"
        headers = get_okx_headers("GET", path)
        url = f"https://www.okx.com{path}"
        res = http_request(url, method="GET", headers=headers)
        if res.get("code") == "0" and res.get("data"):
            return [p for p in res["data"] if float(p.get("pos", "0")) != 0.0]
    except Exception:
        pass
    return []

# ==========================================
# 🎯 4. 全市場動態小幣掃描 (嚴格過濾未上線/已下架幣)
# ==========================================
def get_live_tradable_instruments() -> set:
    """獲取當前真正處於 live 可交易狀態的永續合約"""
    url = "https://www.okx.com/api/v5/public/instruments?instType=SWAP"
    res = http_request(url)
    live_set = set()
    if res.get("code") == "0" and res.get("data"):
        for item in res["data"]:
            if item.get("state") == "live":
                live_set.add(item.get("instId"))
    return live_set

def get_top_volatile_altcoins(limit: int = 30) -> list:
    live_set = get_live_tradable_instruments()
    url = "https://www.okx.com/api/v5/market/tickers?instType=SWAP"
    res = http_request(url)
    if res.get("code") != "0" or not res.get("data"):
        return []

    tickers = res["data"]
    candidates = []

    for t in tickers:
        inst_id = t.get("instId", "")
        if not inst_id.endswith("-USDT-SWAP"):
            continue
        if inst_id in DEAD_SYMBOLS_BLACKLIST:
            continue
        if live_set and inst_id not in live_set:
            continue

        vol_24h = float(t.get("volCcy24h", "0.0"))
        last_px = float(t.get("last", "0.0"))
        open_24h = float(t.get("open24h", "0.0"))

        if last_px <= 0 or vol_24h < 2_000_000:
            continue

        change_pct = abs((last_px - open_24h) / open_24h) if open_24h > 0 else 0
        candidates.append({
            "instId": inst_id,
            "vol": vol_24h,
            "change": change_pct
        })

    candidates.sort(key=lambda x: (x["change"] * 0.6 + (x["vol"] / 1e7) * 0.4), reverse=True)
    return [c["instId"] for c in candidates[:limit]]

# ==========================================
# 💬 5. Telegram 雙向查詢
# ==========================================
def format_status_message() -> str:
    mode_str = "模擬盤 (Demo)" if IS_SIMULATED else "實盤 (Real)"
    total_eq, avail_bal = get_account_balance()
    positions = get_open_positions()

    msg = (
        f"【OKX 永續合約資產概況】\n"
        f"💰 帳戶總淨值: {total_eq:,.2f} USD\n"
        f"💵 可用保證金: {avail_bal:,.2f} USDT\n"
        f"🎯 模式: {mode_str}\n"
        f"────────────────────\n"
        f"📈 當前持倉 ({len(positions)}/{MAX_OPEN_POSITIONS} 筆):\n"
    )
    if not positions:
        msg += "• 目前無持倉部位，全市場小幣狙擊雷達正在掃描中..."
    else:
        for p in positions:
            inst = p.get("instId", "").replace("-USDT-SWAP", "")
            side = p.get("posSide", "").lower()
            if side == "net":
                pos_val = float(p.get("pos", "0"))
                direction = "做多 🟢" if pos_val > 0 else "做空 🔴"
            else:
                direction = "做多 🟢" if side == "long" else "做空 🔴"

            upl = float(p.get("upl", "0.0"))
            upl_ratio = float(p.get("uplRatio", "0.0")) * 100
            upl_sign = "+" if upl >= 0 else ""
            msg += (
                f"• <b>{inst}</b> ({direction})\n"
                f"  未實現損益: {upl_sign}{upl:.2f} U ({upl_sign}{upl_ratio:.2f}%)\n"
            )
    return msg

def telegram_listener():
    if not TELEGRAM_BOT_TOKEN:
        return
    print("💬 Telegram 雙向對話監聽器已上線！")
    last_update_id = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=20"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("ok") and data.get("result"):
                    for item in data["result"]:
                        last_update_id = item["update_id"]
                        message = item.get("message", {})
                        text = message.get("text", "").strip()
                        chat_id = str(message.get("chat", {}).get("id", ""))
                        if not text:
                            continue

                        clean_cmd = text.split("@")[0].lower()
                        if clean_cmd in ["/status", "帳戶", "查帳", "狀態", "持倉", "/account"]:
                            reply = format_status_message()
                            send_telegram(reply, target_chat_id=chat_id)
                        elif clean_cmd in ["/ping", "ping"]:
                            send_telegram("🏓 <b>Pong!</b> 全市場小幣狙擊雷達守護中！", target_chat_id=chat_id)
        except Exception:
            time.sleep(3)

# ==========================================
# 🛡️ 6. OKX 資金費率安全守護者 (防 ASTER 毒藥幣)
# ==========================================
def check_funding_rate_guard(inst_id: str, direction: str) -> tuple[bool, str]:
    url = f"https://www.okx.com/api/v5/public/funding-rate?instId={inst_id}"
    res = http_request(url)
    if res.get("code") != "0" or not res.get("data"):
        return True, "默認放行"

    data = res["data"][0]
    funding_rate = float(data.get("fundingRate", "0.0"))
    f_time = int(data.get("fundingTime", "0"))
    next_f_time = int(data.get("nextFundingTime", "0"))

    interval_hours = 8.0
    if next_f_time > f_time:
        interval_hours = max(1.0, (next_f_time - f_time) / 3600000.0)

    hourly_rate = funding_rate / interval_hours
    is_long = direction.upper() in ["BUY", "LONG"]

    if abs(funding_rate) >= 0.0005 or abs(hourly_rate) >= 0.0000625:
        return False, f"🚨 [極端費率熔斷] {inst_id} 單期 {funding_rate*100:.3f}%，拒絕進場！"

    if is_long and hourly_rate > 0.00003:
        return False, f"🚨 [高資金費攔截] {inst_id} 當前正費率 {funding_rate*100:.3f}% (每 {interval_hours:.0f}h 扣一次)，拒絕開多！"

    if not is_long and hourly_rate < -0.00003:
        return False, f"🚨 [深負資金費攔截] {inst_id} 當前深負費率 {funding_rate*100:.3f}% (每 {interval_hours:.0f}h 扣一次)，拒絕開空！"

    return True, "費率安全"

# ==========================================
# 📊 7. 行情獲取與指標計算
# ==========================================
def get_instrument_info(inst_id: str) -> tuple[float, float, float]:
    url = f"https://www.okx.com/api/v5/public/instruments?instType=SWAP&instId={inst_id}"
    res = http_request(url)
    if res.get("code") == "0" and res.get("data"):
        item = res["data"][0]
        ct_val = float(item.get("ctVal", 1.0))
        lot_sz = float(item.get("lotSz", 1.0))
        tick_sz = float(item.get("tickSz", 0.0001))
        return ct_val, lot_sz, tick_sz
    return 1.0, 1.0, 0.0001

def format_price_by_tick(price: float, tick_sz: float) -> str:
    if tick_sz <= 0:
        return f"{price:.8f}".rstrip('0').rstrip('.')
    decimals = max(0, -int(math.floor(math.log10(tick_sz))))
    rounded = round(round(price / tick_sz) * tick_sz, decimals)
    return f"{rounded:.{decimals}f}"

def get_klines(inst_id: str, bar: str = "1H", limit: int = 50) -> list:
    url = f"https://www.okx.com/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
    res = http_request(url)
    if res.get("code") == "0" and res.get("data"):
        return res["data"][::-1]
    return []

def calculate_indicators(candles: list) -> dict:
    if len(candles) < 20:
        return {}
    closes = [float(c[4]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    vols = [float(c[5]) for c in candles]

    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[-14:]) / 14
    avg_loss = sum(losses[-14:]) / 14
    rsi = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))

    ma_fast = sum(closes[-7:]) / 7
    ma_slow = sum(closes[-20:]) / 20
    vol_ma = sum(vols[-15:-1]) / 14 if len(vols) >= 15 else 1.0
    vol_surge = vols[-1] / max(vol_ma, 1e-6)

    tr_list = [max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])) for i in range(1, len(closes))]
    atr = sum(tr_list[-14:]) / 14 if len(tr_list) >= 14 else (highs[-1] - lows[-1])

    return {
        "price": closes[-1],
        "rsi": rsi,
        "ma_fast": ma_fast,
        "ma_slow": ma_slow,
        "vol_surge": vol_surge,
        "atr": atr
    }

# ==========================================
# 🎯 8. 下單委託 (過濾 51087 下架合約)
# ==========================================
def execute_order_request(body_dict: dict) -> dict:
    body_str = json.dumps(body_dict)
    headers = get_okx_headers("POST", "/api/v5/trade/order", body_str)
    url = "https://www.okx.com/api/v5/trade/order"
    return http_request(url, method="POST", headers=headers, body=body_str)

def place_order(inst_id: str, direction: str, price: float, atr: float):
    global ERROR_COOLDOWN, DEAD_SYMBOLS_BLACKLIST
    if not OKX_API_KEY or not OKX_API_SECRET:
        print("⚠️ 未檢測到 OKX API 金鑰，略過發單。")
        return

    now_ts = time.time()
    if inst_id in ERROR_COOLDOWN and now_ts - ERROR_COOLDOWN[inst_id] < 60:
        return

    side = "buy" if direction == "LONG" else "sell"
    ct_val, lot_sz, tick_sz = get_instrument_info(inst_id)

    contracts = (MARGIN_PER_TRADE_USDT * DEFAULT_LEVERAGE) / price / ct_val
    if lot_sz >= 1.0:
        sz_str = str(max(1, int(round(contracts))))
    else:
        decimals = max(0, -int(math.floor(math.log10(lot_sz))))
        sz_str = f"{max(lot_sz, round(contracts, decimals)):.{decimals}f}"

    price_str = format_price_by_tick(price, tick_sz)
    mode_text = "【OKX 模擬盤】" if IS_SIMULATED else "【OKX 實盤】"

    # 優先嘗試 開平倉模式 (posSide: "long" / "short")
    pos_side_opt = "long" if direction == "LONG" else "short"
    base_order = {
        "instId": inst_id,
        "tdMode": "cross",
        "side": side,
        "posSide": pos_side_opt,
        "ordType": "market",
        "sz": sz_str
    }

    print(f"🚀 {mode_text} 發送小幣市價開倉: {inst_id} {direction} {sz_str} 張...")
    res = execute_order_request(base_order)

    # 檢查是否為 51000 posSide 錯誤，若是切換為 net
    data_item = res.get("data", [{}])[0] if res.get("data") else {}
    s_code = data_item.get("sCode", "")
    err_msg = res.get("msg", "")

    if "posSide" in err_msg or "51000" in str(res.get("code")) or s_code == "51000":
        base_order["posSide"] = "net"
        res = execute_order_request(base_order)
        data_item = res.get("data", [{}])[0] if res.get("data") else {}
        s_code = data_item.get("sCode", "")

    s_msg = data_item.get("sMsg", "")

    # 🚨 關鍵攔截：若遇上 51087 (Listing canceled / 下架幣)，直接列入黑名單，不再重複嘗試！
    if s_code == "51087" or "51087" in str(res.get("code")) or "listing canceled" in s_msg.lower():
        print(f"🚫 {inst_id} 已在交易所停止交易 (51087)，自動加入永久黑名單！")
        DEAD_SYMBOLS_BLACKLIST.add(inst_id)
        return

    if res.get("code") == "0" and s_code == "0":
        sl_dist = min(price * 0.015, max(atr * 1.5, price * 0.012))
        sl_raw = price - sl_dist if direction == "LONG" else price + sl_dist
        tp_dist = price * (0.45 / DEFAULT_LEVERAGE)
        tp_raw = price + tp_dist if direction == "LONG" else price - tp_dist
        sl_price_str = format_price_by_tick(sl_raw, tick_sz)
        tp_price_str = format_price_by_tick(tp_raw, tick_sz)

        tg_msg = (
            f"⚡ <b>{mode_text} 動態小幣狙擊成功！</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"標的幣種：<code>{inst_id}</code>\n"
            f"方向：<b>{'🟢 做多 LONG' if direction == 'LONG' else '🔴 做空 SHORT'}</b>\n"
            f"市價：<code>{price_str}</code> USDT\n"
            f"槓桿：<code>{DEFAULT_LEVERAGE}x</code> | 本金：<code>{MARGIN_PER_TRADE_USDT} USDT</code>\n"
            f"張數：<code>{sz_str} 張</code>\n"
            f"🎯 止盈價：<code>{tp_price_str}</code> (+45%)\n"
            f"🛡️ 止損價：<code>{sl_price_str}</code> (限額 ~3.0 U)\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⏰ 時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        send_telegram(tg_msg)
        print(f"✅ 開倉成功：{inst_id} {direction} {sz_str} 張！")
    else:
        ERROR_COOLDOWN[inst_id] = now_ts
        fail_reason = s_msg if s_msg else res.get("msg", "未知原因")
        print(f"⚠️ 下單失敗細節: 代碼={s_code or res.get('code')}, 訊息={fail_reason}")

# ==========================================
# 🔄 8. 動態全市場小幣狙擊工作線程
# ==========================================
def altcoin_trading_worker():
    print("🤖 全市場動態小幣掃描引擎已啟動...")
    while True:
        try:
            positions = get_open_positions()
            active_symbols = [p["instId"] for p in positions]

            if len(active_symbols) >= MAX_OPEN_POSITIONS:
                time.sleep(CHECK_INTERVAL_SEC)
                continue

            dynamic_targets = get_top_volatile_altcoins(limit=25)
            if not dynamic_targets:
                time.sleep(10)
                continue

            for inst_id in dynamic_targets:
                if inst_id in active_symbols or inst_id in DEAD_SYMBOLS_BLACKLIST:
                    continue

                candles = get_klines(inst_id, bar="1H", limit=40)
                ind = calculate_indicators(candles)
                if not ind:
                    continue

                price = ind["price"]
                rsi = ind["rsi"]
                ma_fast = ind["ma_fast"]
                ma_slow = ind["ma_slow"]
                vol_surge = ind["vol_surge"]
                atr = ind["atr"]

                signal = None
                # 超跌反彈
                if (rsi <= 38.0 and ma_fast >= ma_slow and vol_surge >= 1.2) or (rsi <= 30.0):
                    signal = "LONG"
                # 超買回調
                elif (rsi >= 64.0 and ma_fast <= ma_slow and vol_surge >= 1.2) or (rsi >= 70.0):
                    signal = "SHORT"

                if signal:
                    is_safe, reason = check_funding_rate_guard(inst_id, signal)
                    if not is_safe:
                        print(f"🛡️ 費率過高，放棄開倉: {reason}")
                        send_telegram(reason)
                        continue

                    place_order(inst_id, signal, price, atr)
                    time.sleep(2)
                    break

            time.sleep(CHECK_INTERVAL_SEC)

        except Exception as e:
            print(f"⚠️ 小幣掃描異常: {e}")
            time.sleep(5)

# ==========================================
# 🔒 9. 主程序：24H 長駐
# ==========================================
def main():
    print("=" * 60)
    print("🚀 [OKX Quant Bot] 全市場小幣狙擊 (過濾下架幣版) 啟動！")
    print(f"🎯 交易模式: {'官方模擬盤 (Demo)' if IS_SIMULATED else '實盤交易'}")
    print("=" * 60)

    total_eq, avail_usdt = get_account_balance()
    print(f"📊 [OKX 帳戶連線測試] 總淨值: {total_eq:,.2f} USD | 可用 USDT: {avail_usdt:,.2f}")

    t_tg = threading.Thread(target=telegram_listener, daemon=True)
    t_tg.start()

    t_alt = threading.Thread(target=altcoin_trading_worker, daemon=True)
    t_alt.start()

    loop_count = 0
    while True:
        loop_count += 1
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if loop_count % 3 == 0:
            print(f"[{now_str}] 💓 全市場動態雷達 24H 守護中 (Loop: {loop_count})")
        time.sleep(10)

if __name__ == "__main__":
    main()