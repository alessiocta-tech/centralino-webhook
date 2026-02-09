import os
import re
import asyncio
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

app = FastAPI()


# ----------------------------
# MODELS
# ----------------------------
class RichiestaPrenotazione(BaseModel):
    data: str = Field(..., description="YYYY-MM-DD")
    persone: str = Field(..., description="Numero persone")
    orario: str = Field(..., description="HH:MM")
    nome: str = Field(..., description="Nome e Cognome")
    telefono: str
    email: str
    sede: str = Field(..., description="Talenti, Appia, Ostia, Reggio, Palermo")
    note: str = ""


# ----------------------------
# HELPERS
# ----------------------------
def normalize_sede(raw: str) -> str:
    """
    Normalizza la sede alle label reali viste nelle schermate.
    """
    s = (raw or "").strip().lower()
    mapping = {
        "talenti": "Talenti - Roma",
        "talenti roma": "Talenti - Roma",
        "roma talenti": "Talenti - Roma",
        "appia": "Appia",
        "ostia": "Ostia Lido",
        "ostia lido": "Ostia Lido",
        "reggio": "Reggio Calabria",
        "reggio calabria": "Reggio Calabria",
        "palermo": "Palermo",
    }
    return mapping.get(s, raw.strip())


def needs_seggiolone(note: str) -> bool:
    n = (note or "").lower()
    return any(k in n for k in ["seggiolon", "bimbo", "bambin", "bambina", "baby"])


def choose_turno(orario: str) -> str:
    """
    Regola pratica (modifica se vuoi):
    - <= 20:30 -> I TURNO
    - >= 21:00 -> II TURNO
    """
    try:
        h, m = orario.split(":")
        minutes = int(h) * 60 + int(m)
        return "I TURNO" if minutes <= (20 * 60 + 30) else "II TURNO"
    except:
        return "I TURNO"


def split_name(full: str) -> tuple[str, str]:
    parts = (full or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], "."
    return parts[0], " ".join(parts[1:])


def ensure_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except:
        pass


async def screenshot(page, name: str) -> Optional[str]:
    ensure_dir("/tmp/screens")
    ts = int(datetime.now().timestamp())
    path = f"/tmp/screens/{name}_{ts}.png"
    try:
        await page.screenshot(path=path, full_page=True)
        return path
    except:
        return None


async def safe_click(locator, label: str, timeout_ms: int = 15000) -> None:
    """
    Click robusto: wait visible, scroll, click normale; se fallisce -> force.
    """
    await locator.wait_for(state="visible", timeout=timeout_ms)
    await locator.scroll_into_view_if_needed()
    try:
        await locator.click(timeout=timeout_ms)
    except Exception:
        await locator.click(timeout=timeout_ms, force=True)


async def wait_for_step_orario(page, timeout_ms: int = 25000) -> None:
    """
    Dopo sede/turno, lo step successivo deve mostrare "Orario".
    """
    await page.get_by_text("Orario", exact=False).wait_for(state="visible", timeout=timeout_ms)


# ----------------------------
# FLOW STEPS
# ----------------------------
async def select_persone(page, persone: str) -> None:
    # Nella UI ci sono bottoni 1..9. Provo role=button, poi fallback testo.
    p = persone.strip()
    btn = page.get_by_role("button", name=re.compile(rf"^{re.escape(p)}$")).first
    if await btn.count() > 0:
        await safe_click(btn, f"persone={p}")
        return

    # fallback: riquadri che contengono il numero
    await safe_click(page.get_by_text(p, exact=True).first, f"persone_text={p}")


async def select_seggiolone(page, yes: bool) -> None:
    # Bottoni "NO" "SI"
    target = "SI" if yes else "NO"
    b = page.get_by_role("button", name=re.compile(rf"^{target}$", re.I)).first
    if await b.count() > 0:
        await safe_click(b, f"seggiolone={target}")
        return
    # fallback testo
    await safe_click(page.get_by_text(target, exact=True).first, f"seggiolone_text={target}")


async def select_data(page, data_yyyy_mm_dd: str) -> None:
    """
    Provo prima Oggi/Domani se coincide, altrimenti "Altra data" e input date.
    """
    target = datetime.strptime(data_yyyy_mm_dd, "%Y-%m-%d").date()
    today = datetime.now().date()

    if target == today:
        btn = page.get_by_role("button", name=re.compile(r"^Oggi$", re.I)).first
        if await btn.count() > 0:
            await safe_click(btn, "data=oggi")
            return

    if target == (today + timedelta(days=1)):
        btn = page.get_by_role("button", name=re.compile(r"^Domani$", re.I)).first
        if await btn.count() > 0:
            await safe_click(btn, "data=domani")
            return

    # Altra data
    altra = page.get_by_role("button", name=re.compile(r"Altra\s+data", re.I)).first
    if await altra.count() > 0:
        await safe_click(altra, "data=altra_data")
    else:
        await safe_click(page.get_by_text("Altra data", exact=False).first, "data=altra_data_text")

    # Molti datepicker sono input type="date"
    date_input = page.locator('input[type="date"]').first
    if await date_input.count() > 0:
        await date_input.fill(data_yyyy_mm_dd)
        # spesso serve Enter o blur
        await date_input.press("Enter")
        return

    # fallback: se non è un input date, provo a cliccare il giorno nel calendario
    # (non perfetto, ma salva situazioni)
    day = str(target.day)
    await safe_click(page.get_by_text(day, exact=True).first, f"data_day={day}")


async def select_sede_and_turno(page, sede_label: str, orario: str) -> None:
    """
    V24: trova la "riga" della sede e gestisce:
    - scenario B: bottoni I/II TURNO dentro la riga
    - scenario A: clic sul contenitore cliccabile della riga
    Con retry + verifica avanzamento (Orario).
    """

    # 1) trova il testo sede (preferibilmente label completa es: "Talenti - Roma")
    sede_text = page.get_by_text(re.compile(re.escape(sede_label), re.I)).first
    await sede_text.wait_for(state="visible", timeout=25000)

    # 2) risali a un contenitore “riga” coerente
    # Tentiamo: il primo ancestor div "grande" (molto spesso è la card).
    row = sede_text.locator("xpath=ancestor::div[1]")

    # 3) dentro la riga cerco bottoni turno
    turno_buttons = row.get_by_role("button", name=re.compile(r"\bI TURNO\b|\bII TURNO\b", re.I))
    desired_turno = choose_turno(orario)

    async def try_select_once() -> bool:
        # Weekend: click TURNO specifico
        if await turno_buttons.count() > 0:
            btn = row.get_by_role("button", name=re.compile(rf"^{re.escape(desired_turno)}$", re.I)).first
            if await btn.count() == 0:
                btn = turno_buttons.first
            await safe_click(btn, f"turno={desired_turno}")
        else:
            # Feriale: clicca il vero elemento cliccabile della riga
            # Priorità: un vero button/link dentro la riga
            inner_btn = row.get_by_role("button").first
            if await inner_btn.count() > 0:
                await safe_click(inner_btn, f"sede_inner_button={sede_label}")
            else:
                # prova a cliccare un ancestor con ruolo button (se presente)
                role_btn = sede_text.locator("xpath=ancestor::*[@role='button'][1]")
                if await role_btn.count() > 0:
                    await safe_click(role_btn, f"sede_role_button={sede_label}")
                else:
                    # ultimo fallback: clic sul container row
                    await safe_click(row, f"sede_row={sede_label}")

        # Verifica avanzamento: compare "Orario"
        try:
            await wait_for_step_orario(page, timeout_ms=15000)
            return True
        except PWTimeout:
            return False

    # 4) retry intelligente: se non avanza, riprovo cliccando vari target
    ok = await try_select_once()
    if ok:
        return

    # retry 1: clic direttamente sul testo sede (a volte è link)
    try:
        await safe_click(sede_text, f"sede_text={sede_label}")
        await wait_for_step_orario(page, timeout_ms=15000)
        return
    except Exception:
        pass

    # retry 2: clic su un ancestor più alto (card più estesa)
    try:
        bigger = sede_text.locator("xpath=ancestor::div[2]")
        if await bigger.count() > 0:
            await safe_click(bigger, f"sede_big_container={sede_label}")
            await wait_for_step_orario(page, timeout_ms=15000)
            return
    except Exception:
        pass

    # Se ancora nulla, fail esplicito con screenshot
    raise RuntimeError(f"Selezione sede/turno fallita per '{sede_label}' (nessun avanzamento a 'Orario').")


async def select_orario(page, orario: str) -> None:
    """
    Dalle schermate: campo dropdown “Orario” che apre una lista con orari cliccabili.
    """
    # prova combobox
    combo = page.get_by_role("combobox").first
    if await combo.count() > 0 and await combo.is_visible():
        await safe_click(combo, "orario_combobox")
    else:
        # fallback: clic sul blocco vicino "Orario"
        await safe_click(page.get_by_text("Orario", exact=False).first, "orario_label")

    # scegli orario esatto
    opt = page.get_by_text(orario, exact=True).first
    await safe_click(opt, f"orario={orario}")


async def proceed_conferma(page) -> None:
    btn = page.get_by_role("button", name=re.compile(r"CONFERMA", re.I)).first
    if await btn.count() > 0:
        await safe_click(btn, "conferma")
        return
    await safe_click(page.get_by_text("CONFERMA", exact=False).first, "conferma_text")


async def fill_final_form_and_submit(page, nome: str, telefono: str, email: str) -> None:
    first, last = split_name(nome)

    # campi finali: Nome*, Cognome*, Email*, Telefono*
    await page.get_by_label("Nome*", exact=False).fill(first or ".")
    await page.get_by_label("Cognome*", exact=False).fill(last or ".")
    await page.get_by_label("Email*", exact=False).fill(email)
    await page.get_by_label("Telefono*", exact=False).fill(telefono)

    btn = page.get_by_role("button", name=re.compile(r"PRENOTA", re.I)).first
    if await btn.count() > 0:
        await safe_click(btn, "prenota")
        return
    await safe_click(page.get_by_text("PRENOTA", exact=False).first, "prenota_text")


# ----------------------------
# MAIN ENDPOINT
# ----------------------------
@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione):
    sede_label = normalize_sede(dati.sede)
    seggiolone = needs_seggiolone(dati.note)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu"]
        )
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        page = await context.new_page()
        page.set_default_timeout(60000)

        try:
            print(f"✍️ START BOOKING V24: sede={sede_label}, data={dati.data}, ora={dati.orario}, persone={dati.persone}")

            await page.goto("https://rione.fidy.app/prenew.php?referer=sito", wait_until="domcontentloaded")
            await page.wait_for_timeout(500)

            # 1) Persone
            print("-> 1. Selezione persone...")
            await select_persone(page, dati.persone)

            # 2) Seggiolone
            print("-> 2. Seggiolone...")
            await select_seggiolone(page, seggiolone)

            # 3) Data
            print(f"-> 3. Data ({dati.data})...")
            await select_data(page, dati.data)

            # 4) Sede / Turno (critico)
            print(f"-> 4. Selezione sede/turno: '{sede_label}'...")
            await select_sede_and_turno(page, sede_label, dati.orario)

            # 5) Orario
            print(f"-> 5. Selezione orario: {dati.orario} ...")
            await select_orario(page, dati.orario)

            # 6) Note (se c’è textarea)
            if dati.note:
                print("-> 6. Note...")
                ta = page.locator("textarea").first
                if await ta.count() > 0:
                    await ta.fill(dati.note)

            # 7) Conferma
            print("-> 7. Conferma...")
            await proceed_conferma(page)

            # 8) Dati finali + Prenota
            print("-> 8. Dati finali + PRENOTA...")
            await fill_final_form_and_submit(page, dati.nome, dati.telefono, dati.email)

            # (Opzionale) attesa mini per eventuale pagina di successo
            await page.wait_for_timeout(800)

            return {
                "result": "Success",
                "sede": sede_label,
                "data": dati.data,
                "orario": dati.orario,
                "persone": dati.persone,
                "turno": choose_turno(dati.orario),
                "seggiolone": seggiolone
            }

        except PWTimeout as e:
            path = await screenshot(page, "timeout")
            return {
                "result": f"Error: Timeout - {str(e)}",
                "debug_screenshot": path
            }

        except Exception as e:
            path = await screenshot(page, "error")
            return {
                "result": f"Error: {str(e)}",
                "debug_screenshot": path
            }

        finally:
            await browser.close()


# ----------------------------
# LOCAL RUN (optional)
# ----------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
