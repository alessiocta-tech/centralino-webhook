# main.py
import os
import re
import json
import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone, date, time
from typing import Optional, Union, List, Dict, Any, Tuple

import httpx

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, model_validator, root_validator, validator
from playwright.async_api import async_playwright
try:
    from playwright_stealth import stealth_async as _stealth_async
    _STEALTH_AVAILABLE = True
except ImportError:
    _STEALTH_AVAILABLE = False

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
    "",
    "Gennaio",
    "Febbraio",
    "Marzo",
    "Aprile",
    "Maggio",
    "Giugno",
    "Luglio",
    "Agosto",
    "Settembre",
    "Ottobre",
    "Novembre",
    "Dicembre",
]
WEEKDAYS_IT = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]

WEEKDAY_MAP = {
    "lunedi": 0,
    "lunedì": 0,
    "martedi": 1,
    "martedì": 1,
    "mercoledi": 2,
    "mercoledì": 2,
    "giovedi": 3,
    "giovedì": 3,
    "venerdi": 4,
    "venerdì": 4,
    "sabato": 5,
    "domenica": 6,
}

# ============================================================
# CONFIG
# ============================================================

BOOKING_URL = os.getenv("BOOKING_URL", "https://rione.fidy.app/prenew.php?referer=AI")

PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "25000"))
PW_NAV_TIMEOUT_MS = int(os.getenv("PW_NAV_TIMEOUT_MS", "25000"))
DISABLE_FINAL_SUBMIT = os.getenv("DISABLE_FINAL_SUBMIT", "false").lower() == "true"

DEBUG_ECHO_PAYLOAD = os.getenv("DEBUG_ECHO_PAYLOAD", "false").lower() == "true"
DEBUG_LOG_AJAX_POST = os.getenv("DEBUG_LOG_AJAX_POST", "false").lower() == "true"

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
DATA_DIR = os.getenv("DATA_DIR", "/tmp")
DB_PATH = os.path.join(DATA_DIR, "centralino.sqlite3")

MAX_SLOT_RETRIES = int(os.getenv("MAX_SLOT_RETRIES", "2"))
MAX_SUBMIT_RETRIES = int(os.getenv("MAX_SUBMIT_RETRIES", "1"))
RETRY_TIME_WINDOW_MIN = int(os.getenv("RETRY_TIME_WINDOW_MIN", "90"))
BOOKING_TOTAL_TIMEOUT_S = int(os.getenv("BOOKING_TOTAL_TIMEOUT_S", "50"))

# Timeout specifici scraping availability (evita 30s hard-coded)
AVAIL_SELECTOR_TIMEOUT_MS = int(os.getenv("AVAIL_SELECTOR_TIMEOUT_MS", str(PW_TIMEOUT_MS)))
AVAIL_FUNCTION_TIMEOUT_MS = int(os.getenv("AVAIL_FUNCTION_TIMEOUT_MS", "20000"))
AVAIL_POST_WAIT_MS = int(os.getenv("AVAIL_POST_WAIT_MS", "1200"))

# AJAX wait (final response) — evita errore su MS_PS
AJAX_FINAL_TIMEOUT_MS = int(os.getenv("AJAX_FINAL_TIMEOUT_MS", "30000"))
PENDING_AJAX = set(
    x.strip().upper()
    for x in os.getenv("AJAX_PENDING_CODES", "MS_PS").split(",")
    if x.strip()
)

IPHONE_UA = os.getenv(
    "PLAYWRIGHT_UA",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36",
)

DEFAULT_EMAIL = os.getenv("DEFAULT_EMAIL", "default@prenotazioni.com")

# ============================================================
# FIDY API (check, cancel, update, note)
# ============================================================

FIDY_API_BASE = os.getenv("FIDY_API_BASE", "https://api.fidy.app/api")
FIDY_API_KEY = os.getenv("FIDY_API_KEY", "derione_api_2026_super_secret")
FIDY_TIMEOUT_S = int(os.getenv("FIDY_TIMEOUT_S", "20"))

# ============================================================
# ESERCIZI DB — connessione MySQL al database dei ristoranti
# ============================================================
ESERCIZI_DB_HOST = os.getenv("DB_HOST", os.getenv("ESERCIZI_DB_HOST", ""))
ESERCIZI_DB_PORT = int(os.getenv("DB_PORT", os.getenv("ESERCIZI_DB_PORT", "3306")))
ESERCIZI_DB_NAME = os.getenv("DB_NAME", os.getenv("ESERCIZI_DB_NAME", ""))
ESERCIZI_DB_USER = os.getenv("DB_USER", os.getenv("ESERCIZI_DB_USER", ""))
ESERCIZI_DB_PASS = os.getenv("DB_PASSWORD", os.getenv("DB_PASS", os.getenv("ESERCIZI_DB_PASS", "")))

SEDE_ID_MAP: Dict[str, int] = {
    "talenti": 1,
    "reggio": 2,
    "reggio calabria": 2,
    "rc": 2,
    "ostia": 3,
    "ostia lido": 3,
    "lido": 3,
    "appia": 4,
    "roma appia": 4,
    "palermo": 5,
    "corso trieste": 6,
    "trieste": 6,
}

_ID_TO_SEDE_NAME: Dict[int, str] = {
    1: "Talenti",
    2: "Reggio Calabria",
    3: "Ostia Lido",
    4: "Appia",
    5: "Palermo",
    6: "Corso Trieste",
}

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


def _lookup_last_booking(phone: str, data: str, orario: str) -> Optional[Dict[str, Any]]:
    """Cerca l'ultima prenotazione riuscita per phone+data+orario nell'archivio locale."""
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM bookings WHERE phone=? AND data=? AND orario=? AND ok=1 ORDER BY id DESC LIMIT 1",
        (phone, data, orario),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def _lookup_last_booking_by_date(phone: str, data: str) -> Optional[Dict[str, Any]]:
    """Cerca l'ultima prenotazione riuscita per phone+data (senza orario) nell'archivio locale."""
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM bookings WHERE phone=? AND data=? AND ok=1 ORDER BY id DESC LIMIT 1",
        (phone, data),
    )
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
    """
    Serve solo per capire se la UI Fidy mostra bottoni "Oggi/Domani".
    IMPORTANTISSIMO: usa timezone locale TZ.
    """
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
# MICROSERVIZIO: RISOLUZIONE DATE RELATIVE (ANTI-ERRORE LLM)
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


_MONTH_NAME_MAP: Dict[str, int] = {
    "gennaio": 1, "gen": 1,
    "febbraio": 2, "feb": 2,
    "marzo": 3, "mar": 3,
    "aprile": 4, "apr": 4,
    "maggio": 5, "mag": 5,
    "giugno": 6, "giu": 6,
    "luglio": 7, "lug": 7,
    "agosto": 8, "ago": 8,
    "settembre": 9, "set": 9, "sett": 9,
    "ottobre": 10, "ott": 10,
    "novembre": 11, "nov": 11,
    "dicembre": 12, "dic": 12,
}
_MONTH_PAT = "|".join(sorted(_MONTH_NAME_MAP.keys(), key=len, reverse=True))


_IT_ORDINAL_DAY: Dict[str, str] = {
    "primo": "1", "prima": "1", "l'uno": "1", "uno": "1",
    "due": "2", "tre": "3", "quattro": "4", "cinque": "5",
    "sei": "6", "sette": "7", "otto": "8", "nove": "9", "dieci": "10",
    "undici": "11", "dodici": "12", "tredici": "13", "quattordici": "14",
    "quindici": "15", "sedici": "16", "diciassette": "17", "diciotto": "18",
    "diciannove": "19", "venti": "20", "ventuno": "21", "ventidue": "22",
    "ventitre": "23", "ventitré": "23", "ventiquattro": "24",
    "venticinque": "25", "ventisei": "26", "ventisette": "27",
    "ventotto": "28", "ventotto": "28", "ventinove": "29",
    "trenta": "30", "trentuno": "31",
}
_IT_ORDINAL_PAT = "|".join(sorted(_IT_ORDINAL_DAY.keys(), key=len, reverse=True))


def _normalize_ordinal_days(t: str) -> str:
    """Sostituisce ordinali italiani con il numero corrispondente prima del parsing."""
    def _replace(m: re.Match) -> str:
        word = m.group(1).lower()
        return _IT_ORDINAL_DAY.get(word, m.group(0))
    return re.sub(rf"\b({_IT_ORDINAL_PAT})\b", _replace, t)


def _parse_absolute_date(t: str, today: date) -> Optional[date]:
    """Riconosce date assolute: '14 marzo', 'marzo 14', '14 marzo 2026', '14/03', '14-03'."""
    t = _normalize_ordinal_days(t)
    # "14 marzo [2026]" o "marzo 14 [2026]"
    m = re.search(rf"(\d{{1,2}})\s+({_MONTH_PAT})(?:\s+(\d{{4}}))?", t)
    if not m:
        m_rev = re.search(rf"({_MONTH_PAT})\s+(\d{{1,2}})(?:\s+(\d{{4}}))?", t)
        if m_rev:
            month_name = m_rev.group(1)
            day = int(m_rev.group(2))
            year_str = m_rev.group(3)
        else:
            m_rev = None
        if not m_rev:
            # "14/03" o "14-03"
            m2 = re.search(r"\b(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{4}|\d{2}))?\b", t)
            if m2:
                day = int(m2.group(1))
                month = int(m2.group(2))
                year_str = m2.group(3)
                year = int(year_str) if year_str else today.year
                if len(str(year)) == 2:
                    year += 2000
                try:
                    d = date(year, month, day)
                    if d < today and not year_str:
                        d = date(year + 1, month, day)
                    return d
                except ValueError:
                    return None
            return None
        month = _MONTH_NAME_MAP[month_name]
    else:
        day = int(m.group(1))
        month_name = m.group(2)
        month = _MONTH_NAME_MAP[month_name]
        year_str = m.group(3)

    year = int(year_str) if year_str else today.year
    try:
        d = date(year, month, day)
        if d < today and not year_str:
            d = date(year + 1, month, day)
        return d
    except ValueError:
        return None


@app.post("/resolve_date", response_model=ResolveDateOut)
def resolve_date(payload: ResolveDateIn):
    """
    Risolve espressioni di data in italiano usando TZ locale.
    Gestisce sia date relative (domani, sabato, weekend) sia assolute (14 marzo, 14/03).
    """
    text = (payload.input_text or "").strip().lower()
    if not text:
        raise HTTPException(status_code=400, detail="input_text required")

    t = re.sub(r"\s+", " ", text)
    today = _today_local()

    if "stasera" in t or "questa sera" in t or "questa notte" in t or "stanotte" in t or re.search(r"\boggi\b", t):
        return _format_out(today, requires=False, rule="stasera/oggi")

    if "dopodomani" in t:
        return _format_out(today + timedelta(days=2), True, "dopodomani")
    if "domani" in t:
        return _format_out(today + timedelta(days=1), True, "domani")

    if "weekend" in t:
        return _format_out(_this_or_next_weekend(today), True, "weekend->sabato")

    for key, wd in WEEKDAY_MAP.items():
        if re.search(rf"\b{re.escape(key)}\b", t):
            if today.weekday() == wd:
                return _format_out(today, True, f"weekday_today_ambiguous:{key}")
            d = _next_weekday(today, wd)
            return _format_out(d, True, f"weekday:{key}")

    # Date assolute: "14 marzo", "14/03", "14 marzo 2026", ecc.
    abs_date = _parse_absolute_date(t, today)
    if abs_date is not None:
        return _format_out(abs_date, requires=True, rule="absolute_date")

    raise HTTPException(status_code=422, detail="Unrecognized date expression")


@app.get("/time_now")
def time_now():
    now = datetime.now(TZ)
    return {
        "tz": str(getattr(TZ, "key", "LOCAL_OR_CET")),
        "now_iso": now.isoformat(),
        "date_iso": now.date().isoformat(),
        "weekday": WEEKDAYS_IT[now.weekday()],
    }


# ============================================================
# MODEL BOOKING
# ============================================================


class RichiestaPrenotazione(BaseModel):
    fase: str = Field("book", description='Fase: "availability" oppure "book"')

    nome: Optional[str] = ""
    cognome: Optional[str] = ""
    email: Optional[str] = ""
    telefono: Optional[str] = ""

    sede: Optional[str] = ""

    data: str
    orario: str
    persone: Union[int, str] = Field(...)
    seggiolini: Union[int, str] = 0  # clamp 0..3 (server). Prompt può imporre max 2.
    note: Optional[str] = Field("", alias="nota")

    model_config = {"validate_by_name": True, "extra": "ignore"}

    @root_validator(pre=True)
    def _coerce_fields(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        if values.get("note") not in (None, ""):
            values["nota"] = values.get("note")

        if not values.get("fase"):
            values["fase"] = "book"
        values["fase"] = str(values["fase"]).strip().lower()

        p = values.get("persone")
        if isinstance(p, str):
            p2 = re.sub(r"[^\d]", "", p)
            if p2:
                values["persone"] = int(p2)

        s = values.get("seggiolini")
        if isinstance(s, str):
            s2 = re.sub(r"[^\d]", "", s)
            values["seggiolini"] = int(s2) if s2 else 0
        try:
            values["seggiolini"] = max(0, min(3, int(values.get("seggiolini") or 0)))
        except Exception:
            values["seggiolini"] = 0

        if values.get("orario") is not None:
            values["orario"] = _norm_orario(str(values["orario"]))

        if values.get("sede") is not None:
            values["sede"] = _normalize_sede(str(values["sede"]))

        if values.get("telefono") is not None:
            values["telefono"] = re.sub(r"[^\d]", "", str(values["telefono"]))

        if not values.get("email"):
            values["email"] = DEFAULT_EMAIL

        values["nome"] = (values.get("nome") or "").strip()
        values["cognome"] = (values.get("cognome") or "").strip()

        return values


# ============================================================
# PLAYWRIGHT HELPERS
# ============================================================


async def _block_heavy(route):
    if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
        await route.abort()
    else:
        await route.continue_()


async def _maybe_click_cookie(page):
    for patt in [r"accetta", r"consent", r"ok", r"accetto"]:
        try:
            loc = page.locator(f"text=/{patt}/i").first
            if await loc.count() > 0:
                await loc.click(timeout=1500, force=True)
                return
        except Exception:
            pass


class CaptchaBlockedError(Exception):
    pass


async def _check_captcha_page(page):
    """Rileva immediatamente se il server ha restituito la pagina CAPTCHA invece del form."""
    url = page.url or ""
    if ".well-known/captcha" in url:
        raise CaptchaBlockedError(f"CAPTCHA page detected: {url}")
    try:
        content = await page.content()
        if ".well-known/captcha" in content or "captcha" in url.lower():
            raise CaptchaBlockedError("CAPTCHA page detected in content")
    except CaptchaBlockedError:
        raise
    except Exception:
        pass


async def _wait_ready(page):
    await page.wait_for_selector(".nCoperti", state="visible", timeout=PW_TIMEOUT_MS)


async def _click_persone(page, n: int):
    loc = page.locator(f'.nCoperti[rel="{n}"]').first
    if await loc.count() == 0:
        loc = page.get_by_text(str(n), exact=True).first
    await loc.click(timeout=8000, force=True)


async def _set_seggiolini(page, seggiolini: int):
    seggiolini = max(0, min(5, int(seggiolini or 0)))

    if seggiolini <= 0:
        try:
            no_btn = page.locator(".SeggNO").first
            if await no_btn.count() > 0 and await no_btn.is_visible():
                await no_btn.click(timeout=4000, force=True)
        except Exception:
            pass
        return

    try:
        si_btn = page.locator(".SeggSI").first
        if await si_btn.count() > 0:
            await si_btn.click(timeout=4000, force=True)
    except Exception:
        pass

    await page.wait_for_selector(".nSeggiolini", state="visible", timeout=PW_TIMEOUT_MS)
    loc = page.locator(f'.nSeggiolini[rel="{seggiolini}"]').first
    if await loc.count() == 0:
        loc = page.get_by_text(str(seggiolini), exact=True).first
    await loc.click(timeout=6000, force=True)


async def _set_date(page, data_iso: str):
    tipo = _get_data_type(data_iso)
    if tipo in ("Oggi", "Domani"):
        btn = page.locator(f'.dataBtn[rel="{data_iso}"]').first
        if await btn.count() > 0:
            await btn.click(timeout=6000, force=True)
            return

    await page.evaluate(
        """(val) => {
          const el = document.querySelector('#DataPren') || document.querySelector('input[type="date"]');
          if (!el) return false;
          const nativeSetter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value'
          ).set;
          nativeSetter.call(el, val);
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
          return true;
        }""",
        data_iso,
    )


async def _click_pasto(page, pasto: str):
    loc = page.locator(f'.tipoBtn[rel="{pasto}"]').first
    if await loc.count() > 0:
        await loc.click(timeout=8000, force=True)
        return
    await page.locator(f"text=/{pasto}/i").first.click(timeout=8000, force=True)


async def _scrape_sedi_availability(page) -> List[Dict[str, Any]]:
    """
    Estrae disponibilità sedi dalla .ristoCont.
    Fix principali:
    - nessun timeout hardcoded a 30000
    - wait_for_function con timeout configurabile
    - attesa breve post-fallback per far popolarsi il DOM
    - retry se .ristoCont resta hidden (click pasto di nuovo)
    """
    known = ["Appia", "Talenti", "Ostia Lido", "Palermo", "Reggio Calabria"]

    # Primo tentativo di attesa .ristoCont visibile
    try:
        await page.wait_for_selector(".ristoCont", state="visible", timeout=AVAIL_SELECTOR_TIMEOUT_MS)
    except Exception as first_err:
        # .ristoCont esiste ma resta hidden: probabilmente il click pasto
        # non ha triggerato il caricamento. Proviamo a ri-cliccare i bottoni pasto.
        print(f"⚠️ .ristoCont hidden dopo {AVAIL_SELECTOR_TIMEOUT_MS}ms, tentativo retry...")
        # Diagnostica: logga stato DOM
        try:
            dom_info = await page.evaluate("""() => {
                const rc = document.querySelector('.ristoCont');
                const pasti = Array.from(document.querySelectorAll('.tipoBtn')).map(
                    b => ({rel: b.getAttribute('rel'), cls: b.className, vis: b.offsetParent !== null})
                );
                return {
                    ristoCont_exists: !!rc,
                    ristoCont_display: rc ? getComputedStyle(rc).display : null,
                    ristoCont_visibility: rc ? getComputedStyle(rc).visibility : null,
                    ristoCont_classes: rc ? rc.className : null,
                    pasti_buttons: pasti
                };
            }""")
            print(f"🔍 DOM diagnostics: {json.dumps(dom_info, default=str)}")
        except Exception:
            pass

        # Retry: ri-clicca il bottone pasto attivo (forza il caricamento)
        try:
            for patto in ["PRANZO", "CENA"]:
                loc = page.locator(f'.tipoBtn[rel="{patto}"]').first
                if await loc.count() > 0:
                    is_active = await loc.evaluate("el => el.classList.contains('active') || el.classList.contains('selected')")
                    if is_active:
                        await loc.click(timeout=5000, force=True)
                        break
            # Prova anche a cliccare il primo bottone pasto con testo visibile
            else:
                for pasto_txt in ["PRANZO", "CENA", "pranzo", "cena", "Pranzo", "Cena"]:
                    try:
                        loc = page.locator(f"text=/{pasto_txt}/i").first
                        if await loc.count() > 0:
                            await loc.click(timeout=5000, force=True)
                            break
                    except Exception:
                        continue
        except Exception as re_click_err:
            print(f"⚠️ Retry click pasto fallito: {re_click_err}")

        # Secondo tentativo di attesa
        try:
            await page.wait_for_selector(".ristoCont", state="visible", timeout=AVAIL_SELECTOR_TIMEOUT_MS)
            print("✅ .ristoCont diventato visibile dopo retry")
        except Exception:
            # Ultimo tentativo: reload completo della pagina
            print("⚠️ .ristoCont ancora hidden dopo retry, ultimo tentativo impossibile senza reload")
            raise first_err

    try:
        await page.wait_for_function(
            """(names)=>{
              const root=document.querySelector('.ristoCont');
              if(!root) return false;
              const txt=(root.innerText||'').replace(/\\s+/g,' ').toLowerCase();
              const hasName = names.some(n=>txt.includes(String(n).toLowerCase()));
              const hasSpinner = root.querySelector('.spinner-border,.spinner-grow');
              return hasName || (!hasSpinner && txt.trim().length>0);
            }""",
            [n for n in known],
            timeout=AVAIL_FUNCTION_TIMEOUT_MS,
        )
    except Exception:
        await page.wait_for_timeout(AVAIL_POST_WAIT_MS)

    raw = await page.evaluate(
        """(known) => {
          function norm(s){ return (s||'').replace(/\\s+/g,' ').trim(); }
          const root = document.querySelector('.ristoCont') || document.body;
          const all = Array.from(root.querySelectorAll('*'));
          const out = [];
          for (const name of known){
            const n = norm(name).toLowerCase();
            const el = all.find(x => norm(x.innerText).toLowerCase().includes(n));
            if (!el) continue;
            out.push({ name, txt: norm(el.innerText) });
          }
          const seen = new Set();
          return out.filter(o => { if(seen.has(o.name)) return false; seen.add(o.name); return true; });
        }""",
        known,
    )

    out: List[Dict[str, Any]] = []
    for r in raw:
        name = _normalize_sede((r.get("name") or "").strip())
        txt = (r.get("txt") or "")

        price = None
        m = re.search(r"(\d{1,3}[\.,]\d{2})\s*€", txt)
        if m:
            price = m.group(1).replace(",", ".")

        sold_out = bool(re.search(r"TUTTO\s*ESAURITO", txt, flags=re.I))
        turni: List[str] = []
        if re.search(r"\bI\s*TURNO\b", txt, flags=re.I):
            turni.append("I TURNO")
        if re.search(r"\bII\s*TURNO\b", txt, flags=re.I):
            turni.append("II TURNO")

        out.append({"nome": name, "prezzo": price, "turni": turni, "tutto_esaurito": sold_out})

    order = {n: i for i, n in enumerate(["Appia", "Talenti", "Ostia Lido", "Palermo", "Reggio Calabria"])}
    out.sort(key=lambda x: order.get(x["nome"], 999))
    return out


async def _click_sede(page, sede_target: str, pasto: str = "", orario_req: str = "") -> bool:
    target = _normalize_sede(sede_target)
    await page.wait_for_selector(".ristoCont", state="visible", timeout=PW_TIMEOUT_MS)

    # --- NEW LAYOUT: click I/II TURNO button directly in the sede row ---
    if pasto and orario_req:
        try:
            parts = (orario_req + ":00").split(":")
            hh, mm = int(parts[0]), int(parts[1])
            mins = hh * 60 + mm
            want_second = mins >= (21 * 60) if pasto.upper() == "CENA" else mins >= (13 * 60 + 30)
            turno_label = "II TURNO" if want_second else "I TURNO"

            clicked = await page.evaluate(
                """([sedeName, turnoLabel]) => {
                    const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toUpperCase();
                    const ristoCont = document.querySelector('.ristoCont');
                    if (!ristoCont) return false;
                    const allEls = Array.from(ristoCont.querySelectorAll('*'));
                    // Find leaf-ish elements whose full text equals the turno label
                    const turnoBtns = allEls.filter(el => {
                        const t = norm(el.innerText || '');
                        return t === norm(turnoLabel) && t.length < 20;
                    });
                    for (const btn of turnoBtns) {
                        // Walk up to find a container that includes the sede name
                        let el = btn.parentElement;
                        for (let i = 0; i < 8; i++) {
                            if (!el) break;
                            if (norm(el.innerText || '').includes(norm(sedeName))) {
                                btn.click();
                                return true;
                            }
                            el = el.parentElement;
                        }
                    }
                    return false;
                }""",
                [target, turno_label],
            )
            if clicked:
                try:
                    await page.wait_for_selector("#OraPren", state="visible", timeout=8000)
                    print(f"✅ _click_sede new layout: clicked {turno_label} for {target}")
                    return True
                except Exception:
                    print(f"⚠️ _click_sede new layout: clicked {turno_label} for {target} but #OraPren not visible")
        except Exception as e:
            print(f"⚠️ _click_sede new layout attempt failed: {e}")

    # --- NEW LAYOUT (single turn): click sede card link/button within .ristoCont ---
    # Handles non-double-turn days where no I/II TURNO buttons exist.
    try:
        card_clicked = await page.evaluate(
            """([sedeName]) => {
                const norm = s => (s || '').replace(/\\s+/g, ' ').trim().toUpperCase();
                const ristoCont = document.querySelector('.ristoCont');
                if (!ristoCont) return null;
                const sedeNorm = norm(sedeName);
                const otherSedes = ['TALENTI','OSTIA LIDO','APPIA','PALERMO','REGGIO CALABRIA']
                    .filter(s => s !== sedeNorm);
                const allEls = Array.from(ristoCont.querySelectorAll('*'));
                // Find the sede-specific card: contains sede name but not other sedes
                const sedeEl = allEls.find(el => {
                    const t = norm(el.innerText || '');
                    return t.includes(sedeNorm) && !otherSedes.some(o => t.includes(o));
                });
                if (!sedeEl) return null;
                // Prefer <a> links first (covers URL-navigation layouts)
                const link = sedeEl.querySelector('a');
                if (link) { link.click(); return 'link'; }
                // Then non-TURNO buttons
                const btns = Array.from(sedeEl.querySelectorAll('button')).filter(b => {
                    const t = norm(b.innerText || '');
                    return t !== 'I TURNO' && t !== 'II TURNO';
                });
                if (btns.length > 0) { btns[0].click(); return 'button'; }
                // Last resort: click the card element directly (covers addEventListener-based navigation)
                sedeEl.click();
                return 'card';
            }""",
            [target],
        )
        if card_clicked:
            try:
                await page.wait_for_selector("#OraPren", state="visible", timeout=8000)
                print(f"✅ _click_sede new layout (single-turn/{card_clicked}): clicked for {target}")
                return True
            except Exception:
                print(f"⚠️ _click_sede new layout (single-turn/{card_clicked}): clicked but #OraPren not visible")
    except Exception as e:
        print(f"⚠️ _click_sede new layout (single-turn) attempt failed: {e}")

    # --- OLD LAYOUT: click on the sede name text / ancestor link ---
    for cand in [target, target.replace(" - Roma", ""), target.replace(" - roma", "")]:
        try:
            loc = page.locator(f"text=/{re.escape(cand)}/i").first
            if await loc.count() == 0:
                continue
            try:
                await loc.click(timeout=3000, force=True)
                return True
            except Exception:
                anc = loc.locator("xpath=ancestor-or-self::*[self::a or self::button or @onclick][1]")
                if await anc.count() > 0:
                    await anc.first.click(timeout=3000, force=True)
                    return True
        except Exception:
            pass
    return False


async def _maybe_select_turn(page, pasto: str, orario_req: str):
    try:
        hh, mm = [int(x) for x in orario_req.split(":")]
        mins = hh * 60 + mm

        if pasto.upper() == "CENA":
            choose_second = mins >= (21 * 60)
        else:
            choose_second = mins >= (13 * 60 + 30)

        # --- Approccio 1: pulsanti "I TURNO" / "II TURNO" ---
        # Salta se #OraPren è già visibile (new layout: _click_sede ha già cliccato il turno corretto)
        orario_already_visible = await page.locator("#OraPren").is_visible()
        if not orario_already_visible:
            b1 = page.locator("text=/^\\s*I\\s*TURNO\\s*$/i")
            b2 = page.locator("text=/^\\s*II\\s*TURNO\\s*$/i")
            has1 = await b1.count() > 0
            has2 = await b2.count() > 0
            print(f"🔀 turn: pasto={pasto} orario={orario_req} choose2={choose_second} has1={has1} has2={has2}")

            if has1 and has2:
                target = b2 if choose_second else b1
                await target.first.click(timeout=5000, force=True)
                await page.wait_for_timeout(500)
                # verifica che il click abbia funzionato
                try:
                    await page.wait_for_selector("#OraPren", state="visible", timeout=4000)
                    print("🔀 turn: #OraPren appeared after button click ✓")
                    return
                except Exception:
                    print("🔀 turn: #OraPren NOT appeared after button click — fallback")
        else:
            print(f"🔀 turn: #OraPren già visibile (new layout), skip Approccio 1")
            return  # turno già selezionato da _click_sede — nessuna azione necessaria

        # --- Approccio 2: <select> con opzioni "TURNO" (layout Chrome) ---
        found = await page.evaluate(
            """(choose_second) => {
              const selects = Array.from(document.querySelectorAll('select'));
              for (const sel of selects) {
                const opts = Array.from(sel.options).filter(o =>
                  (o.textContent || '').toUpperCase().includes('TURNO')
                );
                if (opts.length >= 1) {
                  const t = choose_second ? opts[Math.min(1, opts.length - 1)] : opts[0];
                  sel.value = t.value;
                  sel.dispatchEvent(new Event('change', { bubbles: true }));
                  return { found: true, id: sel.id, value: t.value, text: t.textContent.trim() };
                }
              }
              return { found: false };
            }""",
            choose_second,
        )
        print(f"🔀 turn fallback select: {found}")
        if found.get("found"):
            await page.wait_for_timeout(1200)
    except Exception as e:
        print(f"🔀 turn exception: {e}")
        return


async def _get_orario_options(page) -> List[Tuple[str, str]]:
    await page.wait_for_selector("#OraPren", state="visible", timeout=PW_TIMEOUT_MS)
    try:
        await page.click("#OraPren", timeout=3000)
    except Exception:
        pass

    try:
        await page.wait_for_selector("#OraPren option", timeout=PW_TIMEOUT_MS)
    except Exception:
        return []

    opts = await page.evaluate(
        """() => {
          const sel = document.querySelector('#OraPren');
          if (!sel) return [];
          return Array.from(sel.options)
            .filter(o => !o.disabled)
            .map(o => ({value: (o.value||'').trim(), text: (o.textContent||'').trim()}));
        }"""
    )

    out: List[Tuple[str, str]] = []
    for o in opts:
        v = (o.get("value") or "").strip()
        t = (o.get("text") or "").strip()
        if not t:
            continue
        if re.match(r"^\d{1,2}:\d{2}", t):
            out.append(((v or t).strip(), t))
    return out


def _pick_closest_time(target_hhmm: str, options: List[Tuple[str, str]]) -> Optional[str]:
    target_m = _time_to_minutes(target_hhmm)
    if target_m is None:
        return options[0][0] if options else None

    best = None
    best_delta = None
    for v, _ in options:
        hhmm = v[:5]
        m = _time_to_minutes(hhmm)
        if m is None:
            continue
        delta = abs(m - target_m)
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best = v

    if best is not None and best_delta is not None and best_delta <= RETRY_TIME_WINDOW_MIN:
        return best
    return None


async def _select_orario_or_retry(page, wanted_hhmm: str) -> Tuple[str, bool]:
    await page.wait_for_selector("#OraPren", state="visible", timeout=PW_TIMEOUT_MS)
    await page.wait_for_function(
        """() => {
          const sel = document.querySelector('#OraPren');
          return sel && sel.options && sel.options.length > 1;
        }""",
        timeout=PW_TIMEOUT_MS,
    )

    wanted = wanted_hhmm.strip()
    wanted_val = wanted + ":00" if re.fullmatch(r"\d{2}:\d{2}", wanted) else wanted

    try:
        res = await page.locator("#OraPren").select_option(value=wanted_val)
        if res:
            return wanted_val, False
    except Exception:
        pass

    ok = await page.evaluate(
        """(hhmm) => {
          const sel = document.querySelector('#OraPren');
          if (!sel) return false;
          const opt = Array.from(sel.options).find(o => (o.textContent || '').includes(hhmm));
          if (!opt) return false;
          sel.value = opt.value;
          sel.dispatchEvent(new Event('change', { bubbles: true }));
          return true;
        }""",
        wanted,
    )
    if ok:
        val = await page.locator("#OraPren").input_value()
        return val, False

    options = await _get_orario_options(page)
    best = _pick_closest_time(wanted, options)
    if best:
        await page.locator("#OraPren").select_option(value=best)
        return best, True

    raise RuntimeError(f"Orario non disponibile: {wanted}")


async def _fill_note_step5(page, note: str):
    note = (note or "").strip()
    if not note:
        return

    await page.wait_for_selector("#Nota", state="visible", timeout=PW_TIMEOUT_MS)
    await page.locator("#Nota").fill(note, timeout=8000)

    await page.evaluate(
        """(val) => {
          const t = document.querySelector('#Nota');
          if (t){
            t.value = val;
            t.dispatchEvent(new Event('input', { bubbles: true }));
            t.dispatchEvent(new Event('change', { bubbles: true }));
          }
          const h = document.querySelector('#Nota2');
          if (h){ h.value = val; }
        }""",
        note,
    )


async def _click_conferma(page):
    loc = page.locator(".confDati").first
    if await loc.count() > 0:
        await loc.click(timeout=8000, force=True)
        return
    await page.locator("text=/CONFERMA/i").first.click(timeout=8000, force=True)


async def _fill_form(page, nome: str, cognome: str, email: str, telefono: str):
    nome = (nome or "").strip() or "Cliente"
    cognome = (cognome or "").strip() or "Cliente"
    email = (email or "").strip() or DEFAULT_EMAIL
    telefono = re.sub(r"[^\d]", "", (telefono or ""))

    await page.wait_for_selector("#prenoForm", state="visible", timeout=PW_TIMEOUT_MS)
    await page.locator("#Nome").fill(nome, timeout=8000)
    await page.locator("#Cognome").fill(cognome, timeout=8000)
    await page.locator("#Email").fill(email, timeout=8000)
    await page.locator("#Telefono").fill(telefono, timeout=8000)

    try:
        boxes = page.locator("#prenoForm input[type=checkbox]")
        n = await boxes.count()
        for i in range(n):
            b = boxes.nth(i)
            try:
                if await b.is_checked():
                    continue
            except Exception:
                pass
            name = (await b.get_attribute("name") or "").lower()
            _id = (await b.get_attribute("id") or "").lower()
            req = await b.get_attribute("required")
            is_relevant = bool(req) or any(
                k in (name + " " + _id) for k in ["privacy", "consenso", "termin", "gdpr", "policy"]
            )
            if not is_relevant:
                continue
            try:
                await b.scroll_into_view_if_needed()
                await b.click(timeout=2000, force=True)
            except Exception:
                if _id:
                    lab = page.locator(f'label[for="{_id}"]').first
                    if await lab.count() > 0:
                        await lab.click(timeout=2000, force=True)
    except Exception:
        pass


async def _click_prenota(page):
    loc = page.locator('input[type="submit"][value="PRENOTA"]').first
    if await loc.count() > 0:
        await loc.click(timeout=15000, force=True)
        return
    await page.locator("text=/PRENOTA/i").last.click(timeout=15000, force=True)


def _looks_like_full_slot(msg: str) -> bool:
    s = (msg or "").lower()
    patterns = ["pieno", "sold out", "non disponibile", "esaur", "completo", "nessuna disponibil", "turno completo"]
    return any(p in s for p in patterns)


async def _wait_ajax_final(last_ajax_result: Dict[str, Any], timeout_ms: int = AJAX_FINAL_TIMEOUT_MS) -> str:
    """
    Aspetta una risposta AJAX finale.
    Se arriva un codice intermedio (es. MS_PS) continua ad attendere.
    Ritorna il testo finale (es. OK o messaggio errore).
    """
    start = datetime.now(TZ)
    last_txt = ""

    # attende la prima risposta
    while not last_ajax_result.get("seen"):
        await asyncio.sleep(0.05)
        if (datetime.now(TZ) - start).total_seconds() * 1000 > timeout_ms:
            raise RuntimeError("Prenotazione NON confermata: nessuna risposta AJAX intercettata (timeout).")

    # poi attende la finalizzazione (OK o messaggio)
    while True:
        txt = (last_ajax_result.get("text") or "").strip()
        txt_u = txt.upper()

        # se è finale (non pending) ritorna
        if txt and txt_u not in PENDING_AJAX:
            return txt

        # se resta uguale e pending, continua
        last_txt = txt

        await asyncio.sleep(0.05)
        if (datetime.now(TZ) - start).total_seconds() * 1000 > timeout_ms:
            # scaduto: ritorna comunque quello che abbiamo (utile per log)
            return last_txt


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


@app.get("/ip")
async def get_outbound_ip():
    """Ritorna l'IP pubblico in uscita del container Railway. Usare per whitelist SiteGround."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get("https://api.ipify.org?format=json")
            return r.json()
    except Exception as e:
        return {"error": str(e)}


@app.get("/chat", response_class=HTMLResponse)
def chat_widget():
    return """<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>deRione – Prenota con Giulia</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: 'Georgia', serif;
      background: #0d0d0d;
      color: #f5f0e8;
      min-height: 100dvh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 2rem 1rem;
    }

    .container {
      width: 100%;
      max-width: 480px;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 2rem;
    }

    .logo {
      font-size: 2.2rem;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      color: #c8a96e;
      font-weight: normal;
    }

    .tagline {
      font-size: 0.95rem;
      color: #a89b84;
      letter-spacing: 0.06em;
      text-align: center;
    }

    .divider {
      width: 60px;
      height: 1px;
      background: #c8a96e44;
    }

    .intro {
      text-align: center;
      line-height: 1.7;
      font-size: 0.97rem;
      color: #d4cfc7;
      max-width: 360px;
    }

    elevenlabs-convai {
      --el-primary-color: #c8a96e;
      --el-background-color: #1a1712;
      width: 100%;
    }

    .footer {
      margin-top: 1rem;
      font-size: 0.78rem;
      color: #5a5448;
      text-align: center;
      letter-spacing: 0.04em;
    }

    .footer a {
      color: #7a6e5f;
      text-decoration: none;
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="logo">de Rione</div>
    <div class="tagline">Assistente prenotazioni</div>
    <div class="divider"></div>
    <p class="intro">
      Parla con <strong>Giulia</strong>, la nostra assistente digitale.<br/>
      Ti aiuterà a prenotare un tavolo in pochi secondi.
    </p>
    <elevenlabs-convai agent-id="agent_2701kgrsp2gzec6rraa6bfgtrwfw"></elevenlabs-convai>
    <div class="footer">
      Per assistenza chiama <a href="tel:+390656556263">06 56556 263</a>
    </div>
  </div>
  <script src="https://elevenlabs.io/convai-widget/index.js" async></script>
</body>
</html>"""


def _require_admin(request: Request):
    if not ADMIN_TOKEN:
        return
    token = request.headers.get("x-admin-token") or request.query_params.get("token")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/_admin/dashboard")
def admin_dashboard(request: Request):
    _require_admin(request)
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as n, SUM(ok) as ok_sum FROM bookings")
    row = cur.fetchone()
    total = int(row["n"] or 0)
    ok_sum = int(row["ok_sum"] or 0)
    ok_rate = (ok_sum / total * 100.0) if total else 0.0

    cur.execute("SELECT * FROM bookings ORDER BY id DESC LIMIT 25")
    last = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM customers ORDER BY updated_at DESC LIMIT 25")
    cust = [dict(r) for r in cur.fetchall()]
    conn.close()

    return {
        "stats": {"total": total, "ok": ok_sum, "ok_rate_pct": round(ok_rate, 2)},
        "last_bookings": last,
        "customers": cust,
    }


@app.get("/_admin/customer/{phone}")
def admin_customer(phone: str, request: Request):
    _require_admin(request)
    c = _get_customer(re.sub(r"[^\d]", "", phone))
    return {"customer": c}


@app.get("/_admin/fidy_api_probe")
async def fidy_api_probe(
    request: Request,
    date: str = "2026-03-17",
    service: str = "cena",
    persone: int = 2,
    sede: str = "Talenti",
):
    """Intercetta tutte le chiamate di rete verso Fidy durante una sessione Playwright.

    Naviga il form di prenotazione fino alla selezione della sede (senza prenotare),
    catturando URL, metodo, body e risposta di ogni chiamata verso api.fidy.app o ajax.php.
    Usare per scoprire gli endpoint esatti dell'API Fidy per availability e create-reservation.

    Params: date (YYYY-MM-DD), service (pranzo|cena), persone, sede
    """
    _require_admin(request)

    captured: List[Dict[str, Any]] = []
    pasto = "PRANZO" if service.lower() == "pranzo" else "CENA"
    sede_norm = _normalize_sede(sede)

    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--single-process",
                    "--disable-gpu",
                ],
            )
            context = await browser.new_context(
                user_agent=IPHONE_UA, viewport={"width": 390, "height": 844}
            )
            page = await context.new_page()
            page.set_default_timeout(PW_TIMEOUT_MS)
            page.set_default_navigation_timeout(PW_NAV_TIMEOUT_MS)
            await page.route("**/*", _block_heavy)

            async def _capture_request(req):
                url = req.url or ""
                if "fidy" not in url.lower() and "ajax.php" not in url.lower():
                    return
                entry: Dict[str, Any] = {
                    "direction": "request",
                    "method": req.method,
                    "url": url,
                }
                try:
                    body = req.post_data
                    if body:
                        try:
                            entry["body"] = json.loads(body)
                        except Exception:
                            entry["body_raw"] = body[:2000]
                    headers_raw = await req.all_headers()
                    entry["headers"] = {
                        k: v for k, v in headers_raw.items()
                        if k.lower() in ("content-type", "x-api-key", "authorization", "accept", "origin", "referer")
                    }
                except Exception as e:
                    entry["capture_error"] = str(e)
                captured.append(entry)

            async def _capture_response(resp):
                url = resp.url or ""
                if "fidy" not in url.lower() and "ajax.php" not in url.lower():
                    return
                entry: Dict[str, Any] = {
                    "direction": "response",
                    "status": resp.status,
                    "url": url,
                }
                try:
                    ct = resp.headers.get("content-type", "")
                    txt = await resp.text()
                    if "json" in ct or txt.lstrip().startswith("{") or txt.lstrip().startswith("["):
                        try:
                            entry["body"] = json.loads(txt)
                        except Exception:
                            entry["body_raw"] = txt[:3000]
                    else:
                        entry["body_raw"] = txt[:500]
                except Exception as e:
                    entry["capture_error"] = str(e)
                captured.append(entry)

            page.on("request", _capture_request)
            page.on("response", _capture_response)

            # Naviga e compila il form
            await page.goto(BOOKING_URL, wait_until="domcontentloaded")
            await _maybe_click_cookie(page)
            await _check_captcha_page(page)
            await _wait_ready(page)
            await _click_persone(page, persone)
            await _set_date(page, date)
            await _click_pasto(page, pasto)

            # Aspetta che la lista sedi si carichi (trigger availability)
            try:
                await page.wait_for_selector(".ristoCont", state="visible", timeout=15000)
                await asyncio.sleep(1.5)
            except Exception:
                pass

            # Prova anche a cliccare la sede per triggerare ulteriori chiamate API
            try:
                await _click_sede(page, sede_norm, pasto, "20:00")
                await asyncio.sleep(1.5)
            except Exception:
                pass

    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "captured_so_far": captured,
        }
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass

    # Raggruppa request+response per URL
    pairs: List[Dict[str, Any]] = []
    req_map: Dict[str, Dict[str, Any]] = {}
    for entry in captured:
        url = entry["url"]
        if entry["direction"] == "request":
            req_map[url] = entry
            pairs.append({"request": entry, "response": None})
        else:
            # associa alla request corrispondente se esiste
            matched = False
            for pair in reversed(pairs):
                if pair["request"]["url"] == url and pair["response"] is None:
                    pair["response"] = entry
                    matched = True
                    break
            if not matched:
                pairs.append({"request": None, "response": entry})

    return {
        "ok": True,
        "probe_params": {"date": date, "service": service, "persone": persone, "sede": sede},
        "total_calls": len(pairs),
        "calls": pairs,
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

    # Validazioni base
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dati.data or ""):
        msg = f"Formato data non valido: {dati.data}. Usa YYYY-MM-DD."
        _log_booking(dati.model_dump(), False, msg)
        return {"ok": False, "status": "VALIDATION_ERROR", "message": msg}

    if not re.fullmatch(r"\d{2}:\d{2}", dati.orario or ""):
        msg = f"Formato orario non valido: {dati.orario}. Usa HH:MM."
        _log_booking(dati.model_dump(), False, msg)
        return {"ok": False, "status": "VALIDATION_ERROR", "message": msg}

    if not isinstance(dati.persone, int) or dati.persone < 1 or dati.persone > 50:
        msg = f"Numero persone non valido: {dati.persone}."
        _log_booking(dati.model_dump(), False, msg)
        return {"ok": False, "status": "VALIDATION_ERROR", "message": msg}

    fase = (dati.fase or "book").strip().lower()
    if fase not in ("availability", "book"):
        msg = f'Valore fase non valido: {dati.fase}. Usa "availability" oppure "book".'
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

    sede_target = (dati.sede or "").strip()
    orario_req = (dati.orario or "").strip()
    data_req = (dati.data or "").strip()
    pax_req = int(dati.persone)
    pasto = _calcola_pasto(orario_req)

    note_in = re.sub(r"\s+", " ", (dati.note or "")).strip()[:250]
    seggiolini = max(0, min(3, int(dati.seggiolini or 0)))

    telefono = re.sub(r"[^\d]", "", dati.telefono or "")
    email = (dati.email or DEFAULT_EMAIL).strip() or DEFAULT_EMAIL
    cognome = (dati.cognome or "").strip() or "Cliente"

    # memoria email: se default e abbiamo una vera salvata -> usa quella
    cust = _get_customer(telefono) if telefono else None
    if cust and email == DEFAULT_EMAIL and cust.get("email") and ("@" in cust["email"]):
        email = cust["email"]

    print(
        f"🚀 BOOKING: fase={fase} | sede='{sede_target or '-'}' | {data_req} {orario_req} | "
        f"pax={pax_req} | pasto={pasto} | seggiolini={seggiolini}"
    )

    try:
        return await asyncio.wait_for(
            _do_booking(
                dati, fase, sede_target, orario_req, data_req,
                pax_req, pasto, note_in, seggiolini, telefono, email, cognome,
            ),
            timeout=BOOKING_TOTAL_TIMEOUT_S,
        )
    except (asyncio.TimeoutError, TimeoutError):
        _log_booking(dati.model_dump(), False, f"Timeout totale: {BOOKING_TOTAL_TIMEOUT_S}s")
        return {"ok": False, "status": "TECH_ERROR", "message": "Timeout nella verifica disponibilità."}


async def _do_booking(
    dati,
    fase: str,
    sede_target: str,
    orario_req: str,
    data_req: str,
    pax_req: int,
    pasto: str,
    note_in: str,
    seggiolini: int,
    telefono: str,
    email: str,
    cognome: str,
):
    # ============================================================
    # PLAYWRIGHT (SAFE)
    # ============================================================
    browser = None
    context = None
    page = None

    last_ajax_result: Dict[str, Any] = {"seen": False, "text": ""}
    screenshot_path = None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--single-process",
                    "--disable-gpu",
                ],
            )
            context = await browser.new_context(user_agent=IPHONE_UA, viewport={"width": 390, "height": 844})
            page = await context.new_page()
            if _STEALTH_AVAILABLE:
                await _stealth_async(page)
            page.set_default_timeout(PW_TIMEOUT_MS)
            page.set_default_navigation_timeout(PW_NAV_TIMEOUT_MS)
            await page.route("**/*", _block_heavy)

            async def on_response(resp):
                try:
                    url_lower = (resp.url or "").lower()
                    method = (resp.request.method or "").upper()
                    # logga tutti i POST verso fidy per diagnostica URL
                    if method == "POST" and "fidy" in url_lower:
                        print("🌐 POST_RESPONSE_URL:", resp.url, "status:", resp.status)
                    if "ajax.php" in url_lower or ("fidy" in url_lower and method == "POST" and resp.status == 200):
                        txt = await resp.text()
                        txt = (txt or "").strip()
                        if not txt:
                            return
                        last_ajax_result["seen"] = True
                        last_ajax_result["text"] = txt
                        print("🧩 AJAX_RESPONSE:", txt[:500])
                except Exception:
                    pass

            page.on("response", on_response)

            if DEBUG_LOG_AJAX_POST:

                async def on_request(req):
                    try:
                        if "ajax.php" in req.url.lower() and req.method.upper() == "POST":
                            print("🌐 AJAX_POST_URL:", req.url)
                            print("🌐 AJAX_POST_BODY:", (req.post_data or "")[:2000])
                    except Exception:
                        pass

                page.on("request", on_request)

            # ============================================================
            # FLOW
            # ============================================================
            await page.goto(BOOKING_URL, wait_until="domcontentloaded")
            await _maybe_click_cookie(page)
            await _check_captcha_page(page)
            await _wait_ready(page)

            # STEP 1 persone + seggiolini
            await _click_persone(page, pax_req)
            await _set_seggiolini(page, seggiolini)

            # STEP 2 data
            await _set_date(page, data_req)

            # STEP 3 pasto
            await _click_pasto(page, pasto)

            # ----------------------------
            # AVAILABILITY
            # ----------------------------
            if fase == "availability":
                sedi = await _scrape_sedi_availability(page)

                weekday = None
                try:
                    weekday = datetime.fromisoformat(data_req).date().weekday()
                except Exception:
                    pass

                def _doppi_turni_previsti(nome: str) -> bool:
                    n = (nome or "").strip().lower()
                    if n in ("ostia", "ostia lido"):
                        return False
                    if weekday is None:
                        return False

                    is_sat = weekday == 5
                    is_sun = weekday == 6

                    if n == "talenti":
                        if pasto == "PRANZO":
                            return is_sat or is_sun
                        if pasto == "CENA":
                            return is_sat
                        return False

                    if n in ("appia", "palermo"):
                        if pasto == "PRANZO":
                            return is_sat or is_sun
                        if pasto == "CENA":
                            return is_sat
                        return False

                    if n == "reggio calabria":
                        return pasto == "CENA" and is_sat

                    return False

                for s in sedi:
                    s["doppi_turni_previsti"] = _doppi_turni_previsti(s.get("nome"))

                return {
                    "ok": True,
                    "fase": "choose_sede",
                    "pasto": pasto,
                    "data": data_req,
                    "orario": orario_req,
                    "pax": pax_req,
                    "sedi": sedi,
                }

            # ----------------------------
            # BOOK
            # ----------------------------
            try:
                sedi = await _scrape_sedi_availability(page)
            except Exception as avail_err:
                # Retry: ricaricare la pagina e ripetere tutti gli step
                print(f"⚠️ Availability scrape fallito ({avail_err}), retry con reload...")
                await page.goto(BOOKING_URL, wait_until="domcontentloaded")
                await _maybe_click_cookie(page)
                await _check_captcha_page(page)
                await _wait_ready(page)
                await _click_persone(page, pax_req)
                await _set_seggiolini(page, seggiolini)
                await _set_date(page, data_req)
                await _click_pasto(page, pasto)
                sedi = await _scrape_sedi_availability(page)

            entry = next((x for x in sedi if _normalize_sede(x.get("nome")) == _normalize_sede(sede_target)), None)
            if entry and entry.get("tutto_esaurito"):
                return {
                    "ok": False,
                    "status": "SOLD_OUT",
                    "message": "Sede esaurita",
                    "sede": entry.get("nome") or sede_target,
                    "alternative": _suggest_alternative_sedi(entry.get("nome") or sede_target, sedi),
                    "sedi": sedi,
                }

            clicked = await _click_sede(page, sede_target, pasto, orario_req)
            if not clicked:
                return {
                    "ok": False,
                    "status": "SOLD_OUT",
                    "message": "Sede non cliccabile / non trovata",
                    "sede": sede_target,
                    "alternative": _suggest_alternative_sedi(sede_target, sedi),
                    "sedi": sedi,
                }

            await _maybe_select_turn(page, pasto, orario_req)

            selected_orario_value = None
            used_fallback = False
            last_select_error = None
            for _ in range(max(1, MAX_SLOT_RETRIES)):
                try:
                    selected_orario_value, used_fallback = await _select_orario_or_retry(page, orario_req)
                    break
                except Exception as e:
                    last_select_error = e

            if not selected_orario_value:
                raise RuntimeError(str(last_select_error) if last_select_error else "Orario non disponibile")

            await _fill_note_step5(page, note_in)
            await _click_conferma(page)
            await _fill_form(page, dati.nome, cognome, email, telefono)

            if DISABLE_FINAL_SUBMIT:
                msg = "FORM COMPILATO (test mode, submit disattivato)"
                payload_log = dati.model_dump()
                payload_log.update({"email": email, "note": note_in, "seggiolini": seggiolini})
                _log_booking(payload_log, True, msg)
                return {
                    "ok": True,
                    "message": msg,
                    "fallback_time": used_fallback,
                    "selected_time": selected_orario_value[:5],
                }

            submit_attempts = 0
            while True:
                submit_attempts += 1
                last_ajax_result["seen"] = False
                last_ajax_result["text"] = ""

                await _click_prenota(page)

                ajax_txt = await _wait_ajax_final(last_ajax_result, timeout_ms=AJAX_FINAL_TIMEOUT_MS)

                if ajax_txt.strip().upper() == "OK":
                    break

                if not ajax_txt:
                    raise RuntimeError("Prenotazione NON confermata: risposta AJAX vuota.")

                if _looks_like_full_slot(ajax_txt) and submit_attempts <= MAX_SUBMIT_RETRIES:
                    options = await _get_orario_options(page)
                    options = [(v, t) for (v, t) in options if v != selected_orario_value]
                    best = _pick_closest_time(orario_req, options)
                    if not best:
                        raise RuntimeError(
                            f"Slot pieno e nessun orario alternativo entro {RETRY_TIME_WINDOW_MIN} min. Msg: {ajax_txt}"
                        )

                    await page.goto(BOOKING_URL, wait_until="domcontentloaded")
                    await _maybe_click_cookie(page)
                    await _check_captcha_page(page)
                    await _wait_ready(page)
                    await _click_persone(page, pax_req)
                    await _set_seggiolini(page, seggiolini)
                    await _set_date(page, data_req)
                    await _click_pasto(page, pasto)
                    if not await _click_sede(page, sede_target, pasto, orario_req):
                        return {"ok": False, "status": "SOLD_OUT", "message": "Sede esaurita", "sede": sede_target}

                    await page.locator("#OraPren").select_option(value=best)
                    selected_orario_value = best
                    used_fallback = True
                    await _fill_note_step5(page, note_in)
                    await _click_conferma(page)
                    await _fill_form(page, dati.nome, cognome, email, telefono)
                    continue

                raise RuntimeError(f"Errore dal sito: {ajax_txt}")

            if telefono:
                full_name = f"{(dati.nome or '').strip()} {cognome}".strip()
                _upsert_customer(
                    phone=telefono,
                    name=full_name,
                    email=email,
                    sede=_normalize_sede(sede_target),
                    persone=pax_req,
                    seggiolini=seggiolini,
                    note=note_in,
                )

            msg = (
                f"Prenotazione OK: {pax_req} pax - {_normalize_sede(sede_target)} "
                f"{data_req} {selected_orario_value[:5]} - {(dati.nome or '').strip()} {cognome}"
            ).strip()

            payload_log = dati.model_dump()
            payload_log.update(
                {
                    "email": email,
                    "note": note_in,
                    "seggiolini": seggiolini,
                    "orario": selected_orario_value[:5],
                    "cognome": cognome,
                }
            )
            _log_booking(payload_log, True, msg)

            return {"ok": True, "message": msg, "fallback_time": used_fallback, "selected_time": selected_orario_value[:5]}

    except CaptchaBlockedError as e:
        err_str = str(e)
        print(f"🚫 CAPTCHA rilevato, interrompo immediatamente: {err_str}")
        payload_log = dati.model_dump()
        payload_log.update(
            {
                "note": note_in if "note_in" in locals() else "",
                "seggiolini": seggiolini if "seggiolini" in locals() else 0,
            }
        )
        _log_booking(payload_log, False, err_str)
        return {"ok": False, "status": "CAPTCHA_BLOCKED", "message": "Sistema di prenotazione temporaneamente non raggiungibile.", "error": err_str}

    except Exception as e:
        err_str = str(e)

        if page is not None:
            try:
                ts = datetime.now(TZ).strftime("%Y%m%d_%H%M%S_%f")
                screenshot_path = f"booking_error_{ts}.png"
                await page.screenshot(path=screenshot_path, full_page=True)
                print(f"📸 Screenshot salvato: {screenshot_path}")
            except Exception:
                screenshot_path = None

        payload_log = dati.model_dump()
        payload_log.update(
            {
                "note": note_in if "note_in" in locals() else "",
                "seggiolini": seggiolini if "seggiolini" in locals() else 0,
            }
        )
        _log_booking(payload_log, False, err_str)

        status = "TECH_ERROR" if _is_timeout_error(err_str) else "ERROR"
        msg = "Errore tecnico nel verificare la disponibilità." if status == "TECH_ERROR" else "Errore durante la prenotazione."

        return {"ok": False, "status": status, "message": msg, "error": err_str, "screenshot": screenshot_path}

    finally:
        try:
            if context is not None:
                await context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass


# ============================================================
# FIDY API PROXY — check, find, cancel, update_covers, add_note
# ============================================================

FIDY_UA = os.getenv(
    "FIDY_UA",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/133.0.0.0 Safari/537.36",
)


def _fidy_headers() -> Dict[str, str]:
    return {
        "X-API-Key": FIDY_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": FIDY_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": "https://rione.fidy.app",
        "Referer": "https://rione.fidy.app/",
    }


def _resolve_restaurant_id(restaurant_id: Any) -> int:
    """Accetta ID numerico (int/str) o nome sede testuale. Ritorna sempre int."""
    if isinstance(restaurant_id, int):
        return restaurant_id
    s = str(restaurant_id).strip()
    if s.isdigit():
        return int(s)
    return SEDE_ID_MAP.get(s.lower(), int(s) if s.isdigit() else 0)


# --- Modelli Pydantic ---

class CheckReservationIn(BaseModel):
    date: str
    phone: str
    restaurant_id: Optional[Any] = None
    time: Optional[str] = None


class FindReservationForCancelIn(BaseModel):
    reservation_code: Optional[str] = None
    restaurant_id: Optional[Any] = None
    date: Optional[str] = None
    time: Optional[str] = None
    phone: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None


class CancelReservationIn(BaseModel):
    phone: str
    date: Optional[str] = None
    restaurant_id: Optional[Any] = None
    time: Optional[str] = None
    note: Optional[str] = None
    first_name: Optional[str] = None  # Nome del cliente, se noto (aumenta il match su Fidy)

    @validator("date")
    @classmethod
    def validate_date_format(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", v):
            raise ValueError(
                f"Formato data non valido: '{v}'. Usare YYYY-MM-DD (es. 2026-03-10). "
                "Non usare resolve_date per le cancellazioni: convertire internamente."
            )
        return v


class UpdateCoversIn(BaseModel):
    date: str
    phone: str
    new_covers: int
    restaurant_id: Optional[Any] = None
    time: Optional[str] = None


class AddNoteIn(BaseModel):
    phone: str
    date: str
    note: str
    restaurant_id: Any = None
    time: Optional[str] = None


# --- Endpoint proxy ---

@app.get("/check_reservation")
async def check_reservation(
    date: str,
    phone: str,
    restaurant_id: Optional[str] = None,
    time: Optional[str] = None,
):
    """Verifica se esiste una prenotazione per data+telefono (+ sede e orario opzionali)."""
    params: Dict[str, Any] = {
        "date": date,
        "phone": re.sub(r"[^\d+]", "", phone),
    }
    if restaurant_id is not None:
        params["restaurant_id"] = _resolve_restaurant_id(restaurant_id)
    if time is not None:
        params["time"] = time
    try:
        async with httpx.AsyncClient(timeout=FIDY_TIMEOUT_S) as client:
            resp = await client.get(f"{FIDY_API_BASE}/check-reservation", params=params, headers=_fidy_headers())
        if "text/html" in resp.headers.get("content-type", "") or resp.text.lstrip().startswith("<"):
            return {"ok": False, "status": "CAPTCHA_BLOCKED", "message": "Sistema temporaneamente non raggiungibile."}
        return resp.json()
    except httpx.TimeoutException:
        return {"ok": False, "status": "TECH_ERROR", "message": "Timeout contattando il sistema di prenotazione."}
    except Exception as e:
        return {"ok": False, "status": "ERROR", "message": f"Errore Fidy API: {e}"}


@app.post("/find_reservation_for_cancel")
async def find_reservation_for_cancel(body: FindReservationForCancelIn):
    """Trova una prenotazione da cancellare. Ritorna anche il telefono associato per conferma."""
    payload: Dict[str, Any] = {}
    if body.reservation_code:
        payload["reservation_code"] = body.reservation_code
    if body.restaurant_id is not None:
        payload["restaurant_id"] = _resolve_restaurant_id(body.restaurant_id)
    if body.date:
        payload["date"] = body.date
    if body.time:
        payload["time"] = body.time
    if body.phone:
        payload["phone"] = re.sub(r"[^\d+]", "", body.phone)
    if body.first_name:
        payload["first_name"] = body.first_name
    if body.last_name:
        payload["last_name"] = body.last_name

    try:
        async with httpx.AsyncClient(timeout=FIDY_TIMEOUT_S) as client:
            resp = await client.post(f"{FIDY_API_BASE}/find-reservation-for-cancel", json=payload, headers=_fidy_headers())
        if "text/html" in resp.headers.get("content-type", "") or resp.text.lstrip().startswith("<"):
            return {"ok": False, "status": "CAPTCHA_BLOCKED", "message": "Sistema temporaneamente non raggiungibile."}
        return resp.json()
    except httpx.TimeoutException:
        return {"ok": False, "status": "TECH_ERROR", "message": "Timeout contattando il sistema di prenotazione."}
    except Exception as e:
        return {"ok": False, "status": "ERROR", "message": f"Errore Fidy API: {e}"}


@app.post("/cancel_reservation")
async def cancel_reservation(body: CancelReservationIn):
    """Annulla una prenotazione esistente.

    Richiede phone + almeno uno tra date e restaurant_id.
    Chiama sempre find-reservation-for-cancel prima per ottenere i dettagli
    esatti della prenotazione (incluso eventuale ID interno), poi esegue il cancel.
    """
    phone = re.sub(r"[^\d+]", "", body.phone)

    # ── Step 1: trova la prenotazione tramite find-reservation-for-cancel ──
    find_payload: Dict[str, Any] = {"phone": phone}
    if body.restaurant_id is not None:
        find_payload["restaurant_id"] = _resolve_restaurant_id(body.restaurant_id)
    if body.date:
        find_payload["date"] = body.date
    if body.time:
        find_payload["time"] = body.time

    # Arricchisci con il nome: prima dall'argomento esplicito, poi dal DB locale
    if body.first_name:
        find_payload["first_name"] = body.first_name.strip()
        find_payload["last_name"] = "Cliente"
    else:
        customer = _get_customer(phone)
        if customer and customer.get("name"):
            name_parts = customer["name"].strip().split()
            find_payload["first_name"] = name_parts[0] if name_parts else ""
            find_payload["last_name"] = name_parts[1] if len(name_parts) > 1 else "Cliente"

    print(f"[cancel] find payload → {find_payload}")
    reservation_info: Dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(timeout=FIDY_TIMEOUT_S) as client:
            find_resp = await client.post(
                f"{FIDY_API_BASE}/find-reservation-for-cancel",
                json=find_payload,
                headers=_fidy_headers(),
            )
        ct = find_resp.headers.get("content-type", "")
        print(f"[cancel] find-reservation-for-cancel status={find_resp.status_code} body={find_resp.text[:500]}")
        if "text/html" not in ct and not find_resp.text.lstrip().startswith("<"):
            if find_resp.status_code == 200:
                reservation_info = find_resp.json() or {}
    except Exception as exc:
        print(f"[cancel] find step error (ignorato): {exc}")

    # ── Step 2: costruisci payload cancel integrando i dati trovati ──────
    cancel_payload: Dict[str, Any] = {"phone": phone}

    # data: body > trovata
    date = body.date or reservation_info.get("date")
    if date:
        cancel_payload["date"] = date

    # restaurant_id: body > trovato
    if body.restaurant_id is not None:
        cancel_payload["restaurant_id"] = _resolve_restaurant_id(body.restaurant_id)
    elif reservation_info.get("restaurant_id"):
        cancel_payload["restaurant_id"] = reservation_info["restaurant_id"]

    # time: body > trovato
    time = body.time or reservation_info.get("time")
    if time:
        cancel_payload["time"] = time

    # ID/codice prenotazione se restituito da find
    for key in ("id", "reservation_id", "booking_id", "reservation_code", "code"):
        if reservation_info.get(key):
            cancel_payload[key] = reservation_info[key]
            break

    if body.note:
        cancel_payload["note"] = body.note

    # Se ancora non abbiamo la data, non possiamo procedere
    if "date" not in cancel_payload:
        return {
            "ok": False,
            "status": "NOT_FOUND",
            "message": "Prenotazione non trovata con i dati forniti. Verificare numero di telefono e sede.",
        }

    # ── Step 3: cancella ──────────────────────────────────────────────────
    print(f"[cancel] cancel payload → {cancel_payload}")
    try:
        async with httpx.AsyncClient(timeout=FIDY_TIMEOUT_S) as client:
            resp = await client.post(
                f"{FIDY_API_BASE}/cancel-reservation", json=cancel_payload, headers=_fidy_headers()
            )
        print(f"[cancel] cancel-reservation status={resp.status_code} body={resp.text[:500]}")
        content_type = resp.headers.get("content-type", "")
        if "text/html" in content_type or resp.text.lstrip().startswith("<"):
            return {"ok": False, "status": "CAPTCHA_BLOCKED", "message": "Sistema di prenotazione temporaneamente non raggiungibile."}
        try:
            body_json = resp.json()
        except Exception:
            body_json = {"raw": resp.text}
        if resp.status_code >= 400:
            return {"ok": False, "status": "ERROR", "message": f"Errore dal sistema: {resp.status_code}", "detail": body_json}
        # Normalizza la risposta: garantisce sempre ok=true per status 2xx
        if isinstance(body_json, dict) and "ok" not in body_json:
            body_json["ok"] = True
        return body_json
    except httpx.TimeoutException:
        return {"ok": False, "status": "TECH_ERROR", "message": "Timeout contattando il sistema di prenotazione."}
    except Exception as e:
        return {"ok": False, "status": "ERROR", "message": f"Errore Fidy API: {e}"}


@app.post("/update_covers")
async def update_covers(body: UpdateCoversIn):
    """Aggiorna il numero di coperti di una prenotazione esistente.

    Strategia a due livelli:
    1. Prova l'API Fidy /update-covers (veloce, ~1s).
    2. Se fallisce o richiede rebooking → cancella e riprenota via Playwright
       usando i dati dell'archivio locale (bookings + customers).
    """
    phone = re.sub(r"[^\d+]", "", body.phone)
    rest_id = _resolve_restaurant_id(body.restaurant_id) if body.restaurant_id is not None else None
    fidy_payload: Dict[str, Any] = {
        "date": body.date,
        "phone": phone,
        "new_covers": body.new_covers,
    }
    if rest_id is not None:
        fidy_payload["restaurant_id"] = rest_id
    if body.time is not None:
        fidy_payload["time"] = body.time

    # ── Tentativo 1: Fidy API ──────────────────────────────────────────────
    needs_rebooking = False
    try:
        async with httpx.AsyncClient(timeout=FIDY_TIMEOUT_S) as client:
            resp = await client.post(
                f"{FIDY_API_BASE}/update-covers", json=fidy_payload, headers=_fidy_headers()
            )
        content_type = resp.headers.get("content-type", "")
        is_html = "text/html" in content_type or resp.text.lstrip().startswith("<")
        if not is_html:
            try:
                fidy_json = resp.json()
            except Exception:
                fidy_json = None
            if resp.status_code < 400 and fidy_json and not fidy_json.get("requires_rebooking"):
                return fidy_json  # ✅ successo diretto Fidy
        needs_rebooking = True
        print(f"⚠️ update_covers: Fidy API non disponibile (status={resp.status_code}), fallback cancel+rebook")
    except Exception as exc:
        needs_rebooking = True
        print(f"⚠️ update_covers: eccezione Fidy API ({exc}), fallback cancel+rebook")

    # ── Tentativo 2: cancel + rebook via Playwright ────────────────────────
    # Recupera dati dalla prenotazione originale (archivio locale)
    time_val = body.time
    booking = (
        _lookup_last_booking(phone, body.date, time_val)
        if time_val
        else _lookup_last_booking_by_date(phone, body.date)
    )
    if booking and not time_val:
        time_val = (booking or {}).get("orario")
    customer = _get_customer(phone)

    nome = (booking or {}).get("name") or (customer or {}).get("name") or "Cliente"
    email = (customer or {}).get("email") or (booking or {}).get("email") or DEFAULT_EMAIL
    if not email or "@" not in str(email):
        email = DEFAULT_EMAIL
    sede = (booking or {}).get("sede") or (_ID_TO_SEDE_NAME.get(rest_id, "") if rest_id else "")
    seggiolini = int((booking or {}).get("seggiolini") or 0)
    note = (booking or {}).get("note") or ""

    if not sede:
        # Sede sconosciuta: non possiamo riprenota automaticamente
        return {
            "ok": False,
            "requires_rebooking": True,
            "message": "Sede non trovata nell'archivio. Cancella e riprenota manualmente.",
        }

    if not time_val:
        return {
            "ok": False,
            "requires_rebooking": True,
            "message": "Orario non trovato nell'archivio. Cancella e riprenota manualmente.",
        }

    # Cancella la prenotazione esistente
    cancel_payload: Dict[str, Any] = {"phone": phone, "date": body.date}
    if rest_id:
        cancel_payload["restaurant_id"] = rest_id
    if time_val:
        cancel_payload["time"] = time_val
    try:
        async with httpx.AsyncClient(timeout=FIDY_TIMEOUT_S) as client:
            await client.post(
                f"{FIDY_API_BASE}/cancel-reservation", json=cancel_payload, headers=_fidy_headers()
            )
        print(f"✅ update_covers: cancellazione eseguita per {phone} {body.date} {time_val}")
    except Exception as exc:
        print(f"⚠️ update_covers: cancellazione fallita ({exc}), si tenta comunque il rebook")

    # Riprenota con il nuovo numero di coperti
    fake_dati = RichiestaPrenotazione.model_validate({
        "fase": "book",
        "nome": nome,
        "cognome": "Cliente",
        "email": email,
        "telefono": phone,
        "sede": sede,
        "data": body.date,
        "orario": time_val,
        "persone": body.new_covers,
        "seggiolini": seggiolini,
        "nota": note,
    })
    pasto = _calcola_pasto(time_val)
    print(f"🔄 update_covers: rebook {sede} {body.date} {time_val} pax={body.new_covers}")
    try:
        result = await asyncio.wait_for(
            _do_booking(
                fake_dati, "book", sede, time_val, body.date,
                body.new_covers, pasto, note, seggiolini, phone, email, "Cliente",
            ),
            timeout=BOOKING_TOTAL_TIMEOUT_S,
        )
        if result.get("ok"):
            result["update_method"] = "cancel_rebook"
        return result
    except (asyncio.TimeoutError, TimeoutError):
        return {"ok": False, "status": "TECH_ERROR", "message": "Timeout durante il rebooking. Riprova tra qualche minuto."}
    except Exception as exc:
        return {"ok": False, "status": "ERROR", "message": f"Errore durante il rebooking: {exc}"}


@app.post("/add_note")
async def add_note(body: AddNoteIn):
    """Aggiunge una nota a una prenotazione esistente."""
    payload: Dict[str, Any] = {
        "phone": re.sub(r"[^\d+]", "", body.phone),
        "date": body.date,
        "note": body.note,
    }
    if body.restaurant_id is not None:
        payload["restaurant_id"] = _resolve_restaurant_id(body.restaurant_id)
    if body.time is not None:
        payload["time"] = body.time

    try:
        async with httpx.AsyncClient(timeout=FIDY_TIMEOUT_S) as client:
            resp = await client.post(f"{FIDY_API_BASE}/add-note", json=payload, headers=_fidy_headers())
        content_type = resp.headers.get("content-type", "")
        if "text/html" in content_type or resp.text.lstrip().startswith("<"):
            return {"ok": False, "status": "CAPTCHA_BLOCKED", "message": "Sistema di prenotazione temporaneamente non raggiungibile."}
        try:
            body_json = resp.json()
        except Exception:
            body_json = {"raw": resp.text}
        if resp.status_code >= 400:
            return {"ok": False, "status": "ERROR", "message": f"Errore dal sistema: {resp.status_code}", "detail": body_json}
        return body_json
    except httpx.TimeoutException:
        return {"ok": False, "status": "TECH_ERROR", "message": "Timeout contattando il sistema di prenotazione."}
    except Exception as e:
        return {"ok": False, "status": "ERROR", "message": f"Errore Fidy API: {e}"}


# ============================================================
# ESERCIZI — parser calendario e disponibilità settimanale
# ============================================================

_GIORNI_SETTIMANA = ["lunedi", "martedi", "mercoledi", "giovedi", "venerdi", "sabato", "domenica"]
_GIORNI_SETTIMANA_IT = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]

_esercizi_pool = None


async def _get_esercizi_pool():
    """
    Ritorna il pool di connessioni MySQL al database Esercizi (lazy init).
    Lancia HTTPException 503 se le credenziali non sono configurate.
    """
    global _esercizi_pool
    if _esercizi_pool is None:
        if not ESERCIZI_DB_HOST:
            raise HTTPException(
                status_code=503,
                detail="Database non configurato. Impostare DB_HOST, DB_NAME, DB_USER, DB_PASSWORD.",
            )
        import aiomysql
        _esercizi_pool = await aiomysql.create_pool(
            host=ESERCIZI_DB_HOST,
            port=ESERCIZI_DB_PORT,
            db=ESERCIZI_DB_NAME,
            user=ESERCIZI_DB_USER,
            password=ESERCIZI_DB_PASS,
            charset="utf8mb4",
            autocommit=True,
            minsize=1,
            maxsize=5,
        )
    return _esercizi_pool


def _parse_calendario(calendario: Optional[str], coperti: int) -> List[Dict]:
    """
    Parsa il campo Calendario della tabella Esercizi.

    Formato: 14 valori separati da virgola che rappresentano 7 giorni × 2 pasti.
    Ordine: lun_pranzo, lun_cena, mar_pranzo, mar_cena, ... dom_pranzo, dom_cena.

    Se un valore contiene '|' indica doppio turno:
      es. "30|50" → primo turno 30 posti, secondo turno 50 posti.

    Se Calendario è vuoto/None → usa coperti per tutti i 14 slot (no doppio turno).

    Ogni elemento restituito:
      - senza doppio turno: {giorno, giorno_it, pasto, doppio_turno: false, coperti}
      - con doppio turno:   {giorno, giorno_it, pasto, doppio_turno: true,
                             primo_turno_coperti, secondo_turno_coperti}
    """
    result = []
    pasti = ["pranzo", "cena"]

    if not calendario or not calendario.strip():
        # Calendario vuoto → stesso valore coperti per tutti i 14 slot
        for i, giorno in enumerate(_GIORNI_SETTIMANA):
            for pasto in pasti:
                result.append({
                    "giorno": giorno,
                    "giorno_it": _GIORNI_SETTIMANA_IT[i],
                    "pasto": pasto,
                    "doppio_turno": False,
                    "coperti": coperti,
                })
        return result

    slots = [s.strip() for s in calendario.strip().split(",")]
    idx = 0
    for i, giorno in enumerate(_GIORNI_SETTIMANA):
        for pasto in pasti:
            slot = slots[idx] if idx < len(slots) else str(coperti)
            if "|" in slot:
                parts = slot.split("|", 1)
                try:
                    primo = int(parts[0].strip())
                    secondo = int(parts[1].strip())
                except ValueError:
                    primo = coperti
                    secondo = coperti
                result.append({
                    "giorno": giorno,
                    "giorno_it": _GIORNI_SETTIMANA_IT[i],
                    "pasto": pasto,
                    "doppio_turno": True,
                    "primo_turno_coperti": primo,
                    "secondo_turno_coperti": secondo,
                })
            else:
                try:
                    c = int(slot)
                except ValueError:
                    c = coperti
                result.append({
                    "giorno": giorno,
                    "giorno_it": _GIORNI_SETTIMANA_IT[i],
                    "pasto": pasto,
                    "doppio_turno": False,
                    "coperti": c,
                })
            idx += 1

    return result


@app.get("/esercizi")
async def get_esercizi():
    """Lista tutti gli esercizi con dati base (ID, nome, telefono, città, coperti, attivo)."""
    pool = await _get_esercizi_pool()
    try:
        import aiomysql
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT ID, NomeRapp, Telefono, Email, Citta, Coperti, Attivo "
                    "FROM Esercizi ORDER BY ID"
                )
                rows = await cur.fetchall()
        return {"ok": True, "esercizi": [dict(r) for r in rows]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore DB Esercizi: {e}")


@app.get("/esercizi/disponibilita")
async def get_disponibilita_tutti():
    """
    Restituisce la disponibilità settimanale (pranzo/cena) per tutti gli esercizi attivi.

    Per ogni esercizio e per ogni giorno/pasto indica:
    - se c'è doppio turno (doppio_turno: true) con i posti disponibili per ciascun turno
    - altrimenti il numero di coperti disponibili per quel giorno/pasto

    Se il campo Calendario è vuoto, usa il valore Coperti come default per tutti i turni.
    """
    pool = await _get_esercizi_pool()
    try:
        import aiomysql
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT ID, NomeRapp, Coperti, Calendario "
                    "FROM Esercizi WHERE Attivo = 'SI' ORDER BY ID"
                )
                rows = await cur.fetchall()
        result = []
        for row in rows:
            row = dict(row)
            disponibilita = _parse_calendario(row.get("Calendario"), int(row.get("Coperti") or 0))
            result.append({
                "esercizio_id": row["ID"],
                "nome": row["NomeRapp"],
                "coperti_default": row["Coperti"],
                "disponibilita": disponibilita,
            })
        return {"ok": True, "esercizi": result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore DB Esercizi: {e}")


@app.get("/esercizi/{esercizio_id}/disponibilita")
async def get_disponibilita_esercizio(esercizio_id: int):
    """
    Restituisce la disponibilità settimanale (pranzo/cena) per un singolo esercizio.

    Per ogni giorno/pasto indica:
    - se c'è doppio turno (doppio_turno: true) con i posti disponibili per ciascun turno
    - altrimenti il numero di coperti disponibili

    Se il campo Calendario è vuoto, usa il valore Coperti come default per tutti i turni.
    """
    pool = await _get_esercizi_pool()
    try:
        import aiomysql
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT ID, NomeRapp, Coperti, Calendario FROM Esercizi WHERE ID = %s",
                    (esercizio_id,),
                )
                row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Esercizio {esercizio_id} non trovato.")
        row = dict(row)
        disponibilita = _parse_calendario(row.get("Calendario"), int(row.get("Coperti") or 0))
        return {
            "ok": True,
            "esercizio_id": row["ID"],
            "nome": row["NomeRapp"],
            "coperti_default": row["Coperti"],
            "disponibilita": disponibilita,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore DB Esercizi: {e}")


# ============================================================
# DISPONIBILITÀ RESIDUA — capienza vs prenotazioni attive
# ============================================================

# Indice base nel calendario per ogni giorno della settimana
# date.weekday() → 0=lun, 1=mar, ..., 6=dom
# Calendario: lun_pranzo(0), lun_cena(1), mar_pranzo(2), mar_cena(3), ...
_WEEKDAY_SLOT_BASE: Dict[int, int] = {0: 0, 1: 2, 2: 4, 3: 6, 4: 8, 5: 10, 6: 12}

# Finestre orarie per doppio turno — keyed by ID reale del DB Esercizi
# (1=Talenti, 2=Reggio Calabria, 3=Ostia, 4=Appia, 5=Palermo, 6=Corso Trieste)
_DOUBLE_TURN_WINDOWS: Dict[int, Dict[str, List[Tuple[time, time, str]]]] = {
    1: {  # Talenti
        "pranzo": [
            (time(12, 0), time(13, 15), "primo"),
            (time(13, 30), time(23, 59), "secondo"),
        ],
        "cena": [
            (time(19, 0), time(20, 45), "primo"),
            (time(21, 0), time(23, 59), "secondo"),
        ],
    },
    2: {  # Reggio Calabria
        "cena": [
            (time(19, 30), time(21, 15), "primo"),
            (time(21, 30), time(23, 59), "secondo"),
        ],
    },
    4: {  # Appia
        "pranzo": [
            (time(12, 0), time(13, 20), "primo"),
            (time(13, 30), time(23, 59), "secondo"),
        ],
        "cena": [
            (time(19, 30), time(21, 15), "primo"),
            (time(21, 30), time(23, 59), "secondo"),
        ],
    },
    5: {  # Palermo
        "pranzo": [
            (time(12, 0), time(13, 20), "primo"),
            (time(13, 30), time(23, 59), "secondo"),
        ],
        "cena": [
            (time(19, 30), time(21, 15), "primo"),
            (time(21, 30), time(23, 59), "secondo"),
        ],
    },
    # 3 (Ostia) e 6 (Corso Trieste): nessun doppio turno
}


def _capacity_for_date_service(
    calendario: Optional[str], coperti: int, target_date: date, service: str
) -> Dict[str, Any]:
    """
    Estrae la capienza per una data e un servizio specifici dal campo Calendario.
    Ritorna un dict con:
      - senza doppio turno: {double_turn: False, capacity_total: N}
      - con doppio turno:   {double_turn: True, capacity_first_turn: N, capacity_second_turn: M}
    """
    idx = _WEEKDAY_SLOT_BASE[target_date.weekday()] + (0 if service == "pranzo" else 1)

    if not calendario or not calendario.strip():
        return {"double_turn": False, "capacity_total": int(coperti or 0)}

    slots = [s.strip() for s in calendario.strip().split(",")]
    slot = slots[idx] if idx < len(slots) else str(coperti)

    if "|" in slot:
        parts = slot.split("|", 1)
        try:
            primo = int(parts[0].strip())
            secondo = int(parts[1].strip())
        except ValueError:
            primo = secondo = int(coperti or 0)
        return {"double_turn": True, "capacity_first_turn": primo, "capacity_second_turn": secondo}

    try:
        cap = int(slot)
    except ValueError:
        cap = int(coperti or 0)
    if cap <= 0:
        cap = int(coperti or 0)
    return {"double_turn": False, "capacity_total": cap}


def _service_from_booking_time(ora: Any) -> Optional[str]:
    """Ricava il servizio (pranzo/cena) dall'orario di una prenotazione."""
    if isinstance(ora, timedelta):
        total_sec = int(ora.total_seconds())
        hh = (total_sec // 3600) % 24
        mm = (total_sec % 3600) // 60
        ora = time(hh, mm)
    elif isinstance(ora, str):
        try:
            ora = datetime.strptime(ora, "%H:%M:%S").time()
        except ValueError:
            try:
                ora = datetime.strptime(ora, "%H:%M").time()
            except ValueError:
                return None
    if not isinstance(ora, time):
        return None
    if time(12, 0) <= ora <= time(16, 0):
        return "pranzo"
    if time(17, 0) <= ora <= time(23, 59):
        return "cena"
    return None


def _turn_from_booking_time(restaurant_id: int, service: str, ora: Any) -> Optional[str]:
    """Ricava il turno (primo/secondo) dall'orario di una prenotazione con doppio turno."""
    if isinstance(ora, timedelta):
        total_sec = int(ora.total_seconds())
        hh = (total_sec // 3600) % 24
        mm = (total_sec % 3600) // 60
        ora = time(hh, mm)
    elif isinstance(ora, str):
        try:
            ora = datetime.strptime(ora, "%H:%M:%S").time()
        except ValueError:
            try:
                ora = datetime.strptime(ora, "%H:%M").time()
            except ValueError:
                return None
    if not isinstance(ora, time):
        return None
    for start, end, label in _DOUBLE_TURN_WINDOWS.get(restaurant_id, {}).get(service, []):
        if start <= ora <= end:
            return label
    return None


async def _reserved_for_service(
    pool, restaurant_id: int, target_date: date, service: str
) -> Dict[str, int]:
    """
    Somma i coperti delle prenotazioni attive (APERTA + CONFERMATA)
    per un esercizio, una data e un servizio specifici.
    Se il servizio ha doppio turno, suddivide il totale per turno.
    """
    import aiomysql
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                """
                SELECT OraPren, Coperti
                FROM Prenotazioni
                WHERE PRistorante = %s
                  AND DataPren = %s
                  AND Stato IN ('APERTA', 'CONFERMATA')
                """,
                (restaurant_id, target_date),
            )
            rows = await cur.fetchall()

    reserved_total = 0
    reserved_first = 0
    reserved_second = 0

    for row in rows:
        covers = int(row.get("Coperti") or 0)
        ora = row.get("OraPren")
        svc = _service_from_booking_time(ora)
        if svc != service:
            continue
        reserved_total += covers
        turn = _turn_from_booking_time(restaurant_id, service, ora)
        if turn == "primo":
            reserved_first += covers
        elif turn == "secondo":
            reserved_second += covers

    return {
        "reserved_total": reserved_total,
        "reserved_first_turn": reserved_first,
        "reserved_second_turn": reserved_second,
    }


async def _build_remaining_payload(
    pool, esercizio: Dict[str, Any], target_date: date, service: str
) -> Dict[str, Any]:
    restaurant_id = int(esercizio["ID"])
    cap = _capacity_for_date_service(
        esercizio.get("Calendario"), int(esercizio.get("Coperti") or 0), target_date, service
    )
    res = await _reserved_for_service(pool, restaurant_id, target_date, service)

    base: Dict[str, Any] = {
        "restaurant_id": restaurant_id,
        "restaurant_name": esercizio.get("Nome"),
        "date": target_date.isoformat(),
        "service": service,
    }

    if cap["double_turn"]:
        c1 = cap["capacity_first_turn"]
        c2 = cap["capacity_second_turn"]
        r1 = res["reserved_first_turn"]
        r2 = res["reserved_second_turn"]
        base.update({
            "double_turn": True,
            "capacity_first_turn": c1,
            "capacity_second_turn": c2,
            "reserved_first_turn": r1,
            "reserved_second_turn": r2,
            "remaining_first_turn": max(0, c1 - r1),
            "remaining_second_turn": max(0, c2 - r2),
            "capacity_total": c1 + c2,
            "reserved_total": r1 + r2,
            "remaining_total": max(0, c1 - r1) + max(0, c2 - r2),
        })
    else:
        ct = cap["capacity_total"]
        rt = res["reserved_total"]
        base.update({
            "double_turn": False,
            "capacity_total": ct,
            "reserved_total": rt,
            "remaining_total": max(0, ct - rt),
        })

    return base


@app.get("/_health/mysql")
async def mysql_healthcheck():
    """Health check connessione MySQL (database Esercizi)."""
    try:
        pool = await _get_esercizi_pool()
        import aiomysql
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("SELECT 1 AS ok")
                row = await cur.fetchone()
        return {"ok": True, "mysql": dict(row)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"MySQL connection error: {e}")


@app.get("/availability/capacity")
async def availability_capacity(
    restaurant_id: int = Query(...),
    target_date: str = Query(..., description="YYYY-MM-DD"),
    service: str = Query(..., description="pranzo | cena"),
):
    """Capienza teorica per un esercizio, una data e un servizio (pranzo/cena)."""
    try:
        parsed_date = date.fromisoformat(target_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="target_date deve essere YYYY-MM-DD")
    service = service.strip().lower()
    if service not in ("pranzo", "cena"):
        raise HTTPException(status_code=400, detail="service deve essere 'pranzo' oppure 'cena'")

    try:
        pool = await _get_esercizi_pool()
        import aiomysql
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT ID, NomeRapp, Coperti, Calendario FROM Esercizi WHERE ID = %s",
                    (restaurant_id,),
                )
                row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Esercizio {restaurant_id} non trovato.")
        row = dict(row)
        cap = _capacity_for_date_service(
            row.get("Calendario"), int(row.get("Coperti") or 0), parsed_date, service
        )
        return {
            "restaurant_id": int(row["ID"]),
            "restaurant_name": row.get("Nome"),
            "date": parsed_date.isoformat(),
            "service": service,
            **cap,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore DB: {e}")


@app.get("/availability/remaining")
async def availability_remaining(
    restaurant_id: int = Query(...),
    target_date: str = Query(..., description="YYYY-MM-DD"),
    service: str = Query(..., description="pranzo | cena"),
):
    """
    Posti rimanenti = capienza teorica - prenotazioni attive (APERTA + CONFERMATA).
    Se il servizio ha doppio turno restituisce i posti rimanenti per ciascun turno.
    """
    try:
        parsed_date = date.fromisoformat(target_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="target_date deve essere YYYY-MM-DD")
    service = service.strip().lower()
    if service not in ("pranzo", "cena"):
        raise HTTPException(status_code=400, detail="service deve essere 'pranzo' oppure 'cena'")

    try:
        pool = await _get_esercizi_pool()
        import aiomysql
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT ID, NomeRapp, Coperti, Calendario FROM Esercizi WHERE ID = %s",
                    (restaurant_id,),
                )
                row = await cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Esercizio {restaurant_id} non trovato.")
        return await _build_remaining_payload(pool, dict(row), parsed_date, service)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore DB: {e}")


@app.get("/availability/remaining/all")
async def availability_remaining_all(
    target_date: str = Query(..., description="YYYY-MM-DD"),
    service: str = Query(..., description="pranzo | cena"),
    only_active: bool = Query(True, description="Se True, include solo esercizi con Attivo='SI'"),
):
    """
    Posti rimanenti per tutti gli esercizi in una data e un servizio specifico.
    Utile per un colpo d'occhio sulla disponibilità di tutte le sedi.
    """
    try:
        parsed_date = date.fromisoformat(target_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="target_date deve essere YYYY-MM-DD")
    service = service.strip().lower()
    if service not in ("pranzo", "cena"):
        raise HTTPException(status_code=400, detail="service deve essere 'pranzo' oppure 'cena'")

    try:
        pool = await _get_esercizi_pool()
        import aiomysql
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                q = "SELECT ID, NomeRapp, Coperti, Calendario, Attivo FROM Esercizi"
                if only_active:
                    q += " WHERE Attivo = 'SI'"
                q += " ORDER BY ID"
                await cur.execute(q)
                rows = await cur.fetchall()
        items = []
        for row in rows:
            items.append(await _build_remaining_payload(pool, dict(row), parsed_date, service))
        return {"date": parsed_date.isoformat(), "service": service, "items": items}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore DB: {e}")


# ============================================================
# DIRECT BOOK — prenotazione diretta su MySQL (senza Playwright)
# ============================================================

import secrets as _secrets

_DIRECT_BOOK_SOURCE = os.getenv("DIRECT_BOOKING_SOURCE", "AI")
_DIRECT_BOOK_STATUS = os.getenv("DIRECT_BOOKING_STATUS", "APERTA")


class DirectBookIn(BaseModel):
    fase: Optional[str] = None  # ignored, accepted for book_table compatibility
    nome: str = Field(..., min_length=1)
    telefono: str
    sede: Optional[str] = None
    restaurant_id: Optional[int] = None
    data: str = Field(..., description="YYYY-MM-DD")
    orario: str = Field(..., description="HH:MM")
    coperti: Optional[int] = Field(None, ge=1, le=50)
    persone: Optional[str] = None  # alias from book_table, converted to coperti
    cognome: Optional[str] = None
    email: Optional[str] = None
    nota: Optional[str] = None
    seggiolini: int = Field(0, ge=0, le=3)

    @model_validator(mode="before")
    @classmethod
    def _book_table_compat(cls, values):
        """Accept book_table field names: persone→coperti, string→int coercion."""
        if isinstance(values, dict):
            # persone → coperti
            if values.get("coperti") is None and values.get("persone") is not None:
                try:
                    values["coperti"] = int(values["persone"])
                except (ValueError, TypeError):
                    pass
            # string → int coercion for seggiolini
            seg = values.get("seggiolini")
            if isinstance(seg, str):
                try:
                    values["seggiolini"] = int(seg)
                except (ValueError, TypeError):
                    values["seggiolini"] = 0
        return values

    @validator("data")
    @classmethod
    def validate_data(cls, v):
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("data deve essere in formato YYYY-MM-DD")
        return v

    @validator("orario")
    @classmethod
    def validate_orario(cls, v):
        if not re.fullmatch(r"\d{2}:\d{2}", (v or "").strip()):
            raise ValueError("orario deve essere in formato HH:MM")
        return v.strip()

    @validator("telefono")
    @classmethod
    def normalize_phone(cls, v):
        digits = re.sub(r"[^\d+]", "", v or "")
        if len(re.sub(r"\D", "", digits)) < 6:
            raise ValueError("telefono non valido")
        return digits

    @validator("cognome", pre=True, always=True)
    @classmethod
    def default_cognome(cls, v):
        return (v or "Cliente").strip() or "Cliente"

    @validator("nota", pre=True, always=True)
    @classmethod
    def normalize_nota(cls, v):
        return (v or "").strip()[:500]

    @validator("email", pre=True, always=True)
    @classmethod
    def normalize_email(cls, v):
        if not v:
            return None
        v = v.strip()
        if v and "@" not in v:
            raise ValueError("email non valida")
        return v or None


def _resolve_restaurant_id_direct(sede: Optional[str], restaurant_id: Optional[int]) -> int:
    if restaurant_id:
        return int(restaurant_id)
    if sede:
        rid = SEDE_ID_MAP.get(sede.strip().lower())
        if rid:
            return rid
    raise HTTPException(status_code=400, detail="Devi fornire sede oppure restaurant_id valido")


def _parse_time_hhmm(orario: str) -> time:
    try:
        return datetime.strptime(orario, "%H:%M").time()
    except ValueError:
        raise HTTPException(status_code=400, detail="orario deve essere in formato HH:MM")


def _double_turn_error_msg(restaurant_id: int, service: str) -> str:
    windows = _DOUBLE_TURN_WINDOWS.get(restaurant_id, {}).get(service, [])
    if len(windows) >= 2:
        primo_start = windows[0][0].strftime("%H:%M")
        primo_end = windows[0][1].strftime("%H:%M")
        secondo_start = windows[1][0].strftime("%H:%M")
        return (
            f"Orario ambiguo per servizio con doppio turno: "
            f"primo {primo_start}-{primo_end}, secondo dalle {secondo_start}"
        )
    return "Orario ambiguo in un servizio con doppio turno"


class DirectCancelIn(BaseModel):
    telefono: str
    nome: Optional[str] = None
    data: Optional[str] = None          # YYYY-MM-DD
    sede: Optional[str] = None
    restaurant_id: Optional[int] = None

    @validator("telefono")
    @classmethod
    def _clean_phone(cls, v: str) -> str:
        return re.sub(r"[^\d+]", "", v)


@app.post("/direct_cancel")
async def direct_cancel(body: DirectCancelIn):
    """
    Annulla una prenotazione APERTA su MySQL.

    Ricerca per telefono + (nome OPPURE data). Se trova esattamente 1 risultato
    con Stato='APERTA', lo aggiorna a 'ANNULLATA'. Se ne trova >1, restituisce
    la lista per disambiguare.
    """
    import aiomysql

    phone = body.telefono
    if not phone:
        raise HTTPException(status_code=400, detail="telefono è obbligatorio")

    if not body.nome and not body.data:
        raise HTTPException(
            status_code=400,
            detail="Serve almeno uno tra nome e data per identificare la prenotazione",
        )

    pool = await _get_esercizi_pool()

    # ── Build WHERE clause ──────────────────────────────────────────────
    conditions = ["Telefono = %s", "Stato = 'APERTA'"]
    params: list = [phone]

    if body.nome:
        conditions.append("LOWER(Nome) = LOWER(%s)")
        params.append(body.nome.strip())

    if body.data:
        conditions.append("DataPren = %s")
        params.append(body.data)

    rid = None
    if body.sede or body.restaurant_id:
        try:
            rid = _resolve_restaurant_id_direct(body.sede, body.restaurant_id)
            conditions.append("PRistorante = %s")
            params.append(rid)
        except HTTPException:
            pass  # sede not critical for search

    where = " AND ".join(conditions)

    # ── Search ──────────────────────────────────────────────────────────
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                f"""
                SELECT ID, PRistorante, DataPren, OraPren, Nome, Cognome,
                       Telefono, Coperti, Stato, Nota
                FROM Prenotazioni
                WHERE {where}
                ORDER BY DataPren DESC, OraPren DESC
                """,
                params,
            )
            rows = await cur.fetchall()

    if not rows:
        return {
            "ok": False,
            "status": "NOT_FOUND",
            "message": "Nessuna prenotazione aperta trovata con i dati forniti.",
        }

    if len(rows) > 1:
        # Multiple matches — return list for disambiguation
        matches = []
        for r in rows:
            matches.append({
                "id": r["ID"],
                "restaurant_id": r["PRistorante"],
                "date": str(r["DataPren"]),
                "time": str(r["OraPren"]),
                "nome": r["Nome"],
                "covers": r["Coperti"],
            })
        return {
            "ok": False,
            "status": "MULTIPLE",
            "message": f"Trovate {len(rows)} prenotazioni aperte. Servono più dettagli per identificare quella giusta.",
            "matches": matches,
        }

    # ── Exactly 1 match → cancel it ────────────────────────────────────
    row = rows[0]
    booking_id = row["ID"]

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE Prenotazioni SET Stato = 'ANNULLATA' WHERE ID = %s AND Stato = 'APERTA'",
                (booking_id,),
            )
            affected = cur.rowcount

    if affected == 0:
        return {
            "ok": False,
            "status": "ERROR",
            "message": "La prenotazione non è più aperta (potrebbe essere già stata annullata).",
        }

    # Fetch restaurant name
    restaurant_name = None
    try:
        async with pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("SELECT Nome FROM Esercizi WHERE ID = %s", (row["PRistorante"],))
                erow = await cur.fetchone()
                if erow:
                    restaurant_name = erow["Nome"]
    except Exception:
        pass

    return {
        "ok": True,
        "message": "Prenotazione annullata correttamente",
        "booking_id": booking_id,
        "restaurant_id": row["PRistorante"],
        "restaurant_name": restaurant_name,
        "date": str(row["DataPren"]),
        "time": str(row["OraPren"]),
        "nome": row["Nome"],
        "covers": row["Coperti"],
        "status": "ANNULLATA",
    }


@app.post("/direct_book")
async def direct_book(body: DirectBookIn):
    """
    Prenotazione diretta su MySQL: verifica disponibilità residua e inserisce in Prenotazioni.
    Non usa Playwright. Fonte=AI, Stato=APERTA per default.
    Campi richiesti: nome, telefono, sede o restaurant_id, data, orario, coperti.
    Facoltativi: cognome, email, nota, seggiolini.
    """
    import aiomysql

    restaurant_id = _resolve_restaurant_id_direct(body.sede, body.restaurant_id)
    booking_date = date.fromisoformat(body.data)
    booking_time = _parse_time_hhmm(body.orario)
    service = _service_from_booking_time(booking_time)
    if service is None:
        raise HTTPException(
            status_code=400,
            detail="L'orario deve essere in fascia pranzo (12:00-16:00) o cena (17:00-23:59)",
        )

    pool = await _get_esercizi_pool()

    # Fetch esercizio
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT ID, NomeRapp, Coperti, Calendario FROM Esercizi WHERE ID = %s",
                (restaurant_id,),
            )
            esercizio_row = await cur.fetchone()
    if not esercizio_row:
        raise HTTPException(status_code=404, detail=f"Esercizio {restaurant_id} non trovato")
    esercizio = dict(esercizio_row)

    # Verifica disponibilità residua
    remaining = await _build_remaining_payload(pool, esercizio, booking_date, service)

    if remaining["double_turn"]:
        turno = _turn_from_booking_time(restaurant_id, service, booking_time)
        if turno not in ("primo", "secondo"):
            raise HTTPException(
                status_code=400,
                detail=_double_turn_error_msg(restaurant_id, service),
            )
        rem_key = "remaining_first_turn" if turno == "primo" else "remaining_second_turn"
        available = remaining[rem_key]
        if available < body.coperti:
            raise HTTPException(
                status_code=409,
                detail={
                    "ok": False,
                    "status": "SOLD_OUT",
                    "message": f"Posti insufficienti per il {turno} turno",
                    "turno": turno,
                    "remaining": available,
                    "requested": body.coperti,
                    "availability": remaining,
                },
            )
    else:
        turno = None
        available = remaining["remaining_total"]
        if available < body.coperti:
            raise HTTPException(
                status_code=409,
                detail={
                    "ok": False,
                    "status": "SOLD_OUT",
                    "message": "Posti insufficienti per il servizio richiesto",
                    "remaining": available,
                    "requested": body.coperti,
                    "availability": remaining,
                },
            )

    # Inserimento in Prenotazioni
    code_id = _secrets.token_hex(8)  # 16 chars hex
    orario_db = f"{body.orario}:00"  # HH:MM → HH:MM:SS

    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO Prenotazioni (
                        PRistorante, DataPren, OraPren, Nome, Cognome, Telefono, Email,
                        Coperti, Seggiolini, Fonte, Stato, Nota, Prezzo, Tavolo,
                        PCliente, CodeID, Voto, Commento
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s
                    )
                    """,
                    (
                        restaurant_id,
                        body.data,
                        orario_db,
                        body.nome.strip(),
                        body.cognome or "Cliente",
                        body.telefono,
                        body.email or "",
                        body.coperti,
                        body.seggiolini,
                        _DIRECT_BOOK_SOURCE,
                        _DIRECT_BOOK_STATUS,
                        body.nota or "",
                        "0.00",
                        "",
                        0,
                        code_id,
                        0,
                        "",
                    ),
                )
                booking_id = cur.lastrowid
    except aiomysql.MySQLError as e:
        raise HTTPException(status_code=500, detail=f"Errore MySQL: {e}")

    return {
        "ok": True,
        "message": "Prenotazione inserita correttamente",
        "booking_id": booking_id,
        "code_id": code_id,
        "restaurant_id": restaurant_id,
        "restaurant_name": esercizio.get("Nome"),
        "date": body.data,
        "time": body.orario,
        "service": service,
        "turno": turno,
        "covers": body.coperti,
        "nome": body.nome.strip(),
        "cognome": body.cognome or "Cliente",
        "telefono": body.telefono,
        "status": _DIRECT_BOOK_STATUS,
        "source": _DIRECT_BOOK_SOURCE,
        "availability_at_booking": remaining,
    }


# ============================================================
# CHANGE DATE — Modifica data/ora di una prenotazione esistente
# ============================================================


class ChangeDateIn(BaseModel):
    telefono: str
    nome: Optional[str] = None
    data_attuale: Optional[str] = None       # YYYY-MM-DD della prenotazione esistente
    sede: Optional[str] = None
    restaurant_id: Optional[int] = None
    nuova_data: str = Field(..., description="YYYY-MM-DD nuova data")
    nuovo_orario: str = Field(..., description="HH:MM nuovo orario")
    nuovi_coperti: Optional[int] = Field(None, ge=1, le=50, description="Nuovo numero di persone (opzionale)")

    @validator("telefono")
    @classmethod
    def _clean_phone(cls, v: str) -> str:
        return re.sub(r"[^\d+]", "", v)

    @validator("nuova_data")
    @classmethod
    def _validate_nuova_data(cls, v):
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("nuova_data deve essere in formato YYYY-MM-DD")
        return v

    @validator("nuovo_orario")
    @classmethod
    def _validate_nuovo_orario(cls, v):
        if not re.fullmatch(r"\d{2}:\d{2}", (v or "").strip()):
            raise ValueError("nuovo_orario deve essere in formato HH:MM")
        return v.strip()


@app.post("/change_date")
async def change_date(body: ChangeDateIn):
    """
    Modifica data/ora di una prenotazione esistente.

    1. Cerca la prenotazione APERTA per telefono + (nome OPPURE data_attuale)
    2. Verifica disponibilità alla nuova data/ora
    3. Se disponibile: annulla la vecchia e crea la nuova con gli stessi dati
    4. Se non disponibile: restituisce SOLD_OUT con info disponibilità
    """
    import aiomysql

    phone = body.telefono
    if not phone:
        raise HTTPException(status_code=400, detail="telefono è obbligatorio")

    if not body.nome and not body.data_attuale:
        raise HTTPException(
            status_code=400,
            detail="Serve almeno uno tra nome e data_attuale per identificare la prenotazione",
        )

    pool = await _get_esercizi_pool()

    # ── 1. Cerca la prenotazione esistente ────────────────────────
    conditions = ["Telefono = %s", "Stato = 'APERTA'"]
    params: list = [phone]

    if body.nome:
        conditions.append("LOWER(Nome) = LOWER(%s)")
        params.append(body.nome.strip())

    if body.data_attuale:
        conditions.append("DataPren = %s")
        params.append(body.data_attuale)

    rid = None
    if body.sede or body.restaurant_id:
        try:
            rid = _resolve_restaurant_id_direct(body.sede, body.restaurant_id)
            conditions.append("PRistorante = %s")
            params.append(rid)
        except HTTPException:
            pass

    where = " AND ".join(conditions)

    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                f"""
                SELECT ID, PRistorante, DataPren, OraPren, Nome, Cognome,
                       Telefono, Email, Coperti, Seggiolini, Stato, Nota
                FROM Prenotazioni
                WHERE {where}
                ORDER BY DataPren DESC, OraPren DESC
                """,
                params,
            )
            rows = await cur.fetchall()

    if not rows:
        return {
            "ok": False,
            "status": "NOT_FOUND",
            "message": "Nessuna prenotazione aperta trovata con i dati forniti.",
        }

    if len(rows) > 1:
        matches = []
        for r in rows:
            matches.append({
                "id": r["ID"],
                "restaurant_id": r["PRistorante"],
                "date": str(r["DataPren"]),
                "time": str(r["OraPren"]),
                "nome": r["Nome"],
                "covers": r["Coperti"],
            })
        return {
            "ok": False,
            "status": "MULTIPLE",
            "message": f"Trovate {len(rows)} prenotazioni aperte. Servono più dettagli.",
            "matches": matches,
        }

    # ── Prenotazione trovata ──────────────────────────────────────
    old = rows[0]
    old_id = old["ID"]
    restaurant_id = old["PRistorante"]

    # ── 2. Verifica disponibilità alla nuova data/ora ─────────────
    new_date = date.fromisoformat(body.nuova_data)
    new_time = _parse_time_hhmm(body.nuovo_orario)
    service = _service_from_booking_time(new_time)
    if service is None:
        raise HTTPException(
            status_code=400,
            detail="L'orario deve essere in fascia pranzo (12:00-16:00) o cena (17:00-23:59)",
        )

    # Fetch esercizio
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT ID, NomeRapp, Coperti, Calendario FROM Esercizi WHERE ID = %s",
                (restaurant_id,),
            )
            esercizio_row = await cur.fetchone()
    if not esercizio_row:
        raise HTTPException(status_code=404, detail=f"Esercizio {restaurant_id} non trovato")
    esercizio = dict(esercizio_row)

    coperti = body.nuovi_coperti if body.nuovi_coperti is not None else old["Coperti"]
    remaining = await _build_remaining_payload(pool, esercizio, new_date, service)

    # Dato che la vecchia prenotazione verrà annullata, aggiungiamo i suoi coperti
    # alla disponibilità residua (solo se stessa sede + stessa data + stesso servizio)
    old_date = old["DataPren"] if isinstance(old["DataPren"], date) else date.fromisoformat(str(old["DataPren"]))
    old_time_str = str(old["OraPren"])
    if len(old_time_str) > 5:
        old_time_str = old_time_str[:5]
    old_time_obj = _parse_time_hhmm(old_time_str)
    old_service = _service_from_booking_time(old_time_obj)
    same_slot = (old_date == new_date and old_service == service and restaurant_id == old["PRistorante"])

    extra_seats = coperti if same_slot else 0

    if remaining["double_turn"]:
        turno = _turn_from_booking_time(restaurant_id, service, new_time)
        if turno not in ("primo", "secondo"):
            raise HTTPException(
                status_code=400,
                detail=_double_turn_error_msg(restaurant_id, service),
            )
        rem_key = "remaining_first_turn" if turno == "primo" else "remaining_second_turn"

        # If same slot + same turn, the old booking's seats will be freed
        if same_slot:
            old_turno = _turn_from_booking_time(restaurant_id, old_service, old_time_obj)
            if old_turno == turno:
                extra_seats = coperti
            else:
                extra_seats = 0

        available = remaining[rem_key] + extra_seats
        if available < coperti:
            return {
                "ok": False,
                "status": "SOLD_OUT",
                "message": f"Posti insufficienti per il {turno} turno alla nuova data/ora",
                "turno": turno,
                "remaining": remaining[rem_key],
                "requested": coperti,
                "availability": remaining,
            }
    else:
        turno = None
        available = remaining["remaining_total"] + extra_seats
        if available < coperti:
            return {
                "ok": False,
                "status": "SOLD_OUT",
                "message": "Posti insufficienti per il servizio richiesto alla nuova data/ora",
                "remaining": remaining["remaining_total"],
                "requested": coperti,
                "availability": remaining,
            }

    # ── 3. Annulla la vecchia prenotazione ────────────────────────
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE Prenotazioni SET Stato = 'ANNULLATA' WHERE ID = %s AND Stato = 'APERTA'",
                (old_id,),
            )
            affected = cur.rowcount

    if affected == 0:
        return {
            "ok": False,
            "status": "ERROR",
            "message": "La prenotazione non è più aperta (potrebbe essere già stata modificata).",
        }

    # ── 4. Crea la nuova prenotazione con i dati della vecchia ────
    code_id = _secrets.token_hex(8)
    orario_db = f"{body.nuovo_orario}:00"

    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO Prenotazioni (
                        PRistorante, DataPren, OraPren, Nome, Cognome, Telefono, Email,
                        Coperti, Seggiolini, Fonte, Stato, Nota, Prezzo, Tavolo,
                        PCliente, CodeID, Voto, Commento
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s
                    )
                    """,
                    (
                        restaurant_id,
                        body.nuova_data,
                        orario_db,
                        old["Nome"],
                        old["Cognome"] or "Cliente",
                        old["Telefono"],
                        old.get("Email") or "",
                        coperti,
                        old.get("Seggiolini") or 0,
                        _DIRECT_BOOK_SOURCE,
                        _DIRECT_BOOK_STATUS,
                        old.get("Nota") or "",
                        "0.00",
                        "",
                        0,
                        code_id,
                        0,
                        "",
                    ),
                )
                new_booking_id = cur.lastrowid
    except aiomysql.MySQLError as e:
        # Rollback: riapri la vecchia prenotazione
        try:
            async with pool.acquire() as conn2:
                async with conn2.cursor() as cur2:
                    await cur2.execute(
                        "UPDATE Prenotazioni SET Stato = 'APERTA' WHERE ID = %s",
                        (old_id,),
                    )
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Errore MySQL durante inserimento: {e}")

    restaurant_name = _ID_TO_SEDE_NAME.get(restaurant_id)

    return {
        "ok": True,
        "message": "Prenotazione spostata correttamente",
        "old_booking_id": old_id,
        "new_booking_id": new_booking_id,
        "code_id": code_id,
        "restaurant_id": restaurant_id,
        "restaurant_name": restaurant_name,
        "old_date": str(old["DataPren"]),
        "old_time": old_time_str,
        "new_date": body.nuova_data,
        "new_time": body.nuovo_orario,
        "service": service,
        "turno": turno,
        "covers": coperti,
        "old_covers": old["Coperti"],
        "covers_changed": body.nuovi_coperti is not None and body.nuovi_coperti != old["Coperti"],
        "nome": old["Nome"],
        "telefono": old["Telefono"],
        "status": _DIRECT_BOOK_STATUS,
    }
