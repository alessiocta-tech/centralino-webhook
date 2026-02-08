import os
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright

app = FastAPI()

class Prenotazione(BaseModel):
    data: str
    persone: str

@app.get("/")
def home():
    return {"status": "Centralino Railway ATTIVO 🚀"}

@app.post("/check_availability")
async def check_availability(dati: Prenotazione):
    print(f"Richiesta ricevuta: {dati.data} per {dati.persone} persone")
    
    async with async_playwright() as p:
        # SU RAILWAY: headless=True è OBBLIGATORIO (non c'è schermo)
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        try:
            # 1. Caricamento
            print("Apro il sito...")
            await page.goto("https://rione.fidy.app/reservation", timeout=60000)
            await page.wait_for_load_state("networkidle")
            
            # 2. Compilazione
            # Forziamo il valore della data via JS per evitare problemi con i calendari grafici
            await page.evaluate(f"document.querySelector('input[type=date]').value = '{dati.data}'")
            # Cerchiamo input numerico per le persone
            await page.fill("input[type='number']", dati.persone)
            # Clicchiamo il tasto cerca
            await page.click("button[type='submit'], input[type='submit']")
            
            # 3. Attesa risultati
            await page.wait_for_timeout(4000) # Aspettiamo 4 secondi che il sito carichi i turni
            
            # 4. Lettura
            testo_pagina = await page.inner_text("body")
            await browser.close()
            
            # Logica semplice
            if "non ci sono" in testo_pagina.lower() or "completo" in testo_pagina.lower():
                return {"result": f"Ho controllato: per il {dati.data} è tutto pieno."}
            
            return {"result": f"Ho controllato il sito: ci sono posti disponibili per il {dati.data}. Chiedi all'utente che orario preferisce."}

        except Exception as e:
            await browser.close()
            print(f"Errore: {e}")
            return {"result": "Ho avuto un problema tecnico di connessione col sito."}

if __name__ == "__main__":
    # Railway ci assegna una porta variabile, dobbiamo leggerla
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)