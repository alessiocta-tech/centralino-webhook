import os
import re
import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Optional, Literal, Dict, Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, EmailStr, field_validator
import phonenumbers
from phonenumbers.phonenumberutil import NumberParseException

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


# -------------------------
# CONFIG
# -------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("centralino-webhook")

BOOKING_URL = os.getenv("BOOKING_URL", "https://rione.fidy.app/prenew.php?referer=AI")
DEFAULT_REGION = os.getenv("PHONE_DEFAULT_REGION", "IT")
MAX_DAYS_AHEAD = int(os.getenv("MAX_DAYS_AHEAD", "30"))
PLAYWRIGHT_HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() == "true"

# Timeout Playwright (ms)
NAV_TIMEOUT = int(os.getenv("NAV_TIMEOUT_MS", "25000"))
ACTION_TIMEOUT = int(os.getenv("ACTION_TIMEOUT_MS", "15000"))

# Retry
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "0.6"))

# Worker-safe: ogni processo uvicorn avrà il suo browser singleton
_pw = None
_browser: Optional[Browser] = None
_context: Optional[BrowserContext] = None


# -------------------------
# HELPERS
# -------------------------
class TransferRequired(Exception):
    def __init__(self, reason: str, phone: Optional[str] = None):
        super().__init__(reason)
        self.reason = reason
        self.phone = phone


def normalize_time_to_hhmm(raw: str) -> str:
    """
    Accetta:
    - "13" -> "13:00"
    - "ore 13" -> "13:00"
    - "13:5" -> "13:05"
    - "8" -> "08:00"
    - "20.30" -> "20:30"
    - "20,30" -> "20:30"
    """
    if raw is None:
        raise ValueError("Ora mancante")

    s = str(raw).strip().lower()
    s = s.replace(".", ":").replace(",", ":")

    s = re.sub(r"\bore\b", "", s).strip()
    s = re.sub(r"\s+", "", s)

    # Solo ore (es. "13" o "8")
    if re.fullmatch(r"\d{1,2}", s):
        h = int(s)
        if not (0 <= h <= 23):
            raise ValueError("Ora non valida")
        return f"{h:02d}:00"

    # hh:mm
    m = re.fullmatch(r"(\d{1,2}):(\d{1,2})", s)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2))
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            raise ValueError("Ora non valida")
        return f"{h:02d}:{mi:02d}"

    raise ValueError("Formato ora non riconosciuto")


def validate_and_format_phone_it(raw: str) -> str:
    """
    Valida telefono con phonenumbers.
    Output: numero nazionale SOLO cifre, usabile nel form (tipicamente 10 cifre per mobile IT).
    """
    if not raw:
        raise ValueError("Telefono mancante")

    s = re.sub(r"[^\d+]", "", str(raw).strip())

    try:
        pn = phonenumbers.parse(s, DEFAULT_REGION)
    except NumberParseException:
        raise ValueError("Telefono non valido")

    if not phonenumbers.is_valid_number(pn):
        raise ValueError("Telefono non valido")

    if phonenumbers.region_code_for_number(pn) != "IT":
        # se vuoi accettare esteri, qui puoi cambiare logica
        raise ValueError("Telefono non italiano")

    national = str(pn.national_number)

    # In pratica, per booking via form (maxlength=10) conviene standardizzare a 10 cifre mobile
    # Se vuoi gestire anche fissi (lunghezza variabile), serve aggiornare form lato sito.
    if len(national) != 10:
        raise ValueError("Telefono italiano non nel formato atteso (10 cifre)")

    if not national.isdigit():
        raise ValueError("Telefono non valido")

    return national


def validate_date_window(d: date) -> None:
    today = date.today()
    if d < today:
        raise TransferRequired("Non è possibile prenotare per una data passata.")
    if d > today + timedelta(days=MAX_DAYS_AHEAD):
        raise TransferRequired(
            f"Per prenotazioni oltre {MAX_DAYS_AHEAD} giorni è necessario parlare con un operatore."
        )


def sede_to_label(sede: str) -> str:
    """
    Mappa input sede -> testo presente nella UI
    """
    s = sede.strip().lower()
    mapping = {
        "talenti": "Talenti - Roma",
        "roma talenti": "Talenti - Roma",
        "appia": "Appia",
        "ostia": "Ostia Lido",
        "ostia lido": "Ostia Lido",
        "reggio": "Reggio Calabria",
        "reggio calabria": "Reggio Calabria",
        "palermo": "Palermo",
        "palermo centro": "Palermo",
    }
    return mapping.get(s, sede)


async def retry_async(fn, *args, **kwargs):
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning(f"Retry {attempt+1}/{MAX_RETRIES+1} after error: {e}. Waiting {delay:.2f}s")
            await asyncio.sleep(delay)
    raise last_exc


async def ensure_browser():
    global _pw, _browser, _context
    if _pw is None:
        _pw = await async_playwright().start()

    if _browser is None:
        _browser = await _pw.chromium.launch(
            headless=PLAYWRIGHT_HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox",
            ],
        )

    if _context is None:
        _context = await _browser.new_context(
            viewport={"width": 1280, "height": 720},
            locale="it-IT",
        )
    return _context


async def close_browser():
    global _pw, _browser, _context
    try:
        if _context is not None:
            await _context.close()
    except Exception:
        pass
    _context = None
    try:
        if _browser is not None:
            await _browser.close()
    except Exception:
        pass
    _browser = None
    try:
        if _pw is not None:
            await _pw.stop()
    except Exception:
        pass
    _pw = None


# -------------------------
# MODELS
# -------------------------
class CheckAvailabilityRequest(BaseModel):
    sede: str
    data: str  # YYYY-MM-DD
    ora: Optional[str] = None
    persone: int = Field(ge=1, le=9)


class BookTableRequest(BaseModel):
    nome: str
    cognome: str
    email: EmailStr
    telefono: str
    persone: int = Field(ge=1, le=20)  # validiamo poi: >9 -> transfer
    sede: str
    data: str  # YYYY-MM-DD
    ora: str
    seggiolone: bool = False
    seggiolini: int = Field(default=0, ge=0, le=5)
    nota: str = ""
    referer: str = "AI"
    dry_run: bool = False

    @field_validator("nome", "cognome")
    @classmethod
    def name_not_empty(cls, v: str):
        v = (v or "").strip()
        if len(v) < 2:
            raise ValueError("Nome/Cognome non valido")
        return v

    @field_validator("ora")
    @classmethod
    def normalize_time(cls, v: str):
        return normalize_time_to_hhmm(v)

    @field_validator("telefono")
    @classmethod
    def normalize_phone(cls, v: str):
        return validate_and_format_phone_it(v)

    @field_validator("data")
    @classmethod
    def validate_date_str(cls, v: str):
        try:
            d = datetime.strptime(v, "%Y-%m-%d").date()
        except Exception:
            raise ValueError("Data non valida (usa YYYY-MM-DD)")
        validate_date_window(d)
        return v


# -------------------------
# FASTAPI APP (lifespan)
# -------------------------
app = FastAPI(title="centralino-webhook", version="1.0.0")


@app.on_event("startup")
async def _deprecated_startup_notice():
    # lasciato vuoto per compatibilità, ma usiamo lifespan-like logic sotto
    pass


@app.on_event("shutdown")
async def _deprecated_shutdown_notice():
    pass


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/checkavailability")
async def checkavailability(req: CheckAvailabilityRequest):
    """
    Endpoint opzionale: al momento restituiamo ok e rimandiamo la verifica reale al booking step.
    Se vuoi, lo possiamo arricchire interrogando il sito e leggendo orari disponibili.
    """
    try:
        d = datetime.strptime(req.data, "%Y-%m-%d").date()
        validate_date_window(d)
    except TransferRequired as tr:
        return JSONResponse(
            status_code=200,
            content={"available": False, "transfer_required": True, "reason": tr.reason},
        )
    except Exception as e:
        return JSONResponse(
            status_code=200,
            content={"available": False, "error": "invalid_request", "detail": str(e)},
        )

    if req.persone > 9:
        return JSONResponse(
            status_code=200,
            content={
                "available": False,
                "transfer_required": True,
                "reason": "Per gruppi superiori a 9 persone è necessario parlare con un operatore.",
            },
        )

    return {"available": True}


# -------------------------
# PLAYWRIGHT BOOKING FLOW
# -------------------------
async def perform_booking(payload: BookTableRequest) -> Dict[str, Any]:
    """
    Automazione booking su rione.fidy.app
    """
    if payload.persone > 9:
        raise TransferRequired("Per gruppi superiori a 9 persone è necessario parlare con un operatore.")

    # Tipologia PRANZO/CENA in base a ora (regola semplice)
    hh = int(payload.ora.split(":")[0])
    tipologia = "PRANZO" if 11 <= hh <= 16 else "CENA"

    context = await ensure_browser()
    page: Page = await context.new_page()
    page.set_default_navigation_timeout(NAV_TIMEOUT)
    page.set_default_timeout(ACTION_TIMEOUT)

    try:
        await page.goto(BOOKING_URL, wait_until="domcontentloaded")

        # Attendi che compaia la stepCont (dopo intro fade)
        await page.wait_for_selector(".stepCont", state="visible")

        # 1) Coperti
        await page.click(f".nCoperti[rel='{payload.persone}']")

        # seggiolini flow
        if payload.seggiolini > 0:
            await page.click(".seggioliniTxt")
            await page.wait_for_selector(".seggioliniCont", state="visible")
            await page.click(f".nSeggiolini[rel='{payload.seggiolini}']")
        else:
            # default NO già selezionato lato UI; se vuoi forzare:
            pass

        # 2) Data (YYYY-MM-DD)
        # Se è oggi/domani gestiamo clic rapido, altrimenti input date
        d = datetime.strptime(payload.data, "%Y-%m-%d").date()
        today = date.today()
        if d == today:
            await page.click(".dataOggi[rel]")
            # In pagina ci sono due pulsanti "Oggi" e "Domani" con class dataOggi
            # Seleziona quello con rel=oggi:
            await page.click(f".dataOggi[rel='{payload.data}']")
        elif d == today + timedelta(days=1):
            await page.click(f".dataOggi[rel='{payload.data}']")
        else:
            # altra data: setta value nell'input e trigger change
            await page.evaluate(
                """(val) => {
                    const i = document.querySelector('#DataPren');
                    i.value = val;
                    i.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                payload.data,
            )

        # 3) Tipologia
        await page.wait_for_selector(".tipoBtn", state="visible")
        await page.click(f".tipoBtn[rel='{tipologia}']")

        # 4) Selezione ristorante: si carica dinamicamente dentro .ristoCont
        await page.wait_for_selector(".ristoCont", state="visible")
        label = sede_to_label(payload.sede)

        # Click sul ristorante che contiene il testo label
        # (prenew_rist.php genera card/clickable; usiamo text locator robusto)
        await page.get_by_text(label, exact=False).click()

        # 5) Orario: select #OraPren con option value HH:MM
        await page.wait_for_selector("#OraPren", state="visible")
        # attendi che le opzioni vengano popolate
        await page.wait_for_function(
            """() => {
                const s = document.querySelector('#OraPren');
                return s && s.options && s.options.length > 1;
            }"""
        )

        # Se l'opzione non esiste, falliamo con messaggio chiaro
        exists = await page.evaluate(
            """(t) => {
                const s = document.querySelector('#OraPren');
                return Array.from(s.options).some(o => (o.value || '').startsWith(t));
            }""",
            payload.ora,
        )
        if not exists:
            raise HTTPException(status_code=200, detail="Orario non disponibile per la sede/data selezionata")

        await page.select_option("#OraPren", value=re.compile(f"^{re.escape(payload.ora)}"))

        # Nota
        if payload.nota:
            await page.fill("#Nota", payload.nota)

        # CONFERMA (step dati)
        await page.click(".confDati")

        # Compila dati
        await page.wait_for_selector("#Nome", state="visible")
        await page.fill("#Nome", payload.nome)
        await page.fill("#Cognome", payload.cognome)
        await page.fill("#Email", str(payload.email))
        await page.fill("#Telefono", payload.telefono)

        if payload.dry_run:
            return {
                "status": "DRY_RUN_OK",
                "message": "Validazione completata. (dry_run=true)",
                "normalized": {
                    "ora": payload.ora,
                    "telefono": payload.telefono,
                    "tipologia": tipologia,
                    "referer": "AI",
                },
            }

        # Submit
        await page.click("input[type='submit'][value='PRENOTA']")

        # Attendi che la pagina cambi / carichi risultato (prenew_res.php)
        await page.wait_for_timeout(1200)

        # Euristica success: la UI sostituisce .stepCont con pagina di risultato
        # Se non troviamo errori JS e la pagina resta stabile, consideriamo OK.
        content = await page.content()
        if "Si è verificata" in content or "errore" in content.lower():
            raise HTTPException(status_code=200, detail="Errore durante la conferma prenotazione")

        return {
            "status": "OK",
            "message": "Prenotazione inviata correttamente.",
            "normalized": {
                "ora": payload.ora,
                "telefono": payload.telefono,
                "tipologia": tipologia,
                "referer": "AI",
            },
        }

    finally:
        try:
            await page.close()
        except Exception:
            pass


@app.post("/book_table")
async def book_table(req: BookTableRequest):
    """
    Endpoint usato come tool (ElevenLabs / agent).
    Restituisce sempre 200 con payload strutturato:
    - status OK / DRY_RUN_OK
    - transfer_required True quando serve operatore (date >30, date passate, persone>9)
    """
    # forza referer AI sempre
    req.referer = "AI"

    try:
        # Validazioni extra (oltre Pydantic)
        d = datetime.strptime(req.data, "%Y-%m-%d").date()
        validate_date_window(d)

        if req.persone > 9:
            raise TransferRequired("Per gruppi superiori a 9 persone è necessario parlare con un operatore.")

        # Booking con retry (stabilità anti-502)
        result = await retry_async(perform_booking, req)

        # Messaggio post-prenotazione “operativo” (WhatsApp + email + puntualità)
        result["customer_notice"] = (
            "Riceverai una conferma via WhatsApp e via email ai contatti indicati. "
            "Ti consigliamo di rispettare l’orario scelto: in caso di ritardo prolungato "
            "potrebbe non essere possibile garantire il tavolo."
        )
        return result

    except TransferRequired as tr:
        return JSONResponse(
            status_code=200,
            content={
                "status": "TRANSFER_REQUIRED",
                "transfer_required": True,
                "reason": tr.reason,
            },
        )
    except HTTPException as he:
        # Manteniamo 200 per compatibilità tool (evitiamo 502 “a cascata”)
        return JSONResponse(
            status_code=200,
            content={
                "status": "ERROR",
                "transfer_required": False,
                "reason": str(he.detail),
            },
        )
    except Exception as e:
        logger.exception(f"Unhandled error in /book_table: {e}")
        return JSONResponse(
            status_code=200,
            content={
                "status": "ERROR",
                "transfer_required": False,
                "reason": "Si è verificata una difficoltà nel confermare la prenotazione. Riprova tra poco.",
            },
        )
