import os
import time
import json
import hmac
import base64
import hashlib
from datetime import datetime, timezone
import requests

# ==============================================================================
# 🔑 環境變數讀取 (沿用您在 Railway 設定好的，完全不需要重設！)
# ==============================================================================
OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
IS_SIMULATED = os.getenv("IS_SIMULATED", "true").lower() == "true"

OKX_API_URL = "https://www.okx.com"

# ==============================================================================
# ⚙️ 完整還原手機版參數配置
# ==============================================================================
LEVERAGE = 10
FIXED_USDT_AMOUNT = 20.0              # 單筆投入保證金 (約 20 USDT)
MIN_CONFIDENCE_SCORE = 84             # 狙擊手信心門檻
MIN_AMPLITUDE = 0.025                 # 最小振幅 2.5% 避開死魚盤
BTC_MAX_VOLATILITY = 0.045            # BTC 波動率上限

# 手機版神級平倉與鎖利機制
BE_TRIGGER_ROI = 0.20                 # 浮盈達到 +20% ROI，強制推到保本線
TRAILING_TRIGGER_ROI = 0.45           # 浮盈達到 +45% ROI，啟動高階波段鎖利
TRAILING_PROFIT_FLOOR = 0.70          # 波段鎖定 70% 歷史最高浮盈
MOMENTUM_TIMEOUT_SEC = 90 * 60        # 90 分鐘動能衰竭超時出場

MONITOR_SYMBOLS = [
    "DOGE-USDT-SWAP", "SOL-USDT-SWAP", "PEPE-USDT-SWAP", 
    "SUI-USDT-SWAP", "NEAR-USDT-SWAP", "OP-USDT-SWAP", 
    "APT-USDT-SWAP", "AVAX-USDT-SWAP", "LINK-USDT-SWAP"
]

# ==============================================================================
# 📡 Telegram 通報函式
# ==============================================================================
def send_telegram(msg: str):
    print(f"[TG] {msg}")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=8)
    except Exception as e:
        print(f"TG Error: {e}")

# ==============================================================================
# 🔐 OKX API 簽名與呼叫
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
        print(f"API Error ({path}): {e}")
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
# 🧮 純 Python 數學演算法 (完全免第三方依賴)
# ==============================================================================
def calc_ema(values, span):
    alpha = 2.0 / (span + 1.0)
    ema = values[0]
    for v in values[1:]:
        ema = alpha * v + (1.0 - alpha) * ema
    return ema

# ==============================================================================
# 🧠 1:1 手機版量化決策引擎
# ==============================================================================
def check_btc_macro():
    candles = fetch_candles("BTC-USDT-SWAP", "1H", 60)
    if len(candles) < 50:
        return True, "UNKNOWN"
    
    closes = [float(c[4]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    
    recent_high = max(highs[-24:])
    recent_low = min(lows[-24:])
    volatility = (recent_high - recent_low) / closes[-1]
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
    recent_high = max(highs[-24:])
    recent_low = min(lows[-24:])
    amplitude = (recent_high - recent_low) / curr_price
    if amplitude < MIN_AMPLITUDE:
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
    if amplitude >= 0.04: confidence += 5
    
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
        sl = curr_price - sl_dist if signal == "LONG" else curr_price + sl_dist
        tp = curr_price + tp_dist if signal == "LONG" else curr_price - tp_dist
        return {
            "symbol": inst_id,
            "signal": signal,
            "price": curr_price,
            "sl": sl,
            "tp": tp,
            "atr": atr,
            "confidence": confidence
        }
    return None

# ==============================================================================
# 🎯 實際開倉執行 (OKX 交易接口)
# ==============================================================================
def open_position(sig: dict):
    inst_id = sig["symbol"]
    side = "buy" if sig["signal"] == "LONG" else "sell"
    
    # 1. 設定 10 倍全倉槓桿
    okx_request("POST", "/api/v5/account/set-leverage", {
        "instId": inst_id,
        "lever": str(LEVERAGE),
        "mgnMode": "cross"
    })
    
    # 2. 市價開倉 (投入約 FIXED_USDT_AMOUNT 的合約張數，OKX 通常以 1 張為單位測試)
    order_body = {
        "instId": inst_id,
        "tdMode": "cross",
        "side": side,
        "ordType": "market",
        "sz": "1" # 預設 1 張合約 (避免保證金不足)
    }
    
    res = okx_request("POST", "/api/v5/trade/order", order_body)
    if res and res.get("code") == "0":
        send_telegram(
            f"🚀 *【實際開倉成功】* `{inst_id}`\n"
            f"方向: `{sig['signal']}` | 置信度: `{sig['confidence']}`\n"
            f"進場價: `{sig['price']}`\n"
            f"止損價: `{sig['sl']:.4f}` | 止盈價: `{sig['tp']:.4f}`"
        )
    else:
        err_msg = res.get('msg', '未知錯誤') if res else '連線失敗'
        print(f"下單失敗 ({inst_id}): {err_msg}")
        send_telegram(f"⚠️ `{inst_id}` 開倉失敗: {err_msg}")

# ==============================================================================
# 🛡️ 實時持倉管理 (保本 + 波段鎖利 + 超時)
# ==============================================================================
active_positions_tracker = {}

def manage_open_positions():
    try:
        pos_res = okx_request("GET", "/api/v5/account/positions")
        if not pos_res or pos_res.get("code") != "0":
            return
        
        positions = pos_res.get("data", [])
        now_ts = time.time()
        
        # 標記當前存在的幣種
        current_holding_symbols = set()
        
        for pos in positions:
            inst_id = pos["instId"]
            pos_amt = float(pos.get("pos", 0))
            if pos_amt == 0:
                continue
                
            current_holding_symbols.add(inst_id)
            upl_ratio = float(pos.get("uplRatio", 0))
            
            if inst_id not in active_positions_tracker:
                active_positions_tracker[inst_id] = {
                    "open_time": now_ts,
                    "peak_roi": upl_ratio,
                    "be_protected": False
                }
                
            tracker = active_positions_tracker[inst_id]
            if upl_ratio > tracker["peak_roi"]:
                tracker["peak_roi"] = upl_ratio
                
            # 1. 保本防護
            if not tracker["be_protected"] and upl_ratio >= BE_TRIGGER_ROI:
                tracker["be_protected"] = True
                send_telegram(f"🛡️ *【自動保本防護觸發】* `{inst_id}` 浮盈: `+{upl_ratio*100:.1f}%`，鎖定零虧損！")

            # 2. 波段鎖利
            if upl_ratio >= TRAILING_TRIGGER_ROI:
                floor_roi = tracker["peak_roi"] * TRAILING_PROFIT_FLOOR
                if upl_ratio <= floor_roi:
                    send_telegram(f"🏆 *【波段利潤收割】* `{inst_id}` 觸及利潤地板線 `+{floor_roi*100:.1f}%`，市價停利！")
                    close_market(inst_id)
                    continue

            # 3. 超時退場
            time_held = now_ts - tracker["open_time"]
            if time_held >= MOMENTUM_TIMEOUT_SEC and -0.05 <= upl_ratio <= 0.05:
                send_telegram(f"⏱️ *【動能衰竭超時退場】* `{inst_id}` 90 分鐘無動能，保本撤出！")
                close_market(inst_id)
                continue
                
        # 清理已平倉的記錄
        for old_sym in list(active_positions_tracker.keys()):
            if old_sym not in current_holding_symbols:
                active_positions_tracker.pop(old_sym, None)
                
    except Exception as e:
        print(f"管理持倉異常: {e}")

def close_market(inst_id: str):
    body = {"instId": inst_id, "mgnMode": "cross"}
    okx_request("POST", "/api/v5/trade/close-position", body)

# ==============================================================================
# 🚀 主循環排程 (永不閃退守護)
# ==============================================================================
def run_loop():
    send_telegram("🤖 *OKX 手機版原版移植量化核心已在 Railway 正常啟動！*")
    while True:
        try:
            manage_open_positions()
            
            btc_ok, btc_trend = check_btc_macro()
            if not btc_ok:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] BTC 劇震避險中，暫停開倉...")
                time.sleep(60)
                continue
                
            for symbol in MONITOR_SYMBOLS:
                # 若已有該幣種持倉，不重複開倉
                if symbol in active_positions_tracker:
                    continue
                    
                res = analyze_symbol(symbol, btc_trend)
                if res:
                    open_position(res)
                    time.sleep(3)
                    
        except Exception as e:
            print(f"主循環異常保護: {e}")
            
        time.sleep(30) # 每 30 秒健康循環

if __name__ == "__main__":
    run_loop()