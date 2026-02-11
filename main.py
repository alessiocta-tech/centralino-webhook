import os
import re
from datetime import datetime, timedelta
from typing import Optional, List, Any, Dict, Union

from fastapi import FastAPI
from pydantic import BaseModel, Field, EmailStr
from playwright.async_api import async_playwright

# ============================================================
# CONFIG
# ============================================================

BOOKING_URL = os.getenv("BOOKING_URL", "https://rione.fidy.app/prenew.php?referer=AI")
PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "60000"))
PW_NAV_TIMEOUT_MS = int(os.getenv("PW_NAV_TIMEOUT_MS", "60000"))

# Se true: compila form ma NON clicca PRENOTA (test)
DISABLE_FINAL_SUBMIT = os.getenv("DISABLE_FINAL_SUBMIT", "false").lower() == "true"

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(title="centralino-webhook", version="V13-merge-stable")

# ============================================================
# MODELS (compat)
# ============================================================

class DisabledAnyBody(BaseModel):
    """
    Modello volutamente permissivo: non deve mai generare 422.
    """
    class Config:
        extra = "allow"


class RichiestaPrenotazioneCompat(BaseModel):
    """
    Accetta:
    - schema "vecchio": nome, email, telefono, sede, data, orario, persone, note
    - schema tool-call: nome, cognome, email, telefono, sede, data, ora, persone, seggiolone, seggiolini, nota, referer, dry_run
    """

    # dati anagrafici
    nome: Optional[str] = None
    cognome: Optional[str] = None

    email: Optional[str] = None
    telefono: Optional[str] = None

    # booking
    sede: Optional[str] = None
    data: Optional[str] = None  # YYYY-MM-DD

    # orario: accetta sia "orario" sia "ora"
    orario: Optional[str] = None
    ora: Optional[str] = None

    persone: Optional[int] = None

    # note: accetta sia "note" sia "nota"
    note: Optional[str] = ""
    nota: Optional[str] = ""

    # extra tool-call (non obbligatori)
    seggiolone: Optional[bool] = False
    seggiolini: Optional[int] = 0
    referer: Optional[str] = "AI"
    dry_run: Optional[bool] = False

    class Config:
        extra = "allow"


# ============================================================
# HELPERS
# ============================================================

def calcola_pasto(orario_str: str) -> str:
    """13:00 -> PRANZO, 20:30 -> CENA"""
    try:
        hh = int(orario_str.split(":")[0])
        return "PRANZO" if hh < 17 else "CENA"
    except Exception:
        return "CENA"


def get_data_type(data_str: str) -> str:
    """Ritorna: Oggi / Domani / Altra"""
    try:
        data_pren = datetime.strptime(data_str, "%Y-%m-%d").date()
        oggi = datetime.now().date()
        domani = oggi + timedelta(days=1)
        if data_pren == oggi:
            return "Oggi"
        if data_pren == domani:
            return "Domani"
        return "Altra"
    except Exception:
        return "Altra"


def normalize_orario(s: Optional[str]) -> str:
    s = (s or "").strip().lower().replace("ore", "").replace("alle", "").strip()
    s = s.replace(".", ":").replace(",", ":")
    if re.fullmatch(r"\d{1,2}$", s):
        return f"{int(s):02d}:00"
    if re.fullmatch(r"\d{1,2}:\d{2}$", s):
        hh, mm = s.split(":")
        return f"{int(hh):02d}:{int(mm):02d}"
    # se arriva "13:00:00" -> taglio
    if re.fullmatch(r"\d{1,2}:\d{2}:\d{2}$", s):
        hh, mm, _ = s.split(":")
        return f"{int(hh):02d}:{int(mm):02d}"
    return s


def normalize_sede(s: Optional[str]) -> str:
    s0 = (s or "").strip().lower()
    mapping = {
        "talenti": "Talenti - Roma",
        "talenti - roma": "Talenti - Roma",
        "ostia": "Ostia Lido",
        "ostia lido": "Ostia Lido",
        "appia": "Appia",
        "reggio": "Reggio Calabria",
        "reggio calabria": "Reggio Calabria",
        "palermo": "Palermo",
        "palermo centro": "Palermo",
    }
    if s0 in mapping:
        return mapping[s0]
    return (s or "").strip()


def merge_nome_cognome(nome: str, cognome: Optional[str]) -> str:
    nome = (nome or "").strip()
    cognome = (cognome or "").strip()
    if nome and cognome:
        return f"{nome} {cognome}"
    return nome or cognome or ""


def is_valid_date_yyyy_mm_dd(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except Exception:
        return False


def validate_phone_basic(s: str) -> bool:
    x = re.sub(r"\s+", "", (s or ""))
    # accetta +39..., oppure solo cifre >= 8
    if x.startswith("+"):
        x2 = x[1:]
    else:
        x2 = x
    return bool(re.fullmatch(r"\d{8,15}", x2))


def validate_email_basic(s: str) -> bool:
    if not s:
        return False
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", s.strip()))


async def maybe_click_cookie(page):
    for patt in [r"accetta", r"consent", r"ok", r"accetto"]:
        try:
            loc = page.locator(f"text=/{patt}/i").first
            if await loc.count() > 0:
                await loc.click(timeout=2000, force=True)
                return
        except Exception:
            pass


async def click_exact_number(page, n: int):
    txt = str(n)
    for sel in [f"button:text-is('{txt}')", f"div:text-is('{txt}')", f"span:text-is('{txt}')"]:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0:
                await loc.click(timeout=3000, force=True)
                return True
        except Exception:
            pass
    try:
        await page.get_by_text(txt, exact=True).first.click(timeout=3000, force=True)
        return True
    except Exception:
        return False


async def click_seggiolini_no(page):
    try:
        if await page.locator("text=/seggiolini/i").count() > 0:
            no_btn = page.locator("text=/^\\s*NO\\s*$/i").first
            if await no_btn.count() > 0:
                await no_btn.click(timeout=4000, force=True)
                return True
    except Exception:
        pass
    return False


async def set_date(page, data_iso: str):
    tipo = get_data_type(data_iso)
    await page.wait_for_timeout(800)

    if tipo in ["Oggi", "Domani"]:
        try:
            btn = page.locator(f"text=/{tipo}/i").first
            if await btn.count() > 0:
                await btn.click(timeout=4000, force=True)
                return
        except Exception:
            pass

    try:
        altra = page.locator("text=/Altra data/i").first
        if await altra.count() > 0:
            await altra.click(timeout=5000, force=True)
    except Exception:
        pass

    await page.wait_for_timeout(600)
    try:
        await page.evaluate(
            f"""() => {{
                const el = document.querySelector('input[type="date"]');
                if (el) el.value = "{data_iso}";
            }}"""
        )
    except Exception:
        pass

    try:
        date_input = page.locator('input[type="date"]').first
        if await date_input.count() > 0:
            await date_input.press("Enter")
    except Exception:
        pass

    for patt in [r"conferma", r"cerca", r"ok"]:
        try:
            btn = page.locator(f"text=/{patt}/i").first
            if await btn.count() > 0:
                await btn.click(timeout=2000, force=True)
                break
        except Exception:
            pass


async def click_pasto(page, pasto: str):
    await page.wait_for_timeout(1200)
    try:
        btn = page.locator(f"text=/{pasto}/i").first
        if await btn.count() > 0:
            await btn.click(timeout=5000, force=True)
            return True
    except Exception:
        pass
    return False


async def click_sede(page, sede_target: str) -> bool:
    await page.wait_for_timeout(1200)
    try:
        loc = page.get_by_text(sede_target, exact=False).first
        if await loc.count() > 0:
            await loc.click(timeout=6000, force=True)
            return True
    except Exception:
        pass

    base = sede_target.split("-")[0].strip()
    if base:
        try:
            loc = page.get_by_text(base, exact=False).first
            if await loc.count() > 0:
                await loc.click(timeout=6000, force=True)
                return True
        except Exception:
            pass

    return False


async def extract_sedi_visibili(page) -> List[str]:
    """
    Dopo il click PRANZO/CENA, la pagina mostra la lista ristoranti.
    Estraggo i nomi visibili (best-effort).
    """
    js = """
    () => {
      const out = new Set();
      const nodes = document.querySelectorAll('.ristoBtn, .selettore, [class*="ristoBtn"]');
      for (const n of nodes) {
        const t = (n.innerText || '').trim();
        if (!t) continue;
        // prima riga sensata
        const firstLine = t.split('\\n').map(x=>x.trim()).find(x=>x.length>0);
        if (firstLine) out.add(firstLine);
      }
      // fallback: qualsiasi "small" dentro lista
      const smalls = document.querySelectorAll('.ristoCont small, .ristoCont .lead, .ristoCont');
      for (const n of smalls) {
        const t = (n.innerText || '').trim();
        if (!t) continue;
        const firstLine = t.split('\\n').map(x=>x.trim()).find(x=>x.length>0);
        if (firstLine && firstLine.length < 60) out.add(firstLine);
      }
      return Array.from(out);
    }
    """
    try:
        res = await page.evaluate(js)
        if isinstance(res, list):
            # pulizia
            cleaned = []
            for x in res:
                x = re.sub(r"\s+", " ", str(x)).strip()
                if x and x.lower() not in ["orario", "conferma", "pranzo", "cena"]:
                    cleaned.append(x)
            # de-dup preservando ordine
            seen = set()
            out = []
            for x in cleaned:
                if x not in seen:
                    seen.add(x)
                    out.append(x)
            return out[:12]
    except Exception:
        pass
    return []


async def extract_orari_disponibili_da_select(page) -> List[str]:
    """
    Dopo aver scelto sede, normalmente esiste una <select> con <option>.
    Ritorna lista HH:MM disponibili (enabled).
    """
    js = """
    () => {
      const sel = document.querySelector('select');
      if (!sel) return [];
      const out = [];
      for (const opt of sel.querySelectorAll('option')) {
        const val = (opt.value || '').trim();
        const txt = (opt.textContent || '').trim();
        const disabled = opt.disabled || opt.getAttribute('disabled') !== null;
        if (!val) continue;
        // prendo HH:MM dai primi 5 caratteri (es: 13:00:00) oppure da txt
        let hhmm = '';
        if (/^\\d{1,2}:\\d{2}/.test(val)) {
          const m = val.match(/^(\\d{1,2}:\\d{2})/);
          if (m) hhmm = m[1];
        } else {
          const m = txt.match(/(\\d{1,2}:\\d{2})/);
          if (m) hhmm = m[1];
        }
        if (!hhmm) continue;
        if (!disabled) out.push(hhmm);
      }
      return out;
    }
    """
    try:
        res = await page.evaluate(js)
        if isinstance(res, list):
            # normalizzo HH:MM
            out = []
            for x in res:
                out.append(normalize_orario(str(x)))
            # de-dup
            seen = set()
            uniq = []
            for x in out:
                if x not in seen:
                    seen.add(x)
                    uniq.append(x)
            return uniq[:12]
    except Exception:
        pass
    return []


async def click_orario(page, orario_hhmm: str) -> Dict[str, Any]:
    """
    Tenta di selezionare orario.
    Ritorna dict:
      { ok: bool, reason: 'OK'|'NOT_FOUND'|'SOLD_OUT', alternatives: [...] }
    """
    await page.wait_for_timeout(1200)

    # se c'è una select, provo a selezionare l'option corrispondente e verificare disabled
    try:
        sel = page.locator("select").first
        if await sel.count() > 0:
            # cerco option matching per value che inizi con hh:mm
            # nota: sul sito value spesso è "13:00:00"
            opt = page.locator(f"select option[value^='{orario_hhmm}']").first
            if await opt.count() > 0:
                # se disabled -> SOLD_OUT
                try:
                    disabled = await opt.is_disabled()
                except Exception:
                    disabled = False
                if disabled:
                    alts = await extract_orari_disponibili_da_select(page)
                    return {"ok": False, "reason": "SOLD_OUT", "alternatives": alts}

                # seleziono la option
                try:
                    await sel.select_option(value=re.compile(rf"^{re.escape(orario_hhmm)}"))
                    return {"ok": True, "reason": "OK", "alternatives": []}
                except Exception:
                    # fallback click
                    try:
                        await opt.click(timeout=5000, force=True)
                        return {"ok": True, "reason": "OK", "alternatives": []}
                    except Exception:
                        pass

            # non trovata option -> NOT_FOUND + alternatives
            alts = await extract_orari_disponibili_da_select(page)
            return {"ok": False, "reason": "NOT_FOUND", "alternatives": alts}
    except Exception:
        pass

    # fallback: click testo libero
    candidates = [
        f"text=/{re.escape(orario_hhmm)}/",
        f"text={orario_hhmm}",
        f"text=/{re.escape(orario_hhmm.replace(':', '.'))}/",
    ]
    for c in candidates:
        try:
            loc = page.locator(c).first
            if await loc.count() > 0:
                await loc.click(timeout=6000, force=True)
                return {"ok": True, "reason": "OK", "alternatives": []}
        except Exception:
            pass

    alts = await extract_orari_disponibili_da_select(page)
    return {"ok": False, "reason": "NOT_FOUND", "alternatives": alts}


async def click_conferma(page):
    for patt in [r"CONFERMA", r"Conferma", r"continua", r"avanti"]:
        try:
            btn = page.locator(f"text=/{patt}/").first
            if await btn.count() > 0:
                await btn.click(timeout=5000, force=True)
                return True
        except Exception:
            pass
    return False


async def fill_final_form(page, full_name: str, email: str, telefono: str, note: str):
    await page.wait_for_timeout(1200)

    parts = (full_name or "").strip().split(" ", 1)
    n = parts[0] if parts else full_name
    c = parts[1] if len(parts) > 1 else "Cliente"

    async def try_fill_any(selectors, value):
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    await loc.fill(value, timeout=4000)
                    return True
            except Exception:
                pass
        return False

    await try_fill_any(['input[name="Nome"]', 'input[name="nome"]', 'input[placeholder*="Nome" i]', 'input#Nome', 'input#nome'], n)
    await try_fill_any(['input[name="Cognome"]', 'input[name="cognome"]', 'input[placeholder*="Cognome" i]', 'input#Cognome', 'input#cognome'], c)
    await try_fill_any(['input[name="Email"]', 'input[name="email"]', 'input[type="email"]', 'input[placeholder*="Email" i]', 'input#Email'], email)
    await try_fill_any(['input[name="Telefono"]', 'input[name="telefono"]', 'input[type="tel"]', 'input[placeholder*="Telefono" i]', 'input#Telefono'], telefono)

    if note:
        await try_fill_any(['textarea[name="Nota"]', 'textarea[name="note"]', 'textarea', 'textarea[placeholder*="note" i]', 'textarea#Nota'], note)

    # checkbox consensi
    try:
        checkboxes = page.locator('input[type="checkbox"]')
        cnt = await checkboxes.count()
        for i in range(cnt):
            cb = checkboxes.nth(i)
            try:
                if await cb.is_visible():
                    await cb.check(timeout=2000)
            except Exception:
                pass
    except Exception:
        pass


async def click_prenota(page):
    for patt in [r"PRENOTA", r"Prenota", r"CONFERMA PRENOTAZIONE"]:
        try:
            btn = page.locator(f"text=/{patt}/").last
            if await btn.count() > 0:
                await btn.click(timeout=8000, force=True)
                return True
        except Exception:
            pass
    return False


def normalize_payload(dati: RichiestaPrenotazioneCompat) -> Dict[str, Any]:
    # orario: preferisci orario, poi ora
    orario_raw = dati.orario or dati.ora or ""
    orario = normalize_orario(orario_raw)

    # note: preferisci note, poi nota
    note = (dati.note or "").strip()
    if not note:
        note = (dati.nota or "").strip()

    sede = normalize_sede(dati.sede)
    full_name = merge_nome_cognome(dati.nome or "", dati.cognome)

    return {
        "full_name": full_name,
        "email": (dati.email or "").strip(),
        "telefono": (dati.telefono or "").strip(),
        "sede": sede,
        "data": (dati.data or "").strip(),
        "orario": orario,
        "persone": int(dati.persone or 0),
        "note": note,
        "dry_run": bool(dati.dry_run),
        "seggiolini": int(dati.seggiolini or 0),
        "seggiolone": bool(dati.seggiolone),
        "referer": (dati.referer or "AI").strip() or "AI",
    }


# ============================================================
# ROUTES
# ============================================================

@app.get("/")
def home():
    return {"status": "Centralino AI - Booking Engine V13 (merge-stable)"}


@app.post("/check_availability")
async def check_availability(_: DisabledAnyBody):
    """
    Compatibilità: NON usare più questo endpoint.
    Serve solo a non far crashare eventuali chiamate residue.
    Deve rispondere SEMPRE 200 (mai 422).
    """
    return {
        "status": "DISABLED",
        "message": "check_availability disabilitato: procedere direttamente con book_table."
    }


@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazioneCompat):
    payload = normalize_payload(dati)

    # Validazioni minime per evitare 422, ma rispondiamo 200 con errore gestito
    missing = []
    if not payload["full_name"]:
        missing.append("nome")
    if not validate_email_basic(payload["email"]):
        missing.append("email")
    if not validate_phone_basic(payload["telefono"]):
        missing.append("telefono")
    if not payload["sede"]:
        missing.append("sede")
    if not is_valid_date_yyyy_mm_dd(payload["data"]):
        missing.append("data")
    if not re.fullmatch(r"\d{2}:\d{2}", payload["orario"]):
        missing.append("ora")
    if payload["persone"] < 1:
        missing.append("persone")

    if missing:
        return {
            "status": "INVALID",
            "message": f"Dati mancanti/non validi: {', '.join(missing)}",
            "alternatives": []
        }

    # regole operative
    if payload["persone"] > 9:
        return {
            "status": "TRANSFER",
            "message": "Per tavolate superiori a 9 persone è necessario contattare il centralino.",
            "alternatives": []
        }

    orario = payload["orario"]
    pasto = calcola_pasto(orario)

    print(
        f"🚀 BOOKING: {payload['full_name']} -> {payload['sede']} | {payload['data']} {orario} "
        f"| pax={payload['persone']} | pasto={pasto} | dry_run={payload['dry_run'] or DISABLE_FINAL_SUBMIT}"
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--single-process", "--disable-gpu"],
        )
        context = await browser.new_context(user_agent=IPHONE_UA, viewport={"width": 390, "height": 844})
        page = await context.new_page()
        page.set_default_timeout(PW_TIMEOUT_MS)
        page.set_default_navigation_timeout(PW_NAV_TIMEOUT_MS)

        async def route_handler(route):
            if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
                await route.abort()
            else:
                await route.continue_()
        await page.route("**/*", route_handler)

        try:
            await page.goto(BOOKING_URL, wait_until="domcontentloaded")
            await maybe_click_cookie(page)

            # 1) Persone
            await page.wait_for_timeout(800)
            ok_people = await click_exact_number(page, payload["persone"])
            if not ok_people:
                print("⚠️ bottone persone non trovato, continuo...")

            # 2) Seggiolini: per ora NO (come V12)
            await page.wait_for_timeout(700)
            await click_seggiolini_no(page)

            # 3) Data
            await set_date(page, payload["data"])

            # 4) Pasto
            await click_pasto(page, pasto)

            # 5) Sede
            ok_sede = await click_sede(page, payload["sede"])
            if not ok_sede:
                sedi = await extract_sedi_visibili(page)
                return {
                    "status": "SEDE_NOT_FOUND",
                    "message": f"Sede non trovata: {payload['sede']}",
                    "alternatives": sedi
                }

            # 6) Orario
            res_orario = await click_orario(page, orario)
            if not res_orario.get("ok"):
                alts = res_orario.get("alternatives", []) or []
                if res_orario.get("reason") == "SOLD_OUT":
                    return {
                        "status": "SOLD_OUT",
                        "message": "Il turno selezionato è pieno. Ti proponiamo in alternativa il seguente turno.",
                        "alternatives": alts
                    }
                return {
                    "status": "NOT_AVAILABLE",
                    "message": "L’orario selezionato non è disponibile. Ti proponiamo in alternativa il seguente turno.",
                    "alternatives": alts
                }

            # 7) Conferma (vai a form finale)
            await page.wait_for_timeout(800)
            await click_conferma(page)

            # 8) Form finale
            await fill_final_form(page, payload["full_name"], payload["email"], payload["telefono"], payload["note"])

            # test mode: se dry_run true oppure env DISABLE_FINAL_SUBMIT
            if payload["dry_run"] or DISABLE_FINAL_SUBMIT:
                return {
                    "status": "OK_DRY_RUN",
                    "message": "Form compilato correttamente (dry-run, submit non inviato).",
                    "alternatives": []
                }

            ok_submit = await click_prenota(page)
            if not ok_submit:
                return {
                    "status": "ERROR",
                    "message": "Bottone PRENOTA non trovato.",
                    "alternatives": []
                }

            await page.wait_for_timeout(1500)
            return {
                "status": "OK",
                "message": f"Prenotazione inviata: {payload['sede']} {payload['data']} {orario} per {payload['persone']} persone.",
                "alternatives": []
            }

        except Exception as e:
            # screenshot debug
            try:
                ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
                path = f"booking_error_{ts}.png"
                await page.screenshot(path=path, full_page=True)
                print(f"📸 Screenshot salvato: {path}")
            except Exception:
                pass

            return {
                "status": "ERROR",
                "message": f"Errore prenotazione: {str(e)}",
                "alternatives": []
            }
        finally:
            await browser.close()
