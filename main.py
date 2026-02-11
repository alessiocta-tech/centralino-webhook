import os
import re
import json
import time
import hashlib
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple

from fastapi import FastAPI
from pydantic import BaseModel, EmailStr, Field, validator
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError
from playwright.async_api import Error as PWError

# -------------------------
# LOGGING
# -------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | centralino-webhook | %(message)s",
)
logger = logging.getLogger("centralino-webhook")

# -------------------------
# CONFIG
# -------------------------
BOOKING_URL = os.getenv("BOOKING_URL", "https://rione.fidy.app/prenew.php?referer=AI")

PW_HEADLESS = os.getenv("PW_HEADLESS", "true").lower() != "false"
PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "45000"))
PW_RETRIES = int(os.getenv("PW_RETRIES", "2"))  # totale tentativi = PW_RETRIES + 1

IDEMPOTENCY_TTL_SEC = int(os.getenv("IDEMPOTENCY_TTL_SEC", "600"))  # 10 min
GLOBAL_CONCURRENCY = int(os.getenv("GLOBAL_CONCURRENCY", "1"))      # 1 = evita doppie concorrenti
SCREENSHOT_DIR = os.getenv("SCREENSHOT_DIR", "/tmp")

# -------------------------
# FASTAPI
# -------------------------
app = FastAPI(title="Centralino AI - deRione Booking Webhook", version="definitive-v2")
from fastapi import Request


@app.post("/check_availability")
async def check_availability_disabled(request: Request):
    try:
        _ = await request.body()
    except Exception:
        pass

    return {
        "status": "DISABLED",
        "message": "Check availability disabilitato: procedere direttamente con /book_table."
    }


@app.post("/checkavailability")
async def checkavailability_disabled_alias(request: Request):
    try:
        _ = await request.body()
    except Exception:
        pass

    return {
        "status": "DISABLED",
        "message": "Check availability disabilitato: procedere direttamente con /book_table."
    }

# -------------------------
# PLAYWRIGHT GLOBALS
# -------------------------
_pw = None
_browser = None
_browser_lock = asyncio.Lock()
_global_sem = asyncio.Semaphore(GLOBAL_CONCURRENCY)

# idempotency in-memory (best effort)
_idem_cache: Dict[str, Dict[str, Any]] = {}
_idem_lock = asyncio.Lock()

# -------------------------
# MODELS
# -------------------------
class BookingRequest(BaseModel):
    nome: str
    cognome: str
    email: EmailStr
    telefono: str
    persone: int = Field(..., ge=1, le=9)
    sede: str
    data: str  # YYYY-MM-DD
    ora: str   # HH:MM
    seggiolone: bool = False
    seggiolini: int = 0
    nota: str = ""
    referer: str = "AI"
    dry_run: bool = False

    @validator("telefono")
    def normalize_phone(cls, v: str) -> str:
        v = v.strip()
        v = re.sub(r"[^\d+]", "", v)
        if len(re.sub(r"\D", "", v)) < 8:
            raise ValueError("Telefono non valido")
        return v

    @validator("data")
    def validate_date(cls, v: str) -> str:
        try:
            d = datetime.strptime(v, "%Y-%m-%d").date()
        except Exception:
            raise ValueError("Data non valida (formato richiesto YYYY-MM-DD)")
        oggi = datetime.now().date()
        if d < oggi:
            raise ValueError("Non è possibile prenotare per date già trascorse.")
        if d > oggi + timedelta(days=30):
            raise ValueError("Data oltre 30 giorni (richiede transfer).")
        return v

    @validator("ora")
    def validate_time(cls, v: str) -> str:
        v = v.strip().replace(".", ":")
        if re.fullmatch(r"\d{1,2}", v):
            v = f"{int(v):02d}:00"
        elif re.fullmatch(r"\d{1,2}:\d{2}", v):
            h, m = v.split(":")
            v = f"{int(h):02d}:{int(m):02d}"
        else:
            raise ValueError("Ora non valida (formato richiesto HH:MM)")
        hh = int(v.split(":")[0])
        mm = int(v.split(":")[1])
        if hh < 0 or hh > 23 or mm < 0 or mm > 59:
            raise ValueError("Ora non valida (range)")
        return v

class AvailabilityCompat(BaseModel):
    # endpoint compatibilità: accetta qualunque cosa (evita 422)
    persone: Optional[int] = None
    data: Optional[str] = None
    sede: Optional[str] = None
    ora: Optional[str] = None

# -------------------------
# UTILS
# -------------------------
def _fingerprint_payload(payload: Dict[str, Any]) -> str:
    core = dict(payload)
    core.pop("nota", None)
    core.pop("dry_run", None)
    raw = json.dumps(core, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def _cleanup_idem(now: float) -> None:
    dead = []
    for k, v in _idem_cache.items():
        if now - v["ts"] > IDEMPOTENCY_TTL_SEC:
            dead.append(k)
    for k in dead:
        _idem_cache.pop(k, None)

def _norm_sede(s: str) -> str:
    s0 = s.strip().lower()
    if "talenti" in s0:
        return "Talenti - Roma"
    if "appia" in s0:
        return "Appia"
    if "palermo" in s0:
        return "Palermo"
    if "reggio" in s0:
        return "Reggio Calabria"
    if "ostia" in s0:
        return "Ostia Lido"
    return s.strip()

def _tipo_pasto_from_time(hhmm: str) -> str:
    hh = int(hhmm.split(":")[0])
    return "PRANZO" if hh < 17 else "CENA"

def _to_select_value_prefix(hhmm: str) -> str:
    # select value è "HH:MM:00"
    return f"{hhmm}:00"

def _is_target_closed_error(e: Exception) -> bool:
    msg = str(e).lower()
    return (
        "target page, context or browser has been closed" in msg
        or "browser has been closed" in msg
        or "target closed" in msg
    )

async def _ensure_browser():
    global _pw, _browser
    async with _browser_lock:
        if _pw is None:
            logger.info("Starting Playwright...")
            _pw = await async_playwright().start()
            logger.info("Playwright started.")
        if _browser is None:
            logger.info("Launching browser...")
            _browser = await _pw.chromium.launch(
                headless=PW_HEADLESS,
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
            logger.info("Browser launched.")

async def _restart_browser():
    global _browser
    async with _browser_lock:
        try:
            if _browser is not None:
                await _browser.close()
        except Exception:
            pass
        _browser = None
    await _ensure_browser()

async def _new_page():
    await _ensure_browser()
    context = await _browser.new_context(
        user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        viewport={"width": 390, "height": 844},
    )
    page = await context.new_page()

    async def _route(route):
        rt = route.request.resource_type
        if rt in ["image", "media", "font", "stylesheet"]:
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", _route)
    return context, page

async def _safe_screenshot(page, prefix: str) -> Optional[str]:
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = os.path.join(SCREENSHOT_DIR, f"{prefix}_{ts}.png")
        await page.screenshot(path=path, full_page=True)
        logger.error(f"Saved screenshot: {os.path.basename(path)}")
        return path
    except Exception:
        return None

async def _click_if_exists(page, selector: str, timeout=1500) -> bool:
    try:
        loc = page.locator(selector).first
        if await loc.count() > 0:
            await loc.click(timeout=timeout, force=True)
            return True
    except Exception:
        return False
    return False

async def _wait_visible(page, selector: str, timeout=PW_TIMEOUT_MS):
    await page.locator(selector).first.wait_for(state="visible", timeout=timeout)

async def _select_date(page, yyyy_mm_dd: str):
    await _wait_visible(page, ".step2", timeout=PW_TIMEOUT_MS)
    if await _click_if_exists(page, f".step2 .dataBtn[rel='{yyyy_mm_dd}']"):
        return
    try:
        await page.evaluate(
            """(d)=>{
              const inp = document.querySelector('#DataPren');
              if(!inp) return false;
              inp.value = d;
              inp.dispatchEvent(new Event('change', {bubbles:true}));
              return true;
            }""",
            yyyy_mm_dd,
        )
    except Exception:
        pass

async def _collect_enabled_time_options(page) -> List[Dict[str, str]]:
    opts = await page.evaluate(
        """()=>{
          const sel = document.querySelector('#OraPren');
          if(!sel) return [];
          const res = [];
          for(const o of Array.from(sel.querySelectorAll('option'))){
            const v = (o.value||'').trim();
            const t = (o.textContent||'').trim();
            if(!v) continue;
            if(o.disabled) continue;
            res.push({value:v, text:t});
          }
          return res;
        }"""
    )
    return opts or []

async def _has_disabled_option_for_value(page, value_prefix: str) -> bool:
    return bool(
        await page.evaluate(
            """(pref)=>{
              const sel = document.querySelector('#OraPren');
              if(!sel) return false;
              for(const o of Array.from(sel.querySelectorAll('option'))){
                const v = (o.value||'').trim();
                if(!v) continue;
                if(v.startsWith(pref) && o.disabled) return true;
              }
              return false;
            }""",
            value_prefix,
        )
    )

async def _select_time(page, desired_prefix: str) -> Tuple[bool, str]:
    enabled = await _collect_enabled_time_options(page)
    for o in enabled:
        if o["value"].startswith(desired_prefix):
            await page.select_option("#OraPren", o["value"])
            return True, "OK"
    if await _has_disabled_option_for_value(page, desired_prefix):
        return False, "FULL"
    return False, "NOT_FOUND"

async def _submit_form_and_wait_ok(page) -> bool:
    try:
        async with page.expect_response(lambda r: "ajax.php" in r.url, timeout=PW_TIMEOUT_MS) as resp_info:
            await page.locator("#prenoForm input[type='submit'], #prenoForm button[type='submit']").first.click(force=True)
        resp = await resp_info.value
        txt = (await resp.text()).strip()
        return txt == "OK"
    except Exception:
        return False

# -------------------------
# CORE BOOKING FLOW
# -------------------------
async def playwright_book(payload: Dict[str, Any]) -> Dict[str, Any]:
    sede_ui = _norm_sede(payload["sede"])
    persone = str(payload["persone"])
    d = payload["data"]
    hhmm = payload["ora"]
    desired_prefix = _to_select_value_prefix(hhmm)  # HH:MM:00
    tipo = _tipo_pasto_from_time(hhmm)              # PRANZO/CENA

    context = None
    page = None

    try:
        context, page = await _new_page()
        await page.goto(BOOKING_URL, timeout=PW_TIMEOUT_MS, wait_until="domcontentloaded")

        await _click_if_exists(page, "text=/accetta|consent|ok/i", timeout=1200)

        # STEP 1: persone
        await _wait_visible(page, ".step1", timeout=PW_TIMEOUT_MS)
        if not await _click_if_exists(page, f".nCoperti[rel='{persone}']"):
            await _click_if_exists(page, f".step1 span:text-is('{persone}')")

        # seggiolini
        if payload.get("seggiolone") or int(payload.get("seggiolini", 0)) > 0:
            await _click_if_exists(page, ".seggioliniTxt")
            seg = str(int(payload.get("seggiolini", 1) or 1))
            await _click_if_exists(page, f".nSeggiolini[rel='{seg}']")
        else:
            await _click_if_exists(page, ".SeggNO")

        # STEP 2: data
        await _select_date(page, d)

        # STEP 3: pranzo/cena
        await _wait_visible(page, ".step3", timeout=PW_TIMEOUT_MS)
        await _click_if_exists(page, f".tipoBtn[rel='{tipo}']")

        # STEP 4: lista ristoranti
        await _wait_visible(page, ".ristoCont", timeout=PW_TIMEOUT_MS)
        await page.locator(".ristoCont .ristoBtn").first.wait_for(state="visible", timeout=PW_TIMEOUT_MS)

        risto_locator = page.locator(".ristoCont .ristoBtn").filter(has_text=sede_ui).first
        if await risto_locator.count() == 0:
            risto_locator = page.locator(".ristoCont .ristoBtn").filter(has_text=payload["sede"]).first

        if await risto_locator.count() == 0:
            sedi = await page.evaluate(
                """()=>{
                  const res=[];
                  for(const el of Array.from(document.querySelectorAll('.ristoCont .ristoBtn'))){
                    const small=el.querySelector('small');
                    if(small) res.push((small.textContent||'').trim());
                  }
                  return Array.from(new Set(res)).filter(Boolean);
                }"""
            )
            return {
                "status": "NOT_AVAILABLE",
                "message": "Sede non trovata. Proponi una sede tra quelle disponibili.",
                "alternatives": [{"sede": s} for s in (sedi or [])],
            }

        await risto_locator.click(force=True)

        # STEP 5: select orario
        await _wait_visible(page, "#OraPren", timeout=PW_TIMEOUT_MS)
        await page.wait_for_timeout(700)

        selected, reason = await _select_time(page, desired_prefix)
        enabled_options = await _collect_enabled_time_options(page)

        alts = []
        for o in enabled_options:
            hm = o["value"][:5]
            alts.append({"ora": hm, "label": o["text"]})

        if not selected:
            if reason == "FULL":
                msg = "Il turno selezionato è pieno. Ti proponiamo in alternativa il seguente turno"
                chosen = alts[0]["ora"] if alts else None
                return {
                    "status": "FULL",
                    "message": f"{msg} {chosen}" if chosen else msg,
                    "alternatives": alts,
                }
            return {
                "status": "NOT_AVAILABLE",
                "message": "L’orario richiesto non è disponibile. Proponi uno degli orari pubblicati.",
                "alternatives": alts,
            }

        # nota
        nota = (payload.get("nota") or "").strip()
        if nota:
            try:
                await page.locator("#Nota").fill(nota)
            except Exception:
                pass

        # conferma
        await _click_if_exists(page, ".confDati", timeout=3000)

        # dati cliente
        await _wait_visible(page, "#prenoForm", timeout=PW_TIMEOUT_MS)
        await page.locator("#Nome").fill(payload["nome"])
        await page.locator("#Cognome").fill(payload["cognome"])
        await page.locator("#Email").fill(payload["email"])

        digits = re.sub(r"\D", "", payload["telefono"])
        phone10 = digits[-10:] if len(digits) >= 10 else digits
        await page.locator("#Telefono").fill(phone10)

        if payload.get("dry_run"):
            return {"status": "DRY_RUN", "message": "Form compilato (dry_run=true). Prenotazione non inviata.", "alternatives": []}

        ok = await _submit_form_and_wait_ok(page)
        if ok:
            return {"status": "CONFIRMED", "message": "Prenotazione confermata.", "alternatives": []}

        await _safe_screenshot(page, "booking_error")
        return {"status": "ERROR", "message": "Prenotazione non confermata. Riprovare o trasferire.", "alternatives": []}

    except PWTimeoutError:
        if page:
            await _safe_screenshot(page, "timeout")
        return {"status": "ERROR", "message": "Timeout. Riprovare o trasferire.", "alternatives": []}
    except PWError as e:
        if page:
            await _safe_screenshot(page, "pw_error")
        # se il browser/target si è chiuso, segnala per retry esterno
        if _is_target_closed_error(e):
            raise
        return {"status": "ERROR", "message": "Errore Playwright.", "alternatives": []}
    except Exception as e:
        if page:
            await _safe_screenshot(page, "exception")
        # alcuni ambienti buttano eccezione generica con messaggio "Target page..."
        if _is_target_closed_error(e):
            raise
        return {"status": "ERROR", "message": f"Errore: {type(e).__name__}", "alternatives": []}
    finally:
        try:
            if context is not None:
                await context.close()
        except Exception:
            pass

async def run_with_retries(payload: Dict[str, Any]) -> Dict[str, Any]:
    last_err = None
    for attempt in range(PW_RETRIES + 1):
        try:
            logger.info(f"Booking attempt #{attempt+1} - opening {BOOKING_URL}")
            return await playwright_book(payload)
        except Exception as e:
            last_err = e
            if _is_target_closed_error(e):
                logger.error("Target closed: restarting browser...")
                await _restart_browser()
                await asyncio.sleep(0.5)
                continue
            logger.error(f"Attempt {attempt+1} crashed: {type(e).__name__} - {e}")
            await asyncio.sleep(0.5)

    return {"status": "ERROR", "message": "Booking failed after retries.", "alternatives": []}

# -------------------------
# ROUTES
# -------------------------
@app.get("/")
def home():
    return {"status": "Centralino AI - deRione Booking Webhook (definitive-v2)"}

@app.post("/check_availability")
async def check_availability_compat(_: AvailabilityCompat):
    return {"status": "DISABLED", "message": "Check availability disabilitato: il sistema va direttamente in prenotazione."}

@app.post("/book_table")
async def book_table(req: BookingRequest):
    payload = req.dict()

    fp = _fingerprint_payload(payload)
    now = time.time()

    async with _idem_lock:
        _cleanup_idem(now)
        cached = _idem_cache.get(fp)
        if cached and (now - cached["ts"] <= IDEMPOTENCY_TTL_SEC):
            logger.info("Idempotency hit: returning cached result.")
            return cached["result"]

    async with _global_sem:
        async with _idem_lock:
            _cleanup_idem(time.time())
            cached = _idem_cache.get(fp)
            if cached and (time.time() - cached["ts"] <= IDEMPOTENCY_TTL_SEC):
                return cached["result"]

        logger.info(f"BOOK_TABLE request normalized: {json.dumps(payload, ensure_ascii=False)}")
        result = await run_with_retries(payload)

        async with _idem_lock:
            _idem_cache[fp] = {"ts": time.time(), "result": result}

        return result
