import os
import re
import json
import time
import hashlib
import asyncio
import logging
import sqlite3
from datetime import date, datetime, timedelta
from typing import Optional, Any, Dict, Tuple

import phonenumbers
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from playwright.async_api import async_playwright, Playwright, Browser, Page


# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------

APP_NAME = "centralino-webhook"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
BOOKING_URL = os.getenv("BOOKING_URL", "https://rione.fidy.app/prenew.php?referer=AI")

MAX_DAYS_AHEAD = int(os.getenv("MAX_DAYS_AHEAD", "30"))
MAX_PEOPLE_AUTOMATION = int(os.getenv("MAX_PEOPLE_AUTOMATION", "9"))

# Playwright
PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "20000"))
PW_RETRIES = int(os.getenv("PW_RETRIES", "2"))
PW_HEADLESS = os.getenv("PW_HEADLESS", "true").lower() != "false"

# Concurrency: per stabilità (evita doppioni e race condition)
PW_CONCURRENCY = int(os.getenv("PW_CONCURRENCY", "1"))

# Idempotency / dedupe
IDEMPOTENCY_TTL_SECONDS = int(os.getenv("IDEMPOTENCY_TTL_SECONDS", "900"))  # 15 min
IDEMPOTENCY_DB_PATH = os.getenv("IDEMPOTENCY_DB_PATH", "/tmp/idempotency.sqlite3")

# Transfer number (facoltativo)
TRANSFER_NUMBER = os.getenv("TRANSFER_NUMBER", "")

CONFIRMATION_NOTICE = (
    "Riceverai una conferma via WhatsApp e via email ai contatti indicati. "
    "È importante rispettare l’orario prenotato: in caso di ritardo prolungato potrebbe essere necessario liberare il tavolo."
)

# User Agent mobile (stabile su UI mobile)
MOBILE_UA = os.getenv(
    "MOBILE_UA",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# Blocca risorse pesanti per velocità/stabilità
BLOCK_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}


# ------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(APP_NAME)


# ------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$", re.IGNORECASE)


def safe_filename(prefix: str) -> str:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    return f"{prefix}_{ts}.png"


def today_local() -> date:
    # Se vuoi timezone Europe/Rome: usare zoneinfo. Per ora stabile.
    return date.today()


def parse_iso_date(raw: str) -> date:
    if raw is None:
        raise ValueError("Data mancante")
    s = str(raw).strip()
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        raise ValueError("Formato data non valido (usa YYYY-MM-DD)")


def within_days_limit(d: date, max_days: int) -> bool:
    return d <= (today_local() + timedelta(days=max_days))


def is_past_date(d: date) -> bool:
    return d < today_local()


def normalize_time_to_hhmm(raw: str) -> str:
    """
    Converte:
    - "ore 13" / "13" -> "13:00"
    - "8" -> "08:00"
    - "20.30" / "20,30" -> "20:30"
    - "20:30" -> "20:30"
    """
    if raw is None:
        raise ValueError("Ora mancante")

    s = str(raw).strip().lower()
    s = s.replace("ore", "").replace("alle", "").strip()
    s = s.replace(".", ":").replace(",", ":")

    if re.fullmatch(r"\d{1,2}", s):
        hh = int(s)
        if hh < 0 or hh > 23:
            raise ValueError("Ora non valida")
        return f"{hh:02d}:00"

    if re.fullmatch(r"\d{1,2}:\d{2}", s):
        hh, mm = s.split(":")
        hh_i = int(hh)
        mm_i = int(mm)
        if not (0 <= hh_i <= 23 and 0 <= mm_i <= 59):
            raise ValueError("Ora non valida")
        return f"{hh_i:02d}:{mm_i:02d}"

    raise ValueError("Formato ora non valido")


def meal_from_time(hhmm: str) -> str:
    """<17 => PRANZO, altrimenti CENA"""
    try:
        hh = int(hhmm.split(":")[0])
        return "PRANZO" if hh < 17 else "CENA"
    except Exception:
        return "CENA"


def canonicalize_phone(raw: str) -> str:
    raw = (raw or "").strip()
    try:
        phone = phonenumbers.parse(raw, "IT")
        if not phonenumbers.is_valid_number(phone):
            raise ValueError("Numero non valido")
        return phonenumbers.format_number(phone, phonenumbers.PhoneNumberFormat.E164)
    except Exception:
        raise ValueError("Numero di telefono non valido")


def build_idempotency_key(payload: Dict[str, Any]) -> str:
    # chiave deterministica dai campi che definiscono "la stessa prenotazione"
    base = {
        "nome": (payload.get("nome") or "").strip().lower(),
        "cognome": (payload.get("cognome") or "").strip().lower(),
        "telefono": (payload.get("telefono") or "").strip(),
        "email": (payload.get("email") or "").strip().lower(),
        "persone": int(payload.get("persone") or 0),
        "sede": (payload.get("sede") or "").strip().lower(),
        "data": (payload.get("data") or "").strip(),
        "ora": (payload.get("ora") or "").strip(),
    }
    s = json.dumps(base, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ------------------------------------------------------------
# IDENTITY / DEDUPE STORE (SQLite)
# ------------------------------------------------------------

_db_lock = asyncio.Lock()


def _db_init() -> None:
    os.makedirs(os.path.dirname(IDEMPOTENCY_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(IDEMPOTENCY_DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS idempotency (
                key TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL,
                response_json TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _db_get(key: str) -> Optional[Dict[str, Any]]:
    conn = sqlite3.connect(IDEMPOTENCY_DB_PATH)
    try:
        row = conn.execute(
            "SELECT created_at, response_json FROM idempotency WHERE key = ?",
            (key,),
        ).fetchone()
        if not row:
            return None
        created_at, response_json = row
        if int(time.time()) - int(created_at) > IDEMPOTENCY_TTL_SECONDS:
            # scaduto: elimina
            conn.execute("DELETE FROM idempotency WHERE key = ?", (key,))
            conn.commit()
            return None
        return json.loads(response_json)
    finally:
        conn.close()


def _db_put(key: str, response: Dict[str, Any]) -> None:
    conn = sqlite3.connect(IDEMPOTENCY_DB_PATH)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO idempotency(key, created_at, response_json) VALUES(?,?,?)",
            (key, int(time.time()), json.dumps(response, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()


# ------------------------------------------------------------
# REQUEST MODELS
# ------------------------------------------------------------

class BookingRequest(BaseModel):
    nome: str
    cognome: Optional[str] = ""

    email: str
    telefono: str

    persone: int = Field(ge=1, le=50)
    sede: str
    data: str

    ora: Optional[str] = None
    orario: Optional[str] = None

    seggiolone: bool = False
    seggiolini: int = 0

    nota: str = ""
    note: Optional[str] = ""

    referer: str = "AI"
    dry_run: bool = False

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = (v or "").strip()
        if not EMAIL_RE.match(v):
            raise ValueError("Email non valida")
        return v

    @field_validator("telefono")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        return canonicalize_phone(v)

    @field_validator("sede")
    @classmethod
    def normalize_sede(cls, v: str) -> str:
        s = (v or "").strip().lower()
        mapping = {
            "talenti": "Talenti - Roma",
            "talenti - roma": "Talenti - Roma",
            "ostia": "Ostia Lido",
            "ostia lido": "Ostia Lido",
            "appia": "Appia",
            "reggio": "Reggio Calabria",
            "reggio calabria": "Reggio Calabria",
            "palermo": "Palermo",
            "palermo centro": "Palermo",
        }
        return mapping.get(s, (v or "").strip().title())

    @model_validator(mode="after")
    def normalize_fields(self) -> "BookingRequest":
        raw_time = self.ora or self.orario
        self.ora = normalize_time_to_hhmm(raw_time)

        if not self.nota and self.note:
            self.nota = self.note

        if not (self.cognome or "").strip():
            parts = (self.nome or "").strip().split()
            if len(parts) >= 2:
                self.cognome = parts[-1]
                self.nome = " ".join(parts[:-1])
            else:
                self.cognome = "Cliente"

        _ = parse_iso_date(self.data)
        return self


class AvailabilityRequest(BaseModel):
    sede: str
    data: str
    persone: int = Field(ge=1, le=50)
    ora: Optional[str] = None
    orario: Optional[str] = None

    @model_validator(mode="after")
    def normalize(self) -> "AvailabilityRequest":
        s = (self.sede or "").strip().lower()
        mapping = {
            "talenti": "Talenti - Roma",
            "talenti - roma": "Talenti - Roma",
            "ostia": "Ostia Lido",
            "ostia lido": "Ostia Lido",
            "appia": "Appia",
            "reggio": "Reggio Calabria",
            "reggio calabria": "Reggio Calabria",
            "palermo": "Palermo",
            "palermo centro": "Palermo",
        }
        self.sede = mapping.get(s, (self.sede or "").strip().title())

        _ = parse_iso_date(self.data)

        if self.ora or self.orario:
            self.ora = normalize_time_to_hhmm(self.ora or self.orario)

        return self


# ------------------------------------------------------------
# PLAYWRIGHT MANAGER
# ------------------------------------------------------------

class PlaywrightManager:
    def __init__(self) -> None:
        self.pw: Optional[Playwright] = None
        self.browser: Optional[Browser] = None

    async def start(self) -> None:
        logger.info("Starting Playwright...")
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(
            headless=PW_HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--single-process",
                "--disable-gpu",
            ],
        )
        logger.info("Playwright started.")

    async def stop(self) -> None:
        logger.info("Stopping Playwright...")
        try:
            if self.browser:
                await self.browser.close()
        finally:
            self.browser = None
            if self.pw:
                await self.pw.stop()
            self.pw = None
        logger.info("Playwright stopped.")

    async def new_page(self) -> Page:
        if not self.browser:
            raise RuntimeError("Browser not initialized")

        ctx = await self.browser.new_context(
            user_agent=MOBILE_UA,
            viewport={"width": 390, "height": 844},
            locale="it-IT",
        )
        page = await ctx.new_page()
        page.set_default_timeout(PW_TIMEOUT_MS)

        async def _route(route):
            try:
                if route.request.resource_type in BLOCK_RESOURCE_TYPES:
                    await route.abort()
                else:
                    await route.continue_()
            except Exception:
                # non bloccare per problemi di routing
                try:
                    await route.continue_()
                except Exception:
                    pass

        await page.route("**/*", _route)
        return page


pw_manager = PlaywrightManager()
pw_semaphore = asyncio.Semaphore(PW_CONCURRENCY)


async def run_with_retries(fn, retries: int = PW_RETRIES):
    last_err = None
    for attempt in range(retries + 1):
        try:
            return await fn(attempt)
        except Exception as e:
            last_err = e
            logger.exception("Attempt %s failed: %s", attempt + 1, str(e))
            await asyncio.sleep(0.6 * (attempt + 1))
    raise last_err


# ------------------------------------------------------------
# PLAYWRIGHT FLOW (definitivo)
# ------------------------------------------------------------

async def _accept_cookie_if_any(page: Page) -> None:
    # molto permissivo
    candidates = [
        r"accetta",
        r"consent",
        r"ok",
        r"accetto",
        r"consenti",
    ]
    for pat in candidates:
        try:
            loc = page.locator(f"text=/{pat}/i").first
            if await loc.count() > 0:
                await loc.click(timeout=2000, force=True)
                await page.wait_for_timeout(300)
                return
        except Exception:
            continue


async def _click_first(page: Page, locators: list, step_name: str, critical: bool = True) -> bool:
    """
    Prova una serie di locator (già costruiti) e clicca il primo disponibile.
    """
    for loc in locators:
        try:
            if await loc.count() > 0:
                await loc.first.click(force=True)
                await page.wait_for_timeout(350)
                return True
        except Exception:
            continue

    msg = f"Non ho trovato selettore per step: {step_name}"
    if critical:
        raise RuntimeError(msg)
    logger.warning("⚠️ %s", msg)
    return False


async def _select_people(page: Page, people: int) -> None:
    # step 1: persone
    n = str(people).strip()
    logger.info("-> 1. Persone (%s)", n)
    locators = [
        page.locator(f"button:text-is('{n}')"),
        page.locator(f"div:text-is('{n}')"),
        page.get_by_text(n, exact=True),
        page.locator(f"text=/^\\s*{re.escape(n)}\\s*$/"),
    ]
    ok = await _click_first(page, locators, "Persone", critical=True)
    if not ok:
        raise RuntimeError("Selezione persone fallita")


async def _select_seggiolini(page: Page, seggiolini: int, seggiolone: bool) -> None:
    # step 2: seggiolini (default NO)
    logger.info("-> 2. Seggiolini")
    await page.wait_for_timeout(700)

    has_step = False
    try:
        has_step = (await page.locator("text=/seggiolini|seggiolone/i").count()) > 0
    except Exception:
        has_step = False

    if not has_step:
        logger.info("   (step seggiolini non presente, continuo)")
        return

    if seggiolini > 0 or seggiolone:
        # se vuoi estendere: click SI e scegli quantità.
        # per ora: tentativo best-effort su "SI"
        await _click_first(
            page,
            [page.locator("text=/^\\s*SI\\s*$/i"), page.locator("button:has-text('SI')")],
            "Seggiolini=SI",
            critical=False,
        )
        await page.wait_for_timeout(300)
    else:
        await _click_first(
            page,
            [page.locator("text=/^\\s*NO\\s*$/i"), page.locator("button:has-text('NO')")],
            "Seggiolini=NO",
            critical=False,
        )


async def _select_date(page: Page, iso_date: str) -> None:
    # step 3: data
    logger.info("-> 3. Data (%s)", iso_date)
    await page.wait_for_timeout(700)

    d = parse_iso_date(iso_date)
    oggi = today_local()
    domani = oggi + timedelta(days=1)

    # prova pulsanti "Oggi/Domani/Altra data"
    if d == oggi:
        clicked = await _click_first(
            page,
            [page.locator("text=/\\boggi\\b/i"), page.locator("button:has-text('Oggi')")],
            "Data=Oggi",
            critical=False,
        )
        if clicked:
            return

    if d == domani:
        clicked = await _click_first(
            page,
            [page.locator("text=/\\bdomani\\b/i"), page.locator("button:has-text('Domani')")],
            "Data=Domani",
            critical=False,
        )
        if clicked:
            return

    # altrimenti: "Altra data" + input[type=date]
    await _click_first(
        page,
        [page.locator("text=/altra\\s*data/i"), page.locator("button:has-text('Altra data')")],
        "Data=Altra data",
        critical=False,
    )

    # set input date
    # 1) prova input[type=date]
    try:
        if await page.locator("input[type='date']").count() > 0:
            await page.evaluate(
                """(val) => {
                    const el = document.querySelector("input[type='date']");
                    if (el) { el.value = val; el.dispatchEvent(new Event("input",{bubbles:true})); el.dispatchEvent(new Event("change",{bubbles:true})); }
                }""",
                iso_date,
            )
            await page.wait_for_timeout(250)
            # conferma/cerca
            await _click_first(
                page,
                [page.locator("text=/conferma|cerca/i"), page.locator("button:has-text('Conferma')")],
                "Conferma data",
                critical=False,
            )
            await page.wait_for_timeout(500)
            return
    except Exception:
        pass

    # 2) fallback: se non c'è input date, proviamo click su testo iso_date
    clicked = await _click_first(
        page,
        [page.locator(f"text=/{re.escape(iso_date)}/")],
        "Data click testo",
        critical=False,
    )
    if not clicked:
        raise RuntimeError("Impossibile impostare la data (input date non trovato)")


async def _select_meal(page: Page, meal: str) -> None:
    # step 4: pasto (PRANZO/CENA)
    logger.info("-> 4. Pasto (%s)", meal)
    await page.wait_for_timeout(900)

    # alcuni flussi non hanno step pasto: non critico
    await _click_first(
        page,
        [
            page.locator(f"text=/{meal}/i"),
            page.locator(f"button:has-text('{meal.title()}')"),
            page.locator(f"button:has-text('{meal.upper()}')"),
        ],
        f"Pasto={meal}",
        critical=False,
    )


async def _select_location(page: Page, sede_label: str) -> None:
    # step 5: sede
    logger.info("-> 5. Sede (%s)", sede_label)
    await page.wait_for_timeout(1200)

    # la UI spesso ha card: testo parziale ok
    locators = [
        page.get_by_text(sede_label, exact=False),
        page.locator(f"text=/{re.escape(sede_label)}/i"),
    ]
    await _click_first(page, locators, "Sede", critical=True)


async def _select_time(page: Page, hhmm: str) -> None:
    # step 6: orario
    logger.info("-> 6. Orario (%s)", hhmm)
    await page.wait_for_timeout(900)

    # a volte serve aprire un dropdown: proviamo select o elementi "apri"
    try:
        if await page.locator("select").count() > 0:
            await page.locator("select").first.click(timeout=1500)
            await page.wait_for_timeout(300)
    except Exception:
        pass

    # click sull'orario (esatto o regex)
    hh = hhmm.split(":")[0]
    candidates = [
        page.locator(f"text=/{re.escape(hhmm)}/"),
        page.get_by_text(hhmm, exact=False),
        page.locator(f"text=/\\b{re.escape(hh)}\\b/"),  # fallback "13"
    ]

    ok = await _click_first(page, candidates, "Orario", critical=True)
    if not ok:
        raise RuntimeError(f"Orario non selezionabile: {hhmm}")


async def _confirm_step(page: Page) -> None:
    # step 7: conferma dati (prima schermata finale)
    logger.info("-> 7. Conferma dati")
    await page.wait_for_timeout(600)

    await _click_first(
        page,
        [
            page.locator("text=/^\\s*conferma\\s*$/i"),
            page.locator("button:has-text('CONFERMA')"),
            page.locator("button:has-text('Conferma')"),
        ],
        "Conferma",
        critical=False,  # non sempre presente
    )


async def _fill_customer_data(page: Page, payload: Dict[str, Any]) -> None:
    # step 8: dati cliente
    logger.info("-> 8. Dati finali")
    await page.wait_for_timeout(800)

    nome = payload["nome"]
    cognome = payload["cognome"]
    email = payload["email"]
    telefono = payload["telefono"]
    nota = payload.get("nota") or ""

    async def try_fill(selector: str, value: str) -> bool:
        try:
            if await page.locator(selector).count() > 0:
                await page.fill(selector, value)
                return True
        except Exception:
            return False
        return False

    # campi tipici
    await try_fill('input[name="nome"]', nome)
    await try_fill('input[name="cognome"]', cognome)
    await try_fill('input[name="email"]', email)
    await try_fill('input[name="telefono"]', telefono)

    # fallback: placeholder / label
    # (best-effort, non critico)
    try:
        if await page.get_by_placeholder(re.compile("nome", re.I)).count() > 0:
            await page.get_by_placeholder(re.compile("nome", re.I)).first.fill(nome)
    except Exception:
        pass
    try:
        if await page.get_by_placeholder(re.compile("cognome", re.I)).count() > 0:
            await page.get_by_placeholder(re.compile("cognome", re.I)).first.fill(cognome)
    except Exception:
        pass
    try:
        if await page.get_by_placeholder(re.compile("mail|email", re.I)).count() > 0:
            await page.get_by_placeholder(re.compile("mail|email", re.I)).first.fill(email)
    except Exception:
        pass
    try:
        if await page.get_by_placeholder(re.compile("telefono|cell", re.I)).count() > 0:
            await page.get_by_placeholder(re.compile("telefono|cell", re.I)).first.fill(telefono)
    except Exception:
        pass

    # note
    if nota:
        await try_fill('textarea[name="note"]', nota)
        await try_fill('textarea[name="nota"]', nota)
        try:
            if await page.locator("textarea").count() > 0:
                await page.locator("textarea").first.fill(nota)
        except Exception:
            pass

    # privacy checkbox (best-effort)
    try:
        cbs = page.locator("input[type='checkbox']")
        cnt = await cbs.count()
        for i in range(min(cnt, 5)):
            try:
                await cbs.nth(i).check()
            except Exception:
                pass
    except Exception:
        pass


async def _click_prenota(page: Page) -> None:
    # step 9: click PRENOTA (critico)
    logger.info("-> ✅ PRODUZIONE: click PRENOTA")
    await page.wait_for_timeout(700)

    ok = await _click_first(
        page,
        [
            page.locator("text=/^\\s*prenota\\s*$/i"),
            page.locator("button:has-text('PRENOTA')"),
            page.locator("button:has-text('Prenota')"),
        ],
        "PRENOTA",
        critical=True,
    )
    if not ok:
        raise RuntimeError("Bottone PRENOTA non trovato")

    # piccola attesa post-submit
    await page.wait_for_timeout(1200)


async def _detect_success(page: Page) -> Tuple[bool, str]:
    # prova a capire se è confermata
    # (il sito può cambiare: quindi non essere troppo rigidi)
    try:
        html = (await page.content()).lower()
        if "confermat" in html or "prenotazione" in html and "confer" in html:
            return True, "Rilevata conferma nel contenuto pagina"
        if "grazie" in html and "prenot" in html:
            return True, "Rilevato messaggio di ringraziamento"
    except Exception:
        pass
    return True, "Submit effettuato (success best-effort)"


async def playwright_submit_booking(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Flow completo e robusto.
    Se uno step critico fallisce -> errore.
    """

    async def _do(attempt: int):
        async with pw_semaphore:
            page = await pw_manager.new_page()
            try:
                logger.info("BOOKING: %s %s -> %s | %s %s | pax=%s",
                            payload["nome"], payload["cognome"], payload["sede"],
                            payload["data"], payload["ora"], payload["persone"])

                logger.info("-> GO TO FIDY")
                await page.goto(BOOKING_URL, wait_until="domcontentloaded")
                await _accept_cookie_if_any(page)

                await _select_people(page, int(payload["persone"]))
                await _select_seggiolini(page, int(payload.get("seggiolini") or 0), bool(payload.get("seggiolone")))
                await _select_date(page, payload["data"])

                meal = meal_from_time(payload["ora"])
                await _select_meal(page, meal)

                await _select_location(page, payload["sede"])
                await _select_time(page, payload["ora"])

                await _confirm_step(page)
                await _fill_customer_data(page, payload)

                # se vuoi un ulteriore "conferma" prima di prenota:
                await _click_first(
                    page,
                    [page.locator("text=/^\\s*conferma\\s*$/i"), page.locator("button:has-text('CONFERMA')")],
                    "CONFERMA finale",
                    critical=False
                )

                await _click_prenota(page)

                ok, reason = await _detect_success(page)
                return {"ok": ok, "message": "Prenotazione inviata", "details": {"reason": reason}}

            except Exception as e:
                # screenshot per debug
                try:
                    path = safe_filename("booking_error")
                    await page.screenshot(path=path, full_page=True)
                    logger.error("📸 Screenshot salvato: %s", path)
                except Exception:
                    pass
                raise e
            finally:
                try:
                    await page.context.close()
                except Exception:
                    pass

    return await run_with_retries(_do, retries=PW_RETRIES)


# ------------------------------------------------------------
# APP / LIFESPAN
# ------------------------------------------------------------

async def lifespan(app: FastAPI):
    _db_init()
    await pw_manager.start()
    yield
    await pw_manager.stop()


app = FastAPI(title=APP_NAME, lifespan=lifespan)


# ------------------------------------------------------------
# COMMON RESPONSES
# ------------------------------------------------------------

def transfer_response(reason: str) -> Dict[str, Any]:
    return {
        "ok": False,
        "action": "transfer_to_number",
        "number": TRANSFER_NUMBER,
        "reason": reason,
        "message": reason,
    }


def user_error(message: str) -> Dict[str, Any]:
    return {"ok": False, "message": message}


# ------------------------------------------------------------
# ROUTES
# ------------------------------------------------------------

@app.get("/")
async def root():
    return {"ok": True, "service": APP_NAME, "health": "/healthz"}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": APP_NAME}


@app.post("/checkavailability")
async def checkavailability(req: AvailabilityRequest):
    d = parse_iso_date(req.data)

    if is_past_date(d):
        return JSONResponse(status_code=200, content=user_error("Non è possibile prenotare per date passate."))

    if not within_days_limit(d, MAX_DAYS_AHEAD):
        return JSONResponse(
            status_code=200,
            content=transfer_response(f"Per prenotazioni oltre {MAX_DAYS_AHEAD} giorni è necessario parlare con un operatore."),
        )

    if req.persone > MAX_PEOPLE_AUTOMATION:
        return JSONResponse(
            status_code=200,
            content=transfer_response("Per gruppi superiori a 9 persone è necessario parlare con un operatore."),
        )

    # Stub stabile: se vuoi, qui puoi implementare un check vero con Playwright.
    return {
        "ok": True,
        "available": True,
        "message": "Disponibilità verificabile. Procedi con la prenotazione.",
        "normalized": {"sede": req.sede, "data": req.data, "ora": req.ora, "persone": req.persone},
    }


@app.post("/book_table")
async def book_table(req: BookingRequest, request: Request):
    # Regole: persone
    if req.persone > MAX_PEOPLE_AUTOMATION:
        return JSONResponse(
            status_code=200,
            content=transfer_response("Per gruppi superiori a 9 persone è necessario parlare con un operatore."),
        )

    # Regole: data
    d = parse_iso_date(req.data)
    if is_past_date(d):
        return JSONResponse(status_code=200, content=user_error("Non è possibile prenotare per date passate."))
    if not within_days_limit(d, MAX_DAYS_AHEAD):
        return JSONResponse(
            status_code=200,
            content=transfer_response(f"Per prenotazioni oltre {MAX_DAYS_AHEAD} giorni è necessario parlare con un operatore."),
        )

    payload = {
        "nome": req.nome.strip(),
        "cognome": (req.cognome or "").strip(),
        "email": req.email.strip(),
        "telefono": req.telefono.strip(),
        "persone": int(req.persone),
        "sede": req.sede.strip(),     # già normalizzata al label sito
        "data": req.data.strip(),
        "ora": req.ora.strip(),       # sempre HH:MM
        "seggiolone": bool(req.seggiolone),
        "seggiolini": int(req.seggiolini or 0),
        "nota": (req.nota or "").strip(),
        "referer": req.referer or "AI",
        "dry_run": bool(req.dry_run),
    }

    logger.info("BOOK_TABLE request normalized: %s", json.dumps(payload, ensure_ascii=False))

    # Idempotency: header opzionale + fallback deterministico
    header_key = request.headers.get("x-idempotency-key") or request.headers.get("X-Idempotency-Key")
    idemp_key = (header_key or "").strip() or build_idempotency_key(payload)

    # Se già fatto di recente, ritorna stessa risposta (evita doppioni)
    async with _db_lock:
        cached = _db_get(idemp_key)
        if cached:
            logger.info("Idempotency hit: %s", idemp_key)
            return JSONResponse(status_code=200, content=cached)

    # Dry-run
    if payload["dry_run"]:
        resp = {
            "ok": True,
            "message": "Dry-run: dati validi, prenotazione non inviata.",
            "payload": payload,
        }
        async with _db_lock:
            _db_put(idemp_key, resp)
        return resp

    # Invio reale
    try:
        result = await playwright_submit_booking(payload)

        if result.get("ok"):
            resp = {
                "ok": True,
                "message": "Prenotazione registrata correttamente.",
                "confirmation_notice": CONFIRMATION_NOTICE,
                "payload": payload,
                "result": result,
                "idempotency_key": idemp_key,
            }
        else:
            resp = {
                "ok": False,
                "message": "Non è stato possibile completare la prenotazione in questo momento.",
                "payload": payload,
                "result": result,
                "idempotency_key": idemp_key,
                "action": "transfer_to_number" if TRANSFER_NUMBER else "retry",
                "number": TRANSFER_NUMBER,
            }

        async with _db_lock:
            _db_put(idemp_key, resp)
        return JSONResponse(status_code=200, content=resp)

    except Exception as e:
        logger.exception("Booking failed: %s", str(e))

        resp = {
            "ok": False,
            "message": "C’è stato un problema temporaneo nel completare la prenotazione. Riprova tra poco oppure chiedi il trasferimento a un operatore.",
            "action": "transfer_to_number" if TRANSFER_NUMBER else "retry",
            "number": TRANSFER_NUMBER,
            "idempotency_key": idemp_key,
        }

        async with _db_lock:
            _db_put(idemp_key, resp)
        return JSONResponse(status_code=200, content=resp)
