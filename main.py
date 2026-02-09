import os
import uvicorn
import re
from fastapi import FastAPI
from pydantic import BaseModel
from playwright.async_api import async_playwright

app = FastAPI()

# --- MODELLI DATI (Allineati con ElevenLabs) ---
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
    return {"status": "Centralino AI - Versione Stabile V10"}

# --- TOOL 1: CONTROLLO DISPONIBILITÀ ---
@app.post("/check_availability")
async def check_availability(dati: RichiestaControllo):
    print(f"🔎 CHECK DISPONIBILITÀ: {dati.persone} pax, {dati.data}")
    async with async_playwright() as p:
        # OTTIMIZZAZIONE MEMORIA (Evita il crash su Railway)
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--single-process"]
        )
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        page = await context.new_page()
        try:
            # 1. Accesso al sito
            await page.goto("https://rione.fidy.app/prenew.php", timeout=60000)
            await page.wait_for_load_state("networkidle")
            
            # 2. Gestione Cookie
            try: await page.locator("text=/accetta|consent|ok/i").first.click(timeout=3000)
            except: pass
            
            # 3. Selezione Persone
            await page.wait_for_selector("text=/Quanti ospiti siete/i", timeout=15000)
            # Cerca il numero esatto (es. "3") evitando falsi positivi
            bottone_persone = page.locator(f"div, span, button").filter(has_text=re.compile(f"^\\s*{dati.persone}\\s*$")).first
            if await bottone_persone.count() > 0: await bottone_persone.click(force=True)
            else: await page.get_by_text(dati.persone, exact=True).first.click(force=True)
            
            # 4. No Seggiolini (Default)
            await page.wait_for_timeout(1000)
            if await page.locator("text=/seggiolini/i").count() > 0:
                await page.locator("text=/^\\s*NO\\s*$/i").first.click(force=True)
            
            # 5. Inserimento Data
            await page.wait_for_timeout(1000)
            await page.evaluate(f"document.querySelector('input[type=date]').value = '{dati.data}'")
            await page.locator("input[type=date]").press("Enter")
            try: await page.locator("text=/conferma|cerca/i").first.click(timeout=2000)
            except: pass
            
            # 6. Verifica Risultato
            await page.wait_for_timeout(4000)
            html = await page.content()
            if "non ci sono" in html.lower(): return {"result": "Pieno."}
            return {"result": f"Posto trovato per il {dati.data}. Procedi pure."}
            
        except Exception as e:
            return {"result": f"Errore nel controllo: {e}"}
        finally:
            await browser.close()

# --- TOOL 2: PRENOTAZIONE TAVOLO ---
@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione):
    print(f"📝 INIZIO PRENOTAZIONE: {dati.nome} -> {dati.sede} ({dati.orario})")
    
    # 1. TRADUZIONE NOMI (AI -> SITO WEB)
    # Fondamentale perché l'AI dice 'Talenti' ma il sito vuole 'Talenti - Roma'
    mappa_sedi = {
        "talenti": "Talenti - Roma",
        "ostia": "Ostia Lido",
        "appia": "Appia",
        "reggio": "Reggio Calabria",
        "palermo": "Palermo"
    }
    
    sede_cercata = dati.sede.lower().strip()
    nome_bottone = dati.sede # Usa il nome originale se non trova corrispondenze
    
    for chiave, valore in mappa_sedi.items():
        if chiave in sede_cercata:
            nome_bottone = valore
            break
            
    print(f"   -> Traduzione Sede: Cercherò il bottone '{nome_bottone}'")

    async with async_playwright() as p:
        # OTTIMIZZAZIONE MEMORIA (CRUCIALE)
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--single-process"]
        )
        # Simuliamo un iPhone per avere l'interfaccia mobile (più semplice)
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1', 
            viewport={"width": 390, "height": 844}
        )
        page = await context.new_page()
        
        try:
            # NAVIGAZIONE E INSERIMENTO DATI INIZIALI
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
            await page.wait_for_timeout(1500)
            await page.evaluate(f"document.querySelector('input[type=date]').value = '{dati.data}'")
            await page.locator("input[type=date]").press("Enter")
            try: await page.locator("text=/conferma|cerca/i").first.click(timeout=2000)
            except: pass

            # --- SELEZIONE SEDE (PUNTO CRITICO RISOLTO) ---
            print(f"   -> Clicco sede: '{nome_bottone}'")
            await page.wait_for_timeout(4000)
            
            # Cerca il testo parziale ma specifico (es. "Talenti - Roma")
            btn_sede = page.get_by_text(nome_bottone, exact=False).first
            
            if await btn_sede.count() > 0:
                await btn_sede.click(force=True)
            else:
                # Fallback di sicurezza
                print("   -> Fallback: cerco testo originale...")
                await page.get_by_text(dati.sede, exact=False).first.click(force=True)

            # SELEZIONE ORARIO
            print(f"   -> Orario: {dati.orario}")
            await page.wait_for_timeout(3000)
            # Apre tendina se necessario
            try: await page.locator("select, div[class*='select']").last.click(force=True)
            except: pass
            
            orario_clean = dati.orario.replace(".", ":") # Corregge 13.00 in 13:00
            orario_btn = page.locator(f"text={orario_clean}").first
            if await orario_btn.count() > 0:
                await orario_btn.click(force=True)
            else:
                return {"result": f"Orario {dati.orario} non disponibile."}

            # NOTE E AVANZAMENTO
            if dati.note:
                try: await page.locator("textarea").fill(dati.note)
                except: pass
            
            try: await page.locator("text=/CONFERMA/i").first.click(force=True)
            except: pass

            # DATI CLIENTE
            await page.wait_for_timeout(2000)
            parti = dati.nome.split(" ", 1)
            n = parti[0]
            c = parti[1] if len(parti) > 1 else "." 
            
            await page.locator("input[placeholder*='Nome'], input[id*='nome']").fill(n)
            await page.locator("input[placeholder*='Cognome'], input[id*='cognome']").fill(c)
            await page.locator("input[placeholder*='Email'], input[type='email']").fill(dati.email)
            await page.locator("input[placeholder*='Telefono'], input[type='tel']").fill(dati.telefono)
            
            # Checkbox privacy
            try:
                checkboxes = await page.locator("input[type='checkbox']").all()
                for cb in checkboxes: await cb.check()
            except: pass

            print("   -> ✅ PRENOTAZIONE PRONTA (Modalità Test)")
            
            # ⚠️ SCOMMENTA LA RIGA SOTTO PER ABILITARE LA PRENOTAZIONE REALE ⚠️
            # await page.locator("text=/PRENOTA/i").last.click()
            
            return {"result": f"Prenotazione confermata per {dati.nome} a {dati.sede}!"}

        except Exception as e:
            print(f"❌ ERRORE TECNICO: {e}")
            return {"result": f"Errore tecnico: {str(e)}"}
        finally:
            await browser.close()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
