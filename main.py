import os
import re
from datetime import datetime, timedelta
from typing import Tuple

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, TimeoutError as PWTimeout


APP_VERSION = "V29-PROD"
BOOKING_URL = "https://rione.fidy.app/prenew.php?referer=butt"

# Timeout alti (come richiesto)
DEFAULT_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "120000"))  # 120s
NAV_TIMEOUT_MS = int(os.getenv("PW_NAV_TIMEOUT_MS", "120000"))

# Produzione: DRY_RUN=0 su Railway
DRY_RUN = os.getenv("DRY_RUN", "1").strip() not in ("0", "false", "False", "no", "NO")


app = FastAPI()


# ----------------------------
# MODELS
# ----------------------------
class RichiestaPrenotazione(BaseModel):
    data: str = Field(..., description="YYYY-MM-DD")
    persone: str = Field(..., description="Numero persone (1-9)")
    orario: str = Field(..., description="HH:MM")
    nome: str = Field(..., description="Nome e Cognome")
    telefono: str
    email: str
    sede: str = Field(..., description="Talenti, Appia, Ostia, Reggio, Palermo")
    note: str = ""


@app.get("/")
def home():
    return {"status": f"Centralino AI - {APP_VERSION}", "dry_run": DRY_RUN}


# ----------------------------
# HELPERS (pure)
# ----------------------------
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


def split_name(full: str) -> Tuple[str, str]:
    parts = (full or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], "."
    return parts[0], " ".join(parts[1:])


def needs_seggiolone(note: str) -> bool:
    n = (note or "").lower()
    return any(k in n for k in ["seggiolon", "bimbo", "bambin", "bambina", "baby"])


def parse_hhmm(orario: str) -> int:
    """minutes from 00:00"""
    m = re.match(r"^\s*(\d{1,2})[:.](\d{2})\s*$", orario or "")
    if not m:
        raise ValueError(f"Orario non valido: {orario}")
    h = int(m.group(1))
    mm = int(m.group(2))
    return h * 60 + mm


def get_pasto(orario: str) -> str:
    """
    Regola richiesta:
    PRANZO 12:00–14:30
    CENA   19:00–22:00
    """
    mins = parse_hhmm(orario)
    if 12 * 60 <= mins <= 14 * 60 + 30:
        return "PRANZO"
    if 19 * 60 <= mins <= 22 * 60:
        return "CENA"
    # fallback sensato
    return "PRANZO" if mins < 17 * 60 else "CENA"


def choose_turno_by_time(orario: str) -> str:
    """
    Se nella card sede esistono i pulsanti I/II TURNO, scegliamo:
    - I TURNO fino alle 20:30 incluso
    - II TURNO dopo le 20:30
    (Per pranzo spesso non ci sono turni, ma se ci fossero: I TURNO.)
    """
    mins = parse_hhmm(orario)
    if mins > (20 * 60 + 30):
        return "II TURNO"
    return "I TURNO"


# ----------------------------
# HELPERS (playwright)
# ----------------------------
async def safe_click(locator, label: str, timeout_ms: int = 15000, force: bool = True) -> None:
    # Evita "element not in viewport" e simili
    await locator.scroll_into_view_if_needed()
    try:
        await locator.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        pass
    try:
        await locator.click(timeout=timeout_ms, force=force)
    except Exception as e:
        raise RuntimeError(f"Click fallito: {label} -> {e}")


async def wait_step_container(page) -> None:
    # stepCont parte display:none e appare dopo 1.5s (fade)
    await page.locator(".stepCont").wait_for(state="visible", timeout=30000)


async def select_persone(page, persone: str) -> None:
    p = (persone or "").strip()
    loc = page.locator(f"span.nCoperti[rel='{p}']").first
    if await loc.count() == 0:
        raise RuntimeError(f"Numero persone non trovate: {p}")
    await safe_click(loc, f"persone={p}")


async def select_seggiolone(page, yes: bool) -> None:
    # Default è NO (span.SeggNO ha bg-dark text-white), ma clicchiamo comunque per certezza
    if not yes:
        loc = page.locator("span.SeggNO").first
        await safe_click(loc, "seggiolone=NO")
        # imposta seggiolini 0 se appare la lista (non dovrebbe)
        return

    # yes: clicca "SI" (seggioliniTxt) e poi scegli 1 seggiolino (o 1 default)
    await safe_click(page.locator("span.seggioliniTxt").first, "seggiolone=SI")
    # attendi che la barra quantità compaia (display:none -> fadeIn)
    cont = page.locator(".seggioliniCont").first
    await cont.wait_for(state="visible", timeout=15000)
    # scegli 1 seggiolino (puoi cambiarlo se vuoi in base alla nota)
    await safe_click(page.locator("span.nSeggiolini[rel='1']").first, "n_seggiolini=1")


async def select_data(page, data_str: str) -> None:
    # Bottoni "Oggi/Domani" sono span.dataBtn (classe dataOggi)
    target = datetime.strptime(data_str, "%Y-%m-%d").date()
    today = datetime.now().date()
    # In HTML ci sono due span dataOggi: oggi e domani, entrambi classe dataOggi dataBtn e rel=...
    if target == today or target == (today + timedelta(days=1)):
        loc = page.locator(f"span.dataBtn[rel='{data_str}']").first
        if await loc.count() > 0:
            await safe_click(loc, f"data={data_str}")
            return

    # altra data: usare input#DataPren (trigger change)
    # clic sul contenitore "Altra data" (span.altraData)
    await safe_click(page.locator("span.altraData").first, "altra_data")
    inp = page.locator("input#DataPren").first
    await inp.wait_for(state="attached", timeout=15000)
    # set value e dispatch change per far scattare handler jquery
    await page.evaluate(
        """(v) => {
            const el = document.querySelector('#DataPren');
            el.value = v;
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        data_str,
    )


async def select_pasto(page, pasto: str) -> None:
    # span.tipoBtn[rel="PRANZO"/"CENA"]
    loc = page.locator(f"span.tipoBtn[rel='{pasto}']").first
    if await loc.count() == 0:
        raise RuntimeError(f"Bottone pasto non trovato: {pasto}")
    await safe_click(loc, f"pasto={pasto}")


async def wait_risto_loaded(page) -> None:
    """
    Dopo click su PRANZO/CENA, lo script fa:
      $('.ristoCont').show(); $('.ristoCont').load('prenew_rist.php?...')
    Quindi aspettiamo che:
      - .ristoCont sia visibile
      - lo spinner interno sparisca (non garantito) o arrivi testo sedi
    """
    risto = page.locator(".ristoCont").first
    await risto.wait_for(state="visible", timeout=30000)

    # attende che dentro ci sia almeno una sede (Appia/Ostia/Palermo/Reggio/Talenti)
    # (fallback: aspetta che compaia QUALSIASI riga cliccabile)
    await page.wait_for_function(
        """() => {
          const r = document.querySelector('.ristoCont');
          if (!r) return false;
          const txt = (r.innerText || '').toLowerCase();
          return ['appia','ostia','palermo','reggio','talenti'].some(k => txt.includes(k));
        }""",
        timeout=30000,
    )


async def select_sede_and_optional_turno(page, sede_label: str, orario: str) -> None:
    """
    La lista sedi sta dentro .ristoCont.
    Ogni riga è un contenitore grande cliccabile (spesso div/a con border/rounded),
    e in alcuni casi (weekend) può contenere pulsanti I TURNO / II TURNO.
    """
    await wait_risto_loaded(page)

    risto = page.locator(".ristoCont").first
    needle = sede_label.strip().lower()

    # Trova un elemento che contenga il testo sede (case-insensitive) dentro ristoCont
    sede_text = risto.locator(
        "xpath=.//*[contains(translate(normalize-space(.), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
        f"'{needle}')]"
    ).first

    if await sede_text.count() == 0:
        # Debug utile: contenuto ristoCont
        snippet = (await risto.inner_text())[:400]
        raise RuntimeError(f"Sede '{sede_label}' non trovata in ristoCont. Snippet: {snippet}")

    # Risali al blocco “card” cliccabile: il primo ancestor che sia a/div con class border/rounded
    card = sede_text.locator(
        "xpath=ancestor::*[self::a or self::button or self::div[contains(@class,'border')]][1]"
    ).first

    # Cerca eventuali bottoni turni dentro la card
    # (possono essere span/a/button con testo TURNO)
    turno_desiderato = choose_turno_by_time(orario)
    turno_btn = card.locator(
        "xpath=.//*[self::a or self::button or self::span]"
        "[contains(translate(normalize-space(.),"
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'turno')]"
    )

    if await turno_btn.count() > 0:
        # prova a cliccare quello giusto (I TURNO / II TURNO)
        specific = card.locator(
            "xpath=.//*[self::a or self::button or self::span]"
            f"[contains(translate(normalize-space(.),"
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
            f"'{turno_desiderato.lower()}')]"
        ).first

        if await specific.count() > 0:
            await safe_click(specific, f"sede={sede_label} turno={turno_desiderato}")
        else:
            # fallback: primo turno trovato
            await safe_click(turno_btn.first, f"sede={sede_label} turno=first_found")
    else:
        # Scenario feriale: click sulla card intera
        await safe_click(card, f"sede={sede_label} (card)")

    # Dopo selezione sede, dovrebbe comparire lo step5 con select#OraPren
    await page.locator("#OraPren").wait_for(state="visible", timeout=30000)


async def select_orario(page, orario: str) -> None:
    """
    Orari sono option dentro select#OraPren.
    Il value potrebbe essere "HH:MM:SS" o simile, il testo contiene HH:MM e magari TURNO.
    """
    hhmm = re.sub(r"\.", ":", (orario or "").strip())
    if not re.match(r"^\d{1,2}:\d{2}$", hhmm):
        raise RuntimeError(f"Orario non valido: {orario}")

    # aspetta popolamento opzioni
    await page.wait_for_function(
        """() => {
          const s = document.querySelector('#OraPren');
          if (!s) return false;
          return s.querySelectorAll('option').length > 1;
        }""",
        timeout=30000,
    )

    # trova valore best-match (value startswith HH:MM oppure text contains HH:MM)
    value = await page.evaluate(
        """(hhmm) => {
          const s = document.querySelector('#OraPren');
          const opts = Array.from(s.querySelectorAll('option'));
          // skip placeholder (disabled/selected)
          for (const o of opts) {
            const v = (o.getAttribute('value') || '');
            const t = (o.textContent || '');
            if (v.startsWith(hhmm)) return v;
            if (t.includes(hhmm)) return v;
          }
          return null;
        }""",
        hhmm,
    )

    if not value:
        # debug: lista prime option
        debug_opts = await page.evaluate(
            """() => {
              const s = document.querySelector('#OraPren');
              const opts = Array.from(s.querySelectorAll('option')).slice(0, 10);
              return opts.map(o => ({value:o.value, text:o.textContent.trim()}));
            }"""
        )
        raise RuntimeError(f"Orario {hhmm} non trovato. Prime option: {debug_opts}")

    await page.select_option("#OraPren", value=value)


async def click_conferma(page) -> None:
    # a.confDati
    await safe_click(page.locator("a.confDati").first, "conferma_dati_step5")
    # stepDati dovrebbe apparire
    await page.locator(".stepDati").wait_for(state="visible", timeout=30000)


async def fill_final_form(page, nome: str, email: str, telefono: str) -> None:
    first, last = split_name(nome)

    await page.locator("#Nome").fill(first)
    await page.locator("#Cognome").fill(last)
    await page.locator("#Email").fill(email)
    await page.locator("#Telefono").fill(telefono)


async def click_prenota_and_verify_ok(page) -> None:
    """
    Submit invia POST a ajax.php e si aspetta risposta "OK".
    Se non arriva OK, consideriamo fallita la prenotazione.
    """
    btn = page.locator("input[type='submit'][value='PRENOTA']").first

    async with page.expect_response(re.compile(r"ajax\.php"), timeout=DEFAULT_TIMEOUT_MS) as resp_info:
        await safe_click(btn, "PRENOTA", timeout_ms=DEFAULT_TIMEOUT_MS)

    resp = await resp_info.value
    body = ""
    try:
        body = await resp.text()
    except Exception:
        body = ""

    if "OK" not in (body or ""):
        raise RuntimeError(f"Prenota fallito. Risposta ajax.php: {body[:200]}")

    # Dopo OK, la UI carica prenew_res.php dentro .stepCont (AJAX load)
    # Non sempre è facile riconoscerlo, ma almeno attendiamo un attimo e verifichiamo che non resti su form.
    await page.wait_for_timeout(800)
    # Se il form è ancora visibile e non ha cambiato nulla, non è necessariamente errore,
    # ma spesso indica che la load non è partita: lo segnaliamo in log con controllo soft.
    form_visible = await page.locator("#prenoForm").is_visible()
    if form_visible:
        # Non blocco: alcuni ambienti restano sul form anche se OK (dipende dal load).
        # Però almeno lo facciamo sapere.
        print("⚠️ Warning: ajax.php=OK ma #prenoForm ancora visibile (UI load prenew_res.php non rilevata).")


# ----------------------------
# ENDPOINT
# ----------------------------
@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione):
    sede_label = normalize_sede(dati.sede)
    seggiolone = needs_seggiolone(dati.note)
    pasto = get_pasto(dati.orario)

    print(
        f"🚀 BOOKING {APP_VERSION}: {dati.nome} -> {sede_label} | {dati.data} {dati.orario} | "
        f"{dati.persone} pax | pasto={pasto} | seggiolone={seggiolone} | dry_run={DRY_RUN}"
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(viewport={"width": 390, "height": 844})
        page = await context.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)
        page.set_default_navigation_timeout(NAV_TIMEOUT_MS)

        try:
            await page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

            # Aspetta che lo step container appaia (fade dopo 1.5s)
            await wait_step_container(page)

            print("-> 1) Persone")
            await select_persone(page, dati.persone)

            print("-> 2) Seggiolone")
            await select_seggiolone(page, seggiolone)

            print("-> 3) Data")
            await select_data(page, dati.data)

            print(f"-> 4) Pasto ({pasto})")
            await select_pasto(page, pasto)

            print(f"-> 5) Sede ({sede_label}) + eventuale turno")
            await select_sede_and_optional_turno(page, sede_label, dati.orario)

            print(f"-> 6) Orario ({dati.orario})")
            await select_orario(page, dati.orario)

            # Note nello step5
            if (dati.note or "").strip():
                try:
                    await page.locator("#Nota").fill(dati.note.strip())
                except Exception:
                    pass

            print("-> 7) Conferma (step5)")
            await click_conferma(page)

            print("-> 8) Dati finali")
            await fill_final_form(page, dati.nome, dati.email, dati.telefono)

            if DRY_RUN:
                return {
                    "ok": True,
                    "dry_run": True,
                    "message": f"[DRY RUN] Pronto a prenotare: {dati.nome} {dati.data} {dati.orario} {sede_label}",
                }

            print("-> 9) PRENOTA (PRODUZIONE)")
            await click_prenota_and_verify_ok(page)

            return {
                "ok": True,
                "dry_run": False,
                "message": f"Prenotazione inviata e confermata (ajax.php=OK) per {dati.nome} - {sede_label} - {dati.data} {dati.orario} - {dati.persone} pax.",
            }

        except PWTimeout as e:
            raise HTTPException(status_code=504, detail=f"Timeout Playwright: {e}")
        except Exception as e:
            # qui alziamo davvero l'errore (così ElevenLabs/integrazione capisce che è fallito)
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
