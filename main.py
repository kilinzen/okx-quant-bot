import os
import time
import hmac
import base64
import hashlib
import json
import logging
import threading
import requests
import schedule
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ==========================================
# 0. 環境變量配置
# ==========================================
OKX_API_KEY = os.getenv("OKX_API_KEY", "").strip()
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "").strip()
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "").strip()
OKX_SIMULATED = os.getenv("OKX_SIMULATED", "true").lower() == "true"

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()

# 🎯 資金分配核心 (動態彈性)
TRADE_EQUITY_PCT = float(os.getenv("TRADE_EQUITY_PCT", "8.0"))
MIN_MARGIN_USDT = float(os.getenv("MIN_MARGIN_USDT", "20.0"))
MAX_MARGIN_USDT = float(os.getenv("MAX_MARGIN_USDT", "500.0"))
TRADE_LEVERAGE = int(os.getenv("TRADE_LEVERAGE", "10"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "5"))

TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "6.0"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "3.0"))

OKX_HOST = "https://www.okx.com"
CT_VAL_CACHE = {}

def send_tg_msg(message: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TG_CHAT_ID, "text": message, "parse_mode": "HTML"}
        requests.post(url, json=payload, timeout=8)
    except Exception as e:
        logging.error(f"發送 TG 失敗: {e}")

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
        logging.error(f"OKX API 異常 ({path}): {e}")
        return None

def get_account_balance():
    res = okx_request("GET", "/api/v5/account/balance")
    if res and res.get("code") == "0" and res.get("data"):
        data = res["data"][0]
        total_eq = float(data.get("totalEq", "0.0"))
        avail_bal = 0.0
        for detail in data.get("details", []):
            if detail.get("ccy") == "USDT":
                avail_bal = float(detail.get("availBal", "0.0"))
                break
        return total_eq, avail_bal
    return 1000.0, 1000.0

def calculate_dynamic_margin(equity: float):
    calc = equity * (TRADE_EQUITY_PCT / 100.0)
    return round(max(MIN_MARGIN_USDT, min(calc, MAX_MARGIN_USDT)), 2)

def get_contract_val(inst_id: str):
    if inst_id in CT_VAL_CACHE:
        return CT_VAL_CACHE[inst_id]
    try:
        res = requests.get(f"{OKX_HOST}/api/v5/public/instruments?instType=SWAP&instId={inst_id}", timeout=5).json()
        if res.get("code") == "0" and res.get("data"):
            val = float(res["data"][0].get("ctVal", "1"))
            CT_VAL_CACHE[inst_id] = val
            return val
    except:
        pass
    return 1.0

def scan_best_coins():
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
            vol_24h = float(t.get("volCcy24h", "0"))
            if vol_24h < 5000000:
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

        candidates.sort(key=lambda x: abs(x["change24h"]), reverse=True)
        return candidates[:15]
    except Exception as e:
        return []

def calculate_ema(prices, period):
    if len(prices) < period:
        return prices[-1]
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def check_trade_signal(inst_id: str):
    try:
        url = f"{OKX_HOST}/api/v5/market/candles?instId={inst_id}&bar=15m&limit=30"
        res = requests.get(url, timeout=10).json()
        if res.get("code") != "0" or not res.get("data"):
            return None

        candles = res.get("data")
        candles.reverse()
        closes = [float(c[4]) for c in candles]

        current_price = closes[-1]
        ema_fast = calculate_ema(closes, 9)
        ema_slow = calculate_ema(closes, 21)
        bb_basis = sum(closes[-20:]) / 20

        if ema_fast > ema_slow and current_price > bb_basis:
            return "LONG"
        elif ema_fast < ema_slow and current_price < bb_basis:
            return "SHORT"
        return None
    except Exception:
        return None

def get_current_positions():
    res = okx_request("GET", "/api/v5/account/positions")
    if res and res.get("code") == "0":
        return [p for p in res.get("data", []) if float(p.get("pos", "0")) != 0]
    return []

def open_okx_position(inst_id: str, direction: str, price: float, dynamic_margin: float):
    side = "buy" if direction == "LONG" else "sell"
    pos_side = "long" if direction == "LONG" else "short"

    ct_val = get_contract_val(inst_id)
    notional_value = dynamic_margin * TRADE_LEVERAGE
    contract_unit_value = price * ct_val
    sz = max(1, int(notional_value / contract_unit_value)) if contract_unit_value > 0 else 1

    payload = {
        "instId": inst_id,
        "tdMode": "cross",
        "side": side,
        "posSide": pos_side,
        "ordType": "market",
        "sz": str(sz)
    }

    res = okx_request("POST", "/api/v5/trade/order", payload)
    if res and res.get("code") == "0":
        order_data = res.get("data", [{}])[0]
        if order_data.get("sCode") == "0":
            ord_id = order_data.get("ordId")
            msg = (
                f"⚡ <b>【智能彈性開倉成功】</b>\n"
                f"標的: <code>{inst_id}</code>\n"
                f"方向: <b>{direction}</b>\n"
                f"動態本金: <b>{dynamic_margin} USDT</b> (佔淨值 {TRADE_EQUITY_PCT}%)\n"
                f"槓桿倍數: {TRADE_LEVERAGE}x\n"
                f"單號: <code>{ord_id}</code>"
            )
            send_tg_msg(msg)
            return True
    return False

def close_okx_position(pos: dict, reason: str):
    inst_id = pos.get("instId")
    pos_side = pos.get("posSide", "net")
    payload = {
        "instId": inst_id,
        "mgnMode": "cross",
        "posSide": pos_side
    }
    res = okx_request("POST", "/api/v5/trade/close-position", payload)
    if res and res.get("code") == "0":
        pnl = pos.get("upl", "0")
        msg = f"🔔 <b>【自動平倉結算】</b>\n標的: <code>{inst_id}</code>\n原因: <b>{reason}</b>\n平倉損益: <b>{pnl} USDT</b>"
        send_tg_msg(msg)

def close_all_positions():
    positions = get_current_positions()
    if not positions:
        send_tg_msg("⚠️ 目前沒有任何持倉部位需要平倉。")
        return
    for pos in positions:
        close_okx_position(pos, "手動全平指令觸發")
    send_tg_msg(f"✅ 已執行全部平倉操作，共結算 {len(positions)} 筆部位。")

# ==========================================
# 💬 Telegram 雙向互動指令監聽器
# ==========================================
def handle_tg_command(text: str):
    cmd = text.strip().lower()

    if cmd in ["帳戶", "账户", "資產", "资产", "/account", "/balance"]:
        total_eq, avail_bal = get_account_balance()
        positions = get_current_positions()
        
        pos_text = ""
        if positions:
            for p in positions:
                symbol = p.get("instId", "").replace("-USDT-SWAP", "")
                side = "做多 🟢" if p.get("posSide") == "long" else "做空 🔴"
                upl = float(p.get("upl", "0"))
                ratio = float(p.get("uplRatio", "0")) * 100
                pos_text += f"\n• <b>{symbol}</b> ({side})\n  未實現損益: <code>{'+' if upl>=0 else ''}{upl:.2f} U ({ratio:+.2f}%)</code>"
        else:
            pos_text = "\n• 目前無運行中持倉部位"

        msg = (
            f"🏦 <b>【OKX 即時帳戶總覽】</b>\n"
            f"💰 帳戶總淨值: <b>{total_eq:,.2f} USD</b>\n"
            f"💵 可用保證金: <b>{avail_bal:,.2f} USDT</b>\n"
            f"🎯 模式: {'模擬盤 (Demo)' if OKX_SIMULATED else '實盤 (Live)'}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📈 <b>當前持倉 ({len(positions)} 筆):</b>{pos_text}"
        )
        send_tg_msg(msg)

    elif cmd in ["選幣", "选币", "推薦", "/scan"]:
        candidates = scan_best_coins()
        if not candidates:
            send_tg_msg("暫無符合強動量的熱門候選標的。")
            return
        list_str = "\n".join([f"• <b>{c['symbol']}</b>: 現價 {c['price']} | 24H: {c['change24h']:+.2f}%" for c in candidates[:8]])
        send_tg_msg(f"🎯 <b>【當前全市場量化熱門掃描榜】</b>\n━━━━━━━━━━━━━━━\n{list_str}")

    elif cmd in ["全平", "/closeall"]:
        close_all_positions()

    elif cmd in ["日報", "日报", "/report"]:
        send_daily_report()

    elif cmd in ["說明", "帮助", "help", "/help", "/start"]:
        help_msg = (
            "🤖 <b>【冰火菠蘿 OKX 智能指令導覽】</b>\n\n"
            "您可以直接在群組或私聊中輸入以下文字：\n\n"
            "• <b>帳戶</b> - 即時查看總資產、保證金與當前持倉部位\n"
            "• <b>選幣</b> - 查看目前 24H 動量最強的掃描榜單\n"
            "• <b>日報</b> - 立即生成並發送量化損益綜合日報\n"
            "• <b>全平</b> - 緊急平倉當前所有已持有的合約\n"
            "• <b>說明</b> - 顯示此功能指令導覽"
        )
        send_tg_msg(help_msg)

def tg_polling_loop():
    """背景執行緒：即時監聽 Telegram 訊息"""
    last_update_id = 0
    logging.info("💬 Telegram 雙向對話監聽器已啟動...")
    while True:
        try:
            url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=20"
            res = requests.get(url, timeout=25).json()
            if res.get("ok"):
                for update in res.get("result", []):
                    last_update_id = update.get("update_id", last_update_id)
                    msg_obj = update.get("message") or update.get("channel_post")
                    if msg_obj and "text" in msg_obj:
                        text = msg_obj["text"]
                        handle_tg_command(text)
        except Exception:
            pass
        time.sleep(2)

# ==========================================
# 4. 主巡邏與日報排程
# ==========================================
def main_trading_cycle():
    positions = get_current_positions()
    for pos in positions:
        upl_ratio = float(pos.get("uplRatio", "0")) * 100.0
        if upl_ratio >= TAKE_PROFIT_PCT:
            close_okx_position(pos, f"達成目標止盈 (+{upl_ratio:.2f}%)")
        elif upl_ratio <= -STOP_LOSS_PCT:
            close_okx_position(pos, f"觸發強制止損 ({upl_ratio:.2f}%)")

    if len(positions) < MAX_POSITIONS:
        total_eq, _ = get_account_balance()
        dynamic_margin = calculate_dynamic_margin(total_eq)
        candidates = scan_best_coins()
        for cand in candidates:
            if len(positions) >= MAX_POSITIONS:
                break
            inst_id = cand["instId"]
            if any(p.get("instId") == inst_id for p in positions):
                continue
            signal = check_trade_signal(inst_id)
            if signal:
                opened = open_okx_position(inst_id, signal, cand["price"], dynamic_margin)
                if opened:
                    positions = get_current_positions()
                time.sleep(1)

def send_daily_report():
    total_eq, avail_bal = get_account_balance()
    positions = get_current_positions()
    report = (
        f"📊 <b>【OKX 雲端量化機器人 - 定時日報】</b>\n"
        f"⏰ 時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"💰 帳戶總資產: <b>{total_eq:,.2f} USD</b>\n"
        f"💵 可用保證金: <b>{avail_bal:,.2f} USDT</b>\n"
        f"📈 當前持倉部位: <b>{len(positions)}</b> 筆\n"
        f"🎯 動態單筆規模: <b>{calculate_dynamic_margin(total_eq)} USDT</b> ({TRADE_EQUITY_PCT}%)\n"
        f"🤖 運行模式: {'模擬盤 (Demo)' if OKX_SIMULATED else '實盤 (Live)'}\n"
    )
    send_tg_msg(report)

if __name__ == "__main__":
    send_tg_msg("🚀 <b>OKX 24H 雲端量化機器人【全功能互動版】已啟動！輸入「說明」或「帳戶」可直接互動！</b>")
    
    # 啟動 Telegram 雙向指令監聽背景執行緒
    t = threading.Thread(target=tg_polling_loop, daemon=True)
    t.start()

    schedule.every(2).minutes.do(main_trading_cycle)
    schedule.every().day.at("08:00").do(send_daily_report)
    schedule.every().day.at("20:00").do(send_daily_report)

    main_trading_cycle()

    while True:
        schedule.run_pending()
        time.sleep(5)
