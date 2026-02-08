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
    return {"status": "Centralino Debugger Attivo!"}

@app.post("/check_availability")
async def check_availability(dati: Prenotazione):
    print(f"Richiesta: {dati.data}, Persone: {dati.persone}")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        try:
            print("1. Carico pagina...")
            await page.goto("https://rione.fidy.app/prenew.php?referer=AI", timeout=60000)
            await page.wait_for_load_state("domcontentloaded")
            
            # --- DEBUG: STAMPA COSA VEDE IL ROBOT ---
            print("2. Analisi della pagina:")
            title = await page.title()
            print(f"   Titolo Pagina: {title}")
            
            # Cerchiamo tutti gli input presenti e li stampiamo nei log
            inputs = await page.evaluate("""() => {
                return Array.from(document.querySelectorAll('input')).map(i => 
                    `Type: ${i.type}, Name: ${i.name}, ID: ${i.id}, Visible: ${i.offsetParent !== null}`
                )
            }""")
            print(f"   Campi trovati: {inputs}")
            # ----------------------------------------

            print("3. Provo inserimento forzato (Javascript)...")
            
            # TENTATIVO 1: Inserimento diretto via JS (Bypassa i calendari grafici)
            # Cerchiamo di riempire qualsiasi campo che sembri una data
            await page.evaluate(f"""() => {{
                // Cerca campi data
                const dateInputs = document.querySelectorAll('input[type="date"], input[name*="date"], input[name*="data"]');
                dateInputs.forEach(input => {{ input.value = '{dati.data}'; }});
                
                // Cerca campi persone (pax, coperti, number)
                const paxInputs = document.querySelectorAll('input[type="number"], input[name*="pax"], input[name*="persone"]');
                paxInputs.forEach(input => {{ input.value = '{dati.persone}'; }});
            }}""")

            print("4. Clicco Cerca...")
            # Clicchiamo qualsiasi bottone di submit
            await page.click("button[type='submit'], input[type='submit']")
            
            # Attesa risultati
            await page.wait_for_timeout(4000)
            testo = await page.inner_text("body")
            await browser.close()
            
            if "non ci sono" in testo.lower() or "nessuna disp" in testo.lower():
                return {"result": "Tutto pieno."}
            
            return {"result": f"Ho controllato. Disponibilità trovata per il {dati.data}. Chiedi l'orario."}

        except Exception as e:
            await browser.close()
            print(f"ERRORE CRITICO: {e}")
            # Importante: ora l'AI ti leggerà l'errore se fallisce ancora
            return {"result": f"Errore tecnico nel form: {str(e)}"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
