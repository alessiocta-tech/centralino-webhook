# main.py
import os
import re
import json
import asyncio
import logging
import hashlib
from datetime import date, datetime, timedelta
from typing import Optional, Any, Dict, List

import phonenumbers
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from playwright.async_api import async_playwright, Browser, Playwright, Page, Response


# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------

APP_NAME = "centralino-webhook"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

BOOKING_URL = os.getenv("BOOKING_URL", "https://rione.fidy.app/prenew.php?referer=AI")
REFERER_DEFAULT = os.getenv("REFERER_DEFAULT", "AI")

MAX_DAYS_AHEAD = int(os.getenv("MAX_DAYS_AHEAD", "30"))
MAX_PEOPLE_AUTOMATION = int(os.getenv("MAX_PEOPLE_AUTOMATION", "9"))

TRANSFER_NUMBER = os.getenv("TRANSFER_NUMBER", "")

CONFIRMATION_NOTICE = (
    "Prenotazione registrata. Riceverai conferma via WhatsApp e via email ai contatti indicati. "
    "È importante rispettare l’orario prenotato: in caso di ritardo prolungato il tavolo potrebbe essere riassegnato."
)

PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "20000"))
PW_HEADLESS = os.getenv("PW_HEADLESS", "true").lower() != "false"
PW_RETRIES = int(os.getenv("PW_RETRIES", "2"))
PW_SLOWMO_MS = int(os.getenv("PW_SLOWMO_MS", "0"))

DEDUP_TTL_SECONDS = int(os.getenv("DEDUP_TTL_SECONDS", "120"))

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$", re.IGNORECASE)

SEDE_MAP = {
    "talenti": "Talenti",
    "appia": "Appia",
    "ostia": "Ostia",
    "reggio": "Reggio Calabria",
    "reggio calabria": "Reggio Calabria",
    "palermo": "Palermo",
    "palermo centro": "Palermo",
}


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

def is_past_date(d: date) -> bool:
    return d < today_local()

def within_days_limit(d: date, max_days: int) -> bool:
    return d <= (today_local() + timedelta(days=max_days))

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

def calc_pasto_from_time(hhmm: str) -> str:
    hh = int(hhmm.split(":")[0])
    return "PRANZO" if hh < 17 else "CENA"

def safe_filename(prefix: str) -> str:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    return f"{prefix}_{ts}.png"

def _time_to_minutes(hhmm: str) -> int:
    hh, mm = hhmm.split(":")
    return int(hh) * 60 + int(mm)

def _pick_alternatives(times: List[str], requested: str, limit: int = 3) -> List[str]:
    req = _time_to_minutes(requested)
    uniq = sorted({t for t in times}, key=_time_to_minutes)
    after = [t for t in uniq if _time_to_minutes(t) > req]
    if len(after) >= limit:
        return after[:limit]
    by_distance = sorted(uniq, key=lambda t: abs(_time_to_minutes(t) - req))
    out: List[str] = []
    for t in after + by_distance:
        if t not in out:
            out.append(t)
        if len(out) >= limit:
            break
    return out[:limit]

def make_fingerprint(payload: Dict[str, Any]) -> str:
    stable = {
        "nome": payload.get("nome", "").strip().lower(),
        "cognome": payload.get("cognome", "").strip().lower(),
        "telefono": payload.get("telefono", "").strip(),
        "email": payload.get("email", "").strip().lower(),
        "persone": int(payload.get("persone", 0)),
        "sede": payload.get("sede", "").strip().lower(),
        "data": payload.get("data", "").strip(),
        "ora": payload.get("ora", "").strip(),
    }
    raw = json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

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

    referer: str = REFERER_DEFAULT
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
        return SEDE_MAP.get(s, (v or "").strip().title())

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
            slow_mo=PW_SLOWMO_MS if PW_SLOWMO_MS > 0 else None,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-first-run",
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

    async def new_page(self) -> Page:
        if not self.browser:
            raise RuntimeError("Browser not initialized")
        ctx = await self.browser.new_context(
            viewport={"width": 390, "height": 844},
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
            ),
        )
        page = await ctx.new_page()
        page.set_default_timeout(PW_TIMEOUT_MS)

        await page.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ["image", "media", "font", "stylesheet"]
            else route.continue_(),
        )
        return page


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


async def _click_if_exists(page: Page, selector: str, timeout_ms: int = 1500) -> bool:
    try:
        loc = page.locator(selector)
        if await loc.count() > 0:
            await loc.first.click(timeout=timeout_ms, force=True)
            return True
    except Exception:
        return False
    return False


async def _set_date_input(page: Page, iso_date: str) -> None:
    await page.evaluate(
        """(d) => {
            const i = document.querySelector('input#DataPren');
            if (!i) return;
            i.value = d;
            i.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        iso_date,
    )


async def _wait_ristoload_response(page: Page) -> Optional[Response]:
    """
    Aspetta la chiamata XHR che carica la lista sedi/turni.
    """
    try:
        resp = await page.wait_for_response(
            lambda r: ("prenew_rist.php" in r.url) and (r.status == 200),
            timeout=PW_TIMEOUT_MS,
        )
        return resp
    except Exception:
        return None


async def _click_sede_card_and_turno(page: Page, sede: str) -> None:
    """
    Euristica robusta:
    - trova un "contenitore" in .ristoCont che contiene il testo sede
    - dentro quel contenitore clicca un elemento “azione” (button/a/span cliccabile)
    """
    await page.wait_for_selector(".ristoCont", timeout=PW_TIMEOUT_MS)

    # attende che la spinner sparisca o che il contenuto cambi
    await page.wait_for_timeout(700)

    # trova elemento con testo sede dentro ristoCont
    sede_loc = page.locator(".ristoCont").get_by_text(sede, exact=False).first
    if await sede_loc.count() == 0:
        # fallback: prova varianti
        variants = [sede, sede.lower(), sede.upper()]
        if sede.lower() == "reggio calabria":
            variants += ["Reggio", "Reggio Calabria"]
        if sede.lower() == "palermo":
            variants += ["Palermo", "Palermo Centro"]
        found = None
        for v in variants:
            loc = page.locator(".ristoCont").get_by_text(v, exact=False).first
            if await loc.count() > 0:
                found = loc
                break
        if not found:
            raise RuntimeError("Sede non trovata nella lista turni.")
        sede_loc = found

    # risali al contenitore “card”
    # (bootstrap spesso usa .border/.rounded/.p-*/.card ecc.)
    container = sede_loc.locator("xpath=ancestor-or-self::*[contains(@class,'card') or contains(@class,'border') or contains(@class,'rounded')][1]")
    if await container.count() == 0:
        # fallback: usa un ancestor qualsiasi
        container = sede_loc.locator("xpath=ancestor-or-self::*[1]")

    # prova click diretto su sede (a volte è già un bottone)
    try:
        await sede_loc.click(force=True, timeout=1200)
    except Exception:
        pass

    await page.wait_for_timeout(300)

    # cerca “azioni” nella stessa card: link o bottoni
    action_candidates = [
        "a:has-text('TURNO')",
        "button:has-text('TURNO')",
        "a:has-text('PRIMO')",
        "button:has-text('PRIMO')",
        "a:has-text('SECONDO')",
        "button:has-text('SECONDO')",
        "a",
        "button",
        "span[role='button']",
    ]

    clicked = False
    for sel in action_candidates:
        try:
            loc = container.locator(sel)
            if await loc.count() > 0:
                await loc.first.click(force=True, timeout=2000)
                clicked = True
                break
        except Exception:
            continue

    # se non troviamo azioni, proviamo click sul contenitore
    if not clicked:
        try:
            await container.first.click(force=True, timeout=2000)
            clicked = True
        except Exception:
            pass

    if not clicked:
        raise RuntimeError("Impossibile selezionare sede/turno (nessuna azione cliccabile nella card).")

    # attesa che si passi allo step con orari
    await page.wait_for_timeout(600)


async def _extract_available_times(page: Page) -> List[str]:
    await page.wait_for_selector("#OraPren", timeout=PW_TIMEOUT_MS)
    options = page.locator("#OraPren option")
    count = await options.count()
    times: List[str] = []
    for i in range(count):
        val = (await options.nth(i).get_attribute("value")) or ""
        txt = (await options.nth(i).inner_text()) or ""
        val = val.strip()
        txt = txt.strip()
        if not val or "scegli" in txt.lower():
            continue
        hhmm = val[:5] if len(val) >= 5 else val
        if re.fullmatch(r"\d{2}:\d{2}", hhmm):
            times.append(hhmm)
    return times


async def playwright_submit_booking(payload: Dict[str, Any]) -> Dict[str, Any]:
    async def _do(attempt: int):
        page = await pw_manager.new_page()
        try:
            logger.info("Booking attempt #%s - opening %s", attempt + 1, BOOKING_URL)
            await page.goto(BOOKING_URL, wait_until="domcontentloaded")
            await page.wait_for_selector(".stepCont", timeout=PW_TIMEOUT_MS)

            # 1) COPERTI
            coperti = str(payload["persone"])
            if not await _click_if_exists(page, f'.nCoperti[rel="{coperti}"]'):
                await page.get_by_text(coperti, exact=True).first.click(force=True)

            # 2) SEGGIOLINI
            seggiolini = int(payload.get("seggiolini") or 0)
            if seggiolini > 0:
                await _click_if_exists(page, ".seggioliniTxt")
                await page.wait_for_timeout(300)
                await _click_if_exists(page, f'.nSeggiolini[rel="{seggiolini}"]')

            # 3) DATA
            iso_d = payload["data"]
            if not await _click_if_exists(page, f'.dataBtn[rel="{iso_d}"]'):
                await _click_if_exists(page, ".altraData")
                await page.wait_for_timeout(200)
                await _set_date_input(page, iso_d)

            # 4) PRANZO/CENA
            pasto = calc_pasto_from_time(payload["ora"])
            await page.wait_for_timeout(200)
            if not await _click_if_exists(page, f'.tipoBtn[rel="{pasto}"]'):
                await page.get_by_text(pasto, exact=False).first.click(force=True)

            # 5) ATTESA LOAD prenew_rist.php
            # appena selezioni pasto, parte il .load(...) nella ristoCont
            await page.wait_for_selector(".ristoCont", timeout=PW_TIMEOUT_MS)
            await page.wait_for_timeout(250)
            await _wait_ristoload_response(page)  # se fallisce, non blocca: usiamo DOM

            # 6) SEDE + TURNO (card-based)
            sede = payload["sede"]
            await _click_sede_card_and_turno(page, sede)

            # 7) ORARI
            available = await _extract_available_times(page)
            requested = payload["ora"]

            if requested not in available:
                alts = _pick_alternatives(available, requested, limit=3)
                return {
                    "ok": False,
                    "code": "SOLD_OUT",
                    "message": "Il turno selezionato è pieno. Ti proponiamo in alternativa il seguente turno:",
                    "alternatives": alts,
                    "details": {"requested": requested},
                }

            try:
                await page.select_option("#OraPren", value=re.compile(f"^{re.escape(requested)}"))
            except Exception:
                await page.select_option("#OraPren", label=re.compile(re.escape(requested)))

            # 8) NOTE + CONFERMA
            nota = (payload.get("nota") or "").strip()
            if nota:
                try:
                    await page.fill("#Nota", nota)
                except Exception:
                    pass

            await page.get_by_text("CONFERMA", exact=False).first.click(force=True)

            # 9) DATI CLIENTE
            await page.wait_for_selector("form#prenoForm", timeout=PW_TIMEOUT_MS)
            await page.fill("#Nome", payload["nome"])
            await page.fill("#Cognome", payload["cognome"])
            await page.fill("#Email", payload["email"])

            tel = payload["telefono"]
            tel_digits = re.sub(r"\D", "", tel)
            if tel_digits.startswith("39") and len(tel_digits) > 10:
                tel_digits_local = tel_digits[2:]
            else:
                tel_digits_local = tel_digits
            await page.fill("#Telefono", tel_digits_local[:10])

            # 10) SUBMIT
            await page.locator('input[type="submit"][value="PRENOTA"]').click(force=True)

            await page.wait_for_timeout(1200)
            content = (await page.content()).lower()
            if ("confermat" in content) or ("prenotaz" in content and "ok" in content):
                return {"ok": True, "message": "Prenotazione confermata", "details": {"confirmed": True}}

            return {"ok": True, "message": "Prenotazione inviata", "details": {"best_effort": True}}

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
# DEDUP
# ------------------------------------------------------------

_dedup_lock = asyncio.Lock()
_dedup_cache: Dict[str, float] = {}

async def _dedup_check_and_set(fingerprint: str) -> bool:
    now = datetime.utcnow().timestamp()
    async with _dedup_lock:
        dead = [k for k, exp in _dedup_cache.items() if exp <= now]
        for k in dead:
            _dedup_cache.pop(k, None)
        if fingerprint in _dedup_cache:
            return True
        _dedup_cache[fingerprint] = now + DEDUP_TTL_SECONDS
        return False


# ------------------------------------------------------------
# APP
# ------------------------------------------------------------

async def lifespan(app: FastAPI):
    await pw_manager.start()
    yield
    await pw_manager.stop()

app = FastAPI(title=APP_NAME, lifespan=lifespan)


@app.get("/")
async def root():
    return {"ok": True, "service": APP_NAME, "health": "/healthz"}

@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": APP_NAME}

# compat: niente check, si prenota direttamente
@app.post("/check_availability")
async def check_availability_compat(_: Dict[str, Any] = None):
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "available": "unknown",
            "message": "La disponibilità viene verificata durante la prenotazione. Procedi con /book_table.",
        },
    )

@app.post("/book_table")
async def book_table(req: BookingRequest, request: Request):
    if req.persone > MAX_PEOPLE_AUTOMATION:
        return JSONResponse(
            status_code=200,
            content=transfer_response("Per gruppi superiori a 9 persone è necessario parlare con un operatore."),
        )

    d = parse_iso_date(req.data)
    if is_past_date(d):
        return JSONResponse(status_code=200, content=user_error("Non è possibile prenotare per date già trascorse."))
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
        "referer": req.referer or REFERER_DEFAULT,
        "dry_run": bool(req.dry_run),
    }

    fingerprint = make_fingerprint(payload)
    payload["fingerprint"] = fingerprint

    logger.info("BOOK_TABLE request normalized: %s", json.dumps(payload, ensure_ascii=False))

    if await _dedup_check_and_set(fingerprint):
        return JSONResponse(
            status_code=200,
            content={
                "ok": False,
                "code": "DUPLICATE_REQUEST",
                "message": "Richiesta duplicata rilevata. Se vuoi modificare i dettagli, indica un nuovo orario o una nuova data.",
            },
        )

    if payload["dry_run"]:
        return JSONResponse(
            status_code=200,
            content={"ok": True, "message": "Dry-run: dati validi, prenotazione non inviata.", "payload": payload},
        )

    try:
        result = await playwright_submit_booking(payload)

        if result.get("ok") is False and result.get("code") == "SOLD_OUT":
            return JSONResponse(status_code=200, content=result)

        if result.get("ok"):
            return JSONResponse(
                status_code=200,
                content={
                    "ok": True,
                    "message": "Prenotazione registrata correttamente.",
                    "confirmation_notice": CONFIRMATION_NOTICE,
                    "payload": payload,
                    "result": result,
                },
            )

        return JSONResponse(
            status_code=200,
            content={"ok": False, "message": "Non è stato possibile completare la prenotazione in questo momento.", "result": result},
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
