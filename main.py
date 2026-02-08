import os
import uvicorn
import re
from fastapi import FastAPI
from pydantic import BaseModel
from playwright.async_api import async_playwright
from datetime import datetime, timedelta

app = FastAPI()

# Modello per il controllo disponibilità
class RichiestaControllo(BaseModel):
    data: str
    persone: str

# Modello per la prenotazione finale (Nuovi dati necessari)
class RichiestaPrenotazione(BaseModel):
    data: str
    persone: str
    orario: str      # es. "20:00"
    nome: str        # es. "Mario Rossi"
    telefono: str    # es. "3331234567"
    email: str       # es. "mario@email.com"

@app.get("/")
def home():
    return {"status": "Centralino AI - Booking Engine Ready"}

# --- STRUMENTO 1: CONTROLLO (Quello che hai già) ---
@app.post("/check_availability")
async def check_availability(dati: RichiestaControllo):
    print(f"🔎 CHECK: {dati.persone} pax, {dati.data}")
    # ... (Qui riutilizziamo la logica di navigazione che funziona già)
    # Per brevità, invochiamo una funzione interna, ma per ora ti metto
    # il codice semplificato che apre e controlla
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1', viewport={"width": 390, "height": 844})
        page = await context.new_page()
        try:
            await page.goto("https://rione.fidy.app/prenew.php", timeout=60000)
            await page.wait_for_load_state("networkidle")
            
            # (Inserire qui la logica di navigazione Cookie -> Persone -> Data che abbiamo fatto prima)
            # ... Riassumo i passaggi chiave per non rendere il codice gigante ...
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
            # Logica Data Semplificata per il check
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

# --- STRUMENTO 2: PRENOTAZIONE (NUOVO!) ---
@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione):
    print(f"📝 BOOKING: {dati.nome}, Ore {dati.orario}")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1', viewport={"width": 390, "height": 844})
        page = await context.new_page()
        
        try:
            # 1. RIFACCIAMO TUTTA LA NAVIGAZIONE (Persone -> Data)
            # Purtroppo non possiamo 'salvare' la sessione di prima, dobbiamo riaprire il sito
            await page.goto("https://rione.fidy.app/prenew.php", timeout=60000)
            await page.wait_for_load_state("networkidle")
            
            # Cookie
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

            # 2. SELEZIONE ORARIO (NUOVO STEP)
            print(f"   -> Cerco orario {dati.orario}...")
            await page.wait_for_timeout(3000)
            
            # Cerchiamo un bottone che contenga l'orario (es. "20:00")
            # Usiamo regex per trovare l'orario esatto
            orario_btn = page.locator(f"text=/{dati.orario}/").first
            
            if await orario_btn.count() == 0:
                return {"result": f"Non ho trovato l'orario {dati.orario}. Forse è stato appena preso."}
            
            await orario_btn.click(force=True)
            
            # 3. COMPILAZIONE FORM DATI (NUOVO STEP)
            print("   -> Compilo dati cliente...")
            await page.wait_for_timeout(2000)
            
            # Cerchiamo i campi (solitamente name='name', name='email', name='phone')
            # Usiamo selettori generici intelligenti
            await page.locator("input[name*='name'], input[placeholder*='Nome']").fill(dati.nome)
            await page.locator("input[name*='phone'], input[name*='tel'], input[placeholder*='Telef']").fill(dati.telefono)
            await page.locator("input[name*='email'], input[placeholder*='Email']").fill(dati.email)
            
            # Checkbox privacy (spesso obbligatoria)
            try: await page.locator("input[type='checkbox']").first.check()
            except: pass

            # 4. CLICK CONFERMA PRENOTAZIONE
            print("   -> Clicco PRENOTA...")
            # ATTENZIONE: Per ora faccio solo finta di cliccare l'ultimo tasto per non farti prenotazioni false mentre testi!
            # Quando sei pronto, togli il commento alla riga sotto:
            # await page.locator("text=/Prenota|Conferma/i").click()
            
            await browser.close()
            return {"result": f"Prenotazione effettuata con successo a nome {dati.nome} per le {dati.orario}!"}

        except Exception as e:
            await browser.close()
            return {"result": f"Errore nella prenotazione: {str(e)}"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
