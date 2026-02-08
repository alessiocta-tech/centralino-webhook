import os
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from playwright.async_api import async_playwright

app = FastAPI()

class Prenotazione(BaseModel):
    data: str
    persone: str

@app.get("/")
def home():
    return {"status": "Centralino operativo!"}

@app.post("/check_availability")
async def check_availability(dati: Prenotazione):
    print(f"Richiesta ricevuta: {dati.data} per {dati.persone} persone")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        try:
            print("1. Apro il sito...")
            # Usiamo il link specifico che funziona meglio per i bot
            await page.goto("https://rione.fidy.app/prenew.php?referer=AI", timeout=60000)
            await page.wait_for_load_state("networkidle")
            
            print("2. Cerco i campi...")
            
            # STRATEGIA INTELLIGENTE PER LA DATA
            # Cerchiamo un campo che sia di tipo 'date' OPPURE che si chiami 'data'
            # .first serve a prendere il primo che trova
            campo_data = page.locator("input[type='date'], input[name='data']").first
            # fill scrive dentro al campo in modo umano (non usa evaluate)
            await campo_data.fill(dati.data)
            
            # STRATEGIA PER LE PERSONE
            # Cerchiamo input numerico o che si chiama 'pax'/'coperti'
            campo_persone = page.locator("input[type='number'], input[name='pax'], input[name='coperti']").first
            await campo_persone.fill(dati.persone)
            
            print("3. Clicco Cerca...")
            # Clicchiamo il bottone Submit
            await page.click("button[type='submit'], input[type='submit']")
            
            # Aspettiamo che la pagina risponda
            await page.wait_for_timeout(4000)
            
            print("4. Leggo risultato...")
            testo = await page.inner_text("body")
            await browser.close()
            
            # Controllo parole chiave
            if "non ci sono" in testo.lower() or "nessuna disponibilità" in testo.lower():
                return {"result": f"Ho controllato: per il {dati.data} è tutto pieno."}
            
            return {"result": f"Ho controllato il sito: ci sono posti disponibili per il {dati.data}. Chiedi all'utente che orario preferisce."}

        except Exception as e:
            await browser.close()
            print(f"ERRORE: {e}")
            return {"result": "Ho avuto un problema tecnico a leggere il sito. Riprova."}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
