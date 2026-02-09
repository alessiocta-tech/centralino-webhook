import os
import re
import uvicorn
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright

app = FastAPI()


# =============================
# MODELS
# =============================
class RichiestaControllo(BaseModel):
    data: str
    persone: str


class RichiestaPrenotazione(BaseModel):
    data: str = Field(..., description="YYYY-MM-DD")
    persone: str = Field(..., description="Numero persone")
    orario: str = Field(..., description="HH:MM")
    nome: str = Field(..., description="Nome e Cognome")
    telefono: str
    email: str
    sede: str = Field(..., description="Talenti, Appia, Ostia, Reggio, Palermo")
    note: str = ""


@app.get("/")
def home():
    return {"status": "Centralino AI - V28 (No networkidle, Step-based waits)"}


# =============================
# HELPERS
# =============================
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


def parse_minutes(orario: str) -> int:
    try:
        h, m = orario.strip().split(":")
        return int(h) * 60 + int(m)
    except Exception:
        raise ValueError(f"Formato orario non valido: '{orario}'. Usa HH:MM (es. 13:30).")


def get_pasto_rigido(orario: str) -> str:
    """
    PRANZO: 12:00 - 14:30
    CENA:   19:00 - 22:00
    """
    t = parse_minutes(orario)
    if 12 * 60 <= t <= 14 * 60 + 30:
        return "PRANZO"
    if 19 * 60 <= t <= 22 * 60:
        return "CENA"
    raise ValueError(
        f"Orario non prenotabile ({orario}). Fasce: PRANZO 12:00–14:30, CENA 19:00–22:00."
    )


def normalize_sede(raw: str) -> str:
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
    for k, v in mapping.items():
        if k in s:
            return v
    return raw.strip()


def needs_seggiolone(note: str) -> bool:
    n = (note or "").lower()
    return any(k in n for k in ["seggiolon", "bimbo", "bambin", "bambina", "baby"])


def choose_turno(orario: str) -> str:
    """
    <= 20:30 => I TURNO
    >  20:30 => II TURNO
    """
    try:
        t = parse_minutes(orario)
        return "I TURNO" if t <= (20 * 60 + 30) else "II TURNO"
    except:
        return "I TURNO"


def split_name(full: str) -> tuple[str, str]:
    parts = (full or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], "."
    return parts[0], " ".join(parts[1:])


async def accept_cookies_if_any(page) -> None:
    try:
        await page.locator("text=/accetta|consent|ok/i").first.click(timeout=2500)
    except:
        pass


async def safe_click(locator, label: str, timeout_ms: int = 20000) -> None:
    """
    Click serio: se fallisce -> raise.
    """
    try:
        await locator.wait_for(state="visible", timeout=timeout_ms)
        await locator.scroll_into_view_if_needed()
        try:
            await locator.click(timeout=timeout_ms)
        except:
            await locator.click(timeout=timeout_ms, force=True)
    except Exception as e:
        raise RuntimeError(f"Click fallito su '{label}': {e}")


# =============================
# STEP-BASED WAITS (trigger UI reali)
# =============================
async def wait_step_seggiolone(page, timeout=25000):
    await page.wait_for_selector("text=/seggiolin/i", timeout=timeout)


async def wait_step_quando_verrete(page, timeout=25000):
    await page.wait_for_selector("text=/Quando verrete/i", timeout=timeout)


async def wait_step_pasto(page, timeout=25000):
    # in pratica compare PRANZO/CENA
    await page.wait_for_selector("text=/PRANZO|CENA/i", timeout=timeout)


async def wait_step_sedi(page, timeout=25000):
    # almeno una sede visibile (Talenti appare sempre)
    await page.wait_for_selector("text=/Talenti|Appia|Ostia|Reggio|Palermo/i", timeout=timeout)


async def wait_step_orario(page, timeout=25000):
    await page.wait_for_selector("text=/Orario/i", timeout=timeout)


async def wait_step_ci_siamo_quasi(page, timeout=30000):
    await page.wait_for_selector("text=/Ci siamo quasi/i", timeout=timeout)


# =============================
# FLOW STEPS
# =============================
async def select_persone(page, persone: str) -> None:
    p = persone.strip()
    btn = page.get_by_role("button", name=re.compile(rf"\b{re.escape(p)}\b")).first
    await safe_click(btn, f"persone={p}")

    # ✅ trigger reale: compare step seggiolone
    await wait_step_seggiolone(page)


async def select_seggiolone(page, yes: bool) -> None:
    target = "SI" if yes else "NO"
    btn = page.get_by_role("button", name=re.compile(rf"^{target}$", re.I)).first
    if await btn.count() == 0:
        btn = page.get_by_text(target, exact=True).first

    await safe_click(btn, f"seggiolone={target}")

    # ✅ trigger reale: compare "Quando verrete?"
    await wait_step_quando_verrete(page)


async def select_data(page, data_str: str) -> None:
    target = datetime.strptime(data_str, "%Y-%m-%d").date()
    today = datetime.now().date()

    if target == today:
        btn = page.get_by_role("button", name=re.compile(r"^Oggi$", re.I)).first
        if await btn.count() > 0:
            await safe_click(btn, "data=oggi")
            await wait_step_pasto(page)
            return

    if target == today + timedelta(days=1):
        btn = page.get_by_role("button", name=re.compile(r"^Domani$", re.I)).first
        if await btn.count() > 0:
            await safe_click(btn, "data=domani")
            await wait_step_pasto(page)
            return

    altra = page.get_by_role("button", name=re.compile(r"Altra\s+data", re.I)).first
    if await altra.count() == 0:
        altra = page.get_by_text("Altra data", exact=False).first

    await safe_click(altra, "data=altra_data")

    date_input = page.locator("input[type='date']").first
    if await date_input.count() == 0:
        raise RuntimeError("Input data (type=date) non trovato dopo 'Altra data'.")

    await date_input.fill(data_str)
    await date_input.press("Enter")

    await wait_step_pasto(page)


async def select_pasto(page, pasto: str) -> None:
    await wait_step_pasto(page)

    btn = page.get_by_role("button", name=re.compile(rf"^{pasto}$", re.I)).first
    if await btn.count() == 0:
        btn = page.get_by_text(re.compile(rf"^{pasto}$", re.I)).first

    # 1° click
    await safe_click(btn, f"pasto={pasto}")

    # ✅ trigger reale: compaiono le sedi
    try:
        await wait_step_sedi(page, timeout=12000)
        return
    except:
        # retry click
        print("      -> Pasto non recepito, retry...")
        await safe_click(btn, f"pasto_retry={pasto}")
        await wait_step_sedi(page, timeout=20000)


async def select_sede_and_turno(page, sede_label: str, orario: str) -> None:
    await wait_step_sedi(page)

    sede_text = page.get_by_text(re.compile(re.escape(sede_label), re.I)).first
    await sede_text.wait_for(state="visible", timeout=25000)

    row = sede_text.locator("xpath=ancestor::div[1]")

    turno_buttons = row.get_by_role("button", name=re.compile(r"\bI TURNO\b|\bII TURNO\b", re.I))
    if await turno_buttons.count() > 0:
        desired = choose_turno(orario)
        btn = row.get_by_role("button", name=re.compile(rf"^{re.escape(desired)}$", re.I)).first
        if await btn.count() == 0:
            btn = turno_buttons.first
        await safe_click(btn, f"turno={desired}")
    else:
        inner_btn = row.get_by_role("button").first
        if await inner_btn.count() > 0:
            await safe_click(inner_btn, f"sede_inner_button={sede_label}")
        else:
            role_btn = sede_text.locator("xpath=ancestor::*[@role='button'][1]")
            if await role_btn.count() > 0:
                await safe_click(role_btn, f"sede_role_button={sede_label}")
            else:
                await safe_click(row, f"sede_row={sede_label}")

    # ✅ trigger reale: appare step Orario
    await wait_step_orario(page)


async def select_orario(page, orario: str) -> None:
    await wait_step_orario(page)

    combo = page.get_by_role("combobox").first
    if await combo.count() > 0 and await combo.is_visible():
        await safe_click(combo, "orario_combobox")
    else:
        await safe_click(page.get_by_text("Orario", exact=False).first, "orario_label")

    opt = page.get_by_text(orario, exact=True).first
    if await opt.count() == 0:
        raise RuntimeError(f"Orario '{orario}' non trovato tra le opzioni disponibili.")
    await safe_click(opt, f"orario={orario}")

    # ✅ trigger reale: bottone CONFERMA compare (o resta visibile)
    await page.wait_for_selector("text=/CONFERMA/i", timeout=25000)


async def proceed_conferma(page) -> None:
    btn = page.get_by_role("button", name=re.compile(r"CONFERMA", re.I)).first
    if await btn.count() == 0:
        btn = page.get_by_text("CONFERMA", exact=False).first
    await safe_click(btn, "conferma")

    # ✅ trigger reale: pagina finale
    await wait_step_ci_siamo_quasi(page)


async def fill_final_form(page, nome: str, telefono: str, email: str) -> None:
    await wait_step_ci_siamo_quasi(page)

    first, last = split_name(nome)

    await page.get_by_label("Nome*", exact=False).fill(first or ".")
    await page.get_by_label("Cognome*", exact=False).fill(last or ".")
    await page.get_by_label("Email*", exact=False).fill(email)
    await page.get_by_label("Telefono*", exact=False).fill(telefono)


async def click_prenota(page) -> None:
    btn = page.get_by_role("button", name=re.compile(r"PRENOTA", re.I)).first
    if await btn.count() == 0:
        btn = page.get_by_text("PRENOTA", exact=False).first
    await safe_click(btn, "prenota")


# =============================
# ENDPOINTS
# =============================
@app.post("/check_availability")
async def check_availability(dati: RichiestaControllo):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        page = await context.new_page()
        page.set_default_timeout(60000)

        try:
            await page.goto("https://rione.fidy.app/prenew.php?referer=sito", wait_until="domcontentloaded")
            await accept_cookies_if_any(page)

            await select_persone(page, dati.persone)
            await select_seggiolone(page, False)
            await select_data(page, dati.data)

            return {"result": "OK", "detail": "Arrivato fino a step pasto."}

        except Exception as e:
            path = await screenshot(page, "check_error")
            return {"result": "Error", "detail": str(e), "debug_screenshot": path}

        finally:
            await browser.close()


@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione):
    sede_label = normalize_sede(dati.sede)
    seggiolone = needs_seggiolone(dati.note)

    try:
        pasto_target = get_pasto_rigido(dati.orario)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    DRY_RUN = os.getenv("DRY_RUN", "1") == "1"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        page = await context.new_page()
        page.set_default_timeout(60000)

        try:
            print(
                f"🚀 BOOKING V28: {dati.nome} -> {sede_label} | {dati.data} {dati.orario} | "
                f"{dati.persone} pax | pasto={pasto_target} | seggiolone={seggiolone} | dry_run={DRY_RUN}"
            )

            await page.goto("https://rione.fidy.app/prenew.php?referer=sito", wait_until="domcontentloaded")
            await accept_cookies_if_any(page)

            print("-> 1. Persone")
            await select_persone(page, dati.persone)

            print("-> 2. Seggiolone")
            await select_seggiolone(page, seggiolone)

            print("-> 3. Data")
            await select_data(page, dati.data)

            print(f"-> 4. Pasto ({pasto_target})")
            await select_pasto(page, pasto_target)

            print(f"-> 5. Sede/Turno ({sede_label})")
            await select_sede_and_turno(page, sede_label, dati.orario)

            print(f"-> 6. Orario ({dati.orario})")
            await select_orario(page, dati.orario)

            if dati.note:
                print("-> 7. Note")
                ta = page.locator("textarea").first
                if await ta.count() > 0:
                    await ta.fill(dati.note)

            print("-> 8. Conferma")
            await proceed_conferma(page)

            print("-> 9. Form finale")
            await fill_final_form(page, dati.nome, dati.telefono, dati.email)

            if DRY_RUN:
                return {
                    "result": "DryRunSuccess",
                    "detail": "Arrivato fino alla schermata finale. DRY_RUN=1, quindi NON ho cliccato PRENOTA.",
                    "sede": sede_label,
                    "pasto": pasto_target,
                    "data": dati.data,
                    "orario": dati.orario,
                    "persone": dati.persone,
                    "seggiolone": seggiolone,
                }

            print("-> 10. PRENOTA")
            await click_prenota(page)

            return {
                "result": "Success",
                "sede": sede_label,
                "pasto": pasto_target,
                "data": dati.data,
                "orario": dati.orario,
                "persone": dati.persone,
                "seggiolone": seggiolone,
            }

        except Exception as e:
            path = await screenshot(page, "book_error_v28")
            return {"result": "Error", "detail": str(e), "debug_screenshot": path}

        finally:
            await browser.close()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
