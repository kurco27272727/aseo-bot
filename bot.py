from __future__ import annotations

import os
import html
import json
import random
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
)

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------
TZ = ZoneInfo("America/Bogota")
MULTA = 2000                                   # pesos al tarro por tarea no cumplida
REMIND_HOUR = int(os.environ.get("REMIND_HOUR", "19"))  # 7 p.m. por defecto

MARKER = "#ASEO#"
HEADER = "🔒 Estado del bot de aseo — no desanclar ni borrar este mensaje."
CHAT_FILE = "/tmp/aseo_chat"  # ayuda al recordatorio a ubicar el grupo tras un reinicio

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("aseo-bot")

# ---------------------------------------------------------------------------
# Estado (vive dentro de Telegram, en un mensaje fijado del grupo)
# ---------------------------------------------------------------------------
def default_state():
    return {
        "members": {},          # {str(uid): {"name": str, "saldo": int}}
        "tasks": [],            # [{"id","desc","assignee","done"}]
        "period": None,         # semana ISO en curso, ej "2026-W35"
        "settled_period": None,
        "next_id": 1,
        "group_chat_id": None,
        "auto_shuffle": True,   # repartir al azar cada semana nueva
        "last_remind_date": None,  # último día que se mandó el recordatorio
    }

STATE = default_state()
LOADED = False
STATE_MSG_ID = None
_last_saved = None

PENDING = {}       # votaciones de comodín en curso (en memoria)
_next_vote = 1


# ---------------------------------------------------------------------------
# Utilidades puras
# ---------------------------------------------------------------------------
def cop(n: int) -> str:
    return "$" + f"{n:,}".replace(",", ".")


def iso_week(dt: datetime) -> str:
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def register(uid: int, name: str):
    m = STATE["members"].get(str(uid))
    if m:
        m["name"] = name
    else:
        STATE["members"][str(uid)] = {"name": name, "saldo": 0}


def member_name(uid: int | None):
    if uid is None:
        return None
    m = STATE["members"].get(str(uid))
    return m["name"] if m else None


def add_task(desc: str):
    t = {"id": STATE["next_id"], "desc": desc, "assignee": None, "done": False}
    STATE["next_id"] += 1
    STATE["tasks"].append(t)
    return t


def get_task(tid: int):
    for t in STATE["tasks"]:
        if t["id"] == tid:
            return t
    return None


def task_by_num(n: int):
    if 1 <= n <= len(STATE["tasks"]):
        return STATE["tasks"][n - 1]
    return None


def delete_task_by_num(n: int) -> bool:
    t = task_by_num(n)
    if not t:
        return False
    STATE["tasks"].remove(t)
    return True


def shuffle_assign():
    """Reparte las tareas al azar y parejo entre los miembros registrados."""
    uids = [int(u) for u in STATE["members"]]
    if not uids or not STATE["tasks"]:
        return
    random.shuffle(uids)
    tasks = STATE["tasks"][:]
    random.shuffle(tasks)
    for i, t in enumerate(tasks):
        t["assignee"] = uids[i % len(uids)]


def do_charge():
    fallidas = []
    for t in STATE["tasks"]:
        if not t["done"]:
            fallidas.append((t["desc"], member_name(t["assignee"])))
            if t["assignee"] is not None:
                m = STATE["members"].get(str(t["assignee"]))
                if m:
                    m["saldo"] += MULTA
    return fallidas


def reset_done():
    for t in STATE["tasks"]:
        t["done"] = False


def _charge_report(fallidas, titulo) -> str:
    out = [f"📅 *{titulo}*\n"]
    hechas = sum(1 for t in STATE["tasks"] if t["done"])
    out.append(f"✅ Cumplidas esta semana: {hechas}")
    if fallidas:
        out.append(f"\n🔴 No cumplidas (+{cop(MULTA)} c/u):")
        for desc, nm in fallidas:
            out.append(f"   • {desc} — {nm or 'sin asignar'}")
    else:
        out.append("\n🎉 ¡Todas cumplidas!")
    out.append("\n💰 Tarro:")
    total = 0
    for m in sorted(STATE["members"].values(), key=lambda x: x["name"]):
        total += m["saldo"]
        out.append(f"   • {m['name']}: {cop(m['saldo'])}")
    out.append(f"\nTotal: {cop(total)}")
    return "\n".join(out)


def _assign_summary(titulo) -> str:
    lines = [f"🎲 *{titulo}:*"]
    for t in STATE["tasks"]:
        lines.append(f"   • {t['desc']} → {member_name(t['assignee']) or 'sin asignar'}")
    return "\n".join(lines)


def rollover_if_needed(now: datetime):
    """Si empezó una semana nueva: cobra lo no hecho, reinicia y (si toca) reparte al azar."""
    nw = iso_week(now)
    if STATE["period"] is None:
        STATE["period"] = nw
        return None
    if nw == STATE["period"]:
        return None
    report = None
    if STATE["settled_period"] != STATE["period"]:
        fallidas = do_charge()
        report = _charge_report(fallidas, "Nueva semana — cierre automático")
    reset_done()
    STATE["settled_period"] = None
    STATE["period"] = nw
    if STATE.get("auto_shuffle") and STATE["members"] and STATE["tasks"]:
        shuffle_assign()
        rep2 = _assign_summary("Reparto de esta semana (al azar)")
        report = (report + "\n\n" + rep2) if report else rep2
    return report


def manual_close(now: datetime):
    if STATE["period"] is None:
        STATE["period"] = iso_week(now)
    if STATE["settled_period"] != STATE["period"]:
        fallidas = do_charge()
        STATE["settled_period"] = STATE["period"]
        report = _charge_report(fallidas, "Semana cerrada")
    else:
        report = "Esta semana ya estaba cerrada. Reinicio las tareas."
    reset_done()
    return report


def comodin_threshold(requester: int):
    otros = [uid for uid in STATE["members"] if int(uid) != requester]
    eligible = len(otros)
    need = eligible // 2 + 1 if eligible else 0
    return need, eligible


def pending_by_member():
    pend = {}
    for t in STATE["tasks"]:
        if not t["done"] and t["assignee"] is not None:
            pend.setdefault(t["assignee"], []).append(t["desc"])
    return pend


# ---------------------------------------------------------------------------
# Persistencia dentro de Telegram (mensaje fijado)
# ---------------------------------------------------------------------------
def _save_chat_id(cid):
    try:
        with open(CHAT_FILE, "w") as f:
            f.write(str(cid))
    except Exception:
        pass


def _read_chat_id():
    try:
        with open(CHAT_FILE) as f:
            return int(f.read().strip())
    except Exception:
        return None


async def load_from_chat(bot, chat_id):
    global STATE, STATE_MSG_ID, _last_saved, LOADED
    try:
        full = await bot.get_chat(chat_id)
        pin = full.pinned_message
        if pin and pin.text and MARKER in pin.text:
            data = json.loads(pin.text.split(MARKER, 1)[1])
            loaded = default_state()
            loaded.update(data)
            STATE = loaded
            STATE_MSG_ID = pin.message_id
            _last_saved = pin.text
            log.info("Estado cargado desde el mensaje fijado.")
    except Exception as e:
        log.warning("No pude cargar el estado: %s", e)
    if STATE["group_chat_id"] is None:
        STATE["group_chat_id"] = chat_id
    LOADED = True


async def ensure_loaded(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if LOADED:
        return
    await load_from_chat(context.bot, update.effective_chat.id)


async def save_state(context: ContextTypes.DEFAULT_TYPE):
    global STATE_MSG_ID, _last_saved
    if STATE["group_chat_id"] is None:
        return
    _save_chat_id(STATE["group_chat_id"])
    text = HEADER + "\n" + MARKER + json.dumps(STATE, ensure_ascii=False)
    if text == _last_saved:
        return
    chat_id = STATE["group_chat_id"]
    try:
        if STATE_MSG_ID:
            await context.bot.edit_message_text(text, chat_id=chat_id, message_id=STATE_MSG_ID)
        else:
            msg = await context.bot.send_message(chat_id, text)
            STATE_MSG_ID = msg.message_id
            try:
                await context.bot.pin_chat_message(chat_id, STATE_MSG_ID, disable_notification=True)
            except Exception as e:
                log.warning("No pude fijar el mensaje (¿el bot es admin?): %s", e)
        _last_saved = text
    except Exception as e:
        log.warning("Reintentando guardar estado: %s", e)
        try:
            msg = await context.bot.send_message(chat_id, text)
            STATE_MSG_ID = msg.message_id
            await context.bot.pin_chat_message(chat_id, STATE_MSG_ID, disable_notification=True)
            _last_saved = text
        except Exception as e2:
            log.warning("Falló guardar estado: %s", e2)


async def pre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_loaded(update, context)
    chat_id = STATE["group_chat_id"] or update.effective_chat.id
    report = rollover_if_needed(datetime.now(TZ))
    if report:
        await context.bot.send_message(chat_id, report, parse_mode="Markdown")
        await save_state(context)
    # Recordatorio "oportunista": si ya pasaron las 7 y hoy no se ha enviado,
    # se manda ahora (cuando alguien usa el bot). No necesita nada externo.
    if await maybe_remind(context.bot, chat_id):
        await save_state(context)


# ---------------------------------------------------------------------------
# Vistas
# ---------------------------------------------------------------------------
def build_tasks_view():
    if not STATE["tasks"]:
        return ("🧹 No hay tareas.\nAgrega una con:  /nueva lavar la loza"), None
    lines = ["🧹 Tareas de la semana:\n"]
    row, buttons = [], []
    for i, t in enumerate(STATE["tasks"], start=1):
        name = member_name(t["assignee"]) or "sin asignar"
        emoji = "🟢" if t["done"] else "⬜"
        lines.append(f"{i}. {emoji} {t['desc']} — {name}")
        row.append(InlineKeyboardButton(f"{emoji} {i}", callback_data=f"toggle:{t['id']}"))
        if len(row) == 3:
            buttons.append(row); row = []
    if row:
        buttons.append(row)
    lines.append("\nToca tu número para marcar/desmarcar (solo el responsable).")
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


def build_manage_view():
    lines = ["🔧 Gestionar tareas:\n"]
    buttons = []
    for i, t in enumerate(STATE["tasks"], start=1):
        name = member_name(t["assignee"]) or "sin asignar"
        lines.append(f"{i}. {t['desc']} — {name}")
        buttons.append([
            InlineKeyboardButton(f"🗑️ {i}", callback_data=f"del:{t['id']}"),
            InlineKeyboardButton(f"🔄 {i}", callback_data=f"rea:{t['id']}"),
        ])
    lines.append("\n🗑️ eliminar · 🔄 reasignar\nRenombrar: /renombrar <n> <texto>")
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------
AYUDA = (
    "🧹 *Bot de aseo de la casa*\n\n"
    "• /registrarme — regístrate (una vez cada quien)\n"
    "• /nueva <tarea> — crea una tarea\n"
    "• /repartir — reparte las tareas al azar entre todos\n"
    "• /auto — activa/desactiva el reparto aleatorio semanal\n"
    "• /tareas — ver y marcar tareas con botones\n"
    "• /gestionar — eliminar o reasignar tareas\n"
    "• /renombrar <n> <texto> — cambiar el nombre de la tarea n\n"
    "• /comodin [motivo] — pedir que te perdonen una tarea (se vota)\n"
    "• /tarro — cuánto debe cada quien\n"
    "• /saldar — marcar que alguien ya pagó\n"
    "• /cerrar — cerrar la semana ahora\n\n"
    "Las tareas son permanentes y se reparten al azar cada semana.\n"
    f"Cada tarea no cumplida suma {cop(MULTA)} al tarro.\n"
    f"Todos los días a las {REMIND_HOUR}:00 llega un recordatorio de lo pendiente."
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_loaded(update, context)
    if update.effective_chat.type in ("group", "supergroup"):
        STATE["group_chat_id"] = update.effective_chat.id
    await save_state(context)
    await update.message.reply_text(AYUDA, parse_mode="Markdown")


async def cmd_registrarme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await pre(update, context)
    u = update.effective_user
    register(u.id, u.first_name or (u.username or f"user{u.id}"))
    await save_state(context)
    await update.message.reply_text(f"✅ Listo {member_name(u.id)}, quedaste registrado/a.")


async def cmd_nueva(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await pre(update, context)
    desc = " ".join(context.args).strip()
    if not desc:
        await update.message.reply_text("Escríbelo así:  /nueva lavar la loza")
        return
    if not STATE["members"]:
        await update.message.reply_text("Primero cada uno debe usar /registrarme 🙂")
        return
    t = add_task(desc)
    await save_state(context)
    botones = [[InlineKeyboardButton(m["name"], callback_data=f"assign:{t['id']}:{uid}")]
               for uid, m in STATE["members"].items()]
    botones.append([InlineKeyboardButton("🎲 Que decida el azar", callback_data=f"assign:{t['id']}:rnd")])
    await update.message.reply_text(
        f"📝 Tarea creada: “{desc}”.\n¿A quién le toca?",
        reply_markup=InlineKeyboardMarkup(botones))


async def cmd_repartir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await pre(update, context)
    if not STATE["members"]:
        await update.message.reply_text("Primero cada uno debe usar /registrarme 🙂")
        return
    if not STATE["tasks"]:
        await update.message.reply_text("No hay tareas para repartir. Crea con /nueva.")
        return
    shuffle_assign()
    await save_state(context)
    await update.message.reply_text(
        _assign_summary("Reparto al azar"), parse_mode="Markdown")


async def cmd_auto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await pre(update, context)
    STATE["auto_shuffle"] = not STATE.get("auto_shuffle", True)
    await save_state(context)
    estado = "ACTIVADO ✅" if STATE["auto_shuffle"] else "DESACTIVADO ⛔"
    await update.message.reply_text(
        f"🎲 Reparto aleatorio semanal: {estado}\n"
        + ("Cada semana las tareas se reparten solas al azar."
           if STATE["auto_shuffle"] else
           "Las asignaciones se mantienen como estén de una semana a otra."))


async def cmd_tareas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await pre(update, context)
    text, markup = build_tasks_view()
    await update.message.reply_text(text, reply_markup=markup)


async def cmd_gestionar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await pre(update, context)
    if not STATE["tasks"]:
        await update.message.reply_text("No hay tareas para gestionar.")
        return
    text, markup = build_manage_view()
    await update.message.reply_text(text, reply_markup=markup)


async def cmd_renombrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await pre(update, context)
    if len(context.args) < 2 or not context.args[0].isdigit():
        await update.message.reply_text("Uso:  /renombrar 2 sacar la basura")
        return
    n = int(context.args[0]); nuevo = " ".join(context.args[1:]).strip()
    t = task_by_num(n)
    if not t:
        await update.message.reply_text("No existe esa tarea."); return
    viejo = t["desc"]; t["desc"] = nuevo
    await save_state(context)
    await update.message.reply_text(f"✏️ “{viejo}” → “{nuevo}”")


async def cmd_tarro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await pre(update, context)
    if not STATE["members"]:
        await update.message.reply_text("Nadie se ha registrado todavía."); return
    lines = ["💰 Tarro:\n"]; total = 0
    for m in sorted(STATE["members"].values(), key=lambda x: x["name"]):
        total += m["saldo"]
        estado = "" if m["saldo"] else "  (al día ✅)"
        lines.append(f"• {m['name']}: {cop(m['saldo'])}{estado}")
    lines.append(f"\nTotal: {cop(total)}")
    lines.append("\nPara registrar un pago usa /saldar.")
    await update.message.reply_text("\n".join(lines))


async def cmd_saldar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await pre(update, context)
    if not STATE["members"]:
        await update.message.reply_text("Nadie se ha registrado todavía."); return
    deudores = [(uid, m) for uid, m in STATE["members"].items() if m["saldo"] > 0]
    if not deudores:
        await update.message.reply_text("🎉 Nadie debe nada, todos al día."); return
    botones = [[InlineKeyboardButton(f"{m['name']} pagó {cop(m['saldo'])}",
                                     callback_data=f"saldar:{uid}")]
               for uid, m in deudores]
    await update.message.reply_text(
        "¿Quién pagó? Al confirmar, su saldo queda en $0.",
        reply_markup=InlineKeyboardMarkup(botones))


async def cmd_cerrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await pre(update, context)
    report = manual_close(datetime.now(TZ))
    await save_state(context)
    await update.message.reply_text(report, parse_mode="Markdown")


async def cmd_comodin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await pre(update, context)
    u = update.effective_user
    if str(u.id) not in STATE["members"]:
        await update.message.reply_text("Primero usa /registrarme."); return
    mine = [t for t in STATE["tasks"] if t["assignee"] == u.id and not t["done"]]
    if not mine:
        await update.message.reply_text("No tienes tareas pendientes para pedir comodín."); return
    context.user_data["comodin_note"] = " ".join(context.args).strip()
    botones = [[InlineKeyboardButton(t["desc"], callback_data=f"joker:{t['id']}")] for t in mine]
    await update.message.reply_text("🃏 ¿Para cuál tarea pides comodín?",
                                    reply_markup=InlineKeyboardMarkup(botones))


# ---------------------------------------------------------------------------
# Botones
# ---------------------------------------------------------------------------
async def cb_assign(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, tid, uid = q.data.split(":")
    t = get_task(int(tid))
    if not t:
        await q.answer("Ya no existe.", show_alert=True); return
    if uid == "rnd":
        if not STATE["members"]:
            await q.answer("Nadie registrado.", show_alert=True); return
        chosen = random.choice([int(u) for u in STATE["members"]])
        t["assignee"] = chosen
    else:
        if uid not in STATE["members"]:
            await q.answer("Ya no existe.", show_alert=True); return
        t["assignee"] = int(uid)
    await save_state(context)
    await q.answer()
    await q.edit_message_text(
        f"✅ “{t['desc']}” → {member_name(t['assignee'])}\nMárcala en /tareas cuando esté lista.")


async def cb_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    t = get_task(int(q.data.split(":")[1]))
    if not t:
        await q.answer("Esa tarea ya no existe.", show_alert=True); return
    if t["assignee"] is None:
        await q.answer("Primero asígnala a alguien.", show_alert=True); return
    if q.from_user.id != t["assignee"]:
        await q.answer(f"Esa tarea es de {member_name(t['assignee'])} 😉", show_alert=True); return
    t["done"] = not t["done"]
    await save_state(context)
    text, markup = build_tasks_view()
    await q.answer("¡Hecho! 🟢" if t["done"] else "Desmarcada ⬜")
    await q.edit_message_text(text, reply_markup=markup)


async def cb_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    t = get_task(int(q.data.split(":")[1]))
    if t:
        STATE["tasks"].remove(t)
        await save_state(context)
    await q.answer("Eliminada 🗑️")
    if not STATE["tasks"]:
        await q.edit_message_text("No quedan tareas."); return
    text, markup = build_manage_view()
    await q.edit_message_text(text, reply_markup=markup)


async def cb_rea(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tid = int(q.data.split(":")[1])
    t = get_task(tid)
    if not t:
        await q.answer("Ya no existe.", show_alert=True); return
    botones = [[InlineKeyboardButton(m["name"], callback_data=f"assign:{tid}:{uid}")]
               for uid, m in STATE["members"].items()]
    botones.append([InlineKeyboardButton("🎲 Al azar", callback_data=f"assign:{tid}:rnd")])
    await q.answer()
    await q.edit_message_text(f"🔄 Reasignar “{t['desc']}”. ¿A quién?",
                              reply_markup=InlineKeyboardMarkup(botones))


async def cb_saldar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.data.split(":")[1]
    m = STATE["members"].get(uid)
    if not m:
        await q.answer("No existe.", show_alert=True); return
    pagado = m["saldo"]
    m["saldo"] = 0
    await save_state(context)
    await q.answer("Saldado ✅")
    await q.edit_message_text(
        f"🧾 {m['name']} pagó {cop(pagado)}. Queda al día (saldo $0).")


# ----- Comodín (votación) --------------------------------------------------
def _vote_text(v):
    need, eligible = comodin_threshold(v["requester"])
    nombre = member_name(v["requester"]) or "Alguien"
    t = get_task(v["task_id"])
    desc = t["desc"] if t else "(tarea)"
    lines = [f"🃏 *{nombre}* pide comodín para: *{desc}*"]
    if v["note"]:
        lines.append(f"_Motivo:_ {v['note']}")
    lines.append(f"\nNecesita {need} voto(s) a favor de {eligible} persona(s).")
    lines.append(f"👍 {len(v['yes'])}   👎 {len(v['no'])}")
    return "\n".join(lines)


async def cb_joker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global _next_vote
    q = update.callback_query
    tid = int(q.data.split(":")[1])
    t = get_task(tid)
    if not t or t["assignee"] != q.from_user.id:
        await q.answer("No puedes pedir comodín de esa tarea.", show_alert=True); return
    need, eligible = comodin_threshold(q.from_user.id)
    if eligible == 0:
        await q.answer("No hay nadie más para votar.", show_alert=True); return
    vid = _next_vote; _next_vote += 1
    v = {"task_id": tid, "requester": q.from_user.id,
         "note": context.user_data.get("comodin_note", ""), "yes": set(), "no": set()}
    PENDING[vid] = v
    await q.answer()
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("👍 Aprobar", callback_data=f"vote:yes:{vid}"),
        InlineKeyboardButton("👎 Rechazar", callback_data=f"vote:no:{vid}"),
    ]])
    await q.edit_message_text(_vote_text(v), parse_mode="Markdown", reply_markup=kb)


async def cb_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, choice, vid = q.data.split(":")
    vid = int(vid)
    v = PENDING.get(vid)
    if not v:
        await q.answer("Esta votación ya terminó.", show_alert=True); return
    voter = q.from_user.id
    if voter == v["requester"]:
        await q.answer("No puedes votar tu propio comodín 🙂", show_alert=True); return
    if str(voter) not in STATE["members"]:
        await q.answer("Debes usar /registrarme para votar.", show_alert=True); return
    v["yes"].discard(voter); v["no"].discard(voter)
    (v["yes"] if choice == "yes" else v["no"]).add(voter)

    need, _ = comodin_threshold(v["requester"])
    t = get_task(v["task_id"])
    if len(v["yes"]) >= need:
        PENDING.pop(vid, None)
        if t:
            t["done"] = True
            await save_state(context)
        await q.answer("Aprobado")
        await q.edit_message_text(
            f"✅ Comodín APROBADO para “{t['desc'] if t else ''}”. Cuenta como hecha esta semana.")
        return
    if len(v["no"]) >= need:
        PENDING.pop(vid, None)
        await q.answer("Rechazado")
        await q.edit_message_text(
            f"❌ Comodín RECHAZADO para “{t['desc'] if t else ''}”. Sigue pendiente.")
        return
    await q.answer("Voto registrado")
    await q.edit_message_text(_vote_text(v), parse_mode="Markdown", reply_markup=q.message.reply_markup)


# ---------------------------------------------------------------------------
# Recordatorio diario (7 p.m.)
# ---------------------------------------------------------------------------
async def _send_reminder(bot, chat_id):
    """Construye y envía el recordatorio de pendientes, mencionando a cada quien."""
    pend = pending_by_member()
    if not pend:
        return False
    parts = ["🔔 <b>Recordatorio</b> — tareas pendientes:\n"]
    for uid, descs in pend.items():
        name = member_name(uid) or "Alguien"
        mention = f'<a href="tg://user?id={uid}">{html.escape(name)}</a>'
        tareas = ", ".join(html.escape(d) for d in descs)
        parts.append(f"{mention}: {tareas}")
    parts.append("\nMárcalas en /tareas ✅  ·  ¿hiciste otra cosa? pide /comodin 🃏")
    await bot.send_message(chat_id, "\n".join(parts), parse_mode="HTML")
    return True


async def maybe_remind(bot, chat_id) -> bool:
    """Manda el recordatorio del día si ya pasó la hora y aún no se envió hoy.
    Devuelve True si envió algo (para que el que llama guarde el estado)."""
    now = datetime.now(TZ)
    if now.hour < REMIND_HOUR:
        return False
    today = now.date().isoformat()
    if STATE.get("last_remind_date") == today:
        return False
    if await _send_reminder(bot, chat_id):
        STATE["last_remind_date"] = today
        return True
    return False


async def remind_job(context: ContextTypes.DEFAULT_TYPE):
    """Trabajo programado a las 7 p.m. (funciona si el servidor está despierto).
    Si estaba dormido, no pasa nada: el recordatorio saldrá igual cuando alguien
    use el bot esa noche (modo oportunista en pre())."""
    chat_id = STATE["group_chat_id"] or _read_chat_id()
    if not chat_id:
        return
    if not LOADED:
        await load_from_chat(context.bot, chat_id)
    report = rollover_if_needed(datetime.now(TZ))
    if report:
        await context.bot.send_message(chat_id, report, parse_mode="Markdown")
    if await maybe_remind(context.bot, chat_id):
        await save_state(context)
    elif report:
        await save_state(context)


# ---------------------------------------------------------------------------
# Arranque
# ---------------------------------------------------------------------------
def main():
    token = os.environ["BOT_TOKEN"]
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler(["start", "ayuda", "help"], cmd_start))
    app.add_handler(CommandHandler("registrarme", cmd_registrarme))
    app.add_handler(CommandHandler("nueva", cmd_nueva))
    app.add_handler(CommandHandler("repartir", cmd_repartir))
    app.add_handler(CommandHandler("auto", cmd_auto))
    app.add_handler(CommandHandler("tareas", cmd_tareas))
    app.add_handler(CommandHandler("gestionar", cmd_gestionar))
    app.add_handler(CommandHandler("renombrar", cmd_renombrar))
    app.add_handler(CommandHandler("comodin", cmd_comodin))
    app.add_handler(CommandHandler("tarro", cmd_tarro))
    app.add_handler(CommandHandler("saldar", cmd_saldar))
    app.add_handler(CommandHandler(["cerrar", "cerrar_semana"], cmd_cerrar))

    app.add_handler(CallbackQueryHandler(cb_assign, pattern=r"^assign:"))
    app.add_handler(CallbackQueryHandler(cb_toggle, pattern=r"^toggle:"))
    app.add_handler(CallbackQueryHandler(cb_del, pattern=r"^del:"))
    app.add_handler(CallbackQueryHandler(cb_rea, pattern=r"^rea:"))
    app.add_handler(CallbackQueryHandler(cb_saldar, pattern=r"^saldar:"))
    app.add_handler(CallbackQueryHandler(cb_joker, pattern=r"^joker:"))
    app.add_handler(CallbackQueryHandler(cb_vote, pattern=r"^vote:"))

    app.job_queue.run_daily(
        remind_job, time=time(REMIND_HOUR, 0, tzinfo=TZ), name="recordatorio")

    port = int(os.environ.get("PORT", 0))
    base = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_URL")
    if port and base:
        log.info("Modo webhook: %s", base)
        app.run_webhook(listen="0.0.0.0", port=port, url_path=token,
                        webhook_url=f"{base}/{token}", drop_pending_updates=True)
    else:
        log.info("Modo polling (local)")
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
