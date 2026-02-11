# main.py
import os
import re
import json
import time
import uuid
import hashlib
import logging
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field, validator
from playwright.async_api import async_playwright

# ----------------------------
# CONFIG
# ----------------------------
APP_NAME = "centralino-webhook"
BASE_URL = os.getenv("FIDY_URL", "https://rione.fidy.app/prenew.php")
REFERER_DEFAULT = os.getenv("REFERER_DEFAULT", "AI")

# Playwright stability
PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "60000"))
PW_NAV_TIMEOUT_MS = int(os.getenv("PW_NAV_TIMEOUT_MS", "60000"))
PW_RETRIES = int(os.getenv("PW_RETRIES", "2"))  # 0/1/2 recommended
BLOCK_HEAVY_RESOURCES = os.getenv("PW_BLOCK_HEAVY", "1") == "1"

# Idempotency (prevents double booking from repeated tool calls)
IDEMPOTENCY_TTL_SEC = int(os.getenv("IDEMPOTENCY_TTL_SEC", "180"))

# Debug screenshots
SCREENSHOTS_DIR = os.getenv("SCREENSHOTS_DIR", "/tmp")
SAVE_SCREENSHOTS = os.getenv("SAVE_SCREENSHOTS", "1") == "1"

# ----------------------------
# LOGGING
# ----------------------------
logger = logging.getLogger(APP_NAME)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

# ----------------------------
# APP
# ----------------------------
app = FastAPI(title="Centralino Webhook", version="DEF-BOOKING-ONLY")

# In-memory idempotency cache (best-effort; per instance)
_seen: Dict[str, float] = {}


# ----------------------------
# MODELS
# ----------------------------
class BookTablePayload(BaseModel):
    nome: str = Field(..., min_length=1)
    cognome: str = Field(..., min_length=1)
    email: EmailStr
    telefono: str = Field(..., min_length=6)
    persone: int = Field(..., ge=1, le=9)
    sede: str = Field(..., min_length=1)
    data: str = Field(..., description="YYYY-MM-DD")
    ora: str = Field(..., description="HH:MM or HH:MM:SS")
    seggiolone: bool = False
    seggiolini: int = Field(0, ge=0, le=5)
    nota: str = ""
    referer: str = REFERER_DEFAULT
    dry_run: bool = False
    fingerprint: Optional[str] = None

    @validator("telefono")
    def validate_phone(cls, v: str) -> str:
        # Keep + and digits; accept international
        vv = v.strip().replace(" ", "")
        if not re.fullmatch(r"\+?\d{6,15}", vv):
            raise ValueError("Telefono non valido")
        return vv

    @validator("data")
    def validate_date(cls, v: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v.strip()):
            raise ValueError("Data non valida (formato YYYY-MM-DD)")
        return v.strip()

    @validator("ora")
    def normalize_time(cls, v: str) -> str:
        s = v.strip().replace(".", ":")
        # allow "13" -> "13:00"
        if re.fullmatch(r"\d{1,2}", s):
            h = int(s)
            return f"{h:02d}:00"
        if re.fullmatch(r"\d{1,2}:\d{2}", s):
            h, m = s.split(":")
            return f"{int(h):02d}:{int(m):02d}"
        if re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", s):
            h, m, _sec = s.split(":")
            return f"{int(h):02d}:{int(m):02d}"
        raise ValueError("Ora non valida (HH:MM)")

    @validator("sede")
    def normalize_sede(cls, v: str) -> str:
        return v.strip()


# ----------------------------
# HELPERS
# ----------------------------
def now_ts() -> float:
    return time.time()


def make_fingerprint(payload: BookTablePayload) -> str:
    # Stable fingerprint to avoid double bookings when tool retries
    base = {
        "nome": payload.nome.strip().lower(),
        "cognome": payload.cognome.strip().lower(),
        "telefono": payload.telefono.strip(),
        "email": payload.email.strip().lower(),
        "persone": payload.persone,
        "sede": payload.sede.strip().lower(),
        "data": payload.data,
        "ora": payload.ora,
        "seggiolini": payload.seggiolini,
        "nota": (payload.nota or "").strip().lower(),
    }
    raw = json.dumps(base, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def idempotency_check(fingerprint: str) -> bool:
    # returns True if already seen within TTL
    t = now_ts()
    # cleanup
    expired = [k for k, v in _seen.items() if t - v > IDEMPOTENCY_TTL_SEC]
    for k in expired:
        _seen.pop(k, None)
    if fingerprint in _seen and (t - _seen[fingerprint] <= IDEMPOTENCY_TTL_SEC):
        return True
    _seen[fingerprint] = t
    return False


def meal_from_time(hhmm: str) -> str:
    # PRANZO before 17:00 else CENA
    h = int(hhmm.split(":")[0])
    return "PRANZO" if h < 17 else "CENA"


def screenshot_name(prefix: str) -> str:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}_{uuid.uuid4().hex[:6]}.png"


async def safe_screenshot(page, prefix: str) -> Optional[str]:
    if not SAVE_SCREENSHOTS:
        return None
    try:
        path = os.path.join(SCREENSHOTS_DIR, screenshot_name(prefix))
        await page.screenshot(path=path, full_page=True)
        logger.error("Saved screenshot: %s", os.path.basename(path))
        return path
    except Exception:
        return None


async def block_heavy(page):
    if not BLOCK_HEAVY_RESOURCES:
        return

    async def _route(route):
        rtype = route.request.resource_type
        if rtype in ["image", "media", "font", "stylesheet"]:
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", _route)


async def click_best_effort(locator, timeout=2500) -> bool:
    try:
        await locator.first.click(timeout=timeout, force=True)
        return True
    except Exception:
        return False


async def get_select_options(page) -> List[Dict[str, Any]]:
    """
    Reads #OraPren options. Returns list of dict:
    { value, text, disabled(bool) }
    """
    js = """
    () => {
      const sel = document.querySelector('#OraPren');
      if (!sel) return [];
      return Array.from(sel.querySelectorAll('option')).map(o => ({
        value: o.value || "",
        text: (o.textContent || "").trim(),
        disabled: !!o.disabled
      }));
    }
    """
    try:
        return await page.evaluate(js)
    except Exception:
        return []


def normalize_option_time(value: str) -> Optional[str]:
    # Option values are like "13:00:00" -> "13:00"
    if not value:
        return None
    m = re.match(r"^(\d{2}):(\d{2})", value)
    if not m:
        return None
    return f"{m.group(1)}:{m.group(2)}"


def build_alternatives(options: List[Dict[str, Any]]) -> List[str]:
    alts = []
    for o in options:
        t = normalize_option_time(o.get("value", ""))
        if not t:
            continue
        if o.get("disabled"):
            continue
        # skip placeholder
        if t not in alts:
            alts.append(t)
    return alts


def is_option_full_or_disabled(opt: Dict[str, Any]) -> bool:
    if opt.get("disabled"):
        return True
    txt = (opt.get("text") or "").upper()
    if "POSTI ESAURITI" in txt or "ESAURITO" in txt:
        return True
    return False


# ----------------------------
# ROUTES
# ----------------------------
@app.get("/")
def home():
    return {"status": "Centralino AI - Booking Only (DEF)", "endpoints": ["/book_table"]}


# IMPORTANT: keep these endpoints as "no-op" to avoid 422 from external callers (ElevenLabs).
# They must accept ANY body without Pydantic validation and always return 200.
@app.post("/check_availability")
async def check_availability_disabled(request: Request):
    try:
        await request.body()
    except Exception:
        pass
    return JSONResponse(
        status_code=200,
        content={
            "status": "DISABLED",
            "message": "Disponibilità ignorata. Procedere direttamente con prenotazione.",
        },
    )


@app.post("/checkavailability")
async def checkavailability_disabled_alias(request: Request):
    try:
        await request.body()
    except Exception:
        pass
    return JSONResponse(
        status_code=200,
        content={
            "status": "DISABLED",
            "message": "Alias disabilitato. Procedere direttamente con prenotazione.",
        },
    )


@app.post("/book_table")
async def book_table(payload: BookTablePayload):
    # Normalize + idempotency
    fp = payload.fingerprint or make_fingerprint(payload)
    if idempotency_check(fp):
        # best-effort protection vs double calls
        logger.warning("Idempotency hit for fingerprint=%s", fp[:10])
        return {
            "status": "DUPLICATE_PREVENTED",
            "result": "Richiesta duplicata rilevata. Prenotazione già in lavorazione.",
            "fingerprint": fp,
        }

    # Map sede names (fuzzy)
    sede_map = {
        "talenti": "Talenti - Roma",
        "talenti - roma": "Talenti - Roma",
        "roma talenti": "Talenti - Roma",
        "ostia": "Ostia Lido",
        "ostia lido": "Ostia Lido",
        "appia": "Appia",
        "reggio": "Reggio Calabria",
        "reggio calabria": "Reggio Calabria",
        "palermo": "Palermo",
    }
    sede_norm = payload.sede.strip().lower()
    sede_target = sede_map.get(sede_norm, payload.sede.strip())
    tipo_pasto = meal_from_time(payload.ora)

    logger.info(
        "BOOK_TABLE normalized: %s",
        json.dumps(
            {
                "nome": payload.nome,
                "cognome": payload.cognome,
                "email": payload.email,
                "telefono": payload.telefono,
                "persone": payload.persone,
                "sede": sede_target,
                "data": payload.data,
                "ora": payload.ora,
                "tipo": tipo_pasto,
                "seggiolini": payload.seggiolini,
                "dry_run": payload.dry_run,
                "fingerprint": fp,
            },
            ensure_ascii=False,
        ),
    )

    async def _attempt(attempt_no: int):
        url = f"{BASE_URL}?referer={payload.referer or REFERER_DEFAULT}"
        logger.info("Booking attempt #%s - opening %s", attempt_no, url)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-accelerated-2d-canvas",
                    "--no-first-run",
                    "--no-zygote",
                    "--single-process",
                    "--disable-gpu",
                ],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                    "Mobile/15E148 Safari/604.1"
                ),
                viewport={"width": 390, "height": 844},
                locale="it-IT",
            )
            page = await context.new_page()
            await page.set_default_timeout(PW_TIMEOUT_MS)
            await page.set_default_navigation_timeout(PW_NAV_TIMEOUT_MS)

            if BLOCK_HEAVY_RESOURCES:
                await block_heavy(page)

            try:
                # Navigate
                await page.goto(url, wait_until="domcontentloaded")

                # Cookie button (best effort)
                await click_best_effort(page.locator("text=/accetta|consent|ok/i"), timeout=1500)

                # STEP 1 - persone: click exact text 1..9
                # Buttons are <span class="nCoperti" rel="2">2</span>
                # We can click by text-is on a span.
                ok = await click_best_effort(
                    page.locator(f"span.nCoperti:text-is('{payload.persone}')"),
                    timeout=3000,
                )
                if not ok:
                    # fallback: generic exact text
                    await click_best_effort(page.get_by_text(str(payload.persone), exact=True), timeout=2000)

                # seggiolini
                await page.wait_for_timeout(300)
                if payload.seggiolini and payload.seggiolini > 0:
                    # click "SI" -> then pick number
                    await click_best_effort(page.locator(".seggioliniTxt"), timeout=2000)
                    await page.wait_for_timeout(200)
                    await click_best_effort(page.locator(f"span.nSeggiolini[rel='{payload.seggiolini}']"), timeout=2000)
                else:
                    # default NO; often already selected
                    await click_best_effort(page.locator(".SeggNO"), timeout=1200)

                # STEP 2 - date
                await page.wait_for_timeout(300)
                # safest: set hidden #DataPren input and trigger change
                # There is an input #DataPren (type=date) and hidden #DataPren2 used by JS.
                # We set #DataPren value then dispatch change.
                await page.evaluate(
                    """(d) => {
                      const inp = document.querySelector('#DataPren');
                      if (inp) {
                        inp.value = d;
                        inp.dispatchEvent(new Event('change', {bubbles:true}));
                      } else {
                        // fallback: any date input
                        const any = document.querySelector('input[type=date]');
                        if (any) { any.value = d; any.dispatchEvent(new Event('change', {bubbles:true})); }
                      }
                    }""",
                    payload.data,
                )

                # STEP 3 - PRANZO/CENA
                await page.wait_for_timeout(300)
                await click_best_effort(page.locator(f"span.tipoBtn[rel='{tipo_pasto}']"), timeout=4000)

                # STEP 4 - choose restaurant card (ristoBtn contains <small>NAME</small>)
                await page.wait_for_timeout(800)
                # The list is loaded into .ristoCont; wait until at least one .ristoBtn appears
                await page.wait_for_selector(".ristoBtn", timeout=PW_TIMEOUT_MS)

                # click the card whose text contains sede_target
                # Use locator filtering by text
                sede_locator = page.locator(".ristoBtn").filter(has_text=re.compile(re.escape(sede_target), re.I))
                clicked = await click_best_effort(sede_locator, timeout=5000)
                if not clicked:
                    # fallback: try with raw provided "sede"
                    sede_locator2 = page.locator(".ristoBtn").filter(has_text=re.compile(re.escape(payload.sede), re.I))
                    clicked2 = await click_best_effort(sede_locator2, timeout=3000)
                    if not clicked2:
                        await safe_screenshot(page, "booking_error_sede")
                        raise RuntimeError(f"Sede non trovata: {sede_target}")

                # STEP 5 - wait for #OraPren to be populated then pick time
                await page.wait_for_timeout(800)

                desired = payload.ora  # HH:MM
                # options values are HH:MM:SS; match HH:MM prefix
                opts = await get_select_options(page)
                if not opts or len(opts) <= 1:
                    # allow a bit more time
                    await page.wait_for_timeout(900)
                    opts = await get_select_options(page)

                # Find exact requested time option
                chosen = None
                for o in opts:
                    t = normalize_option_time(o.get("value", ""))
                    if t == desired:
                        chosen = o
                        break

                alternatives = build_alternatives(opts)

                if chosen is None:
                    # requested time not present => propose available times
                    return {
                        "status": "TIME_NOT_AVAILABLE",
                        "result": "Il turno selezionato è pieno. Ti proponiamo in alternativa il seguente turno",
                        "requested": desired,
                        "alternatives": alternatives,
                        "sede": sede_target,
                        "data": payload.data,
                        "tipo": tipo_pasto,
                        "fingerprint": fp,
                    }

                # If present but disabled/full
                if is_option_full_or_disabled(chosen):
                    return {
                        "status": "TURN_FULL",
                        "result": "Il turno selezionato è pieno. Ti proponiamo in alternativa il seguente turno",
                        "requested": desired,
                        "alternatives": alternatives,
                        "sede": sede_target,
                        "data": payload.data,
                        "tipo": tipo_pasto,
                        "fingerprint": fp,
                    }

                # Select it
                await page.select_option("#OraPren", value=chosen.get("value", ""))
                await page.wait_for_timeout(300)

                # Notes
                if payload.nota:
                    await click_best_effort(page.locator("#Nota"), timeout=800)
                    try:
                        await page.fill("#Nota", payload.nota)
                    except Exception:
                        pass

                # Click CONFERMA (go to form)
                await click_best_effort(page.locator("a.confDati"), timeout=5000)
                await page.wait_for_timeout(800)

                # Fill customer data
                try:
                    await page.fill("#Nome", payload.nome.strip())
                    await page.fill("#Cognome", payload.cognome.strip())
                    await page.fill("#Email", str(payload.email).strip())
                    # Site expects digits (maxlength 10) but we accept +country:
                    tel_digits = re.sub(r"\D", "", payload.telefono)
                    await page.fill("#Telefono", tel_digits[-10:] if len(tel_digits) >= 10 else tel_digits)
                except Exception:
                    await safe_screenshot(page, "booking_error_form")
                    raise RuntimeError("Impossibile compilare il form dati cliente.")

                # Final submit PRENOTA
                if payload.dry_run:
                    return {
                        "status": "DRY_RUN_OK",
                        "result": "Form compilato (dry_run=true). Click PRENOTA non eseguito.",
                        "sede": sede_target,
                        "data": payload.data,
                        "ora": desired,
                        "tipo": tipo_pasto,
                        "fingerprint": fp,
                        "alternatives": alternatives,
                    }

                # Click PRENOTA (input[type=submit] inside .sbmButt)
                # Use robust selector: form#prenoForm input[type=submit]
                ok_submit = await click_best_effort(page.locator("form#prenoForm input[type='submit']"), timeout=6000)
                if not ok_submit:
                    await safe_screenshot(page, "booking_error_submit")
                    raise RuntimeError("Non riesco a premere PRENOTA.")

                # Wait a bit for response / confirmation
                await page.wait_for_timeout(1500)
                content = (await page.content()).lower()

                # If ajax returns OK it loads prenew_res.php into .stepCont; we can detect "OK" patterns.
                if "prenotazione" in content or "ok" in content:
                    return {
                        "status": "CONFIRMED",
                        "result": f"Prenotazione confermata per {payload.nome} {payload.cognome}.",
                        "sede": sede_target,
                        "data": payload.data,
                        "ora": desired,
                        "tipo": tipo_pasto,
                        "fingerprint": fp,
                    }

                # Even if we can't detect, consider submitted.
                return {
                    "status": "SUBMITTED",
                    "result": f"Richiesta inviata per {payload.nome} {payload.cognome}.",
                    "sede": sede_target,
                    "data": payload.data,
                    "ora": desired,
                    "tipo": tipo_pasto,
                    "fingerprint": fp,
                }

            except Exception as e:
                # Many Playwright "TargetClosedError" variants depend on version; catch by message.
                msg = str(e)
                if "TargetClosed" in msg or "has been closed" in msg:
                    await safe_screenshot(page, "booking_error_targetclosed")
                    raise RuntimeError("Browser chiuso durante la prenotazione (TargetClosed).") from e

                await safe_screenshot(page, "booking_error")
                raise
            finally:
                try:
                    await context.close()
                except Exception:
                    pass
                try:
                    await browser.close()
                except Exception:
                    pass

    last_err: Optional[Exception] = None
    for attempt in range(1, PW_RETRIES + 2):  # 1..retries+1
        try:
            result = await _attempt(attempt)
            # If we returned alternatives, it's not an exception: stop retrying
            return result
        except Exception as e:
            last_err = e
            logger.error("Attempt %s failed: %s", attempt, str(e))
            # small backoff
            await _sleep_ms(350)

    # After retries
    logger.exception("Booking failed: %s", str(last_err))
    return {
        "status": "FAILED",
        "result": "Impossibile completare la prenotazione. Riprovare o trasferire al centralino.",
        "fingerprint": fp,
    }


async def _sleep_ms(ms: int):
    # local helper (avoids importing asyncio in top-level if you want)
    import asyncio

    await asyncio.sleep(ms / 1000.0)
