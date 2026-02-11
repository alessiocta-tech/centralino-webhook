import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Body, FastAPI
from playwright.async_api import async_playwright

# ============================================================
# CONFIG
# ============================================================

BOOKING_URL = os.getenv("BOOKING_URL", "https://rione.fidy.app/prenew.php?referer=AI")
PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "60000"))
PW_NAV_TIMEOUT_MS = int(os.getenv("PW_NAV_TIMEOUT_MS", "60000"))
DISABLE_FINAL_SUBMIT = os.getenv("DISABLE_FINAL_SUBMIT", "false").lower() == "true"

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

app = FastAPI()

# ============================================================
# NORMALIZATION / VALIDATION (compat con ElevenLabs payload)
# ============================================================

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?\d{7,15}$")  # permissivo (E.164-ish)

SEDE_MAP = {
    "talenti": "Talenti - Roma",
    "talenti - roma": "Talenti - Roma",
    "roma talenti": "Talenti - Roma",
    "ostia": "Ostia Lido",
    "ostia lido": "Ostia Lido",
    "appia": "Appia",
    "reggio": "Reggio Calabria",
    "reggio calabria": "Reggio Calabria",
    "palermo": "Palermo",
}


def normalize_sede(s: str) -> str:
    s0 = (s or "").strip().lower()
    return SEDE_MAP.get(s0, (s or "").strip())


def normalize_orario(s: str) -> str:
    s = (s or "").strip().lower().replace("ore", "").replace("alle", "").strip()
    s = s.replace(".", ":").replace(",", ":")
    # "13" -> "13:00"
    if re.fullmatch(r"\d{1,2}$", s):
        return f"{int(s):02d}:00"
    # "13:30" -> "13:30"
    if re.fullmatch(r"\d{1,2}:\d{2}$", s):
        hh, mm = s.split(":")
        return f"{int(hh):02d}:{int(mm):02d}"
    # "13:30:00" -> "13:30"
    if re.fullmatch(r"\d{1,2}:\d{2}:\d{2}$", s):
        hh, mm, _ = s.split(":")
        return f"{int(hh):02d}:{int(mm):02d}"
    return s


def normalize_date(s: str) -> str:
    s = (s or "").strip()
    # accetta YYYY-MM-DD
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except Exception:
        return s


def calcola_pasto(orario_hhmm: str) -> str:
    try:
        hh = int(orario_hhmm.split(":")[0])
        return "PRANZO" if hh < 17 else "CENA"
    except Exception:
        return "CENA"


def get_data_type(data_str: str) -> str:
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


def today_iso() -> str:
    d = datetime.now().date()
    return d.strftime("%Y-%m-%d")


def plus_days_iso(days: int) -> str:
    d = (datetime.now().date() + timedelta(days=days))
    return d.strftime("%Y-%m-%d")


def extract_payload(data: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """
    Compat: accetta sia schema "vecchio" (nome/email/telefono/orario/note)
    sia schema "Eleven" (nome/cognome/email/telefono/data/ora/persone/sede/nota/seggiolone/seggiolini/referer/dry_run)
    Ritorna: payload normalizzato + lista campi mancanti/invalidi (per risposta 200, evitando 422).
    """
    missing: List[str] = []

    nome = (data.get("nome") or "").strip()
    cognome = (data.get("cognome") or "").strip()
    # se arriva tutto in "nome" tipo "Alessio Muzzarelli"
    if nome and not cognome and " " in nome:
        parts = nome.split(" ", 1)
        nome, cognome = parts[0].strip(), parts[1].strip()

    email = (data.get("email") or "").strip()
    telefono = (data.get("telefono") or "").strip()

    sede = (data.get("sede") or "").strip()
    data_iso = (data.get("data") or "").strip()

    # orario compat: "ora" o "orario"
    ora = (data.get("ora") or data.get("orario") or "").strip()

    # persone compat: "persone" o "coperti"
    persone = data.get("persone")
    if persone is None:
        persone = data.get("coperti")
    try:
        persone_int = int(persone)
    except Exception:
        persone_int = 0

    # note compat: "nota" o "note"
    note = (data.get("nota") if data.get("nota") is not None else data.get("note")) or ""
    note = str(note).strip()

    seggiolone = bool(data.get("seggiolone", False))
    try:
        seggiolini = int(data.get("seggiolini", 0) or 0)
    except Exception:
        seggiolini = 0

    referer = (data.get("referer") or "AI").strip()
    dry_run = bool(data.get("dry_run", False))

    # normalize
    sede_n = normalize_sede(sede)
    data_n = normalize_date(data_iso)
    ora_n = normalize_orario(ora)

    # validate required (logica tua)
    if not nome:
        missing.append("nome")
    if not cognome:
        missing.append("cognome")
    if not email or not EMAIL_RE.match(email):
        missing.append("email")
    if not telefono:
        missing.append("telefono")
    else:
        t = telefono.replace(" ", "").replace("-", "")
        telefono = t
        if not PHONE_RE.match(telefono):
            # non blocco duro, ma segnalo (meglio chiedere all’utente)
            missing.append("telefono_valido")

    if not sede_n:
        missing.append("sede")
    if not data_n or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", data_n):
        missing.append("data")
    else:
        # data passata
        try:
            if datetime.strptime(data_n, "%Y-%m-%d").date() < datetime.now().date():
                missing.append("data_passata")
        except Exception:
            pass

    if not ora_n or not re.fullmatch(r"\d{2}:\d{2}", ora_n):
        missing.append("ora")
    if persone_int < 1 or persone_int > 9:
        missing.append("persone_1_9")

    normalized = {
        "nome": nome,
        "cognome": cognome,
        "email": email,
        "telefono": telefono,
        "sede": sede_n,
        "data": data_n,
        "ora": ora_n,
        "persone": persone_int,
        "note": note,
        "seggiolone": seggiolone,
        "seggiolini": seggiolini,
        "referer": referer,
        "dry_run": dry_run,
    }
    return normalized, missing


# ============================================================
# PLAYWRIGHT HELPERS (stile V12 “FUNZIONA”)
# ============================================================

async def maybe_click_cookie(page):
    for patt in [r"accetta", r"consent", r"ok", r"accetto"]:
        try:
            loc = page.locator(f"text=/{patt}/i").first
            if await loc.count() > 0:
                await loc.click(timeout=2000, force=True)
                return
        except Exception:
            pass


async def click_exact_number(page, n: int) -> bool:
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


async def click_seggiolini_no(page) -> bool:
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


async def click_pasto(page, pasto: str) -> bool:
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


async def click_conferma(page) -> bool:
    for patt in [r"CONFERMA", r"Conferma", r"continua", r"avanti"]:
        try:
            btn = page.locator(f"text=/{patt}/").first
            if await btn.count() > 0:
                await btn.click(timeout=5000, force=True)
                return True
        except Exception:
            pass
    return False


async def fill_final_form(page, nome: str, cognome: str, email: str, telefono: str, note: str):
    await page.wait_for_timeout(1200)

    async def try_fill_any(selectors, value) -> bool:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    await loc.fill(value, timeout=4000)
                    return True
            except Exception:
                pass
        return False

    await try_fill_any(['input[name="Nome"]', 'input[name="nome"]', 'input[placeholder*="Nome" i]', 'input#Nome', 'input#nome'], nome)
    await try_fill_any(['input[name="Cognome"]', 'input[name="cognome"]', 'input[placeholder*="Cognome" i]', 'input#Cognome', 'input#cognome'], cognome)
    await try_fill_any(['input[name="Email"]', 'input[name="email"]', 'input[type="email"]', 'input[placeholder*="Email" i]', 'input#Email', 'input#email'], email)
    await try_fill_any(['input[name="Telefono"]', 'input[name="telefono"]', 'input[type="tel"]', 'input[placeholder*="Telefono" i]', 'input#Telefono', 'input#telefono'], telefono)

    if note:
        await try_fill_any(['textarea[name="Nota"]', 'textarea[name="note"]', 'textarea', 'textarea[placeholder*="note" i]', 'textarea#Nota'], note)

    # checkbox privacy/consensi: spunta tutto ciò che è visibile
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


async def click_prenota(page) -> bool:
    for patt in [r"PRENOTA", r"Prenota", r"CONFERMA PRENOTAZIONE"]:
        try:
            btn = page.locator(f"text=/{patt}/").last
            if await btn.count() > 0:
                await btn.click(timeout=8000, force=True)
                return True
        except Exception:
            pass
    return False


async def list_turni_disponibili(page) -> List[str]:
    """
    Legge le option disponibili dentro la select degli orari (se esiste).
    Ritorna lista di HH:MM (uniche, ordinate).
    """
    out: List[str] = []
    try:
        sel = page.locator("select#OraPren, select[name='OraPren'], select").first
        if await sel.count() == 0:
            return out

        options = sel.locator("option")
        cnt = await options.count()
        for i in range(cnt):
            opt = options.nth(i)
            try:
                val = (await opt.get_attribute("value")) or ""
                txt = (await opt.inner_text()) or ""
                disabled = await opt.get_attribute("disabled")
                if disabled is not None:
                    continue
                if not val or val.strip() == "":
                    continue

                # val spesso "13:00:00" o "13:00"
                v = normalize_orario(val.strip())
                if re.fullmatch(r"\d{2}:\d{2}", v):
                    # se testo indica esaurito, skip
                    if "ESAURIT" in txt.upper():
                        continue
                    out.append(v)
            except Exception:
                continue

        out = sorted(list(dict.fromkeys(out)))
    except Exception:
        pass
    return out


async def pick_best_alternatives(turni: List[str], target: str, k: int = 3) -> List[str]:
    """
    Sceglie alternative vicine all’orario target.
    """
    if not turni:
        return []
    try:
        th, tm = target.split(":")
        tgt = int(th) * 60 + int(tm)
    except Exception:
        return turni[:k]

    scored = []
    for t in turni:
        try:
            h, m = t.split(":")
            mins = int(h) * 60 + int(m)
            scored.append((abs(mins - tgt), t))
        except Exception:
            scored.append((999999, t))
    scored.sort(key=lambda x: x[0])
    return [t for _, t in scored[:k]]


# ============================================================
# ROUTES
# ============================================================

@app.get("/")
def home():
    return {"status": "Centralino AI - Booking Engine (Compat, no-422)"}


# Lasciato per evitare che Eleven o cache chiamino ancora /check_availability.
# Risponde SEMPRE 200 e non fa nulla.
@app.post("/check_availability")
@app.post("/checkavailability")
async def check_availability_disabled(_: Dict[str, Any] = Body(default={})):
    return {
        "status": "DISABLED",
        "message": "Check availability disabilitato: si procede direttamente con book_table.",
    }


@app.post("/book_table")
async def book_table(raw: Dict[str, Any] = Body(default={})):
    """
    Endpoint robusto:
    - Mai 422 (Body è dict con default).
    - Valida a mano e ritorna 200 con elenco campi mancanti.
    - Prova prenotazione diretta.
    - Se turno/orario non disponibile o pieno: propone alternative (turni pubblicati).
    """
    data, missing = extract_payload(raw)

    # Se mancano campi, NON 422: ritorna 200 con istruzioni
    if missing:
        return {
            "status": "MISSING_FIELDS",
            "missing": missing,
            "message": "Dati incompleti o non validi.",
            "expected_example": {
                "nome": "Alessio",
                "cognome": "Muzzarelli",
                "email": "alessiocta@gmail.com",
                "telefono": "+393477692795",
                "persone": 2,
                "sede": "Talenti",
                "data": plus_days_iso(1),
                "ora": "13:00",
                "seggiolone": False,
                "seggiolini": 0,
                "nota": "",
                "referer": "AI",
                "dry_run": False,
            },
        }

    sede_target = data["sede"]
    orario = data["ora"]
    pasto = calcola_pasto(orario)

    nome = data["nome"]
    cognome = data["cognome"]
    email = data["email"]
    telefono = data["telefono"]
    persone = data["persone"]
    note = data["note"]
    dry_run = bool(data["dry_run"]) or DISABLE_FINAL_SUBMIT

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--single-process",
                "--disable-gpu",
            ],
        )
        context = await browser.new_context(user_agent=IPHONE_UA, viewport={"width": 390, "height": 844})
        page = await context.new_page()
        page.set_default_timeout(PW_TIMEOUT_MS)
        page.set_default_navigation_timeout(PW_NAV_TIMEOUT_MS)

        # blocca risorse pesanti (come la V12 stabile)
        async def route_handler(route):
            if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", route_handler)

        try:
            await page.goto(BOOKING_URL, wait_until="domcontentloaded")
            await maybe_click_cookie(page)

            # 1) persone
            await page.wait_for_timeout(800)
            await click_exact_number(page, persone)

            # 2) seggiolini (NO / oppure ignora)
            await page.wait_for_timeout(700)
            await click_seggiolini_no(page)

            # 3) data
            await set_date(page, data["data"])

            # 4) pasto
            await click_pasto(page, pasto)

            # 5) sede
            ok_sede = await click_sede(page, sede_target)
            if not ok_sede:
                # prova a suggerire sedi leggendo i bottoni presenti (best effort)
                # (senza fallire in 422)
                return {
                    "status": "SEDE_NOT_FOUND",
                    "message": f"Sede non trovata: {sede_target}",
                    "suggestion": "Prova una sede tra: Talenti, Appia, Ostia, Palermo, Reggio Calabria.",
                }

            # 6) orario: qui NON usiamo click_orario “rigido”.
            # Apriamo la select e verifichiamo se l’orario è presente e non disabled.
            await page.wait_for_timeout(900)

            # prova a selezionare l’orario cercando option con value che inizia con HH:MM
            selected = False
            try:
                sel = page.locator("select#OraPren, select[name='OraPren'], select").first
                if await sel.count() > 0:
                    # costruisco value tipico: "13:00:00"
                    target_val_1 = f"{orario}:00"
                    # seleziona per value exact se possibile
                    try:
                        await sel.select_option(value=target_val_1, timeout=3000)
                        selected = True
                    except Exception:
                        # fallback: scan options
                        options = sel.locator("option")
                        cnt = await options.count()
                        for i in range(cnt):
                            opt = options.nth(i)
                            val = (await opt.get_attribute("value")) or ""
                            dis = await opt.get_attribute("disabled")
                            if dis is not None:
                                continue
                            if normalize_orario(val) == orario:
                                try:
                                    await sel.select_option(value=val, timeout=3000)
                                    selected = True
                                    break
                                except Exception:
                                    pass
            except Exception:
                selected = False

            if not selected:
                # Turno non esiste o pieno: proponi turni pubblicati
                turni = await list_turni_disponibili(page)
                best = await pick_best_alternatives(turni, orario, k=3)
                return {
                    "status": "TURN_NOT_AVAILABLE",
                    "message": "Il turno selezionato è pieno oppure non disponibile.",
                    "requested": {"data": data["data"], "ora": orario, "sede": sede_target, "persone": persone},
                    "alternatives": best,
                    "all_available_turns": turni[:20],  # limite
                }

            # 7) conferma (porta alla form)
            await page.wait_for_timeout(800)
            await click_conferma(page)

            # 8) form finale
            await fill_final_form(page, nome, cognome, email, telefono, note)

            if dry_run:
                return {
                    "status": "OK_TEST",
                    "message": "Form compilato (test mode, submit disattivato).",
                    "requested": {"data": data["data"], "ora": orario, "sede": sede_target, "persone": persone},
                }

            # submit finale
            ok_submit = await click_prenota(page)
            if not ok_submit:
                # se non trovo il bottone, propongo comunque alternative (spesso il flusso è cambiato)
                turni = await list_turni_disponibili(page)
                best = await pick_best_alternatives(turni, orario, k=3)
                return {
                    "status": "SUBMIT_NOT_FOUND",
                    "message": "Non riesco a completare l’invio in automatico. Turni alternativi disponibili:",
                    "alternatives": best,
                    "all_available_turns": turni[:20],
                }

            await page.wait_for_timeout(1500)

            return {
                "status": "OK",
                "message": "Prenotazione inviata.",
                "reservation": {
                    "nome": nome,
                    "cognome": cognome,
                    "persone": persone,
                    "sede": sede_target,
                    "data": data["data"],
                    "ora": orario,
                },
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
                "message": "Errore durante la prenotazione.",
                "detail": str(e),
            }
        finally:
            await browser.close()
