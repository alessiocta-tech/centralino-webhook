import os
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from playwright.async_api import async_playwright
from datetime import datetime, timedelta

app = FastAPI()

class Prenotazione(BaseModel):
    data: str
    persone: str

@app.get("/")
def home():
    return {"status": "Centralino Rione - READY"}

@app.post("/check_availability")
async def check_availability(dati: Prenotazione):
    # QUESTA RIGA CI DIRÀ SE IL CODICE È AGGIORNATO
    print(f"🚀 AVVIO VERSIONE NUOVA (Anti-Cookie) - Cerco per {dati.persone} persone")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # Usiamo User Agent iPhone per forzare la visualizzazione mobile che vediamo negli screen
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1',
            viewport={"width": 390, "height": 844}
        )
        page = await context.new_page()
        
        try:
            print("1. Apro il sito...")
            await page.goto("https://rione.fidy.app/reservation", timeout=60000)
            await page.wait_for_load_state("domcontentloaded")
            
            # --- BLOCCO ANTI-COOKIE (Cruciale) ---
            print("2. Controllo Cookie...")
            # Clicchiamo ovunque possa esserci un "Accetta" o "Consent"
            try:
                if await page.locator("text=Accetta").count() > 0:
                    await page.locator("text=Accetta").click()
                    print("   -> Cookie rimossi.")
            except:
                pass
            # -------------------------------------

            print(f"3. Clicco numero persone: {dati.persone}")
            await page.wait_for_timeout(2000) # Aspettiamo che la pagina si assesti
            
            # Strategia "Cecchino": Cerchiamo il testo esatto del numero
            # force=True clicca anche se c'è un pixel trasparente sopra
            await page.locator(f"text='{dati.persone}'").first.click(force=True)
            
            print("4. Gestione Seggiolini...")
            await page.wait_for_timeout(2000)
            # Se compare la scritta seggiolini, clicchiamo NO
            if await page.locator("text=seggiolini").count() > 0:
                await page.locator("text=NO").first.click(force=True)

            print("5. Selezione Data...")
            await page.wait_for_timeout(1000)
            
            oggi = datetime.now().date()
            data_richiesta = datetime.strptime(dati.data, "%Y-%m-%d").date()
            
            # Logica pulsanti Oggi/Domani
            if data_richiesta == oggi:
                await page.locator("text='Oggi'").first.click(force=True)
            elif data_richiesta == oggi + timedelta(days=1):
                await page.locator("text='Domani'").first.click(force=True)
            else:
                # Altra data
                await page.locator("text='Altra data'").first.click(force=True)
                await page.wait_for_timeout(500)
                await page.locator("input[type='date']").fill(dati.data)
                await page.locator("input[type='date']").press("Enter")
                # Tentativo clic conferma se esiste
                if await page.locator("text=Conferma").count() > 0:
                    await page.locator("text=Conferma").click()

            print("6. Controllo Risultati...")
            await page.wait_for_timeout(4000)
            testo = await page.inner_text("body")
            
            if "non ci sono" in testo.lower() or "completo" in testo.lower():
                return {"result": f"Tutto pieno per il {dati.data}."}
            
            # Se siamo arrivati qui, è andata bene
            return {"result": f"Disponibilità trovata per il {dati.data}. Chiedi l'orario."}

        except Exception as e:
            await browser.close()
            print(f"❌ ERRORE: {e}")
            return {"result": f"Errore tecnico: {e}"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
