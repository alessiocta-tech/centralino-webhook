# main.py
import os
import re
import asyncio
from datetime import datetime, timedelta, date as dt_date
from typing import Optional, Tuple

from fastapi import FastAPI
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
import uvicorn

app = FastAPI()

FIDY_URL = os.environ.get("FIDY_URL", "https://rione.fidy.app/prenew.php?referer=butt")

# =========================
# MODELS
# =========================
class RichiestaControllo(BaseModel):
    data: str = Field(..., description="YYYY-MM-DD")
    persone: str = Field(..., description="Numero persone (1..9)")
    orario: Optional[str] = Field(None, description="HH:MM (opzionale, solo per calcolo pasto)")

class RichiestaPrenotazione(BaseModel):
    data: str = Field(..., description="YYYY-MM-DD")
    persone: str = Field(..., description="Numero persone (1..9)")
    orario: str = Field(..., description="HH:MM")
    nome: str = Field(..., description="Nome e Cognome")
    telefono: str = Field(..., description="Numero (max 10 cifre, come da form)")
    email: str
    sede: str = Field(..., description="Talenti, Appia, Ostia, Reggio, Palermo")
    note: str = ""
    seggiolini: Optional[int] = Field(None, description="Numero seggiolini. Se None: auto da note (0 o 1).")

# =========================
# HEALTH
# =========================
@app.get("/")
def home():
    return {"status": "Centralino AI - Fidy Booking (robusto: spans/selectors, time logic, retries)"}

@app.get("/health")
def health():
    return {"ok": True}

# =========================
# HELPERS: NORMALIZZAZIONE
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
    """Return minutes from midnight. Accepts HH:MM or H:MM or HH.MM"""
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
    """
    Fasce RIGOROSE:
    - PRANZO: 12:00 - 14:30
    - CENA  : 19:00 - 22:00
    Se fuori fascia: fallback ragionato (prima delle 17 => PRANZO, altrimenti CENA),
    ma segnaliamo in log (perché potrebbe non avere slot).
    """
    mins = parse_time_hhmm(orario)
    pranzo_start = 12 * 60
    pranzo_end = 14 * 60 + 30
    cena_start = 19 * 60
    cena_end = 22 * 60

    if pranzo_start <= mins <= pranzo_end:
        return "PRANZO"
    if cena_start <= mins <= cena_end:
        return "CENA"

    # fallback: non blocco, ma cerco di far funzionare il flusso
    return "PRANZO" if mins < (17 * 60) else "CENA"

def safe_digits_phone(phone: str) -> str:
    p = re.sub(r"\D+", "", phone or "")
    return p[:10]  # il form ha maxlength=10

# =========================
# PLAYWRIGHT: UTILITIES
# =========================
async def log_step(msg: str) -> None:
    print(msg, flush=True)

async def wait_visible(locator, timeout_ms: int = 30000):
    await locator.wait_for(state="visible", timeout=timeout_ms)

async def click(locator, label: str, timeout_ms: int = 30000, force: bool = False):
    await log_step(f"      -> click: {label}")
    await locator.wait_for(state="visible", timeout=timeout_ms)
    try:
        await locator.scroll_into_view_if_needed()
    except Exception:
        pass
    await locator.click(timeout=timeout_ms, force=force)

async def accept_cookies_if_any(page):
    # generico: se compare un bottone "accetta/ok/consenti" lo clicco
    for patt in [r"accetta", r"ok", r"consenti", r"consent", r"accept"]:
        loc = page.locator(f"text=/{patt}/i").first
        try:
            if await loc.count() > 0 and await loc.is_visible():
                await click(loc, f"cookie_{patt}", timeout_ms=3000, force=True)
                break
        except Exception:
            pass

async def ensure_app_ready(page):
    # L'intro fa fadeOut ~1.5s e poi mostra .stepCont
    await wait_visible(page.locator(".stepCont"), timeout_ms=60000)

# =========================
# PLAYWRIGHT: FLOW STEPS (basati sull'HTML REALE)
# =========================
async def step_select_persone(page, persone: str):
    p = (persone or "").strip()
    await log_step("-> 1. Persone")
    await ensure_app_ready(page)

    # Gli elementi sono <span class="nCoperti" rel="2">2</span>
    loc = page.locator(f".nCoperti[rel='{p}']").first
    if await loc.count() == 0:
        # fallback sul testo
        loc = page.locator(".nCoperti").filter(has_text=re.compile(rf"^\s*{re.escape(p)}\s*$")).first
    await click(loc, f"persone={p}")

async def step_select_seggiolini(page, seggiolini: int):
    await log_step("-> 2. Seggiolini")
    await ensure_app_ready(page)

    # Se seggiolini == 0: lascia NO (è già selezionato di default), ma clicco per sicurezza
    if seggiolini <= 0:
        loc = page.locator(".SeggNO").first
        await click(loc, "seggiolini=NO")
        return

    # Se seggiolini > 0: clicco SI (apre la lista), poi scelgo nSeggiolini[rel='X']
    si = page.locator(".SeggSI, .seggioliniTxt").first
    await click(si, "seggiolini=SI")

    # La lista appare su .seggioliniCont e items .nSeggiolini[rel='1']
    await wait_visible(page.locator(".seggioliniCont"), timeout_ms=20000)
    item = page.locator(f".nSeggiolini[rel='{seggiolini}']").first
    if await item.count() == 0:
        # fallback: se non esiste quel numero, prendo 1
        item = page.locator(".nSeggiolini[rel='1']").first
    await click(item, f"n_seggiolini={seggiolini}")

async def step_select_data(page, data_str: str):
    await log_step("-> 3. Data")
    await ensure_app_ready(page)

    # La data è in step2 con span.dataOggi.dataBtn rel="YYYY-MM-DD"
    target = datetime.strptime(data_str, "%Y-%m-%d").date()

    # Attendo che step2 sia visibile (dopo persone / seggiolini)
    await wait_visible(page.locator(".step2"), timeout_ms=30000)

    # Provo direttamente rel
    rel_loc = page.locator(f".step2 .dataBtn[rel='{data_str}']").first
    if await rel_loc.count() > 0:
        await click(rel_loc, f"data={data_str}")
        return

    # Altra data: clicca e setta input#DataPren + trigger change (jQuery ascolta change)
    altra = page.locator(".step2 .altraData").first
    await click(altra, "data=altra_data")

    inp = page.locator("#DataPren").first
    await inp.wait_for(state="attached", timeout=15000)
    await inp.fill(data_str)
    # Trigger change
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

    # step3 contiene <span class="tipoBtn" rel="PRANZO">PRANZO</span>
    await wait_visible(page.locator(".step3"), timeout_ms=30000)

    loc = page.locator(f".step3 .tipoBtn[rel='{pasto}']").first
    if await loc.count() == 0:
        loc = page.locator(".step3 .tipoBtn").filter(has_text=re.compile(rf"^{pasto}$", re.I)).first
    await click(loc, f"pasto={pasto}")

async def step_select_sede(page, sede_label: str):
    """
    Dopo aver scelto PRANZO/CENA, step4 carica via:
      $('.ristoCont').load('prenew_rist.php?l=...&d=...&t=...')
    Quindi dobbiamo:
    - aspettare che .ristoCont sia visibile
    - aspettare che compaia il testo della sede
    - cliccare sul "card" (o sul testo stesso come fallback)
    """
    await log_step(f"-> 5. Sede ({sede_label})")
    await ensure_app_ready(page)

    # step4 e ristoCont
    await wait_visible(page.locator(".step4"), timeout_ms=30000)
    risto = page.locator(".ristoCont").first
    await wait_visible(risto, timeout_ms=30000)

    # Attendo che sparisca lo spinner oppure che ci sia contenuto testuale utile
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

    # Trovo la sede
    sede_text = page.locator(".ristoCont").get_by_text(re.compile(re.escape(sede_label), re.I)).first
    await wait_visible(sede_text, timeout_ms=30000)

    # Provo a cliccare un contenitore "card" (div) vicino al testo.
    # Nello screenshot sembra una riga grande cliccabile.
    # Strategia: risalgo di qualche ancestor e provo click; se fallisce, clicco il testo.
    candidates = [
        sede_text.locator("xpath=ancestor::a[1]"),
        sede_text.locator("xpath=ancestor::div[contains(@class,'card')][1]"),
        sede_text.locator("xpath=ancestor::div[1]"),
        sede_text,
    ]

    clicked = False
    for i, cand in enumerate(candidates):
        try:
            if await cand.count() > 0:
                await click(cand.first, f"sede_click_candidate_{i}", timeout_ms=20000, force=True)
                clicked = True
                break
        except Exception:
            continue

    if not clicked:
        await click(sede_text, "sede_click_fallback", timeout_ms=20000, force=True)

async def step_select_orario_and_confirm(page, orario: str, note: str):
    await log_step(f"-> 6. Orario ({orario})")
    await ensure_app_ready(page)

    # step5 appare dopo selezione sede
    await wait_visible(page.locator(".step5"), timeout_ms=40000)

    # select#OraPren popolato dinamicamente
    select = page.locator("#OraPren").first
    await wait_visible(select, timeout_ms=20000)

    # attendo opzioni > 1 (c'è sempre la placeholder)
    await page.wait_for_function(
        """() => {
            const s = document.querySelector('#OraPren');
            return s && s.options && s.options.length > 1;
        }""",
        timeout=30000,
    )

    orario_clean = (orario or "").strip().replace(".", ":")
    # Provo 3 strategie: match exact value, match prefix, match label
    selected = False
    try:
        await select.select_option(value=orario_clean)
        selected = True
    except Exception:
        pass

    if not selected:
        # prova a trovare un option il cui value inizia con "HH:MM"
        try:
            await page.evaluate(
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
            selected = True
        except Exception:
            pass

    if not selected:
        # fallback: cerco per testo dell'option
        try:
            await page.evaluate(
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
            selected = True
        except Exception:
            pass

    if not selected:
        raise RuntimeError(f"Impossibile selezionare orario {orario_clean} (nessuna option compatibile)")

    # note (textarea#Nota)
    if note:
        try:
            await page.locator("#Nota").fill(note)
        except Exception:
            pass

    # click CONFERMA (a.confDati)
    await log_step("-> 7. Conferma dati (vai a schermata finale)")
    conf = page.locator("a.confDati").first
    await click(conf, "CONFIRMA", timeout_ms=20000, force=True)

async def step_fill_final_and_submit(page, nome: str, email: str, telefono: str, dry_run: bool):
    await log_step("-> 8. Dati finali")
    await ensure_app_ready(page)

    # stepDati contiene il form
    await wait_visible(page.locator(".stepDati"), timeout_ms=30000)

    first, last = split_name(nome)
    await page.locator("#Nome").fill(first)
    await page.locator("#Cognome").fill(last)
    await page.locator("#Email").fill(email)
    await page.locator("#Telefono").fill(safe_digits_phone(telefono))

    if dry_run:
        await log_step("-> ✅ DRY RUN: non clicco PRENOTA (simulazione ok)")
        return

    await log_step("-> ✅ PRODUZIONE: click PRENOTA")
    submit = page.locator("#prenoForm input[type='submit'][value='PRENOTA']").first
    await click(submit, "PRENOTA", timeout_ms=30000, force=True)

    # Dopo submit, la pagina carica prenew_res.php dentro .stepCont
    # Aspetto un minimo segnale di esito (testo "OK" non lo vediamo perché è ajax),
    # ma possiamo aspettare che cambi contenuto della .stepCont oppure compaia qualche testo di conferma.
    try:
        await page.wait_for_function(
            """() => {
                const c = document.querySelector('.stepCont');
                if(!c) return false;
                const t = (c.innerText || '').toLowerCase();
                return t.includes('prenot') || t.includes('conferm') || t.includes('grazie');
            }""",
            timeout=20000,
        )
    except Exception:
        # Non sempre compare testo “classico”: non blocco.
        pass

# =========================
# CORE RUNNER
# =========================
async def run_booking_flow(dati: RichiestaPrenotazione, dry_run: bool) -> dict:
    sede_label = normalize_sede(dati.sede)
    pasto = get_pasto_strict(dati.orario)

    # seggiolini: priorità a dati.seggiolini, altrimenti auto da note
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
            args=[
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 390, "height": 844},
            user_agent="Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari",
        )
        page = await context.new_page()
        page.set_default_timeout(60000)

        try:
            await log_step("-> GO TO FIDY")
            await page.goto(FIDY_URL, wait_until="domcontentloaded", timeout=60000)
            await accept_cookies_if_any(page)

            await step_select_persone(page, dati.persone)
            await step_select_seggiolini(page, seggiolini_n)
            await step_select_data(page, dati.data)
            await step_select_pasto(page, pasto)
            await step_select_sede(page, sede_label)
            await step_select_orario_and_confirm(page, dati.orario, dati.note)
            await step_fill_final_and_submit(page, dati.nome, dati.email, dati.telefono, dry_run=dry_run)

            return {
                "ok": True,
                "result": f"Prenotazione {'SIMULATA' if dry_run else 'INVIATA'} per {dati.nome}: {sede_label} - {pasto} - {dati.data} {dati.orario} - {dati.persone} pax",
                "debug": {
                    "sede": sede_label,
                    "pasto": pasto,
                    "seggiolini": seggiolini_n,
                    "dry_run": dry_run,
                    "url_finale": page.url,
                },
            }

        except PWTimeout as e:
            # screenshot su timeout (molto utile su Railway)
            try:
                path = "/tmp/timeout.png"
                await page.screenshot(path=path, full_page=True)
                await log_step(f"📸 Screenshot timeout salvato: {path}")
            except Exception:
                pass
            return {"ok": False, "result": f"Timeout Playwright: {str(e)}"}

        except Exception as e:
            try:
                path = "/tmp/error.png"
                await page.screenshot(path=path, full_page=True)
                await log_step(f"📸 Screenshot errore salvato: {path}")
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
    """
    Check leggero:
    - seleziona persone
    - seggiolini NO
    - data
    - pasto (se orario dato, altrimenti prova PRANZO come default)
    - verifica che in .ristoCont compaia almeno una sede (non "vuoto")
    """
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

            # ora dovrebbe caricare ristoCont con le sedi disponibili
            risto = page.locator(".ristoCont").first
            await wait_visible(risto, timeout_ms=30000)

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
    """
    dry_run=0 => PRODUZIONE (clic PRENOTA)
    dry_run=1 => SIMULAZIONE (arriva alla schermata finale e compila i campi, ma NON invia)
    """
    return await run_booking_flow(dati, dry_run=bool(int(dry_run)))

# =========================
# LOCAL RUN
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
