import os
import asyncio
import logging
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- CONFIG ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID"))
HELIUS_RPC = os.getenv("HELIUS_RPC")
# Lưu ý: Birdeye public key giới hạn rất gắt, nên dùng key xịn hoặc handle lỗi
BIRDSEYE_KEY = os.getenv("BIRDSEYE_KEY", "") 

tracked_wallets = {}
MIN_USD = float(os.getenv("MIN_USD", "100")) # Test nên để thấp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- UTILS ---
def detect_chain(w): 
    w = w.strip()
    # Solana address thường từ 32-44 ký tự Base58
    if 32 <= len(w) <= 44 and w[0] in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz": 
        return "sol"
    if w.startswith("0x") and len(w) == 42: 
        return "evm"
    return "unknown"

def get_token_info(mint):
    try:
        # Thêm timeout để tránh treo bot
        headers = {"X-API-KEY": BIRDSEYE_KEY, "accept": "application/json"}
        url = f"https://public-api.birdeye.so/defi/token_overview?address={mint}"
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            return resp.json().get("data", {})
    except Exception as e:
        logger.error(f"Lỗi lấy giá token {mint}: {e}")
    return {}

# ==================== CORE LOGIC ====================

async def send_alert(bot, text):
    try:
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID, 
            text=text, 
            parse_mode='Markdown', 
            disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Lỗi gửi tin nhắn: {e}")

async def track_wallet(app_bot, wallet, chain):
    """Loop chạy ngầm check giao dịch"""
    last_sig = None
    logger.info(f"Bắt đầu theo dõi: {wallet}")
    
    while wallet in tracked_wallets:
        try:
            if chain == "sol":
                # Lấy chữ ký giao dịch mới nhất
                payload = {
                    "jsonrpc": "2.0", "id": 1, 
                    "method": "getSignaturesForAddress", 
                    "params": [wallet, {"limit": 1}]
                }
                # Chạy requests trong executor để không chặn bot
                r = await asyncio.to_thread(requests.post, HELIUS_RPC, json=payload)
                data = r.json()
                
                if data.get("result"):
                    sig = data["result"][0]["signature"]
                    # Nếu có tx mới và không phải lần chạy đầu tiên
                    if last_sig and sig != last_sig:
                        logger.info(f"Phát hiện TX mới ví {wallet}: {sig}")
                        
                        tx_payload = {
                            "jsonrpc": "2.0", "id": 1, 
                            "method": "getTransaction", 
                            "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
                        }
                        tx_r = await asyncio.to_thread(requests.post, HELIUS_RPC, json=tx_payload)
                        tx_data = tx_r.json()
                        
                        if tx_data.get("result"):
                            await parse_solana_tx(app_bot, tx_data["result"], wallet, sig)
                    
                    last_sig = sig # Cập nhật last_sig
            
            await asyncio.sleep(3) # Nghỉ 3s
        except Exception as e:
            logger.error(f"Lỗi track {wallet}: {e}")
            await asyncio.sleep(10)

async def parse_solana_tx(bot, tx_data, wallet, sig):
    if not tx_data or not tx_data.get("meta"): return

    name = tracked_wallets[wallet]["name"]
    meta = tx_data["meta"]
    
    # Map số dư: {mint: amount}
    pre_balances = {x["mint"]: float(x["uiTokenAmount"]["uiAmount"] or 0) for x in meta.get("preTokenBalances", []) if x.get("owner") == wallet}
    post_balances = {x["mint"]: float(x["uiTokenAmount"]["uiAmount"] or 0) for x in meta.get("postTokenBalances", []) if x.get("owner") == wallet}
    
    # Lấy tập hợp tất cả các token có thay đổi
    all_mints = set(pre_balances.keys()) | set(post_balances.keys())
    
    for mint in all_mints:
        if mint == "So11111111111111111111111111111111111111112": continue # Bỏ qua SOL wrap (tùy chọn)

        pre = pre_balances.get(mint, 0)
        post = post_balances.get(mint, 0)
        diff = post - pre
        
        if abs(diff) == 0: continue # Không đổi thì bỏ qua

        # Lấy thông tin giá (chạy trong thread riêng để ko lag)
        info = await asyncio.to_thread(get_token_info, mint)
        if not info: continue

        symbol = info.get("symbol", "UNKNOWN")
        price = info.get("price", 0)
        mc = info.get("mc", 0)
        
        amount_token = abs(diff)
        usd_value = amount_token * price
        
        if usd_value < MIN_USD: continue

        # Logic hiển thị
        action = "BUY" if diff > 0 else "SELL"
        emoji = "🟢" if action == "BUY" else "🔴"
        
        # --- PnL Logic (Đơn giản hóa) ---
        # Lưu ý: PnL này chỉ chính xác nếu bot chạy liên tục từ lúc mua. 
        # Nếu restart bot, data cost_usd mất => PnL sai. Cần Database mới chuẩn.
        if mint not in tracked_wallets[wallet]["tokens"]:
            tracked_wallets[wallet]["tokens"][mint] = {"cost_usd": 0, "amount": 0}
        
        t_data = tracked_wallets[wallet]["tokens"][mint]
        pnl_str = "N/A"
        
        if action == "BUY":
            t_data["cost_usd"] += usd_value
            t_data["amount"] += amount_token
        elif action == "SELL":
            # Tính giá trung bình vốn
            avg_cost = (t_data["cost_usd"] / t_data["amount"]) if t_data["amount"] > 0 else price
            # PnL thực tế = (Giá bán - Giá vốn) * Số lượng bán
            realized_pnl = (price - avg_cost) * amount_token
            
            pnl_prefix = "+" if realized_pnl >= 0 else "-"
            pnl_str = f"{pnl_prefix}${abs(realized_pnl):,.2f}"
            
            # Trừ số lượng tồn kho
            t_data["amount"] = max(0, t_data["amount"] - amount_token)
            # Giảm vốn tương ứng
            t_data["cost_usd"] = max(0, t_data["cost_usd"] - (avg_cost * amount_token))

        # Soạn tin nhắn
        msg = f"""
{emoji} *{action} {symbol}* | {name}
-------------------------
💰 Volume: ${usd_value:,.2f}
🔢 Amount: {amount_token:,.2f} {symbol}
📉 MC: ${mc/1e6:,.1f}M @ ${price:.4f}
📊 PnL (Session): {pnl_str}

`{mint}`
[Birdeye](https://birdeye.so/token/{mint}?chain=solana) | [Photon](https://photon.sol/en/s/{mint}) | [Scan](https://solscan.io/tx/{sig})
"""
        await send_alert(bot, msg.strip())

# ==================== COMMANDS ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot Ready!\n/add [wallet] [name]\n/list\n/delete [wallet]")

async def add_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        return await update.message.reply_text("Sai cú pháp! VD: /add 9WzDX... Peter")
    
    wallet = context.args[0]
    name = " ".join(context.args[1:])
    chain = detect_chain(wallet)
    
    if chain == "unknown":
        return await update.message.reply_text("Ví không hợp lệ!")
    
    if wallet in tracked_wallets:
        return await update.message.reply_text("Ví này đã thêm rồi!")

    # Tạo task background, truyền context.bot vào để dùng gửi tin
    task = asyncio.create_task(track_wallet(context.bot, wallet, chain))
    
    tracked_wallets[wallet] = {
        "name": name, 
        "chain": chain, 
        "tokens": {}, 
        "task": task
    }
    
    await update.message.reply_text(f"✅ Đã thêm {name} ({chain.upper()})\n`{wallet}`", parse_mode='Markdown')

async def list_wallets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not tracked_wallets:
        return await update.message.reply_text("Chưa theo dõi ví nào.")
    
    msg = "*Danh sách theo dõi:*\n"
    for w, d in tracked_wallets.items():
        msg += f"- {d['name']}: `{w[:6]}...{w[-4:]}`\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def delete_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("/delete [wallet_address]")
    w = context.args[0]
    if w in tracked_wallets:
        tracked_wallets[w]["task"].cancel() # Dừng task ngầm
        del tracked_wallets[w]
        await update.message.reply_text(f"Đã xóa {w}")
    else:
        await update.message.reply_text("Không tìm thấy ví.")

async def delete_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for w in list(tracked_wallets.keys()):
        tracked_wallets[w]["task"].cancel()
    tracked_wallets.clear()
    await update.message.reply_text("Đã xóa tất cả!")

# ==================== MAIN ====================
if __name__ == "__main__":
    if not BOT_TOKEN or not HELIUS_RPC:
        print("❌ Thiếu BOT_TOKEN hoặc HELIUS_RPC trong env!")
    else:
        print("🚀 BOT ĐANG CHẠY...")
        app = Application.builder().token(BOT_TOKEN).build()
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("add", add_wallet))
        app.add_handler(CommandHandler("list", list_wallets))
        app.add_handler(CommandHandler("delete", delete_wallet))
        app.add_handler(CommandHandler("deleteall", delete_all))
        app.run_polling()
