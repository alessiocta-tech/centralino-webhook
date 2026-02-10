import os
import re
import json
import time
import asyncio
import logging
import hashlib
from datetime import date, datetime, timedelta
from typing import Optional, Any, Dict, Tuple

import phonenumbers
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from playwright.async_api import async_playwright, Playwright, Browser, Page, TimeoutError as PWTimeoutError


# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------

APP_NAME = "centralino-webhook"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

BOOKING_URL = os.getenv("BOOKING_URL", "https://rione.fidy.app/prenew.php?referer=AI")

MAX_DAYS_AHEAD = int(os.getenv("MAX_DAYS_AHEAD", "30"))
MAX_PEOPLE_AUTOMATION = int(os.getenv("MAX_PEOPLE_AUTOMATION", "9"))

TRANSFER_NUMBER = os.getenv("TRANSFER_NUMBER", "")
CONFIRMATION_NOTICE = os.getenv(
    "CONFIRMATION_NOTICE",
    "Riceverai una conferma via WhatsApp e via email ai contatti indicati. "
    "È importante rispettare l’orario prenotato: in caso di ritardo prolungato potrebbe essere necessario liberare il tavolo."
)

# Playwright runtime
PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "20000"))
PW_NAV_TIMEOUT_MS = int(os.getenv("PW_NAV_TIMEOUT_MS", "45000"))
PW_RETRIES = int(os.getenv("PW_RETRIES", "2"))
PW_HEADLESS = os.getenv("PW_HEADLESS", "true").lower() != "false"

# Concorrenza: riduci rischi (browser shared + 1/2 contesti alla volta)
PW_MAX_CONCURRENCY = int(os.getenv("PW_MAX_CONCURRENCY", "1"))

# Anti doppie: finestra idempotenza (secondi)
IDEMPOTENCY_TTL_SECONDS = int(os.getenv("IDEMPOTENCY_TTL_SECONDS", "120"))  # 2 minuti

# User-Agent mobile (spesso più “stabile” su stepper)
MOBILE_UA = os.getenv(
    "MOBILE_UA",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
)

# ------------------------------------------------------------
# LOGGING
# ------------------------------------------------------------

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(APP_NAME)

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$", re.IGNORECASE)


# ------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------

def utc_ts() -> str:
    return datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")

def safe_filename(prefix: str) -> str:
    return f"{prefix}_{utc_ts()}.png"

def today_local() -> date:
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
    if raw is None:
        raise ValueError("Ora mancante")
    s = str(raw).strip().lower()
    s = s.replace("ore", "").replace("alle", "").strip()
    s = s.replace(".", ":").replace(",", ":")

    if re.fullmatch(r"\d{1,2}", s):
        hh = int(s)
        if not (0 <= hh <= 23):
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
    # < 17 -> PRANZO, altrimenti CENA
    try:
        hh = int(hhmm.split(":")[0])
        return "PRANZO" if hh < 17 else "CENA"
    except Exception:
        return "CENA"

def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def make_fingerprint(payload: Dict[str, Any]) -> str:
    # fingerprint stabile per prevenire doppie
    key = {
        "nome": payload.get("nome", "").strip().lower(),
        "cognome": payload.get("cognome", "").strip().lower(),
        "telefono": payload.get("telefono", "").strip(),
        "email": payload.get("email", "").strip().lower(),
        "persone": int(payload.get("persone", 0)),
        "sede": payload.get("sede", "").strip().lower(),
        "data": payload.get("data", "").strip(),
        "ora": payload.get("ora", "").strip(),
    }
    return sha256_hex(json.dumps(key, ensure_ascii=False, sort_keys=True))


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
            "talenti - roma": "Talenti",
            "appia": "Appia",
            "ostia": "Ostia",
            "ostia lido": "Ostia",
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
        }
        self.sede = mapping.get(s, (self.sede or "").strip().title())
        _ = parse_iso_date(self.data)
        if self.ora or self.orario:
            self.ora = normalize_time_to_hhmm(self.ora or self.orario)
        return self


# ------------------------------------------------------------
# PLAYWRIGHT MANAGER (shared)
# ------------------------------------------------------------

class PlaywrightManager:
    def __init__(self) -> None:
        self.pw: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.sem = asyncio.Semaphore(PW_MAX_CONCURRENCY)

    async def start(self) -> None:
        logger.info("Starting Playwright...")
        self.pw = await async_playwright().start()
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

    async def new_page(self) -> Tuple[Page, Any]:
        if not self.browser:
            raise RuntimeError("Browser not initialized")
        ctx = await self.browser.new_context(
            user_agent=MOBILE_UA,
            viewport={"width": 390, "height": 844},
        )
        page = await ctx.new_page()
        page.set_default_timeout(PW_TIMEOUT_MS)
        page.set_default_navigation_timeout(PW_NAV_TIMEOUT_MS)

        # blocco risorse pesanti
        async def route_handler(route):
            rtype = route.request.resource_type
            if rtype in ("image", "media", "font", "stylesheet"):
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", route_handler)
        return page, ctx


pw_manager = PlaywrightManager()


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
# IDMPOTENCY (anti doppie)
# ------------------------------------------------------------

class IdempotencyStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._data: Dict[str, Tuple[float, Dict[str, Any]]] = {}  # fp -> (expires_at, response)

    async def get(self, fp: str) -> Optional[Dict[str, Any]]:
        now = time.time()
        async with self._lock:
            item = self._data.get(fp)
            if not item:
                return None
            expires_at, resp = item
            if expires_at < now:
                self._data.pop(fp, None)
                return None
            return resp

    async def set(self, fp: str, resp: Dict[str, Any], ttl: int) -> None:
        async with self._lock:
            self._data[fp] = (time.time() + ttl, resp)

    async def cleanup(self) -> None:
        now = time.time()
        async with self._lock:
            for k, (exp, _) in list(self._data.items()):
                if exp < now:
                    self._data.pop(k, None)

idempo = IdempotencyStore()


# ------------------------------------------------------------
# BOOKING FLOW (step-by-step, robust)
# ------------------------------------------------------------

SEDE_LABELS = {
    "Talenti": ["Talenti - Roma", "Talenti"],
    "Ostia": ["Ostia Lido", "Ostia"],
    "Appia": ["Appia"],
    "Reggio Calabria": ["Reggio Calabria", "Reggio"],
    "Palermo": ["Palermo"],
}

COOKIE_RE = re.compile(r"(accetta|consent|ok|accetto|accetta tutti)", re.IGNORECASE)

async def _safe_click(page: Page, locator, step: str, timeout_ms: int = 4000) -> bool:
    try:
        if await locator.count() > 0:
            await locator.first.click(timeout=timeout_ms, force=True)
            return True
    except Exception:
        return False
    return False

async def _click_text_any(page: Page, patterns, step: str, exact: bool = False, timeout_ms: int = 5000) -> bool:
    # patterns: list[str] or list[regex]
    for p in patterns:
        try:
            if isinstance(p, re.Pattern):
                loc = page.get_by_text(p)
            else:
                loc = page.get_by_text(str(p), exact=exact)
            if await loc.count() > 0:
                await loc.first.click(timeout=timeout_ms, force=True)
                return True
        except Exception:
            continue
    return False

async def _accept_cookies(page: Page):
    try:
        loc = page.get_by_text(COOKIE_RE)
        if await loc.count() > 0:
            await loc.first.click(timeout=2000, force=True)
    except Exception:
        pass

async def _select_people(page: Page, n: int):
    # tenta varie strategie: button/div/span con testo esatto
    txt = str(n)
    ok = await _click_text_any(page, [txt], step="persone", exact=True, timeout_ms=5000)
    if ok:
        return
    # fallback: selector più “grezzo”
    try:
        loc = page.locator(f"button:text-is('{txt}'), div:text-is('{txt}'), span:text-is('{txt}')")
        if await loc.count() > 0:
            await loc.first.click(timeout=5000, force=True)
            return
    except Exception:
        pass
    raise RuntimeError("Step 1 (Persone): bottone non trovato")

async def _select_seggiolini_no(page: Page):
    # se esiste step seggiolini, scegliere NO
    # prima prova testo “NO” in generale (se visibile)
    ok = await _click_text_any(page, [re.compile(r"^\s*NO\s*$", re.IGNORECASE)], step="seggiolini", exact=False, timeout_ms=4000)
    if ok:
        return
    # fallback: se non c’è, non è bloccante
    return

async def _set_date(page: Page, iso_date: str):
    # prova click "Altra data" se presente, poi imposta input[type=date]
    await _click_text_any(page, [re.compile(r"altra\s+data", re.IGNORECASE)], step="data", exact=False, timeout_ms=3000)

    # prova a scrivere su input date
    date_loc = page.locator("input[type='date']")
    try:
        if await date_loc.count() > 0:
            await date_loc.first.fill(iso_date)
            await date_loc.first.press("Enter")
        else:
            # fallback: set via JS se l’input è “strano”
            await page.evaluate(
                """(d) => {
                    const el = document.querySelector("input[type='date']");
                    if (el) { el.value = d; el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); }
                }""",
                iso_date,
            )
    except Exception:
        pass

    # conferma/cerca se presente
    await _click_text_any(page, [re.compile(r"(conferma|cerca)", re.IGNORECASE)], step="data-conferma", exact=False, timeout_ms=3000)

async def _select_meal(page: Page, meal: str):
    # meal = PRANZO/CENA
    ok = await _click_text_any(page, [re.compile(meal, re.IGNORECASE)], step="pasto", exact=False, timeout_ms=6000)
    # se non esiste, non bloccare: alcuni flussi filtrano automaticamente
    return

async def _select_sede(page: Page, sede_norm: str):
    labels = SEDE_LABELS.get(sede_norm, [sede_norm])
    # prova vari label
    for label in labels:
        ok = await _click_text_any(page, [label], step="sede", exact=False, timeout_ms=7000)
        if ok:
            return
    raise RuntimeError(f"Step 5 (Sede): '{sede_norm}' non trovata")

async def _select_orario(page: Page, hhmm: str):
    # prova click diretto sul testo orario
    candidates = [hhmm, hhmm.replace(":", "."), hhmm.lstrip("0")]
    ok = await _click_text_any(page, candidates, step="orario", exact=False, timeout_ms=7000)
    if ok:
        return

    # fallback: se c'è una select HTML, prova select_option
    try:
        sel = page.locator("select")
        if await sel.count() > 0:
            # prova match parziale
            await sel.first.select_option(label=hhmm)
            return
    except Exception:
        pass

    raise RuntimeError(f"Step 6 (Orario): '{hhmm}' non disponibile/non cliccabile")

async def _confirm_step(page: Page):
    # in alcuni flussi c’è “CONFERMA” prima dei dati finali
    await _click_text_any(page, [re.compile(r"conferma", re.IGNORECASE)], step="conferma", exact=False, timeout_ms=5000)

async def _fill_final_form(page: Page, payload: Dict[str, Any]):
    # prova a riempire input più comuni
    async def try_fill(selector: str, value: str):
        try:
            loc = page.locator(selector)
            if await loc.count() > 0:
                await loc.first.fill(value)
                return True
        except Exception:
            return False
        return False

    nome = payload["nome"]
    cognome = payload["cognome"]
    email = payload["email"]
    telefono = payload["telefono"]
    nota = payload.get("nota", "")

    await try_fill('input[name*="nome" i]', nome)
    await try_fill('input[name*="cognome" i]', cognome)
    await try_fill('input[type="email"]', email)
    await try_fill('input[name*="mail" i]', email)
    await try_fill('input[type="tel"]', telefono)
    await try_fill('input[name*="tel" i]', telefono)
    if nota:
        await try_fill('textarea', nota)

    # spunta checkbox privacy se presenti
    try:
        cbs = page.locator('input[type="checkbox"]')
        cnt = await cbs.count()
        if cnt > 0:
            for i in range(cnt):
                cb = cbs.nth(i)
                try:
                    if not await cb.is_checked():
                        await cb.check(force=True)
                except Exception:
                    pass
    except Exception:
        pass

async def _click_prenota(page: Page):
    # click finale PRENOTA
    ok = await _click_text_any(page, [re.compile(r"prenota", re.IGNORECASE)], step="prenota", exact=False, timeout_ms=7000)
    if not ok:
        raise RuntimeError("Step finale (PRENOTA): bottone non trovato")

async def _detect_success(page: Page) -> bool:
    # euristiche “soft”
    success_patterns = [
        re.compile(r"confermat", re.IGNORECASE),
        re.compile(r"prenotazion", re.IGNORECASE),
        re.compile(r"grazie", re.IGNORECASE),
    ]
    try:
        txt = (await page.content()).lower()
        if any(p.search(txt) for p in success_patterns):
            return True
    except Exception:
        pass
    return False

async def playwright_submit_booking(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Flow step-by-step. Se fallisce salva screenshot.
    """

    async def _do(attempt: int):
        async with pw_manager.sem:
            page, ctx = await pw_manager.new_page()
            screenshot_path = None
            try:
                logger.info("Booking attempt #%s - opening %s", attempt + 1, BOOKING_URL)
                await page.goto(BOOKING_URL, wait_until="domcontentloaded")

                await _accept_cookies(page)

                logger.info("-> 1. Persone")
                await _select_people(page, int(payload["persone"]))

                logger.info("-> 2. Seggiolini")
                await page.wait_for_timeout(400)
                await _select_seggiolini_no(page)

                logger.info("-> 3. Data")
                await page.wait_for_timeout(400)
                await _set_date(page, payload["data"])

                logger.info("-> 4. Pasto (%s)", meal_from_time(payload["ora"]))
                await page.wait_for_timeout(600)
                await _select_meal(page, meal_from_time(payload["ora"]))

                logger.info("-> 5. Sede (%s)", payload["sede"])
                await page.wait_for_timeout(800)
                await _select_sede(page, payload["sede"])

                logger.info("-> 6. Orario (%s)", payload["ora"])
                await page.wait_for_timeout(800)
                await _select_orario(page, payload["ora"])

                logger.info("-> 7. Conferma dati (se presente)")
                await page.wait_for_timeout(500)
                await _confirm_step(page)

                logger.info("-> 8. Dati finali")
                await page.wait_for_timeout(800)
                await _fill_final_form(page, payload)

                if payload.get("dry_run"):
                    return {
                        "ok": True,
                        "message": "Dry-run: flow completata fino al form finale (PRENOTA non cliccato).",
                        "details": {"dry_run": True, "attempt": attempt + 1},
                    }

                logger.info("-> 9. Click PRENOTA")
                await _click_prenota(page)

                await page.wait_for_timeout(1500)
                ok = await _detect_success(page)

                return {
                    "ok": True,
                    "message": "Prenotazione inviata",
                    "details": {
                        "best_effort": not ok,
                        "attempt": attempt + 1,
                        "success_detected": ok,
                    },
                }

            except Exception as e:
                try:
                    screenshot_path = safe_filename("booking_error")
                    await page.screenshot(path=screenshot_path, full_page=True)
                    logger.error("Saved screenshot: %s", screenshot_path)
                except Exception:
                    pass
                raise e
            finally:
                try:
                    await ctx.close()
                except Exception:
                    pass

    return await run_with_retries(_do, retries=PW_RETRIES)


# ------------------------------------------------------------
# APP + LIFESPAN
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

@app.get("/")
async def root():
    return {"ok": True, "service": APP_NAME, "health": "/healthz"}

@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": APP_NAME}

# Alias: alcuni tool chiamano /check_availability, altri /checkavailability
async def _check_availability_impl(req: AvailabilityRequest):
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

@app.post("/check_availability")
async def check_availability(req: AvailabilityRequest):
    return await _check_availability_impl(req)

@app.post("/checkavailability")
async def checkavailability(req: AvailabilityRequest):
    return await _check_availability_impl(req)

@app.post("/book_table")
async def book_table(req: BookingRequest, request: Request):
    # pulizia cache idempotenza
    await idempo.cleanup()

    # regole persone
    if req.persone > MAX_PEOPLE_AUTOMATION:
        return JSONResponse(
            status_code=200,
            content=transfer_response("Per gruppi superiori a 9 persone è necessario parlare con un operatore."),
        )

    # regole data
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

    # fingerprint anti-doppie
    fp = make_fingerprint(payload)
    payload["fingerprint"] = fp

    logger.info("BOOK_TABLE request normalized: %s", json.dumps(payload, ensure_ascii=False))

    # se chiamata ripetuta a distanza ravvicinata: ritorna risposta precedente
    cached = await idempo.get(fp)
    if cached:
        return cached

    # dry run
    if payload["dry_run"]:
        resp = {
            "ok": True,
            "message": "Dry-run: dati validi, prenotazione non inviata.",
            "payload": payload,
        }
        await idempo.set(fp, resp, ttl=IDEMPOTENCY_TTL_SECONDS)
        return resp

    # invio con playwright
    try:
        result = await playwright_submit_booking(payload)
        resp = {
            "ok": True,
            "message": "Prenotazione registrata correttamente.",
            "confirmation_notice": CONFIRMATION_NOTICE,
            "payload": payload,
            "result": result,
        }
        await idempo.set(fp, resp, ttl=IDEMPOTENCY_TTL_SECONDS)
        return resp

    except Exception as e:
        logger.exception("Booking failed: %s", str(e))
        resp = JSONResponse(
            status_code=200,
            content={
                "ok": False,
                "message": "C’è stato un problema temporaneo nel completare la prenotazione. "
                           "Riprova tra poco oppure chiedi il trasferimento a un operatore.",
                "action": "transfer_to_number" if TRANSFER_NUMBER else "retry",
                "number": TRANSFER_NUMBER,
                "fingerprint": fp,
            },
        )
        # anche qui cache breve: evita che il tool “martelli” e crei doppie
        await idempo.set(fp, resp.body and json.loads(resp.body.decode("utf-8")) or {"ok": False}, ttl=30)
        return resp
