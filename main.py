import os
import re
from datetime import datetime
from typing import Optional, Tuple

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, validator
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
import uvicorn

# >>> VALIDAZIONE TELEFONO (libphonenumber)
import phonenumbers
from phonenumbers.phonenumberutil import NumberParseException
from phonenumbers import number_type, PhoneNumberType

app = FastAPI()

# Usa referer=AI di default (puoi cambiarlo con env var su Railway)
FIDY_URL = os.environ.get("FIDY_URL", "https://rione.fidy.app/prenew.php?referer=AI")


# =========================
# MODELS
# =========================
class RichiestaControllo(BaseModel):
    data: str = Field(..., description="YYYY-MM-DD")
    persone: str = Field(..., description="Numero persone (1..9)")
    orario: Optional[str] = Field(None, description="HH:MM (opzionale)")

class RichiestaPrenotazione(BaseModel):
    data: str = Field(..., description="YYYY-MM-DD")
    persone: str = Field(..., description="Numero persone (1..9)")
    orario: str = Field(..., description="HH:MM")
    nome: str = Field(..., description="Nome e Cognome")
    telefono: str = Field(..., description="Telefono (accetto +39 o formato nazionale)")
    email: str
    sede: str = Field(..., description="Talenti, Appia, Ostia, Reggio, Palermo")
    note: str = ""
    seggiolini: Optional[int] = Field(None, description="Numero seggiolini (0..5). Se None: auto da note.")

    # --- VALIDAZIONE TELEFONO: Italia, plausibile + valido + mobile
    @validator("telefono")
    def validate_telefono(cls, v: str) -> str:
        raw = (v or "").strip()
        if not raw:
            raise ValueError("Telefono mancante")

        # Normalizza: elimina spazi/parentesi/trattini, mantieni + se presente
        cleaned = re.sub(r"[^\d+]", "", raw)

        try:
            # Se non inizia con +, assumiamo Italia
            pn = phonenumbers.parse(cleaned, "IT" if not cleaned.startswith("+") else None)
        except NumberParseException:
            raise ValueError("Telefono non interpretabile (formato non valido)")

        # Controlli robusti
        if not phonenumbers.is_possible_number(pn):
            raise ValueError("Telefono non plausibile (lunghezza/prefisso non compatibili)")
        if not phonenumbers.is_valid_number(pn):
            raise ValueError("Telefono non valido")

        # Forza Italia (evita numeri esteri per errore)
        region = phonenumbers.region_code_for_number(pn)
        if region != "IT":
            raise ValueError("Telefono non italiano (atteso numero IT)")

        # Forza MOBILE (o FIXED_LINE_OR_MOBILE in alcuni casi)
        t = number_type(pn)
        if t not in (PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE):
            raise ValueError("Telefono non cellulare (atteso mobile)")

        # Ritorno la NSN (solo cifre, senza +39), perfetta per maxlength=10 del form
        nsn = str(pn.national_number)
        # Alcuni numeri storici possono essere 9 cifre; il form accetta fino a 10 → ok.
        if len(nsn) > 10:
            raise ValueError("Telefono troppo lungo per il form (max 10 cifre senza +39)")

        return nsn


# =========================
# HEALTH
# =========================
@app.get("/")
def home():
    return {"status": "Centralino AI - Fidy Booking (referer=AI, phone validation, robust selectors)"}

@app.get("/health")
def health():
    return {"ok": True, "fidy_url": FIDY_URL}


# =========================
# HELPERS
# =========================
def normalize_sede(raw: str) -> str:
    s = (raw or "").strip().lower()
    mapping = {
        "talenti": "Talenti - Roma",
        "talenti roma": "Talenti - Roma",
        "roma talenti": "Talenti - Roma",
        "appia": "Appia",
        "ostia": "Ostia Lido",
        "ostia lido": "Ostia Lido",
        "reggio": "Reggio Calabria",
        "reggio calabria": "Reggio Calabria",
        "palermo": "Palermo",
    }
    for k, v in mapping.items():
        if k in s:
            return v
    return raw.strip()

def split_name(full: str) -> Tuple[str, str]:
    parts = (full or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], "."
    return parts[0], " ".join(parts[1:])

def needs_seggiolone_from_note(note: str) -> bool:
    n = (note or "").lower()
    return any(k in n for k in ["seggiolon", "seggiolin", "bimbo", "bambin", "bambina", "baby"])

def parse_time_hhmm(orario: str) -> int:
    o = (orario or "").strip().replace(".", ":")
    m = re.match(r"^\s*(\d{1,2})\s*:\s*(\d{2})\s*$", o)
    if not m:
        raise ValueError(f"Orario non valido: {orario}")
    hh = int(m.group(1))
    mm = int(m.group(2))
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        raise ValueError(f"Orario non valido: {orario}")
    return hh * 60 + mm

def get_pasto_strict(orario: str) -> str:
    mins = parse_time_hhmm(orario)
    # Fasce richieste:
    # PRANZO 12:00-14:30 | CENA 19:00-22:00
    if 12 * 60 <= mins <= (14 * 60 + 30):
        return "PRANZO"
    if 19 * 60 <= mins <= 22 * 60:
        return "CENA"
    # fallback non bloccante
    return "PRANZO" if mins < (17 * 60) else "CENA"


# =========================
# PLAYWRIGHT UTILS
# =========================
async def log_step(msg: str) -> None:
    print(msg, flush=True)

async def accept_cookies_if_any(page):
    for patt in [r"accetta", r"ok", r"consenti", r"consent", r"accept"]:
        loc = page.locator(f"text=/{patt}/i").first
        try:
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=3000, force=True)
                break
        except Exception:
            pass

async def ensure_app_ready(page):
    # dopo fade intro compare .stepCont
    await page.locator(".stepCont").wait_for(state="visible", timeout=60000)

async def click(locator, label: str, timeout_ms: int = 30000, force: bool = True):
    await log_step(f"      -> click: {label}")
    await locator.wait_for(state="visible", timeout=timeout_ms)
    try:
        await locator.scroll_into_view_if_needed()
    except Exception:
        pass
    await locator.click(timeout=timeout_ms, force=force)


# =========================
# FLOW STEPS (aderenti all'HTML reale)
# =========================
async def step_select_persone(page, persone: str):
    await log_step("-> 1. Persone")
    await ensure_app_ready(page)
    p = (persone or "").strip()
    loc = page.locator(f".nCoperti[rel='{p}']").first
    if await loc.count() == 0:
        loc = page.locator(".nCoperti").filter(has_text=re.compile(rf"^\s*{re.escape(p)}\s*$")).first
    await click(loc, f"persone={p}")

async def step_select_seggiolini(page, seggiolini: int):
    await log_step("-> 2. Seggiolini")
    await ensure_app_ready(page)

    if seggiolini <= 0:
        await click(page.locator(".SeggNO").first, "seggiolini=NO")
        return

    await click(page.locator(".SeggSI, .seggioliniTxt").first, "seggiolini=SI")
    await page.locator(".seggioliniCont").wait_for(state="visible", timeout=20000)

    item = page.locator(f".nSeggiolini[rel='{seggiolini}']").first
    if await item.count() == 0:
        item = page.locator(".nSeggiolini[rel='1']").first
    await click(item, f"n_seggiolini={seggiolini}")

async def step_select_data(page, data_str: str):
    await log_step("-> 3. Data")
    await ensure_app_ready(page)
    # valida formato
    datetime.strptime(data_str, "%Y-%m-%d")

    await page.locator(".step2").wait_for(state="visible", timeout=30000)

    rel_loc = page.locator(f".step2 .dataBtn[rel='{data_str}']").first
    if await rel_loc.count() > 0:
        await click(rel_loc, f"data={data_str}")
        return

    await click(page.locator(".step2 .altraData").first, "data=altra_data")
    inp = page.locator("#DataPren").first
    await inp.wait_for(state="attached", timeout=15000)
    await inp.fill(data_str)

    await page.evaluate(
        """(val) => {
            const i = document.querySelector('#DataPren');
            if(!i) return;
            i.value = val;
            i.dispatchEvent(new Event('change', {bubbles:true}));
        }""",
        data_str,
    )

async def step_select_pasto(page, pasto: str):
    await log_step(f"-> 4. Pasto ({pasto})")
    await ensure_app_ready(page)
    await page.locator(".step3").wait_for(state="visible", timeout=30000)
    loc = page.locator(f".step3 .tipoBtn[rel='{pasto}']").first
    if await loc.count() == 0:
        loc = page.locator(".step3 .tipoBtn").filter(has_text=re.compile(rf"^{pasto}$", re.I)).first
    await click(loc, f"pasto={pasto}")

async def step_select_sede(page, sede_label: str):
    await log_step(f"-> 5. Sede ({sede_label})")
    await ensure_app_ready(page)

    await page.locator(".step4").wait_for(state="visible", timeout=30000)
    risto = page.locator(".ristoCont").first
    await risto.wait_for(state="visible", timeout=30000)

    # aspetta contenuto caricato (non solo spinner)
    try:
        await page.wait_for_function(
            """() => {
                const c = document.querySelector('.ristoCont');
                if(!c) return false;
                const txt = (c.innerText || '').trim();
                return txt.length > 0 && !txt.toLowerCase().includes('loading');
            }""",
            timeout=30000,
        )
    except Exception:
        pass

    sede_text = page.locator(".ristoCont").get_by_text(re.compile(re.escape(sede_label), re.I)).first
    await sede_text.wait_for(state="visible", timeout=30000)

    # click su container vicino al testo (con fallback)
    candidates = [
        sede_text.locator("xpath=ancestor::a[1]"),
        sede_text.locator("xpath=ancestor::div[contains(@class,'card')][1]"),
        sede_text.locator("xpath=ancestor::div[1]"),
        sede_text,
    ]
    for i, cand in enumerate(candidates):
        try:
            if await cand.count() > 0:
                await click(cand.first, f"sede_candidate_{i}", timeout_ms=20000, force=True)
                return
        except Exception:
            continue

    await click(sede_text, "sede_fallback", timeout_ms=20000, force=True)

async def step_select_orario_and_confirm(page, orario: str, note: str):
    await log_step(f"-> 6. Orario ({orario})")
    await ensure_app_ready(page)

    await page.locator(".step5").wait_for(state="visible", timeout=40000)

    select = page.locator("#OraPren").first
    await select.wait_for(state="visible", timeout=20000)

    # opzioni caricate
    await page.wait_for_function(
        """() => {
            const s = document.querySelector('#OraPren');
            return s && s.options && s.options.length > 1;
        }""",
        timeout=30000,
    )

    orario_clean = (orario or "").strip().replace(".", ":")
    selected = False

    # 1) select_option value exact
    try:
        await select.select_option(value=orario_clean)
        selected = True
    except Exception:
        pass

    # 2) value startswith HH:MM
    if not selected:
        selected = await page.evaluate(
            """(hhmm) => {
                const s = document.querySelector('#OraPren');
                if(!s) return false;
                const opts = Array.from(s.options || []);
                const found = opts.find(o => (o.value || '').startsWith(hhmm));
                if(found){
                    s.value = found.value;
                    s.dispatchEvent(new Event('change', {bubbles:true}));
                    return true;
                }
                return false;
            }""",
            orario_clean,
        )

    # 3) option text contains HH:MM
    if not selected:
        selected = await page.evaluate(
            """(hhmm) => {
                const s = document.querySelector('#OraPren');
                if(!s) return false;
                const opts = Array.from(s.options || []);
                const found = opts.find(o => (o.textContent || '').includes(hhmm));
                if(found){
                    s.value = found.value;
                    s.dispatchEvent(new Event('change', {bubbles:true}));
                    return true;
                }
                return false;
            }""",
            orario_clean,
        )

    if not selected:
        raise RuntimeError(f"Impossibile selezionare orario {orario_clean} (nessuna option compatibile)")

    if note:
        try:
            await page.locator("#Nota").fill(note)
        except Exception:
            pass

    await log_step("-> 7. Conferma (vai ai dati finali)")
    await click(page.locator("a.confDati").first, "CONFIRMA", timeout_ms=20000, force=True)

async def step_fill_final_and_submit(page, nome: str, email: str, telefono_nsn: str, dry_run: bool):
    await log_step("-> 8. Dati finali")
    await ensure_app_ready(page)
    await page.locator(".stepDati").wait_for(state="visible", timeout=30000)

    first, last = split_name(nome)
    await page.locator("#Nome").fill(first)
    await page.locator("#Cognome").fill(last)
    await page.locator("#Email").fill(email)
    await page.locator("#Telefono").fill(telefono_nsn)

    if dry_run:
        await log_step("-> ✅ DRY RUN: non clicco PRENOTA")
        return

    await log_step("-> ✅ PRODUZIONE: click PRENOTA")
    await click(page.locator("#prenoForm input[type='submit'][value='PRENOTA']").first, "PRENOTA", timeout_ms=30000, force=True)


# =========================
# CORE
# =========================
async def run_booking_flow(dati: RichiestaPrenotazione, dry_run: bool) -> dict:
    sede_label = normalize_sede(dati.sede)
    pasto = get_pasto_strict(dati.orario)

    if dati.seggiolini is not None:
        seggiolini_n = max(0, int(dati.seggiolini))
    else:
        seggiolini_n = 1 if needs_seggiolone_from_note(dati.note) else 0

    await log_step(
        f"🚀 BOOKING: {dati.nome} -> {sede_label} | {dati.data} {dati.orario} | pax={dati.persone} | pasto={pasto} | seggiolini={seggiolini_n} | dry_run={dry_run}"
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        page = await context.new_page()
        page.set_default_timeout(60000)

        try:
            await log_step(f"-> GO TO: {FIDY_URL}")
            await page.goto(FIDY_URL, wait_until="domcontentloaded", timeout=60000)
            await accept_cookies_if_any(page)

            await step_select_persone(page, dati.persone)
            await step_select_seggiolini(page, seggiolini_n)
            await step_select_data(page, dati.data)
            await step_select_pasto(page, pasto)
            await step_select_sede(page, sede_label)
            await step_select_orario_and_confirm(page, dati.orario, dati.note)
            # telefono è già validato e normalizzato dal validator (NSN max 10)
            await step_fill_final_and_submit(page, dati.nome, dati.email, dati.telefono, dry_run=dry_run)

            return {
                "ok": True,
                "result": f"Prenotazione {'SIMULATA' if dry_run else 'INVIATA'} per {dati.nome}: {sede_label} - {pasto} - {dati.data} {dati.orario} - {dati.persone} pax",
                "debug": {"sede": sede_label, "pasto": pasto, "seggiolini": seggiolini_n, "dry_run": dry_run, "url_finale": page.url},
            }

        except PWTimeout as e:
            try:
                await page.screenshot(path="/tmp/timeout.png", full_page=True)
                await log_step("📸 Screenshot timeout: /tmp/timeout.png")
            except Exception:
                pass
            return {"ok": False, "result": f"Timeout Playwright: {str(e)}"}

        except Exception as e:
            try:
                await page.screenshot(path="/tmp/error.png", full_page=True)
                await log_step("📸 Screenshot errore: /tmp/error.png")
            except Exception:
                pass
            return {"ok": False, "result": f"Errore tecnico: {str(e)}"}

        finally:
            await browser.close()


# =========================
# ENDPOINTS
# =========================
@app.post("/check_availability")
async def check_availability(dati: RichiestaControllo):
    pasto = "PRANZO"
    if dati.orario:
        pasto = get_pasto_strict(dati.orario)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        page = await context.new_page()
        page.set_default_timeout(60000)

        try:
            await log_step(f"🔎 CHECK: {dati.persone} pax | {dati.data} | pasto={pasto}")
            await page.goto(FIDY_URL, wait_until="domcontentloaded", timeout=60000)
            await accept_cookies_if_any(page)

            await step_select_persone(page, dati.persone)
            await step_select_seggiolini(page, 0)
            await step_select_data(page, dati.data)
            await step_select_pasto(page, pasto)

            risto = page.locator(".ristoCont").first
            await risto.wait_for(state="visible", timeout=30000)
            txt = (await risto.inner_text()).strip().lower()

            if not txt or "loading" in txt:
                return {"ok": False, "result": "Non riesco a leggere le disponibilità (contenuto non pronto)."}
            if "non ci sono" in txt or "nessun" in txt:
                return {"ok": True, "result": "Sembra pieno (nessuna disponibilità)."}
            return {"ok": True, "result": "Sembra esserci disponibilità (almeno una sede visibile).", "debug": {"pasto": pasto}}

        except Exception as e:
            return {"ok": False, "result": f"Errore: {str(e)}"}
        finally:
            await browser.close()


@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione, dry_run: int = 0):
    # dry_run=0 => PRODUZIONE (clic PRENOTA)
    # dry_run=1 => SIMULAZIONE
    try:
        return await run_booking_flow(dati, dry_run=bool(int(dry_run)))
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
