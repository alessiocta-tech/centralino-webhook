import os
import uvicorn
import re
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
    return {"status": "Centralino Fidy - Versione ULTIMATE"}

@app.post("/check_availability")
async def check_availability(dati: Prenotazione):
    print(f"🚀 AVVIO ANALISI PROFONDA: {dati.persone} persone, Data: {dati.data}")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # Simuliamo un iPhone Pro per avere la vista mobile corretta
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
            viewport={"width": 390, "height": 844},
            device_scale_factor=3
        )
        page = await context.new_page()
        
        try:
            # 1. CARICAMENTO PAGINA
            print("1. Carico rione.fidy.app...")
            await page.goto("https://rione.fidy.app/prenew.php", timeout=60000)
            # Aspettiamo che la rete si calmi (caricamento completato)
            await page.wait_for_load_state("networkidle")
            
            # 2. GESTIONE POPUP / COOKIE (Proviamo a cliccare tutto ciò che sembra un 'Accetta')
            print("2. Scansione Cookie...")
            try:
                # Cerca bottoni con testo 'Accetta', 'Consent', 'OK' (case insensitive)
                await page.locator("text=/accetta|consent|ok|chiudi/i").first.click(timeout=3000)
                print("   -> Cookie rimossi.")
            except:
                print("   -> Nessun cookie bloccante trovato.")

            # 3. CLICK NUMERO PERSONE (La parte critica)
            print(f"3. Cerco il bottone '{dati.persone}'...")
            
            # Aspettiamo che appaia la domanda chiave
            await page.wait_for_selector("text=/Quanti ospiti siete/i", timeout=15000)
            
            # TECNICA REGEX: Cerchiamo un elemento che contenga ESATTAMENTE il numero, 
            # ignorando spazi vuoti prima o dopo (es. " 3 ").
            # Cerchiamo dentro div, span o button.
            bottone_persone = page.locator(f"div, span, button").filter(has_text=re.compile(f"^\\s*{dati.persone}\\s*$")).first
            
            if await bottone_persone.count() > 0:
                print("   -> Bottone trovato! Clicco...")
                await bottone_persone.click(force=True)
            else:
                # FALLBACK: Se non lo trova, prova a cliccare tramite coordinate o testo parziale
                print("   -> Metodo 1 fallito. Provo Metodo 2 (Testo grezzo)...")
                await page.get_by_text(dati.persone, exact=True).first.click(force=True)

            # 4. GESTIONE SEGGIOLINI (Condizionale)
            print("4. Controllo Seggiolini...")
            await page.wait_for_timeout(2000) # Breve pausa per animazione
            
            # Se appare la scritta "seggiolini", clicchiamo NO
            if await page.locator("text=/seggiolini/i").count() > 0:
                print("   -> Domanda seggiolini trovata. Clicco NO.")
                # Cerchiamo il tasto NO con la regex per essere sicuri
                await page.locator("text=/^\\s*NO\\s*$/i").first.click(force=True)

            # 5. SELEZIONE DATA
            print(f"5. Seleziono data: {dati.data}")
            await page.wait_for_timeout(1000)
            
            oggi = datetime.now().date()
            data_req = datetime.strptime(dati.data, "%Y-%m-%d").date()
            
            if data_req == oggi:
                print("   -> Clicco OGGI")
                await page.locator("text=/^\\s*Oggi\\s*$/i").first.click(force=True)
            elif data_req == oggi + timedelta(days=1):
                print("   -> Clicco DOMANI")
                await page.locator("text=/^\\s*Domani\\s*$/i").first.click(force=True)
            else:
                print("   -> Clicco ALTRA DATA")
                await page.locator("text=/^\\s*Altra data\\s*$/i").first.click(force=True)
                await page.wait_for_timeout(500)
                
                # Riempimento input nascosto
                print("   -> Inserisco data nel calendario...")
                # Forziamo il valore via JS perché i calendari nativi sono ostici
                await page.evaluate(f"document.querySelector('input[type=date]').value = '{dati.data}'")
                # Simuliamo un invio per confermare
                await page.locator("input[type=date]").press("Enter")
                # Clicchiamo conferma se c'è
                try:
                    await page.locator("text=/conferma|cerca/i").first.click(timeout=2000)
                except:
                    pass

            # 6. LETTURA RISULTATI
            print("6. Analisi disponibilità...")
            await page.wait_for_timeout(4000) # Attendiamo il caricamento sedi
            
            html_finale = await page.content()
            testo_visibile = await page.inner_text("body")
            
            # Logica di controllo
            sedi = []
            keywords = ["Talenti", "Palermo", "Appia", "Ostia", "Reggio", "Tiburtina", "Trastevere"]
            
            for k in keywords:
                if k in testo_visibile:
                    sedi.append(k)
            
            await browser.close()
            
            if len(sedi) > 0:
                print(f"   -> SUCCESSO! Sedi trovate: {sedi}")
                return {"result": f"Buone notizie! Ho trovato posto per il {dati.data} nelle sedi: {', '.join(sedi)}. Quale preferisci?"}
            
            if "non ci sono" in testo_visibile.lower() or "completo" in testo_visibile.lower():
                return {"result": f"Mi dispiace, per il {dati.data} sembra tutto pieno."}

            return {"result": "Ho controllato e vedo disponibilità generica. Chiedi all'utente quale sede preferisce."}

        except Exception as e:
            # DEBUG ESTREMO: Se fallisce, stampiamo l'HTML per capire perché
            print(f"❌ ERRORE CRITICO: {str(e)}")
            try:
                # Salviamo un pezzo di HTML nei log per capire cosa vede il robot
                html_error = await page.inner_html("body")
                print(f"--- DUMP HTML (Prime 500 righe) ---\n{html_error[:2000]}\n-----------------------------------")
            except:
                pass
            
            await browser.close()
            return {"result": f"Errore tecnico durante il controllo: {str(e)}"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
