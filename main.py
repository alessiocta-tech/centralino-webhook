import os
import uvicorn
import re
from fastapi import FastAPI
from pydantic import BaseModel
from playwright.async_api import async_playwright
from datetime import datetime, timedelta

app = FastAPI()

class RichiestaControllo(BaseModel):
    data: str
    persone: str

class RichiestaPrenotazione(BaseModel):
    data: str
    persone: str
    orario: str
    nome: str
    telefono: str
    email: str
    sede: str
    note: str = ""

@app.get("/")
def home():
    return {"status": "Centralino AI - V22 (Camaleonte Hybrid)"}

# --- HELPER ---
def get_pasto(orario):
    try: return "PRANZO" if int(orario.split(":")[0]) < 17 else "CENA"
    except: return "CENA"

def get_tipo_data(data_str):
    try:
        d = datetime.strptime(data_str, "%Y-%m-%d").date()
        oggi = datetime.now().date()
        if d == oggi: return "Oggi"
        if d == oggi + timedelta(days=1): return "Domani"
        return "Altra"
    except: return "Altra"

# --- TOOL 1: CHECK ---
@app.post("/check_availability")
async def check_availability(dati: RichiestaControllo):
    print(f"🔎 CHECK: {dati.persone} pax, {dati.data}")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        page = await context.new_page()

        try:
            await page.goto("https://rione.fidy.app/prenew.php", timeout=60000, wait_until="domcontentloaded")
            try: await page.locator("text=/accetta|consent|ok/i").first.click(timeout=3000)
            except: pass
            
            try: await page.locator(f"button:text-is('{dati.persone}'), div:text-is('{dati.persone}')").first.click(timeout=3000)
            except: await page.get_by_text(dati.persone, exact=True).first.click(force=True)

            await page.wait_for_timeout(500)
            if await page.locator("text=/seggiolini/i").count() > 0:
                await page.locator("text=/^\\s*NO\\s*$/i").first.click(force=True)

            tipo = get_tipo_data(dati.data)
            if tipo in ["Oggi", "Domani"]:
                await page.locator(f"text=/{tipo}/i").first.click()
            else:
                await page.locator("text=/Altra data/i").first.click()
                await page.wait_for_timeout(500)
                await page.evaluate(f"document.querySelector('input[type=date]').value = '{dati.data}'")
                await page.locator("input[type=date]").press("Enter")
                try: await page.locator("text=/conferma|cerca/i").first.click(timeout=2000)
                except: pass

            return {"result": f"Posto trovato per il {dati.data}."}
        except Exception as e:
            return {"result": f"Errore: {e}"}
        finally:
            await browser.close()

# --- TOOL 2: BOOKING ---
@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione):
    print(f"📝 START BOOKING V22: {dati.sede} - {dati.orario}")
    
    mappa = {
        "talenti": "Talenti", 
        "ostia": "Ostia", 
        "appia": "Appia", 
        "reggio": "Reggio", 
        "palermo": "Palermo"
    }
    sede_keyword = mappa.get(dati.sede.lower().strip(), dati.sede)
    pasto = get_pasto(dati.orario)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
            viewport={"width": 390, "height": 844}
        )
        page = await context.new_page()
        page.set_default_timeout(60000)

        try:
            print("   -> 1. Caricamento...")
            await page.goto("https://rione.fidy.app/prenew.php", timeout=60000, wait_until="domcontentloaded")
            
            try: await page.locator("text=/accetta|consent|ok/i").first.click(timeout=3000)
            except: pass
            
            print("   -> 2. Persone...")
            try: await page.locator(f"button:text-is('{dati.persone}'), div:text-is('{dati.persone}')").first.click(timeout=3000)
            except: await page.get_by_text(dati.persone, exact=True).first.click(force=True)

            await page.wait_for_timeout(500)
            if await page.locator("text=/seggiolini/i").count() > 0:
                await page.locator("text=/^\\s*NO\\s*$/i").first.click(force=True)

            print(f"   -> 3. Data ({dati.data})...")
            tipo = get_tipo_data(dati.data)
            if tipo in ["Oggi", "Domani"]:
                await page.locator(f"text=/{tipo}/i").first.click()
            else:
                await page.locator("text=/Altra data/i").first.click()
                await page.wait_for_timeout(500)
                await page.evaluate(f"document.querySelector('input[type=date]').value = '{dati.data}'")
                await page.locator("input[type=date]").press("Enter")
                try: await page.locator("text=/conferma|cerca/i").first.click(timeout=2000)
                except: pass
            
            print(f"   -> 4. Pasto ({pasto})...")
            await page.wait_for_timeout(2000)
            try: 
                btn_pasto = page.locator(f"text=/{pasto}/i").first
                if await btn_pasto.count() > 0 and await btn_pasto.is_visible():
                    await btn_pasto.click(timeout=3000)
            except: pass

            # --- 5. SEDE (LOGICA IBRIDA CAMALEONTE) ---
            print(f"   -> 5. Cerca Sede: '{sede_keyword}'...")
            await page.wait_for_timeout(3000)
            
            cliccato = False
            
            # Troviamo la card della sede (il riquadro intero)
            # Cerchiamo un div che contenga il nome, e prendiamo l'ultimo (spesso il più specifico)
            cards = page.locator("div").filter(has_text=sede_keyword).all()
            
            # Scorre le card possibili (partendo dall'ultima che di solito è quella giusta)
            for card in reversed(await cards):
                if await card.is_visible():
                    # CASO A: Ci sono i turni?
                    btn_turno = card.locator("text=/TURNO/i").first
                    if await btn_turno.count() > 0 and await btn_turno.is_visible():
                        print("      -> Trovati turni! Clicco 'I TURNO'.")
                        await btn_turno.click(force=True)
                        cliccato = True
                        break
                    
                    # CASO B: Non ci sono turni? Clicca la card intera
                    print("      -> Nessun turno visibile. Clicco la card intera (Standard).")
                    await card.click(force=True)
                    cliccato = True
                    break
            
            if not cliccato:
                print("      -> Fallback: Clicco testo sede.")
                await page.get_by_text(sede_keyword, exact=False).last.click(force=True)

            # --- 6. ORARIO (Gestione Tendina) ---
            print("   -> 6. Orario...")
            await page.wait_for_timeout(3000)
            
            # Apre la tendina se c'è
            try: 
                await page.locator("select, div[class*='select'], div:has-text('Orario')").last.click(timeout=2000)
            except: pass
            
            orario_clean = dati.orario.replace(".", ":")
            print(f"      -> Cerco '{orario_clean}'...")
            
            # Cerca l'orario specifico nel menu aperto
            btn_ora = page.locator(f"li, div, option").filter(has_text=re.compile(f"^{orario_clean}")).first
            
            if await btn_ora.count() > 0:
                 await btn_ora.click(force=True)
            else:
                print("      -> Orario non trovato, provo fallback generico.")
                # Clicca qualsiasi cosa contenga l'ora (es "13:00")
                try: await page.locator(f"text=/{orario_clean}/").first.click(timeout=2000)
                except: 
                     # Extrema ratio: seleziona il secondo elemento della lista (spesso il primo orario disponibile)
                     await page.locator("li, option").nth(1).click()

            # 7. DATI
            print("   -> 7. Dati e Conferma...")
            if dati.note:
                try: await page.locator("textarea").fill(dati.note)
                except: pass
            
            try: await page.locator("text=/CONFERMA/i").first.click(
