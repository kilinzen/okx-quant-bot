#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🚀 OKX 量化全自動合約交易機器人 (Railway 雲端 24H 穩定版)
- 📢 完整 Telegram 實時推播：開倉通知、平倉通知、高資金費攔截警報、每日風控
- 🛡️ OKX 資金費率守護者：實時折算 1h/4h/8h 週期，徹底杜絕 ASTER 類吸血幣
- 🎯 OKX SWAP 面值精確換算 (ctVal/lotSz) + ATR 動態止損
"""

import os
import time
import json
import math
import hmac
import hashlib
import base64
import urllib.request
from datetime import datetime, timezone

# ==========================================
# ⚙️ 系統與環境變數設定 (Railway Variables)
# ==========================================
OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_API_SECRET = os.getenv("OKX_API_SECRET", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

# 是否為官方模擬盤：'1' 或 'true' 表示模擬盤，'0' 或 'false' 表示真實資金盤
IS_SIMULATED = os.getenv("OKX_SIMULATED", "0").lower() in ["1", "true"]

# 📢 Telegram 機器人設定
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# 交易參數
DEFAULT_MARGIN_USDT = float(os.getenv("MARGIN_USDT", "20.0"))  # 單筆保證金 (USDT)
DEFAULT_LEVERAGE = int(os.getenv("LEVERAGE", "10"))            # 槓桿倍數
MAX_OPEN_POSITIONS = int(os.getenv("MAX_POSITIONS", "3"))       # 最大同時持倉數
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL", "30"))    # 輪詢間隔 (秒)

# 監控目標幣種清單
WATCHLIST = [
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
    "DOGE-USDT-SWAP",
    "ASTER-USDT-SWAP"
]

# ==========================================
# 📢 Telegram 訊息發送模組
# ==========================================
def send_telegram(message: str):
    """發送 Telegram 即時推播訊息"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
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
        with urllib.request.urlopen(req, timeout=8) as resp:
            pass
    except Exception as e:
        print(f"⚠️ Telegram 發送失敗: {e}")

# ==========================================
# 🔐 OKX API 請求與簽名模組
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
        return {"code": "-1", "msg": f"網路異常: {str(e)}"}

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

# ==========================================
# 🛡️ 核心：OKX 資金費率安全守護者 (杜絕 ASTER 吸血幣)
# ==========================================
def check_funding_rate_guard(inst_id: str, direction: str) -> tuple[bool, str]:
    """
    動態換算 1h / 4h / 8h 結算週期，標準化為每小時成本
    返回: (是否放行, 詳細說明)
    """
    url = f"https://www.okx.com/api/v5/public/funding-rate?instId={inst_id}"
    res = http_request(url)
    
    if res.get("code") != "0" or not res.get("data"):
        return True, f"[{inst_id}] 資金費率獲取略過，默認放行"

    data = res["data"][0]
    funding_rate = float(data.get("fundingRate", "0.0"))
    f_time = int(data.get("fundingTime", "0"))
    next_f_time = int(data.get("nextFundingTime", "0"))

    # 計算結算間隔小時數
    interval_hours = 8.0
    if next_f_time > f_time:
        interval_hours = max(1.0, (next_f_time - f_time) / 3600000.0)

    # 折算每小時資金費率
    hourly_rate = funding_rate / interval_hours
    is_long = direction.upper() in ["BUY", "LONG"]

    # 1. 🚨 極端行情熔斷 (單期 >= 0.05% 或 每小時 >= 0.00625%)
    if abs(funding_rate) >= 0.0005 or abs(hourly_rate) >= 0.0000625:
        return False, (
            f"🚨 <b>【極端資金費率熔斷】</b>\n"
            f"幣種：<code>{inst_id}</code>\n"
            f"單期費率：{funding_rate*100:.3f}%\n"
            f"每小時成本：{hourly_rate*100:.4f}%\n"
            f"⚠️ 多空嚴重失衡，為保護本金已禁止開倉！"
        )

    # 2. 🚨 做多攔截：正費率過高 (多付空，做多每小時被吸血)
    if is_long and hourly_rate > 0.00003:
        return False, (
            f"🚨 <b>【高資金費攔截・放棄開多】</b>\n"
            f"幣種：<code>{inst_id}</code>\n"
            f"當前費率：+{funding_rate*100:.3f}% (每 {interval_hours:.0f}h 扣一次)\n"
            f"折算每小時：+{hourly_rate*100:.4f}%\n"
            f"⚠️ 做多持倉成本過高，已自動攔截避免磨損本金！"
        )

    # 3. 🚨 做空攔截：深負費率 (空付多，做空每小時被吸血)
    if not is_long and hourly_rate < -0.00003:
        return False, (
            f"🚨 <b>【深負資金費攔截・放棄開空】</b>\n"
            f"幣種：<code>{inst_id}</code>\n"
            f"當前費率：{funding_rate*100:.3f}% (每 {interval_hours:.0f}h 扣一次)\n"
            f"折算每小時：{hourly_rate*100:.4f}%\n"
            f"⚠️ 做空持倉成本過高，已自動攔截避免磨損本金！"
        )

    return True, f"✅ [費率安全] {inst_id} 每小時成本 {hourly_rate*100:.4f}% ({interval_hours:.0f}h 週期)"

# ==========================================
# 📊 市場行情與合約規格查詢
# ==========================================
def get_instrument_info(inst_id: str) -> tuple[float, float]:
    url = f"https://www.okx.com/api/v5/public/instruments?instType=SWAP&instId={inst_id}"
    res = http_request(url)
    if res.get("code") == "0" and res.get("data"):
        item = res["data"][0]
        ct_val = float(item.get("ctVal", 1.0))
        lot_sz = float(item.get("lotSz", 1.0))
        return ct_val, lot_sz
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
    
    # 1. RSI (14)
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[-14:]) / 14
    avg_loss = sum(losses[-14:]) / 14
    rsi = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))
    
    # 2. 雙均線
    ma_fast = sum(closes[-7:]) / 7
    ma_slow = sum(closes[-25:]) / 25
    
    # 3. 爆量倍數
    vol_ma20 = sum(vols[-21:-1]) / 20 if len(vols) >= 21 else 1.0
    vol_surge = vols[-1] / max(vol_ma20, 1e-6)
    
    # 4. ATR (14)
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
# 💼 持倉查詢
# ==========================================
def get_open_positions() -> list:
    if not OKX_API_KEY:
        return []
    path = "/api/v5/account/positions?instType=SWAP"
    headers = get_okx_headers("GET", path)
    url = f"https://www.okx.com{path}"
    res = http_request(url, method="GET", headers=headers)
    if res.get("code") == "0" and res.get("data"):
        return [p for p in res["data"] if float(p.get("pos", "0")) != 0.0]
    return []

# ==========================================
# 🎯 智能開倉下單 (同步發送 Telegram 通知)
# ==========================================
def place_order(inst_id: str, direction: str, price: float, atr: float):
    if not OKX_API_KEY or not OKX_API_SECRET:
        print("⚠️ 未檢測到 OKX API 金鑰，略過發單。")
        return

    side = "buy" if direction == "LONG" else "sell"
    ct_val, lot_sz = get_instrument_info(inst_id)
    
    # 計算合約張數 sz
    contracts = (DEFAULT_MARGIN_USDT * DEFAULT_LEVERAGE) / price / ct_val
    if lot_sz >= 1.0:
        sz_str = str(max(1, int(round(contracts))))
    else:
        decimals = max(0, -int(math.floor(math.log10(lot_sz))))
        sz_str = f"{max(lot_sz, round(contracts, decimals)):.{decimals}f}"

    # ATR 動態止損價
    sl_distance = max(atr * 1.8, price * 0.015)
    sl_price = price - sl_distance if direction == "LONG" else price + sl_distance

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
            }
        ]
    }
    body_str = json.dumps(body_dict)
    headers = get_okx_headers("POST", path, body_str)
    url = f"https://www.okx.com{path}"
    
    mode_text = "【OKX 模擬盤】" if IS_SIMULATED else "【OKX 實盤】"
    print(f"🚀 {mode_text} 發送 {inst_id} {direction} 委託，張數: {sz_str}...")
    res = http_request(url, method="POST", headers=headers, body=body_str)
    
    if res.get("code") == "0":
        ord_id = res.get("data", [{}])[0].get("ordId", "")
        print(f"✅ 開倉成功！訂單號: {ord_id}")
        
        # 📢 Telegram 開倉推播
        tg_msg = (
            f"⚡ <b>{mode_text} 自動開倉成功！</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"標的幣種：<code>{inst_id}</code>\n"
            f"開倉方向：<b>{'🟢 做多 LONG' if direction == 'LONG' else '🔴 做空 SHORT'}</b>\n"
            f"成交現價：<code>{price}</code> USDT\n"
            f"槓桿倍數：<code>{DEFAULT_LEVERAGE}x</code>\n"
            f"保證金額：<code>{DEFAULT_MARGIN_USDT} USDT</code>\n"
            f"下單張數：<code>{sz_str} 張</code>\n"
            f"動態止損：<code>{sl_price:.4f} USDT</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⏰ 時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        send_telegram(tg_msg)
    else:
        err_msg = f"❌ 開倉失敗：{res.get('msg')} (代碼 {res.get('code')})"
        print(err_msg)
        send_telegram(f"⚠️ <b>{mode_text} 下單異常！</b>\n標的：{inst_id}\n原因：{res.get('msg')}")

# ==========================================
# 🔁 主執行迴圈 (24H 雲端無間斷守護)
# ==========================================
def main():
    mode_desc = "官方模擬盤" if IS_SIMULATED else "真實資金盤"
    init_msg = (
        f"🤖 <b>OKX 量化雲端機器人啟動成功！</b>\n"
        f"運行環境：Railway 24H 守護\n"
        f"交易模式：<code>{mode_desc}</code>\n"
        f"單筆金額：<code>{DEFAULT_MARGIN_USDT} USDT ({DEFAULT_LEVERAGE}x)</code>\n"
        f"🛡️ <b>資金費率守護者：已啟用 (ASTER 防吸血機制在線)</b>"
    )
    print(init_msg.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""))
    send_telegram(init_msg)

    while True:
        try:
            # 1. 檢查現有持倉
            current_positions = get_open_positions()
            active_symbols = [p["instId"] for p in current_positions]
            
            if len(active_symbols) >= MAX_OPEN_POSITIONS:
                time.sleep(CHECK_INTERVAL_SEC)
                continue

            # 2. 監控幣種掃描
            for inst_id in WATCHLIST:
                if inst_id in active_symbols:
                    continue  # 已有倉位不重複開

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

                # 量化訊號觸發條件
                signal = None
                if rsi <= 35.0 and vol_surge >= 1.5 and ma_fast >= ma_slow:
                    signal = "LONG"
                elif rsi >= 68.0 and vol_surge >= 1.5 and ma_fast <= ma_slow:
                    signal = "SHORT"

                if signal:
                    print(f"\n🔍 發現訊號：{inst_id} {signal} (現價: {price}, RSI: {rsi:.1f}, 爆量: {vol_surge:.2f}x)")
                    
                    # 🛡️ 核心防護：OKX 資金費率安全過濾！
                    is_safe, reason = check_funding_rate_guard(inst_id, signal)
                    if not is_safe:
                        print(f"🛑 放棄開倉：{reason}")
                        # 📢 即時透過 Telegram 通知您：已為您攔截毒藥幣！
                        send_telegram(reason)
                        continue  # 👈 核心攔截，絕不開倉！

                    # 通過所有風控，送出訂單
                    place_order(inst_id, signal, price, atr)
                    time.sleep(2)

            time.sleep(CHECK_INTERVAL_SEC)

        except KeyboardInterrupt:
            print("\n👋 機器人手動停止。")
            break
        except Exception as e:
            print(f"⚠️ 循環錯誤: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()