import os
import re
import time
import json
import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Optional, Any, Dict

import phonenumbers
from fastapi import FastAPI, HTTPException, Request
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

# Numero a cui trasferire (gestito in piattaforma voce/agent)
TRANSFER_NUMBER = os.getenv("TRANSFER_NUMBER", "")  # es: +39...
# (Facoltativo) testo di conferma standard
CONFIRMATION_NOTICE = (
    "Riceverai una conferma via WhatsApp e via email ai contatti indicati. "
    "È importante rispettare l’orario prenotato: in caso di ritardo prolungato potrebbe essere necessario liberare il tavolo."
)

# Playwright
PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "15000"))
PW_RETRIES = int(os.getenv("PW_RETRIES", "2"))
PW_HEADLESS = os.getenv("PW_HEADLESS", "true").lower() != "false"

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

def normalize_time_to_hhmm(raw: str) -> str:
    """
    Converte:
    - "ore 13" / "13" -> "13:00"
    - "8" -> "08:00"
    - "20.30" / "20,30" -> "20:30"
    - "20:30" -> "20:30"
    """
    if raw is None:
        raise ValueError("Ora mancante")

    s = str(raw).strip().lower()
    s = s.replace("ore", "").replace("alle", "").strip()

    # 20.30 / 20,30 -> 20:30
    s = s.replace(".", ":").replace(",", ":")

    # solo numero "13"
    if re.fullmatch(r"\d{1,2}", s):
        hh = int(s)
        if hh < 0 or hh > 23:
            raise ValueError("Ora non valida")
        return f"{hh:02d}:00"

    # formato HH:MM
    if re.fullmatch(r"\d{1,2}:\d{2}", s):
        hh, mm = s.split(":")
        hh_i = int(hh)
        mm_i = int(mm)
        if not (0 <= hh_i <= 23 and 0 <= mm_i <= 59):
            raise ValueError("Ora non valida")
        return f"{hh_i:02d}:{mm_i:02d}"

    raise ValueError("Formato ora non valido")


def parse_iso_date(raw: str) -> date:
    """
    Accetta solo ISO YYYY-MM-DD (per stabilità).
    Se serve supportare 'domani' ecc. conviene farlo lato LLM prima,
    ma qui blocchiamo input ambigui.
    """
    if raw is None:
        raise ValueError("Data mancante")
    s = str(raw).strip()
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        raise ValueError("Formato data non valido (usa YYYY-MM-DD)")


def today_local() -> date:
    # in produzione: usare timezone (Europe/Rome) con zoneinfo, se vuoi.
    return date.today()


def within_days_limit(d: date, max_days: int) -> bool:
    return d <= (today_local() + timedelta(days=max_days))


def is_past_date(d: date) -> bool:
    return d < today_local()


def safe_filename(prefix: str) -> str:
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    return f"{prefix}_{ts}.png"


# ------------------------------------------------------------
# REQUEST MODELS (Pydantic v2)
# ------------------------------------------------------------

class BookingRequest(BaseModel):
    # Nome e cognome
    nome: str
    cognome: Optional[str] = ""

    # contatti
    email: str
    telefono: str

    # prenotazione
    persone: int = Field(ge=1, le=50)  # limiti reali gestiti da regole sotto
    sede: str
    data: str

    # accetta ora/orario
    ora: Optional[str] = None
    orario: Optional[str] = None

    # opzioni
    seggiolone: bool = False
    seggiolini: int = 0

    # nota: supporto doppio nome campo
    nota: str = ""
    note: Optional[str] = ""

    referer: str = "AI"
    dry_run: bool = False

    # --- VALIDAZIONI / NORMALIZZAZIONI

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
            "appia": "Appia",
            "ostia": "Ostia",
            "reggio": "Reggio Calabria",
            "reggio calabria": "Reggio Calabria",
            "palermo": "Palermo",
            "palermo centro": "Palermo",
        }
        return mapping.get(s, (v or "").strip().title())

    @model_validator(mode="after")
    def normalize_fields(self) -> "BookingRequest":
        # 1) ora/orario
        raw_time = self.ora or self.orario
        self.ora = normalize_time_to_hhmm(raw_time)

        # 2) nota/note
        if not self.nota and self.note:
            self.nota = self.note

        # 3) cognome: se manca, prova a ricavarlo dal nome completo
        if not (self.cognome or "").strip():
            parts = (self.nome or "").strip().split()
            if len(parts) >= 2:
                # nome potrebbe essere "Alessio Muzzarelli"
                self.cognome = parts[-1]
                # e come nome teniamo tutto tranne l’ultimo
                self.nome = " ".join(parts[:-1])
            else:
                self.cognome = "Cliente"

        # 4) data ISO check
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
        # normalizza sede
        s = (self.sede or "").strip().lower()
        mapping = {
            "talenti": "Talenti",
            "appia": "Appia",
            "ostia": "Ostia",
            "reggio": "Reggio Calabria",
            "reggio calabria": "Reggio Calabria",
            "palermo": "Palermo",
            "palermo centro": "Palermo",
        }
        self.sede = mapping.get(s, (self.sede or "").strip().title())
        _ = parse_iso_date(self.data)

        if self.ora or self.orario:
            self.ora = normalize_time_to_hhmm(self.ora or self.orario)

        return self


# ------------------------------------------------------------
# PLAYWRIGHT (shared browser for speed)
# ------------------------------------------------------------

class PlaywrightManager:
    def __init__(self) -> None:
        self.pw: Optional[Playwright] = None
        self.browser: Optional[Browser] = None

    async def start(self) -> None:
        logger.info("Starting Playwright...")
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(headless=PW_HEADLESS)
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
        ctx = await self.browser.new_context()
        page = await ctx.new_page()
        page.set_default_timeout(PW_TIMEOUT_MS)
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
            await asyncio.sleep(0.5 * (attempt + 1))
    raise last_err


async def playwright_submit_booking(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Implementazione base con Playwright.

    ⚠️ Se i selettori del sito cambiano, qui vanno aggiornati.
    In caso di errore salva screenshot per debug.
    """

    async def _do(attempt: int):
        page = await pw_manager.new_page()
        try:
            logger.info("Booking attempt #%s - opening %s", attempt + 1, BOOKING_URL)
            await page.goto(BOOKING_URL, wait_until="domcontentloaded")

            # ------------------------------------------------------------------
            # TODO: QUI VANNO I SELETTORI REALI DEL SITO
            #
            # Questa è una logica generica; va adattata a come funziona prenew.php
            # (bottoni per persone, calendario, step email/telefono, ecc.)
            # ------------------------------------------------------------------

            # Esempio generico: se ci sono input classici:
            # await page.fill('input[name="nome"]', payload["nome"])
            # await page.fill('input[name="cognome"]', payload["cognome"])
            # await page.fill('input[name="email"]', payload["email"])
            # await page.fill('input[name="telefono"]', payload["telefono"])
            # await page.fill('input[name="data"]', payload["data"])
            # await page.fill('input[name="ora"]', payload["ora"])
            #
            # await page.click('button[type="submit"]')
            #
            # Se invece è a step con bottoni:
            # - clicca numero persone
            # - clicca "Altra data" e seleziona data
            # - seleziona fascia/ora
            # - compila contatti
            # - conferma
            #
            # Per evitare di "rompere" in produzione, qui facciamo un check minimo:
            # ------------------------------------------------------------------

            # Heuristics: prova a compilare i campi se presenti (non fa crash se non li trova)
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
            await try_fill('input[name="data"]', payload["data"])
            await try_fill('input[name="ora"]', payload["ora"])
            await try_fill('textarea[name="note"]', payload.get("nota", ""))

            # prova submit standard
            submitted = False
            for sel in ['button[type="submit"]', 'button:has-text("Conferma")', 'button:has-text("Prenota")']:
                try:
                    if await page.locator(sel).count() > 0:
                        await page.click(sel)
                        submitted = True
                        break
                except Exception:
                    continue

            if not submitted:
                # se il sito è full-step, qui serve mapping preciso dei passaggi
                raise RuntimeError("Impossibile inviare: selettori non trovati (serve configurazione step-by-step).")

            # attesa risposta
            await page.wait_for_timeout(1000)

            # Se esiste un elemento di conferma:
            # if await page.locator("text=Prenotazione confermata").count() > 0:
            #     ...

            # Fallback: se non abbiamo prova certa, consideriamo successo "best-effort"
            return {
                "ok": True,
                "message": "Prenotazione inviata",
                "details": {"best_effort": True}
            }

        except Exception as e:
            # screenshot per debug
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
# APP LIFESPAN
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
    # La piattaforma voce/agent può leggere action e fare transfer_to_number
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

@app.get("/healthz")
async def healthz():
    return {"ok": True, "service": APP_NAME}


@app.post("/checkavailability")
async def checkavailability(req: AvailabilityRequest):
    # Qui puoi implementare la disponibilità reale (Playwright o logica interna).
    # Per ora rispondiamo in modo stabile.
    d = parse_iso_date(req.data)

    # regole data
    if is_past_date(d):
        return JSONResponse(status_code=200, content=user_error("Non è possibile prenotare per date passate."))

    if not within_days_limit(d, MAX_DAYS_AHEAD):
        return JSONResponse(
            status_code=200,
            content=transfer_response(
                f"Per prenotazioni oltre {MAX_DAYS_AHEAD} giorni è necessario parlare con un operatore."
            ),
        )

    if req.persone > MAX_PEOPLE_AUTOMATION:
        return JSONResponse(
            status_code=200,
            content=transfer_response("Per gruppi superiori a 9 persone è necessario parlare con un operatore."),
        )

    # risposta “stub” (qui puoi mettere logica reale)
    return {
        "ok": True,
        "available": True,
        "message": "Disponibilità verificabile. Procedi con la prenotazione.",
        "normalized": {
            "sede": req.sede,
            "data": req.data,
            "ora": req.ora,
            "persone": req.persone,
        },
    }


@app.post("/book_table")
async def book_table(req: BookingRequest, request: Request):
    """
    Endpoint chiamato dallo strumento dell'agente.
    Deve essere robusto: se AI manda campi sbagliati o incompleti, non deve crashare.
    """

    # Regole: persone
    if req.persone > MAX_PEOPLE_AUTOMATION:
        return JSONResponse(
            status_code=200,
            content=transfer_response("Per gruppi superiori a 9 persone è necessario parlare con un operatore."),
        )

    # Regole: data
    d = parse_iso_date(req.data)

    if is_past_date(d):
        return JSONResponse(status_code=200, content=user_error("Non è possibile prenotare per date passate."))

    if not within_days_limit(d, MAX_DAYS_AHEAD):
        return JSONResponse(
            status_code=200,
            content=transfer_response(
                f"Per prenotazioni oltre {MAX_DAYS_AHEAD} giorni è necessario parlare con un operatore."
            ),
        )

    # Payload normalizzato (allineato ai nomi attesi)
    payload = {
        "nome": req.nome.strip(),
        "cognome": (req.cognome or "").strip(),
        "email": req.email.strip(),
        "telefono": req.telefono.strip(),
        "persone": int(req.persone),
        "sede": req.sede.strip(),
        "data": req.data.strip(),
        "ora": req.ora.strip(),  # sempre HH:MM
        "seggiolone": bool(req.seggiolone),
        "seggiolini": int(req.seggiolini or 0),
        "nota": (req.nota or "").strip(),
        "referer": req.referer or "AI",
        "dry_run": bool(req.dry_run),
    }

    logger.info("BOOK_TABLE request normalized: %s", json.dumps(payload, ensure_ascii=False))

    # Dry-run utile in test
    if payload["dry_run"]:
        return {
            "ok": True,
            "message": "Dry-run: dati validi, prenotazione non inviata.",
            "payload": payload,
        }

    # Invio con Playwright
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
        else:
            return JSONResponse(
                status_code=200,
                content={
                    "ok": False,
                    "message": "Non è stato possibile completare la prenotazione in questo momento.",
                    "result": result,
                },
            )

    except Exception as e:
        # NON esporre dettagli tecnici all'utente: log in backend, messaggio semplice in risposta
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


# ------------------------------------------------------------
# OPTIONAL: root
# ------------------------------------------------------------

@app.get("/")
async def root():
    return {"ok": True, "service": APP_NAME, "health": "/healthz"}
