import os
import re
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Union, List, Dict, Any, Tuple

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel, Field, root_validator
from playwright.async_api import async_playwright


# ============================================================
# CONFIG
# ============================================================

BOOKING_URL = os.getenv("BOOKING_URL", "https://rione.fidy.app/prenew.php?referer=AI")

PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "60000"))
PW_NAV_TIMEOUT_MS = int(os.getenv("PW_NAV_TIMEOUT_MS", "60000"))
DISABLE_FINAL_SUBMIT = os.getenv("DISABLE_FINAL_SUBMIT", "false").lower() == "true"

DEBUG_ECHO_PAYLOAD = os.getenv("DEBUG_ECHO_PAYLOAD", "false").lower() == "true"
DEBUG_LOG_AJAX_POST = os.getenv("DEBUG_LOG_AJAX_POST", "false").lower() == "true"

# Dashboard / memoria
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")  # se vuoto -> dashboard non protetta (sconsigliato)
DATA_DIR = os.getenv("DATA_DIR", "/tmp")
DB_PATH = os.path.join(DATA_DIR, "centralino.sqlite3")

# Retry
MAX_SLOT_RETRIES = int(os.getenv("MAX_SLOT_RETRIES", "2"))  # tentativi su orari alternativi
MAX_SUBMIT_RETRIES = int(os.getenv("MAX_SUBMIT_RETRIES", "1"))  # retry dopo errore ajax.php tipo "pieno"
RETRY_TIME_WINDOW_MIN = int(os.getenv("RETRY_TIME_WINDOW_MIN", "90"))  # cerca orari entro +/- N minuti

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

app = FastAPI()


# ============================================================
# DB (dashboard + memoria)
# ============================================================

def _db() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def _db_init():
    conn = _db()
    cur = conn.cursor()
    cur.execute("""
      CREATE TABLE IF NOT EXISTS bookings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        phone TEXT,
        name TEXT,
        email TEXT,
        sede TEXT,
        data TEXT,
        orario TEXT,
        persone INTEGER,
        seggiolini INTEGER,
        note TEXT,
        ok INTEGER,
        message TEXT
      )
    """)
    cur.execute("""
      CREATE TABLE IF NOT EXISTS customers (
        phone TEXT PRIMARY KEY,
        name TEXT,
        email TEXT,
        last_sede TEXT,
        last_persone INTEGER,
        last_seggiolini INTEGER,
        last_note TEXT,
        updated_at TEXT
      )
    """)
    conn.commit()
    conn.close()

_db_init()


def _log_booking(payload: Dict[str, Any], ok: bool, message: str):
    conn = _db()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO bookings (ts, phone, name, email, sede, data, orario, persone, seggiolini, note, ok, message)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.utcnow().isoformat(),
        payload.get("telefono"),
        payload.get("nome"),
        payload.get("email"),
        payload.get("sede"),
        payload.get("data"),
        payload.get("orario"),
        payload.get("persone"),
        payload.get("seggiolini"),
        payload.get("note"),
        1 if ok else 0,
        message[:5000],
    ))
    conn.commit()
    conn.close()


def _upsert_customer(phone: str, name: str, email: str, sede: str, persone: int, seggiolini: int, note: str):
    conn = _db()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO customers (phone, name, email, last_sede, last_persone, last_seggiolini, last_note, updated_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(phone) DO UPDATE SET
        name=excluded.name,
        email=excluded.email,
        last_sede=excluded.last_sede,
        last_persone=excluded.last_persone,
        last_seggiolini=excluded.last_seggiolini,
        last_note=excluded.last_note,
        updated_at=excluded.updated_at
    """, (
        phone, name, email, sede, persone, seggiolini, note, datetime.utcnow().isoformat()
    ))
    conn.commit()
    conn.close()


def _get_customer(phone: str) -> Optional[Dict[str, Any]]:
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM customers WHERE phone = ?", (phone,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


# ============================================================
# NORMALIZZAZIONI
# ============================================================

def _norm_orario(s: str) -> str:
    s = (s or "").strip().lower().replace("ore", "").replace("alle", "").strip()
    s = s.replace(".", ":").replace(",", ":")
    if re.fullmatch(r"\d{1,2}$", s):
        return f"{int(s):02d}:00"
    if re.fullmatch(r"\d{1,2}:\d{2}$", s):
        hh, mm = s.split(":")
        return f"{int(hh):02d}:{int(mm):02d}"
    return s

def _calcola_pasto(orario_hhmm: str) -> str:
    try:
        hh = int(orario_hhmm.split(":")[0])
        return "PRANZO" if hh < 17 else "CENA"
    except Exception:
        return "CENA"

def _get_data_type(data_str: str) -> str:
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

def _is_sold_out_text(txt: str) -> bool:
    t = (txt or "").upper()
    return ("TUTTO ESAURITO" in t) or ("ESAURITO" in t)

def _normalize_sede(s: str) -> str:
    s0 = (s or "").strip().lower()
    mapping = {
        "talenti": "Talenti - Roma",
        "talenti - roma": "Talenti - Roma",
        "roma talenti": "Talenti - Roma",
        "ostia": "Ostia Lido",
        "ostia lido": "Ostia Lido",
        "appia": "Appia",
        "reggio": "Reggio Calabria",
        "reggio calabria": "Reggio Calabria",
        "palermo": "Palermo",
        "palermo centro": "Palermo",
    }
    return mapping.get(s0, (s or "").strip())


def _time_to_minutes(hhmm: str) -> Optional[int]:
    m = re.fullmatch(r"(\d{2}):(\d{2})", hhmm or "")
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2))
    return hh * 60 + mm

def _minutes_to_hhmm(minutes: int) -> str:
    hh = (minutes // 60) % 24
    mm = minutes % 60
    return f"{hh:02d}:{mm:02d}"


# ============================================================
# MODEL
# ============================================================

class RichiestaPrenotazione(BaseModel):
    # fase: "availability" (mostra sedi/turni) oppure "book" (prenota fino in fondo)
    fase: str = Field("book", description='Fase del flusso: "availability" oppure "book"')

    # In availability possono essere vuoti; in book sono obbligatori (validati a runtime)
    nome: Optional[str] = ""
    cognome: Optional[str] = ""
    email: Optional[str] = ""
    telefono: Optional[str] = ""

    # In availability può essere vuota; in book è obbligatoria (validata a runtime)
    sede: Optional[str] = ""

    data: str
    orario: str
    persone: Union[int, str] = Field(...)

    # seggiolini: 0..5 (se >0 -> click SI e seleziona numero)
    seggiolini: Union[int, str] = 0
    seggiolone: Optional[bool] = False  # compatibilità prompt

    # accetta sia "note" che "nota"
    note: Optional[str] = Field("", alias="nota")

    model_config = {
        "validate_by_name": True,  # pydantic v2
        "extra": "ignore",
    }

    @root_validator(pre=True)
    def _coerce_fields(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        # note/nota
        if values.get("note") not in (None, ""):
            values["nota"] = values.get("note")

        # fase normalize
        if values.get("fase") is None or str(values.get("fase")).strip() == "":
            values["fase"] = "book"
        values["fase"] = str(values["fase"]).strip().lower()

        # persone
        p = values.get("persone")
        if isinstance(p, str):
            p2 = re.sub(r"[^\d]", "", p)
            if p2:
                values["persone"] = int(p2)

        # seggiolini
        s = values.get("seggiolini")
        if isinstance(s, str):
            s2 = re.sub(r"[^\d]", "", s)
            values["seggiolini"] = int(s2) if s2 else 0

        # orario
        if values.get("orario") is not None:
            values["orario"] = _norm_orario(str(values["orario"]))

        # sede
        if values.get("sede") is not None:
            values["sede"] = _normalize_sede(str(values["sede"]))

        # telefono
        if values.get("telefono") is not None:
            values["telefono"] = re.sub(r"[^\d]", "", str(values["telefono"]))

        # email fallback (solo se proprio vuota)
        if not values.get("email"):
            values["email"] = "default@prenotazioni.com"

        # nome fallback
        if values.get("nome") is None:
            values["nome"] = ""

        return values

# ============================================================
# PLAYWRIGHT HELPERS
# ============================================================

async def _block_heavy(route):
    if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
        await route.abort()
    else:
        await route.continue_()

async def _maybe_click_cookie(page):
    for patt in [r"accetta", r"consent", r"ok", r"accetto"]:
        try:
            loc = page.locator(f"text=/{patt}/i").first
            if await loc.count() > 0:
                await loc.click(timeout=1500, force=True)
                return
        except Exception:
            pass

async def _wait_ready(page):
    await page.wait_for_selector(".nCoperti", state="visible", timeout=PW_TIMEOUT_MS)

async def _click_persone(page, n: int):
    loc = page.locator(f'.nCoperti[rel="{n}"]').first
    if await loc.count() == 0:
        loc = page.get_by_text(str(n), exact=True).first
    await loc.click(timeout=8000, force=True)

async def _set_seggiolini(page, seggiolini: int):
    seggiolini = int(seggiolini or 0)
    # Se 0 -> NO
    if seggiolini <= 0:
        try:
            no_btn = page.locator(".SeggNO").first
            if await no_btn.count() > 0 and await no_btn.is_visible():
                await no_btn.click(timeout=4000, force=True)
                return
            tno = page.locator("text=/^\\s*NO\\s*$/i").first
            if await tno.count() > 0 and await tno.is_visible():
                await tno.click(timeout=4000, force=True)
        except Exception:
            pass
        return

    # Se >0 -> SI + selezione numero
    try:
        si_btn = page.locator(".SeggSI").first
        if await si_btn.count() > 0:
            await si_btn.click(timeout=4000, force=True)
        else:
            tsi = page.locator("text=/^\\s*SI\\s*$/i").first
            if await tsi.count() > 0:
                await tsi.click(timeout=4000, force=True)
    except Exception:
        pass

    # attende comparsa selettori 0..5
    await page.wait_for_selector(".nSeggiolini", state="visible", timeout=PW_TIMEOUT_MS)
    # click numero
    loc = page.locator(f'.nSeggiolini[rel="{seggiolini}"]').first
    if await loc.count() == 0:
        loc = page.get_by_text(str(seggiolini), exact=True).first
    await loc.click(timeout=6000, force=True)

async def _set_date(page, data_iso: str):
    tipo = _get_data_type(data_iso)

    if tipo in ["Oggi", "Domani"]:
        btn = page.locator(f'.dataBtn[rel="{data_iso}"]').first
        if await btn.count() > 0:
            await btn.click(timeout=6000, force=True)
            return

    await page.evaluate(
        """(val) => {
          const el = document.querySelector('#DataPren') || document.querySelector('input[type="date"]');
          if (!el) return false;
          el.value = val;
          el.dispatchEvent(new Event('change', { bubbles: true }));
          return true;
        }""",
        data_iso,
    )

async def _click_pasto(page, pasto: str):
    loc = page.locator(f'.tipoBtn[rel="{pasto}"]').first
    if await loc.count() > 0:
        await loc.click(timeout=8000, force=True)
        return
    await page.locator(f"text=/{pasto}/i").first.click(timeout=8000, force=True)

async def _scrape_sedi_availability(page) -> List[Dict[str, Any]]:
    """Estrae lista sedi, prezzo e disponibilità turni dalla schermata sedi (STEP 4)."""
    known = ["Appia", "Talenti", "Talenti - Roma", "Reggio Calabria", "Ostia Lido", "Palermo"]
    # Attendi che compaiano i bottoni sede
    await page.wait_for_selector(".ristoBtn", state="visible", timeout=15000)

    js = r"""
    (known) => {
      function norm(s){ return (s||'').replace(/\s+/g,' ').trim(); }
      const all = Array.from(document.querySelectorAll('body *'));
      const hits = [];
      for (const name of known){
        const el = all.find(e => norm(e.textContent) === name);
        if (!el) continue;
        // risali a un contenitore che contenga anche il prezzo o i turni
        let node = el;
        let guard = 0;
        while (node && guard++ < 12){
          const t = norm(node.innerText);
          if (t.includes('€') || /TURNO/i.test(t)) break;
          node = node.parentElement;
        }
        if (!node) continue;
        const txt = norm(node.innerText);
        hits.push({name, txt});
      }
      // de-dup per name
      const out = [];
      const seen = new Set();
      for (const h of hits){
        if (seen.has(h.name)) continue;
        seen.add(h.name);
        out.push(h);
      }
      return out;
    }
    """
    raw = await page.evaluate(js, known)

    out: List[Dict[str, Any]] = []
    for r in raw:
        name = r.get("name") or ""
        txt = r.get("txt") or ""
        price = None
        m = re.search(r"(\d{1,3}[\.,]\d{2})\s*€", txt)
        if m:
            price = m.group(1).replace(",", ".")
        turni = []
        if re.search(r"\bI\s*TURNO\b", txt, flags=re.I):
            turni.append("I TURNO")
        if re.search(r"\bII\s*TURNO\b", txt, flags=re.I):
            turni.append("II TURNO")
        # Canonical name
        if name.strip().lower() in ("talenti - roma", "talenti"):
            name = "Talenti"
        out.append({"nome": name, "prezzo": price, "turni": turni})
    # Ordina come known
    order = {n:i for i,n in enumerate(["Appia","Reggio Calabria","Talenti","Palermo","Ostia Lido"])}
    out.sort(key=lambda x: order.get(x["nome"], 999))
    return out

async def _maybe_select_turn(page, pasto: str, orario_req: str):
    """Se presenti i pulsanti I/II TURNO, seleziona quello coerente con l'orario richiesto."""
    try:
        b1 = page.locator("text=/^\s*I\s*TURNO\s*$/i")
        b2 = page.locator("text=/^\s*II\s*TURNO\s*$/i")
        has1 = await b1.count() > 0
        has2 = await b2.count() > 0
        if not (has1 and has2):
            return

        hh, mm = [int(x) for x in orario_req.split(":")]
        mins = hh * 60 + mm

        # euristiche:
        # - cena: II turno da 21:30 in poi
        # - pranzo: II turno da 14:30 in poi (se esiste)
        if pasto == "cena":
            choose_second = mins >= (21 * 60 + 30)
        else:
            choose_second = mins >= (14 * 60 + 30)

        target = b2 if choose_second else b1
        await target.first.click(timeout=5000, force=True)
        await page.wait_for_timeout(300)
    except Exception:
        return

def _match_sede_text(sede_target: str) -> List[str]:
    base = sede_target.strip()
    parts = [p.strip() for p in re.split(r"[-–]", base) if p.strip()]
    cands = [base] + parts
    seen = set()
    out = []
    for c in cands:
        k = c.lower()
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out

async def _click_sede(page, sede_target: str):
    await page.wait_for_selector(".ristoBtn", state="visible", timeout=PW_TIMEOUT_MS)

    for cand in _match_sede_text(sede_target):
        loc = page.locator(".ristoBtn", has_text=cand).first
        if await loc.count() > 0:
            await loc.click(timeout=10000, force=True)
            return

    raise RuntimeError(f"Sede non trovata: '{sede_target}'")

async def _select_orario_value(page, hhmm: str) -> bool:
    """Seleziona #OraPren accettando option con value o testo che inizia con HH:MM."""
    await page.wait_for_selector("#OraPren", timeout=15000)
    try:
        await page.locator("#OraPren").scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        await page.click("#OraPren")
    except Exception:
        pass
    await page.wait_for_timeout(200)

    opts = await page.query_selector_all("#OraPren option")
    target_val = None
    target_label = None
    for opt in opts:
        try:
            val = (await opt.get_attribute("value")) or ""
            txt = (await opt.inner_text()) or ""
        except Exception:
            continue
        val = val.strip()
        txt = txt.strip()
        if not val and "SCEGLI" in txt.upper():
            continue
        if val.startswith(hhmm) or txt.startswith(hhmm):
            target_val = val
            target_label = txt
            break

    if target_val is None and target_label is None:
        return False

    try:
        if target_val:
            await page.select_option("#OraPren", value=target_val)
        else:
            await page.select_option("#OraPren", label=target_label)
    except Exception:
        return False
    await page.wait_for_timeout(150)
    return True

async def _get_orario_options(page) -> List[Tuple[str, str]]:
    """Return options from the time dropdown.
    Some deployments render <option value="">19:00</option>.
    We keep options even when value is empty, using text as a fallback.
    Returns [(value_or_text, text)].
    """
    # Ensure the select exists and is populated
    await page.wait_for_selector("#OraPren", state="visible", timeout=PW_TIMEOUT_MS)
    try:
        # Trigger possible lazy-population
        await page.click("#OraPren", timeout=3000)
    except Exception:
        pass

    try:
        await page.wait_for_selector("#OraPren option", timeout=PW_TIMEOUT_MS)
    except Exception:
        # If options are not present, return empty list (caller decides)
        return []

    opts = await page.evaluate(
        """() => {
          const sel = document.querySelector('#OraPren');
          if (!sel) return [];
          return Array.from(sel.options)
            .filter(o => !o.disabled)
            .map(o => ({value: (o.value||'').trim(), text: (o.textContent||'').trim()}));
        }"""
    )

    out: List[Tuple[str, str]] = []
    for o in opts:
        v = (o.get("value") or "").strip()
        t = (o.get("text") or "").strip()
        if not t:
            continue
        # Keep only time-like entries
        if re.match(r"^\d{1,2}:\d{2}$", t):
            out.append(((v or t).strip(), t))
    return out

def _pick_closest_time(target_hhmm: str, options: List[Tuple[str, str]]) -> Optional[str]:
    """
    options values look like "21:00:00" or similar.
    We compare by hh:mm in value.
    """
    target_m = _time_to_minutes(target_hhmm)
    if target_m is None:
        return options[0][0] if options else None

    best = None
    best_delta = None
    for v, _ in options:
        hhmm = v[:5]
        m = _time_to_minutes(hhmm)
        if m is None:
            continue
        delta = abs(m - target_m)
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best = v

    # finestra massima
    if best is not None and best_delta is not None and best_delta <= RETRY_TIME_WINDOW_MIN:
        return best
    return None

async def _select_orario_or_retry(page, wanted_hhmm: str) -> Tuple[str, bool]:
    """
    Try selecting the exact time. If missing, pick closest available (retry intelligent).
    Returns (selected_value, used_fallback)
    """
    await page.wait_for_selector("#OraPren", state="visible", timeout=PW_TIMEOUT_MS)
    await page.wait_for_function(
        """() => {
          const sel = document.querySelector('#OraPren');
          return sel && sel.options && sel.options.length > 1;
        }""",
        timeout=PW_TIMEOUT_MS,
    )

    wanted = wanted_hhmm.strip()
    wanted_val = wanted + ":00" if re.fullmatch(r"\d{2}:\d{2}", wanted) else wanted

    # 1) exact by value
    try:
        res = await page.locator("#OraPren").select_option(value=wanted_val)
        if res:
            return wanted_val, False
    except Exception:
        pass

    # 2) by text contains hh:mm
    ok = await page.evaluate(
        """(hhmm) => {
          const sel = document.querySelector('#OraPren');
          if (!sel) return false;
          const opt = Array.from(sel.options).find(o => (o.textContent || '').includes(hhmm));
          if (!opt) return false;
          sel.value = opt.value;
          sel.dispatchEvent(new Event('change', { bubbles: true }));
          return true;
        }""",
        wanted,
    )
    if ok:
        val = await page.locator("#OraPren").input_value()
        return val, False

    # 3) intelligent retry: closest available
    options = await _get_orario_options(page)
    best = _pick_closest_time(wanted, options)
    if best:
        await page.locator("#OraPren").select_option(value=best)
        return best, True

    raise RuntimeError(f"Orario non disponibile: {wanted}")

async def _fill_note_step5_strong(page, note: str):
    note = (note or "").strip()
    if not note:
        return

    await page.wait_for_selector("#Nota", state="visible", timeout=PW_TIMEOUT_MS)

    await page.locator("#Nota").click(timeout=5000)
    await page.locator("#Nota").fill(note, timeout=8000)

    await page.evaluate(
        """(val) => {
          const t = document.querySelector('#Nota');
          if (!t) return;
          t.value = val;
          t.dispatchEvent(new Event('input', { bubbles: true }));
          t.dispatchEvent(new Event('change', { bubbles: true }));
          t.blur();
        }""",
        note,
    )

    await page.evaluate(
        """(val) => {
          const h = document.querySelector('#Nota2');
          if (!h) return;
          h.value = val;
        }""",
        note,
    )

async def _click_conferma(page):
    loc = page.locator(".confDati").first
    if await loc.count() > 0:
        await loc.click(timeout=8000, force=True)
        return
    await page.locator("text=/CONFERMA/i").first.click(timeout=8000, force=True)

async def _fill_form(page, nome: str, cognome: str, email: str, telefono: str):
    parti = (nome or "").strip().split(" ", 1)
    nome1 = parti[0] if parti else (nome or "Cliente")
    cognome = parti[1] if len(parti) > 1 else "Cliente"

    await page.wait_for_selector("#prenoForm", state="visible", timeout=PW_TIMEOUT_MS)
    await page.locator("#Nome").fill(nome1, timeout=8000)
    await page.locator("#Cognome").fill(cognome, timeout=8000)
    await page.locator("#Email").fill(email, timeout=8000)
    await page.locator("#Telefono").fill(telefono, timeout=8000)

    # Consensi/Privacy: alcuni form richiedono checkbox (termini/privacy/consenso)
    try:
        boxes = page.locator("#prenoForm input[type=checkbox]")
        n = await boxes.count()
        for i in range(n):
            b = boxes.nth(i)
            # se già spuntato, continua
            try:
                checked = await b.is_checked()
            except Exception:
                checked = False
            if checked:
                continue
            # prova a capire se è rilevante (required / termini / privacy / consenso)
            name = (await b.get_attribute("name") or "").lower()
            _id = (await b.get_attribute("id") or "").lower()
            req = await b.get_attribute("required")
            is_relevant = bool(req) or any(k in (name + " " + _id) for k in ["privacy", "consenso", "termin", "gdpr", "policy"])
            if not is_relevant:
                continue
            # click sul checkbox o sulla label associata
            try:
                await b.scroll_into_view_if_needed()
                await b.click(timeout=2000, force=True)
            except Exception:
                try:
                    if _id:
                        lab = page.locator(f'label[for="{_id}"]').first
                        if await lab.count() > 0:
                            await lab.click(timeout=2000, force=True)
                except Exception:
                    pass
    except Exception:
        pass


async def _click_prenota(page):
    loc = page.locator('input[type="submit"][value="PRENOTA"]').first
    if await loc.count() > 0:
        await loc.click(timeout=15000, force=True)
        return
    await page.locator("text=/PRENOTA/i").last.click(timeout=15000, force=True)


def _looks_like_full_slot(msg: str) -> bool:
    s = (msg or "").lower()
    patterns = [
        "pieno", "sold out", "non disponibile", "esaur", "completo",
        "non ci sono", "nessuna disponibil", "turno completo"
    ]
    return any(p in s for p in patterns)


# ============================================================
# ROUTES
# ============================================================

@app.get("/")
def home():
    return {
        "status": "Centralino AI - Booking Engine (Railway)",
        "disable_final_submit": DISABLE_FINAL_SUBMIT,
        "db": DB_PATH,
    }


def _require_admin(request: Request):
    if not ADMIN_TOKEN:
        return
    token = request.headers.get("x-admin-token") or request.query_params.get("token")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/_admin/dashboard")
def admin_dashboard(request: Request):
    _require_admin(request)
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as n, SUM(ok) as ok_sum FROM bookings")
    row = cur.fetchone()
    total = int(row["n"] or 0)
    ok_sum = int(row["ok_sum"] or 0)
    ok_rate = (ok_sum / total * 100.0) if total else 0.0

    cur.execute("SELECT * FROM bookings ORDER BY id DESC LIMIT 25")
    last = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM customers ORDER BY updated_at DESC LIMIT 25")
    cust = [dict(r) for r in cur.fetchall()]
    conn.close()

    return {
        "stats": {"total": total, "ok": ok_sum, "ok_rate_pct": round(ok_rate, 2)},
        "last_bookings": last,
        "customers": cust,
    }


@app.get("/_admin/customer/{phone}")
def admin_customer(phone: str, request: Request):
    _require_admin(request)
    c = _get_customer(re.sub(r"[^\d]", "", phone))
    return {"customer": c}


@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione, request: Request):
    if DEBUG_ECHO_PAYLOAD:
        try:
            raw = await request.json()
            print("🧾 RAW_PAYLOAD:", json.dumps(raw, ensure_ascii=False))
        except Exception:
            pass

    # Validazioni
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dati.data or ""):
        msg = f"Formato data non valido: {dati.data}. Usa YYYY-MM-DD."
        _log_booking(dati.model_dump(), False, msg)
        return {"ok": False, "message": msg}

    if not re.fullmatch(r"\d{2}:\d{2}", dati.orario or ""):
        msg = f"Formato orario non valido: {dati.orario}. Usa HH:MM (es. 13:00)."
        _log_booking(dati.model_dump(), False, msg)
        return {"ok": False, "message": msg}

    if not isinstance(dati.persone, int) or dati.persone < 1 or dati.persone > 50:
        msg = f"Numero persone non valido: {dati.persone}."
        _log_booking(dati.model_dump(), False, msg)
        return {"ok": False, "message": msg}

    fase = (dati.fase or "book").strip().lower()
    if fase not in ("availability", "book"):
        msg = f'Valore fase non valido: {dati.fase}. Usa "availability" oppure "book".'
        _log_booking(dati.model_dump(), False, msg)
        return {"ok": False, "message": msg}

    # Regola operativa: oltre 9 persone gestiamo con operatore umano (tavolate)
    if int(dati.persone) > 9:
        msg = "Per tavoli da più di 9 persone gestiamo una divisione gruppi: contatta il centralino 06 56556 263."
        _log_booking(dati.model_dump(), False, msg)
        return {"ok": False, "message": msg, "handoff": True, "phone": "06 56556 263"}

    # In fase book, i dati cliente e la sede sono obbligatori
    if fase == "book":
        if not (dati.sede or "").strip():
            msg = "Sede mancante."
            _log_booking(dati.model_dump(), False, msg)
            return {"ok": False, "message": msg}
        if not (dati.nome or "").strip():
            msg = "Nome mancante."
            _log_booking(dati.model_dump(), False, msg)
            return {"ok": False, "message": msg}
        tel_clean = re.sub(r"[^\d]", "", dati.telefono or "")
        if len(tel_clean) < 6:
            msg = "Telefono mancante o non valido."
            _log_booking(dati.model_dump(), False, msg)
            return {"ok": False, "message": msg}

    sede_target = dati.sede
    orario_req = dati.orario
    pasto = _calcola_pasto(orario_req)

    # NOTE: sanificazione richiesta
    note_in = (dati.note or "")
    note_in = re.sub(r"\s+", " ", note_in).strip()[:250]

    seggiolini = int(dati.seggiolini or 0)
    telefono = re.sub(r"[^\d]", "", dati.telefono or "")
    email = dati.email or "default@prenotazioni.com"

    # Memoria clienti: se email è quella di default e ho un’email reale salvata, usa quella
    cust = _get_customer(telefono) if telefono else None
    if cust and (email == "default@prenotazioni.com") and cust.get("email") and ("@" in cust["email"]):
        email = cust["email"]

    print(
        f"🚀 BOOKING: {dati.nome} -> {sede_target} | {dati.data} {orario_req} | "
        f"pax={dati.persone} | pasto={pasto} | seggiolini={seggiolini} | note='{note_in}'"
    )

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
        await page.route("**/*", _block_heavy)

        # Intercetta ajax.php per capire esito reale (OK / messaggio)
        last_ajax_result = {"seen": False, "text": ""}

        async def on_response(resp):
            try:
                url = (resp.url or "").lower()
                if "ajax.php" in url:
                    txt = await resp.text()
                    last_ajax_result["seen"] = True
                    last_ajax_result["text"] = (txt or "").strip()
                    print("🧩 AJAX_RESPONSE:", last_ajax_result["text"][:500])
            except Exception:
                pass

        page.on("response", on_response)

        if DEBUG_LOG_AJAX_POST:
            async def on_request(req):
                try:
                    url = req.url.lower()
                    if "ajax.php" in url and req.method.upper() == "POST":
                        print("🌐 AJAX_POST_URL:", req.url)
                        print("🌐 AJAX_POST_BODY:", (req.post_data or "")[:4000])
                except Exception:
                    pass
            page.on("request", on_request)

        try:
            await page.goto(BOOKING_URL, wait_until="domcontentloaded")
            await _maybe_click_cookie(page)
            await _wait_ready(page)

            # STEP 1: persone + seggiolini
            await _click_persone(page, int(dati.persone))
            await _set_seggiolini(page, seggiolini)

            # STEP 2: data
            await _set_date(page, dati.data)

            # STEP 3: pasto
            await _click_pasto(page, pasto)

            if fase == "availability":
                # 1) lista sedi (con eventuali bottoni turno presenti in UI)
                sedi = await _scrape_sedi_availability(page)

                # 2) se il cliente ha già indicato una sede, entriamo nella sede
                #    e leggiamo gli orari REALMENTE disponibili dal dropdown (quando presente).
                sede_selezionata = None
                orari_disponibili = []
                orario_richiesto_disponibile = None
                orario_suggerito = None
                time_read_error = None

                if sede_target and sede_target.strip():
                    try:
                        clicked = await _click_sede(page, sede_target)
                        if not clicked:
                            return {
                                "ok": True,
                                "fase": "availability",
                                "sedi": [{"nome": sede_target, "sold_out": True}],
                                "orari_disponibili": [],
                                "orario_richiesto_disponibile": False,
                                "orario_suggerito": "",
                            }
                        # Porta in vista il selettore orario (alcune UI caricano le option solo dopo scroll/click)
                        try:
                            await page.locator('#OraPren').scroll_into_view_if_needed(timeout=3000)
                        except Exception:
                            pass
                        # Se esistono pulsanti I/II TURNO nella UI, scegli in base all'orario richiesto
                        await _maybe_select_turn(page, pasto, orario_req)

                        opts = await _get_orario_options(page)  # [(value,text)]
                        for v, t in opts:
                            tt = (t or "").strip()
                            if re.match(r"^\d{1,2}:\d{2}$", tt):
                                hh, mm = tt.split(":")
                                orari_disponibili.append(f"{int(hh):02d}:{mm}")
                        orari_disponibili = sorted(set(orari_disponibili))

                        sede_selezionata = sede_target.strip()
                        if orari_disponibili:
                            orario_richiesto_disponibile = (orario_req in orari_disponibili)
                            if not orario_richiesto_disponibile:
                                options_for_pick = [(o, o) for o in orari_disponibili]
                                best = _pick_closest_time(orario_req, options_for_pick)
                                orario_suggerito = best
                    except Exception as e:
                        orari_disponibili = []
                        orario_richiesto_disponibile = None
                        orario_suggerito = None
                        sede_selezionata = sede_target.strip()
                        time_read_error = str(e)

                msg = "Disponibilità rilevata."
                payload_log = dati.model_dump()
                payload_log.update({"fase": "availability", "seggiolini": seggiolini})
                _log_booking(payload_log, True, msg)
                return {
                    "ok": True,
                    "fase": "availability",
                    "message": msg,
                    "data": dati.data,
                    "orario_richiesto": orario_req,
                    "pasto": pasto,
                    "persone": int(dati.persone),
                    "seggiolini": seggiolini,
                    "sedi": sedi,
                    "sede_selezionata": sede_selezionata,
                    "orari_disponibili": orari_disponibili,
                    "orario_richiesto_disponibile": orario_richiesto_disponibile,
                    "orario_suggerito": orario_suggerito,
                    "time_read_error": time_read_error,
                }

            # STEP 4: sede
            clicked = await _click_sede(page, sede_target)
            if not clicked:
                return {"ok": False, "status": "SOLD_OUT", "fase": "book", "message": "Sede esaurita"}
            await _maybe_select_turn(page, pasto, orario_req)


            # STEP 5: orario (con retry intelligente)
            selected_orario_value = None
            used_fallback = False
            last_select_error = None
            for _ in range(max(1, MAX_SLOT_RETRIES)):
                try:
                    selected_orario_value, used_fallback = await _select_orario_or_retry(page, orario_req)
                    break
                except Exception as e:
                    last_select_error = e
            if not selected_orario_value:
                raise RuntimeError(str(last_select_error) if last_select_error else "Orario non disponibile")

            # STEP 5: note (campo “Vuoi aggiungere qualcosa?”)
            await _fill_note_step5_strong(page, note_in)

            # Conferma step5 -> form dati
            await _click_conferma(page)

            # Form dati
            await _fill_form(page, dati.nome, email, telefono)

            if DISABLE_FINAL_SUBMIT:
                msg = "FORM COMPILATO (test mode, submit disattivato)"
                payload_log = dati.model_dump()
                payload_log.update({"email": email, "note": note_in, "seggiolini": seggiolini})
                _log_booking(payload_log, True, msg)
                return {"ok": True, "message": msg, "note": note_in, "seggiolini": seggiolini, "fallback_time": used_fallback}

            
            # Submit con retry se ajax.php risponde con “pieno/non disponibile”
            # IMPORTANT: non considerare mai "ok" se non abbiamo una conferma dal sistema.
            submit_attempts = 0
            while True:
                submit_attempts += 1
                last_ajax_result["seen"] = False
                last_ajax_result["text"] = ""

                await _click_prenota(page)

                # Attendi la risposta AJAX (fino a ~6s)
                for _ in range(12):
                    if last_ajax_result["seen"]:
                        break
                    await page.wait_for_timeout(500)

                if not last_ajax_result["seen"]:
                    raise RuntimeError("Prenotazione NON confermata: nessuna risposta dal sistema (AJAX non intercettato).")

                ajax_txt = (last_ajax_result["text"] or "").strip()

                if ajax_txt == "OK":
                    break  # success “reale”

                # Slot pieno: retry su altro orario (se previsto)
                if _looks_like_full_slot(ajax_txt) and submit_attempts <= MAX_SUBMIT_RETRIES:
                    options = await _get_orario_options(page)
                    options = [(v, t) for (v, t) in options if v != selected_orario_value]
                    best = _pick_closest_time(orario_req, options)
                    if not best:
                        raise RuntimeError(
                            f"Slot pieno e nessun orario alternativo entro {RETRY_TIME_WINDOW_MIN} min. Msg: {ajax_txt}"
                        )

                    # Torna a inizio flusso e riprova con nuovo orario
                    await page.goto(BOOKING_URL, wait_until="domcontentloaded")
                    await _maybe_click_cookie(page)
                    await _wait_ready(page)
                    await _click_persone(page, int(dati.persone))
                    await _set_seggiolini(page, seggiolini)
                    await _set_date(page, dati.data)
                    await _click_pasto(page, pasto)
                    clicked = await _click_sede(page, sede_target)
                    if not clicked:
                        return {"ok": False, "status": "SOLD_OUT", "fase": "book", "message": "Sede esaurita"}

                    ok_sel = await _select_orario_value(page, best)
                    if not ok_sel:
                        raise RuntimeError(f"Impossibile selezionare l'orario fallback: {best}")
                    selected_orario_value = best
                    used_fallback = True
                    await _fill_note_step5_strong(page, note_in)
                    await _click_conferma(page)
                    await _fill_form(page, dati.nome, "", email, telefono)
                    continue
                # Alcuni siti rispondono con codici brevi (es. MS_PS) quando manca un consenso.
                if ajax_txt and re.fullmatch(r"[A-Z_]{4,10}", ajax_txt) and submit_attempts <= (MAX_SUBMIT_RETRIES + 1):
                    # Prova a (ri)spuntare consensi e ripetere una volta
                    try:
                        boxes = page.locator("#prenoForm input[type=checkbox]")
                        n = await boxes.count()
                        for i in range(n):
                            b = boxes.nth(i)
                            try:
                                if await b.is_checked():
                                    continue
                            except Exception:
                                pass
                            name = (await b.get_attribute("name") or "").lower()
                            _id = (await b.get_attribute("id") or "").lower()
                            req = await b.get_attribute("required")
                            is_relevant = bool(req) or any(k in (name + " " + _id) for k in ["privacy", "consenso", "termin", "gdpr", "policy"])
                            if not is_relevant:
                                continue
                            try:
                                await b.scroll_into_view_if_needed()
                                await b.click(timeout=2000, force=True)
                            except Exception:
                                try:
                                    if _id:
                                        lab = page.locator(f'label[for="{_id}"]').first
                                        if await lab.count() > 0:
                                            await lab.click(timeout=2000, force=True)
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    continue

                # Errore esplicito dal sito
                raise RuntimeError(f"Errore dal sito: {ajax_txt}")


            # Salvataggio memoria clienti
            if telefono:
                _upsert_customer(
                    phone=telefono,
                    name=dati.nome,
                    email=email,
                    sede=sede_target,
                    persone=int(dati.persone),
                    seggiolini=seggiolini,
                    note=note_in,
                )

            msg = f"Prenotazione inviata: {dati.persone} pax - {sede_target} {dati.data} {selected_orario_value[:5]} - {dati.nome}"
            payload_log = dati.model_dump()
            payload_log.update({"email": email, "note": note_in, "seggiolini": seggiolini, "orario": selected_orario_value[:5]})
            _log_booking(payload_log, True, msg)

            return {
                "ok": True,
                "message": msg,
                "note": note_in,
                "seggiolini": seggiolini,
                "fallback_time": used_fallback,
                "selected_time": selected_orario_value[:5],
            }

        except Exception as e:
            screenshot_path = None
            try:
                ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
                screenshot_path = f"booking_error_{ts}.png"
                await page.screenshot(path=screenshot_path, full_page=True)
                print(f"📸 Screenshot salvato: {screenshot_path}")
            except Exception:
                pass

            payload_log = dati.model_dump()
            payload_log.update({"note": note_in, "seggiolini": seggiolini})
            _log_booking(payload_log, False, str(e))

            user_msg = "Ti prego di attendere qualche secondo, sto verificando la disponibilità."
            return {"ok": False, "message": user_msg, "error": str(e), "screenshot": screenshot_path}
        finally:
            await browser.close()
