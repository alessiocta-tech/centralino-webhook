import os
import re
import json
from datetime import datetime, timedelta
from typing import Optional, List, Any, Dict
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
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

app = FastAPI(title="centralino-webhook", version="V13-raw-body")


# ============================================================
# RAW BODY PARSER (NO 422)
# ============================================================

async def get_payload_any(request: Request) -> Dict[str, Any]:
    """
    Accetta QUALSIASI body:
    - application/json
    - application/x-www-form-urlencoded
    - text/plain
    Ritorna sempre un dict (mai eccezioni -> mai 422).
    """
    raw = await request.body()
    if not raw:
        return {}

    # prova JSON
    try:
        data = json.loads(raw.decode("utf-8", errors="ignore"))
        if isinstance(data, dict):
            return data
        # se è lista/altro, lo incapsulo
        return {"_raw": data}
    except Exception:
        pass

    # prova form-urlencoded
    try:
        qs = parse_qs(raw.decode("utf-8", errors="ignore"))
        out = {}
        for k, v in qs.items():
            out[k] = v[0] if isinstance(v, list) and v else v
        if out:
            return out
    except Exception:
        pass

    # fallback: testo
    try:
        txt = raw.decode("utf-8", errors="ignore").strip()
        return {"_raw_text": txt}
    except Exception:
        return {}


# ============================================================
# HELPERS
# ============================================================

def calcola_pasto(orario_str: str) -> str:
    try:
        hh = int(orario_str.split(":")[0])
        return "PRANZO" if hh < 17 else "CENA"
    except Exception:
        return "CENA"


def get_data_type(data_str: str) -> str:
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


def normalize_orario(s: Optional[str]) -> str:
    s = (s or "").strip().lower().replace("ore", "").replace("alle", "").strip()
    s = s.replace(".", ":").replace(",", ":")
    if re.fullmatch(r"\d{1,2}$", s):
        return f"{int(s):02d}:00"
    if re.fullmatch(r"\d{1,2}:\d{2}$", s):
        hh, mm = s.split(":")
        return f"{int(hh):02d}:{int(mm):02d}"
    if re.fullmatch(r"\d{1,2}:\d{2}:\d{2}$", s):
        hh, mm, _ = s.split(":")
        return f"{int(hh):02d}:{int(mm):02d}"
    return s


def normalize_sede(s: Optional[str]) -> str:
    s0 = (s or "").strip().lower()
    mapping = {
        "talenti": "Talenti - Roma",
        "talenti - roma": "Talenti - Roma",
        "ostia": "Ostia Lido",
        "ostia lido": "Ostia Lido",
        "appia": "Appia",
        "reggio": "Reggio Calabria",
        "reggio calabria": "Reggio Calabria",
        "palermo": "Palermo",
    }
    return mapping.get(s0, (s or "").strip())


def merge_nome_cognome(nome: str, cognome: Optional[str]) -> str:
    nome = (nome or "").strip()
    cognome = (cognome or "").strip()
    if nome and cognome:
        return f"{nome} {cognome}"
    return nome or cognome or ""


def is_valid_date_yyyy_mm_dd(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except Exception:
        return False


def validate_phone_basic(s: str) -> bool:
    x = re.sub(r"\s+", "", (s or ""))
    if x.startswith("+"):
        x = x[1:]
    return bool(re.fullmatch(r"\d{8,15}", x))


def validate_email_basic(s: str) -> bool:
    if not s:
        return False
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s.strip()))


def normalize_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    # accetta vari alias
    nome = data.get("nome") or data.get("Nome") or ""
    cognome = data.get("cognome") or data.get("Cognome") or ""

    email = data.get("email") or data.get("Email") or ""
    telefono = data.get("telefono") or data.get("Telefono") or ""

    sede = data.get("sede") or data.get("Sede") or ""
    data_pren = data.get("data") or data.get("Data") or data.get("data_prenotazione") or ""

    orario_raw = data.get("orario") or data.get("ora") or data.get("Ora") or ""
    orario = normalize_orario(orario_raw)

    persone = data.get("persone") or data.get("Persone") or data.get("pax") or 0
    try:
        persone = int(persone)
    except Exception:
        persone = 0

    note = data.get("note") or data.get("nota") or data.get("Nota") or ""
    note = (note or "").strip()

    dry_run = data.get("dry_run") or data.get("dryRun") or False
    dry_run = str(dry_run).lower() in ("1", "true", "yes", "y", "ok")

    full_name = merge_nome_cognome(str(nome), str(cognome))

    return {
        "full_name": full_name,
        "email": str(email).strip(),
        "telefono": str(telefono).strip(),
        "sede": normalize_sede(str(sede)),
        "data": str(data_pren).strip(),
        "orario": orario,
        "persone": persone,
        "note": note,
        "dry_run": dry_run,
    }


# ============================================================
# PLAYWRIGHT HELPERS (dal tuo V12)
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


async def click_sede(page, sede_target: str) -> bool:
    await page.wait_for_timeout(1200)

    # 1) prova match pieno
    try:
        loc = page.get_by_text(sede_target, exact=False).first
        if await loc.count() > 0:
            await loc.click(timeout=6000, force=True)
            return True
    except Exception:
        pass

    # 2) fallback sul primo token ("Talenti", "Ostia", "Reggio", etc.)
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


async def click_orario(page, orario_hhmm: str) -> bool:
    await page.wait_for_timeout(1200)
    try:
        sel = page.locator("select").first
        if await sel.count() > 0:
            # tenta select_option su value "13:00:00" (prefisso HH:MM)
            try:
                await sel.select_option(value=re.compile(rf"^{re.escape(orario_hhmm)}"))
                return True
            except Exception:
                pass
    except Exception:
        pass

    # fallback click testo
    for c in [f"text=/{re.escape(orario_hhmm)}/", f"text={orario_hhmm}"]:
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


async def fill_final_form(page, full_name: str, email: str, telefono: str, note: str):
    await page.wait_for_timeout(1200)
    parts = (full_name or "").strip().split(" ", 1)
    n = parts[0] if parts else full_name
    c = parts[1] if len(parts) > 1 else "Cliente"

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

    await try_fill_any(['input[name="Nome"]', 'input[name="nome"]', 'input#Nome', 'input#nome'], n)
    await try_fill_any(['input[name="Cognome"]', 'input[name="cognome"]', 'input#Cognome', 'input#cognome'], c)
    await try_fill_any(['input[name="Email"]', 'input[name="email"]', 'input[type="email"]', 'input#Email'], email)
    await try_fill_any(['input[name="Telefono"]', 'input[name="telefono"]', 'input[type="tel"]', 'input#Telefono'], telefono)

    if note:
        await try_fill_any(['textarea[name="Nota"]', 'textarea[name="note"]', 'textarea', 'textarea#Nota'], note)

    # check consensi
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
    return {"status": "Centralino AI - Booking Engine (raw-body, no 422)"}


@app.post("/check_availability")
async def check_availability(request: Request):
    # compat: mai 422
    _ = await get_payload_any(request)
    return {
        "status": "DISABLED",
        "message": "check_availability disabilitato: usare book_table."
    }


@app.post("/book_table")
async def book_table(request: Request):
    # 1) payload grezzo (mai 422)
    raw_data = await get_payload_any(request)
    payload = normalize_payload(raw_data)

    print("📩 Incoming raw payload:", raw_data)
    print("🧩 Normalized payload:", payload)

    # 2) validazioni soft (ritorno 200, niente 422)
    missing = []
    if not payload["full_name"]:
        missing.append("nome+cognome")
    if not validate_email_basic(payload["email"]):
        missing.append("email")
    if not validate_phone_basic(payload["telefono"]):
        missing.append("telefono")
    if not payload["sede"]:
        missing.append("sede")
    if not is_valid_date_yyyy_mm_dd(payload["data"]):
        missing.append("data (YYYY-MM-DD)")
    if not re.fullmatch(r"\d{2}:\d{2}", payload["orario"]):
        missing.append("ora (HH:MM)")
    if payload["persone"] < 1:
        missing.append("persone")

    if missing:
        return {
            "status": "INVALID",
            "message": f"Dati mancanti/non validi: {', '.join(missing)}",
            "alternatives": []
        }

    if payload["persone"] > 9:
        return {
            "status": "TRANSFER",
            "message": "Per tavolate superiori a 9 persone contatta il centralino.",
            "alternatives": []
        }

    orario = payload["orario"]
    pasto = calcola_pasto(orario)

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

            # 1) Persone
            await page.wait_for_timeout(800)
            await click_exact_number(page, payload["persone"])

            # 2) Seggiolini NO
            await page.wait_for_timeout(700)
            await click_seggiolini_no(page)

            # 3) Data
            await set_date(page, payload["data"])

            # 4) Pasto
            await click_pasto(page, pasto)

            # 5) Sede
            ok_sede = await click_sede(page, payload["sede"])
            if not ok_sede:
                return {
                    "status": "SEDE_NOT_FOUND",
                    "message": f"Sede non trovata: {payload['sede']}",
                    "alternatives": []
                }

            # 6) Orario
            ok_orario = await click_orario(page, orario)
            if not ok_orario:
                return {
                    "status": "NOT_AVAILABLE",
                    "message": "Orario non disponibile.",
                    "alternatives": []
                }

            # 7) Conferma
            await page.wait_for_timeout(800)
            await click_conferma(page)

            # 8) Form
            await fill_final_form(page, payload["full_name"], payload["email"], payload["telefono"], payload["note"])

            if payload["dry_run"] or DISABLE_FINAL_SUBMIT:
                return {
                    "status": "OK_DRY_RUN",
                    "message": "Form compilato (dry-run, submit non inviato).",
                    "alternatives": []
                }

            ok_submit = await click_prenota(page)
            if not ok_submit:
                return {
                    "status": "ERROR",
                    "message": "Bottone PRENOTA non trovato.",
                    "alternatives": []
                }

            await page.wait_for_timeout(1500)
            return {
                "status": "OK",
                "message": f"Prenotazione inviata: {payload['sede']} {payload['data']} {orario} per {payload['persone']} persone.",
                "alternatives": []
            }

        except Exception as e:
            try:
                ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
                path = f"booking_error_{ts}.png"
                await page.screenshot(path=path, full_page=True)
                print(f"📸 Screenshot salvato: {path}")
            except Exception:
                pass

            return {
                "status": "ERROR",
                "message": f"Errore prenotazione: {str(e)}",
                "alternatives": []
            }
        finally:
            await browser.close()
