# =============================================================
# BOT-MANEMAN — sheets.py
# Compatible local (credentials.json) ET Render (GOOGLE_CREDENTIALS)
# Feuilles : events | daily
#   events : journal brut de chaque action
#   daily  : synthèse détaillée par employé/jour (upsert en temps réel)
# =============================================================

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timezone, timedelta
import os
import json

# =========================
# CONFIG
# =========================
MADAGASCAR_TZ = timezone(timedelta(hours=3))

HEADERS_EVENTS = [
    "timestamp",
    "user_id",
    "full_name",
    "username",
    "event_type",
    "date"
]

HEADERS_DAILY = [
    "date",
    "user_id",
    "full_name",
    "username",
    "statut",
    "heure_arrivee",
    "heure_depart",
    "retard_min",
    "depart_anticipe_min",
    "temps_travail",
    "temps_effectif",
    "pause_repas_count",
    "pause_repas_duree",
    "pauses_courtes_count",
    "pauses_courtes_duree",
    "pauses_longues_count",
    "pauses_longues_duree",
    "pauses_cigarette_count",
    "pauses_cigarette_duree",
    "departs_temp_count",
    "departs_temp_duree",
    "activites_total",
    "depassements",
    "last_update"
]

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

sheet_events = None
sheet_daily  = None


# =========================
# FORMATTER DURÉE
# =========================
def _fmt_sec(total_sec) -> str:
    total_sec = int(total_sec or 0)
    if total_sec <= 0:
        return "0min"
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    s = total_sec % 60
    if h:
        return f"{h}h{m:02d}min"
    if m and s:
        return f"{m}min {s}s"
    if m:
        return f"{m}min"
    return f"{s}s"


# =========================
# CONNEXION GOOGLE SHEETS
# =========================
def _ensure_headers(sheet, headers):
    first_row = sheet.row_values(1)
    if not first_row:
        sheet.append_row(headers)
        print(f"  ✅ En-têtes créés dans '{sheet.title}'")
    elif first_row != headers:
        print(f"  ⚠️  En-têtes incorrects dans '{sheet.title}' — attendu : {headers}")


def _get_or_create_worksheet(wb, title, headers):
    try:
        ws = wb.worksheet(title)
        print(f"  📋 Feuille '{title}' trouvée")
    except gspread.WorksheetNotFound:
        ws = wb.add_worksheet(title=title, rows=2000, cols=len(headers))
        print(f"  ✅ Feuille '{title}' créée")
    _ensure_headers(ws, headers)
    return ws


try:
    _gc_env = os.environ.get("GOOGLE_CREDENTIALS")
    if _gc_env:
        creds_dict = json.loads(_gc_env)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        print("🔑 Credentials chargées depuis GOOGLE_CREDENTIALS (env)")
    else:
        creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
        print("🔑 Credentials chargées depuis credentials.json (local)")

    client = gspread.authorize(creds)

    _spreadsheet_id = os.environ.get("SPREADSHEET_ID", "").strip()
    if "/d/" in _spreadsheet_id:
        _spreadsheet_id = _spreadsheet_id.split("/d/")[1].split("/")[0]

    if _spreadsheet_id:
        wb = client.open_by_key(_spreadsheet_id)
        print(f"📊 Spreadsheet ouvert par ID : {_spreadsheet_id}")
    else:
        SHEET_NAME = os.environ.get("SHEET_NAME", "robot-attedance-web-db")
        wb = client.open(SHEET_NAME)
        print(f"📊 Spreadsheet ouvert par nom : {SHEET_NAME}")

    sheet_events = _get_or_create_worksheet(wb, "events", HEADERS_EVENTS)
    sheet_daily  = _get_or_create_worksheet(wb, "daily",  HEADERS_DAILY)

    print("✅ Google Sheets connecté — robot-attedance-web-db")

except Exception as e:
    print(f"❌ Connexion Google Sheets échouée : {e}")


# =========================
# SAVE EVENT (journal brut)
# =========================
def save_event(user_id: int, full_name: str, username: str, event_type: str):
    if not sheet_events:
        return
    try:
        now       = datetime.now(MADAGASCAR_TZ)
        timestamp = now.strftime("%d/%m/%Y %H:%M:%S")
        date_str  = now.strftime("%d/%m/%Y")
        sheet_events.append_row([
            timestamp, str(user_id), full_name, username, event_type, date_str
        ])
    except Exception as e:
        print(f"❌ save_event error: {e}")


# =========================
# SAVE DAILY STATUS (upsert)
# =========================
def save_daily_status(user_id: int, full_name: str, username: str, stats: dict):
    """
    Insère ou met à jour la ligne quotidienne de l'employé dans la feuille 'daily'.
    stats keys attendues :
      statut, heure_arrivee, heure_depart,
      retard_min, depart_anticipe_min,
      temps_travail_sec, temps_effectif_sec,
      pause_repas_count, pause_repas_sec,
      pauses_courtes_count, pauses_courtes_sec,
      pauses_longues_count, pauses_longues_sec,
      pauses_cigarette_count, pauses_cigarette_sec,
      departs_temp_count, departs_temp_sec,
      activites_total_sec, depassements_sec
    """
    if not sheet_daily:
        return
    try:
        now      = datetime.now(MADAGASCAR_TZ)
        date_str = now.strftime("%d/%m/%Y")
        ts_str   = now.strftime("%H:%M:%S")

        dep_sec = stats.get("depassements_sec", 0)

        row = [
            date_str,
            str(user_id),
            full_name,
            username,
            stats.get("statut", "Absent"),
            stats.get("heure_arrivee", "--:--"),
            stats.get("heure_depart", "--:--"),
            stats.get("retard_min", 0),
            stats.get("depart_anticipe_min", 0),
            _fmt_sec(stats.get("temps_travail_sec", 0)),
            _fmt_sec(stats.get("temps_effectif_sec", 0)),
            stats.get("pause_repas_count", 0),
            _fmt_sec(stats.get("pause_repas_sec", 0)),
            stats.get("pauses_courtes_count", 0),
            _fmt_sec(stats.get("pauses_courtes_sec", 0)),
            stats.get("pauses_longues_count", 0),
            _fmt_sec(stats.get("pauses_longues_sec", 0)),
            stats.get("pauses_cigarette_count", 0),
            _fmt_sec(stats.get("pauses_cigarette_sec", 0)),
            stats.get("departs_temp_count", 0),
            _fmt_sec(stats.get("departs_temp_sec", 0)),
            _fmt_sec(stats.get("activites_total_sec", 0)),
            _fmt_sec(dep_sec) if dep_sec > 0 else "Aucun",
            ts_str
        ]

        # ── Upsert : cherche la ligne user_id + date ──
        all_values = sheet_daily.get_all_values()

        if not all_values:
            sheet_daily.append_row(row)
            return

        header = all_values[0]
        try:
            uid_col_idx  = header.index("user_id")
            date_col_idx = header.index("date")
        except ValueError:
            sheet_daily.append_row(row)
            return

        found_row_num = None
        for i, r in enumerate(all_values[1:], start=2):   # 1-indexed, skip header
            uid_val  = r[uid_col_idx]  if len(r) > uid_col_idx  else ""
            date_val = r[date_col_idx] if len(r) > date_col_idx else ""
            if uid_val == str(user_id) and date_val == date_str:
                found_row_num = i
                break

        col_end = chr(ord("A") + len(row) - 1)   # 'X' pour 24 colonnes

        if found_row_num:
            sheet_daily.update(
                f"A{found_row_num}:{col_end}{found_row_num}",
                [row]
            )
        else:
            sheet_daily.append_row(row)

    except Exception as e:
        print(f"❌ save_daily_status error: {e}")


# =========================
# HELPERS DE LECTURE
# =========================
def parse_time(t: str):
    try:
        return datetime.strptime(t, "%d/%m/%Y %H:%M:%S")
    except Exception:
        try:
            return datetime.fromisoformat(t)
        except Exception:
            return None


def get_history(user_id: int) -> list:
    if not sheet_events:
        return []
    try:
        data    = sheet_events.get_all_records()
        history = []
        for row in data:
            if str(row.get("user_id")) != str(user_id):
                continue
            ts = parse_time(row.get("timestamp", ""))
            if ts:
                history.append({"action": row.get("event_type"), "timestamp": ts})
        history.sort(key=lambda x: x["timestamp"])
        return history
    except Exception as e:
        print(f"❌ get_history error: {e}")
        return []
