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
    return {"status": "Centralino AI - V20 (Tree Climber)"}

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

# --- TOOL 1: CHECK ---
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

# --- TOOL 2: BOOKING ---
@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione):
    print(f"📝 START BOOKING V20: {dati.sede} - {dati.orario}")
    
    mappa = {
        "talenti": "Talenti - Roma", 
        "ostia": "Ostia Lido", 
        "appia": "Appia", 
        "reggio": "Reggio Calabria", 
        "palermo": "Palermo"
    }
    sede_target = mappa.get(dati.sede.lower().strip(), dati.sede)
    pasto = get_pasto(dati.orario)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        context = await browser.new_context(
            user_agent='Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
            viewport={"width": 390, "height": 844}
        )
        page = await context.new_page()
        page.set_default_timeout(60000)

        try:
            print("   -> 1. Caricamento...")
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
                btn_pasto = page.locator(f"text=/{pasto}/i").first
                if await btn_pasto.count() > 0 and await btn_pasto.is_visible():
                    await btn_pasto.click(timeout=3000)
            except: 
                print("      (Pasto skip)")

            # --- 5. SEDE & TURNO (TREE CLIMBER) ---
            print(f"   -> 5. Cerca Sede: '{sede_target}' (Scalatore)...")
            await page.wait_for_timeout(3000)
            
            cliccato = False
            
            # Cerca l'elemento che contiene esattamente il nome della sede
            target_text = page.locator(f"text={sede_target}").first
            
            if await target_text.count() > 0:
                # Inizia a scalare verso l'alto (Padre -> Nonno -> Bisnonno)
                elemento_corrente = target_text
                found_card = None
                
                # Prova a risalire per 5 livelli
                for i in range(5):
                    # Cerca bottone turno dentro l'elemento corrente
                    btn_turno = elemento_corrente.locator("text=/TURNO/i").first
                    if await btn_turno.count() > 0:
                        print(f"      -> Trovato TURNO al livello {i}! Clicco 'I TURNO'...")
                        # Se ci sono più turni, clicchiamo il primo (I TURNO) che di solito è per le 12:00-13:00
                        # Se l'utente volesse il II turno, servirebbe logica extra, ma per ora priorità al click.
                        await btn_turno.click(force=True)
                        cliccato = True
                        break
                    
                    # Sali di un livello
                    elemento_corrente = elemento_corrente.locator("..")
            
            if not cliccato:
                print("      -> Fallback: Cerco 'I TURNO' globale vicino alla sede.")
                # Prova generica
                await page.locator("div").filter(has_text=sede_target).filter(has_text="TURNO").locator("text=/TURNO/i").first.click(force=True, timeout=5000)

            # 6. ORARIO
            print("   -> 6. Orario...")
            await page.wait_for_timeout(3000)
            try: await page.locator("select").first.click(timeout=2000)
            except: pass
            
            orario_clean = dati.orario.replace(".", ":")
            try: await page.locator(f"text=/{orario_clean}/").first.click(timeout=3000)
            except: await page.locator("select option").nth(1).click()

            # 7. DATI
            print("   -> 7. Conferma...")
            if dati.note:
                try: await page.locator("textarea").fill(dati.note)
                except: pass
            
            try: await page.locator("text=/CONFERMA/i").first.click(force=True)
            except: pass
            
            await page.wait_for_timeout(2000)
            p = dati.nome.split(" ", 1)
            await page.locator("input[placeholder*='Nome'], input[id*='nome']").fill(p[0])
            await page.locator("input[placeholder*='Cognome'], input[id*='cognome']").fill(p[1] if len(p)>1 else ".")
            await page.locator("input[placeholder*='Email'], input[type='email']").fill(dati.email)
            await page.locator("input[placeholder*='Telefono'], input[type='tel']").fill(dati.telefono)
            
            try: 
                for cb in await page.locator("input[type='checkbox']").all(): await cb.check()
            except: pass

            print("   -> ✅ OK!")
            # ATTIVA PER PRENOTARE REALE:
            # await page.locator("text=/PRENOTA/i").last.click() 

            return {"result": f"Prenotazione confermata per {dati.nome}!"}

        except Exception as e:
            print(f"❌ ERRORE: {e}")
            return {"result": f"Errore: {str(e)}"}
        finally:
            await browser.close()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
