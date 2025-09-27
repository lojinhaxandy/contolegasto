# app.py
import os
import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify
import requests
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Dispatcher, CommandHandler

# ----------------- Config -----------------
TELEGRAM_TOKEN = os.getenv("7735147007:AAEN-_lLmBCRIgu4y2PwTWD4qNclDFRgPxY")            # token do bot Telegram
MP_ACCESS_TOKEN = os.getenv("APP_USR-1661690156955161-061015-1277fc50c082df9755ad4a4f043449c3-1294489094")          # Access Token do Mercado Pago (token de integração)
KEEP_MONTHS = int(os.getenv("KEEP_MONTHS", "6"))        # quantos meses manter (padrão 6)
ADMIN_CHAT_ID = os.getenv("8084023622")             # opcional: seu chat_id para alertas

# DB path (sqlite)
DB_PATH = os.getenv("DB_PATH", "data.db")

# Init
app = Flask(__name__)
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(bot, None, workers=0, use_context=True)

# ----------------- DB helpers -----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mp_id TEXT UNIQUE,
        date TEXT,
        amount REAL,
        status TEXT,
        payer_email TEXT,
        raw JSON
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        amount REAL,
        description TEXT
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    )""")
    conn.commit()
    conn.close()

def insert_payment(mp_id, date, amount, status, payer_email, raw):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("INSERT INTO payments (mp_id,date,amount,status,payer_email,raw) VALUES (?,?,?,?,?,?)",
                  (str(mp_id), date, amount, status, payer_email, raw))
        conn.commit()
        inserted = True
    except sqlite3.IntegrityError:
        inserted = False
    conn.close()
    return inserted

def insert_expense(date, amount, description):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO expenses (date,amount,description) VALUES (?,?,?)",
              (date, amount, description))
    conn.commit()
    conn.close()

def sum_payments_for_month(year, month):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    start = f"{year:04d}-{month:02d}-01"
    # naive end: next month
    if month == 12:
        end = f"{year+1:04d}-01-01"
    else:
        end = f"{year:04d}-{month+1:02d}-01"
    c.execute("SELECT SUM(amount) FROM payments WHERE status='approved' AND date >= ? AND date < ?", (start, end))
    res = c.fetchone()[0] or 0.0
    conn.close()
    return res

def sum_expenses_for_month(year, month):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end = f"{year+1:04d}-01-01"
    else:
        end = f"{year:04d}-{month+1:02d}-01"
    c.execute("SELECT SUM(amount) FROM expenses WHERE date >= ? AND date < ?", (start, end))
    res = c.fetchone()[0] or 0.0
    conn.close()
    return res

def cleanup_old_months(keep_months=6):
    # remove payments and expenses older than keep_months
    cutoff = datetime.utcnow().replace(day=1)
    # subtract months
    ym = cutoff.year * 12 + cutoff.month - keep_months
    cutoff_year = (ym - 1) // 12
    cutoff_month = (ym - 1) % 12 + 1
    cutoff_str = f"{cutoff_year:04d}-{cutoff_month:02d}-01"
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM payments WHERE date < ?", (cutoff_str,))
    c.execute("DELETE FROM expenses WHERE date < ?", (cutoff_str,))
    conn.commit()
    conn.close()

# ----------------- Mercado Pago helper -----------------
MP_BASE = "https://api.mercadopago.com"

def fetch_mp_payment(payment_id):
    url = f"{MP_BASE}/v1/payments/{payment_id}"
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    r = requests.get(url, headers=headers, timeout=15)
    if r.status_code == 200:
        return r.json()
    else:
        app.logger.warning("MP fetch failed %s %s", r.status_code, r.text)
        return None

# ----------------- Telegram handlers -----------------
def start(update, context):
    update.message.reply_text(
        "Bot de finanças MP.\nComandos:\n"
        "/addexpense <valor> <descr> - registra gasto\n"
        "/profit <mm> <aaaa> - mostra lucro do mês\n"
        "/balance <mm> <aaaa> - mostra vendas, gastos do mês\n        /lastmonths <n> - mostra lucros últimos n meses (padrão 6)\n"
    )

def addexpense(update, context):
    try:
        args = context.args
        if len(args) < 2:
            update.message.reply_text("Uso: /addexpense 12.50 Descrição do gasto")
            return
        amount = float(args[0].replace(",", "."))
        description = " ".join(args[1:])
        date = datetime.utcnow().strftime("%Y-%m-%d")
        insert_expense(date, amount, description)
        update.message.reply_text(f"Despesa salva: R$ {amount:.2f} — {description}")
    except Exception as e:
        update.message.reply_text("Erro ao salvar despesa: " + str(e))

def profit(update, context):
    try:
        args = context.args
        if len(args) >= 2:
            month = int(args[0])
            year = int(args[1])
        else:
            now = datetime.utcnow()
            month = now.month
            year = now.year
        vendas = sum_payments_for_month(year, month)
        gastos = sum_expenses_for_month(year, month)
        lucro = vendas - gastos
        msg = f"Resumo {month:02d}/{year}\nVendas aprovadas: R$ {vendas:.2f}\nGastos: R$ {gastos:.2f}\nLucro: R$ {lucro:.2f}"
        update.message.reply_text(msg)
    except Exception as e:
        update.message.reply_text("Erro: " + str(e))

def balance(update, context):
    profit(update, context)

def lastmonths(update, context):
    try:
        n = int(context.args[0]) if context.args else KEEP_MONTHS
        now = datetime.utcnow()
        lines = []
        y = now.year
        m = now.month
        for i in range(n):
            vendas = sum_payments_for_month(y, m)
            gastos = sum_expenses_for_month(y, m)
            lucro = vendas - gastos
            lines.append(f"{m:02d}/{y} — V: R${vendas:.2f} G: R${gastos:.2f} L: R${lucro:.2f}")
            # decrement month
            m -= 1
            if m == 0:
                m = 12
                y -= 1
        update.message.reply_text("\n".join(lines))
    except Exception as e:
        update.message.reply_text("Erro: " + str(e))

# Register handlers
dp.add_handler(CommandHandler("start", start))
dp.add_handler(CommandHandler("addexpense", addexpense))
dp.add_handler(CommandHandler("profit", profit))
dp.add_handler(CommandHandler("balance", balance))
dp.add_handler(CommandHandler("lastmonths", lastmonths))

# ----------------- Flask routes -----------------
@app.route("/telegram_webhook", methods=["POST"])
def telegram_webhook():
    # Used if you want Telegram -> webhook to us. We won't rely on it for payments, only for bot commands.
    update = Update.de_json(request.get_json(force=True), bot)
    dp.process_update(update)
    return "OK"

@app.route("/mp_webhook", methods=["POST"])
def mp_webhook():
    """
    Mercado Pago sends a notification with JSON body like:
    {"action":"payment.created","data":{"id":123456},"topic":"payment"}
    Or older format: {"type":"payment","id":"123"}
    We will support both: get id, fetch payment details, store.
    """
    try:
        payload = request.get_json(force=True, silent=True) or {}
        # Work with different formats
        mp_id = None
        if "data" in payload and isinstance(payload["data"], dict) and payload["data"].get("id"):
            mp_id = payload["data"]["id"]
        elif payload.get("id"):
            mp_id = payload.get("id")
        elif payload.get("action") and payload.get("data", {}).get("id"):
            mp_id = payload["data"]["id"]
        else:
            # fallback: check query params
            mp_id = request.args.get("id")

        if not mp_id:
            app.logger.warning("mp_webhook: no id in payload: %s", payload)
            return jsonify({"ok": False, "reason": "no id"}), 400

        payment = fetch_mp_payment(mp_id)
        if not payment:
            return jsonify({"ok": False, "reason": "fetch_failed"}), 500

        amount = 0.0
        # amount can be in 'transaction_amount' or 'transaction_amount' inside array depending on API
        amount = float(payment.get("transaction_amount") or payment.get("transaction_amount_paid") or 0.0)
        status = payment.get("status")  # e.g., approved, refused, pending
        payer_email = None
        if payment.get("payer"):
            payer_email = payment["payer"].get("email")

        date = payment.get("date_created", datetime.utcnow().isoformat())[:10]  # YYYY-MM-DD
        inserted = insert_payment(mp_id, date, amount, status, payer_email, str(payment))
        if inserted:
            app.logger.info("Inserted payment %s amount %s status %s", mp_id, amount, status)
            # optional: notify admin
            if ADMIN_CHAT_ID:
                try:
                    bot.send_message(int(ADMIN_CHAT_ID),
                                     f"Novo pagamento: R$ {amount:.2f} — status: {status} — {date}")
                except Exception as e:
                    app.logger.warning("notify failed %s", e)
        else:
            app.logger.info("Payment %s already exists", mp_id)

        # after storing, do cleanup
        cleanup_old_months(KEEP_MONTHS)
        return jsonify({"ok": True})
    except Exception as e:
        app.logger.exception("mp_webhook error")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/", methods=["GET"])
def index():
    return "Bot de finanças Mercado Pago - OK"

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
