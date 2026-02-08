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

# AGGIUNTO IL CAMPO 'sede'
class RichiestaPrenotazione(BaseModel):
    data: str
    persone: str
    orario: str
    nome: str
    telefono: str
    email: str
    sede: str 

@app.get("/")
def home():
    return {"status": "Centralino AI - Booking Engine V2"}

@app.post("/check_availability")
async def check_availability(dati: RichiestaControllo):
    # ... (Codice check identico a prima, lo lascio invariato per brevità) ...
    # Se vuoi ti rimetto tutto il blocco, ma per ora concentriamoci sul booking
    print(f"🔎 CHECK: {dati.persone} pax, {dati.data}")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # User agent iPhone
        context = await browser.new_context(user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1', viewport={"width": 390, "height": 844})
        page = await context.new_page()
        try:
            await page.goto("https://rione.fidy.app/prenew.php", timeout=60000)
            await page.wait_for_load_state("networkidle")
            try: await page.locator("text=/accetta|consent|ok/i").first.click(timeout=3000)
            except: pass
            await page.wait_for_selector("text=/Quanti ospiti siete/i", timeout=15000)
            bottone_persone = page.locator(f"div, span, button").filter(has_text=re.compile(f"^\\s*{dati.persone}\\s*$")).first
            if await bottone_persone.count() > 0: await bottone_persone.click(force=True)
            else: await page.get_by_text(dati.persone, exact=True).first.click(force=True)
            await page.wait_for_timeout(1000)
            if await page.locator("text=/seggiolini/i").count() > 0:
                await page.locator("text=/^\\s*NO\\s*$/i").first.click(force=True)
            await page.wait_for_timeout(1000)
            await page.evaluate(f"document.querySelector('input[type=date]').value = '{dati.data}'")
            await page.locator("input[type=date]").press("Enter")
            try: await page.locator("text=/conferma|cerca/i").first.click(timeout=2000)
            except: pass
            await page.wait_for_timeout(4000)
            html = await page.content()
            if "non ci sono" in html.lower(): return {"result": "Pieno."}
            return {"result": f"Posto trovato per il {dati.data}. Chiedi l'orario e i dati per prenotare."}
        except Exception as e:
            return {"result": f"Errore: {e}"}
        finally:
            await browser.close()

# --- PRENOTAZIONE CORRETTA (Con Selezione Sede) ---
@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione):
    print(f"📝 BOOKING: {dati.nome} a {dati.sede} per le {dati.orario}")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1', viewport={"width": 390, "height": 844})
        page = await context.new_page()
        
        try:
            # 1. Navigazione Iniziale
            await page.goto("https://rione.fidy.app/prenew.php", timeout=60000)
            await page.wait_for_load_state("networkidle")
            
            try: await page.locator("text=/accetta|consent|ok/i").first.click(timeout=3000)
            except: pass
            
            # Persone
            await page.wait_for_selector("text=/Quanti ospiti siete/i", timeout=15000)
            bottone_persone = page.locator(f"div, span, button").filter(has_text=re.compile(f"^\\s*{dati.persone}\\s*$")).first
            if await bottone_persone.count() > 0: await bottone_persone.click(force=True)
            else: await page.get_by_text(dati.persone, exact=True).first.click(force=True)
            
            # Seggiolini
            await page.wait_for_timeout(1000)
            if await page.locator("text=/seggiolini/i").count() > 0:
                await page.locator("text=/^\\s*NO\\s*$/i").first.click(force=True)

            # Data
            await page.wait_for_timeout(1000)
            await page.evaluate(f"document.querySelector('input[type=date]').value = '{dati.data}'")
            await page.locator("input[type=date]").press("Enter")
            try: await page.locator("text=/conferma|cerca/i").first.click(timeout=2000)
            except: pass

            # --- NUOVO PASSAGGIO: SELEZIONE SEDE ---
            print(f"   -> Cerco sede: {dati.sede}")
            await page.wait_for_timeout(3000) # Aspettiamo la lista sedi
            
            # Cerchiamo un testo che contenga il nome della sede (es. "Talenti")
            # Usiamo regex case-insensitive
            btn_sede = page.locator(f"text=/{dati.sede}/i").first
            
            if await btn_sede.count() > 0:
                await btn_sede.click(force=True)
            else:
                return {"result": f"Errore: Non ho trovato la sede '{dati.sede}' nella lista."}
            # ---------------------------------------

            # 2. Selezione Orario
            print(f"   -> Cerco orario {dati.orario}...")
            await page.wait_for_timeout(3000) # Aspettiamo che carichi gli orari della sede scelta
            
            # Formattiamo l'orario per essere sicuri (es. se arriva 13.00 lo cerca come 13:00)
            orario_clean = dati.orario.replace(".", ":")
            
            orario_btn = page.locator(f"text=/{orario_clean}/").first
            if await orario_btn.count() == 0:
                return {"result": f"L'orario {orario_clean} non è più disponibile."}
            
            await orario_btn.click(force=True)
            
            # 3. Compilazione Dati
            print("   -> Compilo dati cliente...")
            await page.wait_for_timeout(2000)
            
            await page.locator("input[name*='name'], input[placeholder*='Nome']").fill(dati.nome)
            await page.locator("input[name*='phone'], input[name*='tel'], input[placeholder*='Telef']").fill(dati.telefono)
            await page.locator("input[name*='email'], input[placeholder*='Email']").fill(dati.email)
            try: await page.locator("input[type='checkbox']").first.check()
            except: pass

            # 4. Click Finale (Ancora disattivato per sicurezza, togli # per attivare)
            # await page.locator("text=/Prenota|Conferma/i").click()
            
            await browser.close()
            return {"result": f"Prenotazione confermata a {dati.sede} per {dati.nome} alle {dati.orario}!"}

        except Exception as e:
            await browser.close()
            return {"result": f"Errore tecnico: {str(e)}"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
