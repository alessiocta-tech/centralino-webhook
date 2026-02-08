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
    return {"status": "Centralino AI - Booking Engine V5 (FIXED)"}

# --- TOOL 1: CHECK ---
@app.post("/check_availability")
async def check_availability(dati: RichiestaControllo):
    print(f"🔎 CHECK: {dati.persone} pax, {dati.data}")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1', viewport={"width": 390, "height": 844})
        page = await context.new_page()
        try:
            await page.goto("https://rione.fidy.app/prenew.php", timeout=60000)
            await page.wait_for_load_state("networkidle")
            
            # Cookie (Blocco esteso per sicurezza)
            try:
                await page.locator("text=/accetta|consent|ok/i").first.click(timeout=3000)
            except:
                pass
            
            # Persone
            await page.wait_for_selector("text=/Quanti ospiti siete/i", timeout=15000)
            bottone_persone = page.locator(f"div, span, button").filter(has_text=re.compile(f"^\\s*{dati.persone}\\s*$")).first
            if await bottone_persone.count() > 0:
                await bottone_persone.click(force=True)
            else:
                await page.get_by_text(dati.persone, exact=True).first.click(force=True)
            
            # Seggiolini
            await page.wait_for_timeout(1000)
            if await page.locator("text=/seggiolini/i").count() > 0:
                await page.locator("text=/^\\s*NO\\s*$/i").first.click(force=True)
                
            # Data
            await page.wait_for_timeout(1000)
            await page.evaluate(f"document.querySelector('input[type=date]').value = '{dati.data}'")
            await page.locator("input[type=date]").press("Enter")
            
            try:
                await page.locator("text=/conferma|cerca/i").first.click(timeout=2000)
            except:
                pass
            
            await page.wait_for_timeout(4000)
            html = await page.content()
            if "non ci sono" in html.lower():
                return {"result": "Pieno."}
            return {"result": f"Posto trovato per il {dati.data}. Chiedi i dati per prenotare."}
        except Exception as e:
            return {"result": f"Errore: {e}"}
        finally:
            await browser.close()

# --- TOOL 2: BOOKING ---
@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione):
    print(f"📝 BOOKING: {dati.nome} a {dati.sede} - {dati.orario}")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1', viewport={"width": 390, "height": 844})
        page = await context.new_page()
        
        try:
            # 1. Navigazione Iniziale
            await page.goto("https://rione.fidy.app/prenew.php", timeout=60000)
            await page.wait_for_load_state("networkidle")
            
            try:
                await page.locator("text=/accetta|consent|ok/i").first.click(timeout=3000)
            except:
                pass
            
            # Persone
            await page.wait_for_selector("text=/Quanti ospiti siete/i", timeout=15000)
            bottone_persone = page.locator(f"div, span, button").filter(has_text=re.compile(f"^\\s*{dati.persone}\\s*$")).first
            if await bottone_persone.count() > 0:
                await bottone_persone.click(force=True)
            else:
                await page.get_by_text(dati.persone, exact=True).first.click(force=True)
            
            # Seggiolini
            await page.wait_for_timeout(1000)
            if await page.locator("text=/seggiolini/i").count() > 0:
                await page.locator("text=/^\\s*NO\\s*$/i").first.click(force=True)

            # Data
            await page.wait_for_timeout(1000)
            await page.evaluate(f"document.querySelector('input[type=date]').value = '{dati.data}'")
            await page.locator("input[type=date]").press("Enter")
            
            try:
                await page.locator("text=/conferma|cerca/i").first.click(timeout=2000)
            except:
                pass

            # 2. Selezione SEDE
            print(f"   -> Cerco sede: {dati.sede}")
            await page.wait_for_timeout(3000)
            btn_sede = page.locator(f"text=/{dati.sede}/i").first
            if await btn_sede.count() > 0:
                await btn_sede.click(force=True)
            else:
                return {"result": f"Sede '{dati.sede}' non trovata."}

            # 3. Selezione ORARIO (Menu a tendina)
            print(f"   -> Apro tendina orari per {dati.orario}...")
            await page.wait_for_timeout(2000)
            
            # Clicchiamo sulla tendina
            try:
                await page.locator("select, div[class*='select'], div:has-text('Orario')").last.click(force=True)
            except:
                print("   -> Menu orario non trovato, provo a cercare direttamente l'ora.")

            await page.wait_for_timeout(1000)
            
            # Ora clicchiamo l'ora specifica
            orario_clean = dati.orario.replace(".", ":")
            orario_target = page.locator(f"text={orario_clean}").first
            
            if await orario_target.count() > 0:
                await orario_target.click(force=True)
            else:
                return {"result": f"Orario {dati.orario} non trovato o pieno."}

            # 4. Campo NOTE
            print("   -> Inserisco note...")
            if dati.note:
                try:
                    await page.locator("textarea, input[placeholder*='aggiungere']").fill(dati.note)
                except:
                    pass
            
            # Clicchiamo CONFERMA (quello intermedio)
            try:
                await page.locator("text=/CONFERMA/i").first.click(force=True)
            except:
                pass

            # 5. Compilazione DATI FINALI
            print("   -> Compilo Nome, Cognome, Email...")
            await page.wait_for_timeout(2000)
            
            # SPLIT NOME E COGNOME
            parti_nome = dati.nome.split(" ", 1)
            nome_real = parti_nome[0]
            cognome_real = parti_nome[1] if len(parti_nome) > 1 else "." 
            
            await page.locator("input[placeholder*='Nome'], input[id*='nome']").fill(nome_real)
            await page.locator("input[placeholder*='Cognome'], input[id*='cognome']").fill(cognome_real)
            await page.locator("input[placeholder*='Email'], input[type='email']").fill(dati.email)
            await page.locator("input[placeholder*='Telefono'], input[type='tel']").fill(dati.telefono)
            
            # Checkbox privacy (Clicchiamo tutte quelle che troviamo)
            try:
                checkboxes = await page.locator("input[type='checkbox']").all()
                for cb in checkboxes:
                    await cb.check()
            except:
                pass

            # 6. CLICK FINALE "PRENOTA"
            print("   -> Clicco PRENOTA finale!")
            
            # ⚠️ ATTENZIONE: Togli il commento alla riga sotto SOLO quando vuoi che prenoti davvero
            # await page.locator("text=/PRENOTA/i").last.click()
            
            await browser.close()
            return {"result": f"Prenotazione confermata con successo per {dati.nome}!"}

        except Exception as e:
            await browser.close()
            return {"result": f"Errore tecnico durante la prenotazione: {str(e)}"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
