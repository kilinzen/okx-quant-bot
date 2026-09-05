import os
import time
import json
import hmac
import base64
import hashlib
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests

# ==============================================================================
# 🔑 環境變數 (直接沿用 Railway 設定，完全不用重設)
# ==============================================================================
OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
IS_SIMULATED = os.getenv("IS_SIMULATED", "true").lower() == "true"
PORT = int(os.getenv("PORT", 8080))

OKX_API_URL = "https://www.okx.com"

# ==============================================================================
# ⚙️ 核心交易參數 (嚴格對齊手機版)
# ==============================================================================
LEVERAGE = 10
MIN_CONFIDENCE_SCORE = 84
MIN_AMPLITUDE = 0.025
BTC_MAX_VOLATILITY = 0.045

BE_TRIGGER_ROI = 0.20        # +20% 自動推保本
TRAILING_TRIGGER_ROI = 0.45  # +45% 波段移動鎖利
TRAILING_PROFIT_FLOOR = 0.70 # 鎖定 70% 利潤
MOMENTUM_TIMEOUT_SEC = 90 * 60

MONITOR_SYMBOLS = [
    "DOGE-USDT-SWAP", "SOL-USDT-SWAP", "PEPE-USDT-SWAP", 
    "SUI-USDT-SWAP", "NEAR-USDT-SWAP", "OP-USDT-SWAP", 
    "APT-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP"
]

# ==============================================================================
# 📡 Telegram 穩定推播 (純文字模式，絕不因符號報錯)
# ==============================================================================
def send_telegram(msg: str):
    print(f"[TG LOG] {msg}")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    try:
        r = requests.post(url, json=payload, timeout=8)
        if r.status_code != 200:
            print(f"TG 發送失敗 HTTP {r.status_code}: {r.text}")
    except Exception as e:
        print(f"TG 例外: {e}")

# ==============================================================================
# 🔐 OKX API 簽名
# ==============================================================================
def okx_request(method: str, path: str, body: dict = None):
    now = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace("+00:00", "Z")
    body_str = json.dumps(body) if body else ""
    msg = f"{now}{method}{path}{body_str}"
    h = hmac.new(OKX_SECRET_KEY.encode('utf-8'), msg.encode('utf-8'), hashlib.sha256)
    sign = base64.b64encode(h.digest()).decode('utf-8')
    
    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": now,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json",
        "x-simulated-trading": "1" if IS_SIMULATED else "0"
    }
    url = f"{OKX_API_URL}{path}"
    try:
        if method == "GET":
            resp = requests.get(url, headers=headers, timeout=10)
        else:
            resp = requests.post(url, headers=headers, data=body_str, timeout=10)
        return resp.json()
    except Exception as e:
        print(f"OKX 連線異常 ({path}): {e}")
        return None

def fetch_candles(inst_id: str, bar="1H", limit=100):
    url = f"{OKX_API_URL}/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
    try:
        resp = requests.get(url, timeout=8).json()
        if resp.get("code") == "0":
            data = resp.get("data", [])
            data.reverse()
            return data
    except Exception:
        pass
    return []

# ==============================================================================
# 💬 TG 指令互動功能 (1:1 還原「帳戶」、「說明」查詢)
# ==============================================================================
def get_account_summary():
    try:
        bal_res = okx_request("GET", "/api/v5/account/balance?ccy=USDT")
        total_eq = "0.00"
        avail_bal = "0.00"
        if bal_res and bal_res.get("code") == "0":
            data = bal_res.get("data", [{}])[0]
            total_eq = data.get("totalEq", "0.00")
            for detail in data.get("details", []):
                if detail.get("ccy") == "USDT":
                    avail_bal = detail.get("availBal", "0.00")
                    break

        pos_res = okx_request("GET", "/api/v5/account/positions")
        pos_list = []
        if pos_res and pos_res.get("code") == "0":
            for p in pos_res.get("data", []):
                sz = float(p.get("pos", 0))
                if sz != 0:
                    sym = p.get("instId", "").replace("-USDT-SWAP", "")
                    side = "做多 🟢" if (p.get("posSide") == "long" or sz > 0) else "做空 🔴"
                    upl = float(p.get("upl", 0))
                    upl_ratio = float(p.get("uplRatio", 0)) * 100
                    pos_list.append(f"• {sym} ({side})\n  未實現損益: {upl:+.2f} U ({upl_ratio:+.2f}%)")

        pos_text = "\n".join(pos_list) if pos_list else "無持倉"
        mode_text = "模擬盤 (Demo)" if IS_SIMULATED else "實盤 (Live)"
        
        reply = (
            "【OKX 永續合約資產概況】\n"
            f"💰 帳戶總淨值: {float(total_eq):,.2f} USD\n"
            f"💵 可用保證金: {float(avail_bal):,.2f} USDT\n"
            f"🎯 模式: {mode_text}\n"
            "-------------------------\n"
            f"📈 當前持倉 ({len(pos_list)} 筆):\n"
            f"{pos_text}"
        )
        return reply
    except Exception as e:
        return f"查詢失敗: {e}"

def tg_polling_loop():
    last_update_id = 0
    while True:
        try:
            if not TELEGRAM_BOT_TOKEN:
                time.sleep(10)
                continue
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=15"
            r = requests.get(url, timeout=20).json()
            if r.get("ok"):
                for item in r.get("result", []):
                    last_update_id = item["update_id"]
                    msg = item.get("message", {})
                    text = msg.get("text", "").strip()
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    
                    if text in ["帳戶", "账户", "/account", "查詢", "查询"]:
                        summary = get_account_summary()
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": summary})
                    elif text in ["說明", "说明", "/help"]:
                        help_text = (
                            "🤖 冰火菠蘿量化機器人指令：\n\n"
                            "1. 輸入「帳戶」：查詢即時資產餘額與各幣種損益\n"
                            "2. 輸入「說明」：查看使用指令\n"
                            "3. 核心自動每 30 秒掃描行情並風控"
                        )
                        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": help_text})
        except Exception:
            pass
        time.sleep(2)

# ==============================================================================
# 🧠 量化決策核心
# ==============================================================================
def calc_ema(values, span):
    alpha = 2.0 / (span + 1.0)
    ema = values[0]
    for v in values[1:]:
        ema = alpha * v + (1.0 - alpha) * ema
    return ema

def check_btc_macro():
    candles = fetch_candles("BTC-USDT-SWAP", "1H", 60)
    if len(candles) < 50:
        return True, "UNKNOWN"
    closes = [float(c[4]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    volatility = (max(highs[-24:]) - min(lows[-24:])) / closes[-1]
    if volatility > BTC_MAX_VOLATILITY:
        return False, "HIGH_VOLATILITY"
    ema50 = calc_ema(closes, 50)
    trend = "UP" if closes[-1] > ema50 else "DOWN"
    return True, trend

def analyze_symbol(inst_id: str, btc_trend: str):
    candles = fetch_candles(inst_id, "1H", 80)
    if len(candles) < 50:
        return None
    closes = [float(c[4]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    vols = [float(c[5]) for c in candles]
    curr_price = closes[-1]
    
    if (max(highs[-24:]) - min(lows[-24:])) / curr_price < MIN_AMPLITUDE:
        return None
    
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, 50)
    
    tr_list = []
    for j in range(1, len(candles)):
        tr = max(highs[j] - lows[j], abs(highs[j] - closes[j-1]), abs(lows[j] - closes[j-1]))
        tr_list.append(tr)
    recent_tr = tr_list[-14:]
    atr = sum(recent_tr) / len(recent_tr) if recent_tr else (curr_price * 0.02)
    
    prev_vols = vols[-20:-1]
    vol_avg = sum(prev_vols) / len(prev_vols) if prev_vols else 1.0
    vol_surge = vols[-1] / max(vol_avg, 1e-6)
    
    confidence = 70
    if vol_surge >= 1.8: confidence += 10
    if vol_surge >= 2.5: confidence += 5
    if (max(highs[-24:]) - min(lows[-24:])) / curr_price >= 0.04: confidence += 5
    
    signal = None
    if btc_trend == "UP" and ema20 > ema50 and vol_surge >= 1.8 and curr_price > closes[-2]:
        confidence += 5
        signal = "LONG"
    elif btc_trend == "DOWN" and ema20 < ema50 and vol_surge >= 1.8 and curr_price < closes[-2]:
        confidence += 5
        signal = "SHORT"
        
    if signal and confidence >= MIN_CONFIDENCE_SCORE:
        sl_dist = atr * 1.5
        tp_dist = atr * 3.2
        return {
            "symbol": inst_id,
            "signal": signal,
            "price": curr_price,
            "sl": curr_price - sl_dist if signal == "LONG" else curr_price + sl_dist,
            "tp": curr_price + tp_dist if signal == "LONG" else curr_price - tp_dist,
            "confidence": confidence
        }
    return None

# ==============================================================================
# 🎯 實際下單與持倉風控
# ==============================================================================
active_positions_tracker = {}

def open_position(sig: dict):
    inst_id = sig["symbol"]
    side = "buy" if sig["signal"] == "LONG" else "sell"
    
    okx_request("POST", "/api/v5/account/set-leverage", {
        "instId": inst_id, "lever": str(LEVERAGE), "mgnMode": "cross"
    })
    
    order_body = {
        "instId": inst_id, "tdMode": "cross", "side": side, "ordType": "market", "sz": "1"
    }
    res = okx_request("POST", "/api/v5/trade/order", order_body)
    if res and res.get("code") == "0":
        send_telegram(
            f"🚀【實際開倉成功】{inst_id}\n"
            f"方向: {sig['signal']} | 置信度: {sig['confidence']}\n"
            f"進場價: {sig['price']}\n"
            f"止損價: {sig['sl']:.4f} | 止盈價: {sig['tp']:.4f}"
        )
    else:
        err = res.get('msg', '未知錯誤') if res else '連線失敗'
        send_telegram(f"⚠️ {inst_id} 下單失敗: {err}")

def manage_open_positions():
    try:
        pos_res = okx_request("GET", "/api/v5/account/positions")
        if not pos_res or pos_res.get("code") != "0":
            return
        
        positions = pos_res.get("data", [])
        now_ts = time.time()
        current_symbols = set()
        
        for pos in positions:
            inst_id = pos["instId"]
            pos_amt = float(pos.get("pos", 0))
            if pos_amt == 0:
                continue
            current_symbols.add(inst_id)
            upl_ratio = float(pos.get("uplRatio", 0))
            
            if inst_id not in active_positions_tracker:
                active_positions_tracker[inst_id] = {
                    "open_time": now_ts, "peak_roi": upl_ratio, "be_protected": False
                }
            tracker = active_positions_tracker[inst_id]
            if upl_ratio > tracker["peak_roi"]:
                tracker["peak_roi"] = upl_ratio
                
            # 1. 保本
            if not tracker["be_protected"] and upl_ratio >= BE_TRIGGER_ROI:
                tracker["be_protected"] = True
                send_telegram(f"🛡️【自動保本防護觸發】{inst_id} 浮盈: +{upl_ratio*100:.1f}%，鎖定零虧損！")

            # 2. 波段鎖利
            if upl_ratio >= TRAILING_TRIGGER_ROI:
                floor_roi = tracker["peak_roi"] * TRAILING_PROFIT_FLOOR
                if upl_ratio <= floor_roi:
                    send_telegram(f"🏆【波段利潤收割】{inst_id} 觸及地板線 +{floor_roi*100:.1f}%，市價停利！")
                    close_market(inst_id)
                    continue

            # 3. 超時退場
            if (now_ts - tracker["open_time"]) >= MOMENTUM_TIMEOUT_SEC and -0.05 <= upl_ratio <= 0.05:
                send_telegram(f"⏱️【動能衰竭超時】{inst_id} 90 分鐘無動能，微損/保本撤出！")
                close_market(inst_id)
                continue

        for old in list(active_positions_tracker.keys()):
            if old not in current_symbols:
                active_positions_tracker.pop(old, None)
    except Exception as e:
        print(f"持倉管理異常: {e}")

def close_market(inst_id: str):
    okx_request("POST", "/api/v5/trade/close-position", {"instId": inst_id, "mgnMode": "cross"})

# ==============================================================================
# 🌐 守護伺服器 (防止 Railway 判定無 Web 服務而 Stopping Container)
# ==============================================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"OKX QUANT BOT RUNNING OK")
    def log_message(self, format, *args):
        pass

def start_health_server():
    server = HTTPServer(('0.0.0.0', PORT), HealthHandler)
    server.serve_forever()

# ==============================================================================
# 🚀 主程式入口
# ==============================================================================
if __name__ == "__main__":
    send_telegram("🤖 OKX 手機版原版移植量化核心已在 Railway 正常啟動！")
    
    # 啟動 Web 保活伺服器 (防止 Railway 關閉容器)
    threading.Thread(target=start_health_server, daemon=True).start()
    
    # 啟動 Telegram 監聽線程 (支援群組輸入「帳戶」、「說明」)
    threading.Thread(target=tg_polling_loop, daemon=True).start()
    
    # 主量化循環
    while True:
        try:
            manage_open_positions()
            btc_ok, btc_trend = check_btc_macro()
            if btc_ok:
                for symbol in MONITOR_SYMBOLS:
                    if symbol not in active_positions_tracker:
                        res = analyze_symbol(symbol, btc_trend)
                        if res:
                            open_position(res)
                            time.sleep(3)
        except Exception as e:
            print(f"主循環異常: {e}")
        time.sleep(30)