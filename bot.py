# =============================================================
# BOT-MANEMAN  —  bot.py
# Bot de gestion du temps de travail des employés
# Logique : privé = pas sauvegardé | groupe = sauvegardé
# Compatible Render Web Service (aiohttp + polling simultanés)
# + endpoint /api/stats pour l'application web React
# =============================================================

from sheets import save_event, save_daily_status
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)
from aiohttp import web
from datetime import datetime, date, timedelta, timezone
from collections import defaultdict
from threading import Lock
import asyncio
import os
import urllib.request
import json
import time
import logging
from dotenv import load_dotenv

# =========================
# LOGGING
# =========================
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.ERROR
)
logger = logging.getLogger(__name__)

# =========================
# ENV CONFIG
# =========================
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
PORT  = int(os.getenv("PORT", 10000))

# =========================
# TIMEZONE MADAGASCAR (UTC+3)
# =========================
MADAGASCAR_TZ = timezone(timedelta(hours=3))

def now_madagascar() -> datetime:
    return datetime.now(MADAGASCAR_TZ)

def today_madagascar() -> date:
    return now_madagascar().date()

# =========================
# GROUP LOCK
# =========================
ALLOWED_GROUP_ID = -1003692081885

# =========================
# MANAGER — mentionné quand une pause est dépassée
# =========================
MANAGER_USERNAMES = ["@apasiihhhzz", "@cegilinuarea", "@voplaledalala2"]
managers_mention = " ".join(MANAGER_USERNAMES)


# =========================
# HORAIRES PERSONNALISÉS PAR USERNAME
# Employés dont l'heure de début diffère de 07:40
# =========================
CUSTOM_START_TIMES = {
    "maherylp": (8, 10),   # heure, minute
    # "autreusername": (9, 0),  ← ajoute d'autres ici si besoin
}
DEFAULT_START_TIME = (7, 40)  # heure par défaut pour tous les autres



# =========================
# STATES
# =========================
OFF_SHIFT          = "OFF_SHIFT"
WORKING            = "WORKING"
BREAK_FOOD         = "BREAK_FOOD"
BREAK_TOILET_SMALL = "BREAK_TOILET_SMALL"
BREAK_TOILET_BIG   = "BREAK_TOILET_BIG"
BREAK_SMOKE        = "BREAK_SMOKE"

BREAK_STATES = {
    BREAK_FOOD,
    BREAK_TOILET_SMALL,
    BREAK_TOILET_BIG,
    BREAK_SMOKE
}

# =========================
# LIMITS (secondes)
# =========================
LIMITS = {
    BREAK_FOOD:         40 * 60,
    BREAK_TOILET_SMALL:  7 * 60,
    BREAK_TOILET_BIG:   20 * 60,
    BREAK_SMOKE:         7 * 60
}

WARN_BEFORE_SEC = 2 * 60

OVERDUE_ALERTS_SEC = [0, 10*60, 20*60, 30*60, 40*60, 50*60, 60*60]

# =========================
# LABELS / EMOJIS
# =========================
LABELS = {
    BREAK_FOOD:         "Eat",
    BREAK_TOILET_SMALL: "Small toilet",
    BREAK_TOILET_BIG:   "Big toilet",
    BREAK_SMOKE:        "Smoke"
}

EMOJIS = {
    BREAK_FOOD:         "🍽",
    BREAK_TOILET_SMALL: "🚻",
    BREAK_TOILET_BIG:   "🚽",
    BREAK_SMOKE:        "🚬"
}

BREAK_COLOR_EMOJIS = {
    BREAK_FOOD:         "🟡",
    BREAK_TOILET_SMALL: "🟡",
    BREAK_TOILET_BIG:   "🟡",
    BREAK_SMOKE:        "🟡"
}

MAX_DAILY = {
    BREAK_FOOD:       3,
    BREAK_TOILET_SMALL:  6,
    BREAK_TOILET_BIG: 2,
    BREAK_SMOKE:      5
}

# =========================
# MEMORY — UNIFIÉE + THREAD-SAFE
# =========================
users      = {}
users_lock = Lock()

def _default_user_context() -> dict:
    return {
        "state":              OFF_SHIFT,
        "work_start":         None,   # heure du dernier Start Work (pour affichage arrivée)
        "first_work_start":   None,   # toute première arrivée du jour (jamais écrasée)
        "break_start":        None,
        "break_type":         None,
        "sessions":           [],     # pauses terminées
        "work_segments":      [],     # segments de travail [{start, end, duration}]
        "break_counts":       {},
        "last_date":          None,
        "warn_task":          None,
        "started_today":      False,
        "retard_sec":         0,      # retard en secondes (calculé au 1er Start Work)
    }

def _default_user() -> dict:
    return {
        "metadata": {
            "full_name":  "Unknown",
            "username":   "N/A",
            "created_at": now_madagascar().isoformat()
        },
        "contexts": {}
    }

def get_user_context(user_id: int, save_to_db: bool, group_id: int = None) -> dict:
    with users_lock:
        if user_id not in users:
            users[user_id] = _default_user()
        store_key = "private" if not save_to_db else f"group_{group_id}"
        if store_key not in users[user_id]["contexts"]:
            users[user_id]["contexts"][store_key] = _default_user_context()
        return users[user_id]["contexts"][store_key]

def update_user_metadata(user_id: int, full_name: str, username: str):
    with users_lock:
        if user_id not in users:
            users[user_id] = _default_user()
        users[user_id]["metadata"]["full_name"] = full_name
        users[user_id]["metadata"]["username"]  = username or "N/A"

def reset_user_context(user_id: int, save_to_db: bool, group_id: int = None):
    with users_lock:
        if user_id not in users:
            users[user_id] = _default_user()
        store_key = "private" if not save_to_db else f"group_{group_id}"
        ctx = _default_user_context()
        ctx["last_date"]     = today_madagascar().isoformat()
        ctx["started_today"] = False
        users[user_id]["contexts"][store_key] = ctx

def check_and_reset_daily(u: dict) -> dict:
    """Remet à zéro les flags journaliers à minuit (Madagascar)."""
    today = today_madagascar().isoformat()
    if u.get("last_date") != today:
        u["started_today"]    = False
        u["break_counts"]     = {}
        u["last_date"]        = today
        u["work_segments"]    = []
        u["sessions"]         = []
        u["first_work_start"] = None
        u["work_start"]       = None
        u["retard_sec"]       = 0
        u["state"]            = OFF_SHIFT
        u["break_start"]      = None
        u["break_type"]       = None
    u.pop("lunch_taken", None)
    return u

# =========================
# KEYBOARD
# =========================
def get_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        [
            KeyboardButton("🟢 Start Work"),
            KeyboardButton("🔴 Off Work"),
            KeyboardButton("🍽 EAT")
        ],
        [
            KeyboardButton("🚻 Small Toilet"),
            KeyboardButton("🚽 Big Toilet"),
            KeyboardButton("🚬 SMOKE")
        ],
        [
            KeyboardButton("🔵 BACK TO SEAT")
        ]
    ], resize_keyboard=True)

# =========================
# HELPERS
# =========================
def check_break_limit(break_type: str, duration_sec: float):
    limit = LIMITS.get(break_type)
    if not limit:
        return False, None
    exceeded = duration_sec > limit
    return exceeded, (duration_sec - limit) if exceeded else None

def check_stuck_break(u: dict, now: datetime):
    start = u.get("break_start")
    btype = u.get("break_type")
    if not start or not btype:
        return None
    duration = (now - start).total_seconds()
    limit    = LIMITS.get(btype, 0)
    return duration if duration > limit * 2 else None

def fmt_duration(total_seconds: float) -> str:
    total_seconds = int(total_seconds)
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    if h:
        return f"{h}h{m:02d}min"
    if s and not h:
        return f"{m}min and {s} seconds" if m else f"{s} seconds"
    return f"{m}min"

def fmt_duration_short(total_seconds: float) -> str:
    total_seconds = int(total_seconds)
    m = total_seconds // 60
    s = total_seconds % 60
    if m and s:
        return f"{m}min {s}s"
    if m:
        return f"{m}min"
    return f"{s}s"

def build_break_reminder(u: dict, now: datetime) -> str:
    btype     = u.get("break_type")
    b_start   = u.get("break_start")
    label     = LABELS.get(btype, "break")
    emoji     = EMOJIS.get(btype, "⏸")
    limit_sec = LIMITS.get(btype, 0)

    if b_start:
        elapsed  = (now - b_start).total_seconds()
        overtime = elapsed - limit_sec
        line1 = f"{emoji} You are still on a {label} break."
        line2 = f"🚀Started : {b_start.strftime('%H:%M:%S')}"
        line3 = f"🕒Elapsed : {fmt_duration_short(elapsed)}"
        line4 = f"⏳Limit : {fmt_duration_short(limit_sec)}"
        if overtime > 0:
            return (
                f"{line1}\n\n"
                f"{line2}\n{line3}\n"
                f"⚠️ Over limit by {fmt_duration_short(overtime)}\n\n"
                f"Check in and return to your seat and Press BACK TO SEAT.\n\n"
                f"Be careful not to spend too much time on breaks, time is precious and should not be wasted.\n\n"
                f"💸 Late returning to your seat will be fined."
            )
        return (
            f"{line1}\n\n"
            f"{line2}\n{line3}\n{line4}\n\n"
            f"🗣 Ehh! Check in and return to your seat promptly after completing the activity.\n\n"
            f"🔥 Yo man! Your lack of awareness means you ain't focused on the job.\n\n"
            f"💸 Late returning to your seat will be fined.\n\n"
            f"🔵 Press BACK TO SEAT once you are seated."
        )
    return (
        f"{emoji} You are still on a {label} break.\n\n"
        f"🗣 Ehh! Check in and return to your seat promptly after completing the activity.\n\n"
        f"💸 Yo man! If you’re just wasting time, you’re gonna get fined.\n\n"
        f"🔵 Press BACK TO SEAT once you are seated."
        
    )

def build_back_to_seat_msg(name, username, user_id, now, b_start,
                            b_type, duration_sec, exceeded, extra, sessions):
    label = LABELS.get(b_type, "Break")
    emoji = EMOJIS.get(b_type, "⏸")
    mention = f"@{username}" if username != "N/A" else name

    dur_min = int(duration_sec) // 60
    dur_sec = int(duration_sec) % 60
    if dur_min and dur_sec:
        dur_str = f"{dur_min}min {dur_sec}s"
    elif dur_min:
        dur_str = f"{dur_min}min"
    else:
        dur_str = f"{dur_sec}s"

    msg = (
        f"🔵 BACK TO SEAT\n\n"
        f"👤User: {mention}\n"
        f"🪪 User ID: {user_id}\n\n"
        f"✅ {now.strftime('%d/%m %H:%M:%S')} Successfully clocked in for returning to work has been saved.\n\n"
        f"📅Date : {now.strftime('%d/%m/%Y')}\n"
        f"👤Name : {name}\n"
        f"{emoji} {label} Start : {b_start.strftime('%H:%M:%S')}\n"
        f"🔚 {label} End : {now.strftime('%H:%M:%S')}\n"
        f"⏱️Duration : {dur_str}"
    )

    if exceeded:
        limit_min = LIMITS[b_type] // 60
        extra_min = int(extra // 60)
        extra_sec = int(extra % 60)
        over_str  = f"{extra_min}minutes {extra_sec}s" if extra_sec else f"{extra_min}minutes"
        msg += (
            f"\n\n⚠️ {label.upper()} EXCEEDED\n"
            f"Limit: {limit_min} minutes | Over by: +{over_str} \n\n"
            f"👀{managers_mention}"
        )

    all_sessions = sessions + [{"type": b_type, "duration": duration_sec, "exceeded": exceeded}]

    type_totals = defaultdict(float)
    type_counts = defaultdict(int)
    total_all   = 0.0

    for s in all_sessions:
        type_totals[s["type"]] += s["duration"]
        type_counts[s["type"]] += 1
        total_all               += s["duration"]

    type_total_sec = type_totals[b_type]
    type_total_min = int(type_total_sec) // 60
    type_total_s   = int(type_total_sec) % 60
    if type_total_min and type_total_s:
        type_total_str = f"{type_total_min} minutes {type_total_s} seconds"
    elif type_total_min:
        type_total_str = f"{type_total_min} minutes"
    else:
        type_total_str = f"{type_total_s} seconds"

    total_all_sec = int(total_all)
    total_all_min = total_all_sec // 60
    total_all_s   = total_all_sec % 60
    if total_all_min and total_all_s:
        total_all_str = f"{total_all_min} minutes {total_all_s} seconds"
    elif total_all_min:
        total_all_str = f"{total_all_min} minutes"
    else:
        total_all_str = f"{total_all_s} seconds"

    msg += (
        f"\n──────────────────────\n"
        f"⏱ Total {label} today : {type_total_str}\n"
        f"📊 Total all breaks today : {total_all_str}\n"
        f" ──────────────────────\n"
    )

    eat_count    = type_counts[BREAK_FOOD]
    ts_count     = type_counts[BREAK_TOILET_SMALL]
    tb_count     = type_counts[BREAK_TOILET_BIG]
    smoke_count  = type_counts[BREAK_SMOKE]
    toilet_count = ts_count + tb_count

    if eat_count:
        msg += f"🍽  Eat today : {eat_count}X\n"
    if ts_count:
        msg += f"🚻 Small toilet today : {ts_count}X\n"
    if tb_count:
        msg += f"🚽 Big toilet today : {tb_count}X\n"
    if smoke_count:
        msg += f"🚬 Smoke breaks today : {smoke_count}X\n"
    if not (eat_count or toilet_count or smoke_count):
        msg += "  • No breaks taken\n"

    return msg.rstrip()

# =========================
# AVERTISSEMENTS
# =========================
async def _send_break_warning(bot, chat_id, user_id, name, username,
                               b_type, break_start, u):
    limit_sec = LIMITS.get(b_type, 0)
    label     = LABELS.get(b_type, "break")
    delay     = limit_sec - WARN_BEFORE_SEC
    mention   = f"@{username}" if username != "N/A" else name

    if delay > 0:
        await asyncio.sleep(delay)
        if u.get("break_start") != break_start or u.get("break_type") != b_type:
            return
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"👤User: {mention}\n"
                f"🪪 User ID: {user_id}\n\n"
                f"*⚠️ Warning: You still have less than 2 minutes left "
                f"for your {label} break.*\n\n"
                f"Please make sure to return to your seat promptly once you have finished the activity.\n\n"
                f"*Yo man❗️ Messin' up your time management gonna get you punished* — this company don't play, and the grind don't wait.\n\n"
                f"💸 Late returning to your seat will be fined."
            ),
            parse_mode="Markdown"
        )

    await asyncio.sleep(WARN_BEFORE_SEC)
    if u.get("break_start") != break_start or u.get("break_type") != b_type:
        return
    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"👤User: {mention}\n"
            f"🪪 User ID: {user_id}\n\n"
            f"🚨 Time's up! Your {label} time limit has been reached.\n\n"
            f"🔵 Once you are seated press BACK TO SEAT immediately!\n"
            f"❌ You are fined because you exceeded the given time limit."
        )
    )

    prev_offset = 0
    for offset_sec in OVERDUE_ALERTS_SEC[1:]:
        sleep_duration = offset_sec - prev_offset
        await asyncio.sleep(sleep_duration)
        prev_offset = offset_sec

        if u.get("break_start") != break_start or u.get("break_type") != b_type:
            return

        now_t         = now_madagascar()
        elapsed_total = (now_t - break_start).total_seconds()
        over_limit    = elapsed_total - limit_sec
        over_min      = int(over_limit) // 60
        over_sec      = int(over_limit) % 60

        if over_min and over_sec:
            over_str = f"{over_min}min {over_sec}s"
        elif over_min:
            over_str = f"{over_min}min"
        else:
            over_str = f"{over_sec}s"

        if offset_sec >= 60 * 60:
            urgency = f"🚨🚨🚨 URGENT — {managers_mention}\n"
        elif offset_sec >= 30 * 60:
            urgency = f"🚨🚨 CRITICAL — {managers_mention}\n"
        else:
            urgency = f"🚨 REMINDER — {managers_mention}\n"

        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"{urgency}"
                f"👤User: {mention}\n"
                f"🪪 User ID: {user_id}\n\n"
                f"⏰ You have been on your {label} break for {fmt_duration(elapsed_total)}!\n"
                f"⚠️ Over limit by: +{over_str}⚠️\n\n"
                f"🔵 Once you are seated, please press BACK TO SEAT immediately.\n"
                f"*💸 Yo man❗️ You are fined because you exceeded the given time limit.*"
            ),
            parse_mode="Markdown"
        )

# =========================
# SESSION RESET
# =========================
def reset_telegram_session():
    if not TOKEN:
        return
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/deleteWebhook?drop_pending_updates=true"
        with urllib.request.urlopen(url, timeout=5) as r:
            json.loads(r.read())
    except Exception:
        pass
    for attempt in range(1, 6):
        try:
            url = f"https://api.telegram.org/bot{TOKEN}/getUpdates?timeout=0&offset=-1"
            with urllib.request.urlopen(url, timeout=5) as r:
                resp = json.loads(r.read())
            if resp.get("ok"):
                print(f"✅ Session Telegram libérée (tentative {attempt}).")
                return
        except Exception:
            pass
        print(f"⏳ Attente libération session ({attempt}/5)...")
        time.sleep(2)
    print("⚠️  Session non libérée — arrêtez l'instance distante ou révoquez le token.")

# =========================
# ERROR HANDLER
# =========================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    if "Conflict" in type(error).__name__ or "Conflict" in str(error):
        print("⚠️  Conflict Telegram : une autre instance est active.")
        return
    logger.error("Erreur non gérée", exc_info=context.error)

# =========================
# HELPER STATS PAR EMPLOYÉ
# (pour la sauvegarde Google Sheets)
# =========================
def _compute_user_stats(u: dict, now: datetime) -> dict:
    """
    Calcule les statistiques complètes d'un employé à partir de son contexte.
    Retourne un dict compatible avec save_daily_status().
    """
    state               = u.get("state", OFF_SHIFT)
    work_start          = u.get("work_start")          # dernier start work (segment en cours)
    first_work_start    = u.get("first_work_start")    # toute première arrivée du jour
    sessions            = u.get("sessions", [])
    work_segments       = u.get("work_segments", [])   # segments terminés
    break_counts        = u.get("break_counts", {})
    current_break_type  = u.get("break_type")
    current_break_start = u.get("break_start")

    if state == WORKING:
        statut = "Présent"
    elif state in BREAK_STATES:
        statut = "Pause"
    else:
        statut = "Absent"

    # Temps de travail = segments terminés + segment en cours
    work_total_sec = sum(s["duration"] for s in work_segments)
    if work_start and state in ({WORKING} | BREAK_STATES):
        work_total_sec += (now - work_start).total_seconds()

    # Total pauses terminées
    break_total_sec = sum(s["duration"] for s in sessions)
    # Pause en cours
    if current_break_start and state in BREAK_STATES:
        break_total_sec += (now - current_break_start).total_seconds()

    effective_sec = max(work_total_sec - break_total_sec, 0)

    # Dépassements
    overdue_sec = 0.0
    for s in sessions:
        if s.get("exceeded"):
            limit = LIMITS.get(s["type"], 0)
            overdue_sec += max(s["duration"] - limit, 0)

    # Retard — utilise retard_sec stocké au 1er Start Work (avec secondes)
    retard_sec = u.get("retard_sec", 0)
    retard_min = int(retard_sec // 60)

    # Stats par type de pause
    def _pause(btype):
        count = break_counts.get(btype, 0)
        total = sum(s["duration"] for s in sessions if s["type"] == btype)
        if current_break_type == btype and current_break_start:
            total += (now - current_break_start).total_seconds()
        return {"count": count, "sec": int(total)}

    repas     = _pause(BREAK_FOOD)
    courtes   = _pause(BREAK_TOILET_SMALL)
    longues   = _pause(BREAK_TOILET_BIG)
    cigarette = _pause(BREAK_SMOKE)

    return {
        "statut":                 statut,
        "heure_arrivee":          first_work_start.strftime("%H:%M") if first_work_start else (work_start.strftime("%H:%M") if work_start else "--:--"),
        "heure_depart":           "--:--",
        "retard_min":             retard_min,
        "retard_sec":             int(retard_sec),
        "depart_anticipe_min":    0,
        "temps_travail_sec":      int(work_total_sec),
        "temps_effectif_sec":     int(effective_sec),
        "pause_repas_count":      repas["count"],
        "pause_repas_sec":        repas["sec"],
        "pauses_courtes_count":   courtes["count"],
        "pauses_courtes_sec":     courtes["sec"],
        "pauses_longues_count":   longues["count"],
        "pauses_longues_sec":     longues["sec"],
        "pauses_cigarette_count": cigarette["count"],
        "pauses_cigarette_sec":   cigarette["sec"],
        "departs_temp_count":     len(sessions),
        "departs_temp_sec":       sum(int(s["duration"]) for s in sessions),
        "activites_total_sec":    int(work_total_sec),
        "depassements_sec":       int(overdue_sec),
    }


async def _async_save_daily(user_id, name, username, stats):
    """Wrapper asynchrone pour ne pas bloquer le bot."""
    try:
        await asyncio.to_thread(save_daily_status, user_id, name, username, stats)
    except Exception as e:
        print(f"❌ _async_save_daily error: {e}")


# =========================
# /start
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ *Attendant Bot Activated*\n"
        "──────────────────────\n\n"
        "🟢 *Start Work*\n"
        "Press as soon as you arrive to log your workday and shift.\n\n"
        "🟡 *Break Type*\n"
        "Select the correct break type each time you leave your seat.\n\n"
        "🔵 *Back to Seat*\n"
        "Press after each break to confirm your return.\n\n"
        "🍜 *Meal break: Breakfast: 08:00, Lunch: 12:30 , Dinner: 18:00*.\n\n"
        "🚫 *All type break not allowed after 18:40*.\n\n"
        "🕕 *Return Before End*\n"
        "🏢 Yo, what the company cares about right now is your focus and your hustle on the job.\n\n"
        "🔴 *Off Work*\n"
        "At day's end, press to close your workday.\n"
        "──────────────────────\n"
        "Choose menu 👇",
        parse_mode="Markdown",
        reply_markup=get_keyboard()
    )

# =========================
# MAIN HANDLER
# =========================
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user:
        return

    chat = update.message.chat

    if chat.type == "private":
        save_to_db = False
        group_id   = None
    elif chat.type in ("group", "supergroup") and chat.id == ALLOWED_GROUP_ID:
        save_to_db = True
        group_id   = chat.id
    else:
        return

    user     = update.message.from_user
    user_id  = user.id
    name     = user.full_name
    username = user.username or "N/A"
    text     = update.message.text

    update_user_metadata(user_id, name, username)

    valid_buttons = [
        "🟢 Start Work", "🔴 Off Work", "🍽 EAT",
        "🚻 Small Toilet", "🚽 Big Toilet", "🚬 SMOKE",
        "🔵 BACK TO SEAT"
    ]

    if text not in valid_buttons:
        return

    u   = get_user_context(user_id, save_to_db, group_id)
    u   = check_and_reset_daily(u)
    now = now_madagascar()

    mapping = {
        "🟢 Start Work":   WORKING,
        "🔴 Off Work":     OFF_SHIFT,
        "🍽 EAT":          BREAK_FOOD,
        "🚻 Small Toilet": BREAK_TOILET_SMALL,
        "🚽 Big Toilet":   BREAK_TOILET_BIG,
        "🚬 SMOKE":        BREAK_SMOKE,
        "🔵 BACK TO SEAT": WORKING
    }

    new_state = mapping[text]
    old_state = u["state"]

    mention = f"@{username}" if username != "N/A" else name

    # Garde 1 : pas encore démarré
    if old_state == OFF_SHIFT and text != "🟢 Start Work":
        await update.message.reply_text(
            "You must press START WORK button first.\n"
            "To begin and check in your workday and shift.\n\n"
            "⚠️ If you skip this step, your work hours won't be tracked. Make sure you start properly."
        )
        return

    # Garde 2 : en pause → autre pause
    if old_state in BREAK_STATES and new_state in BREAK_STATES:
        await update.message.reply_text(build_break_reminder(u, now))
        return

    # Garde 3 : en pause → Start Work
    if old_state in BREAK_STATES and text == "🟢 Start Work":
        await update.message.reply_text(build_break_reminder(u, now))
        return

    # Garde 4 : en pause → Off Work
    if old_state in BREAK_STATES and text == "🔴 Off Work":
        await update.message.reply_text(build_break_reminder(u, now))
        return

    # Garde 5 : pas en pause → BACK TO SEAT
    if text == "🔵 BACK TO SEAT" and old_state not in BREAK_STATES:
        await update.message.reply_text(
            "⚠️ You are not on a BREAK.\n"
            "No need to press BACK TO SEAT\n\n"
            "Just work hard and take a break when you really need it.\n\n"
            "*⏰ But don't spend too much time on breaks, because time is precious.*\n"
            "*🏆 Every effort you make brings you closer to success…*\n\n"
            "With that hustle and determination, you gonna be one of the realest out here.\n\n"
            "💸 Enjoy the grind, fam — the effort you put in today makes you greatness.",
            parse_mode="Markdown"
        )
        return

    # Alerte pause oubliée
    if text != "🔵 BACK TO SEAT":
        stuck = check_stuck_break(u, now)
        if stuck:
            await update.message.reply_text(
                f"🚨 WARNING 🚨\n\n"
                f"You have not pressed BACK TO SEAT\n"
                f"Away for too long.\n\n"
                f"🔵 Once you are seated, please press BACK TO SEAT immediately.\n"
                f"Current break duration: {fmt_duration(stuck)}\n\n"
                f"👤 @{username} — please confirm your return.\n\n"
                f"👀  {', '.join(MANAGER_USERNAMES)} — {mention} has been on a break for {fmt_duration(stuck)} without confirming return."
            )

    # Garde 6 : limite journalière
    if new_state in BREAK_STATES and new_state in MAX_DAILY:
        count_taken = u.get("break_counts", {}).get(new_state, 0)
        max_allowed = MAX_DAILY[new_state]
        if count_taken >= max_allowed:
            label = LABELS.get(new_state, "break")
            emoji = EMOJIS.get(new_state, "⏸")
            await update.message.reply_text(
                f"⛔ {emoji} *{label.upper()} — Daily limit reached*\n\n"
                f"You have already used this break {count_taken}x today.\n"
                f"Maximum allowed : {max_allowed}x per day\n\n"
                f"⚠️ *Warning: Time discipline is essential.*\n\n"
                f"🔥 Ignoring limits weakens your focus and respect for work.",
                parse_mode="Markdown"
            )
            return

    # Garde 7 : pauses interdites après 18h
    after_1840 = now.hour > 18 or (now.hour == 18 and now.minute >= 40)
    if new_state in BREAK_STATES and after_1840:
        label = LABELS.get(new_state, "break")
        emoji = EMOJIS.get(new_state, "⏸")
        await update.message.reply_text(
            f"⛔ {emoji} *{label.upper()} — Break not allowed after 18:40*\n\n"
            f"*🕕 It is currently {now.strftime('%H:%M')}.*\n\n"
            f"*20 minutes before the end of workday, you are expected to return to your seat.*\n\n"
            f"*Please stay at your seat until 19:00.*\n"
            f"🏆 Do the best you can!",
            parse_mode="Markdown"
        )
        return

    # ========================
    # START WORK
    # ========================
    if text == "🟢 Start Work":
        # Cas : déjà en train de travailler (même session, sans Off Work entre les deux)
        if u.get("state") == WORKING:
            await update.message.reply_text(
                "*⚠️ You have already STARTED WORK today.*\n\n"
                "Just work hard and take a break when you really need it.\n\n"
                "*⏰ But don't spend too much time on breaks, because time is precious.*",
                parse_mode="Markdown"
            )
            return

        # Calcul du retard (uniquement au tout premier Start Work de la journée)
        is_first_start = not u.get("started_today", False)
        retard_sec_val = 0
        retard_msg     = ""

        if is_first_start:
            start_h, start_m = CUSTOM_START_TIMES.get(username.lower(), DEFAULT_START_TIME)
            normal_start   = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
            diff           = (now - normal_start).total_seconds()
            retard_sec_val = max(diff, 0)
            u["retard_sec"]       = retard_sec_val
            u["first_work_start"] = now   # première arrivée du jour, jamais écrasée

            if retard_sec_val > 0:
                r_min = int(retard_sec_val) // 60
                r_sec = int(retard_sec_val) % 60
                if r_min and r_sec:
                    retard_str = f"{r_min} minutes and {r_sec} seconds"
                elif r_min:
                    retard_str = f"{r_min} minutes"
                else:
                    retard_str = f"{r_sec} seconds"
                retard_msg = (
                    f"\n\n⚠️ *LATE ARRIVAL*\n\n"
                    f"You started work at {now.strftime('%H:%M:%S')}, which is *{retard_str} late.*\n"
                    f"❗️ This lateness has been recorded and you are fined.\n"
                    f"👀 {managers_mention}"
                )
        # Si ce n'est pas le premier start (retour après Off Work), on garde retard_sec déjà stocké

        u["started_today"] = True
        u["work_start"]    = now    # début du segment en cours
        u["state"]         = WORKING

        # Ne pas reset les sessions ni break_counts — on accumule sur la journée
        if "work_segments" not in u:
            u["work_segments"] = []

        await update.message.reply_text(
            f"🟢 START WORK\n\n"
            f"👤User: {mention}\n"
            f"🪪 User ID: {user_id}\n"
            f"✅ Check-in successful: Work start saved.\n\n"
            f"📅Date : {now.strftime('%d/%m/%Y')}\n"
            f"👤Name : {name}\n"
            f"🚀Start : {now.strftime('%H:%M:%S')}\n\n"
            f"──────────────────────\n"
            f"📋 Break allowance for today :\n\n"
            f"🍽  EAT           — 3x × 40 minutes\n"
            f"🚽 Big toilet   — 2x × 20 minutes\n"
            f"🚬 Smoke         — 5x × 7 minutes\n"
            f"🚻 Small toilet — 6x × 7 minutes\n"
            f"──────────────────────\n"
            f"💸 Yo, stay sharp — chase that paper and make today count!"
            + retard_msg,
            parse_mode="Markdown"
        )

        if save_to_db:
            save_event(user_id, name, username, WORKING)
            stats = _compute_user_stats(u, now)
            asyncio.create_task(_async_save_daily(user_id, name, username, stats))
        return

    # ========================
    # BREAK START
    # ========================
    if new_state in BREAK_STATES:
        u["break_start"] = now
        u["break_type"]  = new_state
        u["state"]       = new_state


        label     = LABELS[new_state]
        emoji     = EMOJIS[new_state]
        limit_min = LIMITS[new_state] // 60
        current_count = u.get("break_counts", {}).get(new_state, 0) + 1
        ordinals  = {1: "1st", 2: "2nd", 3: "3rd"}
        ordinal   = ordinals.get(current_count, f"{current_count}th")

        # Label contextuel pour les repas
        meal_labels = {1: "🌅 Breakfast", 2: "☀️ Lunch", 3: "🌙 Dinner"}
        meal_note   = ""
        if new_state == BREAK_FOOD:
            meal_name = meal_labels.get(current_count, f"Meal #{current_count}")
            meal_note = f"🍽 Meal type   : *{meal_name}*\n"

        await update.message.reply_text(
            f"🟡 {label.upper()} START\n\n"
            f"✅ Check-in successful: {label} saved.\n"
            f"📝Note: This is your {ordinal} time using the {label.lower()}.\n"
            f"{meal_note}\n"
            f"📅Date : {now.strftime('%d/%m/%Y')}\n"
            f"👤Name : {name}\n"
            f"🚀Start : {now.strftime('%H:%M:%S')}\n"
            f"⏳Limit : {limit_min} minutes\n"
            f"──────────────────────\n"
            f"*⏰ But don't spend too much time on breaks, because time is precious.*\n"
            f"Late returning to your seat will be fined.",
            parse_mode="Markdown"
        )

        if u.get("warn_task") and not u["warn_task"].done():
            u["warn_task"].cancel()

        u["warn_task"] = asyncio.create_task(
            _send_break_warning(
                bot=context.bot,
                chat_id=chat.id,
                user_id=user_id,
                name=name,
                username=username,
                b_type=new_state,
                break_start=now,
                u=u
            )
        )

        if save_to_db:
            save_event(user_id, name, username, new_state)
            stats = _compute_user_stats(u, now)
            asyncio.create_task(_async_save_daily(user_id, name, username, stats))
        return

    # ========================
    # BACK TO SEAT
    # ========================
    if text == "🔵 BACK TO SEAT":
        b_start      = u["break_start"]
        b_type       = u["break_type"]
        end          = now
        duration_sec = (end - b_start).total_seconds()
        exceeded, extra = check_break_limit(b_type, duration_sec)

        if "break_counts" not in u:
            u["break_counts"] = {}
        u["break_counts"][b_type] = u["break_counts"].get(b_type, 0) + 1

        msg = build_back_to_seat_msg(
            name=name, username=username, user_id=user_id,
            now=end, b_start=b_start, b_type=b_type,
            duration_sec=duration_sec, exceeded=exceeded,
            extra=extra or 0, sessions=u["sessions"]
        )

        await update.message.reply_text(msg)

        u["sessions"].append({
            "type":     b_type,
            "start":    b_start,
            "end":      end,
            "duration": duration_sec,
            "exceeded": exceeded
        })

        if u.get("warn_task") and not u["warn_task"].done():
            u["warn_task"].cancel()
        u["warn_task"]   = None
        u["break_start"] = None
        u["break_type"]  = None
        u["state"]       = WORKING

        if save_to_db:
            save_event(user_id, name, username, "BACK_TO_SEAT")
            stats = _compute_user_stats(u, now)
            asyncio.create_task(_async_save_daily(user_id, name, username, stats))
        return

    # ========================
    # OFF WORK
    # ========================
    if new_state == OFF_SHIFT:
        if not u["work_start"]:
            await update.message.reply_text("⚠️ No work session started")
            return

        end_time   = now
        start_time = u["work_start"]
        seg_sec    = (end_time - start_time).total_seconds()

        # Enregistre le segment de travail qui se termine
        if "work_segments" not in u:
            u["work_segments"] = []
        u["work_segments"].append({
            "start":    start_time,
            "end":      end_time,
            "duration": seg_sec
        })

        # Calcul du total sur toute la journée
        work_sec  = sum(s["duration"] for s in u["work_segments"])
        break_sec = sum(s["duration"] for s in u["sessions"])
        net_sec   = max(work_sec - break_sec, 0)

        # ── Blocage Off Work avant 18h27 ──
        normal_end  = end_time.replace(hour=18, minute=59, second=0, microsecond=0)
        diff_depart = (normal_end - end_time).total_seconds()

        if end_time < normal_end:
            da_min = int(diff_depart) // 60
            da_sec = int(diff_depart) % 60
            if da_min and da_sec:
                da_str = f"{da_min} minutes and {da_sec} seconds"
            elif da_min:  
                da_str = f"{da_min} minutes"
            else:
                da_str = f"{da_sec} seconds"

            # Annuler l'ajout du segment qu'on vient d'ajouter par erreur
            u["work_segments"].pop()

            await update.message.reply_text(
                f"⛔ *OFF WORK — Too early*\n\n"
                f"🕐 It is currently *{end_time.strftime('%H:%M')}*.\n"
                f"🏁 Normal end of shift: *19:00*\n\n"
                f"You still have *{da_str}* left before your shift ends.",
                parse_mode="Markdown"
            )
            return

        # ── Calcul stats complet avant reset ──
        if save_to_db:
            stats = _compute_user_stats(u, end_time)
            stats["heure_depart"]        = end_time.strftime("%H:%M")
            stats["statut"]              = "Absent"
            stats["temps_travail_sec"]   = int(work_sec)
            stats["temps_effectif_sec"]  = int(net_sec)
            stats["depart_anticipe_min"] = max(int(diff_depart / 60), 0)
            save_event(user_id, name, username, OFF_SHIFT)
            asyncio.create_task(_async_save_daily(user_id, name, username, stats))
        type_stats = defaultdict(
            lambda: {"count": 0, "total_sec": 0.0, "exceeded_sec": 0.0}
        )
        for s in u["sessions"]:
            st = type_stats[s["type"]]
            st["count"]     += 1
            st["total_sec"] += s["duration"]
            if s["exceeded"]:
                limit = LIMITS.get(s["type"], 0)
                st["exceeded_sec"] += max(s["duration"] - limit, 0)

        break_lines = ""
        for btype, st in type_stats.items():
            label     = LABELS.get(btype, btype)
            total_str = fmt_duration(st["total_sec"])
            line = f"  • {label} — {st['count']}x — Total : {total_str}"
            if st["exceeded_sec"] > 0:
                line += f" ⚠️ Over limit : +{fmt_duration(st['exceeded_sec'])}"
            break_lines += line + "\n"

        if not break_lines:
            break_lines = "  • No breaks taken\n"

        eat_count    = type_stats[BREAK_FOOD]["count"]
        toilet_count = (type_stats[BREAK_TOILET_SMALL]["count"]
                        + type_stats[BREAK_TOILET_BIG]["count"])
        smoke_count  = type_stats[BREAK_SMOKE]["count"]

        # Heure d'arrivée = première arrivée du jour
        first_start = u.get("first_work_start") or start_time

        msg = (
            f"👤 User : {name}\n"
            f"🪪 ID   : {user_id}\n\n"
            f"✅ {now.strftime('%d/%m %H:%M:%S')} Work time for today saved.\n\n"
            f"📅 Date  : {now.strftime('%d/%m/%Y')}\n"
            f"🕐 Start : {first_start.strftime('%H:%M:%S')}\n"
            f"🕐 End   : {end_time.strftime('%H:%M:%S')}\n\n"
            f"⏱ Total shift: {fmt_duration(work_sec)}\n"
            f"☕ Total breaks: {fmt_duration(break_sec)}\n"
            f"💼 Total work time: {fmt_duration(net_sec)}\n\n"
            f"📋 Break detail :\n"
            f"{break_lines}\n"
            f"──────────────────────\n"
            f"🍽  Meals today         : {eat_count}X\n"
            f"🚻 Toilet breaks today : {toilet_count}X\n"
            f"🚬 Smoke breaks today  : {smoke_count}X\n"
            f"──────────────────────\n\n"
            f"*💸 Yo, stay sharp — chase that paper and make today count!*"
        )

        await update.message.reply_text(msg, parse_mode="Markdown")

        # Mettre l'état à OFF_SHIFT sans effacer l'historique du jour
        u["state"]       = OFF_SHIFT
        u["work_start"]  = None
        u["break_start"] = None
        u["break_type"]  = None
        if u.get("warn_task") and not u["warn_task"].done():
            u["warn_task"].cancel()
        u["warn_task"] = None
        return


# =========================
# API /api/stats — pour l'application web React
# =========================
def build_stats_payload() -> dict:
    """
    Construit le JSON complet de toutes les données en mémoire
    pour l'application web. Appelé par l'endpoint /api/stats.
    """
    now   = now_madagascar()
    today = today_madagascar().isoformat()
    result = []

    with users_lock:
        snapshot = {uid: dict(data) for uid, data in users.items()}

    for user_id, data in snapshot.items():
        meta     = data.get("metadata", {})
        name     = meta.get("full_name", "Unknown")
        username = meta.get("username", "N/A")

        for store_key, ctx in data.get("contexts", {}).items():
            expected_key = f"group_{ALLOWED_GROUP_ID}"
            if store_key != expected_key:
                continue

            state        = ctx.get("state", OFF_SHIFT)
            work_start   = ctx.get("work_start")
            first_work_start = ctx.get("first_work_start")
            sessions     = ctx.get("sessions", [])
            work_segments = ctx.get("work_segments", [])
            break_counts = ctx.get("break_counts", {})

            if state == WORKING:
                statut = "present"
            elif state in BREAK_STATES:
                statut = "pause"
            else:
                statut = "absent"

            current_break_type     = ctx.get("break_type")
            current_break_start    = ctx.get("break_start")
            current_break_elapsed  = None
            current_break_limit    = None
            current_break_exceeded = False
            if current_break_start and current_break_type:
                elapsed = (now - current_break_start).total_seconds()
                limit   = LIMITS.get(current_break_type, 0)
                current_break_elapsed  = int(elapsed)
                current_break_limit    = limit
                current_break_exceeded = elapsed > limit

            work_total_sec = sum(s["duration"] for s in work_segments)
            if work_start and state in ({WORKING} | BREAK_STATES):
                work_total_sec += (now - work_start).total_seconds()

            break_total_sec = sum(s["duration"] for s in sessions)
            if current_break_start and state in BREAK_STATES:
                break_total_sec += (now - current_break_start).total_seconds()

            effective_sec = max(work_total_sec - break_total_sec, 0)

            overdue_sec = 0.0
            for s in sessions:
                if s.get("exceeded"):
                    limit = LIMITS.get(s["type"], 0)
                    overdue_sec += max(s["duration"] - limit, 0)

            retard_sec_val = ctx.get("retard_sec", 0)
            retard_min = int(retard_sec_val // 60)
            retard_sec_display = int(retard_sec_val % 60)

            depart_anticipe_min = 0

            def pause_stats(btype):
                count = break_counts.get(btype, 0)
                total = sum(s["duration"] for s in sessions if s["type"] == btype)
                if current_break_type == btype and current_break_start:
                    total += (now - current_break_start).total_seconds()
                return {"count": count, "duree_sec": int(total)}

            entry = {
                "user_id":   user_id,
                "nom":       name,
                "username":  username,
                "statut":    statut,

                "heure_arrivee":   (first_work_start or work_start).strftime("%H:%M") if (first_work_start or work_start) else "--:--",
                "heure_depart":    "--:--",

                "retard_min":         retard_min,
                "depart_anticipe_min": depart_anticipe_min,

                "temps_travail_sec":  int(work_total_sec),
                "temps_effectif_sec": int(effective_sec),
                "pauses_total_sec":   int(break_total_sec),
                "depassements_sec":   int(overdue_sec),

                "pause_repas":       pause_stats(BREAK_FOOD),
                "pauses_courtes":    pause_stats(BREAK_TOILET_SMALL),
                "pauses_longues":    pause_stats(BREAK_TOILET_BIG),
                "pauses_cigarette":  pause_stats(BREAK_SMOKE),

                "departs_temporaires": {
                    "count": len(sessions),
                    "duree_sec": sum(int(s["duration"]) for s in sessions)
                },

                "activites_total_sec": int(work_total_sec),

                "pause_courante": {
                    "type":     current_break_type,
                    "label":    LABELS.get(current_break_type, "") if current_break_type else "",
                    "elapsed":  current_break_elapsed,
                    "limit":    current_break_limit,
                    "exceeded": current_break_exceeded
                } if current_break_type else None,

                "sessions": [
                    {
                        "type":     s["type"],
                        "label":    LABELS.get(s["type"], s["type"]),
                        "start":    s["start"].strftime("%H:%M:%S") if s.get("start") else "",
                        "end":      s["end"].strftime("%H:%M:%S") if s.get("end") else "",
                        "duration": int(s["duration"]),
                        "exceeded": s.get("exceeded", False)
                    }
                    for s in sessions
                ],

                "last_update": now.strftime("%H:%M:%S")
            }

            result.append(entry)

    return {
        "timestamp": now.isoformat(),
        "date":      today,
        "employes":  result
    }


# =========================
# SERVEUR WEB — Render Web Service
# =========================
async def run_web_server():
    async def serve_index(request):
        index_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
        if os.path.exists(index_path):
            return web.FileResponse(index_path)
        return web.Response(text="BOT-MANEMAN OK")

    async def health(request):
        return web.Response(text="BOT-MANEMAN OK")

    async def api_stats(request):
        try:
            payload = build_stats_payload()
            return web.Response(
                text=json.dumps(payload, ensure_ascii=False, default=str),
                content_type="application/json",
                headers={
                    "Access-Control-Allow-Origin":  "*",
                    "Access-Control-Allow-Methods": "GET, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type",
                    "Cache-Control":                "no-cache"
                }
            )
        except Exception as e:
            logger.error(f"Erreur /api/stats : {e}", exc_info=True)
            return web.Response(
                text=json.dumps({"error": str(e)}),
                content_type="application/json",
                status=500,
                headers={"Access-Control-Allow-Origin": "*"}
            )

    async def api_stats_options(request):
        return web.Response(
            headers={
                "Access-Control-Allow-Origin":  "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type"
            }
        )

    app_web = web.Application()
    app_web.router.add_get("/",              serve_index)
    app_web.router.add_get("/health",        health)
    app_web.router.add_get("/api/stats",     api_stats)
    app_web.router.add_options("/api/stats", api_stats_options)

    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"✅ Web server listening on port {PORT}")
    print(f"✅ API endpoint : http://0.0.0.0:{PORT}/api/stats")
    await asyncio.Event().wait()

# =========================
# BOT POLLING
# =========================
async def run_bot():
    tg_app = (
        ApplicationBuilder()
        .token(TOKEN)
        .get_updates_connect_timeout(10)
        .get_updates_read_timeout(10)
        .get_updates_write_timeout(10)
        .get_updates_pool_timeout(10)
        .build()
    )

    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle))
    tg_app.add_error_handler(error_handler)

    async with tg_app:
        await tg_app.start()
        await tg_app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
        print("✅ Bot started...")
        await asyncio.Event().wait()
        await tg_app.updater.stop()
        await tg_app.stop()

# =========================
# MAIN
# =========================
def main():
    if not TOKEN:
        print("❌ BOT_TOKEN manquant dans le fichier .env")
        return

    reset_telegram_session()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    loop.run_until_complete(
        asyncio.gather(
            run_web_server(),
            run_bot()
        )
    )


if __name__ == "__main__":
    main()
