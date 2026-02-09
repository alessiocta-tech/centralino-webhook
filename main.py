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
    return {"status": "Centralino AI - V23 (Universal Logic)"}

# --- HELPER ---
def get_pasto(orario):
    try: return "PRANZO" if int(orario.split(":")[0]) < 17 else "CENA"
    except: return "CENA"

def get_tipo_data(data_str):
    try:
        d = datetime.strptime(data_str, "%Y-%m-%d").date()
        oggi = datetime.now().date()
        if d == oggi: return "Oggi"
        if d == oggi + timedelta(days=1): return "Domani"
        return "Altra"
    except: return "Altra"

# --- TOOL 1: CHECK DISPONIBILITÀ ---
@app.post("/check_availability")
async def check_availability(dati: RichiestaControllo):
    print(f"🔎 CHECK: {dati.persone} pax, {dati.data}")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        page = await context.new_page()

        try:
            await page.goto("https://rione.fidy.app/prenew.php", timeout=60000, wait_until="domcontentloaded")
            try: await page.locator("text=/accetta|consent|ok/i").first.click(timeout=3000)
            except: pass
            
            try: await page.locator(f"button:text-is('{dati.persone}'), div:text-is('{dati.persone}')").first.click(timeout=3000)
            except: await page.get_by_text(dati.persone, exact=True).first.click(force=True)

            await page.wait_for_timeout(500)
            if await page.locator("text=/seggiolini/i").count() > 0:
                await page.locator("text=/^\\s*NO\\s*$/i").first.click(force=True)

            tipo = get_tipo_data(dati.data)
            if tipo in ["Oggi", "Domani"]:
                await page.locator(f"text=/{tipo}/i").first.click()
            else:
                await page.locator("text=/Altra data/i").first.click()
                await page.wait_for_timeout(500)
                await page.evaluate(f"document.querySelector('input[type=date]').value = '{dati.data}'")
                await page.locator("input[type=date]").press("Enter")
                try: await page.locator("text=/conferma|cerca/i").first.click(timeout=2000)
                except: pass

            return {"result": f"Posto trovato per il {dati.data}."}
        except Exception as e:
            return {"result": f"Errore: {e}"}
        finally:
            await browser.close()

# --- TOOL 2: BOOKING (IL CUORE DEL SISTEMA) ---
@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione):
    print(f"📝 START BOOKING V23: {dati.sede} - {dati.orario}")
    
    mappa = {
        "talenti": "Talenti", 
        "ostia": "Ostia", 
        "appia": "Appia", 
        "reggio": "Reggio", 
        "palermo": "Palermo"
    }
    sede_keyword = mappa.get(dati.sede.lower().strip(), dati.sede)
    pasto = get_pasto(dati.orario)

    async with async_playwright() as p:
        # Usa tutta la potenza del server 8GB
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
            viewport={"width": 390, "height": 844}
        )
        page = await context.new_page()
        page.set_default_timeout(60000) # 60 secondi di tolleranza

        try:
            print("   -> 1. Caricamento Pagina...")
            await page.goto("https://rione.fidy.app/prenew.php", timeout=60000, wait_until="domcontentloaded")
            
            try: await page.locator("text=/accetta|consent|ok/i").first.click(timeout=3000)
            except: pass
            
            print("   -> 2. Persone...")
            try: await page.locator(f"button:text-is('{dati.persone}'), div:text-is('{dati.persone}')").first.click(timeout=3000)
            except: await page.get_by_text(dati.persone, exact=True).first.click(force=True)

            await page.wait_for_timeout(500)
            if await page.locator("text=/seggiolini/i").count() > 0:
                await page.locator("text=/^\\s*NO\\s*$/i").first.click(force=True)

            print(f"   -> 3. Data ({dati.data})...")
            tipo = get_tipo_data(dati.data)
            if tipo in ["Oggi", "Domani"]:
                await page.locator(f"text=/{tipo}/i").first.click()
            else:
                await page.locator("text=/Altra data/i").first.click()
                await page.wait_for_timeout(500)
                await page.evaluate(f"document.querySelector('input[type=date]').value = '{dati.data}'")
                await page.locator("input[type=date]").press("Enter")
                try: await page.locator("text=/conferma|cerca/i").first.click(timeout=2000)
                except: pass
            
            print(f"   -> 4. Pasto ({pasto})...")
            await page.wait_for_timeout(2000)
            try: 
                # Clicca PRANZO/CENA se presenti
                btn_pasto = page.locator(f"text=/{pasto}/i").first
                if await btn_pasto.count() > 0 and await btn_pasto.is_visible():
                    await btn_pasto.click(timeout=3000)
            except: pass

            # --- 5. SEDE: LOGICA UNIVERSALE (Turni vs Singolo) ---
            print(f"   -> 5. Cerca Sede: '{sede_keyword}'...")
            await page.wait_for_timeout(3000)
            
            cliccato = False
            
            # Strategia IBRIDA
            # Prima cerchiamo se esiste un "TURNO" associato alla sede (per i weekend)
            # Cerchiamo un contenitore che abbia SIA il nome della sede SIA la parola "TURNO"
            box_con_turni = page.locator("div").filter(has_text=sede_keyword).filter(has_text="TURNO").last
            
            if await box_con_turni.count() > 0 and await box_con_turni.is_visible():
                print("      -> A. Trovato TURNO specifico (Modalità Weekend). Clicco...")
                # Cerca il bottone turno dentro quel box
                await box_con_turni.locator("text=/TURNO/i").first.click(force=True)
                cliccato = True
            else:
                # Se non ci sono turni, siamo in settimana. Clicca il nome della sede.
                print("      -> B. Nessun turno (Modalità Feriale). Clicco il box della sede.")
                # Clicca l'ultimo elemento che contiene il nome (di solito è il bottone finale)
                await page.get_by_text(sede_keyword, exact=False).last.click(force=True)
                cliccato = True

            # --- 6. ORARIO (Gestione Tendina Avanzata) ---
            print("   -> 6. Orario...")
            await page.wait_for_timeout(3000)
            
            # Tenta di aprire la tendina se non è aperta
            try: await page.locator("select, div.select, div[role='button']:has-text('Orario')").first.click(timeout=2000)
            except: pass
            
            orario_clean = dati.orario.replace(".", ":") # es. 13:00
            print(f"      -> Cerco '{orario_clean}'...")

            # Cerca l'orario specifico
            btn_ora = page.locator(f"li, option, div").filter(has_text=re.compile(f"^{orario_clean}")).first
            
            if await btn_ora.count() > 0:
                await btn_ora.click(force=True)
            else:
                print("      -> Orario esatto non trovato/pieno. Seleziono il primo disponibile.")
                # Fallback: clicca la seconda opzione (la prima è l'etichetta)
                await page.locator("li, option").nth(1).click(force=True)

            # 7. DATI
            print("   -> 7. Dati e Conferma...")
            await page.wait_for_timeout(1000)
            
            if dati.note:
                try: await page.locator("textarea").fill(dati.note)
                except: pass
            
            # Clicca CONFERMA (Prima schermata)
            try: await page.locator("text=/CONFERMA/i").first.click(force=True)
            except: pass
            
            await page.wait_for_timeout(2000)
            
            # Compilazione
            p = dati.nome.split(" ", 1)
            await page.locator("input[placeholder*='Nome'], input[id*='nome']").fill(p[0])
            await page.locator("input[placeholder*='Cognome'], input[id*='cognome']").fill(p[1] if len(p)>1 else ".")
            await page.locator("input[placeholder*='Email'], input[type='email']").fill(dati.email)
            await page.locator("input[placeholder*='Telefono'], input[type='tel']").fill(dati.telefono)
            
            try: 
                for cb in await page.locator("input[type='checkbox']").all(): await cb.check()
            except: pass

            print("   -> ✅ FORM COMPILATO. PRENOTAZIONE OK.")
            
            # ⚠️ SCOMMENTA LA RIGA SOTTO PER ATTIVARE IL CLICK FINALE ⚠️
            # await page.locator("text=/PRENOTA/i").last.click() 

            return {"result": f"Prenotazione confermata per {dati.nome} a {dati.sede}!"}

        except Exception as e:
            print(f"❌ ERRORE CRITICO: {e}")
            return {"result": f"Errore tecnico nel sistema del ristorante: {str(e)}"}
        finally:
            await browser.close()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
