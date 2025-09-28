import os
import re
import json
import sqlite3
import logging
from datetime import datetime
from queue import Queue

from flask import Flask, request, jsonify

from telegram import Bot, Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Dispatcher, CommandHandler, MessageHandler, Filters

# =========================
# ======= LOGGING =========
# =========================
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("contolegasto")

# =========================
# ======= CONFIG ==========
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
ADMIN_CHAT_ID  = os.getenv("ADMIN_CHAT_ID", "").strip()  # opcional: protege rotas utilitárias
KEEP_MONTHS    = int(os.getenv("KEEP_MONTHS", "6"))
DB_PATH        = os.getenv("DB_PATH", "data.db")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Falta TELEGRAM_TOKEN no ambiente.")

# =========================
# ======= APP/TG ==========
# =========================
app = Flask(__name__)

bot = Bot(token=TELEGRAM_TOKEN)
update_queue = Queue()
dispatcher = Dispatcher(bot=bot, update_queue=update_queue, workers=0, use_context=True)

# =========================
# ======= DB ==============
# =========================
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    """Cria as tabelas se não existirem (idempotente)."""
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        amount REAL,
        raw TEXT,
        created_at TEXT,
        source TEXT,
        user_code TEXT,
        referrer_code TEXT
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        amount REAL,
        description TEXT
    )""")
    conn.commit()
    conn.close()

def insert_payment_manual(date_yyyy_mm_dd, amount, created_at, user_code=None, referrer_code=None, raw_text=None):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO payments (date, amount, raw, created_at, source, user_code, referrer_code)
        VALUES (?,?,?,?,?,?,?)
    """, (date_yyyy_mm_dd, float(amount), (raw_text or "")[:200000], created_at or "", "manual_text", user_code or "", referrer_code or ""))
    conn.commit()
    conn.close()

def insert_expense(date, amount, description):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO expenses (date,amount,description) VALUES (?,?,?)", (date, float(amount), description))
    conn.commit()
    conn.close()

def month_range(year, month):
    start = f"{year:04d}-{month:02d}-01"
    end = f"{(year + (month==12)) :04d}-{(1 if month==12 else month+1):02d}-01"
    return start, end

def sum_payments_for_month(year, month):
    start, end = month_range(year, month)
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT SUM(amount) FROM payments WHERE date >= ? AND date < ?", (start, end))
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

# ===== init DB no import (funciona com gunicorn) =====
try:
    init_db()
    log.info("[DB] Tabelas verificadas/criadas")
except Exception as e:
    log.exception("[DB] Falha ao iniciar DB: %s", e)

# =========================
# ======= PARSER ==========
# =========================
BLOCK_SPLIT_RE = re.compile(r"(?:^|\n)\s*[\U0001F4B0💰]\s*Novo\s+DEP[ÓO]SITO\b", re.IGNORECASE)
USER_RE        = re.compile(r"User:\s*([0-9]+)")
VALOR_RE       = re.compile(r"Valor:\s*R\$\s*([0-9]+[.,][0-9]{2})", re.IGNORECASE)
DATA_RE        = re.compile(r"Data:\s*([0-9]{2}/[0-9]{2}/[0-9]{4})(?:\s+([0-9]{2}:[0-9]{2}:[0-9]{2}))?")
REF_RE         = re.compile(r"Indicado por:\s*([0-9]+)", re.IGNORECASE)

def to_decimal(s):
    s = s.strip()
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    return float(s)

def parse_date(dmy, hms):
    try:
        dd = datetime.strptime(dmy, "%d/%m/%Y")
        return dd.strftime("%Y-%m-%d"), hms or ""
    except Exception:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        return today, hms or ""

def extract_deposits_from_text(text):
    if not text:
        return []
    text = text.replace("\u00A0", " ").strip()

    deposits = []
    parts = BLOCK_SPLIT_RE.split(text)
    for chunk in parts:
        if not chunk:
            continue
        m_val = VALOR_RE.search(chunk)
        if not m_val:
            continue
        amount = to_decimal(m_val.group(1))

        m_user = USER_RE.search(chunk)
        user_code = m_user.group(1) if m_user else ""

        m_data = DATA_RE.search(chunk)
        if m_data:
            date_ymd, _hms = parse_date(m_data.group(1), m_data.group(2))
            created_at = f"{m_data.group(1)} {m_data.group(2) or ''}".strip()
        else:
            date_ymd = datetime.utcnow().strftime("%Y-%m-%d")
            created_at = ""

        m_ref = REF_RE.search(chunk)
        referrer_code = m_ref.group(1) if m_ref else ""

        deposits.append({
            "amount": amount,
            "date_ymd": date_ymd,
            "created_at": created_at,
            "user_code": user_code,
            "referrer_code": referrer_code,
            "raw": chunk.strip()
        })

    if not deposits:
        m_val = VALOR_RE.search(text)
        if m_val:
            amount = to_decimal(m_val.group(1))
            m_user = USER_RE.search(text)
            user_code = m_user.group(1) if m_user else ""
            m_data = DATA_RE.search(text)
            if m_data:
                date_ymd, _hms = parse_date(m_data.group(1), m_data.group(2))
                created_at = f"{m_data.group(1)} {m_data.group(2) or ''}".strip()
            else:
                date_ymd = datetime.utcnow().strftime("%Y-%m-%d")
                created_at = ""
            m_ref = REF_RE.search(text)
            referrer_code = m_ref.group(1) if m_ref else ""
            deposits.append({
                "amount": amount,
                "date_ymd": date_ymd,
                "created_at": created_at,
                "user_code": user_code,
                "referrer_code": referrer_code,
                "raw": text.strip()
            })

    return deposits

# =========================
# ======= MENU ============
# =========================
BTN_PROFIT     = "📊 Lucro do mês"
BTN_LASTMONTHS = "📆 Últimos meses"
BTN_ADD_EXP    = "➕ Registrar gasto"
BTN_HELP       = "ℹ️ Ajuda"

def main_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(BTN_PROFIT), KeyboardButton(BTN_LASTMONTHS)],
            [KeyboardButton(BTN_ADD_EXP), KeyboardButton(BTN_HELP)],
        ],
        resize_keyboard=True
    )

def send_menu(chat_id, intro_text=None):
    text = intro_text or (
        "🤖 *Controle de Vendas & Despesas*\n"
        "Encaminhe mensagens de *Novo DEPÓSITO* para registrar vendas (bônus é ignorado).\n\n"
        "Escolha uma opção:"
    )
    bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="Markdown",
        reply_markup=main_menu()
    )

# =========================
# ===== COMANDOS TG =======
# =========================
def cmd_start(update, context):
    send_menu(update.effective_chat.id, "👋 *Bem-vindo!*")

def cmd_test(update, context):
    update.message.reply_text("✅ Bot online e webhook OK.", reply_markup=main_menu())

def cmd_me(update, context):
    update.message.reply_text(f"Seu chat_id: `{update.effective_chat.id}`", parse_mode="Markdown", reply_markup=main_menu())

def cmd_addexpense(update, context):
    try:
        args = context.args
        if len(args) < 2:
            update.message.reply_text(
                "Uso: `/addexpense 12.50 Descrição do gasto`\n"
                "_Ex.:_ `/addexpense 8.90 Taxa da plataforma`",
                parse_mode="Markdown",
                reply_markup=main_menu()
            )
            return
        amount = float(args[0].replace(",", "."))
        desc = " ".join(args[1:])
        date = datetime.utcnow().strftime("%Y-%m-%d")
        insert_expense(date, amount, desc)
        update.message.reply_text(f"✅ *Despesa salva*\n• Valor: R$ {amount:.2f}\n• Desc.: {desc}", parse_mode="Markdown", reply_markup=main_menu())
    except Exception as e:
        update.message.reply_text(f"❌ Erro: {e}", reply_markup=main_menu())

def _parse_month_year(args):
    if len(args) >= 2:
        m = int(args[0]); y = int(args[1])
    else:
        now = datetime.utcnow(); m = now.month; y = now.year
    if not (1 <= m <= 12): raise ValueError("Mês inválido 1-12")
    return m, y

def _profit_text(m, y):
    vendas = sum_payments_for_month(y, m)
    gastos = sum_expenses_for_month(y, m)
    lucro  = vendas - gastos
    return (
        f"📊 *Resumo {m:02d}/{y}*\n"
        f"• Vendas: R$ {vendas:.2f}\n"
        f"• Gastos: R$ {gastos:.2f}\n"
        f"• 💰 Lucro: *R$ {lucro:.2f}*"
    )

def cmd_profit(update, context):
    try:
        m, y = _parse_month_year(context.args)
        update.message.reply_text(_profit_text(m, y), parse_mode="Markdown", reply_markup=main_menu())
    except Exception as e:
        update.message.reply_text(f"❌ Erro: {e}", reply_markup=main_menu())

def cmd_lastmonths(update, context):
    try:
        n = int(context.args[0]) if context.args else KEEP_MONTHS
        if n < 1: n = KEEP_MONTHS
        now = datetime.utcnow(); y = now.year; m = now.month
        lines = []
        for _ in range(n):
            v = sum_payments_for_month(y, m)
            g = sum_expenses_for_month(y, m)
            l = v - g
            lines.append(f"{m:02d}/{y} — *V*: R${v:.2f}  *G*: R${g:.2f}  *L*: R${l:.2f}")
            m -= 1
            if m == 0: m = 12; y -= 1
        update.message.reply_text("📆 *Últimos meses*\n" + "\n".join(lines), parse_mode="Markdown", reply_markup=main_menu())
    except Exception as e:
        update.message.reply_text(f"❌ Erro: {e}", reply_markup=main_menu())

# =========================
# ===== ATALHOS BOTÕES ====
# =========================
def handle_buttons(update, context, text):
    text = text.strip()
    if text == BTN_PROFIT:
        now = datetime.utcnow(); m = now.month; y = now.year
        update.message.reply_text(_profit_text(m, y), parse_mode="Markdown", reply_markup=main_menu())
        return True
    if text == BTN_LASTMONTHS:
        # usa default KEEP_MONTHS
        now = datetime.utcnow(); y = now.year; m = now.month
        lines = []
        for _ in range(KEEP_MONTHS):
            v = sum_payments_for_month(y, m)
            g = sum_expenses_for_month(y, m)
            l = v - g
            lines.append(f"{m:02d}/{y} — *V*: R${v:.2f}  *G*: R${g:.2f}  *L*: R${l:.2f}")
            m -= 1
            if m == 0: m = 12; y -= 1
        update.message.reply_text("📆 *Últimos meses*\n" + "\n".join(lines), parse_mode="Markdown", reply_markup=main_menu())
        return True
    if text == BTN_ADD_EXP:
        update.message.reply_text(
            "➕ *Registrar gasto*\n"
            "Envie no formato: `/addexpense 12.50 Descrição`\n"
            "_Ex.:_ `/addexpense 8.90 Taxa da plataforma`",
            parse_mode="Markdown",
            reply_markup=main_menu()
        )
        return True
    if text == BTN_HELP:
        cmd_start(update, context)
        return True
    return False

# =========================
# ===== HANDLER TEXTO =====
# =========================
def handle_text(update, context):
    try:
        text = (update.message.text or "").strip()
        if not text:
            send_menu(update.effective_chat.id)
            return

        # 1) Se for um botão do menu, atende e retorna
        if handle_buttons(update, context, text):
            return

        # 2) Tenta extrair depósitos do texto
        deposits = extract_deposits_from_text(text)
        if deposits:
            total = 0.0
            for d in deposits:
                insert_payment_manual(
                    date_yyyy_mm_dd=d["date_ymd"],
                    amount=d["amount"],
                    created_at=d["created_at"],
                    user_code=d["user_code"],
                    referrer_code=d["referrer_code"],
                    raw_text=d["raw"]
                )
                total += d["amount"]

            update.message.reply_text(
                f"✅ *Depósito(s) registrado(s)*\n"
                f"• Quantidade: {len(deposits)}\n"
                f"• Soma: *R$ {total:.2f}*\n\n"
                f"Use /profit para ver o lucro do mês.",
                parse_mode="Markdown",
                reply_markup=main_menu()
            )
            return

        # 3) Caso não seja depósito nem botão, apenas mostre o menu
        send_menu(update.effective_chat.id)

    except Exception as e:
        log.exception("Erro ao processar texto")
        update.message.reply_text(f"❌ Erro: {e}", reply_markup=main_menu())

# =========================
# ===== REGISTRAR HND =====
# =========================
dispatcher.add_handler(CommandHandler("start",      cmd_start))
dispatcher.add_handler(CommandHandler("test",       cmd_test))
dispatcher.add_handler(CommandHandler("me",         cmd_me))
dispatcher.add_handler(CommandHandler("addexpense", cmd_addexpense))
dispatcher.add_handler(CommandHandler("profit",     cmd_profit))
dispatcher.add_handler(CommandHandler("lastmonths", cmd_lastmonths))
dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))

# =========================
# ====== ROTAS FLASK ======
# =========================
@app.route("/", methods=["GET"])
def index():
    return "OK - Bot de finanças manual (depósitos por texto) + menu"

@app.route("/telegram_webhook", methods=["POST"])
def telegram_webhook():
    try:
        data = request.get_json(force=True, silent=True)
        log.info("[TG] Update: %s", data)
        if not data:
            return "EMPTY", 400
        update = Update.de_json(data, bot)
        dispatcher.process_update(update)
        return "OK"
    except Exception as e:
        log.exception("Erro no webhook: %s", e)
        return "ERR", 500

# utilidades
@app.route("/tg_set_webhook", methods=["GET"])
def tg_set_webhook():
    if not ADMIN_CHAT_ID:
        return "ADMIN_CHAT_ID não configurado", 400
    key = request.args.get("key", "")
    guard = ADMIN_CHAT_ID[-6:] if len(ADMIN_CHAT_ID) >= 6 else ADMIN_CHAT_ID
    if key != guard:
        return "unauthorized", 401
    url = request.url_root.rstrip("/") + "/telegram_webhook"
    ok = bot.set_webhook(url=url, allowed_updates=["message"], max_connections=40)
    info = bot.get_webhook_info()
    return jsonify({"set_webhook": ok, "webhook_info": info.to_dict(), "url": url})

@app.route("/tg_webhook_info", methods=["GET"])
def tg_webhook_info():
    try:
        info = bot.get_webhook_info()
        return jsonify(info.to_dict())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/db_status", methods=["GET"])
def db_status():
    try:
        conn = get_conn(); c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in c.fetchall()]
        counts = {}
        for t in tables:
            try:
                c.execute(f"SELECT COUNT(*) FROM {t}")
                counts[t] = c.fetchone()[0]
            except Exception:
                counts[t] = "n/a"
        conn.close()
        return jsonify({"db_path": DB_PATH, "tables": tables, "counts": counts})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/db_init", methods=["GET"])
def db_init():
    if not ADMIN_CHAT_ID:
        return "ADMIN_CHAT_ID não configurado", 400
    key = request.args.get("key", "")
    guard = ADMIN_CHAT_ID[-6:] if len(ADMIN_CHAT_ID) >= 6 else ADMIN_CHAT_ID
    if key != guard:
        return "unauthorized", 401
    try:
        init_db()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# =========================
# ======= MAIN ============
# =========================
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
