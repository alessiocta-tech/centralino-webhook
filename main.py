import os
import re
import json
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Optional, Union, List, Dict, Any, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from pydantic import BaseModel, Field, ConfigDict, model_validator
from playwright.async_api import async_playwright

# ============================================================
# CONFIG (env)
# ============================================================

BOOKING_URL = os.getenv("BOOKING_URL", "https://rione.fidy.app/prenew.php?referer=AI")

PW_TIMEOUT_MS = int(os.getenv("PW_TIMEOUT_MS", "60000"))
PW_NAV_TIMEOUT_MS = int(os.getenv("PW_NAV_TIMEOUT_MS", "60000"))

DISABLE_FINAL_SUBMIT = os.getenv("DISABLE_FINAL_SUBMIT", "false").lower() == "true"

DEBUG_ECHO_PAYLOAD = os.getenv("DEBUG_ECHO_PAYLOAD", "false").lower() == "true"
DEBUG_LOG_AJAX_POST = os.getenv("DEBUG_LOG_AJAX_POST", "false").lower() == "true"

# retry “slot pieno / orario non disponibile”
MAX_BOOKING_ATTEMPTS = int(os.getenv("MAX_BOOKING_ATTEMPTS", "2"))  # tentativi end-to-end
RETRY_TIME_WINDOW_MIN = int(os.getenv("RETRY_TIME_WINDOW_MIN", "45"))  # +/- minuti
RETRY_STEP_MIN = int(os.getenv("RETRY_STEP_MIN", "15"))  # step tra orari

# memoria / dashboard
DB_PATH = os.getenv("DB_PATH", "/tmp/booking.db")  # su Railway metti un Volume se vuoi persistere
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "")  # se vuoto, dashboard non protetta

DEFAULT_EMAIL = os.getenv("DEFAULT_EMAIL", "prenotazione@prenotazione.com")

IPHONE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)

app = FastAPI()

# ============================================================
# DB (sqlite) + lock
# ============================================================

_db_lock = threading.Lock()


def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _db_init():
    with _db_lock:
        conn = _db_connect()
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS clients (
                telefono TEXT PRIMARY KEY,
                nome TEXT,
                email TEXT,
                first_seen TEXT,
                last_seen TEXT,
                bookings_count INTEGER DEFAULT 0
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                telefono TEXT,
                nome TEXT,
                email TEXT,
                sede TEXT,
                data TEXT,
                orario_requested TEXT,
                orario_used TEXT,
                persone INTEGER,
                note TEXT,
                status TEXT,
                message TEXT,
                dialog_alert TEXT
            )
            """
        )
        conn.commit()
        conn.close()


@app.on_event("startup")
def _startup():
    _db_init()


async def db_get_client(telefono: str) -> Optional[Dict[str, Any]]:
    def _work():
        with _db_lock:
            conn = _db_connect()
            cur = conn.cursor()
            cur.execute("SELECT * FROM clients WHERE telefono=?", (telefono,))
            row = cur.fetchone()
            conn.close()
            return dict(row) if row else None

    return await run_in_threadpool(_work)


async def db_upsert_client(telefono: str, nome: str, email: str):
    now = datetime.utcnow().isoformat()

    def _work():
        with _db_lock:
            conn = _db_connect()
            cur = conn.cursor()
            cur.execute("SELECT telefono, bookings_count FROM clients WHERE telefono=?", (telefono,))
            row = cur.fetchone()
            if row:
                cur.execute(
                    """
                    UPDATE clients
                    SET nome=?, email=?, last_seen=?, bookings_count=bookings_count+1
                    WHERE telefono=?
                    """,
                    (nome, email, now, telefono),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO clients(telefono, nome, email, first_seen, last_seen, bookings_count)
                    VALUES(?,?,?,?,?,1)
                    """,
                    (telefono, nome, email, now, now),
                )
            conn.commit()
            conn.close()

    await run_in_threadpool(_work)


async def db_insert_booking(
    telefono: str,
    nome: str,
    email: str,
    sede: str,
    data: str,
    orario_requested: str,
    orario_used: str,
    persone: int,
    note: str,
    status: str,
    message: str,
    dialog_alert: str,
):
    now = datetime.utcnow().isoformat()

    def _work():
        with _db_lock:
            conn = _db_connect()
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO bookings(
                  ts, telefono, nome, email, sede, data,
                  orario_requested, orario_used, persone, note,
                  status, message, dialog_alert
                )
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    now,
                    telefono,
                    nome,
                    email,
                    sede,
                    data,
                    orario_requested,
                    orario_used,
                    persone,
                    note,
                    status,
                    message,
                    dialog_alert,
                ),
            )
            conn.commit()
            conn.close()

    await run_in_threadpool(_work)


async def db_recent_bookings(limit: int = 50) -> List[Dict[str, Any]]:
    def _work():
        with _db_lock:
            conn = _db_connect()
            cur = conn.cursor()
            cur.execute("SELECT * FROM bookings ORDER BY id DESC LIMIT ?", (limit,))
            rows = cur.fetchall()
            conn.close()
            return [dict(r) for r in rows]

    return await run_in_threadpool(_work)


async def db_stats() -> Dict[str, Any]:
    def _work():
        with _db_lock:
            conn = _db_connect()
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS n FROM bookings")
            total = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM bookings WHERE status='ok'")
            ok = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM bookings WHERE status='fail'")
            fail = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM clients")
            clients = cur.fetchone()["n"]
            conn.close()
            return {"total": total, "ok": ok, "fail": fail, "clients": clients}

    return await run_in_threadpool(_work)


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


def _sanitize_note(note_in: str) -> str:
    # ✅ fix richiesto: comprime spazi, trim, max 250
    return re.sub(r"\s+", " ", (note_in or "")).strip()[:250]


# ============================================================
# MODEL (Pydantic v2)
# ============================================================

class RichiestaPrenotazione(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    nome: str
    email: Optional[str] = ""
    telefono: str

    sede: str
    data: str
    orario: str
    persone: Union[int, str] = Field(...)

    # accetta sia "note" che "nota"
    note: Optional[str] = Field("", alias="nota")

    @model_validator(mode="before")
    @classmethod
    def _coerce_fields(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values

        # se arriva "note", copia su alias "nota" (e poi verrà letto come .note)
        if values.get("note") not in (None, ""):
            values["nota"] = values.get("note")

        p = values.get("persone")
        if isinstance(p, str):
            p2 = re.sub(r"[^\d]", "", p)
            if p2:
                values["persone"] = int(p2)

        if values.get("orario") is not None:
            values["orario"] = _norm_orario(str(values["orario"]))

        if values.get("sede") is not None:
            values["sede"] = _normalize_sede(str(values["sede"]))

        if values.get("telefono") is not None:
            values["telefono"] = re.sub(r"[^\d]", "", str(values["telefono"]))

        if not values.get("email"):
            values["email"] = DEFAULT_EMAIL

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


async def _click_seggiolini_no(page):
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


def _hhmm_to_minutes(hhmm: str) -> int:
    hh, mm = hhmm.split(":")
    return int(hh) * 60 + int(mm)


def _minutes_to_hhmm(m: int) -> str:
    m = max(0, min(23 * 60 + 59, m))
    return f"{m // 60:02d}:{m % 60:02d}"


async def _get_orari_options(page) -> List[str]:
    # ritorna lista HH:MM (da option text o value)
    opts = await page.evaluate(
        """() => {
          const sel = document.querySelector('#OraPren');
          if (!sel) return [];
          const out = [];
          for (const o of Array.from(sel.options)) {
            if (o.disabled) continue;
            const txt = (o.textContent || '').trim();
            const val = (o.value || '').trim();
            // prova a estrarre HH:MM dal testo, altrimenti dal value (che spesso è HH:MM:SS)
            const m = txt.match(/\\b(\\d{1,2}):(\\d{2})\\b/);
            if (m) out.push((m[1].padStart(2,'0')) + ':' + m[2]);
            else {
              const mv = val.match(/^(\\d{1,2}):(\\d{2})/);
              if (mv) out.push((mv[1].padStart(2,'0')) + ':' + mv[2]);
            }
          }
          // unique
          return Array.from(new Set(out));
        }"""
    )
    return opts or []


def _pick_best_alternative(requested: str, available: List[str]) -> Optional[str]:
    if not available:
        return None

    req_m = _hhmm_to_minutes(requested)
    candidates: List[Tuple[int, str]] = []
    for hhmm in available:
        try:
            mm = _hhmm_to_minutes(hhmm)
            dist = abs(mm - req_m)
            candidates.append((dist, hhmm))
        except Exception:
            pass
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1] if candidates else None


def _build_retry_targets(orario: str) -> List[str]:
    # genera orari +/- finestra con step, includendo prima orari “dopo” e poi “prima”
    base = _hhmm_to_minutes(orario)
    step = max(5, RETRY_STEP_MIN)
    win = max(step, RETRY_TIME_WINDOW_MIN)

    after = [base + i for i in range(step, win + 1, step)]
    before = [base - i for i in range(step, win + 1, step)]

    targets = [base] + after + before
    out = []
    for m in targets:
        hhmm = _minutes_to_hhmm(m)
        if hhmm not in out:
            out.append(hhmm)
    return out


async def _select_orario(page, orario_hhmm: str):
    await page.wait_for_selector("#OraPren", state="visible", timeout=PW_TIMEOUT_MS)

    wanted = orario_hhmm.strip()
    wanted_val = wanted + ":00" if re.fullmatch(r"\d{2}:\d{2}", wanted) else wanted

    await page.wait_for_function(
        """() => {
          const sel = document.querySelector('#OraPren');
          return sel && sel.options && sel.options.length > 1;
        }""",
        timeout=PW_TIMEOUT_MS,
    )

    # 1) prova select_option per value
    try:
        res = await page.locator("#OraPren").select_option(value=wanted_val)
        if res:
            return
    except Exception:
        pass

    # 2) fallback: match testo
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
        return

    raise RuntimeError(f"Orario non disponibile: {wanted}")


async def _fill_note_step5_strong(page, note: str):
    note = _sanitize_note(note)
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

    # copia anche nel hidden Nota2
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


async def _fill_form(page, nome: str, email: str, telefono: str):
    parti = (nome or "").strip().split(" ", 1)
    nome1 = parti[0] if parti else (nome or "Cliente")
    cognome = parti[1] if len(parti) > 1 else "Cliente"

    await page.wait_for_selector("#prenoForm", state="visible", timeout=PW_TIMEOUT_MS)
    await page.locator("#Nome").fill(nome1, timeout=8000)
    await page.locator("#Cognome").fill(cognome, timeout=8000)
    await page.locator("#Email").fill(email, timeout=8000)
    await page.locator("#Telefono").fill(telefono, timeout=8000)


async def _click_prenota(page):
    loc = page.locator('input[type="submit"][value="PRENOTA"]').first
    if await loc.count() > 0:
        await loc.click(timeout=15000, force=True)
        return
    await page.locator("text=/PRENOTA/i").last.click(timeout=15000, force=True)


def _is_slot_full_message(msg: str) -> bool:
    # euristiche: adattale se conosci i testi del tuo ajax.php
    m = (msg or "").lower()
    patterns = [
        "orario non disponibile",
        "non disponibile",
        "posti esauriti",
        "completo",
        "nessuna disponibilità",
        "seleziona prima un orario",
    ]
    return any(p in m for p in patterns)


# ============================================================
# DASHBOARD auth
# ============================================================

def _check_dashboard_auth(request: Request) -> bool:
    if not DASHBOARD_TOKEN:
        return True
    token = request.query_params.get("token", "")
    if token == DASHBOARD_TOKEN:
        return True
    header = request.headers.get("x-dashboard-token", "")
    return header == DASHBOARD_TOKEN


# ============================================================
# ROUTES
# ============================================================

@app.get("/", response_class=JSONResponse)
def home():
    return {
        "status": "Centralino AI - Booking Engine (Railway)",
        "disable_final_submit": DISABLE_FINAL_SUBMIT,
        "db_path": DB_PATH,
        "max_booking_attempts": MAX_BOOKING_ATTEMPTS,
    }


@app.get("/health", response_class=JSONResponse)
async def health():
    s = await db_stats()
    return {"ok": True, "stats": s}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not _check_dashboard_auth(request):
        return HTMLResponse("Unauthorized", status_code=401)

    stats = await db_stats()
    bookings = await db_recent_bookings(60)

    def esc(x: Any) -> str:
        return (str(x) if x is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    rows = []
    for b in bookings:
        status = b.get("status", "")
        badge = "✅" if status == "ok" else "❌"
        rows.append(
            f"""
            <tr>
              <td>{esc(b.get("id"))}</td>
              <td>{esc(b.get("ts"))}</td>
              <td>{badge} {esc(status)}</td>
              <td>{esc(b.get("nome"))}</td>
              <td>{esc(b.get("telefono"))}</td>
              <td>{esc(b.get("sede"))}</td>
              <td>{esc(b.get("data"))}</td>
              <td>{esc(b.get("orario_requested"))}</td>
              <td><b>{esc(b.get("orario_used"))}</b></td>
              <td>{esc(b.get("persone"))}</td>
              <td style="max-width:320px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{esc(b.get("note"))}</td>
              <td style="max-width:380px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{esc(b.get("message"))}</td>
            </tr>
            """
        )

    html = f"""
    <html>
      <head>
        <meta charset="utf-8"/>
        <meta name="viewport" content="width=device-width, initial-scale=1"/>
        <title>deRione - Dashboard Prenotazioni</title>
        <style>
          body {{ font-family: Arial, sans-serif; padding: 18px; }}
          .cards {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom: 16px; }}
          .card {{ border:1px solid #ddd; border-radius: 10px; padding: 12px 14px; min-width: 180px; }}
          table {{ width:100%; border-collapse: collapse; }}
          th, td {{ border-bottom: 1px solid #eee; padding: 8px; font-size: 13px; text-align:left; }}
          th {{ position: sticky; top: 0; background: #fafafa; }}
        </style>
      </head>
      <body>
        <h2>Dashboard Prenotazioni</h2>

        <div class="cards">
          <div class="card"><div><b>Totale</b></div><div style="font-size:22px">{stats["total"]}</div></div>
          <div class="card"><div><b>OK</b></div><div style="font-size:22px">{stats["ok"]}</div></div>
          <div class="card"><div><b>Fail</b></div><div style="font-size:22px">{stats["fail"]}</div></div>
          <div class="card"><div><b>Clienti</b></div><div style="font-size:22px">{stats["clients"]}</div></div>
        </div>

        <h3>Ultime prenotazioni</h3>
        <table>
          <thead>
            <tr>
              <th>ID</th><th>TS</th><th>Stato</th><th>Nome</th><th>Telefono</th><th>Sede</th>
              <th>Data</th><th>Ora richiesta</th><th>Ora usata</th><th>Pax</th><th>Note</th><th>Messaggio</th>
            </tr>
          </thead>
          <tbody>
            {''.join(rows)}
          </tbody>
        </table>
      </body>
    </html>
    """
    return HTMLResponse(html)


@app.get("/clients/{telefono}", response_class=JSONResponse)
async def client_lookup(telefono: str):
    t = re.sub(r"[^\d]", "", telefono or "")
    c = await db_get_client(t)
    if not c:
        return JSONResponse({"ok": False, "message": "not found"}, status_code=404)
    return {"ok": True, "client": c}


# ============================================================
# BOOKING CORE (1 attempt)
# ============================================================

async def _run_booking_attempt(
    *,
    dati: RichiestaPrenotazione,
    orario_target: str,
) -> Dict[str, Any]:
    sede_target = dati.sede
    pasto = _calcola_pasto(orario_target)
    note_in = _sanitize_note(dati.note or "")

    dialog_alert = ""

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

        # cattura eventuali alert() del sito (es: ajax.php risponde errore)
        async def on_dialog(d):
            nonlocal dialog_alert
            try:
                dialog_alert = (d.message or "").strip()
                await d.dismiss()
            except Exception:
                pass

        page.on("dialog", on_dialog)

        if DEBUG_LOG_AJAX_POST:
            async def on_request(req):
                try:
                    url = (req.url or "").lower()
                    if "ajax.php" in url and req.method.upper() == "POST":
                        print("🌐 AJAX_POST_URL:", req.url)
                        print("🌐 AJAX_POST_BODY:", req.post_data or "")
                except Exception:
                    pass
            page.on("request", on_request)

        try:
            await page.goto(BOOKING_URL, wait_until="domcontentloaded")
            await _maybe_click_cookie(page)
            await _wait_ready(page)

            await _click_persone(page, int(dati.persone))
            await _click_seggiolini_no(page)

            await _set_date(page, dati.data)
            await _click_pasto(page, pasto)

            await _click_sede(page, sede_target)

            # selezione orario (se fallisce → eccezione gestita sopra)
            await _select_orario(page, orario_target)

            # note
            await _fill_note_step5_strong(page, note_in)

            # conferma step5 → form dati
            await _click_conferma(page)
            await _fill_form(page, dati.nome, dati.email, dati.telefono)

            if DISABLE_FINAL_SUBMIT:
                return {
                    "ok": True,
                    "status": "ok",
                    "message": "FORM COMPILATO (test mode, submit disattivato)",
                    "orario_used": orario_target,
                    "dialog_alert": dialog_alert,
                }

            await _click_prenota(page)

            # lascia tempo ad ajax/redirect
            await page.wait_for_timeout(2000)

            # se è uscito un alert, trattalo come errore “gestionale”
            if dialog_alert:
                return {
                    "ok": False,
                    "status": "fail",
                    "message": f"alert: {dialog_alert}",
                    "orario_used": orario_target,
                    "dialog_alert": dialog_alert,
                }

            return {
                "ok": True,
                "status": "ok",
                "message": f"Prenotazione inviata: {dati.persone} pax - {sede_target} {dati.data} {orario_target} - {dati.nome}",
                "orario_used": orario_target,
                "dialog_alert": dialog_alert,
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

            msg = str(e)
            if dialog_alert and dialog_alert not in msg:
                msg = f"{msg} | alert={dialog_alert}"

            return {
                "ok": False,
                "status": "fail",
                "message": msg,
                "orario_used": orario_target,
                "dialog_alert": dialog_alert,
                "screenshot": screenshot_path,
            }
        finally:
            await browser.close()


# ============================================================
# BOOK_TABLE endpoint (retry + memoria + log DB)
# ============================================================

@app.post("/book_table")
async def book_table(dati: RichiestaPrenotazione, request: Request):
    if DEBUG_ECHO_PAYLOAD:
        try:
            raw = await request.json()
            print("🧾 RAW_PAYLOAD:", json.dumps(raw, ensure_ascii=False))
        except Exception:
            pass

    # Validazioni minime
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dati.data or ""):
        return {"ok": False, "message": f"Formato data non valido: {dati.data}. Usa YYYY-MM-DD."}
    if not re.fullmatch(r"\d{2}:\d{2}", dati.orario or ""):
        return {"ok": False, "message": f"Formato orario non valido: {dati.orario}. Usa HH:MM (es. 13:00)."}
    if not isinstance(dati.persone, int) or dati.persone < 1 or dati.persone > 50:
        return {"ok": False, "message": f"Numero persone non valido: {dati.persone}."}

    # memoria: se email è default e ho un client con email reale, uso quella
    client = await db_get_client(dati.telefono)
    if client:
        if (dati.email or "").strip().lower() == DEFAULT_EMAIL.lower():
            ce = (client.get("email") or "").strip()
            if ce and ce.lower() != DEFAULT_EMAIL.lower():
                dati.email = ce

        # se nome “debole”, preferisci quello in memoria (opzionale, non invasivo)
        if dati.nome and len(dati.nome.strip()) <= 2:
            cn = (client.get("nome") or "").strip()
            if cn:
                dati.nome = cn

    sede_target = dati.sede
    orario_requested = dati.orario
    pasto = _calcola_pasto(orario_requested)
    note_in = _sanitize_note(dati.note or "")

    print(
        f"🚀 BOOKING: {dati.nome} -> {sede_target} | {dati.data} {orario_requested} | pax={dati.persone} | pasto={pasto} | note='{note_in}'"
    )

    # strategia retry:
    # 1) prova orario richiesto
    # 2) se “slot pieno / orario non disponibile”, prova alternative (closest disponibili) entro finestra
    # 3) max tentativi end-to-end = MAX_BOOKING_ATTEMPTS
    attempts_used = 0
    last_result: Dict[str, Any] = {}

    # lista target in ordine (richiesto + range +/-)
    retry_targets = _build_retry_targets(orario_requested)

    # limiter “end-to-end”: non provare troppe volte anche se retry_targets è lunga
    for target in retry_targets:
        if attempts_used >= MAX_BOOKING_ATTEMPTS:
            break

        attempts_used += 1
        res = await _run_booking_attempt(dati=dati, orario_target=target)
        last_result = res

        # log su DB a ogni tentativo
        await db_insert_booking(
            telefono=dati.telefono,
            nome=dati.nome,
            email=dati.email or DEFAULT_EMAIL,
            sede=sede_target,
            data=dati.data,
            orario_requested=orario_requested,
            orario_used=res.get("orario_used", target),
            persone=int(dati.persone),
            note=note_in,
            status="ok" if res.get("ok") else "fail",
            message=res.get("message", ""),
            dialog_alert=res.get("dialog_alert", "") or "",
        )

        if res.get("ok"):
            # aggiorna “memoria” cliente
            await db_upsert_client(dati.telefono, dati.nome, dati.email or DEFAULT_EMAIL)
            return res

        # se il fallimento NON sembra “slot pieno”, stoppa subito (non sprecare retry)
        msg = (res.get("message") or "")
        if not _is_slot_full_message(msg):
            break

        # se è slot pieno e ho ancora tentativi, continua verso prossimo target

    # nessun tentativo riuscito
    return last_result or {"ok": False, "message": "Prenotazione non riuscita"}


"""
COSA DEVI FARE (Railway)
1) Se vuoi “memoria clienti” persistente: crea un Volume e monta DB_PATH lì (es: /data/booking.db)
   - Imposta DB_PATH=/data/booking.db
2) (Opzionale) Proteggi dashboard:
   - Imposta DASHBOARD_TOKEN=una_stringa_lunga
   - Apri /dashboard?token=... oppure header X-Dashboard-Token
3) Se vuoi più retry:
   - MAX_BOOKING_ATTEMPTS=2 o 3 (consiglio 2)
   - RETRY_TIME_WINDOW_MIN=45
   - RETRY_STEP_MIN=15
4) Debug:
   - DEBUG_ECHO_PAYLOAD=true (poi rimettilo false)
   - DEBUG_LOG_AJAX_POST=true solo se ti serve analizzare ajax.php
"""
