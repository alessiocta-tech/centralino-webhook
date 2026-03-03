# =========================
# PATCH: CONTROLLO DATA “NELLO STESSO TOOL”
# - Aggiunge fasi: time_now, resolve_date
# - Rende data/orario/persone NON obbligatori per queste fasi
# - Sposta le validazioni data/orario/persone SOLO su availability/book
# =========================

import os
import re
import json
import sqlite3
from datetime import datetime, timedelta, timezone, date
from typing import Optional, Union, List, Dict, Any, Tuple

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field, root_validator
from playwright.async_api import async_playwright

# ============================================================
# TIMEZONE (CRASH-PROOF) — CRITICO PER "OGGI/DOMANI/STASERA"
# ============================================================

try:
    from zoneinfo import ZoneInfo  # py>=3.9
except Exception:
    ZoneInfo = None  # type: ignore


def _load_tz():
    """
    Prova Europe/Rome (DST corretto). Se non disponibile (tzdata mancante),
    fallback su timezone locale del container, altrimenti CET fisso (+01:00).
    """
    if ZoneInfo is not None:
        try:
            return ZoneInfo("Europe/Rome")
        except Exception:
            pass
    try:
        return datetime.now().astimezone().tzinfo or timezone(timedelta(hours=1))
    except Exception:
        return timezone(timedelta(hours=1))


TZ = _load_tz()

MONTHS_IT = [
    "", "Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"
]
WEEKDAYS_IT = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]

WEEKDAY_MAP = {
    "lunedi": 0, "lunedì": 0,
    "martedi": 1, "martedì": 1,
    "mercoledi": 2, "mercoledì": 2,
    "giovedi": 3, "giovedì": 3,
    "venerdi": 4, "venerdì": 4,
    "sabato": 5,
    "domenica": 6,
}

# ============================================================
# CONFIG
# ============================================================

BOOKING_URL = os.getenv("BOOKING_URL", "https://rione.fidy.app/prenew.php?referer=AI")

PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "60000"))
PW_NAV_TIMEOUT_MS = int(os.getenv("PW_NAV_TIMEOUT_MS", "60000"))
DISABLE_FINAL_SUBMIT = os.getenv("DISABLE_FINAL_SUBMIT", "false").lower() == "true"

DEBUG_ECHO_PAYLOAD = os.getenv("DEBUG_ECHO_PAYLOAD", "false").lower() == "true"
DEBUG_LOG_AJAX_POST = os.getenv("DEBUG_LOG_AJAX_POST", "false").lower() == "true"

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
DATA_DIR = os.getenv("DATA_DIR", "/tmp")
DB_PATH = os.path.join(DATA_DIR, "centralino.sqlite3")

MAX_SLOT_RETRIES = int(os.getenv("MAX_SLOT_RETRIES", "2"))
MAX_SUBMIT_RETRIES = int(os.getenv("MAX_SUBMIT_RETRIES", "1"))
RETRY_TIME_WINDOW_MIN = int(os.getenv("RETRY_TIME_WINDOW_MIN", "90"))

AVAIL_SELECTOR_TIMEOUT_MS = int(os.getenv("AVAIL_SELECTOR_TIMEOUT_MS", str(PW_TIMEOUT_MS)))
AVAIL_FUNCTION_TIMEOUT_MS = int(os.getenv("AVAIL_FUNCTION_TIMEOUT_MS", "60000"))
AVAIL_POST_WAIT_MS = int(os.getenv("AVAIL_POST_WAIT_MS", "1200"))

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

DEFAULT_EMAIL = os.getenv("DEFAULT_EMAIL", "default@prenotazioni.com")

app = FastAPI()

# ============================================================
# DB (dashboard + memoria)
# ============================================================

def _db() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _db_init() -> None:
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts TEXT NOT NULL,
          phone TEXT,
          name TEXT,
          email TEXT,
          sede TEXT,
          data TEXT,
          orario TEXT,
          persone INTEGER,
          seggiolini INTEGER,
          note TEXT,
          ok INTEGER,
          message TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS customers (
          phone TEXT PRIMARY KEY,
          name TEXT,
          email TEXT,
          last_sede TEXT,
          last_persone INTEGER,
          last_seggiolini INTEGER,
          last_note TEXT,
          updated_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


_db_init()

def _log_booking(payload: Dict[str, Any], ok: bool, message: str) -> None:
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO bookings (ts, phone, name, email, sede, data, orario, persone, seggiolini, note, ok, message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now(TZ).isoformat(),
            payload.get("telefono"),
            payload.get("nome"),
            payload.get("email"),
            payload.get("sede"),
            payload.get("data"),
            payload.get("orario"),
            payload.get("persone"),
            payload.get("seggiolini"),
            payload.get("note"),
            1 if ok else 0,
            (message or "")[:5000],
        ),
    )
    conn.commit()
    conn.close()


def _upsert_customer(
    phone: str,
    name: str,
    email: str,
    sede: str,
    persone: int,
    seggiolini: int,
    note: str,
) -> None:
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO customers (phone, name, email, last_sede, last_persone, last_seggiolini, last_note, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET
          name=excluded.name,
          email=excluded.email,
          last_sede=excluded.last_sede,
          last_persone=excluded.last_persone,
          last_seggiolini=excluded.last_seggiolini,
          last_note=excluded.last_note,
          updated_at=excluded.updated_at
        """,
        (
            phone,
            name,
            email,
            sede,
            persone,
            seggiolini,
            note,
            datetime.now(TZ).isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def _get_customer(phone: str) -> Optional[Dict[str, Any]]:
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM customers WHERE phone = ?", (phone,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

# ============================================================
# NORMALIZZAZIONI
# ============================================================

def _norm_orario(s: str) -> str:
    s = (s or "").strip().lower().replace("ore", "").replace("alle", "").strip()
    s = s.replace(".", ":").replace(",", ":")
    if re.fullmatch(r"\d{1,2}$", s):
        return f"{int(s):02d}:00"
    if re.fullmatch(r"\d{1,2}:\d{2}$", s):
        hh, mm = s.split(":")
        return f"{int(hh):02d}:{int(mm):02d}"
    return s


def _calcola_pasto(orario_hhmm: str) -> str:
    try:
        hh = int(orario_hhmm.split(":")[0])
        return "PRANZO" if hh < 17 else "CENA"
    except Exception:
        return "CENA"


def _get_data_type(data_str: str) -> str:
    try:
        data_pren = datetime.strptime(data_str, "%Y-%m-%d").date()
        oggi = datetime.now(TZ).date()
        domani = oggi + timedelta(days=1)
        if data_pren == oggi:
            return "Oggi"
        if data_pren == domani:
            return "Domani"
        return "Altra"
    except Exception:
        return "Altra"


def _normalize_sede(s: str) -> str:
    s0 = (s or "").strip().lower()
    mapping = {
        "talenti": "Talenti",
        "talenti - roma": "Talenti",
        "talenti roma": "Talenti",
        "roma talenti": "Talenti",
        "ostia": "Ostia Lido",
        "ostia lido": "Ostia Lido",
        "ostia lido - roma": "Ostia Lido",
        "appia": "Appia",
        "reggio": "Reggio Calabria",
        "reggio calabria": "Reggio Calabria",
        "palermo": "Palermo",
        "palermo centro": "Palermo",
    }
    return mapping.get(s0, (s or "").strip())


def _suggest_alternative_sedi(target: str, sedi: List[Dict[str, Any]]) -> List[str]:
    target_n = _normalize_sede(target)
    order_map = {
        "Talenti": ["Appia", "Ostia Lido", "Palermo", "Reggio Calabria"],
        "Appia": ["Talenti", "Ostia Lido", "Palermo", "Reggio Calabria"],
        "Ostia Lido": ["Talenti", "Appia", "Palermo", "Reggio Calabria"],
        "Palermo": ["Reggio Calabria", "Talenti", "Appia", "Ostia Lido"],
        "Reggio Calabria": ["Palermo", "Talenti", "Appia", "Ostia Lido"],
    }
    pref = order_map.get(target_n, [])
    sold = {_normalize_sede(x.get("nome", "")): bool(x.get("tutto_esaurito")) for x in (sedi or [])}
    out: List[str] = []
    for s in pref:
        if sold.get(_normalize_sede(s), False):
            continue
        out.append(s)
    for x in (sedi or []):
        n = _normalize_sede(x.get("nome", ""))
        if n == target_n:
            continue
        if sold.get(n, False):
            continue
        if n not in out:
            out.append(n)
    return out


def _time_to_minutes(hhmm: str) -> Optional[int]:
    m = re.fullmatch(r"(\d{2}):(\d{2})", hhmm or "")
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))

# ============================================================
# DATE RELATIVE (RIUSO)
# ============================================================

class ResolveDateIn(BaseModel):
    input_text: str


class ResolveDateOut(BaseModel):
    ok: bool = True
    date_iso: str
    weekday_spoken: str
    day_number: int
    month_spoken: str
    requires_confirmation: bool
    matched_rule: str


def _today_local() -> date:
    return datetime.now(TZ).date()


def _next_weekday(d: date, target_wd: int) -> date:
    days_ahead = (target_wd - d.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return d + timedelta(days=days_ahead)


def _this_or_next_weekend(d: date) -> date:
    return _next_weekday(d, 5)


def _format_out(d: date, requires: bool, rule: str) -> ResolveDateOut:
    return ResolveDateOut(
        date_iso=d.isoformat(),
        weekday_spoken=WEEKDAYS_IT[d.weekday()],
        day_number=d.day,
        month_spoken=MONTHS_IT[d.month],
        requires_confirmation=requires,
        matched_rule=rule,
    )


def _resolve_date_text(text: str) -> ResolveDateOut:
    """
    Versione “callabile” internamente, senza endpoint separato.
    """
    t0 = (text or "").strip().lower()
    if not t0:
        raise ValueError("input_text required")

    t = re.sub(r"\s+", " ", t0)
    today = _today_local()

    if "stasera" in t or re.search(r"\boggi\b", t):
        return _format_out(today, requires=False, rule="stasera/oggi")

    if "dopodomani" in t:
        return _format_out(today + timedelta(days=2), True, "dopodomani")
    if "domani" in t:
        return _format_out(today + timedelta(days=1), True, "domani")

    if "weekend" in t:
        return _format_out(_this_or_next_weekend(today), True, "weekend->sabato")

    for key, wd in WEEKDAY_MAP.items():
        if re.search(rf"\b{re.escape(key)}\b", t):
            d = _next_weekday(today, wd)
            return _format_out(d, True, f"weekday:{key}")

    raise ValueError("Unrecognized relative date expression")


# ============================================================
# MODEL BOOKING (PATCH)
# ============================================================

class RichiestaPrenotazione(BaseModel):
    # ora supporta anche time_now / resolve_date
    fase: str = Field("book", description='Fase: "time_now", "resolve_date", "availability", "book"')

    # per resolve_date
    input_text: Optional[str] = ""

    nome: Optional[str] = ""
    cognome: Optional[str] = ""
    email: Optional[str] = ""
    telefono: Optional[str] = ""

    sede: Optional[str] = ""

    # PATCH: diventano opzionali (necessari SOLO per availability/book)
    data: Optional[str] = ""
    orario: Optional[str] = ""
    persone: Optional[Union[int, str]] = None

    seggiolini: Union[int, str] = 0  # clamp 0..3 (server)
    note: Optional[str] = Field("", alias="nota")

    model_config = {"validate_by_name": True, "extra": "ignore"}

    @root_validator(pre=True)
    def _coerce_fields(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        if values.get("note") not in (None, ""):
            values["nota"] = values.get("note")

        if not values.get("fase"):
            values["fase"] = "book"
        values["fase"] = str(values["fase"]).strip().lower()

        # persone
        p = values.get("persone")
        if isinstance(p, str):
            p2 = re.sub(r"[^\d]", "", p)
            if p2:
                values["persone"] = int(p2)

        # seggiolini
        s = values.get("seggiolini")
        if isinstance(s, str):
            s2 = re.sub(r"[^\d]", "", s)
            values["seggiolini"] = int(s2) if s2 else 0
        try:
            values["seggiolini"] = max(0, min(3, int(values.get("seggiolini") or 0)))
        except Exception:
            values["seggiolini"] = 0

        # orario normalize
        if values.get("orario") is not None:
            values["orario"] = _norm_orario(str(values.get("orario") or ""))

        # sede normalize
        if values.get("sede") is not None:
            values["sede"] = _normalize_sede(str(values.get("sede") or ""))

        # telefono digits
        if values.get("telefono") is not None:
            values["telefono"] = re.sub(r"[^\d]", "", str(values.get("telefono") or ""))

        # email fallback
        if not values.get("email"):
            values["email"] = DEFAULT_EMAIL

        values["nome"] = (values.get("nome") or "").strip()
        values["cognome"] = (values.get("cognome") or "").strip()

        # input_text normalize
        values["input_text"] = (values.get("input_text") or "").strip()

        # data/orario normalize
        values["data"] = (values.get("data") or "").strip()
        values["orario"] = (values.get("orario") or "").strip()

        return values

# ============================================================
# PLAYWRIGHT HELPERS
# (tutto uguale: non modifico qui)
# ============================================================
# ... (lascia invariato tutto il blocco helpers dal tuo file) ...


# ============================================================
# ROUTES
# ============================================================

@app.get("/")
def home():
    return {
        "status": "Centralino AI - Booking Engine (Railway)",
        "disable_final_submit": DISABLE_FINAL_SUBMIT,
        "db": DB_PATH,
        "tz": str(getattr(TZ, "key", "LOCAL_OR_CET")),
    }


def _is_timeout_error(err: str) -> bool:
    s = (err or "").lower()
    return ("timeout" in s) or ("exceeded" in s)


@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione, request: Request):
    if DEBUG_ECHO_PAYLOAD:
        try:
            raw = await request.json()
            print("🧾 RAW_PAYLOAD:", json.dumps(raw, ensure_ascii=False))
        except Exception:
            pass

    fase = (dati.fase or "book").strip().lower()

    # ============================================================
    # PATCH: FASE time_now (prima di qualunque validazione)
    # ============================================================
    if fase == "time_now":
        now = datetime.now(TZ)
        return {
            "ok": True,
            "fase": "time_now",
            "tz": str(getattr(TZ, "key", "LOCAL_OR_CET")),
            "now_iso": now.isoformat(),
            "date_iso": now.date().isoformat(),
            "weekday": WEEKDAYS_IT[now.weekday()],
        }

    # ============================================================
    # PATCH: FASE resolve_date (prima di qualunque validazione)
    # ============================================================
    if fase == "resolve_date":
        try:
            out = _resolve_date_text(dati.input_text or "")
            return out.model_dump()
        except Exception as e:
            return {"ok": False, "fase": "resolve_date", "status": "VALIDATION_ERROR", "message": str(e)}

    # ============================================================
    # Da qui in poi: solo availability/book
    # ============================================================
    if fase not in ("availability", "book"):
        msg = f'Valore fase non valido: {dati.fase}. Usa "time_now", "resolve_date", "availability" oppure "book".'
        _log_booking(dati.model_dump(), False, msg)
        return {"ok": False, "status": "VALIDATION_ERROR", "message": msg}

    # --- Validazioni base SOLO per availability/book ---
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", (dati.data or "")):
        msg = f"Formato data non valido: {dati.data}. Usa YYYY-MM-DD."
        _log_booking(dati.model_dump(), False, msg)
        return {"ok": False, "status": "VALIDATION_ERROR", "message": msg}

    if not re.fullmatch(r"\d{2}:\d{2}", (dati.orario or "")):
        msg = f"Formato orario non valido: {dati.orario}. Usa HH:MM."
        _log_booking(dati.model_dump(), False, msg)
        return {"ok": False, "status": "VALIDATION_ERROR", "message": msg}

    if not isinstance(dati.persone, int) or dati.persone < 1 or dati.persone > 50:
        msg = f"Numero persone non valido: {dati.persone}."
        _log_booking(dati.model_dump(), False, msg)
        return {"ok": False, "status": "VALIDATION_ERROR", "message": msg}

    # Oltre 9 persone -> handoff
    if int(dati.persone) > 9:
        msg = "Per tavoli da più di 9 persone gestiamo la divisione gruppi: contatta il centralino 06 56556 263."
        _log_booking(dati.model_dump(), False, msg)
        return {"ok": False, "status": "HANDOFF", "message": msg, "handoff": True, "phone": "06 56556 263"}

    # In fase book: sede + nome + telefono obbligatori
    if fase == "book":
        if not (dati.sede or "").strip():
            msg = "Sede mancante."
            _log_booking(dati.model_dump(), False, msg)
            return {"ok": False, "status": "VALIDATION_ERROR", "message": msg}
        if not (dati.nome or "").strip():
            msg = "Nome mancante."
            _log_booking(dati.model_dump(), False, msg)
            return {"ok": False, "status": "VALIDATION_ERROR", "message": msg}
        tel_clean = re.sub(r"[^\d]", "", dati.telefono or "")
        if len(tel_clean) < 6:
            msg = "Telefono mancante o non valido."
            _log_booking(dati.model_dump(), False, msg)
            return {"ok": False, "status": "VALIDATION_ERROR", "message": msg}

    # ============================================================
    # DA QUI: TUTTO IL TUO CODICE ESISTENTE (INVARIATO)
    # usa:
    sede_target = (dati.sede or "").strip()
    orario_req = (dati.orario or "").strip()
    data_req = (dati.data or "").strip()
    pax_req = int(dati.persone)
    pasto = _calcola_pasto(orario_req)
    # ecc...
    # ============================================================

    # >>> INCOLLA QUI ESATTAMENTE IL RESTO DEL TUO /book_table ATTUALE
    #     (tutto identico dal punto: sede_target=... fino al finally)

    raise HTTPException(status_code=500, detail="Patch applicata: incolla qui il resto del handler /book_table.")
