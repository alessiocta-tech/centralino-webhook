import os
import re
import asyncio
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field
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
    persone: int = Field(ge=1, le=50)
    orario: Optional[str] = None  # opzionale


class RichiestaPrenotazione(BaseModel):
    nome: str
    email: str
    telefono: str

    sede: str
    data: str  # YYYY-MM-DD
    orario: str  # "13:00" ecc
    persone: int = Field(ge=1, le=50)

    note: Optional[str] = ""


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
    return s


def normalize_sede(s: str) -> str:
    s0 = (s or "").strip().lower()
    mapping = {
        "talenti": "Talenti - Roma",
        "ostia": "Ostia Lido",
        "appia": "Appia",
        "reggio": "Reggio Calabria",
        "reggio calabria": "Reggio Calabria",
        "palermo": "Palermo",
        "palermo centro": "Palermo",
    }
    return mapping.get(s0, s.strip())


async def maybe_click_cookie(page):
    # Cookie/popup: prova varie parole chiave
    for patt in [r"accetta", r"consent", r"ok", r"accetto"]:
        try:
            loc = page.locator(f"text=/{patt}/i").first
            if await loc.count() > 0:
                await loc.click(timeout=2000, force=True)
                return
        except Exception:
            pass


async def click_exact_number(page, n: int):
    # spesso i bottoni persone sono "2", "3", ecc.
    txt = str(n)
    # prova button:text-is('2') e div:text-is('2')
    for sel in [f"button:text-is('{txt}')", f"div:text-is('{txt}')", f"span:text-is('{txt}')"]:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                await loc.click(timeout=3000, force=True)
                return True
        except Exception:
            pass
    # fallback: get_by_text exact
    try:
        await page.get_by_text(txt, exact=True).first.click(timeout=3000, force=True)
        return True
    except Exception:
        return False


async def click_seggiolini_no(page):
    # se esiste step seggiolini, clicca NO
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
    # Step data: Oggi/Domani/Altra data con input date
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

    # Altra data (o fallback)
    # clicca "Altra data" se esiste
    try:
        altra = page.locator("text=/Altra data/i").first
        if await altra.count() > 0:
            await altra.click(timeout=5000, force=True)
    except Exception:
        pass

    await page.wait_for_timeout(600)
    # imposta input[type=date]
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

    # a volte serve "CONFERMA" o "CERCA"
    for patt in [r"conferma", r"cerca", r"ok"]:
        try:
            btn = page.locator(f"text=/{patt}/i").first
            if await btn.count() > 0:
                await btn.click(timeout=2000, force=True)
                break
        except Exception:
            pass


async def click_pasto(page, pasto: str):
    # Step critico: PRANZO / CENA
    await page.wait_for_timeout(1200)
    try:
        btn = page.locator(f"text=/{pasto}/i").first
        if await btn.count() > 0:
            await btn.click(timeout=5000, force=True)
            return True
    except Exception:
        pass
    # se non c’è, spesso è già implicito
    return False


async def click_sede(page, sede_target: str):
    # Step sede: click sul testo della sede (con fallback)
    await page.wait_for_timeout(1200)

    # 1) prova exact-ish
    try:
        loc = page.get_by_text(sede_target, exact=False).first
        if await loc.count() > 0:
            await loc.click(timeout=6000, force=True)
            return True
    except Exception:
        pass

    # 2) fallback: cerca "Talenti" dentro
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
    # Step orario: a volte c'è una select / dropdown
    await page.wait_for_timeout(1200)

    # apri select se c'è
    try:
        sel = page.locator("select").first
        if await sel.count() > 0:
            await sel.click(timeout=2000)
    except Exception:
        pass

    # prova testo esatto "13:00" ecc.
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

    # fallback: prova solo l'ora "13"
    try:
        hh = orario_hhmm.split(":")[0]
        loc = page.locator(f"text=/{hh}/").first
        if await loc.count() > 0:
            await loc.click(timeout=6000, force=True)
            return True
    except Exception:
        pass

    return False


async def click_conferma(page):
    # Step: CONFERMA prima della form finale (se esiste)
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
    # Form finale: spesso ha campi nome/cognome/email/tel e checkbox privacy
    await page.wait_for_timeout(1200)

    # split nome/cognome
    parti = (nome or "").strip().split(" ", 1)
    n = parti[0] if parti else nome
    c = parti[1] if len(parti) > 1 else "Cliente"

    # prova vari selettori possibili
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

    await try_fill_any(['input[name="nome"]', 'input[placeholder*="Nome" i]', 'input#nome'], n)
    await try_fill_any(['input[name="cognome"]', 'input[placeholder*="Cognome" i]', 'input#cognome'], c)
    await try_fill_any(['input[name="email"]', 'input[type="email"]', 'input[placeholder*="Email" i]'], email)
    await try_fill_any(['input[name="telefono"]', 'input[type="tel"]', 'input[placeholder*="Telefono" i]'], telefono)

    if note:
        await try_fill_any(['textarea[name="note"]', 'textarea', 'textarea[placeholder*="note" i]'], note)

    # checkbox privacy/consensi: prova a spuntare tutto ciò che è required/checkbox visibile
    try:
        checkboxes = page.locator('input[type="checkbox"]')
        cnt = await checkboxes.count()
        for i in range(cnt):
            cb = checkboxes.nth(i)
            try:
                # solo se visibile
                if await cb.is_visible():
                    await cb.check(timeout=2000)
            except Exception:
                pass
    except Exception:
        pass


async def click_prenota(page):
    # click finale PRENOTA
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
    return {"status": "Centralino AI - Booking Engine V12 (Full Flow)"}


@app.post("/check_availability")
async def check_availability(dati: RichiestaControllo):
    # Questo endpoint è “light”: entra, fa step fino a data (e volendo pasto/sede)
    sede_target = normalize_sede(dati.sede)
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

        # blocca risorse pesanti
        async def route_handler(route):
            if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
                await route.abort()
            else:
                await route.continue_()
        await page.route("**/*", route_handler)

        try:
            await page.goto(BOOKING_URL, wait_until="domcontentloaded")
            await maybe_click_cookie(page)

            print("-> 1. Persone")
            await page.wait_for_timeout(600)
            await click_exact_number(page, dati.persone)

            print("-> 2. Seggiolini")
            await click_seggiolini_no(page)

            print("-> 3. Data")
            await set_date(page, dati.data)

            if pasto:
                print(f"-> 4. Pasto ({pasto})")
                await click_pasto(page, pasto)

            # Non forzo sede/orario in check: dipende da come vuoi usarlo
            return {"result": f"Step completati (check) per {dati.data}. Se non hai errori, puoi procedere a prenotare."}

        except Exception as e:
            return {"result": f"Errore Check: {e}"}
        finally:
            await browser.close()


@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione):
    # Normalizzazioni
    sede_target = normalize_sede(dati.sede)
    orario = normalize_orario(dati.orario)
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

        # blocca risorse pesanti (come la versione che ti reggeva)
        async def route_handler(route):
            if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
                await route.abort()
            else:
                await route.continue_()
        await page.route("**/*", route_handler)

        try:
            print("-> GO TO FIDY")
            await page.goto(BOOKING_URL, wait_until="domcontentloaded")
            await maybe_click_cookie(page)

            # 1) Persone
            print("-> 1. Persone")
            await page.wait_for_timeout(800)
            ok_people = await click_exact_number(page, dati.persone)
            if not ok_people:
                print("   ⚠️ Non ho trovato il bottone persone, continuo comunque...")

            # 2) Seggiolini (NO)
            print("-> 2. Seggiolini")
            await page.wait_for_timeout(700)
            await click_seggiolini_no(page)

            # 3) Data
            print("-> 3. Data")
            await set_date(page, dati.data)

            # 4) Pasto (PRANZO/CENA)
            print(f"-> 4. Pasto ({pasto})")
            await click_pasto(page, pasto)

            # 5) Sede
            print(f"-> 5. Sede ({sede_target})")
            ok_sede = await click_sede(page, sede_target)
            if not ok_sede:
                raise RuntimeError(f"Sede non trovata/cliccabile: {sede_target}")

            # 6) Orario
            print(f"-> 6. Orario ({orario})")
            ok_orario = await click_orario(page, orario)
            if not ok_orario:
                raise RuntimeError(f"Orario non disponibile: {orario}")

            # 7) Conferma (vai a schermata finale)
            print("-> 7. Conferma dati (vai a schermata finale)")
            await page.wait_for_timeout(800)
            await click_conferma(page)

            # 8) Dati finali (form)
            print("-> 8. Dati finali (form)")
            await fill_final_form(page, dati.nome, dati.email, dati.telefono, dati.note or "")

            # ✅ PRODUZIONE: click PRENOTA
            if DISABLE_FINAL_SUBMIT:
                print("🟡 DISABLE_FINAL_SUBMIT=true -> NON clicco PRENOTA (test mode)")
                return {"result": "FORM COMPILATO (test mode, submit disattivato)"}

            print("✅ PRODUZIONE: click PRENOTA")
            ok_submit = await click_prenota(page)
            if not ok_submit:
                raise RuntimeError("Bottone PRENOTA non trovato")

            # attendo un attimo per eventuale conferma
            await page.wait_for_timeout(1500)

            return {"result": f"Prenotazione inviata per {dati.nome} ({dati.persone} pax) - {sede_target} {dati.data} {orario}"}

        except Exception as e:
            # screenshot di debug (utile su Railway logs/files)
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
