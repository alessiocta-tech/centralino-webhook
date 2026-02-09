# main.py
# Centralino webhook → Prenotazione Fidy (Playwright) + controlli robusti (telefono/email/data) + tracking referer=AI
#
# ENV consigliate su Railway:
#   PORT=8080 (Railway la imposta spesso da sola)
#   FIDY_BASE_URL=https://rione.fidy.app/prenew.php
#   DEFAULT_REFERER=AI
#   PHONE_DEFAULT_REGION=IT
#   DRY_RUN=0   # 0 = PRODUZIONE (prenota davvero), 1 = solo simulazione
#   RATE_LIMIT_PER_MINUTE=30
#   ALLOWED_ORIGINS=*   (opzionale se usi CORS)
#
# Avvio locale:
#   uvicorn main:app --host 0.0.0.0 --port 8080

from __future__ import annotations

import os
import re
import time
import uuid
import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Optional, Dict, Any, Tuple

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, validator

import phonenumbers
from phonenumbers import NumberParseException

from playwright.async_api import async_playwright, Browser, Page, TimeoutError as PWTimeoutError

# -----------------------------
# Logging
# -----------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("centralino")

# -----------------------------
# Config
# -----------------------------
FIDY_BASE_URL = os.getenv("FIDY_BASE_URL", "https://rione.fidy.app/prenew.php")
DEFAULT_REFERER = os.getenv("DEFAULT_REFERER", "AI")
PHONE_DEFAULT_REGION = os.getenv("PHONE_DEFAULT_REGION", "IT")
DRY_RUN_DEFAULT = os.getenv("DRY_RUN", "1").strip()  # 0 prod, 1 dry
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*").split(",")

# Playwright tuning
PW_NAV_TIMEOUT_MS = int(os.getenv("PW_NAV_TIMEOUT_MS", "30000"))
PW_ACTION_TIMEOUT_MS = int(os.getenv("PW_ACTION_TIMEOUT_MS", "20000"))
PW_RETRIES = int(os.getenv("PW_RETRIES", "2"))  # tentativi extra oltre al primo

# -----------------------------
# Simple in-memory rate limiter (per IP)
# -----------------------------
_rate_state: Dict[str, list[float]] = {}

def rate_limit_ok(ip: str) -> bool:
    now = time.time()
    window_start = now - 60
    hits = _rate_state.get(ip, [])
    hits = [t for t in hits if t >= window_start]
    if len(hits) >= RATE_LIMIT_PER_MINUTE:
        _rate_state[ip] = hits
        return False
    hits.append(now)
    _rate_state[ip] = hits
    return True

# -----------------------------
# Helpers: validation
# -----------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def normalize_email(email: str) -> str:
    email = (email or "").strip()
    if not email or not EMAIL_RE.match(email):
        raise ValueError("Email non valida")
    return email.lower()

def validate_and_format_phone(phone: str, region: str = PHONE_DEFAULT_REGION) -> str:
    """
    Valida telefono con phonenumbers e restituisce formato E.164.
    Accetta: '3331112222', '+393331112222', '0039333...' ecc.
    """
    raw = (phone or "").strip()
    if not raw:
        raise ValueError("Telefono mancante")

    # Togli spazi e caratteri comuni
    raw = re.sub(r"[^\d\+]", "", raw)

    try:
        parsed = phonenumbers.parse(raw, region)
    except NumberParseException:
        raise ValueError("Telefono non parsabile")

    if not phonenumbers.is_valid_number(parsed):
        raise ValueError("Telefono non valido")

    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)

def parse_date_flex(value: str) -> date:
    """
    Accetta:
      - '2026-02-10'
      - '10/02/2026'
      - '10-02-2026'
      - 'domani' / 'oggi'
    """
    v = (value or "").strip().lower()
    if not v:
        raise ValueError("Data mancante")

    today = date.today()
    if v in ("oggi", "today"):
        return today
    if v in ("domani", "tomorrow"):
        return today + timedelta(days=1)

    # ISO
    try:
        return datetime.strptime(v, "%Y-%m-%d").date()
    except ValueError:
        pass

    # dd/mm/yyyy
    try:
        return datetime.strptime(v, "%d/%m/%Y").date()
    except ValueError:
        pass

    # dd-mm-yyyy
    try:
        return datetime.strptime(v, "%d-%m-%Y").date()
    except ValueError:
        pass

    raise ValueError("Formato data non riconosciuto")

def parse_time_hhmm(value: str) -> str:
    """
    Accetta:
      - '13:15'
      - '13.15'
      - '1315'
    Ritorna 'HH:MM'
    """
    v = (value or "").strip()
    if not v:
        raise ValueError("Orario mancante")

    v = v.replace(".", ":")
    if re.fullmatch(r"\d{4}", v):
        v = v[:2] + ":" + v[2:]
    if not re.fullmatch(r"\d{2}:\d{2}", v):
        raise ValueError("Formato orario non valido (usa HH:MM)")

    hh, mm = v.split(":")
    h = int(hh)
    m = int(mm)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("Orario fuori range")

    return f"{h:02d}:{m:02d}"

def normalize_location(value: str) -> str:
    """
    Normalizza sede. Le etichette visibili in Fidy sembrano:
      Appia, Ostia Lido, Palermo, Reggio Calabria, Talenti - Roma
    """
    v = (value or "").strip().lower()
    if not v:
        raise ValueError("Sede mancante")

    mapping = {
        "appia": "Appia",
        "ostia": "Ostia Lido",
        "ostia lido": "Ostia Lido",
        "ostia - lido": "Ostia Lido",
        "palermo": "Palermo",
        "reggio": "Reggio Calabria",
        "reggio calabria": "Reggio Calabria",
        "talenti": "Talenti - Roma",
        "talenti roma": "Talenti - Roma",
        "talenti - roma": "Talenti - Roma",
        "roma talenti": "Talenti - Roma",
    }

    # match diretto
    if v in mapping:
        return mapping[v]

    # match "contains"
    for k, out in mapping.items():
        if k in v:
            return out

    # fallback: title case
    return value.strip()

def infer_meal_type(dt: date, hhmm: str, explicit: Optional[str]) -> str:
    """
    Tipologia: PRANZO / CENA
    Se esplicita, la usa. Altrimenti inferisce (prima delle 17 = PRANZO).
    """
    if explicit:
        e = explicit.strip().upper()
        if e in ("PRANZO", "CENA"):
            return e
    hh = int(hhmm.split(":")[0])
    return "PRANZO" if hh < 17 else "CENA"

def safe_note(note: Optional[str]) -> str:
    """
    Nota max 300 char, pulizia base.
    """
    if not note:
        return ""
    n = note.strip()
    n = re.sub(r"\s+", " ", n)
    return n[:300]

# -----------------------------
# Request model (flessibile)
# -----------------------------
class BookingRequest(BaseModel):
    # campi minimi
    nome: str = Field(..., description="Nome cliente")
    cognome: str = Field(..., description="Cognome cliente")
    email: str = Field(..., description="Email cliente")
    telefono: str = Field(..., description="Telefono cliente (anche senza prefisso)")
    persone: int = Field(..., ge=1, le=9, description="Numero ospiti (1..9). >9 gestisci via centralino")
    sede: str = Field(..., description="Sede: Talenti, Ostia Lido, Appia, Palermo, Reggio Calabria, ecc.")
    data: str = Field(..., description="Data: YYYY-MM-DD oppure 'domani'/'oggi'")
    ora: str = Field(..., description="Orario: HH:MM")

    # opzionali
    seggiolone: Optional[bool] = Field(default=False, description="Se serve seggiolone")
    seggiolini: Optional[int] = Field(default=0, ge=0, le=5, description="Numero seggiolini (0..5)")
    tipologia: Optional[str] = Field(default=None, description="PRANZO o CENA (opzionale)")
    nota: Optional[str] = Field(default="", description="Nota breve")
    referer: Optional[str] = Field(default=None, description="Tracking referer (AI, butt, sito...)")
    dry_run: Optional[bool] = Field(default=None, description="Override DRY_RUN env")
    # per debug/trace
    request_id: Optional[str] = Field(default=None)

    @validator("nome", "cognome")
    def _name_not_empty(cls, v: str) -> str:
        v = (v or "").strip()
        if len(v) < 2:
            raise ValueError("Nome/Cognome troppo corto")
        return v

    @validator("persone")
    def _persone_limit(cls, v: int) -> int:
        # Il sito gestisce 1..9; oltre chiama centralino
        if v > 9:
            raise ValueError("Per più di 9 persone contattare il centralino")
        return v


# -----------------------------
# Playwright lifecycle
# -----------------------------
pw = None
browser: Optional[Browser] = None

async def startup_playwright():
    global pw, browser
    pw = await async_playwright().start()
    # args importanti per container
    browser = await pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    logger.info("Playwright avviato (Chromium headless).")

async def shutdown_playwright():
    global pw, browser
    try:
        if browser:
            await browser.close()
    finally:
        if pw:
            await pw.stop()
    logger.info("Playwright chiuso.")

# -----------------------------
# Core automation
# -----------------------------
def build_fidy_url(referer: str) -> str:
    # es: https://rione.fidy.app/prenew.php?referer=AI
    r = (referer or DEFAULT_REFERER).strip()
    if "?" in FIDY_BASE_URL:
        return f"{FIDY_BASE_URL}&referer={r}"
    return f"{FIDY_BASE_URL}?referer={r}"

async def with_page() -> Page:
    if not browser:
        raise RuntimeError("Browser non inizializzato")
    context = await browser.new_context()
    page = await context.new_page()
    page.set_default_navigation_timeout(PW_NAV_TIMEOUT_MS)
    page.set_default_timeout(PW_ACTION_TIMEOUT_MS)
    return page

async def close_page(page: Page):
    try:
        await page.context.close()
    except Exception:
        pass

async def click_span_by_rel(page: Page, selector: str, rel_value: str):
    """
    Clicca <span ... rel="X">, robusto.
    """
    el = page.locator(f'{selector}[rel="{rel_value}"]')
    await el.first.wait_for(state="visible")
    await el.first.click()

async def click_span_by_text(page: Page, selector: str, text: str):
    el = page.locator(selector, has_text=text)
    await el.first.wait_for(state="visible")
    await el.first.click()

async def run_booking_flow(req: BookingRequest) -> Dict[str, Any]:
    """
    Automazione step-by-step su prenew.php
    Ritorna dettagli + eventuale screenshot base64? (qui no, solo log)
    """
    # Normalizzazioni & validazioni forti
    email = normalize_email(req.email)
    phone_e164 = validate_and_format_phone(req.telefono, PHONE_DEFAULT_REGION)
    d = parse_date_flex(req.data)
    hhmm = parse_time_hhmm(req.ora)
    sede = normalize_location(req.sede)
    tipologia = infer_meal_type(d, hhmm, req.tipologia)
    nota = safe_note(req.nota)
    referer = (req.referer or DEFAULT_REFERER).strip() or DEFAULT_REFERER

    # seggiolini coerenti
    seggiolone = bool(req.seggiolone) or (req.seggiolini or 0) > 0
    seggiolini = int(req.seggiolini or 0)
    if not seggiolone:
        seggiolini = 0
    if seggiolone and seggiolini == 0:
        # se ha detto seggiolone=True ma non numero, default 1
        seggiolini = 1

    url = build_fidy_url(referer)

    # DRY RUN (richiesta) o ENV
    if req.dry_run is None:
        dry_run = DRY_RUN_DEFAULT != "0"
    else:
        dry_run = bool(req.dry_run)

    rid = req.request_id or str(uuid.uuid4())[:8]

    logger.info(
        f"🚀 BOOKING {rid}: {req.nome} {req.cognome} -> {sede} | {d.isoformat()} {hhmm} | "
        f"{req.persone} pax | tipologia={tipologia} | seggiolini={seggiolini} | referer={referer} | dry_run={dry_run}"
    )

    page = await with_page()
    try:
        # STEP 0: apri pagina
        await page.goto(url, wait_until="domcontentloaded")

        # Attendi UI (logo/step)
        # La pagina mostra introCont e poi stepCont dopo 1.5s: aspettiamo stepCont visibile
        await page.locator(".stepCont").wait_for(state="visible")

        # STEP 1: persone
        await click_span_by_rel(page, ".nCoperti", str(req.persone))

        # STEP 1b: seggiolini (se serve)
        if seggiolini > 0:
            # clic SI
            await page.locator(".seggioliniTxt").wait_for(state="visible")
            await page.locator(".seggioliniTxt").click()
            await click_span_by_rel(page, ".nSeggiolini", str(seggiolini))
        else:
            # lascia default NO (già selezionato)
            pass

        # STEP 2: data
        # Se data = oggi/domani presenti, altrimenti input date
        iso = d.isoformat()
        data_btn = page.locator(f'.dataBtn[rel="{iso}"]')
        if await data_btn.count() > 0:
            await data_btn.first.click()
        else:
            # Usa input date
            inp = page.locator("#DataPren")
            await inp.evaluate("el => el.value = ''")  # pulisci
            await inp.fill(iso)
            await inp.dispatch_event("change")

        # STEP 3: pranzo/cena
        await click_span_by_rel(page, ".tipoBtn", tipologia)

        # STEP 4: selezione sede (caricata via .ristoCont load)
        # La lista sedi sta in ristoCont; attendiamo che compaia almeno un item cliccabile
        await page.locator(".ristoCont").wait_for(state="visible")
        # Qui non conosciamo l'HTML di prenew_rist.php, ma dallo screenshot è una lista con testo della sede.
        # Tentiamo click per testo:
        await click_span_by_text(page, ".ristoCont *", sede)

        # STEP 5: selezione orario
        # L'elemento select è #OraPren. Aspettiamo che abbia option utili.
        await page.locator("#OraPren").wait_for(state="visible")

        # Attendi che le options si popolino (diverse da placeholder)
        async def options_ready() -> bool:
            opts = await page.locator("#OraPren option").count()
            return opts >= 2

        for _ in range(40):
            if await options_ready():
                break
            await asyncio.sleep(0.25)

        # Seleziona ora (il value può essere "12:30" etc)
        try:
            await page.select_option("#OraPren", hhmm)
        except Exception:
            # fallback: prova a cercare option con text che contiene hhmm
            opt = page.locator("#OraPren option", has_text=hhmm)
            if await opt.count() == 0:
                raise HTTPException(status_code=409, detail=f"Orario {hhmm} non disponibile")
            value = await opt.first.get_attribute("value")
            if not value:
                raise HTTPException(status_code=409, detail=f"Orario {hhmm} non selezionabile")
            await page.select_option("#OraPren", value)

        # Nota
        if nota:
            await page.fill("#Nota", nota)

        # Conferma dati (porta alla form Nome/Cognome/Email/Telefono)
        await page.locator(".confDati").click()
        await page.locator("#Nome").wait_for(state="visible")

        # Compila dati finali
        await page.fill("#Nome", req.nome)
        await page.fill("#Cognome", req.cognome)
        await page.fill("#Email", email)

        # Il form accetta 10 cifre IT senza prefisso (+39). Per coerenza:
        # estraiamo nazionali se IT, altrimenti mettiamo solo digits e max 10 (per non rompere form).
        digits = re.sub(r"\D", "", phone_e164)
        if phone_e164.startswith("+39") and len(digits) >= 12:
            # +39 + 10 cifre
            phone_form = digits[-10:]
        else:
            phone_form = digits[-10:] if len(digits) >= 10 else digits

        if len(phone_form) < 8:
            raise HTTPException(status_code=422, detail="Telefono troppo corto per il form")

        await page.fill("#Telefono", phone_form)

        if dry_run:
            logger.info(f"🧪 DRY RUN {rid}: compilazione completata, NON invio la prenotazione.")
            return {
                "ok": True,
                "dry_run": True,
                "request_id": rid,
                "sede": sede,
                "data": iso,
                "ora": hhmm,
                "tipologia": tipologia,
                "persone": req.persone,
                "seggiolini": seggiolini,
                "referer": referer,
                "telefono_e164": phone_e164,
                "telefono_form": phone_form,
            }

        # Invio (submit form)
        # Il submit viene intercettato da jQuery e manda ajax.php.
        await page.locator('input[type="submit"][value="PRENOTA"]').click()

        # Attendi risultato: la pagina carica prenew_res.php dentro .stepCont.
        # Cerchiamo un segnale generico: cambiamento contenuto / presenza di testo "OK" o simili
        # Qui: aspettiamo che spariscano i campi o compaia qualcosa di diverso.
        for _ in range(60):
            if await page.locator("#Nome").count() == 0:
                break
            await asyncio.sleep(0.25)

        logger.info(f"✅ BOOKED {rid}: prenotazione inviata su Fidy.")

        return {
            "ok": True,
            "dry_run": False,
            "request_id": rid,
            "sede": sede,
            "data": iso,
            "ora": hhmm,
            "tipologia": tipologia,
            "persone": req.persone,
            "seggiolini": seggiolini,
            "referer": referer,
            "telefono_e164": phone_e164,
            "telefono_form": phone_form,
        }

    finally:
        await close_page(page)

async def book_with_retries(req: BookingRequest) -> Dict[str, Any]:
    """
    Retry su timeout/instabilità. Se fallisce restituisce errore leggibile.
    """
    last_err: Optional[str] = None

    for attempt in range(PW_RETRIES + 1):
        try:
            if attempt > 0:
                logger.warning(f"🔁 Retry attempt {attempt}/{PW_RETRIES}")
            return await run_booking_flow(req)

        except PWTimeoutError as e:
            last_err = f"Timeout Playwright: {str(e)}"
            logger.error(last_err)
        except HTTPException:
            raise
        except Exception as e:
            last_err = f"Errore booking: {type(e).__name__}: {str(e)}"
            logger.error(last_err)

        # backoff breve
        await asyncio.sleep(0.8 + attempt * 0.6)

    raise HTTPException(status_code=502, detail=last_err or "Errore sconosciuto")

# -----------------------------
# FastAPI app
# -----------------------------
app = FastAPI(title="centralino-webhook", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOWED_ORIGINS if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def _startup():
    await startup_playwright()

@app.on_event("shutdown")
async def _shutdown():
    await shutdown_playwright()

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/book_table")
async def book_table(req: Request):
    ip = req.client.host if req.client else "unknown"
    if not rate_limit_ok(ip):
        raise HTTPException(status_code=429, detail="Troppe richieste, riprova tra poco")

    payload = await req.json()

    # Supporto payload "liberi": se arrivano nomi diversi li rimappiamo.
    # (così il chatbot può mandare dati anche con chiavi differenti)
    def pick(*keys: str) -> Optional[Any]:
        for k in keys:
            if k in payload and payload[k] is not None:
                return payload[k]
        return None

    normalized = {
        "nome": pick("nome", "Nome", "first_name", "firstname"),
        "cognome": pick("cognome", "Cognome", "last_name", "lastname"),
        "email": pick("email", "Email", "mail"),
        "telefono": pick("telefono", "Telefono", "phone", "cell", "cellulare"),
        "persone": pick("persone", "Persone", "pax", "coperti", "Coperti"),
        "sede": pick("sede", "Sede", "ristorante", "Ristorante", "location"),
        "data": pick("data", "Data", "data_pren", "DataPren", "DataPren2"),
        "ora": pick("ora", "Ora", "orario", "OraPren", "OraPren2"),
        "seggiolone": pick("seggiolone", "Seggiolone"),
        "seggiolini": pick("seggiolini", "Seggiolini"),
        "tipologia": pick("tipologia", "Tipologia", "pasto", "Pasto"),
        "nota": pick("nota", "Nota", "note", "Note"),
        "referer": pick("referer", "Fonte", "fonte"),
        "dry_run": pick("dry_run", "dryRun"),
        "request_id": pick("request_id", "requestId"),
    }

    # valori di default
    if normalized["referer"] is None:
        normalized["referer"] = DEFAULT_REFERER

    try:
        booking_req = BookingRequest(**normalized)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Payload non valido: {str(e)}")

    result = await book_with_retries(booking_req)
    return result
