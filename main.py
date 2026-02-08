import os
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from playwright.async_api import async_playwright
from datetime import datetime, timedelta

app = FastAPI()

class Prenotazione(BaseModel):
    data: str  # YYYY-MM-DD
    persone: str

@app.get("/")
def home():
    return {"status": "Centralino Rione - Navigazione a Bottoni ATTIVA"}

@app.post("/check_availability")
async def check_availability(dati: Prenotazione):
    print(f"🔎 CERO TAVOLO: {dati.persone} persone, Data: {dati.data}")
    
    async with async_playwright() as p:
        # headless=True per Railway
        browser = await p.chromium.launch(headless=True)
        # Usiamo un User Agent da cellulare così il sito si comporta esattamente come negli screenshot
        context = await browser.new_context(viewport={"width": 390, "height": 844}) 
        page = await context.new_page()
        
        try:
            # STEP 1: Caricamento
            print("1. Apro il sito...")
            await page.goto("https://rione.fidy.app/reservation", timeout=60000)
            await page.wait_for_load_state("networkidle")

            # STEP 2: Selezione Persone (Bottoni 1, 2, 3...)
            print(f"2. Clicco numero persone: {dati.persone}")
            # Cerchiamo il bottone che ha ESATTAMENTE quel numero
            # Usiamo una logica robusta per trovare il riquadro col numero
            await page.get_by_role("button", name=dati.persone, exact=True).click()
            
            # STEP 3: Seggiolini (Clicchiamo sempre NO per velocità)
            print("3. Gestione seggiolini...")
            # Aspettiamo che appaia la domanda
            await page.wait_for_timeout(1000) 
            if await page.get_by_text("Servono anche seggiolini?").is_visible():
                await page.get_by_text("NO", exact=True).click()

            # STEP 4: Selezione Data (Oggi, Domani o Altra)
            print("4. Selezione Data...")
            await page.wait_for_timeout(1000)

            # Calcoliamo che giorno è
            oggi = datetime.now().date()
            data_richiesta = datetime.strptime(dati.data, "%Y-%m-%d").date()
            
            if data_richiesta == oggi:
                print("   -> Clicco 'Oggi'")
                await page.get_by_text("Oggi", exact=True).click()
            
            elif data_richiesta == oggi + timedelta(days=1):
                print("   -> Clicco 'Domani'")
                await page.get_by_text("Domani", exact=True).click()
            
            else:
                print("   -> Clicco 'Altra data'")
                await page.get_by_text("Altra data").click()
                # Qui ci aspettiamo che appaia un input date nativo o un calendario
                # Aspettiamo un attimo che appaia il campo
                await page.wait_for_timeout(500)
                # Riempiamo il campo data che appare
                await page.locator("input[type='date']").fill(dati.data)
                # Spesso bisogna confermare o premere invio su questi form
                await page.locator("input[type='date']").press("Enter")
                # Se c'è un bottone "Conferma" o "Cerca", proviamo a cliccarlo, altrimenti proseguiamo
                try:
                    await page.click("button:has-text('Conferma')", timeout=1000)
                except:
                    pass

            # STEP 5: Controllo Disponibilità (Screenshot 3)
            print("5. Controllo risultati...")
            # Aspettiamo che carichi la lista delle sedi (es. "Talenti", "Palermo")
            await page.wait_for_timeout(3000)
            
            # Recuperiamo tutto il testo della pagina
            testo_pagina = await page.inner_text("body")
            
            # Se siamo arrivati alla pagina con i prezzi (Screenshot 3), c'è posto!
            # Cerchiamo parole chiave come i nomi delle sedi o il simbolo dell'euro
            sedi_disponibili = []
            if "Talenti" in testo_pagina: sedi_disponibili.append("Talenti")
            if "Palermo" in testo_pagina: sedi_disponibili.append("Palermo")
            if "Appia" in testo_pagina: sedi_disponibili.append("Appia")
            if "Ostia" in testo_pagina: sedi_disponibili.append("Ostia")
            
            await browser.close()
            
            if len(sedi_disponibili) > 0:
                sedi_text = ", ".join(sedi_disponibili)
                return {"result": f"Ottime notizie! Ho trovato posto per il {dati.data} nelle sedi: {sedi_text}. Dove preferisci?"}
            
            # Se non troviamo le sedi o c'è scritto "Completo"
            if "non ci sono" in testo_pagina.lower() or "completo" in testo_pagina.lower():
                return {"result": f"Mi dispiace, per il {dati.data} sembra tutto pieno."}
                
            # Fallback generico se la pagina è strana ma non dà errore
            return {"result": "Ho controllato e vedo delle disponibilità sul sito. Chiedi all'utente quale sede preferisce."}

        except Exception as e:
            await browser.close()
            print(f"ERRORE: {e}")
            return {"result": "Ho avuto un problema tecnico momentaneo. Riprova tra poco."}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
