import os
import re
from datetime import datetime, timedelta
from typing import Optional, Union, List, Dict, Any

from fastapi import FastAPI
from pydantic import BaseModel, Field, root_validator
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

def _norm_orario(s: str) -> str:
    s = (s or "").strip().lower().replace("ore", "").replace("alle", "").strip()
    s = s.replace(".", ":").replace(",", ":")
    if re.fullmatch(r"\d{1,2}$", s):
        return f"{int(s):02d}:00"
    if re.fullmatch(r"\d{1,2}:\d{2}$", s):
        hh, mm = s.split(":")
        return f"{int(hh):02d}:{int(mm):02d}"
    return s


def _calcola_pasto(orario_hhmm: str) -> str:
    try:
        hh = int(orario_hhmm.split(":")[0])
        return "PRANZO" if hh < 17 else "CENA"
    except Exception:
        return "CENA"


def _get_data_type(data_str: str) -> str:
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


def _normalize_sede(s: str) -> str:
    s0 = (s or "").strip().lower()
    mapping = {
        "talenti": "Talenti - Roma",
        "talenti - roma": "Talenti - Roma",
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


class RichiestaPrenotazione(BaseModel):
    nome: str
    email: str
    telefono: str

    sede: str
    data: str  # YYYY-MM-DD
    orario: str  # HH:MM
    persone: Union[int, str] = Field(...)

    note: Optional[str] = ""

    @root_validator(pre=True)
    def _coerce_fields(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        p = values.get("persone")
        if isinstance(p, str):
            p2 = re.sub(r"[^\d]", "", p)
            if p2:
                values["persone"] = int(p2)

        if "orario" in values and values["orario"] is not None:
            values["orario"] = _norm_orario(str(values["orario"]))

        if "sede" in values and values["sede"] is not None:
            values["sede"] = _normalize_sede(str(values["sede"]))

        if "telefono" in values and values["telefono"] is not None:
            t = re.sub(r"[^\d]", "", str(values["telefono"]))
            values["telefono"] = t

        return values


# ============================================================
# PLAYWRIGHT HELPERS
# ============================================================

async def _block_heavy(route):
    if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
        await route.abort()
    else:
        await route.continue_()


async def _maybe_click_cookie(page):
    for patt in [r"accetta", r"consent", r"ok", r"accetto"]:
        try:
            loc = page.locator(f"text=/{patt}/i").first
            if await loc.count() > 0:
                await loc.click(timeout=1500, force=True)
                return
        except Exception:
            pass


async def _wait_ready(page):
    await page.wait_for_selector(".nCoperti", state="visible", timeout=PW_TIMEOUT_MS)


async def _click_persone(page, n: int):
    loc = page.locator(f'.nCoperti[rel="{n}"]').first
    if await loc.count() == 0:
        loc = page.get_by_text(str(n), exact=True).first
    await loc.click(timeout=8000, force=True)


async def _click_seggiolini_no(page):
    try:
        no_btn = page.locator(".SeggNO").first
        if await no_btn.count() > 0 and await no_btn.is_visible():
            await no_btn.click(timeout=4000, force=True)
            return
        tno = page.locator("text=/^\\s*NO\\s*$/i").first
        if await tno.count() > 0 and await tno.is_visible():
            await tno.click(timeout=4000, force=True)
    except Exception:
        pass


async def _set_date(page, data_iso: str):
    tipo = _get_data_type(data_iso)

    if tipo in ["Oggi", "Domani"]:
        btn = page.locator(f'.dataBtn[rel="{data_iso}"]').first
        if await btn.count() > 0:
            await btn.click(timeout=6000, force=True)
            return
        t = page.locator(f"text=/{tipo}/i").first
        if await t.count() > 0:
            await t.click(timeout=6000, force=True)
            return

    await page.evaluate(
        """(val) => {
          const el = document.querySelector('#DataPren') || document.querySelector('input[type="date"]');
          if (!el) return false;
          el.value = val;
          el.dispatchEvent(new Event('change', { bubbles: true }));
          return true;
        }""",
        data_iso,
    )


async def _click_pasto(page, pasto: str):
    loc = page.locator(f'.tipoBtn[rel="{pasto}"]').first
    if await loc.count() > 0:
        await loc.click(timeout=8000, force=True)
        return
    await page.locator(f"text=/{pasto}/i").first.click(timeout=8000, force=True)


async def _available_sedi(page) -> List[str]:
    try:
        nodes = page.locator(".ristoBtn")
        cnt = await nodes.count()
        out = []
        for i in range(cnt):
            txt = (await nodes.nth(i).inner_text()).strip()
            first_line = txt.splitlines()[0].strip()
            if first_line and first_line not in out:
                out.append(first_line)
        return out
    except Exception:
        return []


def _match_sede_text(sede_target: str) -> List[str]:
    base = sede_target.strip()
    parts = [p.strip() for p in re.split(r"[-–]", base) if p.strip()]
    cands = [base] + parts
    seen = set()
    out = []
    for c in cands:
        k = c.lower()
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


async def _click_sede(page, sede_target: str):
    await page.wait_for_selector(".ristoBtn", state="visible", timeout=PW_TIMEOUT_MS)

    for cand in _match_sede_text(sede_target):
        loc = page.locator(".ristoBtn", has_text=cand).first
        if await loc.count() > 0:
            await loc.click(timeout=10000, force=True)
            return

    avail = await _available_sedi(page)
    raise RuntimeError(f"Sede non trovata: '{sede_target}'. Disponibili: {', '.join(avail) if avail else 'N/D'}")


async def _select_orario(page, orario_hhmm: str):
    await page.wait_for_selector("#OraPren", state="visible", timeout=PW_TIMEOUT_MS)

    wanted = orario_hhmm.strip()
    wanted_val = wanted + ":00" if re.fullmatch(r"\d{2}:\d{2}", wanted) else wanted

    await page.wait_for_function(
        """() => {
          const sel = document.querySelector('#OraPren');
          return sel && sel.options && sel.options.length > 1;
        }""",
        timeout=PW_TIMEOUT_MS,
    )

    try:
        res = await page.locator("#OraPren").select_option(value=wanted_val)
        if res:
            return
    except Exception:
        pass

    try:
        await page.evaluate(
            """(hhmm) => {
              const sel = document.querySelector('#OraPren');
              if (!sel) return false;
              const opt = Array.from(sel.options).find(o => (o.textContent || '').includes(hhmm));
              if (!opt) return false;
              sel.value = opt.value;
              sel.dispatchEvent(new Event('change', { bubbles: true }));
              return true;
            }""",
            wanted,
        )
        val = await page.locator("#OraPren").input_value()
        if val and val != "":
            return
    except Exception:
        pass

    opts = await page.evaluate(
        """() => {
          const sel = document.querySelector('#OraPren');
          if (!sel) return [];
          return Array.from(sel.options).map(o => ({value:o.value, text:(o.textContent||'').trim(), disabled:o.disabled}));
        }"""
    )
    raise RuntimeError(f"Orario non disponibile: {wanted}. Opzioni: {opts}")


async def _fill_note_step5(page, note: str):
    note = (note or "").strip()
    if not note:
        return
    try:
        await page.wait_for_selector("#Nota", state="visible", timeout=PW_TIMEOUT_MS)
        await page.locator("#Nota").fill(note, timeout=8000)
    except Exception:
        pass


async def _click_conferma(page):
    loc = page.locator(".confDati").first
    if await loc.count() > 0:
        await loc.click(timeout=8000, force=True)
        return
    await page.locator("text=/CONFERMA/i").first.click(timeout=8000, force=True)


async def _fill_form(page, nome: str, email: str, telefono: str):
    parti = (nome or "").strip().split(" ", 1)
    nome1 = parti[0] if parti else (nome or "Cliente")
    cognome = parti[1] if len(parti) > 1 else "Cliente"

    await page.wait_for_selector("#prenoForm", state="visible", timeout=PW_TIMEOUT_MS)

    await page.locator("#Nome").fill(nome1, timeout=8000)
    await page.locator("#Cognome").fill(cognome, timeout=8000)
    await page.locator("#Email").fill(email, timeout=8000)
    await page.locator("#Telefono").fill(telefono, timeout=8000)


async def _click_prenota(page):
    loc = page.locator('input[type="submit"][value="PRENOTA"]').first
    if await loc.count() > 0:
        await loc.click(timeout=15000, force=True)
        return
    await page.locator("text=/PRENOTA/i").last.click(timeout=15000, force=True)


# ============================================================
# ROUTES
# ============================================================

@app.get("/")
def home():
    return {"status": "Centralino AI - Booking Engine (Railway)"}


@app.post("/check_availability")
async def check_availability_compat(payload: Dict[str, Any]):
    return {"ok": True, "message": "check_availability disabilitato: usa /book_table", "received": payload}


@app.post("/checkavailability")
async def checkavailability_compat(payload: Dict[str, Any]):
    return {"ok": True, "message": "check_availability disabilitato: usa /book_table", "received": payload}


@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dati.data or ""):
        return {"ok": False, "message": f"Formato data non valido: {dati.data}. Usa YYYY-MM-DD."}
    if not re.fullmatch(r"\d{2}:\d{2}", dati.orario or ""):
        return {"ok": False, "message": f"Formato orario non valido: {dati.orario}. Usa HH:MM (es. 13:00)."}
    if not isinstance(dati.persone, int) or dati.persone < 1 or dati.persone > 50:
        return {"ok": False, "message": f"Numero persone non valido: {dati.persone}."}

    sede_target = dati.sede
    orario = dati.orario
    pasto = _calcola_pasto(orario)

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
        await page.route("**/*", _block_heavy)

        try:
            await page.goto(BOOKING_URL, wait_until="domcontentloaded")
            await _maybe_click_cookie(page)
            await _wait_ready(page)

            await _click_persone(page, int(dati.persone))
            await _click_seggiolini_no(page)

            await _set_date(page, dati.data)
            await _click_pasto(page, pasto)

            await _click_sede(page, sede_target)
            await _select_orario(page, orario)

            # NOTE: qui è il punto giusto (step5)
            await _fill_note_step5(page, dati.note or "")

            await _click_conferma(page)
            await _fill_form(page, dati.nome, dati.email, dati.telefono)

            if DISABLE_FINAL_SUBMIT:
                return {"ok": True, "message": "FORM COMPILATO (test mode, submit disattivato)"}

            await _click_prenota(page)
            await page.wait_for_timeout(1500)

            return {
                "ok": True,
                "message": f"Prenotazione inviata: {dati.persone} pax - {sede_target} {dati.data} {orario} - {dati.nome}",
            }

        except Exception as e:
            try:
                ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
                path = f"booking_error_{ts}.png"
                import base64

img = await page.screenshot(full_page=True)
b64 = base64.b64encode(img).decode()

print("📸 SCREENSHOT_BASE64_START")
print(b64)
print("📸 SCREENSHOT_BASE64_END")

            except Exception:
                path = None

            return {"ok": False, "message": f"Errore prenotazione: {e}", "screenshot": path}
        finally:
            await browser.close()
