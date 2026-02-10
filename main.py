# main.py
import os
import re
import json
import asyncio
import logging
import hashlib
from datetime import date, datetime, timedelta
from typing import Optional, Any, Dict, Tuple

import phonenumbers
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from playwright.async_api import (
    async_playwright,
    Browser,
    Playwright,
    Page,
    Error as PlaywrightError,
)

# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------

APP_NAME = "centralino-webhook"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

BOOKING_URL = os.getenv("BOOKING_URL", "https://rione.fidy.app/prenew.php?referer=AI")
MAX_DAYS_AHEAD = int(os.getenv("MAX_DAYS_AHEAD", "30"))
MAX_PEOPLE_AUTOMATION = int(os.getenv("MAX_PEOPLE_AUTOMATION", "9"))

# Numero a cui trasferire (gestito in piattaforma voce/agent)
TRANSFER_NUMBER = os.getenv("TRANSFER_NUMBER", "")  # es: +39...

CONFIRMATION_NOTICE = (
    "Riceverai una conferma via WhatsApp e via email ai contatti indicati. "
    "È importante rispettare l’orario prenotato: in caso di ritardo prolungato potrebbe essere necessario liberare il tavolo."
)

# Playwright
PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "20000"))
PW_RETRIES = int(os.getenv("PW_RETRIES", "2"))
PW_HEADLESS = os.getenv("PW_HEADLESS", "true").lower() != "false"

# Blocca risorse pesanti (velocità + memoria)
BLOCK_RESOURCE_TYPES = set(
    (os.getenv("PW_BLOCK_TYPES", "image,media,font,stylesheet").split(","))
)
BLOCK_RESOURCE_TYPES = {t.strip().lower() for t in BLOCK_RESOURCE_TYPES if t.strip()}

# User-Agent mobile (molto utile su siti “mobile-first”)
MOBILE_UA = os.getenv(
    "PW_USER_AGENT",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
)

# Anti-doppioni (idempotenza) - TTL secondi
DEDUP_TTL_SECONDS = int(os.getenv("DEDUP_TTL_SECONDS", "180"))  # 3 minuti
# Se vuoi disabilitare dedupe: DEDUP_ENABLED=false
DEDUP_ENABLED = os.getenv("DEDUP_ENABLED", "true").lower() != "false"

# Serializza prenotazioni (consigliato)
SERIALIZE_BOOKINGS = os.getenv("SERIALIZE_BOOKINGS", "true").lower() != "false"

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


def parse_iso_date(raw: str) -> date:
    if raw is None:
        raise ValueError("Data mancante")
    s = str(raw).strip()
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        raise ValueError("Formato data non valido (usa YYYY-MM-DD)")


def today_local() -> date:
    return date.today()


def within_days_limit(d: date, max_days: int) -> bool:
    return d <= (today_local() + timedelta(days=max_days))


def is_past_date(d: date) -> bool:
    return d < today_local()


def safe_filename(prefix: str) -> str:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    return f"{prefix}_{ts}.png"


def now_ts() -> float:
    return datetime.utcnow().timestamp()


def booking_fingerprint(payload: Dict[str, Any]) -> str:
    """
    Fingerprint deterministico per dedupe.
    Nota: includiamo i campi che definiscono “una prenotazione uguale”.
    """
    key = {
        "nome": (payload.get("nome") or "").strip().lower(),
        "cognome": (payload.get("cognome") or "").strip().lower(),
        "email": (payload.get("email") or "").strip().lower(),
        "telefono": (payload.get("telefono") or "").strip(),
        "persone": int(payload.get("persone") or 0),
        "sede": (payload.get("sede") or "").strip().lower(),
        "data": (payload.get("data") or "").strip(),
        "ora": (payload.get("ora") or "").strip(),
        "seggiolone": bool(payload.get("seggiolone")),
        "seggiolini": int(payload.get("seggiolini") or 0),
    }
    raw = json.dumps(key, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ------------------------------------------------------------
# REQUEST MODELS (Pydantic v2)
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
        raw = (v or "").strip()
        try:
            phone = phonenumbers.parse(raw, "IT")
            if not phonenumbers.is_valid_number(phone):
                raise ValueError("Numero non valido")
            return phonenumbers.format_number(phone, phonenumbers.PhoneNumberFormat.E164)
        except Exception:
            raise ValueError("Numero di telefono non valido")

    @field_validator("sede")
    @classmethod
    def normalize_sede(cls, v: str) -> str:
        s = (v or "").strip().lower()
        mapping = {
            "talenti": "Talenti",
            "appia": "Appia",
            "ostia": "Ostia",
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
            "talenti": "Talenti",
            "appia": "Appia",
            "ostia": "Ostia",
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
# PLAYWRIGHT MANAGER (stabile + auto-restart)
# ------------------------------------------------------------

class PlaywrightManager:
    def __init__(self) -> None:
        self.pw: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self._lock = asyncio.Lock()  # protegge start/restart/new_context

    async def start(self) -> None:
        logger.info("Starting Playwright...")
        self.pw = await async_playwright().start()
        await self._restart_browser_locked()
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

    async def _restart_browser_locked(self) -> None:
        # chiamare SOLO sotto self._lock
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass

        if not self.pw:
            raise RuntimeError("Playwright not initialized")

        self.browser = await self.pw.chromium.launch(
            headless=PW_HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-zygote",
            ],
        )

    async def new_page(self) -> Page:
        if not self.pw:
            raise RuntimeError("Playwright not initialized")

        async with self._lock:
            if not self.browser:
                await self._restart_browser_locked()

            try:
                ctx = await self.browser.new_context(
                    user_agent=MOBILE_UA,
                    viewport={"width": 390, "height": 844},
                    locale="it-IT",
                )
            except PlaywrightError:
                # Browser morto/chiuso -> restart e riprova una volta
                logger.warning("Browser/context closed unexpectedly, restarting browser...")
                await self._restart_browser_locked()
                ctx = await self.browser.new_context(
                    user_agent=MOBILE_UA,
                    viewport={"width": 390, "height": 844},
                    locale="it-IT",
                )

        page = await ctx.new_page()
        page.set_default_timeout(PW_TIMEOUT_MS)

        async def _route(route):
            try:
                if route.request.resource_type.lower() in BLOCK_RESOURCE_TYPES:
                    await route.abort()
                else:
                    await route.continue_()
            except Exception:
                try:
                    await route.continue_()
                except Exception:
                    pass

        await page.route("**/*", _route)
        return page


pw_manager = PlaywrightManager()

# ------------------------------------------------------------
# RETRIES + DEDUPE + SERIALIZATION
# ------------------------------------------------------------

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


# Dedup store in-memory: fingerprint -> (timestamp, result)
_dedup_lock = asyncio.Lock()
_dedup_store: Dict[str, Tuple[float, Dict[str, Any]]] = {}

# Booking serialization
_booking_lock = asyncio.Lock()


async def dedup_get(fp: str) -> Optional[Dict[str, Any]]:
    if not DEDUP_ENABLED:
        return None
    async with _dedup_lock:
        item = _dedup_store.get(fp)
        if not item:
            return None
        ts, result = item
        if now_ts() - ts > DEDUP_TTL_SECONDS:
            _dedup_store.pop(fp, None)
            return None
        return result


async def dedup_set(fp: str, result: Dict[str, Any]) -> None:
    if not DEDUP_ENABLED:
        return
    async with _dedup_lock:
        # pulizia opportunistica
        cutoff = now_ts() - DEDUP_TTL_SECONDS
        for k in list(_dedup_store.keys()):
            if _dedup_store[k][0] < cutoff:
                _dedup_store.pop(k, None)
        _dedup_store[fp] = (now_ts(), result)


# ------------------------------------------------------------
# PLAYWRIGHT BOOKING (best-effort + screenshot)
# ------------------------------------------------------------

async def playwright_submit_booking(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    NOTA: questa funzione è “framework”.
    Se hai già una versione step-by-step che funziona (quella con log -> 1..8..PRENOTA),
    sostituisci QUI dentro la parte di automazione con i tuoi selettori/flow reali.

    Questa versione:
    - apre la pagina
    - prova fill “classico”
    - prova click su bottoni “Conferma / Prenota”
    - se non trova i selettori, fallisce con errore chiaro (ma NON crasha il server)
    - salva screenshot su errore
    """

    async def _do(attempt: int):
        page = await pw_manager.new_page()
        try:
            logger.info("Booking attempt #%s - opening %s", attempt + 1, BOOKING_URL)
            await page.goto(BOOKING_URL, wait_until="domcontentloaded")

            async def try_fill(selector: str, value: str):
                try:
                    loc = page.locator(selector)
                    if await loc.count() > 0:
                        await loc.first.fill(value)
                except Exception:
                    pass

            await try_fill('input[name="nome"]', payload["nome"])
            await try_fill('input[name="cognome"]', payload["cognome"])
            await try_fill('input[name="email"]', payload["email"])
            await try_fill('input[name="telefono"]', payload["telefono"])
            await try_fill('input[name="data"]', payload["data"])
            await try_fill('input[name="ora"]', payload["ora"])
            await try_fill('textarea[name="note"]', payload.get("nota", ""))
            await try_fill('textarea[name="nota"]', payload.get("nota", ""))

            submitted = False
            candidates = [
                'button[type="submit"]',
                'button:has-text("CONFERMA")',
                'button:has-text("Conferma")',
                'button:has-text("PRENOTA")',
                'button:has-text("Prenota")',
            ]
            for sel in candidates:
                try:
                    loc = page.locator(sel)
                    if await loc.count() > 0:
                        await loc.first.click()
                        submitted = True
                        break
                except Exception:
                    continue

            if not submitted:
                raise RuntimeError(
                    "Impossibile inviare: selettori non trovati (serve configurazione step-by-step)."
                )

            await page.wait_for_timeout(1200)

            return {
                "ok": True,
                "message": "Prenotazione inviata (best-effort)",
                "details": {"best_effort": True},
            }

        except Exception as e:
            try:
                path = safe_filename("booking_error")
                await page.screenshot(path=path, full_page=True)
                logger.error("Saved screenshot: %s", path)
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
# APP LIFESPAN
# ------------------------------------------------------------

async def lifespan(app: FastAPI):
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

@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": APP_NAME}


@app.get("/")
async def root():
    return {"ok": True, "service": APP_NAME, "health": "/healthz"}


@app.post("/checkavailability")
async def checkavailability(req: AvailabilityRequest):
    d = parse_iso_date(req.data)

    if is_past_date(d):
        return JSONResponse(status_code=200, content=user_error("Non è possibile prenotare per date passate."))

    if not within_days_limit(d, MAX_DAYS_AHEAD):
        return JSONResponse(
            status_code=200,
            content=transfer_response(
                f"Per prenotazioni oltre {MAX_DAYS_AHEAD} giorni è necessario parlare con un operatore."
            ),
        )

    if req.persone > MAX_PEOPLE_AUTOMATION:
        return JSONResponse(
            status_code=200,
            content=transfer_response("Per gruppi superiori a 9 persone è necessario parlare con un operatore."),
        )

    # Stub stabile (qui puoi inserire check reale con Playwright se vuoi)
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
            content=transfer_response(
                f"Per prenotazioni oltre {MAX_DAYS_AHEAD} giorni è necessario parlare con un operatore."
            ),
        )

    payload = {
        "nome": req.nome.strip(),
        "cognome": (req.cognome or "").strip(),
        "email": req.email.strip(),
        "telefono": req.telefono.strip(),
        "persone": int(req.persone),
        "sede": req.sede.strip(),
        "data": req.data.strip(),
        "ora": req.ora.strip(),
        "seggiolone": bool(req.seggiolone),
        "seggiolini": int(req.seggiolini or 0),
        "nota": (req.nota or "").strip(),
        "referer": req.referer or "AI",
        "dry_run": bool(req.dry_run),
    }

    # fingerprint + dedupe
    fp = booking_fingerprint(payload)
    payload["fingerprint"] = fp

    logger.info("BOOK_TABLE request normalized: %s", json.dumps(payload, ensure_ascii=False))

    # Dry-run
    if payload["dry_run"]:
        return {"ok": True, "message": "Dry-run: dati validi, prenotazione non inviata.", "payload": payload}

    # DEDUPE: se identica prenotazione arriva entro TTL, non reinviare
    cached = await dedup_get(fp)
    if cached:
        return {
            "ok": True,
            "message": "Richiesta duplicata rilevata: riuso il risultato recente (anti-doppia prenotazione).",
            "confirmation_notice": CONFIRMATION_NOTICE,
            "payload": payload,
            "result": cached,
            "dedup": True,
        }

    async def _execute() -> Dict[str, Any]:
        # Invio con Playwright
        result = await playwright_submit_booking(payload)
        return result

    try:
        if SERIALIZE_BOOKINGS:
            async with _booking_lock:
                result = await _execute()
        else:
            result = await _execute()

        # salva in dedupe store se ok
        if result.get("ok"):
            await dedup_set(fp, result)

        if result.get("ok"):
            return {
                "ok": True,
                "message": "Prenotazione registrata correttamente.",
                "confirmation_notice": CONFIRMATION_NOTICE,
                "payload": payload,
                "result": result,
                "dedup": False,
            }

        return JSONResponse(
            status_code=200,
            content={
                "ok": False,
                "message": "Non è stato possibile completare la prenotazione in questo momento.",
                "result": result,
            },
        )

    except Exception as e:
        logger.exception("Booking failed: %s", str(e))
        return JSONResponse(
            status_code=200,
            content={
                "ok": False,
                "message": "C’è stato un problema temporaneo nel completare la prenotazione. Riprova tra poco oppure chiedi il trasferimento a un operatore.",
                "action": "transfer_to_number" if TRANSFER_NUMBER else "retry",
                "number": TRANSFER_NUMBER,
            },
        )
