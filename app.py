import os
import json
import sqlite3
from datetime import datetime
from queue import Queue

import requests
from flask import Flask, request, jsonify

from telegram import Bot, Update
from telegram.ext import Dispatcher, CommandHandler

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "").strip()
MP_ACCESS_TOKEN  = os.getenv("MP_ACCESS_TOKEN", "").strip()
ADMIN_CHAT_ID    = os.getenv("ADMIN_CHAT_ID", "").strip()
KEEP_MONTHS      = int(os.getenv("KEEP_MONTHS", "6"))
DB_PATH          = os.getenv("DB_PATH", "data.db")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Falta TELEGRAM_TOKEN no ambiente.")
if not MP_ACCESS_TOKEN:
    print("[AVISO] MP_ACCESS_TOKEN ausente. /mp_webhook não conseguirá buscar dados.")

app = Flask(__name__)

bot = Bot(token=TELEGRAM_TOKEN)
update_queue = Queue()
dispatcher = Dispatcher(bot=bot, update_queue=update_queue, workers=0, use_context=True)

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mp_id TEXT UNIQUE,
        date TEXT,
        amount REAL,
        status TEXT,
        payer_email TEXT,
        raw TEXT
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

def insert_payment(mp_id, date, amount, status, payer_email, raw_as_text):
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("""
            INSERT INTO payments (mp_id, date, amount, status, payer_email, raw)
            VALUES (?,?,?,?,?,?)
        """, (str(mp_id), date, float(amount or 0), status or "", payer_email or "", raw_as_text or ""))
        conn.commit()
        ok = True
    except sqlite3.IntegrityError:
        ok = False
    conn.close()
    return ok

def insert_expense(date, amount, description):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO expenses (date, amount, description) VALUES (?,?,?)",
              (date, float(amount), description))
    conn.commit()
    conn.close()

def month_range(year, month):
    start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end = f"{year+1:04d}-01-01"
    else:
        end = f"{year:04d}-{month+1:02d}-01"
    return start, end

def sum_payments_for_month(year, month):
    start, end = month_range(year, month)
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT SUM(amount) FROM payments WHERE status='approved' AND date >= ? AND date < ?", (start, end))
    total = c.fetchone()[0] or 0.0
    conn.close()
    return float(total)

def sum_expenses_for_month(year, month):
    start, end = month_range(year, month)
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT SUM(amount) FROM expenses WHERE date >= ? AND date < ?", (start, end))
    total = c.fetchone()[0] or 0.0
    conn.close()
    return float(total)

def cleanup_old_months(keep_months=6):
    now = datetime.utcnow().replace(day=1)
    ym = now.year * 12 + now.month - keep_months
    cutoff_year = (ym - 1) // 12
    cutoff_month = (ym - 1) % 12 + 1
    cutoff_str = f"{cutoff_year:04d}-{cutoff_month:02d}-01"
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM payments WHERE date < ?", (cutoff_str,))
    c.execute("DELETE FROM expenses WHERE date < ?", (cutoff_str,))
    conn.commit()
    conn.close()

MP_BASE = "https://api.mercadopago.com"

def fetch_mp_payment(payment_id):
    if not MP_ACCESS_TOKEN:
        return None
    url = f"{MP_BASE}/v1/payments/{payment_id}"
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    try:
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code == 200:
            return r.json()
        else:
            print(f"[MP] Falha {r.status_code}: {r.text}")
            return None
    except Exception as e:
        print(f"[MP] Erro fetch: {e}")
        return None

def cmd_start(update, context):
    update.message.reply_text(
        "🤖 Bot de Controle de Vendas/Despesas (Mercado Pago)\n\n"
        "Comandos:\n"
        "• /addexpense <valor> <descrição> — registrar gasto\n"
        "• /profit [mm aaaa] — lucro do mês (padrão: mês atual)\n"
        "• /balance [mm aaaa] — vendas/gastos/lucro do mês\n"
        "• /lastmonths [n] — resumo dos últimos n meses (padrão: 6)\n"
    )

def cmd_addexpense(update, context):
    try:
        args = context.args
        if len(args) < 2:
            update.message.reply_text("Uso: /addexpense 12.50 Descrição do gasto")
            return
        amount = float(args[0].replace(",", "."))
        description = " ".join(args[1:])
        date = datetime.utcnow().strftime("%Y-%m-%d")
        insert_expense(date, amount, description)
        update.message.reply_text(f"✅ Despesa salva: R$ {amount:.2f} — {description}")
    except Exception as e:
        update.message.reply_text(f"❌ Erro ao salvar despesa: {e}")

def _parse_month_year(args):
    if len(args) >= 2:
        month = int(args[0])
        year = int(args[1])
    else:
        now = datetime.utcnow()
        month = now.month
        year = now.year
    if not (1 <= month <= 12):
        raise ValueError("Mês inválido. Use 1-12.")
    return month, year

def cmd_profit(update, context):
    try:
        month, year = _parse_month_year(context.args)
        vendas = sum_payments_for_month(year, month)
        gastos = sum_expenses_for_month(year, month)
        lucro  = vendas - gastos
        msg = (
            f"📊 Resumo {month:02d}/{year}\n"
            f"• Vendas aprovadas: R$ {vendas:.2f}\n"
            f"• Gastos:           R$ {gastos:.2f}\n"
            f"• 💰 Lucro:          R$ {lucro:.2f}"
        )
        update.message.reply_text(msg)
    except Exception as e:
        update.message.reply_text(f"❌ Erro: {e}")

def cmd_balance(update, context):
    cmd_profit(update, context)

def cmd_lastmonths(update, context):
    try:
        n = int(context.args[0]) if context.args else KEEP_MONTHS
        if n < 1:
            n = KEEP_MONTHS
        now = datetime.utcnow()
        y = now.year
        m = now.month
        lines = []
        for _ in range(n):
            vendas = sum_payments_for_month(y, m)
            gastos = sum_expenses_for_month(y, m)
            lucro  = vendas - gastos
            lines.append(f"{m:02d}/{y} — V: R${vendas:.2f}  G: R${gastos:.2f}  L: R${lucro:.2f}")
            m -= 1
            if m == 0:
                m = 12
                y -= 1
        update.message.reply_text("📆 Últimos meses:\n" + "\n".join(lines))
    except Exception as e:
        update.message.reply_text(f"❌ Erro: {e}")

dispatcher.add_handler(CommandHandler("start",      cmd_start))
dispatcher.add_handler(CommandHandler("addexpense", cmd_addexpense))
dispatcher.add_handler(CommandHandler("profit",     cmd_profit))
dispatcher.add_handler(CommandHandler("balance",    cmd_balance))
dispatcher.add_handler(CommandHandler("lastmonths", cmd_lastmonths))

app = Flask(__name__)

@app.route("/", methods=["GET"])
def index():
    return "OK - Bot de finanças Mercado Pago"

@app.route("/telegram_webhook", methods=["POST"])
def telegram_webhook():
    try:
        data = request.get_json(force=True)
        update = Update.de_json(data, bot)
        dispatcher.process_update(update)
        return "OK"
    except Exception as e:
        print(f"[TG] Erro no webhook: {e}")
        return "ERR", 500

@app.route("/set_webhook", methods=["POST", "GET"])
def set_webhook():
    secret = request.args.get("secret", "")
    if not ADMIN_CHAT_ID or secret != (ADMIN_CHAT_ID[-6:] if len(ADMIN_CHAT_ID) >= 6 else "ok"):
        return "unauthorized", 401
    url = request.url_root.rstrip("/") + "/telegram_webhook"
    ok = bot.set_webhook(url=url, allowed_updates=["message"], max_connections=40)
    return jsonify({"set_webhook": ok, "url": url})

@app.route("/mp_webhook", methods=["POST"])
def mp_webhook():
    try:
        payload = request.get_json(force=True, silent=True) or {}
        mp_id = None
        if isinstance(payload.get("data"), dict) and payload["data"].get("id"):
            mp_id = payload["data"]["id"]
        elif payload.get("id"):
            mp_id = payload["id"]
        if not mp_id:
            mp_id = request.args.get("id")
        if not mp_id:
            return jsonify({"ok": False, "reason": "no_id"}), 400

        payment = fetch_mp_payment(mp_id)
        if not payment:
            return jsonify({"ok": False, "reason": "fetch_failed"}), 500

        amount = float(payment.get("transaction_amount") or payment.get("transaction_amount_paid") or 0.0)
        status = payment.get("status") or ""
        payer_email = ""
        if isinstance(payment.get("payer"), dict):
            payer_email = payment["payer"].get("email") or ""

        date = (payment.get("date_created") or datetime.utcnow().isoformat())[:10]

        inserted = insert_payment(mp_id, date, amount, status, payer_email, json.dumps(payment)[:200000])
        if inserted:
            print(f"[MP] Inserido {mp_id} R${amount:.2f} status={status}")
            if ADMIN_CHAT_ID:
                try:
                    bot.send_message(chat_id=int(ADMIN_CHAT_ID),
                                     text=f"🧾 Pagamento MP #{mp_id}\nR$ {amount:.2f} — {status} — {date}")
                except Exception as e:
                    print(f"[TG] Falha ao notificar ADMIN: {e}")
        else:
            print(f"[MP] Já existia {mp_id}")

        cleanup_old_months(KEEP_MONTHS)
        return jsonify({"ok": True})
    except Exception as e:
        print(f"[MP] Erro webhook: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
