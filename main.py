import os
import re
import json
import time
import asyncio
import hashlib
import logging
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Body, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import Error as PlaywrightError


# -----------------------------
# Config
# -----------------------------
APP_NAME = "centralino-webhook"
BASE_URL = os.getenv("FIDY_BASE_URL", "https://rione.fidy.app/prenew.php")
REFERER_DEFAULT = os.getenv("REFERER_DEFAULT", "AI")

# Concorrenza: se troppo alta Playwright può chiudere context/browser
MAX_CONCURRENT_BOOKINGS = int(os.getenv("MAX_CONCURRENT_BOOKINGS", "1"))

# Retry prenotazione (errori intermittenti, load lento, ecc.)
PW_RETRIES = int(os.getenv("PW_RETRIES", "2"))
PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "25000"))

# Idempotenza / anti-duplicati
IDEMPOTENCY_TTL_SECONDS = int(os.getenv("IDEMPOTENCY_TTL_SECONDS", "180"))

# Screenshot su errori
SCREENSHOT_DIR = os.getenv("SCREENSHOT_DIR", ".")
SAVE_SCREENSHOT_ON_ERROR = os.getenv("SAVE_SCREENSHOT_ON_ERROR", "1") == "1"

# Mapping sedi: normalizzazione input → label da cercare in pagina
# (adatta se i nomi reali differiscono)
SEDE_ALIASES = {
    "talenti": "Talenti - Roma",
    "talenti - roma": "Talenti - Roma",
    "roma talenti": "Talenti - Roma",
    "appia": "Appia",
    "palermo": "Palermo",
    "reggio calabria": "Reggio Calabria",
    "ostia": "Ostia Lido",
    "ostia lido": "Ostia Lido",
}

# -----------------------------
# Logging
# -----------------------------
logger = logging.getLogger(APP_NAME)
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
logger.addHandler(handler)

# -----------------------------
# App
# -----------------------------
app = FastAPI(title=APP_NAME)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# Playwright global
# -----------------------------
_pw = None
_browser = None
_booking_sem = asyncio.Semaphore(MAX_CONCURRENT_BOOKINGS)

# Idempotency stores
_idempo_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_idempo_locks: Dict[str, asyncio.Lock] = {}


# -----------------------------
# Utilities
# -----------------------------
def _now_ts() -> float:
    return time.time()


def _clean_cache():
    # Pulizia TTL
    now = _now_ts()
    expired = [k for k, (ts, _) in _idempo_cache.items() if now - ts > IDEMPOTENCY_TTL_SECONDS]
    for k in expired:
        _idempo_cache.pop(k, None)


def _norm_str(x: Any) -> str:
    if x is None:
        return ""
    return str(x).strip()


def _norm_bool(x: Any) -> bool:
    if isinstance(x, bool):
        return x
    s = _norm_str(x).lower()
    return s in ("1", "true", "yes", "si", "sì", "y")


def _norm_int(x: Any, default: int = 0) -> int:
    try:
        return int(str(x).strip())
    except Exception:
        return default


def _norm_date(x: Any) -> Optional[str]:
    s = _norm_str(x)
    if not s:
        return None
    # accetta YYYY-MM-DD
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    # prova dd/mm/yyyy
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        dd, mm, yy = m.group(1), m.group(2), m.group(3)
        return f"{yy}-{int(mm):02d}-{int(dd):02d}"
    return None


def _norm_time(x: Any) -> Optional[str]:
    s = _norm_str(x)
    if not s:
        return None
    # “13” -> 13:00
    if re.fullmatch(r"\d{1,2}", s):
        return f"{int(s):02d}:00"
    # 13.30 / 13:30 / 13 e trenta -> gestiamo i casi base
    s = s.replace(".", ":")
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"
    return None


def _normalize_sede(raw: Any) -> Optional[str]:
    s = _norm_str(raw).lower()
    if not s:
        return None
    s = re.sub(r"\s+", " ", s)
    return SEDE_ALIASES.get(s, None) or raw  # fallback: usa il testo originale


def _fingerprint(payload: Dict[str, Any]) -> str:
    # Se arriva già fingerprint, usa quello.
    fp = _norm_str(payload.get("fingerprint"))
    if fp:
        return fp
    # Altrimenti calcola su campi determinanti
    key = {
        "nome": _norm_str(payload.get("nome")),
        "cognome": _norm_str(payload.get("cognome")),
        "telefono": _norm_str(payload.get("telefono")),
        "email": _norm_str(payload.get("email")),
        "persone": _norm_int(payload.get("persone"), 0),
        "sede": _norm_str(payload.get("sede")),
        "data": _norm_str(payload.get("data")),
        "ora": _norm_str(payload.get("ora")),
        "seggiolone": bool(payload.get("seggiolone", False)),
        "seggiolini": _norm_int(payload.get("seggiolini"), 0),
    }
    blob = json.dumps(key, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _validate_required(p: Dict[str, Any]) -> List[str]:
    missing = []
    for k in ("nome", "cognome", "telefono", "email", "persone", "sede", "data", "ora"):
        if k not in p or _norm_str(p.get(k)) == "":
            missing.append(k)
    # range persone
    if "persone" in p:
        n = _norm_int(p.get("persone"), 0)
        if n < 1 or n > 9:
            missing.append("persone_range_1_9")
    # data valida e non passata
    if p.get("data"):
        try:
            d = datetime.strptime(p["data"], "%Y-%m-%d").date()
            if d < date.today():
                missing.append("data_passata")
            if d > date.today() + timedelta(days=30):
                missing.append("data_oltre_30gg")
        except Exception:
            missing.append("data_formato")
    # ora valida HH:MM
    if p.get("ora"):
        if not re.fullmatch(r"\d{2}:\d{2}", p["ora"]):
            missing.append("ora_formato")
    # email base
    if p.get("email"):
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", p["email"]):
            missing.append("email_formato")
    # telefono base (molto permissivo)
    if p.get("telefono"):
        t = re.sub(r"\s+", "", p["telefono"])
        if not re.fullmatch(r"\+?\d{8,15}", t):
            missing.append("telefono_formato")
    return missing


def _format_message_slot_full(alt: Optional[str]) -> str:
    if alt:
        return f"Il turno selezionato è pieno. Ti proponiamo in alternativa il seguente turno {alt}."
    return "Il turno selezionato è pieno. Ti proponiamo in alternativa un altro turno disponibile."


# -----------------------------
# Startup / Shutdown
# -----------------------------
@app.on_event("startup")
async def _startup():
    global _pw, _browser
    logger.info("Starting Playwright...")
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    )
    logger.info("Playwright started.")


@app.on_event("shutdown")
async def _shutdown():
    global _pw, _browser
    try:
        if _browser:
            await _browser.close()
    finally:
        _browser = None
    try:
        if _pw:
            await _pw.stop()
    finally:
        _pw = None


# -----------------------------
# Health & Home
# -----------------------------
@app.get("/", response_class=PlainTextResponse)
async def home():
    return "OK"


# -----------------------------
# IMPORTANT: Compat endpoints so ElevenLabs won't 422
# -----------------------------
@app.post("/check_availability")
async def check_availability_compat(_: Dict[str, Any] = Body(default={})):
    # Endpoint mantenuto solo per compatibilità: risponde sempre 200.
    # (Così anche se ElevenLabs lo chiama, non rompe il flusso)
    return {"ok": True, "status": "disabled", "message": "availability_check_disabled"}


@app.post("/checkavailability")
async def checkavailability_alias(_: Dict[str, Any] = Body(default={})):
    return {"ok": True, "status": "disabled", "message": "availability_check_disabled"}


# -----------------------------
# Playwright booking logic
# -----------------------------
async def _save_screenshot(page, prefix: str = "booking_error") -> Optional[str]:
    if not SAVE_SCREENSHOT_ON_ERROR:
        return None
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = os.path.join(SCREENSHOT_DIR, f"{prefix}_{ts}.png")
        await page.screenshot(path=path, full_page=True)
        logger.error(f"Saved screenshot: {os.path.basename(path)}")
        return path
    except Exception:
        return None


async def _extract_time_options(page) -> List[str]:
    # Legge tutte le option non disabled presenti in #OraPren
    opts = await page.locator("#OraPren option").all()
    out = []
    for o in opts:
        val = (await o.get_attribute("value")) or ""
        disabled = await o.get_attribute("disabled")
        if not val or val.strip() == "" or disabled is not None:
            continue
        # val spesso è HH:MM:00
        m = re.match(r"^(\d{2}:\d{2})", val.strip())
        if m:
            out.append(m.group(1))
    # unique preserving order
    seen = set()
    uniq = []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


async def _pick_best_alternative(desired_hhmm: str, available: List[str]) -> Optional[str]:
    if not available:
        return None
    # scegli il più vicino >= desired, altrimenti il primo disponibile
    try:
        dh, dm = map(int, desired_hhmm.split(":"))
        desired_min = dh * 60 + dm
        avail_min = []
        for t in available:
            h, m = map(int, t.split(":"))
            avail_min.append((h * 60 + m, t))
        later = [x for x in avail_min if x[0] >= desired_min]
        if later:
            later.sort(key=lambda x: x[0])
            return later[0][1]
        avail_min.sort(key=lambda x: x[0])
        return avail_min[0][1]
    except Exception:
        return available[0]


async def _click_people(page, n: int) -> None:
    # .nCoperti rel="1..9"
    await page.locator(f'.nCoperti[rel="{n}"]').first.click(timeout=PW_TIMEOUT_MS)


async def _set_seggiolini(page, seggiolini: int) -> None:
    # Default NO (SeggNO già selezionato). Se seggiolini > 0 clicca SI e seleziona numero.
    if seggiolini and seggiolini > 0:
        await page.locator(".seggioliniTxt, .SeggSI").first.click(timeout=PW_TIMEOUT_MS)
        await page.locator(f'.nSeggiolini[rel="{seggiolini}"]').first.click(timeout=PW_TIMEOUT_MS)
    else:
        # clicca NO per sicurezza (se esiste)
        if await page.locator(".SeggNO").count() > 0:
            await page.locator(".SeggNO").first.click(timeout=PW_TIMEOUT_MS)


async def _set_date(page, yyyy_mm_dd: str) -> None:
    # Usa input #DataPren (presente) oppure bottoni dataOggi.
    if await page.locator("#DataPren").count() > 0:
        await page.locator("#DataPren").fill(yyyy_mm_dd, timeout=PW_TIMEOUT_MS)
        # trigger change
        await page.locator("#DataPren").dispatch_event("change")
    else:
        # fallback: tenta click bottone rel=...
        btn = page.locator(f'.dataBtn[rel="{yyyy_mm_dd}"], .dataOggi[rel="{yyyy_mm_dd}"]')
        if await btn.count() > 0:
            await btn.first.click(timeout=PW_TIMEOUT_MS)


async def _set_tipologia(page, hhmm: str) -> str:
    # Decide PRANZO/CENA in base all’orario richiesto (semplice regola)
    h = int(hhmm.split(":")[0])
    tip = "PRANZO" if h < 17 else "CENA"
    # click bottone
    btn = page.locator(f'.tipoBtn[rel="{tip}"]')
    if await btn.count() > 0:
        await btn.first.click(timeout=PW_TIMEOUT_MS)
    return tip


async def _select_sede(page, sede_label: str) -> None:
    # La lista ristoranti viene caricata in .ristoCont con .ristoBtn contenenti il nome
    # Aspetta che compaia almeno un .ristoBtn
    await page.wait_for_selector(".ristoBtn", timeout=PW_TIMEOUT_MS)

    # Cerca un ristoBtn che contenga il testo sede_label
    candidates = page.locator(".ristoBtn")
    count = await candidates.count()
    best = None
    for i in range(count):
        el = candidates.nth(i)
        txt = (await el.inner_text()).strip()
        if sede_label.lower() in txt.lower():
            best = el
            break

    if best is None:
        # fallback: prova match con alias (Talenti ecc.)
        best = candidates.first

    await best.click(timeout=PW_TIMEOUT_MS)


async def _select_time(page, hhmm: str) -> Tuple[str, List[str], str]:
    """
    Returns:
      status: "ok" | "slot_not_found" | "slot_full"
      alternatives: list of HH:MM
      selected_value: string value chosen (HH:MM:00)
    """
    await page.wait_for_selector("#OraPren", timeout=PW_TIMEOUT_MS)

    desired_value = f"{hhmm}:00"
    option = page.locator(f'#OraPren option[value="{desired_value}"]')
    if await option.count() == 0:
        # Non esiste → proponi turni pubblicati
        available = await _extract_time_options(page)
        return "slot_not_found", available, ""

    # Esiste: verifica disabled
    disabled = await option.get_attribute("disabled")
    if disabled is not None:
        available = await _extract_time_options(page)
        return "slot_full", available, ""

    # Seleziona
    await page.select_option("#OraPren", value=desired_value, timeout=PW_TIMEOUT_MS)
    return "ok", [], desired_value


async def _fill_customer_and_submit(page, payload: Dict[str, Any]) -> None:
    # click CONFERMA per arrivare ai dati (stepDati)
    if await page.locator(".confDati").count() > 0:
        await page.locator(".confDati").first.click(timeout=PW_TIMEOUT_MS)

    # attende form
    await page.wait_for_selector("#prenoForm", timeout=PW_TIMEOUT_MS)

    await page.locator("#Nome").fill(payload["nome"], timeout=PW_TIMEOUT_MS)
    await page.locator("#Cognome").fill(payload["cognome"], timeout=PW_TIMEOUT_MS)
    await page.locator("#Email").fill(payload["email"], timeout=PW_TIMEOUT_MS)

    # Telefono: nel form originale pattern \d* maxlength 10.
    # Noi togliamo +39 e non numeri e prendiamo ultime 10 cifre se serve.
    tel = re.sub(r"\D+", "", payload["telefono"])
    if len(tel) > 10:
        tel = tel[-10:]
    await page.locator("#Telefono").fill(tel, timeout=PW_TIMEOUT_MS)

    if payload.get("nota"):
        if await page.locator("#Nota").count() > 0:
            await page.locator("#Nota").fill(payload["nota"], timeout=PW_TIMEOUT_MS)

    # submit
    await page.locator('#prenoForm input[type="submit"], #prenoForm .sbmButt input[type="submit"]').first.click(timeout=PW_TIMEOUT_MS)


async def playwright_submit_booking(payload: Dict[str, Any]) -> Dict[str, Any]:
    referer = payload.get("referer") or REFERER_DEFAULT
    url = f"{BASE_URL}?referer={referer}"

    ctx = await _browser.new_context()
    page = await ctx.new_page()

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=PW_TIMEOUT_MS)

        # Step 1: persone
        await page.wait_for_selector(".nCoperti", timeout=PW_TIMEOUT_MS)
        await _click_people(page, int(payload["persone"]))

        # seggiolini
        await _set_seggiolini(page, int(payload.get("seggiolini", 0)))

        # data
        await _set_date(page, payload["data"])

        # tipologia in base a ora
        tipologia = await _set_tipologia(page, payload["ora"])
        # la pagina usa Tipologia per caricare ristoCont; attendiamo che compaia
        # (alcuni flussi lo mostrano solo dopo i click)
        await page.wait_for_timeout(400)

        # selezione sede
        sede_label = payload["sede_label"]
        await _select_sede(page, sede_label)

        # selezione orario/turno
        status, alternatives, _ = await _select_time(page, payload["ora"])
        if status == "slot_not_found":
            best = await _pick_best_alternative(payload["ora"], alternatives)
            return {
                "ok": False,
                "status": "slot_not_found",
                "message": "Orario non disponibile. Ecco i turni pubblicati.",
                "alternatives": alternatives,
                "suggested": best,
            }
        if status == "slot_full":
            best = await _pick_best_alternative(payload["ora"], alternatives)
            return {
                "ok": False,
                "status": "slot_full",
                "message": _format_message_slot_full(best),
                "alternatives": alternatives,
                "suggested": best,
            }

        # nota (se presente) prima di conferma
        if payload.get("nota") and await page.locator("#Nota").count() > 0:
            await page.locator("#Nota").fill(payload["nota"], timeout=PW_TIMEOUT_MS)

        # conferma dati e invio form
        await _fill_customer_and_submit(page, payload)

        # attesa esito: non sappiamo la pagina finale, quindi controlliamo segnali
        # 1) URL cambia su prenew_res.php oppure 2) testo "OK" / "confermata" ecc.
        try:
            await page.wait_for_load_state("networkidle", timeout=PW_TIMEOUT_MS)
        except Exception:
            pass

        content = (await page.content()).lower()

        # euristiche esito (da adattare se serve)
        if "prenotazione" in content and ("confermata" in content or "confermato" in content or "grazie" in content):
            return {"ok": True, "status": "confirmed", "message": "Prenotazione confermata."}

        # fallback: se non troviamo segnali, consideriamo “submitted”
        return {"ok": True, "status": "submitted", "message": "Richiesta inviata. In attesa conferma."}

    except PlaywrightTimeoutError as e:
        await _save_screenshot(page)
        raise RuntimeError(f"timeout: {str(e)}") from e
    except PlaywrightError as e:
        # include target closed, page closed, ecc.
        await _save_screenshot(page)
        raise RuntimeError(f"playwright_error: {str(e)}") from e
    except Exception as e:
        await _save_screenshot(page)
        raise
    finally:
        try:
            await ctx.close()
        except Exception:
            pass


async def run_with_retries(fn, retries: int):
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return await fn(attempt)
        except Exception as e:
            last_err = e
            logger.error(f"Attempt {attempt} failed: {e}")
            await asyncio.sleep(0.4 * attempt)
    raise last_err


# -----------------------------
# Public endpoint: book_table
# -----------------------------
@app.post("/book_table")
async def book_table(payload: Dict[str, Any] = Body(...)):
    """
    Accetta qualunque JSON e normalizza internamente.
    Risponde SEMPRE 200 con {ok: bool, status: ..., message: ..., alternatives?: [...]}
    (così ElevenLabs non si incastra su 422)
    """
    _clean_cache()

    # Normalizzazione chiavi possibili (camelCase ecc.)
    p = dict(payload or {})

    normalized = {
        "nome": _norm_str(p.get("nome") or p.get("Nome")),
        "cognome": _norm_str(p.get("cognome") or p.get("Cognome")),
        "email": _norm_str(p.get("email") or p.get("Email")),
        "telefono": _norm_str(p.get("telefono") or p.get("Telefono")),
        "persone": _norm_int(p.get("persone") or p.get("Persone") or p.get("coperti"), 0),
        "sede": _normalize_sede(p.get("sede") or p.get("Sede") or p.get("ristorante")),
        "data": _norm_date(p.get("data") or p.get("Data") or p.get("giorno")),
        "ora": _norm_time(p.get("ora") or p.get("Ora") or p.get("orario")),
        "seggiolone": _norm_bool(p.get("seggiolone") or p.get("Seggiolone") or False),
        "seggiolini": _norm_int(p.get("seggiolini") or p.get("Seggiolini") or 0, 0),
        "nota": _norm_str(p.get("nota") or p.get("Nota") or ""),
        "referer": _norm_str(p.get("referer") or REFERER_DEFAULT),
        "dry_run": _norm_bool(p.get("dry_run") or False),
    }

    # label sede (quella da cercare nella pagina)
    sede_label = _norm_str(normalized["sede"])
    sede_label_norm = SEDE_ALIASES.get(sede_label.lower().strip(), None)
    normalized["sede_label"] = sede_label_norm or sede_label

    fp = _fingerprint({**normalized, **p})
    normalized["fingerprint"] = fp

    logger.info(f"BOOK_TABLE request normalized: {json.dumps(normalized, ensure_ascii=False)}")

    # Idempotenza
    if fp in _idempo_cache:
        ts, cached = _idempo_cache[fp]
        if _now_ts() - ts <= IDEMPOTENCY_TTL_SECONDS:
            return JSONResponse(status_code=200, content={"ok": True, "status": "idempotent_replay", **cached})

    # Lock per fingerprint (anti doppio invio simultaneo)
    lock = _idempo_locks.get(fp)
    if lock is None:
        lock = asyncio.Lock()
        _idempo_locks[fp] = lock

    async with lock:
        # ricontrolla cache dopo attesa
        if fp in _idempo_cache:
            ts, cached = _idempo_cache[fp]
            if _now_ts() - ts <= IDEMPOTENCY_TTL_SECONDS:
                return JSONResponse(status_code=200, content={"ok": True, "status": "idempotent_replay", **cached})

        # validazione business (ma rispondiamo 200 per non far impazzire ElevenLabs)
        missing = _validate_required(normalized)
        if missing:
            msg = "Dati mancanti o non validi: " + ", ".join(missing)
            out = {"ok": False, "status": "invalid_request", "message": msg, "missing": missing}
            _idempo_cache[fp] = (_now_ts(), out)
            return JSONResponse(status_code=200, content=out)

        # dry_run
        if normalized.get("dry_run"):
            out = {"ok": True, "status": "dry_run", "message": "Dry run: nessuna prenotazione eseguita."}
            _idempo_cache[fp] = (_now_ts(), out)
            return JSONResponse(status_code=200, content=out)

        async with _booking_sem:
            async def _do(attempt: int):
                logger.info(f"Booking attempt #{attempt} - opening {BASE_URL}?referer={normalized['referer']}")
                result = await playwright_submit_booking(normalized)
                return result

            try:
                result = await run_with_retries(_do, retries=PW_RETRIES)
                # cache
                _idempo_cache[fp] = (_now_ts(), result)
                return JSONResponse(status_code=200, content=result)
            except Exception as e:
                # Anche qui: 200, ma ok=False (così ElevenLabs gestisce a voce)
                err = str(e)
                out = {
                    "ok": False,
                    "status": "failed",
                    "message": "Non riesco a completare la prenotazione in questo momento.",
                    "debug": err,  # se non vuoi mai esporre, togli questa riga
                }
                _idempo_cache[fp] = (_now_ts(), out)
                logger.error(f"Booking failed: {e}", exc_info=True)
                return JSONResponse(status_code=200, content=out)
