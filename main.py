# main.py
import os
import re
from datetime import datetime, timedelta
from typing import Optional, Union

from fastapi import FastAPI, HTTPException
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
# MODELS
# ============================================================

class RichiestaControllo(BaseModel):
    sede: str
    data: str  # YYYY-MM-DD
    persone: Union[int, str] = Field(..., ge=1)
    orario: Optional[str] = None

    @validator("persone", pre=True)
    def _coerce_persone(cls, v):
        if isinstance(v, int):
            return v
        s = str(v).strip()
        if not s:
            raise ValueError("persone vuoto")
        return int(re.sub(r"[^\d]", "", s))

    @validator("data")
    def _validate_data(cls, v):
        datetime.strptime(v, "%Y-%m-%d")
        return v


class RichiestaPrenotazione(BaseModel):
    nome: str
    email: str
    telefono: str

    sede: str
    data: str  # YYYY-MM-DD
    orario: str  # HH:MM
    persone: Union[int, str] = Field(..., ge=1)

    note: Optional[str] = ""

    @validator("persone", pre=True)
    def _coerce_persone(cls, v):
        if isinstance(v, int):
            return v
        s = str(v).strip()
        if not s:
            raise ValueError("persone vuoto")
        return int(re.sub(r"[^\d]", "", s))

    @validator("data")
    def _validate_data(cls, v):
        datetime.strptime(v, "%Y-%m-%d")
        return v

    @validator("orario")
    def _validate_orario(cls, v):
        # accetto "13", "13:00", "13.00", "ore 13"
        return normalize_orario(v)


# ============================================================
# HELPERS
# ============================================================

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


def normalize_orario(s: str) -> str:
    s = (s or "").strip().lower().replace("ore", "").replace("alle", "").strip()
    s = s.replace(".", ":").replace(",", ":")
    if re.fullmatch(r"\d{1,2}$", s):
        return f"{int(s):02d}:00"
    if re.fullmatch(r"\d{1,2}:\d{2}$", s):
        hh, mm = s.split(":")
        return f"{int(hh):02d}:{int(mm):02d}"
    return s.strip()


def normalize_sede(s: str) -> str:
    s0 = (s or "").strip().lower()
    mapping = {
        "talenti": "Talenti - Roma",
        "talenti roma": "Talenti - Roma",
        "ostia": "Ostia Lido",
        "ostia lido": "Ostia Lido",
        "appia": "Appia",
        "reggio": "Reggio Calabria",
        "reggio calabria": "Reggio Calabria",
        "palermo": "Palermo",
        "palermo centro": "Palermo",
    }
    return mapping.get(s0, s.strip())


# ID stabili dal tuo HTML (rel="X")
SEDE_TO_REL = {
    "Talenti - Roma": "1",
    "Reggio Calabria": "2",
    "Ostia Lido": "3",
    "Appia": "4",
    "Palermo": "5",
}


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
    await page.wait_for_timeout(600)

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

    await page.wait_for_timeout(400)

    # set input date via JS + Enter
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
    await page.wait_for_timeout(900)
    try:
        btn = page.locator(f"text=/{pasto}/i").first
        if await btn.count() > 0:
            await btn.click(timeout=6000, force=True)
            return True
    except Exception:
        pass
    return False


async def click_sede(page, sede_target: str):
    await page.wait_for_timeout(600)

    # attendo che la lista sia stata caricata
    await page.wait_for_selector(".ristoBtn", timeout=20000)

    rel = SEDE_TO_REL.get(sede_target)
    if rel:
        loc = page.locator(f'.ristoBtn[rel="{rel}"]').first
        if await loc.count() > 0:
            await loc.scroll_into_view_if_needed()
            await loc.click(timeout=8000, force=True)
            return True

    # fallback testuale (se cambierà l'HTML)
    try:
        loc2 = page.get_by_text(sede_target, exact=False).first
        if await loc2.count() > 0:
            await loc2.scroll_into_view_if_needed()
            await loc2.click(timeout=8000, force=True)
            return True
    except Exception:
        pass

    return False


async def select_orario(page, orario_hhmm: str):
    await page.wait_for_timeout(700)

    sel = page.locator("select#OraPren").first
    if await sel.count() > 0:
        # sul sito il value è spesso "13:00:00"
        val = f"{orario_hhmm}:00" if re.fullmatch(r"\d{2}:\d{2}", orario_hhmm) else orario_hhmm
        try:
            await sel.select_option(value=val, timeout=8000)
            return True
        except Exception:
            # fallback: cerca option con testo "13:00"
            opt = page.locator(f'select#OraPren option:has-text("{orario_hhmm}")').first
            if await opt.count() > 0:
                v = await opt.get_attribute("value")
                if v:
                    await sel.select_option(value=v, timeout=8000)
                    return True

    # fallback estremo (non consigliato, ma utile se l'UI cambia)
    try:
        loc = page.locator(f"text=/{re.escape(orario_hhmm)}/").first
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
                await btn.click(timeout=7000, force=True)
                return True
        except Exception:
            pass
    return False


async def fill_final_form(page, nome: str, email: str, telefono: str, note: str):
    await page.wait_for_timeout(900)

    parti = (nome or "").strip().split(" ", 1)
    n = parti[0] if parti else nome
    c = parti[1] if len(parti) > 1 else "Cliente"

    async def try_fill_any(selectors, value):
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    await loc.fill(value, timeout=5000)
                    return True
            except Exception:
                pass
        return False

    await try_fill_any(['input[name="Nome"]', 'input[name="nome"]', 'input[placeholder*="Nome" i]', 'input#Nome'], n)
    await try_fill_any(['input[name="Cognome"]', 'input[name="cognome"]', 'input[placeholder*="Cognome" i]', 'input#Cognome'], c)
    await try_fill_any(['input[name="Email"]', 'input[name="email"]', 'input[type="email"]', 'input#Email'], email)
    await try_fill_any(['input[name="Telefono"]', 'input[name="telefono"]', 'input[type="tel"]', 'input#Telefono'], telefono)

    if note:
        await try_fill_any(['textarea[name="Nota"]', 'textarea[name="note"]', 'textarea', 'textarea#Nota'], note)

    # spunta checkbox se presenti
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
                await btn.click(timeout=10000, force=True)
                return True
        except Exception:
            pass
    return False


# ============================================================
# ROUTES
# ============================================================

@app.get("/")
def home():
    return {"status": "Centralino AI - Booking Engine (stable rel-click + select_option)"}


@app.post("/check_availability")
async def check_availability(dati: RichiestaControllo):
    sede_target = normalize_sede(dati.sede)
    orario = normalize_orario(dati.orario) if dati.orario else None
    pasto = calcola_pasto(orario) if orario else None
    persone = int(dati.persone)

    print(f"🔎 CHECK: {persone} pax | {dati.data} | sede={sede_target} | ora={orario or '-'}")

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

            await click_exact_number(page, persone)
            await click_seggiolini_no(page)
            await set_date(page, dati.data)
            if pasto:
                await click_pasto(page, pasto)

            return {"ok": True, "message": "Check completato (step base)."}
        except Exception as e:
            print(f"❌ CHECK ERROR: {repr(e)}")
            raise HTTPException(status_code=500, detail=f"Errore Check: {e}")
        finally:
            await browser.close()


@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione):
    sede_target = normalize_sede(dati.sede)
    orario = normalize_orario(dati.orario)
    pasto = calcola_pasto(orario)
    persone = int(dati.persone)

    print(f"🚀 BOOKING: {dati.nome} -> {sede_target} | {dati.data} {orario} | pax={persone} | pasto={pasto}")

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
            ok_people = await click_exact_number(page, persone)
            if not ok_people:
                print("⚠️ Bottone persone non trovato (continuo)")

            # 2) seggiolini NO
            await click_seggiolini_no(page)

            # 3) data
            await set_date(page, dati.data)

            # 4) pasto
            await click_pasto(page, pasto)

            # 5) sede (ROBUSTO via rel)
            ok_sede = await click_sede(page, sede_target)
            if not ok_sede:
                raise RuntimeError(f"Sede non trovata/cliccabile: {sede_target}")

            # 6) orario (ROBUSTO via select_option)
            ok_orario = await select_orario(page, orario)
            if not ok_orario:
                raise RuntimeError(f"Orario non disponibile: {orario}")

            # 7) conferma per passare alla form
            await page.wait_for_timeout(400)
            await click_conferma(page)

            # 8) compila form
            await fill_final_form(page, dati.nome, dati.email, dati.telefono, dati.note or "")

            if DISABLE_FINAL_SUBMIT:
                return {"ok": True, "message": "FORM COMPILATO (test mode, submit disattivato)"}

            ok_submit = await click_prenota(page)
            if not ok_submit:
                raise RuntimeError("Bottone PRENOTA non trovato")

            await page.wait_for_timeout(1500)
            return {
                "ok": True,
                "message": f"Prenotazione inviata: {sede_target} {dati.data} {orario} - {persone} pax - {dati.nome}"
            }

        except Exception as e:
            print(f"❌ BOOK ERROR: {repr(e)}")
            screenshot_path = None
            try:
                ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
                screenshot_path = f"/tmp/booking_error_{ts}.png"
                await page.screenshot(path=screenshot_path, full_page=True)
                print(f"📸 Screenshot salvato: {screenshot_path}")
            except Exception as se:
                print(f"⚠️ Screenshot fallito: {repr(se)}")

            detail = f"Errore prenotazione: {e}"
            if screenshot_path:
                detail += f" | screenshot: {screenshot_path}"
            raise HTTPException(status_code=500, detail=detail)

        finally:
            await browser.close()
