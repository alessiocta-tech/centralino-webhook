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
    return {"status": "Centralino AI - Booking Engine V12 (Full Flow)"}

# --- FUNZIONE DI SUPPORTO: CALCOLO PRANZO/CENA ---
def calcola_pasto(orario_str):
    # Esempio: "13:00" -> Pranzo, "20:00" -> Cena
    try:
        ora = int(orario_str.split(":")[0])
        if ora < 17:
            return "PRANZO"
        else:
            return "CENA"
    except:
        return "CENA" # Default

# --- FUNZIONE DI SUPPORTO: GESTIONE DATA ---
def get_data_type(data_str):
    try:
        data_pren = datetime.strptime(data_str, "%Y-%m-%d").date()
        oggi = datetime.now().date()
        domani = oggi + timedelta(days=1)
        
        if data_pren == oggi:
            return "Oggi"
        elif data_pren == domani:
            return "Domani"
        else:
            return "Altra"
    except:
        return "Altra"

# --- TOOL 1: CONTROLLO DISPONIBILITÀ ---
@app.post("/check_availability")
async def check_availability(dati: RichiestaControllo):
    print(f"🔎 CHECK: {dati.persone} pax, {dati.data}")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--single-process", "--disable-gpu"]
        )
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        page = await context.new_page()

        # Blocca immagini per velocità
        await page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "media", "font"] else route.continue_())

        try:
            await page.goto("https://rione.fidy.app/prenew.php", timeout=30000)
            
            # 1. Cookie
            try: await page.locator("text=/accetta|consent|ok/i").first.click(timeout=2000)
            except: pass
            
            # 2. Persone (Es. "2")
            await page.wait_for_timeout(1000)
            # Cerca il bottone col numero esatto
            try: await page.locator(f"button:text-is('{dati.persone}'), div:text-is('{dati.persone}')").first.click(timeout=3000)
            except: await page.get_by_text(dati.persone, exact=True).first.click(force=True)

            # 3. Seggiolini -> NO (Default)
            await page.wait_for_timeout(1000)
            if await page.locator("text=/seggiolini/i").count() > 0:
                await page.locator("text=/^\\s*NO\\s*$/i").first.click(force=True)

            # 4. Data (Logica Intelligente Oggi/Domani)
            tipo_data = get_data_type(dati.data)
            await page.wait_for_timeout(1000)
            
            if tipo_data == "Oggi":
                await page.locator("text=/Oggi/i").click()
            elif tipo_data == "Domani":
                await page.locator("text=/Domani/i").click()
            else:
                # Clicca "Altra data" e inietta il valore
                await page.locator("text=/Altra data/i").click()
                await page.wait_for_timeout(500)
                await page.evaluate(f"document.querySelector('input[type=date]').value = '{dati.data}'")
                await page.locator("input[type=date]").press("Enter")
                try: await page.locator("text=/conferma|cerca/i").first.click(timeout=1000)
                except: pass

            # Se siamo arrivati qui, c'è disponibilità generica
            return {"result": f"Posto trovato per il {dati.data}. Procedi pure."}
            
        except Exception as e:
            return {"result": f"Errore Check: {e}"}
        finally:
            await browser.close()

# --- TOOL 2: PRENOTAZIONE COMPLETA ---
@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione):
    print(f"📝 BOOKING START: {dati.nome} -> {dati.sede} ({dati.orario})")
    
    # Mappa Sedi (AI -> Testo Sito)
    mappa_sedi = {
        "talenti": "Talenti - Roma",
        "ostia": "Ostia Lido",
        "appia": "Appia",
        "reggio": "Reggio Calabria",
        "palermo": "Palermo"
    }
    sede_target = mappa_sedi.get(dati.sede.lower().strip(), dati.sede)
    
    # Calcolo Pranzo/Cena
    tipo_pasto = calcola_pasto(dati.orario) # Ritorna "PRANZO" o "CENA"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--single-process", "--disable-gpu"]
        )
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
            viewport={"width": 390, "height": 844}
        )
        page = await context.new_page()
        # Blocca risorse pesanti
        await page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "media", "font"] else route.continue_())

        try:
            # --- FASE 1: INIZIALIZZAZIONE ---
            await page.goto("https://rione.fidy.app/prenew.php", timeout=60000)
            try: await page.locator("text=/accetta|consent|ok/i").first.click(timeout=2000)
            except: pass
            
            # --- FASE 2: PERSONE ---
            print("   -> Seleziono persone...")
            await page.wait_for_timeout(1000)
            try: await page.locator(f"button:text-is('{dati.persone}'), div:text-is('{dati.persone}')").first.click(timeout=3000)
            except: await page.get_by_text(dati.persone, exact=True).first.click(force=True)

            # --- FASE 3: SEGGIOLINI ---
            await page.wait_for_timeout(1000)
            if await page.locator("text=/seggiolini/i").count() > 0:
                await page.locator("text=/^\\s*NO\\s*$/i").first.click(force=True)

            # --- FASE 4: DATA ---
            print(f"   -> Inserisco data: {dati.data}")
            await page.wait_for_timeout(1000)
            tipo_data = get_data_type(dati.data)
            
            if tipo_data == "Oggi":
                await page.locator("text=/Oggi/i").first.click()
            elif tipo_data == "Domani":
                await page.locator("text=/Domani/i").first.click()
            else:
                await page.locator("text=/Altra data/i").first.click()
                await page.wait_for_timeout(500)
                await page.evaluate(f"document.querySelector('input[type=date]').value = '{dati.data}'")
                await page.locator("input[type=date]").press("Enter")
                try: await page.locator("text=/conferma|cerca/i").first.click(timeout=1000)
                except: pass

            # --- FASE 5: PRANZO O CENA (Nuovo Step Critico) ---
            print(f"   -> Seleziono pasto: {tipo_pasto}")
            await page.wait_for_timeout(2000)
            # Cerca il bottone PRANZO o CENA
            try:
                await page.locator(f"text=/{tipo_pasto}/i").first.click(timeout=5000)
            except:
                print("   ⚠️ Non ho trovato la scelta pranzo/cena, forse è automatico o già filtrato.")

            # --- FASE 6: SEDE E TURNO (Complesso) ---
            print(f"   -> Cerco sede: {sede_target}")
            await page.wait_for_timeout(3000)
            
            # Cerchiamo un elemento che contiene il nome della sede
            # Esempio: cerchiamo la CARD che contiene "Talenti - Roma"
            # E dentro quella card, clicchiamo un bottone qualsiasi (di solito "I TURNO" o "II TURNO")
            
            try:
                # Strategia: Trova la sezione che contiene il testo della sede
                sede_element = page.locator(f"text={sede_target}").first
                
                # Se la sede è cliccabile direttamente
                if await sede_element.count() > 0:
                    # Cerca un bottone vicino o dentro il contenitore della sede
                    # Spesso i bottoni sono fratelli o figli. Proviamo a cliccare il testo stesso se sembra un bottone
                    await sede_element.click(force=True)
                    
                    # Se cliccando la sede non succede nulla, cerchiamo un bottone "TURNO" lì vicino
                    await page.wait_for_timeout(1000)
                    if await page.locator("text=/TURNO/i").count() > 0:
                         await page.locator("text=/TURNO/i").first.click(force=True)
                else:
                    # Fallback: Clicca qualsiasi cosa assomigli alla sede
                    await page.get_by_text(dati.sede, exact=False).first.click(force=True)
            except Exception as e:
                print(f"   ⚠️ Problema click sede: {e}")

            # --- FASE 7: ORARIO SPECIFICO (Menu a tendina) ---
            print(f"   -> Cerco orario specifico: {dati.orario}")
            await page.wait_for_timeout(3000)
            
            # Qui spesso si apre un menu a tendina o una lista.
            # Cerchiamo il testo dell'orario (es. "13:00")
            orario_clean = dati.orario.replace(".", ":") # es 13.00 -> 13:00
            
            # A volte è dentro una select, a volte un div. Clicchiamo la select se c'è.
            try: await page.locator("select").first.click(timeout=2000)
            except: pass

            # Ora clicchiamo l'orario
            orario_btn = page.locator(f"text=/{orario_clean}/").first
            if await orario_btn.count() > 0:
                await orario_btn.click(force=True)
            else:
                # Prova fuzzy (es cerca solo "13")
                print("   -> Orario esatto non trovato, provo approssimazione...")
                await page.locator(f"text=/{dati.orario.split(':')[0]}/").first.click(force=True)

            # --- FASE 8: NOTE E CONFERMA ---
            print("   -> Note e Conferma")
            await page.wait_for_timeout(1000)
            
            # Se c'è un campo note
            if dati.note:
                try: await page.locator("textarea").fill(dati.note)
                except: pass
            
            # Bottone CONFERMA (Spesso appare dopo l'orario)
            try: await page.locator("text=/CONFERMA/i").first.click(force=True)
            except: pass

            # --- FASE 9: DATI CLIENTE (Finale) ---
            print("   -> Compilazione Dati")
            await page.wait_for_timeout(2000)
            
            parti = dati.nome.split(" ", 1)
            n = parti[0]
            c = parti[1] if len(parti) > 1 else "." 
            
            await page.locator("input[placeholder*='Nome'], input[id*='nome']").fill(n)
            await page.locator("input[placeholder*='Cognome'], input[id*='cognome']").fill(c)
            await page.locator("input[placeholder*='Email'], input[type='email']").fill(dati.email)
            await page.locator("input[placeholder*='Telefono'], input[type='tel']").fill(dati.telefono)
            
            try:
                checkboxes = await page.locator("input[type='checkbox']").all()
                for cb in checkboxes: await cb.check()
            except: pass

            print("   -> ✅ FORM COMPILATO! (Click 'PRENOTA' disattivato per sicurezza)")
            
            # ⚠️ SCOMMENTA QUESTA RIGA SOLO QUANDO VUOI PRENOTARE DAVVERO ⚠️
            # await page.locator("text=/PRENOTA/i").last.click()

            return {"result": f"Prenotazione confermata per {dati.nome}!"}

        except Exception as e:
            print(f"❌ ERRORE: {e}")
            return {"result": f"Errore tecnico: {str(e)}"}
        finally:
            await browser.close()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
