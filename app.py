import os
import re
import io
import csv
import json
import sqlite3
import logging
from datetime import datetime, timedelta
from queue import Queue
from typing import List, Tuple

from flask import Flask, request, jsonify, Response

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
ADMIN_CHAT_ID  = os.getenv("ADMIN_CHAT_ID", "").strip()  # opcional (recomendado)
KEEP_MONTHS    = int(os.getenv("KEEP_MONTHS", "6"))
DB_PATH        = os.getenv("DB_PATH", "data.db")
PAGE_SIZE      = int(os.getenv("PAGE_SIZE", "10"))

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
    """Cria/atualiza as tabelas (idempotente)."""
    conn = get_conn()
    c = conn.cursor()
    # receitas (depósitos)
    c.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,            -- YYYY-MM-DD (data do depósito)
        amount REAL,          -- valor do depósito (sem bônus)
        raw TEXT,             -- texto bruto salvo
        created_at TEXT,      -- data/hora informada no texto (string)
        source TEXT,          -- 'manual_text' ou 'channel'
        user_code TEXT,       -- ex: 1039020435
        referrer_code TEXT,   -- se houver 'Indicado por'
        inserted_at TEXT      -- instante do insert UTC ISO
    )""")
    # despesas
    c.execute("""
    CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        amount REAL,
        description TEXT,
        inserted_at TEXT      -- instante do insert UTC ISO
    )""")
    conn.commit()
    # garantir colunas novas (migrations simples)
    for alter in [
        "ALTER TABLE payments ADD COLUMN inserted_at TEXT",
        "ALTER TABLE expenses ADD COLUMN inserted_at TEXT",
    ]:
        try:
            c.execute(alter)
            conn.commit()
        except Exception:
            pass
    conn.close()

def now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat()

def insert_payment_manual(date_yyyy_mm_dd, amount, created_at, user_code=None, referrer_code=None, raw_text=None, source="manual_text"):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO payments (date, amount, raw, created_at, source, user_code, referrer_code, inserted_at)
        VALUES (?,?,?,?,?,?,?,?)
    """, (date_yyyy_mm_dd, float(amount), (raw_text or "")[:200000], created_at or "", source, user_code or "", referrer_code or "", now_iso()))
    conn.commit()
    conn.close()

def insert_expense(date, amount, description):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT INTO expenses (date,amount,description,inserted_at) VALUES (?,?,?,?)",
              (date, float(amount), description, now_iso()))
    conn.commit()
    conn.close()

def month_range(year, month):
    start = f"{year:04d}-{month:02d}-01"
    end = f"{(year + (month==12)) :04d}-{(1 if month==12 else month+1):02d}-01"
    return start, end

def sum_payments_for_month(year, month):
    start, end = month_range(year, month)
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT SUM(amount) FROM payments WHERE date >= ? AND date < ?", (start, end))
    total = c.fetchone()[0] or 0.0
    conn.close()
    return float(total)

def sum_expenses_for_month(year, month):
    start, end = month_range(year, month)
    conn = get_conn(); c = conn.cursor()
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
    conn = get_conn(); c = conn.cursor()
    c.execute("DELETE FROM payments WHERE date < ?", (cutoff_str,))
    c.execute("DELETE FROM expenses WHERE date < ?", (cutoff_str,))
    conn.commit(); conn.close()

def fetch_entries_for_month(year, month) -> Tuple[List[tuple], List[tuple]]:
    """Retorna (payments, expenses) do mês."""
    start, end = month_range(year, month)
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT id,date,amount,raw,created_at,source,user_code,referrer_code,inserted_at "
              "FROM payments WHERE date>=? AND date<? ORDER BY date, inserted_at", (start, end))
    payments = c.fetchall()
    c.execute("SELECT id,date,amount,description,inserted_at "
              "FROM expenses WHERE date>=? AND date<? ORDER BY date, inserted_at", (start, end))
    expenses = c.fetchall()
    conn.close()
    return payments, expenses

def fetch_entries_between(start_date, end_date) -> Tuple[List[tuple], List[tuple]]:
    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT id,date,amount,raw,created_at,source,user_code,referrer_code,inserted_at "
              "FROM payments WHERE date>=? AND date<? ORDER BY date, inserted_at", (start_date, end_date))
    payments = c.fetchall()
    c.execute("SELECT id,date,amount,description,inserted_at "
              "FROM expenses WHERE date>=? AND date<? ORDER BY date, inserted_at", (start_date, end_date))
    expenses = c.fetchall()
    conn.close()
    return payments, expenses

def undo_last_entry() -> Tuple[str, int]:
    """Apaga o último registro (payment ou expense) pelo inserted_at mais recente."""
    conn = get_conn(); c = conn.cursor()
    # pega últimas timestamps
    c.execute("SELECT 'payments' as t, id, inserted_at FROM payments ORDER BY inserted_at DESC LIMIT 1")
    p = c.fetchone()
    c.execute("SELECT 'expenses' as t, id, inserted_at FROM expenses ORDER BY inserted_at DESC LIMIT 1")
    e = c.fetchone()
    target = None
    if p and e:
        target = p if (p[2] >= e[2]) else e
    else:
        target = p or e
    if not target:
        conn.close()
        return ("none", 0)
    table, row_id = target[0], target[1]
    c.execute(f"DELETE FROM {table} WHERE id=?", (row_id,))
    conn.commit(); conn.close()
    return (table, row_id)

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

def extract_deposits_from_text(text, source="manual_text"):
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
            "raw": chunk.strip(),
            "source": source
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
                "raw": text.strip(),
                "source": source
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
        "Encaminhe mensagens de *Novo DEPÓSITO* (de canal ou chat) para registrar vendas — bônus é ignorado.\n\n"
        "Escolha uma opção:"
    )
    bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=main_menu())

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
        update.message.reply_text(f"✅ *Despesa salva*\n• Valor: R$ {amount:.2f}\n• Desc.: {desc}",
                                  parse_mode="Markdown", reply_markup=main_menu())
    except Exception as e:
        update.message.reply_text(f"❌ Erro: {e}", reply_markup=main_menu())

def _parse_month_year(args):
    if len(args) >= 2 and args[0].isdigit() and args[1].isdigit():
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
        n = int(context.args[0]) if (context.args and context.args[0].isdigit()) else KEEP_MONTHS
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
        update.message.reply_text("📆 *Últimos meses*\n" + "\n".join(lines),
                                  parse_mode="Markdown", reply_markup=main_menu())
    except Exception as e:
        update.message.reply_text(f"❌ Erro: {e}", reply_markup=main_menu())

def cmd_undo(update, context):
    table, row_id = undo_last_entry()
    if table == "none":
        update.message.reply_text("⚠️ Nada para desfazer.", reply_markup=main_menu())
    else:
        nome = "depósito" if table == "payments" else "despesa"
        update.message.reply_text(f"↩️ Desfeito: último {nome} (id {row_id}).", reply_markup=main_menu())

def _date_from_mm_yyyy(m: int, y: int) -> str:
    return f"{y:04d}-{m:02d}-01"

def _month_after(m: int, y: int) -> Tuple[int,int]:
    return (1, y+1) if m == 12 else (m+1, y)

def _range_from_args(args: List[str]) -> Tuple[str, str, str]:
    """
    Constrói [start_date, end_date, label] a partir de argumentos:
    - ""                 -> mês atual
    - "mm aaaa"          -> mês específico
    - "mm aaaa mm aaaa"  -> intervalo [m1/y1, m2/y2] inclusivo
    """
    now = datetime.utcnow()
    if len(args) >= 4 and all(a.isdigit() for a in args[:4]):
        m1, y1, m2, y2 = map(int, args[:4])
        start = _date_from_mm_yyyy(m1, y1)
        m2n, y2n = _month_after(m2, y2)
        end = _date_from_mm_yyyy(m2n, y2n)
        return start, end, f"{m1:02d}/{y1}–{m2:02d}/{y2}"
    elif len(args) >= 2 and all(a.isdigit() for a in args[:2]):
        m, y = map(int, args[:2])
        start = _date_from_mm_yyyy(m, y)
        mn, yn = _month_after(m, y)
        end = _date_from_mm_yyyy(mn, yn)
        return start, end, f"{m:02d}/{y}"
    else:
        m, y = now.month, now.year
        start = _date_from_mm_yyyy(m, y)
        mn, yn = _month_after(m, y)
        end = _date_from_mm_yyyy(mn, yn)
        return start, end, f"{m:02d}/{y}"

def _csv_rows_for_range(start_date: str, end_date: str):
    pays, exps = fetch_entries_between(start_date, end_date)
    rows = [("type","date","amount","description_or_raw","user_code","referrer_code","created_at","inserted_at")]
    for p in pays:
        _id, date, amount, raw, created_at, source, user_code, referrer_code, inserted_at = p
        rows.append(("payment", date, f"{amount:.2f}", raw, user_code, referrer_code, created_at, inserted_at))
    for e in exps:
        _id, date, amount, desc, inserted_at = e
        rows.append(("expense", date, f"{amount:.2f}", desc, "", "", "", inserted_at))
    return rows

def cmd_exportcsv(update, context):
    try:
        start_date, end_date, label = _range_from_args(context.args)
        rows = _csv_rows_for_range(start_date, end_date)

        # cria CSV em memória
        buf = io.StringIO()
        w = csv.writer(buf)
        for r in rows:
            w.writerow(r)
        data = buf.getvalue().encode("utf-8")
        buf.close()

        # envia como arquivo
        file_name = f"lancamentos_{label.replace('/','-')}.csv"
        bot.send_document(
            chat_id=update.effective_chat.id,
            document=("lancamentos.csv", data, "text/csv"),
            filename=file_name,
            caption=f"📄 CSV do período {label}"
        )
    except Exception as e:
        update.message.reply_text(f"❌ Erro no export: {e}", reply_markup=main_menu())

def cmd_list(update, context):
    try:
        args = context.args[:]
        page = 1
        # três formatos: [page] | [mm aaaa] [page?] | []
        if len(args) == 1 and args[0].isdigit():
            # só página
            page = int(args[0]); args = []
        elif len(args) == 3 and args[0].isdigit() and args[1].isdigit() and args[2].isdigit():
            page = int(args[2]); args = args[:2]

        start_date, end_date, label = _range_from_args(args)
        pays, exps = fetch_entries_between(start_date, end_date)

        entries = []
        for p in pays:
            _id,date,amount,raw,created_at,source,user_code,referrer_code,inserted_at = p
            entries.append(("P", _id, date, amount, (user_code or ""), (created_at or ""), inserted_at))
        for e in exps:
            _id,date,amount,desc,inserted_at = e
            entries.append(("E", _id, date, amount, (desc or ""), "", inserted_at))
        # ordena por inserted_at desc (mais recente primeiro)
        entries.sort(key=lambda x: x[-1], reverse=True)

        total_pages = max(1, (len(entries) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = max(1, min(page, total_pages))
        start_idx = (page - 1) * PAGE_SIZE
        page_entries = entries[start_idx:start_idx + PAGE_SIZE]

        if not page_entries:
            update.message.reply_text(f"⚠️ Sem lançamentos em {label}.", reply_markup=main_menu())
            return

        lines = [f"🗂 *Lançamentos {label}* — página {page}/{total_pages}"]
        for t, _id, date, amount, aux, created, ins in page_entries:
            if t == "P":
                lines.append(f"• [#{_id}] DEP — {date} — R${amount:.2f} — user:{aux} — {created}")
            else:
                lines.append(f"• [#{_id}] DES — {date} — R${amount:.2f} — {aux}")
        lines.append("\nDica: `/list 2` ou `/list 09 2025 3`")
        update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=main_menu())
    except Exception as e:
        update.message.reply_text(f"❌ Erro no list: {e}", reply_markup=main_menu())

# =========================
# ===== ATALHOS BOTÕES ====
# =========================
BTN_PROFIT     = "📊 Lucro do mês"
BTN_LASTMONTHS = "📆 Últimos meses"
BTN_ADD_EXP    = "➕ Registrar gasto"
BTN_HELP       = "ℹ️ Ajuda"

def handle_buttons(update, context, text):
    text = text.strip()
    if text == BTN_PROFIT:
        now = datetime.utcnow(); m = now.month; y = now.year
        update.message.reply_text(_profit_text(m, y), parse_mode="Markdown", reply_markup=main_menu())
        return True
    if text == BTN_LASTMONTHS:
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
            "➕ *Registrar gasto*\nEnvie: `/addexpense 12.50 Descrição`",
            parse_mode="Markdown", reply_markup=main_menu()
        )
        return True
    if text == BTN_HELP:
        cmd_start(update, context)
        return True
    return False

# =========================
# ===== HANDLERS TEXTO ====
# =========================
def _process_text_and_reply(chat_id, text, source="manual_text", reply=True, channel_title=None):
    deposits = extract_deposits_from_text(text, source=source)
    if deposits:
        total = 0.0
        for d in deposits:
            insert_payment_manual(
                date_yyyy_mm_dd=d["date_ymd"],
                amount=d["amount"],
                created_at=d["created_at"],
                user_code=d["user_code"],
                referrer_code=d["referrer_code"],
                raw_text=d["raw"],
                source=source
            )
            total += d["amount"]
        if reply:
            bot.send_message(
                chat_id=chat_id,
                text=(f"✅ *Depósito(s) registrado(s)*\n"
                      f"• Quantidade: {len(deposits)}\n"
                      f"• Soma: *R$ {total:.2f}*\n\nUse /profit para ver o mês."),
                parse_mode="Markdown",
                reply_markup=main_menu()
            )
        else:
            # aviso opcional para admin
            if ADMIN_CHAT_ID:
                bot.send_message(
                    chat_id=int(ADMIN_CHAT_ID),
                    text=(f"📥 Registrei {len(deposits)} dep. (R$ {total:.2f}) "
                          f"recebidos do canal: {channel_title or '—'}")
                )
        return True
    return False

def handle_text(update, context):
    try:
        # Blindagem: só processa se houver mensagem de texto (chat privado/grupo)
        em = update.effective_message
        if not em or not getattr(em, "text", None):
            if update.effective_chat:
                send_menu(update.effective_chat.id)
            return

        text = em.text.strip()

        # Botões
        if handle_buttons(update, context, text):
            return

        # Tenta registrar depósitos a partir do texto
        if _process_text_and_reply(update.effective_chat.id, text, source="manual_text", reply=True):
            return

        # Se não era depósito nem botão, mostra menu
        send_menu(update.effective_chat.id)

    except Exception as e:
        log.exception("Erro ao processar texto")
        try:
            if update.effective_chat:
                bot.send_message(update.effective_chat.id, f"❌ Erro: {e}", reply_markup=main_menu())
        except:
            pass

def handle_channel_post(update, context):
    try:
        post = update.channel_post
        if not post or not (post.text or post.caption):
            return
        text = (post.text or post.caption or "").strip()
        # processa silenciosamente e avisa ADMIN
        _process_text_and_reply(
            chat_id=post.chat_id,  # não respondemos no canal; apenas registra e notifica ADMIN (se configurado)
            text=text,
            source="channel",
            reply=False,
            channel_title=getattr(post.chat, "title", None)
        )
    except Exception as e:
        log.exception("Erro canal")

# =========================
# ===== REGISTRAR HND =====
# =========================
dispatcher.add_handler(CommandHandler("start",       cmd_start))
dispatcher.add_handler(CommandHandler("test",        cmd_test))
dispatcher.add_handler(CommandHandler("me",          cmd_me))
dispatcher.add_handler(CommandHandler("addexpense",  cmd_addexpense))
dispatcher.add_handler(CommandHandler("profit",      cmd_profit))
dispatcher.add_handler(CommandHandler("lastmonths",  cmd_lastmonths))
dispatcher.add_handler(CommandHandler("exportcsv",   cmd_exportcsv))
dispatcher.add_handler(CommandHandler("list",        cmd_list))
dispatcher.add_handler(CommandHandler("undo",        cmd_undo))

# mensagens de chat privado/grupo (texto, não comando, e NÃO canal)
dispatcher.add_handler(
    MessageHandler(Filters.text & ~Filters.command & ~Filters.chat_type.channel, handle_text)
)
# posts de CANAL (texto em canal)
dispatcher.add_handler(
    MessageHandler(Filters.text & Filters.chat_type.channel, handle_channel_post)
)

# =========================
# ====== ROTAS FLASK ======
# =========================
@app.route("/", methods=["GET"])
def index():
    return "OK - Bot de finanças (manual + canal ponte) com CSV, list, undo e /admin"

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

# ---- utilidades TG ----
@app.route("/tg_set_webhook", methods=["GET"])
def tg_set_webhook():
    if not ADMIN_CHAT_ID:
        return "ADMIN_CHAT_ID não configurado", 400
    key = request.args.get("key", "")
    guard = ADMIN_CHAT_ID[-6:] if len(ADMIN_CHAT_ID) >= 6 else ADMIN_CHAT_ID
    if key != guard:
        return "unauthorized", 401
    url = request.url_root.rstrip("/") + "/telegram_webhook"
    ok = bot.set_webhook(url=url, allowed_updates=["message","channel_post"], max_connections=40)
    info = bot.get_webhook_info()
    return jsonify({"set_webhook": ok, "webhook_info": info.to_dict(), "url": url})

@app.route("/tg_webhook_info", methods=["GET"])
def tg_webhook_info():
    try:
        info = bot.get_webhook_info()
        return jsonify(info.to_dict())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---- /admin (HTML + gráfico) ----
def monthly_series(last_n=6):
    """Retorna listas (labels, vendas, gastos, lucro) dos últimos N meses (mais recente por último)."""
    today = datetime.utcnow().replace(day=1)
    months = []
    for i in range(last_n-1, -1, -1):
        mdate = (today - timedelta(days=31*i)).replace(day=1)
        y, m = mdate.year, mdate.month
        months.append((y, m))
    labels, vendas, gastos, lucros = [], [], [], []
    for y, m in months:
        labels.append(f"{m:02d}/{y}")
        v = sum_payments_for_month(y, m)
        g = sum_expenses_for_month(y, m)
        vendas.append(round(v, 2))
        gastos.append(round(g, 2))
        lucros.append(round(v - g, 2))
    return labels, vendas, gastos, lucros

@app.route("/admin", methods=["GET"])
def admin():
    # mês atual
    now = datetime.utcnow(); m, y = now.month, now.year
    vendas = sum_payments_for_month(y, m)
    gastos = sum_expenses_for_month(y, m)
    lucro  = vendas - gastos

    labels, series_v, series_g, series_l = monthly_series(8)
    # HTML simples com Chart.js via CDN
    html = f"""<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<title>Admin - Controle</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{{font-family:Arial,Helvetica,sans-serif;margin:16px;}}
.card{{border:1px solid #eee;border-radius:12px;padding:16px;margin-bottom:16px;box-shadow:0 2px 8px rgba(0,0,0,.05);}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;}}
h1{{margin:8px 0 16px;}}
.kpi{{font-size:14px;color:#444}}
.kpi b{{display:block;font-size:22px;margin-top:6px}}
table{{width:100%;border-collapse:collapse;margin-top:8px}}
th,td{{border-bottom:1px solid #eee;padding:8px;text-align:left}}
.actions a{{display:inline-block;margin-right:8px}}
.footer{{margin-top:24px;color:#777;font-size:12px}}
</style>
</head>
<body>
<h1>📊 Painel — {m:02d}/{y}</h1>
<div class="grid">
  <div class="card kpi">Vendas do mês<b>R$ {vendas:.2f}</b></div>
  <div class="card kpi">Gastos do mês<b>R$ {gastos:.2f}</b></div>
  <div class="card kpi">Lucro do mês<b>R$ {lucro:.2f}</b></div>
</div>

<div class="card">
  <canvas id="chart" height="120"></canvas>
</div>

<div class="card actions">
  <h3>Ações</h3>
  <a href="/export_csv?mm={m:02d}&yyyy={y}" target="_blank">Baixar CSV do mês</a>
  &middot;
  <a href="/export_csv?range=3" target="_blank">Baixar CSV últimos 3 meses</a>
</div>

<div class="footer">Atualizado agora (UTC). Para mais: use /exportcsv, /list e /undo no Telegram.</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script>
const labels = {json.dumps(labels)};
const vendas = {json.dumps(series_v)};
const gastos = {json.dumps(series_g)};
const lucros = {json.dumps(series_l)};
const ctx = document.getElementById('chart').getContext('2d');
new Chart(ctx, {{
  type: 'line',
  data: {{
    labels: labels,
    datasets: [
      {{label:'Vendas', data: vendas, fill:false}},
      {{label:'Gastos', data: gastos, fill:false}},
      {{label:'Lucro',  data: lucros, fill:false}},
    ]
  }},
  options: {{
    responsive: true,
    tension: 0.25,
    plugins: {{
      legend: {{position:'bottom'}}
    }},
    scales: {{
      y: {{ beginAtZero: true }}
    }}
  }}
}});
</script>
</body>
</html>"""
    return Response(html, mimetype="text/html")

@app.route("/export_csv", methods=["GET"])
def export_csv_http():
    """Download CSV via navegador (/admin links)."""
    mm = request.args.get("mm")
    yyyy = request.args.get("yyyy")
    rng = request.args.get("range")  # últimos N meses
    if mm and yyyy:
        m, y = int(mm), int(yyyy)
        start = f"{y:04d}-{m:02d}-01"
        mn, yn = (1, y+1) if m==12 else (m+1, y)
        end = f"{yn:04d}-{mn:02d}-01"
        label = f"{m:02d}/{y}"
    elif rng:
        n = max(1, int(rng))
        today = datetime.utcnow().replace(day=1)
        start_date = (today - timedelta(days=31*(n-1))).strftime("%Y-%m-01")
        end = (today + timedelta(days=31)).strftime("%Y-%m-01")
        start = start_date
        label = f"ultimos_{n}_meses"
    else:
        now = datetime.utcnow(); m, y = now.month, now.year
        start = f"{y:04d}-{m:02d}-01"
        mn, yn = (1, y+1) if m==12 else (m+1, y)
        end = f"{yn:04d}-{mn:02d}-01"
        label = f"{m:02d}/{y}"

    rows = _csv_rows_for_range(start, end)
    buf = io.StringIO(); w = csv.writer(buf)
    for r in rows: w.writerow(r)
    data = buf.getvalue().encode("utf-8"); buf.close()
    fname = f"lancamentos_{label.replace('/','-')}.csv"
    return Response(data, mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={fname}"})

# =========================
# ======= MAIN ============
# =========================
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
