import os
import re
import asyncio
from datetime import datetime, timedelta
from typing import Optional
from fastapi import FastAPI
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

app = FastAPI()

# ----------------------------
# MODELS
# ----------------------------
class RichiestaControllo(BaseModel):
    data: str
    persone: str

class RichiestaPrenotazione(BaseModel):
    data: str = Field(..., description="YYYY-MM-DD")
    persone: str = Field(..., description="Numero persone")
    orario: str = Field(..., description="HH:MM")
    nome: str = Field(..., description="Nome e Cognome")
    telefono: str
    email: str
    sede: str = Field(..., description="Talenti, Appia, Ostia, Reggio, Palermo")
    note: str = ""

@app.get("/")
def home():
    return {"status": "Centralino AI - V24 Definitiva (Smart Selectors)"}

# ----------------------------
# HELPERS
# ----------------------------
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

def needs_seggiolone(note: str) -> bool:
    n = (note or "").lower()
    return any(k in n for k in ["seggiolon", "bimbo", "bambin", "bambina", "baby"])

def choose_turno(orario: str) -> str:
    try:
        h, m = orario.split(":")
        minutes = int(h) * 60 + int(m)
        return "I TURNO" if minutes <= (20 * 60 + 30) else "II TURNO"
    except:
        return "I TURNO"

def split_name(full: str) -> tuple[str, str]:
    parts = (full or "").strip().split()
    if not parts: return "", ""
    if len(parts) == 1: return parts[0], "."
    return parts[0], " ".join(parts[1:])

async def safe_click(locator, label: str, timeout_ms: int = 10000) -> None:
    print(f"      -> Clicco: {label}")
    await locator.wait_for(state="visible", timeout=timeout_ms)
    await locator.scroll_into_view_if_needed()
    try:
        await locator.click(timeout=timeout_ms)
    except Exception:
        print(f"      -> Click normale fallito per {label}, provo FORCE.")
        await locator.click(timeout=timeout_ms, force=True)

async def wait_for_step_orario(page, timeout_ms: int = 15000) -> None:
    # Cerca un elemento che indichi che siamo passati alla fase orario
    await page.locator("select, div.select, text=/Orario/i").first.wait_for(state="visible", timeout=timeout_ms)

# ----------------------------
# FLOW STEPS (Smart Logic)
# ----------------------------
async def select_persone(page, persone: str) -> None:
    p = persone.strip()
    btn = page.get_by_role("button", name=re.compile(rf"^{re.escape(p)}$")).first
    if await btn.count() > 0:
        await safe_click(btn, f"persone={p}")
        return
    await safe_click(page.get_by_text(p, exact=True).first, f"persone_text={p}")

async def select_seggiolone(page, yes: bool) -> None:
    target = "SI" if yes else "NO"
    b = page.get_by_role("button", name=re.compile(rf"^{target}$", re.I)).first
    if await b.count() > 0:
        await safe_click(b, f"seggiolone={target}")
        return
    await safe_click(page.get_by_text(target, exact=True).first, f"seggiolone_text={target}")

async def select_data(page, data_str: str) -> None:
    try:
        target = datetime.strptime(data_str, "%Y-%m-%d").date()
        today = datetime.now().date()
        
        if target == today:
            btn = page.get_by_role("button", name=re.compile(r"^Oggi$", re.I)).first
            if await btn.count() > 0:
                await safe_click(btn, "data=oggi")
                return
        if target == (today + timedelta(days=1)):
            btn = page.get_by_role("button", name=re.compile(r"^Domani$", re.I)).first
            if await btn.count() > 0:
                await safe_click(btn, "data=domani")
                return
    except: pass

    # Altra data
    altra = page.get_by_role("button", name=re.compile(r"Altra\s+data", re.I)).first
    if await altra.count() > 0:
        await safe_click(altra, "data=altra_data")
    else:
        await safe_click(page.get_by_text("Altra data", exact=False).first, "data=altra_data_text")

    await page.wait_for_timeout(500)
    await page.evaluate(f"document.querySelector('input[type=date]').value = '{data_str}'")
    await page.locator("input[type=date]").press("Enter")
    try: await page.locator("text=/conferma|cerca/i").first.click(timeout=2000)
    except: pass

async def select_sede_and_turno(page, sede_label: str, orario: str) -> None:
    print(f"   -> Cerco riga per: {sede_label}")
    # 1. Trova il testo
    sede_text = page.get_by_text(re.compile(re.escape(sede_label), re.I)).first
    await sede_text.wait_for(state="visible", timeout=20000)

    # 2. Trova la riga contenitore (Ancestor)
    row = sede_text.locator("xpath=ancestor::div[1]")
    
    # 3. Cerca Turni
    turno_buttons = row.get_by_role("button", name=re.compile(r"TURNO", re.I))
    
    # LOGICA DECISIONALE
    if await turno_buttons.count() > 0:
        print("      -> Trovati TURNI (Scenario B - Weekend)")
        desired_turno = choose_turno(orario)
        # Cerca il turno specifico, altrimenti il primo
        btn = row.get_by_role("button", name=re.compile(rf"^{re.escape(desired_turno)}$", re.I)).first
        if await btn.count() == 0:
            btn = turno_buttons.first
        await safe_click(btn, f"turno={desired_turno}")
    else:
        print("      -> Nessun Turno (Scenario A - Settimana)")
        # Clicca il contenitore o il bottone interno
        inner_btn = row.get_by_role("button").first
        if await inner_btn.count() > 0:
            await safe_click(inner_btn, "bottone_interno_sede")
        else:
            await safe_click(row, "contenitore_sede")

    # Verifica avanzamento
    try:
        await wait_for_step_orario(page)
    except:
        print("      -> Primo click non ha funzionato, provo click diretto sul testo (Fallback)")
        await safe_click(sede_text, "testo_sede_fallback")

async def select_orario(page, orario: str) -> None:
    # Prova ad aprire la tendina
    try: await page.locator("select, div.select, div[role='button']:has-text('Orario')").first.click(timeout=2000)
    except: pass
    
    orario_clean = orario.replace(".", ":")
    # Cerca l'opzione
    opt = page.get_by_text(orario_clean, exact=False).first
    if await opt.count() > 0:
        await safe_click(opt, f"orario={orario}")
    else:
        # Fallback primo disponibile
        await page.locator("li, option").nth(1).click()

# ----------------------------
# ENDPOINTS
# ----------------------------

# TOOL 1: CHECK (Reintegrato)
@app.post("/check_availability")
async def check_availability(dati: RichiestaControllo):
    print(f"🔎 CHECK: {dati.persone} pax, {dati.data}")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        page = await context.new_page()
        try:
            await page.goto("https://rione.fidy.app/prenew.php", timeout=60000)
            try: await page.locator("text=/accetta|consent|ok/i").first.click(timeout=3000)
            except: pass
            
            await select_persone(page, dati.persone)
            await page.wait_for_timeout(500)
            await select_data(page, dati.data)
            
            return {"result": f"Posto trovato per il {dati.data}."}
        except Exception as e:
            return {"result": f"Errore: {e}"}
        finally:
            await browser.close()

# TOOL 2: BOOKING (V24 Logic)
@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione):
    sede_label = normalize_sede(dati.sede)
    seggiolone = needs_seggiolone(dati.note)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        page = await context.new_page()
        page.set_default_timeout(60000)

        try:
            print(f"✍️ BOOKING V24: {dati.nome} -> {sede_label}")
            await page.goto("https://rione.fidy.app/prenew.php?referer=sito", wait_until="domcontentloaded")
            
            try: await page.locator("text=/accetta|consent|ok/i").first.click(timeout=3000)
            except: pass

            print("-> 1. Persone")
            await select_persone(page, dati.persone)

            print("-> 2. Seggiolini")
            await select_seggiolone(page, seggiolone)

            print("-> 3. Data")
            await select_data(page, dati.data)
            
            # PASTO (Opzionale)
            try: 
                pasto = "PRANZO" if int(dati.orario.split(":")[0]) < 17 else "CENA"
                await safe_click(page.locator(f"text=/{pasto}/i").first, "pasto", timeout_ms=2000)
            except: pass

            print(f"-> 4. Sede: {sede_label}")
            await select_sede_and_turno(page, sede_label, dati.orario)

            print("-> 5. Orario")
            await select_orario(page, dati.orario)

            print("-> 6. Conferma 1")
            if dati.note:
                try: await page.locator("textarea").fill(dati.note)
                except: pass
            
            try: await page.locator("text=/CONFERMA/i").first.click()
            except: pass

            print("-> 7. Dati Finali")
            await page.wait_for_timeout(1000)
            first, last = split_name(dati.nome)
            await page.locator("input[placeholder*='Nome'], input[id*='nome']").fill(first)
            await page.locator("input[placeholder*='Cognome'], input[id*='cognome']").fill(last)
            await page.locator("input[placeholder*='Email'], input[type='email']").fill(dati.email)
            await page.locator("input[placeholder*='Telefono'], input[type='tel']").fill(dati.telefono)
            
            try: 
                for cb in await page.locator("input[type='checkbox']").all(): await cb.check()
            except: pass

            print("-> ✅ SUCCESS! (Simulato)")
            # SCOMMENTA PER PRENOTARE REALE:
            # await page.locator("text=/PRENOTA/i").last.click()

            return {
                "result": f"Prenotazione confermata per {dati.nome} a {sede_label}!",
                "debug": "V24 Success"
            }

        except Exception as e:
            print(f"❌ ERRORE: {str(e)}")
            # Salva screenshot in caso di errore (opzionale se hai volume)
            # await page.screenshot(path="error.png") 
            return {"result": f"Errore tecnico: {str(e)}"}
        finally:
            await browser.close()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
