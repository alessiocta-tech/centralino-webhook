import os
import re
from datetime import datetime, timedelta
from typing import Optional, Any, Dict, Union

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, validator
from playwright.async_api import async_playwright

# ============================================================
# CONFIG
# ============================================================

BOOKING_URL = os.getenv("BOOKING_URL", "https://rione.fidy.app/prenew.php?referer=AI")
PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "60000"))
PW_NAV_TIMEOUT_MS = int(os.getenv("PW_NAV_TIMEOUT_MS", "60000"))
DISABLE_FINAL_SUBMIT = os.getenv("DISABLE_FINAL_SUBMIT", "false").lower() == "true"

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

app = FastAPI()


# ============================================================
# DEBUG: LOG 422 WITH BODY (super utile con ElevenLabs)
# ============================================================

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    try:
        body = await request.json()
    except Exception:
        body = "<non-json body>"
    print("❌ 422 VALIDATION ERROR")
    print("PATH:", request.url.path)
    print("BODY:", body)
    print("DETAILS:", exc.errors())
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors(), "received_body": body},
    )


# ============================================================
# INPUT NORMALIZATION (ElevenLabs compat)
# ============================================================

def unwrap_body(payload: Any) -> Dict[str, Any]:
    """
    ElevenLabs a volte manda:
      { "body": { ... } }
    oppure manda direttamente:
      { ... }
    """
    if isinstance(payload, dict) and "body" in payload and isinstance(payload["body"], dict):
        return payload["body"]
    if isinstance(payload, dict):
        return payload
    return {}


def normalize_orario(s: str) -> str:
    s = (s or "").strip().lower().replace("ore", "").replace("alle", "").strip()
    s = s.replace(".", ":").replace(",", ":")
    if re.fullmatch(r"\d{1,2}$", s):
        return f"{int(s):02d}:00"
    if re.fullmatch(r"\d{1,2}:\d{2}$", s):
        hh, mm = s.split(":")
        return f"{int(hh):02d}:{int(mm):02d}"
    return s


def normalize_sede(s: str) -> str:
    s0 = (s or "").strip().lower()
    mapping = {
        "talenti": "Talenti - Roma",
        "talenti roma": "Talenti - Roma",
        "roma talenti": "Talenti - Roma",
        "ostia": "Ostia Lido",
        "ostia lido": "Ostia Lido",
        "appia": "Appia",
        "reggio": "Reggio Calabria",
        "reggio calabria": "Reggio Calabria",
        "palermo": "Palermo",
        "palermo centro": "Palermo",
    }
    return mapping.get(s0, (s or "").strip())


def parse_data_iso(value: str) -> str:
    """
    Accetta:
    - '2026-02-12'
    - '12/02/2026'
    - 'domani', 'oggi'
    Ritorna sempre YYYY-MM-DD
    """
    v = (value or "").strip().lower()

    oggi = datetime.now().date()
    if v in ("oggi", "today"):
        return oggi.strftime("%Y-%m-%d")
    if v in ("domani", "tomorrow"):
        return (oggi + timedelta(days=1)).strftime("%Y-%m-%d")

    # YYYY-MM-DD
    try:
        d = datetime.strptime(value.strip(), "%Y-%m-%d").date()
        return d.strftime("%Y-%m-%d")
    except Exception:
        pass

    # DD/MM/YYYY
    try:
        d = datetime.strptime(value.strip(), "%d/%m/%Y").date()
        return d.strftime("%Y-%m-%d")
    except Exception:
        pass

    # fallback: lascia com’è (meglio che 422)
    return value.strip()


def calcola_pasto(orario_str: str) -> str:
    """13:00 -> PRANZO, 20:30 -> CENA"""
    try:
        hh = int(orario_str.split(":")[0])
        return "PRANZO" if hh < 17 else "CENA"
    except Exception:
        return "CENA"


def get_data_type(data_str: str) -> str:
    """Ritorna: Oggi / Domani / Altra"""
    try:
        data_pren = datetime.strptime(data_str, "%Y-%m-%d").date()
        oggi = datetime.now().date()
        domani = oggi + timedelta(days=1)
        if data_pren == oggi:
            return "Oggi"
        if data_pren == domani:
            return "Domani"
        return "Altra"
    except Exception:
        return "Altra"


# ============================================================
# MODELS (compat: persone può arrivare string)
# ============================================================

class RichiestaControllo(BaseModel):
    sede: str
    data: str  # YYYY-MM-DD
    persone: Union[int, str] = Field(..., description="Numero persone")
    orario: Optional[str] = None

    @validator("persone", pre=True)
    def coerce_persone(cls, v):
        if v is None:
            return v
        if isinstance(v, int):
            return v
        s = str(v).strip()
        # prendi primo numero dentro stringa
        m = re.search(r"\d+", s)
        return int(m.group(0)) if m else 1

    @validator("data", pre=True)
    def coerce_data(cls, v):
        return parse_data_iso(str(v))

    @validator("sede", pre=True)
    def coerce_sede(cls, v):
        return normalize_sede(str(v))


class RichiestaPrenotazione(BaseModel):
    nome: str
    email: str
    telefono: str

    sede: str
    data: str
    orario: str
    persone: Union[int, str]
    note: Optional[str] = ""

    @validator("persone", pre=True)
    def coerce_persone(cls, v):
        if v is None:
            return v
        if isinstance(v, int):
            return v
        s = str(v).strip()
        m = re.search(r"\d+", s)
        return int(m.group(0)) if m else 1

    @validator("data", pre=True)
    def coerce_data(cls, v):
        return parse_data_iso(str(v))

    @validator("orario", pre=True)
    def coerce_orario(cls, v):
        return normalize_orario(str(v))

    @validator("sede", pre=True)
    def coerce_sede(cls, v):
        return normalize_sede(str(v))


# ============================================================
# PLAYWRIGHT HELPERS (come versione FUNZIONA)
# ============================================================

async def maybe_click_cookie(page):
    for patt in [r"accetta", r"consent", r"ok", r"accetto"]:
        try:
            loc = page.locator(f"text=/{patt}/i").first
            if await loc.count() > 0:
                await loc.click(timeout=2000, force=True)
                return
        except Exception:
            pass


async def click_exact_number(page, n: int):
    txt = str(n)
    for sel in [f"button:text-is('{txt}')", f"div:text-is('{txt}')", f"span:text-is('{txt}')"]:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                await loc.click(timeout=3000, force=True)
                return True
        except Exception:
            pass
    try:
        await page.get_by_text(txt, exact=True).first.click(timeout=3000, force=True)
        return True
    except Exception:
        return False


async def click_seggiolini_no(page):
    try:
        if await page.locator("text=/seggiolini/i").count() > 0:
            no_btn = page.locator("text=/^\\s*NO\\s*$/i").first
            if await no_btn.count() > 0:
                await no_btn.click(timeout=4000, force=True)
                return True
    except Exception:
        pass
    return False


async def set_date(page, data_iso: str):
    tipo = get_data_type(data_iso)
    await page.wait_for_timeout(800)

    if tipo in ["Oggi", "Domani"]:
        try:
            btn = page.locator(f"text=/{tipo}/i").first
            if await btn.count() > 0:
                await btn.click(timeout=4000, force=True)
                return
        except Exception:
            pass

    try:
        altra = page.locator("text=/Altra data/i").first
        if await altra.count() > 0:
            await altra.click(timeout=5000, force=True)
    except Exception:
        pass

    await page.wait_for_timeout(600)
    try:
        await page.evaluate(
            f"""() => {{
                const el = document.querySelector('input[type="date"]');
                if (el) el.value = "{data_iso}";
            }}"""
        )
    except Exception:
        pass

    try:
        date_input = page.locator('input[type="date"]').first
        if await date_input.count() > 0:
            await date_input.press("Enter")
    except Exception:
        pass


async def click_pasto(page, pasto: str):
    await page.wait_for_timeout(1200)
    try:
        btn = page.locator(f"text=/{pasto}/i").first
        if await btn.count() > 0:
            await btn.click(timeout=5000, force=True)
            return True
    except Exception:
        pass
    return False


async def click_sede(page, sede_target: str):
    await page.wait_for_timeout(1200)

    # prova testo completo
    try:
        loc = page.get_by_text(sede_target, exact=False).first
        if await loc.count() > 0:
            await loc.click(timeout=6000, force=True)
            return True
    except Exception:
        pass

    # fallback: prima parola (es: "Talenti")
    base = sede_target.split("-")[0].strip()
    if base:
        try:
            loc = page.get_by_text(base, exact=False).first
            if await loc.count() > 0:
                await loc.click(timeout=6000, force=True)
                return True
        except Exception:
            pass

    return False


async def click_orario(page, orario_hhmm: str):
    await page.wait_for_timeout(1200)

    try:
        sel = page.locator("select").first
        if await sel.count() > 0:
            await sel.click(timeout=2000)
    except Exception:
        pass

    candidates = [
        f"text=/{re.escape(orario_hhmm)}/",
        f"text={orario_hhmm}",
        f"text=/{re.escape(orario_hhmm.replace(':', '.'))}/",
    ]
    for c in candidates:
        try:
            loc = page.locator(c).first
            if await loc.count() > 0:
                await loc.click(timeout=6000, force=True)
                return True
        except Exception:
            pass

    return False


async def click_conferma(page):
    for patt in [r"CONFERMA", r"Conferma", r"continua", r"avanti"]:
        try:
            btn = page.locator(f"text=/{patt}/").first
            if await btn.count() > 0:
                await btn.click(timeout=5000, force=True)
                return True
        except Exception:
            pass
    return False


async def fill_final_form(page, nome: str, email: str, telefono: str, note: str):
    await page.wait_for_timeout(1200)
    parti = (nome or "").strip().split(" ", 1)
    n = parti[0] if parti else nome
    c = parti[1] if len(parti) > 1 else "Cliente"

    async def try_fill_any(selectors, value):
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    await loc.fill(value, timeout=4000)
                    return True
            except Exception:
                pass
        return False

    await try_fill_any(['input[name="Nome"]', 'input[name="nome"]', 'input[placeholder*="Nome" i]', 'input#Nome', 'input#nome'], n)
    await try_fill_any(['input[name="Cognome"]', 'input[name="cognome"]', 'input[placeholder*="Cognome" i]', 'input#Cognome', 'input#cognome'], c)
    await try_fill_any(['input[name="Email"]', 'input[name="email"]', 'input[type="email"]', 'input[placeholder*="Email" i]', 'input#Email'], email)
    await try_fill_any(['input[name="Telefono"]', 'input[name="telefono"]', 'input[type="tel"]', 'input[placeholder*="Telefono" i]', 'input#Telefono'], telefono)

    if note:
        await try_fill_any(['textarea[name="Nota"]', 'textarea[name="note"]', 'textarea', 'textarea[placeholder*="note" i]'], note)

    try:
        checkboxes = page.locator('input[type="checkbox"]')
        cnt = await checkboxes.count()
        for i in range(cnt):
            cb = checkboxes.nth(i)
            try:
                if await cb.is_visible():
                    await cb.check(timeout=2000)
            except Exception:
                pass
    except Exception:
        pass


async def click_prenota(page):
    for patt in [r"PRENOTA", r"Prenota", r"CONFERMA PRENOTAZIONE"]:
        try:
            btn = page.locator(f"text=/{patt}/").last
            if await btn.count() > 0:
                await btn.click(timeout=8000, force=True)
                return True
        except Exception:
            pass
    return False


# ============================================================
# ROUTES
# ============================================================

@app.get("/")
def home():
    return {"status": "Centralino AI - Booking Engine (ElevenLabs compat)"}


@app.post("/check_availability")
async def check_availability_raw(request: Request):
    payload = unwrap_body(await request.json())
    dati = RichiestaControllo(**payload)

    sede_target = dati.sede
    orario = normalize_orario(dati.orario) if dati.orario else None
    pasto = calcola_pasto(orario) if orario else None

    print(f"🔎 CHECK: {dati.persone} pax | {dati.data} | sede={sede_target} | ora={orario or '-'}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--single-process", "--disable-gpu"],
        )
        context = await browser.new_context(user_agent=IPHONE_UA, viewport={"width": 390, "height": 844})
        page = await context.new_page()
        page.set_default_timeout(PW_TIMEOUT_MS)
        page.set_default_navigation_timeout(PW_NAV_TIMEOUT_MS)

        async def route_handler(route):
            if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
                await route.abort()
            else:
                await route.continue_()
        await page.route("**/*", route_handler)

        try:
            await page.goto(BOOKING_URL, wait_until="domcontentloaded")
            await maybe_click_cookie(page)

            await page.wait_for_timeout(600)
            await click_exact_number(page, int(dati.persone))
            await click_seggiolini_no(page)
            await set_date(page, dati.data)

            if pasto:
                await click_pasto(page, pasto)

            return {"result": "OK (check completato fino a data/pasto)."}
        except Exception as e:
            return {"result": f"Errore Check: {e}"}
        finally:
            await browser.close()


@app.post("/book_table")
async def book_table_raw(request: Request):
    payload = unwrap_body(await request.json())
    dati = RichiestaPrenotazione(**payload)

    sede_target = dati.sede
    orario = dati.orario
    pasto = calcola_pasto(orario)

    print(f"🚀 BOOKING: {dati.nome} -> {sede_target} | {dati.data} {orario} | pax={dati.persone} | pasto={pasto}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--single-process", "--disable-gpu"],
        )
        context = await browser.new_context(user_agent=IPHONE_UA, viewport={"width": 390, "height": 844})
        page = await context.new_page()
        page.set_default_timeout(PW_TIMEOUT_MS)
        page.set_default_navigation_timeout(PW_NAV_TIMEOUT_MS)

        async def route_handler(route):
            if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
                await route.abort()
            else:
                await route.continue_()
        await page.route("**/*", route_handler)

        try:
            await page.goto(BOOKING_URL, wait_until="domcontentloaded")
            await maybe_click_cookie(page)

            # 1) persone
            await page.wait_for_timeout(800)
            await click_exact_number(page, int(dati.persone))

            # 2) seggiolini NO
            await page.wait_for_timeout(700)
            await click_seggiolini_no(page)

            # 3) data
            await set_date(page, dati.data)

            # 4) pasto
            await click_pasto(page, pasto)

            # 5) sede
            ok_sede = await click_sede(page, sede_target)
            if not ok_sede:
                raise RuntimeError(f"Sede non trovata/cliccabile: {sede_target}")

            # 6) orario
            ok_orario = await click_orario(page, orario)
            if not ok_orario:
                raise RuntimeError(f"Orario non disponibile: {orario}")

            # 7) conferma (vai a schermata finale)
            await page.wait_for_timeout(800)
            await click_conferma(page)

            # 8) form finale
            await fill_final_form(page, dati.nome, dati.email, dati.telefono, dati.note or "")

            if DISABLE_FINAL_SUBMIT:
                return {"result": "FORM COMPILATO (test mode, submit disattivato)"}

            ok_submit = await click_prenota(page)
            if not ok_submit:
                raise RuntimeError("Bottone PRENOTA non trovato")

            await page.wait_for_timeout(1500)
            return {"result": f"Prenotazione inviata per {dati.nome} - {sede_target} {dati.data} {orario} ({dati.persone} pax)"}

        except Exception as e:
            try:
                ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
                path = f"booking_error_{ts}.png"
                await page.screenshot(path=path, full_page=True)
                print(f"📸 Screenshot salvato: {path}")
            except Exception:
                pass
            return {"result": f"Errore prenotazione: {e}"}
        finally:
            await browser.close()


# ============================================================
# ALIAS ENDPOINTS (evitano chiamate “sbagliate”)
# ============================================================

@app.post("/checkavailability")
async def checkavailability_alias(request: Request):
    return await check_availability_raw(request)


@app.post("/booktable")
async def booktable_alias(request: Request):
    return await book_table_raw(request)
