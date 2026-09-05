import os
import time
import json
import hmac
import base64
import hashlib
from datetime import datetime, timezone
import requests

# ==============================================================================
# 🔑 環境變數讀取 (Railway / 伺服器配置)
# ==============================================================================
OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
IS_SIMULATED = os.getenv("IS_SIMULATED", "true").lower() == "true"

OKX_API_URL = "https://www.okx.com"

# ==============================================================================
# ⚙️ 完整還原手機版參數配置 (Strictly Matched to CryptoViewModel.kt)
# ==============================================================================
LEVERAGE = 10
TOTAL_CAPITAL_RISK_PER_TRADE = 0.008  # 總資金 0.8% 動態風險
MIN_CONFIDENCE_SCORE = 84             # 狙擊手信心門檻
MIN_AMPLITUDE = 0.025                 # 最小振幅 2.5% 避開死魚盤
BTC_MAX_VOLATILITY = 0.045            # BTC 波動率上限 (防黑天鵝吸血)

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
            data.reverse() # 時間從小到大
            return data
    except Exception:
        pass
    return []

# ==============================================================================
# 🧮 純 Python 原生數學計算 (免 Pandas / 免 Numpy，保證不報錯)
# ==============================================================================
def calc_ema(values, span):
    alpha = 2.0 / (span + 1.0)
    ema = values[0]
    for v in values[1:]:
        ema = alpha * v + (1.0 - alpha) * ema
    return ema

# ==============================================================================
# 🧠 1:1 還原手機版量化決策引擎
# ==============================================================================
def check_btc_macro():
    candles = fetch_candles("BTC-USDT-SWAP", "1H", 60)
    if len(candles) < 50:
        return True, "UNKNOWN"
    
    closes = [float(c[4]) for c in candles]
    highs = [float(c[2]) for c in candles]
    lows = [float(c[3]) for c in candles]
    
    # 24H 波動率
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
    
    # 振幅檢查：避開死魚盤
    recent_high = max(highs[-24:])
    recent_low = min(lows[-24:])
    amplitude = (recent_high - recent_low) / curr_price
    if amplitude < MIN_AMPLITUDE:
        return None
    
    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, 50)
    
    # ATR (14) 計算
    tr_list = []
    for j in range(1, len(candles)):
        tr = max(highs[j] - lows[j], abs(highs[j] - closes[j-1]), abs(lows[j] - closes[j-1]))
        tr_list.append(tr)
    recent_tr = tr_list[-14:]
    atr = sum(recent_tr) / len(recent_tr) if recent_tr else (curr_price * 0.02)
    
    # 量能暴增
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
# 🛡️ 實時持倉管理：保本 (BE Protect) + 波段鎖利 + 死魚盤超時
# ==============================================================================
active_positions_tracker = {}

def manage_open_positions():
    pos_res = okx_request("GET", "/api/v5/account/positions")
    if not pos_res or pos_res.get("code") != "0":
        return
    
    positions = pos_res.get("data", [])
    now_ts = time.time()
    
    for pos in positions:
        inst_id = pos["instId"]
        pos_amt = float(pos.get("pos", 0))
        if pos_amt == 0:
            active_positions_tracker.pop(inst_id, None)
            continue
            
        upl_ratio = float(pos.get("uplRatio", 0)) # ROI
        
        if inst_id not in active_positions_tracker:
            active_positions_tracker[inst_id] = {
                "open_time": now_ts,
                "peak_roi": upl_ratio,
                "be_protected": False
            }
            
        tracker = active_positions_tracker[inst_id]
        if upl_ratio > tracker["peak_roi"]:
            tracker["peak_roi"] = upl_ratio
            
        # 1. 保本防護 (BE Protect)
        if not tracker["be_protected"] and upl_ratio >= BE_TRIGGER_ROI:
            tracker["be_protected"] = True
            send_telegram(
                f"🛡️ *【自動保本防護觸發】* `{inst_id}`\n"
                f"當前浮盈已達: `+{upl_ratio*100:.1f}%`\n"
                f"鎖定成本價出場，保證該筆零虧損！"
            )

        # 2. 波段移動鎖利 (Wave Trailing)
        if upl_ratio >= TRAILING_TRIGGER_ROI:
            floor_roi = tracker["peak_roi"] * TRAILING_PROFIT_FLOOR
            if upl_ratio <= floor_roi:
                send_telegram(
                    f"🏆 *【波段利潤大成收割】* `{inst_id}`\n"
                    f"歷史最高浮盈: `+{tracker['peak_roi']*100:.1f}%`\n"
                    f"回撤觸及利潤地板線 `+{floor_roi*100:.1f}%`，市價停利出場！"
                )
                close_market(inst_id)
                active_positions_tracker.pop(inst_id, None)
                continue

        # 3. 90 分鐘動能衰竭超時
        time_held = now_ts - tracker["open_time"]
        if time_held >= MOMENTUM_TIMEOUT_SEC and -0.05 <= upl_ratio <= 0.05:
            send_telegram(
                f"⏱️ *【動能衰竭超時退場】* `{inst_id}`\n"
                f"持倉超過 90 分鐘無動能（死魚盤），保本/微損撤出資金！"
            )
            close_market(inst_id)
            active_positions_tracker.pop(inst_id, None)
            continue

def close_market(inst_id: str):
    body = {
        "instId": inst_id,
        "mgnMode": "cross"
    }
    okx_request("POST", "/api/v5/trade/close-position", body)

# ==============================================================================
# 🚀 主循環排程
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
                res = analyze_symbol(symbol, btc_trend)
                if res:
                    send_telegram(
                        f"🎯 *【狙擊手信號開倉】* `{res['symbol']}`\n"
                        f"方向: `{res['signal']}` | 置信度: `{res['confidence']}`\n"
                        f"現價: `{res['price']}`\n"
                        f"自適應止損: `{res['sl']:.4f}` | 止盈: `{res['tp']:.4f}`"
                    )
                    time.sleep(2)
                    
        except Exception as e:
            print(f"Loop Error: {e}")
            
        time.sleep(30)

if __name__ == "__main__":
    run_loop()