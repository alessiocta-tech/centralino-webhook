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
    return {"status": "Centralino AI - Versione Ultra-Light V13"}

# --- LOGICA DI SUPPORTO ---
def get_pasto(orario):
    try:
        h = int(orario.split(":")[0])
        return "PRANZO" if h < 17 else "CENA"
    except: return "CENA"

def get_tipo_data(data_str):
    try:
        d = datetime.strptime(data_str, "%Y-%m-%d").date()
        oggi = datetime.now().date()
        domani = oggi + timedelta(days=1)
        if d == oggi: return "Oggi"
        if d == domani: return "Domani"
        return "Altra"
    except: return "Altra"

# --- FUNZIONE PER BLOCCARE TUTTO IL PESO INUTILE ---
async def blocca_risorse(route):
    # Blocca immagini, font, stili, media per risparmiare RAM
    if route.request.resource_type in ["image", "media", "font", "stylesheet", "other"]:
        await route.abort()
    else:
        await route.continue_()

# --- TOOL 1: CHECK ---
@app.post("/check_availability")
async def check_availability(dati: RichiestaControllo):
    print(f"🔎 CHECK: {dati.persone} pax, {dati.data}")
    async with async_playwright() as p:
        # Browser super ottimizzato per Railway
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                "--disable-accelerated-2d-canvas", "--no-first-run", "--no-zygote",
                "--single-process", "--disable-gpu", "--disable-extensions"
            ]
        )
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        # Applica il blocco risorse
        await context.route("**/*", blocca_risorse)
        
        page = await context.new_page()
        try:
            await page.goto("https://rione.fidy.app/prenew.php", timeout=30000, wait_until="domcontentloaded")
            
            # 1. Cookie
            try: await page.locator("text=/accetta|consent|ok/i").first.click(timeout=1500)
            except: pass
            
            # 2. Persone
            try: await page.locator(f"button:text-is('{dati.persone}'), div:text-is('{dati.persone}')").first.click(timeout=2000)
            except: await page.get_by_text(dati.persone, exact=True).first.click(force=True)

            # 3. Seggiolini NO
            await page.wait_for_timeout(500)
            if await page.locator("text=/seggiolini/i").count() > 0:
                await page.locator("text=/^\\s*NO\\s*$/i").first.click(force=True)

            # 4. Data
            tipo = get_tipo_data(dati.data)
            await page.wait_for_timeout(500)
            if tipo == "Oggi": await page.locator("text=/Oggi/i").first.click()
            elif tipo == "Domani": await page.locator("text=/Domani/i").first.click()
            else:
                await page.locator("text=/Altra data/i").first.click()
                await page.wait_for_timeout(500)
                await page.evaluate(f"document.querySelector('input[type=date]').value = '{dati.data}'")
                await page.locator("input[type=date]").press("Enter")
                try: await page.locator("text=/conferma|cerca/i").first.click(timeout=1000)
                except: pass

            return {"result": f"Posto trovato per il {dati.data}."}
        except Exception as e:
            return {"result": f"Errore: {e}"}
        finally:
            await browser.close()

# --- TOOL 2: BOOKING ---
@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione):
    print(f"📝 BOOKING: {dati.nome} -> {dati.sede} ({dati.orario})")
    
    # Mappa Sedi
    mappa = {
        "talenti": "Talenti - Roma", "ostia": "Ostia Lido",
        "appia": "Appia", "reggio": "Reggio Calabria", "palermo": "Palermo"
    }
    sede_target = mappa.get(dati.sede.lower().strip(), dati.sede)
    pasto = get_pasto(dati.orario)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                "--disable-accelerated-2d-canvas", "--no-first-run", "--no-zygote",
                "--single-process", "--disable-gpu", "--disable-extensions"
            ]
        )
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
            viewport={"width": 390, "height": 844}
        )
        # BLOCCA CSS E IMMAGINI (Salva la RAM!)
        await context.route("**/*", blocca_risorse)
        
        page = await context.new_page()

        try:
            await page.goto("https://rione.fidy.app/prenew.php", timeout=45000, wait_until="domcontentloaded")
            
            # 1. Cookie
            try: await page.locator("text=/accetta|consent|ok/i").first.click(timeout=1500)
            except: pass
            
            # 2. Persone
            try: await page.locator(f"button:text-is('{dati.persone}'), div:text-is('{dati.persone}')").first.click(timeout=2000)
            except: await page.get_by_text(dati.persone, exact=True).first.click(force=True)

            # 3. Seggiolini NO
            await page.wait_for_timeout(500)
            if await page.locator("text=/seggiolini/i").count() > 0:
                await page.locator("text=/^\\s*NO\\s*$/i").first.click(force=True)

            # 4. Data
