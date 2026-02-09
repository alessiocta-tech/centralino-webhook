import os
import re
import uvicorn
from datetime import datetime, timedelta

from fastapi import FastAPI
from pydantic import BaseModel
from playwright.async_api import async_playwright, TimeoutError

app = FastAPI()


# =============================
# MODELS
# =============================

class BookingRequest(BaseModel):
    data: str          # YYYY-MM-DD
    persone: str
    orario: str        # HH:MM
    nome: str
    telefono: str
    email: str
    sede: str
    note: str = ""


# =============================
# HELPERS
# =============================

def normalize_sede(s: str) -> str:
    s = s.lower().strip()

    if "talent" in s:
        return "Talenti"
    if "appia" in s:
        return "Appia"
    if "ostia" in s:
        return "Ostia"
    if "reggio" in s:
        return "Reggio"
    if "palermo" in s:
        return "Palermo"

    return s


def get_pasto(orario: str) -> str:
    h, m = map(int, orario.split(":"))
    t = h * 60 + m

    # Pranzo 12–14:30
    if 720 <= t <= 870:
        return "PRANZO"

    # Cena 19–22
    if 1140 <= t <= 1320:
        return "CENA"

    return "PRANZO" if h < 17 else "CENA"


def choose_turno(orario: str) -> str:
    h, m = map(int, orario.split(":"))
    t = h * 60 + m

    return "I TURNO" if t <= 1230 else "II TURNO"


async def safe_click(el, label: str):
    print(f"      -> Click {label}")

    await el.wait_for(state="visible", timeout=20000)
    await el.scroll_into_view_if_needed()

    try:
        await el.click(timeout=8000)
    except:
        await el.click(force=True)


async def wait_next(page):
    """Aspetta caricamento ajax"""

    await page.wait_for_load_state("networkidle", timeout=20000)


# =============================
# FLOW
# =============================

async def select_persone(page, n):

    btn = page.get_by_role("button", name=re.compile(f"^{n}$"))

    await safe_click(btn.first, f"persone={n}")
    await wait_next(page)


async def select_seggiolone(page, note):

    target = "SI" if "seggiol" in note.lower() else "NO"

    btn = page.get_by_role("button", name=re.compile(f"^{target}$", re.I))

    await safe_click(btn.first, f"seggiolone={target}")
    await wait_next(page)


async def select_data(page, data):

    target = datetime.strptime(data, "%Y-%m-%d").date()
    today = datetime.now().date()

    if target == today:
        btn = page.get_by_role("button", name="Oggi")
        await safe_click(btn.first, "oggi")
        return

    if target == today + timedelta(days=1):
        btn = page.get_by_role("button", name="Domani")
        await safe_click(btn.first, "domani")
        return

    btn = page.get_by_role("button", name=re.compile("Altra", re.I))
    await safe_click(btn.first, "altra_data")

    await page.locator("input[type=date]").fill(data)
    await page.keyboard.press("Enter")

    await wait_next(page)


async def select_pasto(page, pasto):

    print(f"      -> Pasto {pasto}")

    btn = page.get_by_role("button", name=re.compile(pasto, re.I))

    await safe_click(btn.first, f"pasto={pasto}")
    await wait_next(page)


async def select_sede_turno(page, sede, orario):

    print(f"      -> Sede {sede}")

    row = page.locator("div").filter(has_text=re.compile(sede, re.I)).first

    await row.wait_for(timeout=20000)

    # Turni?
    turni = row.get_by_role("button", name=re.compile("TURNO", re.I))

    if await turni.count() > 0:

        print("      -> Modalità WEEKEND")

        target = choose_turno(orario)

        btn = row.get_by_role(
            "button",
            name=re.compile(f"^{target}$", re.I)
        )

        if await btn.count() == 0:
            btn = turni.first

        await safe_click(btn, f"turno={target}")

    else:

        print("      -> Modalità FERIALE")

        btn = row.get_by_role("button").first

        if await btn.count() > 0:
            await safe_click(btn, "sede_btn")
        else:
            await safe_click(row, "sede_box")

    await wait_next(page)


async def select_orario(page, orario):

    try:
        await page.locator("select").click(timeout=3000)
    except:
        pass

    opt = page.get_by_text(orario)

    await safe_click(opt.first, f"orario={orario}")

    await wait_next(page)


# =============================
# ENDPOINT
# =============================

@app.post("/book_table")
async def book_table(dati: BookingRequest):

    sede = normalize_sede(dati.sede)
    pasto = get_pasto(dati.orario)

    print(f"🚀 BOOKING {dati.nome} → {sede} {dati.data} {dati.orario}")

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox"]
        )

        context = await browser.new_context(
            viewport={"width": 390, "height": 844}
        )

        page = await context.new_page()
        page.set_default_timeout(60000)

        try:

            await page.goto(
                "https://rione.fidy.app/prenew.php?referer=sito",
                wait_until="domcontentloaded"
            )

            # Cookie
            try:
                await page.locator("text=/accetta|ok/i").first.click(timeout=3000)
            except:
                pass


            print("-> 1 Persone")
            await select_persone(page, dati.persone)

            print("-> 2 Seggiolone")
            await select_seggiolone(page, dati.note)

            print("-> 3 Data")
            await select_data(page, dati.data)

            print("-> 4 Pasto")
            await select_pasto(page, pasto)

            print("-> 5 Sede/Turno")
            await select_sede_turno(page, sede, dati.orario)

            print("-> 6 Orario")
            await select_orario(page, dati.orario)

            print("-> 7 Conferma")
            await page.get_by_text("CONFERMA").click()

            await wait_next(page)

            print("-> 8 Dati finali")

            nome, *cogn = dati.nome.split()
            cognome = " ".join(cogn)

            await page.locator("input[type=text]").nth(0).fill(nome)
            await page.locator("input[type=text]").nth(1).fill(cognome)
            await page.locator("input[type=email]").fill(dati.email)
            await page.locator("input[type=tel]").fill(dati.telefono)

            for cb in await page.locator("input[type=checkbox]").all():
                await cb.check()

            # === LIVE ===
            # await page.get_by_text("PRENOTA").click()

            print("✅ PRENOTAZIONE COMPLETATA")

            return {
                "result": "OK",
                "status": "Prenotazione completata"
            }


        except Exception as e:

            print("❌ ERRORE:", str(e))

            return {
                "result": "ERROR",
                "message": str(e)
            }

        finally:

            await browser.close()


# =============================
# RUN
# =============================

if __name__ == "__main__":

    port = int(os.environ.get("PORT", 8000))

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port
    )
