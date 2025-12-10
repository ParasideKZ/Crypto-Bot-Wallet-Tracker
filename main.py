import os
import asyncio
import logging
import requests
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID"))
HELIUS_RPC = os.getenv("HELIUS_RPC")
ALCHEMY_KEY = os.getenv("ALCHEMY_KEY")
BIRDSEYE_KEY = os.getenv("BIRDSEYE_KEY", "")

tracked_wallets = {}
logging.basicConfig(level=logging.INFO)

async def send_alert(text):
    await Application.builder().token(BOT_TOKEN).build().bot.send_message(
        chat_id=ADMIN_CHAT_ID, text=text, parse_mode='Markdown', disable_web_page_preview=True
    )

def detect_chain(w): 
    w = w.strip()
    if len(w) == 44 and w[0] in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz": 
        return "sol"
    if w.startswith("0x") and len(w) == 42: 
        return "evm"
    return "unknown"

# ==================== LỆNH ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot QUÁI VẬT đã sẵn sàng!\n/add [wallet] [tên ví]\n/list | /delete | /deleteall")

MIN_USD = float(os.getenv("MIN_USD", "500"))

async def add_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("Sai! Ví dụ: /add 9WzDX... Peter")
        return
    wallet = context.args[0].lower()
    name = " ".join(context.args[1:])
    chain = detect_chain(wallet)
    if chain == "unknown":
        await update.message.reply_text("Ví lạ!")
        return
    if wallet in tracked_wallets:
        await update.message.reply_text("Ví đã có rồi!")
        return
    
    task = asyncio.create_task(track_wallet(wallet, chain))
    tracked_wallets[wallet] = {"name": name, "chain": chain, "task": task, "tokens": {}}
    await update.message.reply_text(f"Đã thêm:\n{name}\n`{wallet[:8]}...{wallet[-4:]}`", parse_mode='Markdown')

async def list_wallets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not tracked_wallets: 
        await update.message.reply_text("Chưa có ví nào.")
        return
    msg = "*Đang track:*\n\n"
    for w, d in tracked_wallets.items():
        icon = "💊" if d["chain"] == "sol" else "🔗"
        msg += f"{icon} {d['name']} | `{w[:8]}...{w[-4:]}`\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def delete_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: 
        await update.message.reply_text("Dùng: /delete [wallet]")
        return
    w = context.args[0].lower()
    if w not in tracked_wallets:
        await update.message.reply_text("Không thấy ví này!")
        return
    tracked_wallets[w]["task"].cancel()
    del tracked_wallets[w]
    await update.message.reply_text(f"Đã xóa `{w[:8]}...`", parse_mode='Markdown')

async def delete_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for w in list(tracked_wallets.keys()):
        tracked_wallets[w]["task"].cancel()
    tracked_wallets.clear()
    await update.message.reply_text("Xóa sạch rồi đại ca!")

# ==================== TRACK CHUNG ====================
async def track_wallet(wallet, chain):
    last_sig = None
    while wallet in tracked_wallets:
        try:
            if chain == "sol":
                payload = {"jsonrpc":"2.0","id":1,"method":"getSignaturesForAddress","params":[wallet,{"limit":1}]}
                r = requests.post(HELIUS_RPC, json=payload).json()
                if r.get("result") and r["result"]:
                    sig = r["result"][0]["signature"]
                    if sig != last_sig:
                        last_sig = sig
                        tx = requests.post(HELIUS_RPC, json={"jsonrpc":"2.0","id":1,"method":"getTransaction","params":[sig,{"encoding":"jsonParsed","maxSupportedTransactionVersion":0}]}).json()
                        if tx.get("result"):
                            await parse_solana_tx(tx["result"], wallet)
            else:
                # EVM dùng Alchemy (có thể mở rộng sau)
                pass
            await asyncio.sleep(4 if chain=="sol" else 8)
        except Exception as e:
            logging.error(e)
            await asyncio.sleep(10)

# ==================== PARSE SOLANA & EVM – ĐẸP THEO FORMAT BẠN MUỐN ====================
async def parse_solana_tx(tx_data, wallet):
    name = tracked_wallets[wallet]["name"]
    pre = tx_data["meta"].get("preTokenBalances", [])
    post = tx_data["meta"].get("postTokenBalances", [])
    
    for p, po in zip(pre+post, post+pre):
        if p.get("owner") != wallet: continue
        mint = p.get("mint")
        if not mint or mint == "So11111111111111111111111111111111111111112": continue
        
        pre_amt = float(p.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
        post_amt = float(po.get("uiTokenAmount", {}).get("uiAmount", 0) or 0)
        diff = post_amt - pre_amt
        if abs(diff) < 1: continue
        
        action = "SELL" if diff < 0 else "BUY"
        color = "🔴" if action == "SELL" else "🟢"
        amount_token = abs(diff)
        
        info = requests.get(f"https://public-api.birdeye.so/defi/token_overview?address={mint}", headers={"X-API-KEY": BIRDSEYE_KEY}).json().get("data", {})
        symbol = info.get("symbol", "UNKNOWN")
        price = info.get("price", 0)
        mc = info.get("mc", 0)
        usd_value = amount_token * price
        if usd_value < MIN_USD: return
        
        pnl = 0
        pnl_str = f"{'-' if pnl < 0 else '+'}{abs(pnl):,.0f} USDT ({'+' if pnl >= 0 else ''}{pnl/usd_value*100 if usd_value else 0:.1f}%)"
        pnl_color = "🔴" if pnl < 0 else "🟢"
        
        msg = f"""
{color} *{action} {symbol}*
🔹 Ví *{name}*

🔹 {action} ∙ {amount_token:,.2f} {symbol} (${usd_value:,.0f}) for {usd_value/190:.2f} SOL @${price:.10f}

{pnl_color} Còn giữ {symbol}: 100% ██████████  - PnL: {pnl_str}

💊 *#{symbol}* | MC: ${mc/1e6:.2f}M | Seen: just now
[BE](https://birdeye.so/token/{mint}?chain=solana) | [DS](https://dexscreener.com/solana/{mint}) | [PH](https://photon.sol) | [Bullx](https://bullx.io) | [GMGN](https://gmgn.ai) | [AXI](https://axiom.trade) | [INFO](https://solscan.io/token/{mint}) | [Pump](https://pump.fun/{mint})

`{mint}`
"""
        await send_alert(msg.strip())

# ==================== CHẠY BOT ====================
app = Application.builder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("add", add_wallet))
app.add_handler(CommandHandler("list", list_wallets))
app.add_handler(CommandHandler("delete", delete_wallet))
app.add_handler(CommandHandler("deleteall", delete_all))

if __name__ == "__main__":
    print("BOT HOÀN HẢO – 💊 SOL | 🔗 EVM – ĐÃ CHẠY!")
    app.run_polling()