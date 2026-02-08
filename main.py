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
    return {"status": "Centralino operativo e pronto!"}

@app.post("/check_availability")
async def check_availability(dati: Prenotazione):
    print(f"Richiesta ricevuta: {dati.data} per {dati.persone} persone")
    
    async with async_playwright() as p:
        # headless=True è OBBLIGATORIO su Railway
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        try:
            # 1. Caricamento - USO IL LINK SPECIFICO DEL FORM
            print("Apro il sito corretto...")
            # Usiamo il link preciso che porta dritto al form
            await page.goto("https://rione.fidy.app/prenew.php?referer=AI", timeout=60000)
            await page.wait_for_load_state("networkidle")
            
            # 2. Compilazione "Intelligente" (Attende che i campi appaiano)
            print("Cerco i campi da compilare...")

            # Cerchiamo il campo DATA (proviamo più strategie se il sito è vecchio)
            # Strategia A: Cerca input di tipo date
            # Strategia B: Cerca input che si chiama 'date' o 'data'
            input_data = page.locator("input[type='date'], input[name='date'], input[name='data']").first
            await input_data.fill(dati.data)
            
            # Cerchiamo il campo PERSONE (pax)
            input_persone = page.locator("input[type='number'], input[name='pax'], input[name='persone']").first
            await input_persone.fill(dati.persone)
            
            # 3. Click sul tasto Cerca
            print("Clicco cerca...")
            btn_cerca = page.locator("button[type='submit'], input[type='submit'], button:has-text('Cerca')").first
            await btn_cerca.click()
            
            # 4. Lettura Risultati
            print("Leggo i risultati...")
            await page.wait_for_timeout(4000) # Aspettiamo 4 secondi per sicurezza
            
            testo_pagina = await page.inner_text("body")
            await browser.close()
            
            # Analisi semplice del testo
            testo_lower = testo_pagina.lower()
            if "non ci sono" in testo_lower or "completo" in testo_lower or "nessuna disponibilità" in testo_lower:
                return {"result": f"Ho controllato: per il {dati.data} è tutto pieno."}
            
            # Se non c'è scritto 'pieno', probabilmente ci sono gli orari
            return {"result": f"Ho controllato il sito: ci sono posti disponibili per il {dati.data}. Chiedi all'utente che orario preferisce."}

        except Exception as e:
            await browser.close()
            print(f"Errore critico: {e}")
            # Restituiamo l'errore all'AI così capiamo cosa succede
            return {"result": f"Non riesco a leggere il sito. Errore tecnico: {str(e)}"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
