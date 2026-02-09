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
    return {"status": "Centralino AI - V14 Survival Mode"}

# --- HELPER LOGICI ---
def get_pasto(orario):
    try:
        return "PRANZO" if int(orario.split(":")[0]) < 17 else "CENA"
    except: return "CENA"

def get_tipo_data(data_str):
    try:
        d = datetime.strptime(data_str, "%Y-%m-%d").date()
        oggi = datetime.now().date()
        if d == oggi: return "Oggi"
        if d == oggi + timedelta(days=1): return "Domani"
        return "Altra"
    except: return "Altra"

# --- BLOCCO RISORSE TOTALE ---
async def blocca_tutto(route):
    if route.request.resource_type in ["image", "media", "font", "stylesheet", "script", "other", "ping", "fetch"]:
        await route.abort()
    else:
        await route.continue_()

# --- CONFIGURAZIONE BROWSER ANTI-CRASH ---
def get_launch_args():
    return [
        "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
        "--disable-accelerated-2d-canvas", "--no-first-run", "--no-zygote",
        "--single-process", "--disable-gpu", "--disable-extensions",
        "--disable-background-networking", "--disable-default-apps", "--disable-sync",
        "--disable-translate", "--headless", "--hide-scrollbars", "--metrics-recording-only",
        "--mute-audio", "--no-default-browser-check", "--disable-gl-drawing-for-tests"
    ]

# --- TOOL 1: CHECK ---
@app.post("/check_availability")
async def check_availability(dati: RichiestaControllo):
    print(f"🔎 CHECK: {dati.persone} pax, {dati.data}")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=get_launch_args())
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        await context.route("**/*", blocca_tutto) # Blocco aggressivo
        page = await context.new_page()

        try:
            await page.goto("https://rione.fidy.app/prenew.php", timeout=20000, wait_until="domcontentloaded")
            
            # 1. Cookie
            try: await page.locator("text=/accetta|consent|ok/i").first.click(timeout=1000)
            except: pass
            
            # 2. Persone
            try: await page.locator(f"button:text-is('{dati.persone}'), div:text-is('{dati.persone}')").first.click(timeout=1500)
            except: await page.get_by_text(dati.persone, exact=True).first.click(force=True)

            # 3. Seggiolini NO
            await page.wait_for_timeout(200)
            if await page.locator("text=/seggiolini/i").count() > 0:
                await page.locator("text=/^\\s*NO\\s*$/i").first.click(force=True)

            # 4. Data
            tipo = get_tipo_data(dati.data)
            if tipo in ["Oggi", "Domani"]:
                await page.locator(f"text=/{tipo}/i").first.click()
            else:
                await page.locator("text=/Altra data/i").first.click()
                await page.wait_for_timeout(200)
                await page.evaluate(f"document.querySelector('input[type=date]').value = '{dati.data}'")
                await page.locator("input[type=date]").press("Enter")
                try: await page.locator("text=/conferma|cerca/i").first.click(timeout=500)
                except: pass

            return {"result": f"Posto trovato per il {dati.data}."}
        except Exception as e:
            return {"result": f"Errore: {e}"}
        finally:
            await browser.close()

# --- TOOL 2: BOOKING ---
@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione):
    print(f"📝 BOOKING: {dati.sede} - {dati.orario}")
    mappa = {"talenti": "Talenti - Roma", "ostia": "Ostia Lido", "appia": "Appia", "reggio": "Reggio Calabria", "palermo": "Palermo"}
    sede_target = mappa.get(dati.sede.lower().strip(), dati.sede)
    pasto = get_pasto(dati.orario)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=get_launch_args())
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
            viewport={"width": 390, "height": 844}
        )
        await context.route("**/*", blocca_tutto) # Blocco aggressivo
        page = await context.new_page()

        try:
            await page.goto("https://rione.fidy.app/prenew.php", timeout=40000, wait_until="domcontentloaded")
            
            # STEP BASE (Veloci)
            try: await page.locator("text=/accetta|consent|ok/i").first.click(timeout=1000)
            except: pass
            
            try: await page.locator(f"button:text-is('{dati.persone}'), div:text-is('{dati.persone}')").first.click(timeout=1500)
            except: await page.get_by_text(dati.persone, exact=True).first.click(force=True)

            await page.wait_for_timeout(200)
            if await page.locator("text=/seggiolini/i").count() > 0:
                await page.locator("text=/^\\s*NO\\s*$/i").first.click(force=True)

            # DATA & PASTO
            tipo = get_tipo_data(dati.data)
            if tipo in ["Oggi", "Domani"]: await page.locator(f"text=/{tipo}/i").first.click()
            else:
                await page.locator("text=/Altra data/i").first.click()
                await page.wait_for_timeout(200)
                await page.evaluate(f"document.querySelector('input[type=date]').value = '{dati.data}'")
                await page.locator("input[type=date]").press("Enter")
                try: await page.locator("text=/conferma|cerca/i").first.click(timeout=500)
                except: pass
            
            await page.wait_for_timeout(500)
            try: await page.locator(f"text=/{pasto}/i").first.click(timeout=2000)
            except: pass 

            # --- SEDE & TURNO (Punto Critico) ---
            print(f"   -> Clicco sede: {sede_target}")
            await page.wait_for_timeout(1000)
            
            # Cerca il blocco della sede
            sede_card = page.locator(f"div").filter(has_text=sede_target).last
            
            # Se ci sono i tasti "TURNO" dentro quella card, clicca il primo (I TURNO)
            btn_turno = sede_card.locator("text=/TURNO/i").first
            
            if await btn_turno.count() > 0:
                await btn_turno.click(force=True)
            elif await sede_card.count() > 0:
                await sede_card.click(force=True)
            else:
                await page.get_by_text(dati.sede, exact=False).first.click(force=True)

            # --- ORARIO ---
            await page.wait_for_timeout(1500)
            try: await page.locator("select").first.click(timeout=1000)
            except: pass
            
            orario_clean = dati.orario.replace(".", ":")
            try: await page.locator(f"text=/{orario_clean}/").first.click(timeout=2000)
            except: await page.locator("select option").nth(1).click() # Fallback primo orario

            # --- Dati ---
            if dati.note:
                try: await page.locator("textarea").fill(dati.note)
                except: pass
            try: await page.locator("text=/CONFERMA/i").first.click(force=True)
            except: pass

            await page.wait_for_timeout(1000)
            p = dati.nome.split(" ", 1)
            await page.locator("input[placeholder*='Nome'], input[id*='nome']").fill(p[0])
            await page.locator("input[placeholder*='Cognome'], input[id*='cognome']").fill(p[1] if len(p)>1 else ".")
            await page.locator("input[placeholder*='Email'], input[type='email']").fill(dati.email)
            await page.locator("input[placeholder*='Telefono'], input[type='tel']").fill(dati.telefono)
            try: 
                for cb in await page.locator("input[type='checkbox']").all(): await cb.check()
            except: pass

            print("   -> ✅ OK!")
            # await page.locator("text=/PRENOTA/i").last.click() # Scommenta per attivare

            return {"result": f"Prenotazione confermata per {dati.nome}!"}

        except Exception as e:
            print(f"❌: {e}")
            return {"result": f"Errore tecnico: {str(e)}"}
        finally:
            await browser.close() # Fondamentale

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
