import os
import re
import json
import asyncio
import logging
import hashlib
from datetime import date, datetime, timedelta
from typing import Optional, Any, Dict, Callable, Awaitable

import phonenumbers
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from playwright.async_api import async_playwright, Browser, Playwright, Page


# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------

APP_NAME = "centralino-webhook"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

BOOKING_URL = os.getenv("BOOKING_URL", "https://rione.fidy.app/prenew.php?referer=AI")
MAX_DAYS_AHEAD = int(os.getenv("MAX_DAYS_AHEAD", "30"))
MAX_PEOPLE_AUTOMATION = int(os.getenv("MAX_PEOPLE_AUTOMATION", "9"))

TRANSFER_NUMBER = os.getenv("TRANSFER_NUMBER", "")  # es: +39...
CONFIRMATION_NOTICE = (
    "Riceverai una conferma via WhatsApp e via email ai contatti indicati. "
    "È importante rispettare l’orario prenotato: in caso di ritardo prolungato potrebbe essere necessario liberare il tavolo."
)

# Playwright
PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "20000"))
PW_RETRIES = int(os.getenv("PW_RETRIES", "2"))
PW_HEADLESS = os.getenv("PW_HEADLESS", "true").lower() != "false"

# Dedup / anti-doppione (in-memory)
IDEMPOTENCY_TTL_SEC = int(os.getenv("IDEMPOTENCY_TTL_SEC", "120"))  # 2 minuti


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

SEDE_MAP = {
    "talenti": "Talenti - Roma",
    "appia": "Appia",
    "ostia": "Ostia Lido",
    "reggio": "Reggio Calabria",
    "reggio calabria": "Reggio Calabria",
    "palermo": "Palermo",
    "palermo centro": "Palermo",
}


def normalize_time_to_hhmm(raw: str) -> str:
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


def meal_from_time(hhmm: str) -> str:
    try:
        hh = int(hhmm.split(":")[0])
        return "PRANZO" if hh < 17 else "CENA"
    except Exception:
        return "CENA"


def normalize_sede(v: str) -> str:
    s = (v or "").strip().lower()
    return SEDE_MAP.get(s, (v or "").strip().title())


def make_fingerprint(payload: Dict[str, Any]) -> str:
    # fingerprint stabile (evita doppie prenotazioni su retry / doppio invio)
    keys = ["nome", "cognome", "telefono", "email", "sede", "data", "ora", "persone"]
    base = {k: payload.get(k) for k in keys}
    raw = json.dumps(base, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


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
    def sede_norm(cls, v: str) -> str:
        return normalize_sede(v)

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
        self.sede = normalize_sede(self.sede)
        _ = parse_iso_date(self.data)
        if self.ora or self.orario:
            self.ora = normalize_time_to_hhmm(self.ora or self.orario)
        return self


# ------------------------------------------------------------
# PLAYWRIGHT MANAGER (shared browser per process)
# ------------------------------------------------------------

class PlaywrightManager:
    def __init__(self) -> None:
        self.pw: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
            if self.browser:
                return
            logger.info("Starting Playwright...")
            self.pw = await async_playwright().start()
            self.browser = await self.pw.chromium.launch(
                headless=PW_HEADLESS,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--single-process",
                    "--no-zygote",
                ],
            )
            logger.info("Playwright started.")

    async def stop(self) -> None:
        async with self._lock:
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
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                       "Mobile/15E148 Safari/604.1",
            viewport={"width": 390, "height": 844},
        )

        # risparmio risorse
        async def _route(route):
            rt = route.request.resource_type
            if rt in ["image", "media", "font", "stylesheet"]:
                await route.abort()
            else:
                await route.continue_()

        await ctx.route("**/*", _route)

        page = await ctx.new_page()
        page.set_default_timeout(PW_TIMEOUT_MS)
        return page


pw_manager = PlaywrightManager()


async def run_with_retries(fn: Callable[[int], Awaitable[Any]], retries: int = PW_RETRIES):
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
# BOOKING FLOW (FIDY step-by-step)
# ------------------------------------------------------------

async def playwright_submit_booking(payload: Dict[str, Any]) -> Dict[str, Any]:
    async def _do(attempt: int):
        page = await pw_manager.new_page()
        try:
            pasto = meal_from_time(payload["ora"])
            logger.info("🚀 BOOKING: %s %s -> %s | %s %s | pax=%s | pasto=%s",
                        payload["nome"], payload["cognome"], payload["sede"],
                        payload["data"], payload["ora"], payload["persone"], pasto)

            logger.info("-> GO TO FIDY")
            await page.goto(BOOKING_URL, wait_until="domcontentloaded")

            # cookie (best effort)
            try:
                await page.locator("text=/accetta|consenti|ok/i").first.click(timeout=2000)
            except Exception:
                pass

            # 1) Persone
            logger.info("-> 1. Persone")
            try:
                # prova selettori numerici comuni
                p = str(payload["persone"])
                loc = page.locator(f"button:text-is('{p}'), div:text-is('{p}'), span:text-is('{p}')").first
                if await loc.count() > 0:
                    await loc.click(force=True, timeout=4000)
                else:
                    await page.get_by_text(p, exact=True).first.click(force=True, timeout=4000)
            except Exception:
                logger.warning("⚠️ Non ho trovato il bottone persone, continuo comunque...")

            # 2) Seggiolini (NO)
            logger.info("-> 2. Seggiolini")
            await page.wait_for_timeout(600)
            try:
                if await page.locator("text=/seggiolini/i").count() > 0:
                    await page.locator("text=/^\\s*NO\\s*$/i").first.click(force=True, timeout=3000)
            except Exception:
                pass

            # 3) Data (input type=date)
            logger.info("-> 3. Data")
            await page.wait_for_timeout(800)
            try:
                await page.evaluate(
                    "([val]) => { const el = document.querySelector('input[type=date]'); if (el) { el.value = val; el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); } }",
                    payload["data"],
                )
                try:
                    await page.locator("input[type=date]").press("Enter")
                except Exception:
                    pass
            except Exception:
                logger.warning("⚠️ Input date non trovato/iniettabile, continuo...")

            # (facoltativo) conferma/cerca
            try:
                await page.locator("text=/conferma|cerca/i").first.click(timeout=1500)
            except Exception:
                pass

            # 4) Pasto (PRANZO/CENA)
            logger.info("-> 4. Pasto (%s)", pasto)
            await page.wait_for_timeout(800)
            try:
                await page.locator(f"text=/{pasto}/i").first.click(timeout=4000)
            except Exception:
                # a volte è già filtrato/automatico
                pass

            # 5) Sede
            logger.info("-> 5. Sede (%s)", payload["sede"])
            await page.wait_for_timeout(1200)
            try:
                await page.get_by_text(payload["sede"], exact=False).first.click(force=True, timeout=6000)
            except Exception:
                # fallback: prova nome breve (es "Talenti")
                fallback = payload["sede"].split("-")[0].strip()
                await page.get_by_text(fallback, exact=False).first.click(force=True, timeout=6000)

            # 6) Orario
            logger.info("-> 6. Orario (%s)", payload["ora"])
            await page.wait_for_timeout(1200)
            orario_clean = payload["ora"].replace(".", ":")
            try:
                # a volte serve aprire un menu: click su select se presente
                try:
                    if await page.locator("select").count() > 0:
                        await page.locator("select").first.click(timeout=1000)
                except Exception:
                    pass

                loc_time = page.locator(f"text=/{re.escape(orario_clean)}/").first
                if await loc_time.count() > 0:
                    await loc_time.click(force=True, timeout=5000)
                else:
                    raise RuntimeError(f"Orario {orario_clean} non disponibile")
            except Exception as e:
                raise RuntimeError(str(e))

            # 7) Conferma (se presente)
            logger.info("-> 7. Conferma")
            await page.wait_for_timeout(800)
            try:
                await page.locator("text=/CONFERMA/i").first.click(force=True, timeout=3000)
            except Exception:
                pass

            # 8) Dati finali (se ci sono input)
            logger.info("-> 8. Dati finali")
            await page.wait_for_timeout(1200)

            async def try_fill(selector: str, value: str):
                try:
                    if await page.locator(selector).count() > 0:
                        await page.fill(selector, value)
                except Exception:
                    pass

            await try_fill('input[name="nome"]', payload["nome"])
            await try_fill('input[name="cognome"]', payload["cognome"])
            await try_fill('input[name="email"]', payload["email"])
            await try_fill('input[name="telefono"]', payload["telefono"])
            await try_fill('textarea[name="note"]', payload.get("nota", ""))

            # 9) PRENOTA
            logger.info("✅ PRODUZIONE: click PRENOTA")
            await page.wait_for_timeout(900)
            try:
                await page.locator("text=/PRENOTA/i").last.click(force=True, timeout=6000)
            except Exception:
                # fallback: bottone generico
                for sel in ['button:has-text("PRENOTA")', 'button:has-text("Prenota")']:
                    if await page.locator(sel).count() > 0:
                        await page.locator(sel).first.click(force=True, timeout=6000)
                        break
                else:
                    raise RuntimeError("Bottone PRENOTA non trovato")

            await page.wait_for_timeout(1500)
            return {"ok": True, "message": "Prenotazione completata", "details": {"pasto": pasto}}

        except Exception as e:
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
# APP + LIFESPAN
# ------------------------------------------------------------

# idempotency store
_idem_lock = asyncio.Lock()
_idem_store: Dict[str, float] = {}  # fingerprint -> expires_at (unix time)


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

@app.get("/")
async def root():
    return {"ok": True, "service": APP_NAME, "health": "/healthz"}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": APP_NAME}


@app.post("/checkavailability")
@app.post("/check_availability")
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

    return {
        "ok": True,
        "available": True,
        "message": "Disponibilità verificabile. Procedi con la prenotazione.",
        "normalized": {"sede": req.sede, "data": req.data, "ora": req.ora, "persone": req.persone},
    }


@app.post("/book_table")
async def book_table(req: BookingRequest, request: Request):
    if req.persone > MAX_PEOPLE_AUTOMATION:
        return JSONResponse(
            status_code=200,
            content=transfer_response("Per gruppi superiori a 9 persone è necessario parlare con un operatore."),
        )

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
        "sede": req.sede.strip(),
        "data": req.data.strip(),
        "ora": req.ora.strip(),
        "seggiolone": bool(req.seggiolone),
        "seggiolini": int(req.seggiolini or 0),
        "nota": (req.nota or "").strip(),
        "referer": req.referer or "AI",
        "dry_run": bool(req.dry_run),
    }

    fp = make_fingerprint(payload)
    payload["fingerprint"] = fp

    logger.info("BOOK_TABLE request normalized: %s", json.dumps(payload, ensure_ascii=False))

    # anti doppione
    now = datetime.utcnow().timestamp()
    async with _idem_lock:
        # cleanup
        expired = [k for k, exp in _idem_store.items() if exp <= now]
        for k in expired:
            _idem_store.pop(k, None)

        if fp in _idem_store:
            return {
                "ok": True,
                "message": "Richiesta già in lavorazione (anti-doppione).",
                "payload": payload,
                "result": {"ok": True, "details": {"idempotency": "hit"}},
            }
        _idem_store[fp] = now + IDEMPOTENCY_TTL_SEC

    if payload["dry_run"]:
        return {
            "ok": True,
            "message": "Dry-run: dati validi, prenotazione non inviata.",
            "payload": payload,
        }

    try:
        result = await playwright_submit_booking(payload)
        if result.get("ok"):
            return {
                "ok": True,
                "message": "Prenotazione registrata correttamente.",
                "confirmation_notice": CONFIRMATION_NOTICE,
                "payload": payload,
                "result": result,
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
    finally:
        # libera il lock idempotency un po' prima della TTL in caso di errori:
        # (se vuoi tenerlo fino a TTL, elimina questo blocco)
        pass
