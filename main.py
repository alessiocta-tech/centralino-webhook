import os
import re
import asyncio
from datetime import date, datetime, timedelta
from typing import Optional, Literal, Any, Dict, List

import phonenumbers
from phonenumbers.phonenumberutil import NumberParseException
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from playwright.async_api import async_playwright, Browser, Playwright, TimeoutError as PWTimeoutError


# ----------------------------
# CONFIG
# ----------------------------
DEFAULT_TZ = "Europe/Rome"
MAX_DAYS_AHEAD = 30
MAX_PEOPLE_AUTOMATION = 9

# Link prenotazione: aggiunge metadato referer=AI
BOOKING_URL = "https://rione.fidy.app/prenew.php?referer=AI"

# Numero operatore per gruppi / richieste speciali (metti quello reale)
OPERATOR_PHONE = os.getenv("OPERATOR_PHONE", "+390656556263")

# Playwright tuning
PW_NAV_TIMEOUT_MS = int(os.getenv("PW_NAV_TIMEOUT_MS", "12000"))     # navigazione
PW_STEP_TIMEOUT_MS = int(os.getenv("PW_STEP_TIMEOUT_MS", "9000"))    # step/selector
PW_TOTAL_TIMEOUT_SEC = int(os.getenv("PW_TOTAL_TIMEOUT_SEC", "25"))  # hard cap per chiamata tool

# Concorrenza: evita di aprire 3 prenotazioni insieme e impallare
BOOKING_CONCURRENCY = int(os.getenv("BOOKING_CONCURRENCY", "1"))
_booking_sem = asyncio.Semaphore(BOOKING_CONCURRENCY)

# Retry su errori transient (1 retry = sufficiente)
MAX_RETRY = int(os.getenv("MAX_RETRY", "1"))

# Globals (browser persistente)
_pw: Optional[Playwright] = None
_browser: Optional[Browser] = None


# ----------------------------
# UTILS
# ----------------------------
def _today_rome() -> date:
    # Evitiamo dipendenze extra: assumiamo server in CET/UTC e lavoriamo a giorni.
    # Per regole "passato / 30 giorni" basta confrontare date ISO che arrivano dal bot.
    return date.today()


def normalize_time_to_hhmm(raw: str) -> str:
    """
    Accetta: '13', 'ore 13', '13:00', '8', '08', '8:30', 'ore 20.15'
    Ritorna sempre HH:MM oppure alza ValueError.
    """
    s = (raw or "").strip().lower()
    s = s.replace(".", ":")
    s = re.sub(r"\bore\b", "", s).strip()

    # Match "H" or "HH"
    m1 = re.fullmatch(r"(\d{1,2})", s)
    if m1:
        h = int(m1.group(1))
        if 0 <= h <= 23:
            return f"{h:02d}:00"
        raise ValueError("Orario non valido")

    # Match "H:MM" or "HH:MM"
    m2 = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if m2:
        h = int(m2.group(1))
        mm = int(m2.group(2))
        if 0 <= h <= 23 and 0 <= mm <= 59:
            return f"{h:02d}:{mm:02d}"
        raise ValueError("Orario non valido")

    raise ValueError("Orario non valido")


def validate_and_format_phone(phone: str, default_region: str = "IT") -> str:
    """
    Valida e normalizza in E.164 se possibile.
    Accetta numeri italiani con o senza +39.
    """
    p = (phone or "").strip()
    if not p:
        raise ValueError("Telefono mancante")

    # Togli spazi e separatori
    p_clean = re.sub(r"[^\d+]", "", p)

    try:
        num = phonenumbers.parse(p_clean, default_region)
    except NumberParseException:
        raise ValueError("Numero di telefono non valido")

    if not phonenumbers.is_valid_number(num):
        raise ValueError("Numero di telefono non valido")

    # E.164 (+39347...)
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)


def enforce_date_rules(d: date, people: int) -> None:
    today = _today_rome()

    if d < today:
        raise ValueError("Non è possibile prenotare per date passate.")

    if d > today + timedelta(days=MAX_DAYS_AHEAD):
        # Non automatizziamo oltre 30 giorni
        raise ValueError(
            f"Per prenotazioni oltre {MAX_DAYS_AHEAD} giorni è necessario contattare un operatore."
        )

    if people > MAX_PEOPLE_AUTOMATION:
        raise ValueError("Per tavoli superiori a 9 persone è necessario contattare un operatore.")


def _map_sede_label(sede: str) -> str:
    """
    Normalizza sede: accetta varianti.
    """
    s = (sede or "").strip().lower()
    m = {
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
    if s in m:
        return m[s]
    # fallback: tenta titolo
    return sede.strip()


def _tipo_from_time_or_label(tipologia: str) -> str:
    """
    Tipologia deve essere PRANZO o CENA.
    """
    t = (tipologia or "").strip().upper()
    if t in ("PRANZO", "CENA"):
        return t
    raise ValueError("Tipologia deve essere PRANZO o CENA")


def confirmation_text() -> str:
    return (
        "Riceverai la conferma della prenotazione via WhatsApp e via email. "
        "È importante rispettare l’orario scelto: in caso di ritardo prolungato "
        "il tavolo potrebbe non essere più garantito."
    )


# ----------------------------
# MODELS (Pydantic v2)
# ----------------------------
class BookTableRequest(BaseModel):
    nome: str = Field(..., min_length=1)
    cognome: str = Field(..., min_length=1)
    email: EmailStr
    telefono: str
    persone: int = Field(..., ge=1, le=50)
    sede: str
    data: str  # YYYY-MM-DD (già risolta dall’agente)
    ora: str   # HH:MM o forme convertibili
    seggiolone: bool = False
    seggiolini: int = Field(0, ge=0, le=5)
    nota: str = ""
    referer: str = "AI"
    dry_run: bool = False

    # Normalizza ora
    @field_validator("ora")
    @classmethod
    def _v_ora(cls, v: str) -> str:
        try:
            return normalize_time_to_hhmm(v)
        except ValueError:
            raise ValueError("Ora non valida. Usa HH:MM (es. 13:00) o un orario chiaro (es. 13).")

    # Normalizza sede
    @field_validator("sede")
    @classmethod
    def _v_sede(cls, v: str) -> str:
        vv = (v or "").strip()
        if not vv:
            raise ValueError("Sede mancante")
        return _map_sede_label(vv)

    # Telefono valido
    @field_validator("telefono")
    @classmethod
    def _v_tel(cls, v: str) -> str:
        try:
            return validate_and_format_phone(v)
        except ValueError as e:
            raise ValueError(str(e))

    @field_validator("data")
    @classmethod
    def _v_data(cls, v: str) -> str:
        vv = (v or "").strip()
        try:
            datetime.strptime(vv, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Data non valida. Usa il formato YYYY-MM-DD.")
        return vv

    @model_validator(mode="after")
    def _business_rules(self) -> "BookTableRequest":
        d = datetime.strptime(self.data, "%Y-%m-%d").date()
        enforce_date_rules(d, self.persone)
        if self.seggiolini == 0 and self.seggiolone:
            # seggiolone true senza seggiolini indicati: impostiamo 1
            self.seggiolini = 1
        if self.referer != "AI":
            # forziamo sempre AI in produzione per tracking
            self.referer = "AI"
        return self


class AvailabilityRequest(BaseModel):
    persone: int = Field(..., ge=1, le=9)
    sede: str
    data: str  # YYYY-MM-DD
    tipologia: Literal["PRANZO", "CENA"]

    @field_validator("sede")
    @classmethod
    def _v_sede(cls, v: str) -> str:
        vv = (v or "").strip()
        if not vv:
            raise ValueError("Sede mancante")
        return _map_sede_label(vv)

    @field_validator("data")
    @classmethod
    def _v_data(cls, v: str) -> str:
        vv = (v or "").strip()
        try:
            datetime.strptime(vv, "%Y-%m-%d")
        except ValueError:
            raise ValueError("Data non valida. Usa il formato YYYY-MM-DD.")
        d = datetime.strptime(vv, "%Y-%m-%d").date()
        enforce_date_rules(d, people=1)  # check date range; people check handled separately
        return vv


# ----------------------------
# FASTAPI (lifespan)
# ----------------------------
async def lifespan(app: FastAPI):
    global _pw, _browser
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(
        headless=True,
        args=[
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-gpu",
        ],
    )
    yield
    try:
        if _browser:
            await _browser.close()
    finally:
        _browser = None
        if _pw:
            await _pw.stop()
        _pw = None


app = FastAPI(title="centralino-webhook", version="1.0.0", lifespan=lifespan)


# ----------------------------
# PLAYWRIGHT CORE
# ----------------------------
async def _new_fast_context():
    """
    Crea contesto ottimizzato: blocca immagini/font/media per velocizzare.
    """
    if _browser is None:
        raise RuntimeError("Browser non inizializzato")

    context = await _browser.new_context(
        locale="it-IT",
        viewport={"width": 1280, "height": 800},
    )

    async def route_handler(route, request):
        if request.resource_type in ("image", "media", "font"):
            await route.abort()
            return
        await route.continue_()

    await context.route("**/*", route_handler)
    return context


async def _click_by_text(page, text: str, timeout_ms: int = PW_STEP_TIMEOUT_MS):
    loc = page.get_by_text(text, exact=False)
    await loc.first.click(timeout=timeout_ms)


async def _select_time_option(page, hhmm: str):
    """
    select#OraPren, value potrebbe essere "13:00" o "13:00:00".
    Proviamo match esatto e fallback.
    """
    # Primo tentativo: value == HH:MM
    try:
        await page.select_option("#OraPren", value=hhmm, timeout=PW_STEP_TIMEOUT_MS)
        return
    except Exception:
        pass

    # Fallback: trova opzioni che iniziano con HH:MM
    opts = await page.query_selector_all("#OraPren option")
    for o in opts:
        val = await o.get_attribute("value")
        if val and val.startswith(hhmm):
            await page.select_option("#OraPren", value=val, timeout=PW_STEP_TIMEOUT_MS)
            return

    raise ValueError("Orario non disponibile per i parametri scelti.")


async def _wait_ajax_ok(page):
    """
    Attende la risposta POST ajax.php e verifica che torni OK.
    """
    def is_target(resp):
        return ("ajax.php" in resp.url) and resp.request.method == "POST"

    async with page.expect_response(is_target, timeout=PW_NAV_TIMEOUT_MS) as resp_info:
        await page.locator('input[type="submit"][value="PRENOTA"]').click(timeout=PW_STEP_TIMEOUT_MS)

    resp = await resp_info.value
    if resp.status != 200:
        raise RuntimeError("Errore nella conferma prenotazione (HTTP non 200).")

    txt = (await resp.text()).strip()
    if txt != "OK":
        raise RuntimeError(txt or "Errore nella conferma prenotazione.")


async def _perform_booking(req: BookTableRequest) -> Dict[str, Any]:
    """
    Automazione completa prenotazione.
    """
    context = await _new_fast_context()
    page = await context.new_page()
    page.set_default_navigation_timeout(PW_NAV_TIMEOUT_MS)
    page.set_default_timeout(PW_STEP_TIMEOUT_MS)

    try:
        await page.goto(BOOKING_URL, wait_until="domcontentloaded")

        # Intro: a volte fa fadeIn; aspettiamo step1 visibile
        await page.wait_for_selector(".stepCont", state="visible", timeout=PW_NAV_TIMEOUT_MS)

        # STEP 1: coperti
        await page.locator(f'.nCoperti[rel="{req.persone}"]').click(timeout=PW_STEP_TIMEOUT_MS)

        # Seggiolini
        if req.seggiolini > 0:
            await page.locator(".seggioliniTxt").click(timeout=PW_STEP_TIMEOUT_MS)
            await page.locator(f'.nSeggiolini[rel="{req.seggiolini}"]').click(timeout=PW_STEP_TIMEOUT_MS)
        else:
            # Forza NO se presente
            if await page.locator(".SeggNO").count():
                await page.locator(".SeggNO").click(timeout=PW_STEP_TIMEOUT_MS)

        # STEP 2: data
        # Se data = oggi/domani usa i bottoni, altrimenti input date
        d = req.data
        today = _today_rome().strftime("%Y-%m-%d")
        tomorrow = (_today_rome() + timedelta(days=1)).strftime("%Y-%m-%d")

        if d == today:
            await page.locator('.dataOggi[rel="' + today + '"]').click(timeout=PW_STEP_TIMEOUT_MS)
        elif d == tomorrow:
            await page.locator('.dataOggi[rel="' + tomorrow + '"]').click(timeout=PW_STEP_TIMEOUT_MS)
        else:
            # input#DataPren + trigger change
            await page.locator("#DataPren").fill(d, timeout=PW_STEP_TIMEOUT_MS)
            await page.locator("#DataPren").dispatch_event("change")

        # STEP 3: pranzo/cena
        tipologia = _tipo_from_time_or_label("PRANZO")  # placeholder
        # In questo sistema la tipologia arriva implicitamente dall’agente, ma nel tool la riceviamo già?
        # Qui usiamo un euristico: se ora < 17:00 => PRANZO else CENA.
        h = int(req.ora.split(":")[0])
        tipologia = "PRANZO" if h < 17 else "CENA"
        await page.locator(f'.tipoBtn[rel="{tipologia}"]').click(timeout=PW_STEP_TIMEOUT_MS)

        # STEP 4: scelta ristorante
        # Aspetta che lista sedi compaia
        await page.wait_for_selector(".ristoCont", state="visible", timeout=PW_NAV_TIMEOUT_MS)

        # Click sulla riga che contiene il nome sede
        await _click_by_text(page, req.sede, timeout_ms=PW_STEP_TIMEOUT_MS)

        # STEP 5: orario
        await page.wait_for_selector("#OraPren", state="visible", timeout=PW_NAV_TIMEOUT_MS)
        await _select_time_option(page, req.ora)

        # Nota (facoltativa)
        if req.nota:
            await page.locator("#Nota").fill(req.nota[:250], timeout=PW_STEP_TIMEOUT_MS)

        # Conferma step dati
        await page.locator(".confDati").click(timeout=PW_STEP_TIMEOUT_MS)

        # STEP DATI: form
        await page.wait_for_selector("#prenoForm", state="visible", timeout=PW_NAV_TIMEOUT_MS)

        await page.locator("#Nome").fill(req.nome, timeout=PW_STEP_TIMEOUT_MS)
        await page.locator("#Cognome").fill(req.cognome, timeout=PW_STEP_TIMEOUT_MS)
        await page.locator("#Email").fill(str(req.email), timeout=PW_STEP_TIMEOUT_MS)

        # Telefono: sito sembra accettare 10 cifre italiane.
        # Noi abbiamo E.164: convertiamo a nazionale IT se +39
        tel = req.telefono
        if tel.startswith("+39"):
            tel_local = tel.replace("+39", "")
        else:
            tel_local = re.sub(r"\D", "", tel)
        tel_local = tel_local[:10]
        await page.locator("#Telefono").fill(tel_local, timeout=PW_STEP_TIMEOUT_MS)

        # Submit + verifica OK
        await _wait_ajax_ok(page)

        return {
            "status": "OK",
            "message": "Prenotazione confermata.",
            "confirmations": {
                "whatsapp": True,
                "email": True,
                "note": confirmation_text(),
            },
        }

    finally:
        await context.close()


async def _perform_availability(req: AvailabilityRequest) -> Dict[str, Any]:
    """
    Ritorna lista orari disponibili (best effort).
    """
    context = await _new_fast_context()
    page = await context.new_page()
    page.set_default_navigation_timeout(PW_NAV_TIMEOUT_MS)
    page.set_default_timeout(PW_STEP_TIMEOUT_MS)

    try:
        await page.goto(BOOKING_URL, wait_until="domcontentloaded")
        await page.wait_for_selector(".stepCont", state="visible", timeout=PW_NAV_TIMEOUT_MS)

        await page.locator(f'.nCoperti[rel="{req.persone}"]').click(timeout=PW_STEP_TIMEOUT_MS)

        d = req.data
        today = _today_rome().strftime("%Y-%m-%d")
        tomorrow = (_today_rome() + timedelta(days=1)).strftime("%Y-%m-%d")

        if d == today:
            await page.locator('.dataOggi[rel="' + today + '"]').click(timeout=PW_STEP_TIMEOUT_MS)
        elif d == tomorrow:
            await page.locator('.dataOggi[rel="' + tomorrow + '"]').click(timeout=PW_STEP_TIMEOUT_MS)
        else:
            await page.locator("#DataPren").fill(d, timeout=PW_STEP_TIMEOUT_MS)
            await page.locator("#DataPren").dispatch_event("change")

        await page.locator(f'.tipoBtn[rel="{req.tipologia}"]').click(timeout=PW_STEP_TIMEOUT_MS)

        await page.wait_for_selector(".ristoCont", state="visible", timeout=PW_NAV_TIMEOUT_MS)
        await _click_by_text(page, req.sede, timeout_ms=PW_STEP_TIMEOUT_MS)

        await page.wait_for_selector("#OraPren", state="visible", timeout=PW_NAV_TIMEOUT_MS)

        options = await page.query_selector_all("#OraPren option")
        times: List[str] = []
        for o in options:
            val = await o.get_attribute("value")
            if not val:
                continue
            # prendi solo HH:MM
            m = re.match(r"^(\d{2}:\d{2})", val)
            if m:
                times.append(m.group(1))

        times = sorted(list(dict.fromkeys(times)))
        return {"status": "OK", "available_times": times}

    finally:
        await context.close()


# ----------------------------
# ROUTES
# ----------------------------
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/checkavailability")
async def checkavailability(req: AvailabilityRequest):
    # regole date (in caso)
    d = datetime.strptime(req.data, "%Y-%m-%d").date()
    # MAX 30 giorni / no passato
    today = _today_rome()
    if d < today:
        return JSONResponse(
            status_code=200,
            content={"status": "NOT_ALLOWED", "reason": "PAST_DATE", "message": "Non è possibile prenotare per date passate."},
        )
    if d > today + timedelta(days=MAX_DAYS_AHEAD):
        return JSONResponse(
            status_code=200,
            content={
                "status": "NEED_HUMAN",
                "reason": "TOO_FAR",
                "message": f"Per prenotazioni oltre {MAX_DAYS_AHEAD} giorni è necessario contattare un operatore.",
                "transfer_to": OPERATOR_PHONE,
            },
        )

    async with _booking_sem:
        try:
            return await _perform_availability(req)
        except Exception as e:
            # Best effort: non bloccare il bot
            return JSONResponse(
                status_code=200,
                content={"status": "ERROR", "message": "Non sono riuscito a recuperare gli orari disponibili in questo momento.", "detail": str(e)},
            )


@app.post("/book_table")
async def book_table(req: BookTableRequest):
    # Regole business (già validate in model), ma qui ritorniamo messaggi strutturati per il bot.
    d = datetime.strptime(req.data, "%Y-%m-%d").date()
    today = _today_rome()

    if req.persone > MAX_PEOPLE_AUTOMATION:
        return JSONResponse(
            status_code=200,
            content={
                "status": "NEED_HUMAN",
                "reason": "TOO_MANY_PEOPLE",
                "message": "Per tavoli superiori a 9 persone è necessario contattare un operatore.",
                "transfer_to": OPERATOR_PHONE,
            },
        )

    if d < today:
        return JSONResponse(
            status_code=200,
            content={
                "status": "NOT_ALLOWED",
                "reason": "PAST_DATE",
                "message": "Non è possibile prenotare per date passate.",
            },
        )

    if d > today + timedelta(days=MAX_DAYS_AHEAD):
        return JSONResponse(
            status_code=200,
            content={
                "status": "NEED_HUMAN",
                "reason": "TOO_FAR",
                "message": f"Per prenotazioni oltre {MAX_DAYS_AHEAD} giorni è necessario contattare un operatore.",
                "transfer_to": OPERATOR_PHONE,
            },
        )

    # Hard cap totale (evita timeout Railway)
    async def _run_with_timeout():
        async with _booking_sem:
            attempt = 0
            last_err: Optional[str] = None
            while attempt <= MAX_RETRY:
                try:
                    return await _perform_booking(req)
                except (PWTimeoutError, asyncio.TimeoutError) as e:
                    last_err = f"Timeout: {e}"
                except Exception as e:
                    last_err = str(e)

                attempt += 1

            # fallback: dopo retry, proponi operatore
            return {
                "status": "NEED_HUMAN",
                "reason": "AUTOMATION_FAILED",
                "message": "Non sono riuscito a completare la prenotazione in automatico. Ti passo un operatore per chiuderla rapidamente.",
                "transfer_to": OPERATOR_PHONE,
                "detail": last_err,
            }

    try:
        result = await asyncio.wait_for(_run_with_timeout(), timeout=PW_TOTAL_TIMEOUT_SEC)
        return result
    except asyncio.TimeoutError:
        return JSONResponse(
            status_code=200,
            content={
                "status": "NEED_HUMAN",
                "reason": "TIMEOUT",
                "message": "Per evitare attese, ti passo un operatore per completare la prenotazione.",
                "transfer_to": OPERATOR_PHONE,
            },
        )
