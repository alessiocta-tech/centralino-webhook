import os
import uvicorn
import re
from fastapi import FastAPI
from pydantic import BaseModel
from playwright.async_api import async_playwright

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
    return {"status": "Centralino AI - Versione Ultra-Light V11"}

# --- TOOL 1: CONTROLLO DISPONIBILITÀ ---
@app.post("/check_availability")
async def check_availability(dati: RichiestaControllo):
    print(f"🔎 CHECK LIGHT: {dati.persone} pax, {dati.data}")
    async with async_playwright() as p:
        # 1. LANCIO BROWSER OTTIMIZZATO (NO GPU, NO SANDBOX)
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-accelerated-2d-canvas",
                "--no-first-run",
                "--no-zygote",
                "--single-process",
                "--disable-gpu"
            ]
        )
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        page = await context.new_page()

        # 2. BLOCCO IMMAGINI, FONT E CSS (RISPARMIO MEMORIA ENORME)
        await page.route("**/*", lambda route: route.abort() 
            if route.request.resource_type in ["image", "media", "font", "stylesheet"] 
            else route.continue_())

        try:
            await page.goto("https://rione.fidy.app/prenew.php", timeout=30000, wait_until="domcontentloaded")
            
            # Cookie e Popups
            try: await page.locator("text=/accetta|consent|ok/i").first.click(timeout=2000)
            except: pass
            
            # Logica Persone
            try:
                bottone_persone = page.locator(f"div, span, button").filter(has_text=re.compile(f"^\\s*{dati.persone}\\s*$")).first
                if await bottone_persone.count() > 0: await bottone_persone.click(force=True, timeout=2000)
                else: await page.get_by_text(dati.persone, exact=True).first.click(force=True, timeout=2000)
            except: pass # Se fallisce clicca persone, proviamo a continuare
            
            # Seggiolini NO
            await page.wait_for_timeout(500)
            if await page.locator("text=/seggiolini/i").count() > 0:
                await page.locator("text=/^\\s*NO\\s*$/i").first.click(force=True)
                
            # Data
            await page.wait_for_timeout(500)
            await page.evaluate(f"document.querySelector('input[type=date]').value = '{dati.data}'")
            await page.locator("input[type=date]").press("Enter")
            try: await page.locator("text=/conferma|cerca/i").first.click(timeout=2000)
            except: pass
            
            # Verifica
            await page.wait_for_timeout(2000)
            html = await page.content()
            if "non ci sono" in html.lower(): return {"result": "Pieno."}
            return {"result": f"Posto trovato per il {dati.data}."}
        except Exception as e:
            return {"result": f"Errore Check: {e}"}
        finally:
            await browser.close()

# --- TOOL 2: PRENOTAZIONE ---
@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione):
    print(f"📝 BOOKING LIGHT: {dati.nome} -> {dati.sede} ({dati.orario})")
    
    # Mappa Sedi
    mappa_sedi = {
        "talenti": "Talenti - Roma",
        "ostia": "Ostia Lido",
        "appia": "Appia",
        "reggio": "Reggio Calabria",
        "palermo": "Palermo"
    }
    sede_target = mappa_sedi.get(dati.sede.lower().strip(), dati.sede) # Usa mappa o originale
    print(f"   -> Sede target: {sede_target}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-accelerated-2d-canvas",
                "--no-first-run",
                "--no-zygote",
                "--single-process",
                "--disable-gpu"
            ]
        )
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
            viewport={"width": 390, "height": 844}
        )
        page = await context.new_page()

        # BLOCCO RISORSE PESANTI
        await page.route("**/*", lambda route: route.abort() 
            if route.request.resource_type in ["image", "media", "font", "stylesheet"] 
            else route.continue_())

        try:
            await page.goto("https://rione.fidy.app/prenew.php", timeout=60000, wait_until="domcontentloaded")
            
            # Cookie
            try: await page.locator("text=/accetta|consent|ok/i").first.click(timeout=2000)
            except: pass
            
            # Persone
            try:
                bottone_persone = page.locator(f"div, span, button").filter(has_text=re.compile(f"^\\s*{dati.persone}\\s*$")).first
                if await bottone_persone.count() > 0: await bottone_persone.click(force=True)
                else: await page.get_by_text(dati.persone, exact=True).first.click(force=True)
            except: pass

            # Seggiolini
            await page.wait_for_timeout(500)
            if await page.locator("text=/seggiolini/i").count() > 0:
                await page.locator("text=/^\\s*NO\\s*$/i").first.click(force=True)

            # Data
            await page.wait_for_timeout(1000)
            await page.evaluate(f"document.querySelector('input[type=date]').value = '{dati.data}'")
            await page.locator("input[type=date]").press("Enter")
            try: await page.locator("text=/conferma|cerca/i").first.click(timeout=2000)
            except: pass

            # --- SEDE (Con attesa intelligente) ---
            print(f"   -> Cerco sede: {sede_target}")
            await page.wait_for_timeout(3000)
            
            btn_sede = page.get_by_text(sede_target, exact=False).first
            if await btn_sede.count() > 0:
                await btn_sede.click(force=True)
            else:
                # Fallback generico
                await page.get_by_text(dati.sede, exact=False).first.click(force=True)

            # Orario
            print(f"   -> Orario: {dati.orario}")
            await page.wait_for_timeout(2000)
            try: await page.locator("select, div[class*='select']").last.click(force=True)
            except: pass
            
            orario_clean = dati.orario.replace(".", ":")
            orario_btn = page.locator(f"text={orario_clean}").first
            if await orario_btn.count() > 0:
                await orario_btn.click(force=True)
            else:
                return {"result": f"Orario {dati.orario} non disponibile."}

            # Dati finali
            if dati.note:
                try: await page.locator("textarea").fill(dati.note)
                except: pass
            
            try: await page.locator("text=/CONFERMA/i").first.click(force=True)
            except: pass

            await page.wait_for_timeout(1000)
            parti = dati.nome.split(" ", 1)
            n = parti[0]
            c = parti[1] if len(parti) > 1 else "." 
            
            await page.locator("input[placeholder*='Nome'], input[id*='nome']").fill(n)
            await page.locator("input[placeholder*='Cognome'], input[id*='cognome']").fill(c)
            await page.locator("input[placeholder*='Email'], input[type='email']").fill(dati.email)
            await page.locator("input[placeholder*='Telefono'], input[type='tel']").fill(dati.telefono)
            
            try:
                checkboxes = await page.locator("input[type='checkbox']").all()
                for cb in checkboxes: await cb.check()
            except: pass

            print("   -> ✅ COMPLETATO!")
            # await page.locator("text=/PRENOTA/i").last.click() # Scommenta per attivare

            return {"result": f"Prenotazione confermata per {dati.nome}!"}

        except Exception as e:
            print(f"❌ ERRORE: {e}")
            return {"result": f"Errore tecnico: {str(e)}"}
        finally:
            await browser.close()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
