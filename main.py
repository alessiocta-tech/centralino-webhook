# main.py
import os
import re
import json
import time
import uuid
import hashlib
import logging
from typing import Optional, Dict, Any, List, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from playwright.async_api import async_playwright

APP_NAME = "centralino-webhook"

# URL Fidy
BASE_URL = os.getenv("FIDY_URL", "https://rione.fidy.app/prenew.php")
REFERER_DEFAULT = os.getenv("REFERER_DEFAULT", "AI")

# Playwright stability
PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "60000"))
PW_NAV_TIMEOUT_MS = int(os.getenv("PW_NAV_TIMEOUT_MS", "60000"))
PW_RETRIES = int(os.getenv("PW_RETRIES", "2"))
BLOCK_HEAVY_RESOURCES = os.getenv("PW_BLOCK_HEAVY", "1") == "1"

# Idempotency (anti-doppia prenotazione)
IDEMPOTENCY_TTL_SEC = int(os.getenv("IDEMPOTENCY_TTL_SEC", "180"))
_seen: Dict[str, float] = {}

# Screenshots
SCREENSHOTS_DIR = os.getenv("SCREENSHOTS_DIR", "/tmp")
SAVE_SCREENSHOTS = os.getenv("SAVE_SCREENSHOTS", "1") == "1"

logger = logging.getLogger(APP_NAME)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

app = FastAPI(title="Centralino Webhook", version="DEF-BOOKING-ONLY-COMPAT")


# ----------------------------
# Utils
# ----------------------------
def now_ts() -> float:
    return time.time()


def cleanup_idempotency():
    t = now_ts()
    expired = [k for k, v in _seen.items() if t - v > IDEMPOTENCY_TTL_SEC]
    for k in expired:
        _seen.pop(k, None)


def make_fingerprint(p: Dict[str, Any]) -> str:
    base = {
        "nome": (p.get("nome") or "").strip().lower(),
        "cognome": (p.get("cognome") or "").strip().lower(),
        "telefono": (p.get("telefono") or "").strip(),
        "email": (p.get("email") or "").strip().lower(),
        "persone": p.get("persone"),
        "sede": (p.get("sede") or "").strip().lower(),
        "data": p.get("data"),
        "ora": p.get("ora"),
        "seggiolini": p.get("seggiolini", 0),
        "nota": (p.get("nota") or "").strip().lower(),
    }
    raw = json.dumps(base, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def idempotency_hit(fp: str) -> bool:
    cleanup_idempotency()
    t = now_ts()
    if fp in _seen and (t - _seen[fp] <= IDEMPOTENCY_TTL_SEC):
        return True
    _seen[fp] = t
    return False


def normalize_time(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip().replace(".", ":")
    if re.fullmatch(r"\d{1,2}", s):
        h = int(s)
        return f"{h:02d}:00"
    if re.fullmatch(r"\d{1,2}:\d{2}", s):
        h, m = s.split(":")
        return f"{int(h):02d}:{int(m):02d}"
    if re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", s):
        h, m, _sec = s.split(":")
        return f"{int(h):02d}:{int(m):02d}"
    return None


def normalize_date(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    return None


def normalize_phone(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip().replace(" ", "")
    # accetta + e cifre (6..15)
    if re.fullmatch(r"\+?\d{6,15}", s):
        return s
    # fallback: prova a ripulire
    digits = re.sub(r"\D", "", s)
    if 6 <= len(digits) <= 15:
        return "+" + digits if s.strip().startswith("+") else digits
    return None


def looks_like_email(v: Any) -> bool:
    if v is None:
        return False
    s = str(v).strip()
    # validazione “soft”, per evitare 422
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s))


def meal_from_time(hhmm: str) -> str:
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


def normalize_option_time(value: str) -> Optional[str]:
    if not value:
        return None
    m = re.match(r"^(\d{2}):(\d{2})", value)
    if not m:
        return None
    return f"{m.group(1)}:{m.group(2)}"


async def get_select_options(page) -> List[Dict[str, Any]]:
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


def build_alternatives(options: List[Dict[str, Any]]) -> List[str]:
    alts = []
    for o in options:
        t = normalize_option_time(o.get("value", ""))
        if not t:
            continue
        if o.get("disabled"):
            continue
        if t not in alts:
            alts.append(t)
    return alts


def is_option_full_or_disabled(opt: Dict[str, Any]) -> bool:
    if opt.get("disabled"):
        return True
    txt = (opt.get("text") or "").upper()
    return ("POSTI ESAURITI" in txt) or ("ESAURITO" in txt)


def sede_map(s: str) -> str:
    m = {
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
    k = (s or "").strip().lower()
    return m.get(k, (s or "").strip())


def coerce_int(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None


def normalize_payload(raw: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """
    Accetta input “sporchi” da ElevenLabs:
    - chiavi diverse (Nome/Cognome/Telefono ecc.)
    - ora "13:00:00"
    - persone come stringa
    Restituisce payload normalizzato + lista campi mancanti/invalidi.
    """
    # mapping “robusto”
    p = dict(raw or {})

    # alias keys (ElevenLabs / varianti)
    aliases = {
        "nome": ["nome", "Nome", "first_name", "firstname"],
        "cognome": ["cognome", "Cognome", "last_name", "lastname", "surname"],
        "email": ["email", "Email", "mail"],
        "telefono": ["telefono", "Telefono", "phone", "tel"],
        "persone": ["persone", "Persone", "coperti", "Coperti", "guests"],
        "sede": ["sede", "Sede", "ristorante", "Ristorante", "location"],
        "data": ["data", "Data", "date"],
        "ora": ["ora", "Ora", "time"],
        "seggiolone": ["seggiolone", "Seggiolone"],
        "seggiolini": ["seggiolini", "Seggiolini"],
        "nota": ["nota", "Nota", "notes", "note"],
        "referer": ["referer", "Referer"],
        "dry_run": ["dry_run", "dryRun", "test"],
        "fingerprint": ["fingerprint", "fp", "idempotency_key"],
    }

    out: Dict[str, Any] = {}
    for k, keys in aliases.items():
        for kk in keys:
            if kk in p and p[kk] is not None and str(p[kk]).strip() != "":
                out[k] = p[kk]
                break

    # normalize fields
    out["nome"] = (out.get("nome") or "").strip()
    out["cognome"] = (out.get("cognome") or "").strip()
    out["email"] = (out.get("email") or "").strip()
    out["telefono"] = normalize_phone(out.get("telefono"))
    out["persone"] = coerce_int(out.get("persone"))
    out["sede"] = sede_map(out.get("sede") or "")
    out["data"] = normalize_date(out.get("data"))
    out["ora"] = normalize_time(out.get("ora"))
    out["seggiolone"] = bool(out.get("seggiolone")) if out.get("seggiolone") is not None else False
    out["seggiolini"] = coerce_int(out.get("seggiolini")) or 0
    out["nota"] = (out.get("nota") or "").strip()
    out["referer"] = (out.get("referer") or REFERER_DEFAULT).strip()
    out["dry_run"] = bool(out.get("dry_run")) if out.get("dry_run") is not None else False

    missing = []

    # required fields
    if not out["nome"]:
        missing.append("nome")
    if not out["cognome"]:
        missing.append("cognome")
    if not looks_like_email(out["email"]):
        missing.append("email")
    if not out["telefono"]:
        missing.append("telefono")
    if out["persone"] is None or not (1 <= out["persone"] <= 9):
        missing.append("persone")
    if not out["sede"]:
        missing.append("sede")
    if not out["data"]:
        missing.append("data")
    if not out["ora"]:
        missing.append("ora")

    # bounds
    if out["seggiolini"] < 0:
        out["seggiolini"] = 0
    if out["seggiolini"] > 5:
        out["seggiolini"] = 5

    return out, missing


# ----------------------------
# Routes
# ----------------------------
@app.get("/")
def home():
    return {"status": "OK", "endpoints": ["/book_table"]}


# Disponibilità DISABILITATA, ma non deve MAI generare 422
@app.post("/check_availability")
async def check_availability_disabled(request: Request):
    try:
        await request.body()
    except Exception:
        pass
    return JSONResponse(
        status_code=200,
        content={"status": "DISABLED", "message": "Disponibilità ignorata. Usa /book_table."},
    )


@app.post("/checkavailability")
async def checkavailability_disabled_alias(request: Request):
    try:
        await request.body()
    except Exception:
        pass
    return JSONResponse(
        status_code=200,
        content={"status": "DISABLED", "message": "Alias disabilitato. Usa /book_table."},
    )


@app.post("/book_table")
async def book_table(request: Request):
    """
    Endpoint COMPAT: non usa Pydantic per evitare 422 da ElevenLabs.
    Valida qui dentro e risponde SEMPRE 200 con stato e dettagli.
    """
    try:
        raw = await request.json()
        if not isinstance(raw, dict):
            raw = {"value": raw}
    except Exception:
        raw = {}

    payload, missing = normalize_payload(raw)

    logger.info("BOOK_TABLE incoming(raw)=%s", json.dumps(raw, ensure_ascii=False))
    logger.info("BOOK_TABLE normalized=%s", json.dumps(payload, ensure_ascii=False))

    if missing:
        # NON 422: ritorniamo 200 e diciamo cosa manca.
        return {
            "status": "NEED_DATA",
            "missing": missing,
            "message": "Dati mancanti o non validi. Richiedere solo i campi mancanti.",
        }

    fp = payload.get("fingerprint") or make_fingerprint(payload)
    if idempotency_hit(fp):
        logger.warning("Idempotency hit for fp=%s", fp[:10])
        return {
            "status": "DUPLICATE_PREVENTED",
            "result": "Richiesta duplicata rilevata. Prenotazione già in lavorazione.",
            "fingerprint": fp,
        }

    tipo = meal_from_time(payload["ora"])

    async def _attempt(attempt_no: int):
        url = f"{BASE_URL}?referer={payload.get('referer') or REFERER_DEFAULT}"
        logger.info("Booking attempt #%s - opening %s", attempt_no, url)

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
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
                await page.goto(url, wait_until="domcontentloaded")

                # step 1: persone
                await click_best_effort(page.locator(f"span.nCoperti:text-is('{payload['persone']}')"), timeout=4000)

                # seggiolini
                if payload.get("seggiolini", 0) > 0:
                    await click_best_effort(page.locator(".seggioliniTxt"), timeout=2000)
                    await page.wait_for_timeout(200)
                    await click_best_effort(page.locator(f"span.nSeggiolini[rel='{payload['seggiolini']}']"), timeout=2000)
                else:
                    await click_best_effort(page.locator(".SeggNO"), timeout=1200)

                # step 2: data via input date
                await page.evaluate(
                    """(d) => {
                      const inp = document.querySelector('#DataPren') || document.querySelector('input[type=date]');
                      if (inp) { inp.value = d; inp.dispatchEvent(new Event('change', {bubbles:true})); }
                    }""",
                    payload["data"],
                )

                # step 3: pranzo/cena
                await click_best_effort(page.locator(f"span.tipoBtn[rel='{tipo}']"), timeout=6000)

                # step 4: sede
                await page.wait_for_selector(".ristoBtn", timeout=PW_TIMEOUT_MS)
                sede_target = payload["sede"]
                sede_locator = page.locator(".ristoBtn").filter(has_text=re.compile(re.escape(sede_target), re.I))
                ok = await click_best_effort(sede_locator, timeout=6000)
                if not ok:
                    await safe_screenshot(page, "booking_error_sede")
                    raise RuntimeError(f"Sede non trovata: {sede_target}")

                # step 5: orario
                await page.wait_for_timeout(800)
                opts = await get_select_options(page)
                if len(opts) <= 1:
                    await page.wait_for_timeout(800)
                    opts = await get_select_options(page)

                desired = payload["ora"]
                chosen = None
                for o in opts:
                    t = normalize_option_time(o.get("value", ""))
                    if t == desired:
                        chosen = o
                        break

                alternatives = build_alternatives(opts)

                if chosen is None:
                    return {
                        "status": "TIME_NOT_AVAILABLE",
                        "result": "Il turno selezionato è pieno. Ti proponiamo in alternativa il seguente turno",
                        "requested": desired,
                        "alternatives": alternatives,
                        "sede": sede_target,
                        "data": payload["data"],
                        "tipo": tipo,
                        "fingerprint": fp,
                    }

                if is_option_full_or_disabled(chosen):
                    return {
                        "status": "TURN_FULL",
                        "result": "Il turno selezionato è pieno. Ti proponiamo in alternativa il seguente turno",
                        "requested": desired,
                        "alternatives": alternatives,
                        "sede": sede_target,
                        "data": payload["data"],
                        "tipo": tipo,
                        "fingerprint": fp,
                    }

                await page.select_option("#OraPren", value=chosen.get("value", ""))
                await page.wait_for_timeout(250)

                if payload.get("nota"):
                    try:
                        await page.fill("#Nota", payload["nota"])
                    except Exception:
                        pass

                await click_best_effort(page.locator("a.confDati"), timeout=6000)
                await page.wait_for_timeout(800)

                # dati cliente
                tel_digits = re.sub(r"\D", "", payload["telefono"])
                tel_site = tel_digits[-10:] if len(tel_digits) >= 10 else tel_digits

                await page.fill("#Nome", payload["nome"])
                await page.fill("#Cognome", payload["cognome"])
                await page.fill("#Email", payload["email"])
                await page.fill("#Telefono", tel_site)

                if payload.get("dry_run"):
                    return {
                        "status": "DRY_RUN_OK",
                        "result": "Form compilato (dry_run=true). Click PRENOTA non eseguito.",
                        "sede": sede_target,
                        "data": payload["data"],
                        "ora": desired,
                        "tipo": tipo,
                        "fingerprint": fp,
                        "alternatives": alternatives,
                    }

                ok_submit = await click_best_effort(page.locator("form#prenoForm input[type='submit']"), timeout=8000)
                if not ok_submit:
                    await safe_screenshot(page, "booking_error_submit")
                    raise RuntimeError("Non riesco a premere PRENOTA.")

                await page.wait_for_timeout(1500)

                # conferma best-effort
                content = (await page.content()).lower()
                if "ok" in content or "prenot" in content:
                    return {
                        "status": "CONFIRMED",
                        "result": "Prenotazione confermata.",
                        "sede": sede_target,
                        "data": payload["data"],
                        "ora": desired,
                        "tipo": tipo,
                        "fingerprint": fp,
                    }

                return {
                    "status": "SUBMITTED",
                    "result": "Richiesta inviata.",
                    "sede": sede_target,
                    "data": payload["data"],
                    "ora": desired,
                    "tipo": tipo,
                    "fingerprint": fp,
                }

            except Exception as e:
                await safe_screenshot(page, "booking_error")
                msg = str(e)
                if "TargetClosed" in msg or "has been closed" in msg:
                    raise RuntimeError("Browser chiuso durante la prenotazione.") from e
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
    for attempt in range(1, PW_RETRIES + 2):
        try:
            return await _attempt(attempt)
        except Exception as e:
            last_err = e
            logger.error("Attempt %s failed: %s", attempt, str(e))
            await _sleep_ms(350)

    logger.exception("Booking failed: %s", str(last_err))
    return {
        "status": "FAILED",
        "result": "Impossibile completare la prenotazione. Trasferire al centralino.",
    }


async def _sleep_ms(ms: int):
    import asyncio
    await asyncio.sleep(ms / 1000.0)
