#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🚀 OKX 量化全自動合約交易機器人 (Railway 永不退出強韌版)
- 🔒 主線程強韌心跳硬鎖：徹底防止任何異常導致容器 Exit / Stopping Container
- 💰 本金規格：15,000 TWD (單筆 20 USDT, 槓桿 10x, 嚴格止損)
- 💬 Telegram 雙向查詢：在群組輸入「帳戶」、「/status」、「查帳」秒回
- 🛡️ 資金費率守護者：實時攔截 ASTER 類高頻吸血幣
"""

import os
import sys
import time
import json
import math
import hmac
import hashlib
import base64
import traceback
import threading
import urllib.request
from datetime import datetime, timezone

# ==========================================
# ⚙️ 1. 系統與環境變數設定
# ==========================================
OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_API_SECRET = os.getenv("OKX_API_SECRET", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")
IS_SIMULATED = os.getenv("OKX_SIMULATED", "0").lower() in ["1", "true"]

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# 💰 15,000 元台幣本金風控配置
TOTAL_EQUITY_TWD = 15000.0
MARGIN_PER_TRADE_USDT = float(os.getenv("MARGIN_USDT", "20.0"))  # 單筆 20 U
DEFAULT_LEVERAGE = int(os.getenv("LEVERAGE", "10"))              # 槓桿 10x
MAX_OPEN_POSITIONS = int(os.getenv("MAX_POSITIONS", "3"))         # 最多 3 筆
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL", "30"))

WATCHLIST = [
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
    "DOGE-USDT-SWAP",
    "ASTER-USDT-SWAP"
]

# ==========================================
# 📢 2. Telegram 訊息推播模組
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
        print(f"⚠️ TG 推播發送失敗 (非致命): {e}")

# ==========================================
# 🔐 3. OKX API 請求與簽名
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
    except Exception as e:
        return {"code": "-1", "msg": f"請求異常: {e}"}

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
    if not OKX_API_KEY:
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
                    avail_usdt = float(item.get("availBal", item.get("eq", "0.0")))
                    break
            return total_eq, avail_usdt
    except Exception as e:
        print(f"⚠️ 餘額查詢異常: {e}")
    return 0.0, 0.0

def get_open_positions() -> list:
    if not OKX_API_KEY:
        return []
    try:
        path = "/api/v5/account/positions?instType=SWAP"
        headers = get_okx_headers("GET", path)
        url = f"https://www.okx.com{path}"
        res = http_request(url, method="GET", headers=headers)
        if res.get("code") == "0" and res.get("data"):
            return [p for p in res["data"] if float(p.get("pos", "0")) != 0.0]
    except Exception as e:
        print(f"⚠️ 持倉查詢異常: {e}")
    return []

# ==========================================
# 💬 4. Telegram 雙向查詢監聽
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
        f"📈 當前持倉 ({len(positions)} 筆):\n"
    )
    if not positions:
        msg += "• 目前無持倉部位，正在監控中..."
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
        print("ℹ️ 未設定 TELEGRAM_BOT_TOKEN，略過 TG 監聽。")
        return
    last_update_id = 0
    print("💬 Telegram 雙向對話監聽器已啟動...")
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
                            send_telegram("🏓 <b>Pong!</b> 15000 台幣量化守護中！", target_chat_id=chat_id)
        except Exception as e:
            time.sleep(3)

# ==========================================
# 🛡️ 5. OKX 資金費率安全守護者 (防 ASTER 毒藥幣)
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

    # 極端費率熔斷
    if abs(funding_rate) >= 0.0005 or abs(hourly_rate) >= 0.0000625:
        return False, f"🚨 [極端費率熔斷] {inst_id} 單期 {funding_rate*100:.3f}% (每小時 {hourly_rate*100:.4f}%)，禁止進場！"

    # 做多高正費率攔截
    if is_long and hourly_rate > 0.00003:
        return False, f"🚨 [高資金費攔截] {inst_id} 當前正費率 {funding_rate*100:.3f}% (每 {interval_hours:.0f}h 扣一次)，拒絕開多！"

    # 做空深負費率攔截
    if not is_long and hourly_rate < -0.00003:
        return False, f"🚨 [深負資金費攔截] {inst_id} 當前深負費率 {funding_rate*100:.3f}% (每 {interval_hours:.0f}h 扣一次)，拒絕開空！"

    return True, "費率安全"

# ==========================================
# 📊 6. 合約規格與指標計算
# ==========================================
def get_instrument_info(inst_id: str) -> tuple[float, float]:
    url = f"https://www.okx.com/api/v5/public/instruments?instType=SWAP&instId={inst_id}"
    res = http_request(url)
    if res.get("code") == "0" and res.get("data"):
        item = res["data"][0]
        return float(item.get("ctVal", 1.0)), float(item.get("lotSz", 1.0))
    return 1.0, 1.0

def get_klines(inst_id: str, bar: str = "1H", limit: int = 60) -> list:
    url = f"https://www.okx.com/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
    res = http_request(url)
    if res.get("code") == "0" and res.get("data"):
        return res["data"][::-1]
    return []

def calculate_indicators(candles: list) -> dict:
    if len(candles) < 26:
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
    ma_slow = sum(closes[-25:]) / 25
    vol_ma20 = sum(vols[-21:-1]) / 20 if len(vols) >= 21 else 1.0
    vol_surge = vols[-1] / max(vol_ma20, 1e-6)

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
# 🎯 7. 下單委託 (合乎 15,000 元台幣規格)
# ==========================================
def place_order(inst_id: str, direction: str, price: float, atr: float):
    if not OKX_API_KEY or not OKX_API_SECRET:
        print("⚠️ 未檢測到 OKX API 金鑰，略過發單。")
        return

    side = "buy" if direction == "LONG" else "sell"
    ct_val, lot_sz = get_instrument_info(inst_id)

    contracts = (MARGIN_PER_TRADE_USDT * DEFAULT_LEVERAGE) / price / ct_val
    if lot_sz >= 1.0:
        sz_str = str(max(1, int(round(contracts))))
    else:
        decimals = max(0, -int(math.floor(math.log10(lot_sz))))
        sz_str = f"{max(lot_sz, round(contracts, decimals)):.{decimals}f}"

    # 嚴格止損：限額約 3.0 USDT (15% 保證金)
    sl_dist = min(price * 0.015, max(atr * 1.5, price * 0.012))
    sl_price = price - sl_dist if direction == "LONG" else price + sl_dist

    # 止盈：+40% ROI
    tp_dist = price * (0.40 / DEFAULT_LEVERAGE)
    tp_price = price + tp_dist if direction == "LONG" else price - tp_dist

    path = "/api/v5/trade/order"
    body_dict = {
        "instId": inst_id,
        "tdMode": "cross",
        "side": side,
        "posSide": "net",
        "ordType": "market",
        "sz": sz_str,
        "attachAlgoOrds": [
            {
                "attachAlgoClOrdId": f"sl_{int(time.time())}",
                "slTriggerPx": f"{sl_price:.4f}",
                "slOrdPx": "-1"
            },
            {
                "attachAlgoClOrdId": f"tp_{int(time.time())}",
                "tpTriggerPx": f"{tp_price:.4f}",
                "tpOrdPx": "-1"
            }
        ]
    }
    body_str = json.dumps(body_dict)
    headers = get_okx_headers("POST", path, body_str)
    url = f"https://www.okx.com{path}"

    mode_text = "【OKX 模擬盤】" if IS_SIMULATED else "【OKX 實盤】"
    print(f"🚀 {mode_text} 送出 OKX 開倉: {inst_id} {direction} {sz_str} 張 (本金: {MARGIN_PER_TRADE_USDT} U)...")
    res = http_request(url, method="POST", headers=headers, body=body_str)

    if res.get("code") == "0":
        tg_msg = (
            f"⚡ <b>{mode_text} 自動開倉成功！</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"標的幣種：<code>{inst_id}</code>\n"
            f"方向：<b>{'🟢 做多 LONG' if direction == 'LONG' else '🔴 做空 SHORT'}</b>\n"
            f"現價：<code>{price}</code> USDT\n"
            f"槓桿：<code>{DEFAULT_LEVERAGE}x</code> | 本金：<code>{MARGIN_PER_TRADE_USDT} USDT</code>\n"
            f"張數：<code>{sz_str} 張</code>\n"
            f"🎯 止盈價：<code>{tp_price:.4f}</code>\n"
            f"🛡️ 止損價：<code>{sl_price:.4f}</code> (限額 ~3.0 U)\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⏰ 時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        send_telegram(tg_msg)
    else:
        send_telegram(f"⚠️ <b>{mode_text} 下單異常！</b>\n標的：{inst_id}\n原因：{res.get('msg')}")

# ==========================================
# 🔄 8. 背景交易工作線程
# ==========================================
def trading_worker():
    """獨立背景線程，即使發生任何錯誤也不影響主線程長駐"""
    print("🤖 交易監控線程正式啟動...")
    while True:
        try:
            positions = get_open_positions()
            active_symbols = [p["instId"] for p in positions]

            if len(active_symbols) >= MAX_OPEN_POSITIONS:
                time.sleep(CHECK_INTERVAL_SEC)
                continue

            for inst_id in WATCHLIST:
                if inst_id in active_symbols:
                    continue

                candles = get_klines(inst_id, bar="1H", limit=60)
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
                if rsi <= 35.0 and vol_surge >= 1.5 and ma_fast >= ma_slow:
                    signal = "LONG"
                elif rsi >= 68.0 and vol_surge >= 1.5 and ma_fast <= ma_slow:
                    signal = "SHORT"

                if signal:
                    is_safe, reason = check_funding_rate_guard(inst_id, signal)
                    if not is_safe:
                        send_telegram(reason)
                        continue

                    place_order(inst_id, signal, price, atr)
                    time.sleep(2)

            time.sleep(CHECK_INTERVAL_SEC)

        except Exception as e:
            print(f"⚠️ 交易線程異常 (已自動攔截並恢復): {e}")
            traceback.print_exc()
            time.sleep(5)

# ==========================================
# 🔒 9. 主程序：死鎖保護心跳循環 (絕對永不退出)
# ==========================================
def main():
    print("=" * 60)
    print("🚀 [OKX Quant Bot] 正在初始化啟動...")
    print("=" * 60)

    # 1. 啟動 Telegram 監聽線程
    t_tg = threading.Thread(target=telegram_listener, daemon=True)
    t_tg.start()

    # 2. 啟動量化交易工作線程
    t_trade = threading.Thread(target=trading_worker, daemon=True)
    t_trade.start()

    # 3. 發送 Telegram 上線通知
    mode_desc = "官方模擬盤" if IS_SIMULATED else "真實資金盤"
    init_msg = (
        f"🤖 <b>【OKX 量化機器人・永不退出版】已上線！</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"模式：<code>{mode_desc}</code>\n"
        f"本金規格：<code>{TOTAL_EQUITY_TWD:,.0f} TWD (單筆 {MARGIN_PER_TRADE_USDT} U / {DEFAULT_LEVERAGE}x)</code>\n"
        f"持倉上限：<code>最多 {MAX_OPEN_POSITIONS} 筆</code>\n"
        f"🛡️ 資金費率防護：<b>ASTER 等吸血幣即時攔截</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👉 在群組隨時發送 <code>帳戶</code> 或 <code>/status</code> 即可查帳！"
    )
    send_telegram(init_msg)

    # 4. 🔒 主線程死鎖心跳保護：只要 Python 不被外力強制殺死，它將永久鎖定在此循環！
    loop_count = 0
    while True:
        loop_count += 1
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # 每 30 秒印出一次心跳日誌，向 Railway 證明程序 100% 活躍
        if loop_count % 3 == 0:
            print(f"[{now_str}] 💓 機器人健康運作中 (Loop: {loop_count}) | 守護在線...")
        time.sleep(10)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ 嚴重主程序崩潰: {e}")
        traceback.print_exc()
        # 即使崩潰也休眠 60 秒印出日誌，防止無聲退出
        time.sleep(60)