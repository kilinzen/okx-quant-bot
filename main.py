#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🚀 OKX 量化全自動合約交易機器人 (Railway 24H 專屬守護版)
- 💰 本金規格：15,000 TWD (約 465 USDT)
- 📊 倉位分配：單筆保證金 20 USDT (約 4.3%)，槓桿 10x，最大同時持有 3 筆 (動用率 < 15%)
- 🛡️ 風控配置：ATR 動態止損 (最大容忍 -15% 保證金) + 階梯式自動止盈
- 🚨 資金費率守護者：實時折算 1h/4h 扣費週期，杜絕 ASTER 類吸血幣
- 🌐 Railway Keep-Alive：內建 HTTP 探針回應，徹底杜絕 15 秒 Stopping Container
- 💬 Telegram 雙向控制與成交即時推播
"""

import os
import time
import json
import math
import hmac
import hashlib
import base64
import threading
import urllib.request
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

# ==========================================
# 🌐 1. Railway Keep-Alive 守護伺服器 (解決 Stopping Container)
# ==========================================
class RailwayHealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK - 15000 TWD OKX Quant Bot Running 24/7")

    def log_message(self, format, *args):
        pass  # 靜音健康檢查日誌

def start_keep_alive_server():
    port = int(os.getenv("PORT", "8080"))
    try:
        server = HTTPServer(("0.0.0.0", port), RailwayHealthHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        print(f"🌐 [Railway 守護] Keep-Alive 伺服器已在 Port {port} 啟動，防止容器 15 秒被殺！")
    except Exception as e:
        print(f"⚠️ Keep-Alive 啟動跳過: {e}")

# ==========================================
# ⚙️ 2. 帳戶與本金規格 (嚴格對應 15,000 元台幣)
# ==========================================
OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_API_SECRET = os.getenv("OKX_API_SECRET", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")
IS_SIMULATED = os.getenv("OKX_SIMULATED", "0").lower() in ["1", "true"]

# 📢 Telegram 推播設定
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# 💰 15,000 TWD 本金精密配置
TOTAL_EQUITY_TWD = 15000.0
TWD_USDT_RATE = 32.2
ESTIMATED_BALANCE_USDT = TOTAL_EQUITY_TWD / TWD_USDT_RATE  # 約 465 USDT

# 單筆下單保證金：20 USDT (約佔本金 4.3%，極度穩健抗震)
MARGIN_PER_TRADE_USDT = float(os.getenv("MARGIN_USDT", "20.0"))
DEFAULT_LEVERAGE = int(os.getenv("LEVERAGE", "10"))
MAX_OPEN_POSITIONS = int(os.getenv("MAX_POSITIONS", "3"))  # 最多同時 3 筆 (佔總本金 < 15%)
CHECK_INTERVAL_SEC = int(os.getenv("CHECK_INTERVAL", "30"))

# 止損與止盈參數
MAX_SL_PERCENT = 0.15   # 單筆最大虧損限制 15% 保證金 (約 -3.0 USDT / 96 元台幣)
TP_STEP_1_ROI = 0.20    # 收益達 20% 啟動保本
TP_STEP_2_ROI = 0.45    # 收益達 45% (利潤 9 USDT) 移動止盈
TP_STEP_3_ROI = 0.80    # 收益達 80% (利潤 16 USDT) 強制止盈

# 監控目標幣種
WATCHLIST = [
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
    "DOGE-USDT-SWAP",
    "ASTER-USDT-SWAP"
]

# ==========================================
# 📢 3. Telegram 訊息推播模組
# ==========================================
def send_telegram(message: str):
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
        with urllib.request.urlopen(req, timeout=8):
            pass
    except Exception as e:
        print(f"⚠️ Telegram 發送失敗: {e}")

# ==========================================
# 💬 4. Telegram 雙向指令監聽器
# ==========================================
def telegram_listener():
    if not TELEGRAM_BOT_TOKEN:
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
                        msg = item.get("message", {})
                        text = msg.get("text", "").strip()
                        chat_id = str(msg.get("chat", {}).get("id", ""))
                        
                        if chat_id == str(TELEGRAM_CHAT_ID):
                            if text in ["/status", "狀態", "查帳"]:
                                positions = get_open_positions()
                                pos_info = "\n".join([
                                    f"• <code>{p['instId']}</code> ({p['posSide']}) 張數: {p['pos']} | 未結盈虧: {p.get('upl', '0')} U"
                                    for p in positions
                                ]) if positions else "目前無在倉單"
                                
                                send_telegram(
                                    f"📊 <b>【15,000 台幣量化盤 運行狀態】</b>\n"
                                    f"━━━━━━━━━━━━━━━━━━\n"
                                    f"基準總本金：約 {ESTIMATED_BALANCE_USDT:.1f} USDT (15,000 TWD)\n"
                                    f"單筆下單：{MARGIN_PER_TRADE_USDT} USDT ({DEFAULT_LEVERAGE}x)\n"
                                    f"在倉部位數：{len(positions)} / {MAX_OPEN_POSITIONS}\n"
                                    f"━━━━━━━━━━━━━━━━━━\n{pos_info}"
                                )
                            elif text in ["/ping", "ping"]:
                                send_telegram("🏓 <b>Pong!</b> 15000 台幣守護機器人運行中！")
        except Exception:
            time.sleep(5)

# ==========================================
# 🔐 5. OKX 簽名與底層請求
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
# 🛡️ 6. 核心：OKX 資金費率安全守護者 (杜絕 ASTER 吸血幣)
# ==========================================
def check_funding_rate_guard(inst_id: str, direction: str) -> tuple[bool, str]:
    url = f"https://www.okx.com/api/v5/public/funding-rate?instId={inst_id}"
    res = http_request(url)
    if res.get("code") != "0" or not res.get("data"):
        return True, f"[{inst_id}] 資金費率獲取略過，默認放行"

    data = res["data"][0]
    funding_rate = float(data.get("fundingRate", "0.0"))
    f_time = int(data.get("fundingTime", "0"))
    next_f_time = int(data.get("nextFundingTime", "0"))

    interval_hours = 8.0
    if next_f_time > f_time:
        interval_hours = max(1.0, (next_f_time - f_time) / 3600000.0)

    hourly_rate = funding_rate / interval_hours
    is_long = direction.upper() in ["BUY", "LONG"]

    # 1. 極端行情雙向熔斷
    if abs(funding_rate) >= 0.0005 or abs(hourly_rate) >= 0.0000625:
        return False, (
            f"🚨 <b>【極端資金費率熔斷・拒絕進場】</b>\n"
            f"幣種：<code>{inst_id}</code>\n"
            f"單期費率：{funding_rate*100:.3f}%\n"
            f"每小時成本：{hourly_rate*100:.4f}%\n"
            f"⚠️ 市場多空擠壓嚴重，為保護 15,000 元本金已拒絕開倉！"
        )

    # 2. 做多高正費率攔截 (每小時 > 0.003%)
    if is_long and hourly_rate > 0.00003:
        return False, (
            f"🚨 <b>【高資金費攔截・放棄開多】</b>\n"
            f"幣種：<code>{inst_id}</code>\n"
            f"當前費率：+{funding_rate*100:.3f}% (每 {interval_hours:.0f}h 扣一次)\n"
            f"折算每小時：+{hourly_rate*100:.4f}%\n"
            f"⚠️ 類似 ASTER 類頻繁扣費，已自動攔截避免本金遭蠶食！"
        )

    # 3. 做空深負費率攔截 (每小時 < -0.003%)
    if not is_long and hourly_rate < -0.00003:
        return False, (
            f"🚨 <b>【深負資金費攔截・放棄開空】</b>\n"
            f"幣種：<code>{inst_id}</code>\n"
            f"當前費率：{funding_rate*100:.3f}% (每 {interval_hours:.0f}h 扣一次)\n"
            f"折算每小時：{hourly_rate*100:.4f}%\n"
            f"⚠️ 做空持倉成本極高，已自動攔截保護本金！"
        )

    return True, f"✅ [費率安全] {inst_id} 每小時成本 {hourly_rate*100:.4f}% ({interval_hours:.0f}h 週期)"

# ==========================================
# 📊 7. 合約規格與指標計算
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
    
    # RSI (14)
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[-14:]) / 14
    avg_loss = sum(losses[-14:]) / 14
    rsi = 100.0 if avg_loss == 0 else 100.0 - (100.0 / (1.0 + (avg_gain / avg_loss)))
    
    # 雙均線 (快7, 慢25)
    ma_fast = sum(closes[-7:]) / 7
    ma_slow = sum(closes[-25:]) / 25
    
    # 爆量倍數
    vol_ma20 = sum(vols[-21:-1]) / 20 if len(vols) >= 21 else 1.0
    vol_surge = vols[-1] / max(vol_ma20, 1e-6)
    
    # ATR (14)
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
# 💼 8. 持倉管理與精準下單 (附帶止損止盈)
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

def place_order(inst_id: str, direction: str, price: float, atr: float):
    if not OKX_API_KEY or not OKX_API_SECRET:
        print("⚠️ 未檢測到 OKX API 金鑰，略過發單。")
        return

    side = "buy" if direction == "LONG" else "sell"
    ct_val, lot_sz = get_instrument_info(inst_id)
    
    # 依 20 USDT 保證金計算合約張數
    contracts = (MARGIN_PER_TRADE_USDT * DEFAULT_LEVERAGE) / price / ct_val
    if lot_sz >= 1.0:
        sz_str = str(max(1, int(round(contracts))))
    else:
        decimals = max(0, -int(math.floor(math.log10(lot_sz))))
        sz_str = f"{max(lot_sz, round(contracts, decimals)):.{decimals}f}"

    # 嚴格動態止損：最大虧損不超過保證金的 15% (即 20 * 0.15 = 3 USDT)
    max_sl_distance = (price * (MAX_SL_PERCENT / DEFAULT_LEVERAGE))
    atr_sl_distance = max(atr * 1.5, price * 0.012)
    final_sl_distance = min(atr_sl_distance, max_sl_distance)
    
    sl_price = price - final_sl_distance if direction == "LONG" else price + final_sl_distance

    # 階梯止盈目標價 (+45% ROI 目標)
    tp_distance = price * (TP_STEP_2_ROI / DEFAULT_LEVERAGE)
    tp_price = price + tp_distance if direction == "LONG" else price - tp_distance

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
                # 自動止損單
                "attachAlgoClOrdId": f"sl_{int(time.time())}",
                "slTriggerPx": f"{sl_price:.4f}",
                "slOrdPx": "-1"
            },
            {
                # 自動止盈單
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
    print(f"🚀 {mode_text} 發送 {inst_id} {direction} 委託，張數: {sz_str}...")
    res = http_request(url, method="POST", headers=headers, body=body_str)
    
    if res.get("code") == "0":
        ord_id = res.get("data", [{}])[0].get("ordId", "")
        print(f"✅ 開倉成功！訂單號: {ord_id}")
        
        tg_msg = (
            f"⚡ <b>{mode_text} 自動開倉成功！</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"標的幣種：<code>{inst_id}</code>\n"
            f"開倉方向：<b>{'🟢 做多 LONG' if direction == 'LONG' else '🔴 做空 SHORT'}</b>\n"
            f"成交現價：<code>{price}</code> USDT\n"
            f"槓桿倍數：<code>{DEFAULT_LEVERAGE}x</code>\n"
            f"保證金額：<code>{MARGIN_PER_TRADE_USDT} USDT</code> (約 644 TWD)\n"
            f"下單張數：<code>{sz_str} 張</code>\n"
            f"🎯 預設止盈：<code>{tp_price:.4f}</code> (+45% ROI)\n"
            f"🛡️ 嚴格止損：<code>{sl_price:.4f}</code> (限額 -15% / ~3.0 U)\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⏰ 時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        send_telegram(tg_msg)
    else:
        print(f"❌ 開倉失敗：{res.get('msg')}")
        send_telegram(f"⚠️ <b>{mode_text} 下單失敗！</b>\n幣種：{inst_id}\n原因：{res.get('msg')}")

# ==========================================
# 🔁 9. 主執行迴圈 (24H 永續守護)
# ==========================================
def main():
    # 1. 啟動 Railway Keep-Alive 探針
    start_keep_alive_server()

    # 2. 啟動 Telegram 雙向監聽
    t_tg = threading.Thread(target=telegram_listener, daemon=True)
    t_tg.start()

    mode_desc = "官方模擬盤" if IS_SIMULATED else "真實資金盤"
    init_msg = (
        f"🤖 <b>【OKX 量化機器人・15000 台幣守護版】已啟動！</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"運行環境：Railway 24H 雲端守護 (Keep-Alive 在線)\n"
        f"交易模式：<code>{mode_desc}</code>\n"
        f"總本金額：<code>{TOTAL_EQUITY_TWD:,.0f} TWD (約 {ESTIMATED_BALANCE_USDT:.1f} USDT)</code>\n"
        f"單筆下單：<code>{MARGIN_PER_TRADE_USDT} USDT (4.3% 本金, {DEFAULT_LEVERAGE}x 槓桿)</code>\n"
        f"持倉上限：<code>最多 {MAX_OPEN_POSITIONS} 筆 (總動用率 &lt; 15%)</code>\n"
        f"🛡️ 資金費率防護：<b>ASTER 等高頻吸血幣全自動攔截</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"隨時在 TG 輸入 <code>/status</code> 即可查帳！"
    )
    print(init_msg.replace("<b>", "").replace("</b>", "").replace("<code>", "").replace("</code>", ""))
    send_telegram(init_msg)

    while True:
        try:
            current_positions = get_open_positions()
            active_symbols = [p["instId"] for p in current_positions]
            
            # 若已達持倉上限，等待平倉，絕不盲目加倉
            if len(active_symbols) >= MAX_OPEN_POSITIONS:
                time.sleep(CHECK_INTERVAL_SEC)
                continue

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

                # 量化訊號 (突破共振)
                signal = None
                if rsi <= 35.0 and vol_surge >= 1.5 and ma_fast >= ma_slow:
                    signal = "LONG"
                elif rsi >= 68.0 and vol_surge >= 1.5 and ma_fast <= ma_slow:
                    signal = "SHORT"

                if signal:
                    print(f"\n🔍 捕捉訊號：{inst_id} {signal} (現價: {price}, RSI: {rsi:.1f}, 爆量: {vol_surge:.2f}x)")
                    
                    # 🛡️ 資金費率安全過濾
                    is_safe, reason = check_funding_rate_guard(inst_id, signal)
                    if not is_safe:
                        print(f"🛑 放棄開倉：{reason}")
                        send_telegram(reason)
                        continue

                    # 風控通過，執行精準下單
                    place_order(inst_id, signal, price, atr)
                    time.sleep(2)

            time.sleep(CHECK_INTERVAL_SEC)

        except KeyboardInterrupt:
            print("\n👋 機器人正常停止。")
            break
        except Exception as e:
            print(f"⚠️ 循環保護重試: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()