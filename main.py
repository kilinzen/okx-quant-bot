import os
import time
import hmac
import base64
import hashlib
import json
import logging
import requests
import schedule
from datetime import datetime, timezone

# 設置日誌
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ==========================================
# 0. 環境變量配置讀取
# ==========================================
OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")
OKX_SIMULATED = os.getenv("OKX_SIMULATED", "true").lower() == "true"  # true: 模擬盤, false: 實盤

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

TRADE_MARGIN_USDT = float(os.getenv("TRADE_MARGIN_USDT", "100"))  # 每筆本金
TRADE_LEVERAGE = int(os.getenv("TRADE_LEVERAGE", "10"))           # 槓桿倍數
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "5"))              # 最多同時持倉幣種數
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "3.0"))          # 止損 %
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "6.0"))      # 止盈 %

OKX_HOST = "https://www.okx.com"

# ==========================================
# 1. Telegram 通知模組
# ==========================================
def send_tg_msg(message: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        logging.info(f"[TG 未配置] {message}")
        return
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=8)
    except Exception as e:
        logging.error(f"發送 TG 消息失敗: {e}")

# ==========================================
# 2. OKX API 簽名與請求模組
# ==========================================
def okx_signature(timestamp: str, method: str, request_path: str, body: str = ""):
    message = timestamp + method.upper() + request_path + body
    mac = hmac.new(OKX_SECRET_KEY.encode("utf-8"), message.encode("utf-8"), digestmod=hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")

def okx_request(method: str, path: str, body_dict: dict = None):
    url = OKX_HOST + path
    now = datetime.now(timezone.utc).isoformat()[:-9] + "Z"
    body_str = json.dumps(body_dict) if body_dict else ""
    sign = okx_signature(now, method, path, body_str)

    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": now,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json"
    }
    if OKX_SIMULATED:
        headers["x-simulated-trading"] = "1"

    try:
        if method.upper() == "GET":
            res = requests.get(url, headers=headers, timeout=10)
        else:
            res = requests.post(url, headers=headers, data=body_str, timeout=10)
        return res.json()
    except Exception as e:
        logging.error(f"OKX API 請求異常 ({path}): {e}")
        return None

# ==========================================
# 3. 全市場自動掃描選幣模組 (Market Scanner)
# ==========================================
def scan_best_coins():
    """從 OKX 所有永續合約中掃描出波動動量最強、交易活躍的熱門幣種"""
    logging.info("🔍 開始全市場掃描熱門強勢合約...")
    try:
        url = f"{OKX_HOST}/api/v5/market/tickers?instType=SWAP"
        res = requests.get(url, timeout=10).json()
        if res.get("code") != "0":
            return []

        tickers = res.get("data", [])
        candidates = []
        for t in tickers:
            inst_id = t.get("instId", "")
            if not inst_id.endswith("-USDT-SWAP"):
                continue
            
            # 排除成交量極小的死幣
            vol_24h = float(t.get("volCcy24h", "0"))
            if vol_24h < 5000000:  # 24h 成交量需大於 500 萬 USDT
                continue

            open_24h = float(t.get("open24h", "1"))
            last_price = float(t.get("last", "1"))
            change_24h = ((last_price - open_24h) / open_24h) * 100.0

            candidates.append({
                "instId": inst_id,
                "symbol": inst_id.replace("-USDT-SWAP", ""),
                "price": last_price,
                "change24h": change_24h,
                "vol": vol_24h
            })

        # 排序：優先選出成交量大且處於強動量區間的幣種
        candidates.sort(key=lambda x: abs(x["change24h"]), reverse=True)
        top_candidates = candidates[:15]
        logging.info(f"掃描完畢，選出前 {len(top_candidates)} 隻強勢觀察幣: {[c['symbol'] for c in top_candidates]}")
        return top_candidates
    except Exception as e:
        logging.error(f"全市場選幣異常: {e}")
        return []

# ==========================================
# 4. K 線分析與趨勢突破策略 (Strategy)
# ==========================================
def calculate_ema(prices, period):
    if len(prices) < period:
        return prices[-1]
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def check_trade_signal(inst_id: str):
    """抓取 15 分鐘 K 線，計算 EMA 均線突破 + 布林動量"""
    try:
        url = f"{OKX_HOST}/api/v5/market/candles?instId={inst_id}&bar=15m&limit=30"
        res = requests.get(url, timeout=10).json()
        if res.get("code") != "0" or not res.get("data"):
            return None

        candles = res.get("data")
        candles.reverse()  # 轉為時間由舊到新
        closes = [float(c[4]) for c in candles]

        current_price = closes[-1]
        ema_fast = calculate_ema(closes, 9)
        ema_slow = calculate_ema(closes, 21)

        # 做多條件：9 EMA 向上突破 21 EMA 且現價高於均線
        if ema_fast > ema_slow and current_price > ema_fast:
            return "LONG"
        # 做空條件：9 EMA 向下跌破 21 EMA 且現價低於均線
        elif ema_fast < ema_slow and current_price < ema_fast:
            return "SHORT"
        return None
    except Exception as e:
        logging.error(f"分析信號異常 ({inst_id}): {e}")
        return None

# ==========================================
# 5. OKX 下單與平倉執行引擎 (Trading Engine)
# ==========================================
def get_current_positions():
    """獲取當前 OKX 官方持倉"""
    res = okx_request("GET", "/api/v5/account/positions")
    if res and res.get("code") == "0":
        return [p for p in res.get("data", []) if float(p.get("pos", "0")) != 0]
    return []

def open_okx_position(inst_id: str, direction: str, price: float):
    side = "buy" if direction == "LONG" else "sell"
    pos_side = "long" if direction == "LONG" else "short"
    sz = max(1, int((TRADE_MARGIN_USDT * TRADE_LEVERAGE) / 10))  # 合約張數換算

    payload = {
        "instId": inst_id,
        "tdMode": "cross",
        "side": side,
        "posSide": pos_side,
        "ordType": "market",
        "sz": str(sz)
    }

    logging.info(f"🚀 送出 OKX 開倉委託: {inst_id} {direction} {sz} 張...")
    res = okx_request("POST", "/api/v5/trade/order", payload)
    if res and res.get("code") == "0":
        order_data = res.get("data", [{}])[0]
        s_code = order_data.get("sCode")
        if s_code == "0":
            ord_id = order_data.get("ordId")
            msg = f"⚡ <b>【自動開倉成功】</b>\n標的: <code>{inst_id}</code>\n方向: <b>{direction}</b>\n槓桿: {TRADE_LEVERAGE}x\n進場參考價: {price}\n單號: {ord_id}"
            send_tg_msg(msg)
            return True
        else:
            logging.error(f"下單被拒絕: {order_data.get('sMsg')}")
    return False

def close_okx_position(pos: dict, reason: str):
    inst_id = pos.get("instId")
    pos_side = pos.get("posSide", "net")
    side = "sell" if pos_side == "long" else "buy"
    sz = abs(float(pos.get("pos", "0")))

    payload = {
        "instId": inst_id,
        "mgnMode": "cross",
        "posSide": pos_side
    }
    logging.info(f"🛑 送出平倉委託: {inst_id} 原因: {reason}")
    res = okx_request("POST", "/api/v5/trade/close-position", payload)
    if res and res.get("code") == "0":
        pnl = pos.get("upl", "0")
        msg = f"🔔 <b>【自動平倉觸發】</b>\n標的: <code>{inst_id}</code>\n原因: <b>{reason}</b>\n浮動盈虧: {pnl} USDT"
        send_tg_msg(msg)

# ==========================================
# 6. 主循環監控與風控巡邏 (Main Loop)
# ==========================================
def main_trading_cycle():
    logging.info("--- 🔄 執行量化巡邏與風控週期 ---")
    
    # 1. 檢查當前持倉並執行止盈/止損
    positions = get_current_positions()
    logging.info(f"目前持有部位: {len(positions)} 筆")
    
    for pos in positions:
        upl_ratio = float(pos.get("uplRatio", "0")) * 100.0  # 浮盈虧百分比
        inst_id = pos.get("instId")
        if upl_ratio >= TAKE_PROFIT_PCT:
            close_okx_position(pos, f"達到目標止盈 (+{upl_ratio:.2f}%)")
        elif upl_ratio <= -STOP_LOSS_PCT:
            close_okx_position(pos, f"觸發強制止損 ({upl_ratio:.2f}%)")

    # 2. 如果持倉未達上限，進行全市場動態選幣與開倉
    if len(positions) < MAX_POSITIONS:
        candidates = scan_best_coins()
        for cand in candidates:
            if len(positions) >= MAX_POSITIONS:
                break
            
            inst_id = cand["instId"]
            # 避免重複持有同一幣種
            if any(p.get("instId") == inst_id for p in positions):
                continue

            signal = check_trade_signal(inst_id)
            if signal:
                opened = open_okx_position(inst_id, signal, cand["price"])
                if opened:
                    positions = get_current_positions()
                time.sleep(1)

# ==========================================
# 7. Telegram 定時資產日報 (Daily Report)
# ==========================================
def send_daily_report():
    bal_res = okx_request("GET", "/api/v5/account/balance")
    total_eq = "0.00"
    if bal_res and bal_res.get("code") == "0":
        data = bal_res.get("data", [{}])[0]
        total_eq = data.get("totalEq", "0.00")

    positions = get_current_positions()
    report = (
        f"📊 <b>【OKX 雲端量化機器人 - 定時日報】</b>\n"
        f"⏰ 時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"💰 帳戶總資產估值: <b>{float(total_eq):,.2f} USD</b>\n"
        f"📈 當前運行中持倉: <b>{len(positions)}</b> 筆\n"
        f"🤖 運行模式: {'模擬盤 (Demo)' if OKX_SIMULATED else '實盤 (Live)'}\n"
    )
    send_tg_msg(report)

if __name__ == "__main__":
    send_tg_msg("🚀 <b>OKX 24H 雲端量化自動選幣機器人 已啟動上線！</b>")
    
    # 排程：每 2 分鐘進行一次全市場掃描與風控巡邏
    schedule.every(2).minutes.do(main_trading_cycle)
    # 排程：每天 08:00 與 20:00 發送定時資產日報
    schedule.every().day.at("08:00").do(send_daily_report)
    schedule.every().day.at("20:00").do(send_daily_report)

    # 首次啟動立即執行一次
    main_trading_cycle()

    while True:
        schedule.run_pending()
        time.sleep(5)
