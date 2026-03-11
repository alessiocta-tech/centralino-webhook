# CLAUDE.md — Centralino Webhook

## Overview

This is the **backend webhook service** for the **deRione** restaurant chain's AI phone agent. A voice AI agent named **Giulia** answers incoming phone calls and handles table reservations by calling this webhook. The webhook uses FastAPI to expose the API and Playwright to drive a headless Chromium browser on the booking website (`rione.fidy.app`).

The entire application lives in a single file: `main.py` (≈1550 lines).

---

## System Architecture

```
📞 Incoming phone call
    ↓
🤖 Giulia (voice AI agent — Italian, informal "tu")
    ↓
    ├── POST /resolve_date          (date parsing)
    ├── POST /book_table            (availability + booking via Playwright)
    ├── GET  /check_reservation     (verify existing booking)
    ├── POST /find_reservation_for_cancel  (search booking to cancel)
    ├── POST /cancel_reservation    (cancel booking)
    ├── POST /update_covers         (change party size)
    └── POST /add_note              (add note to booking)
         ↓
🌐 centralino-webhook (this repo — FastAPI + Playwright + httpx)
    ↓                          ↓
🖥️ rione.fidy.app          🔌 api.fidy.app
   (Playwright browser)       (Fidy REST API — check/cancel/update/note)
```

**Giulia** is the voice AI agent that speaks to customers on the phone. She handles new reservations, verification, cancellation, cover updates, and note additions by calling this webhook. The webhook either drives a headless browser (for new bookings) or proxies requests to the Fidy REST API (for all other operations).

Production URL: `https://centralino-webhook-production.up.railway.app`

---

## Voice Agent Integration (Giulia)

### Agent Identity
- Name: **Giulia**, assistente digitale di deRione
- Language: Italian, informal ("tu")
- Tone: Professional, clear, no irony, no jokes
- Rule: One question at a time (exception: "Nome e cellulare?")

### Info Collection Rules
- **Extract all info mentioned in the first message**: if the customer says "prenota per sabato sera per due persone a Talenti", extract date=sabato, persone=2, sede=Talenti — do NOT re-ask these fields.
- Skip any step whose information was already provided by the customer, even if provided in a previous turn.
- Retain all collected info across the entire conversation (name, phone, email, notes, sede, persone, orario). Never re-ask info already given.

### Conversational Flow — Sequenza Obbligatoria

Segui questo ordine **esatto** per ogni nuova prenotazione. Salta i passi per cui il dato è già noto dal messaggio del cliente.

**Passo 1 — DATA**
Chiama `POST /resolve_date` per convertire espressioni relative ("sabato sera", "domani") in data ISO.
Chiedi conferma solo se necessario: "Per sicurezza intendi sabato 14 marzo, giusto?"
Non proseguire finché la data non è confermata.

**Passo 2 — PERSONE**
Se non già fornito: "Quante persone?"

**Passo 3 — SEDE**
Se non già fornita: "In quale sede preferisci?"

**Passo 4 — ⚠️ CONTROLLO DOPPIO TURNO — OBBLIGATORIO**
Appena sono noti sede + data + fascia (pranzo/cena), esegui i 3 passi in ordine **prima** di fare qualsiasi domanda sull'orario:

> *Verifica 1* — Il giorno è sabato o domenica?
> → Se NO: doppio turno non esiste. Vai al Passo 5.
> → Se SÌ: vai alla Verifica 2.
>
> *Verifica 2* — La combinazione sede + giorno + fascia è nella tabella doppio turno (vedi sotto)?
> → Se NO: doppio turno non esiste. Vai al Passo 5.
> → Se SÌ: vai alla Verifica 3.
>
> *Verifica 3* — Il cliente ha già indicato un orario?
> → Se NO: **NON chiedere "A che ora preferisci?"** — dì direttamente:
>   "Qui c'è doppio turno: primo [range], secondo [range]. Quale preferisci?"
>   Attendi la risposta. Assegna `orario_tool` = orario ufficiale di inizio turno. Vai al Passo 6.
> → Se SÌ: determina il turno dall'orario dichiarato (vedi Caso B sotto). Vai al Passo 6.

**⚠️ REGOLA CRITICA:** Il Passo 4 avviene SEMPRE prima del Passo 5. Se sede + data + fascia sono già noti dal primo messaggio del cliente, il controllo doppio turno si esegue **immediatamente** — senza aver fatto altre domande. Non è mai consentito chiedere "A che ora preferisci?" se il doppio turno si applica.

**Passo 5 — ORARIO**
Solo se non c'è doppio turno E il cliente non ha già indicato un orario: "A che ora preferisci?"
Se il cliente ha già indicato un orario: normalizzalo e usalo direttamente. Non chiedere nulla.

**Passo 6 — NOTE**
"Allergie o richieste per il tavolo?"

**Passo 7 — NOME E CELLULARE**
"Nome e cellulare?"

**Passo 8 — EMAIL**
"Vuoi ricevere la conferma della prenotazione per email?"

**Passo 9 — RIEPILOGO**
Una sola volta, formato fisso: "Riepilogo: [Sede] [giorno] [numero] [mese] alle [orario], [persone] persone. Nome: [nome]. Confermi?"

**Passo 10 — BOOK**
Solo dopo il "sì". Chiama `POST /book_table` con `fase=book` direttamente (nessun controllo disponibilità separato — `fase=book` lo gestisce internamente e restituisce `SOLD_OUT` se il turno è pieno).

**Passo 11 — CONFERMA**
"Perfetto. Prenotazione confermata: [...]. Controlla WhatsApp per la conferma."

> **Nota:** `fase=availability` è deprecato nel flusso agente. NON chiamarlo prima della prenotazione. `fase=book` controlla la disponibilità internamente e restituisce `SOLD_OUT` con alternative se il turno è pieno. Saltare la chiamata availability separata evita una sessione browser aggiuntiva (~30–50s) e timeout HTTP 504.

### Handling book_table Errors
The webhook response always includes a `status` field. Handle it as follows:

| `status` | Meaning | Giulia's action |
|----------|---------|----------------|
| `SOLD_OUT` | That time slot / sede is actually full | "Purtroppo il turno scelto è esaurito. Preferisci [alternativa concreta]?" — propose a specific alternative (other turn, +30 min, or other sede) |
| `TECH_ERROR` | Timeout or technical failure on the booking system | "C'è stato un problema tecnico. Riprovo subito." — retry the **same** `book_table` call once automatically, without re-asking any info |
| `ERROR` | Unexpected error from the booking site | "C'è stato un errore imprevisto. Posso riprovare tra un momento o puoi chiamarci al 06 56556 263." |

**Critical rules:**
- `TECH_ERROR` must NEVER be communicated to the customer as "disponibilità cambiata" or "posto esaurito" — it is a technical failure, not a lack of seats.
- After a `TECH_ERROR` retry, if it fails again, say: "Il sistema è temporaneamente non raggiungibile. Richiamaci tra qualche minuto oppure prenota su rione.fidy.app."
- When retrying after `TECH_ERROR`, reuse exactly the same parameters (sede, orario, turno, nome, telefono, email, note) — do NOT re-ask the customer anything.

### Double Turn Table (Doppio Turno)

Alcune combinazioni sede + giorno + pasto hanno due turni. Consultare questa tabella al Passo 4 del flusso di prenotazione.

| Sede | Giorno | Pasto | 1° Turno | 2° Turno |
|------|--------|-------|----------|----------|
| Talenti | Sabato | Pranzo | 12:00–13:15 | 13:30+ |
| Talenti | Domenica | Pranzo | 12:00–13:15 | 13:30+ |
| Talenti | Sabato | Cena | 19:00–20:45 | 21:00+ |
| Appia | Sabato | Pranzo | 12:00–13:20 | 13:30+ |
| Appia | Domenica | Pranzo | 12:00–13:20 | 13:30+ |
| Appia | Sabato | Cena | 19:30–21:15 | 21:30+ |
| Palermo | Sabato | Pranzo | 12:00–13:20 | 13:30+ |
| Palermo | Domenica | Pranzo | 12:00–13:20 | 13:30+ |
| Palermo | Sabato | Cena | 19:30–21:15 | 21:30+ |
| Reggio Calabria | Sabato | Cena | 19:30–21:15 | 21:30+ |
| Ostia Lido | — | — | Mai doppio turno | — |

**Regole doppio turno:**
- Applicare la logica doppio turno **solo** per i giorni/fasce elencati — NON menzionare turni per i giorni non in tabella (es. lunedì–venerdì, domenica sera, ecc.)
- **Caso A — cliente NON ha indicato orario:** NON chiedere "A che ora preferisci?" — presentare direttamente i turni: "Qui c'è doppio turno: primo [range], secondo [range]. Quale preferisci?" Usare l'orario ufficiale di inizio turno per il webhook.
- **Caso B — cliente HA già indicato un orario:** determinare il turno dall'orario dichiarato (es. "20:00" ad Appia sabato cena → 1° turno 19:30–21:15 → `orario_tool = "19:30"`). Non chiedere nulla sul turno.
- In doppio turno, inviare sempre al webhook l'orario ufficiale di inizio turno (non l'orario interno del cliente)
- Se >9 persone, NON chiamare il webhook — fornire il numero 06 56556 263

### Standard Time Slots
When there is no double turn:

| Lunch | Dinner |
|-------|--------|
| 12:00, 12:30, 13:00, 13:30, 14:00, 14:30 | 19:00, 19:30, 20:00, 20:30, 21:00, 21:30, 22:00, 22:30 |

### Meal Period Detection
- `sera`, `stasera`, `domani sera` → cena (never ask "pranzo o cena?")
- `pranzo`, `domani a pranzo` → pranzo
- Time 12:00–16:00 → pranzo
- Time 17:00–23:00 → cena

---

## Repository Structure

```
centralino-webhook/
├── main.py            # Entire application — API, DB, browser automation
├── requirements.txt   # Python dependencies (FastAPI, uvicorn, playwright)
├── Dockerfile         # Container build using Microsoft Playwright Python image
├── railway.json       # Railway.app deployment configuration
├── .gitignore         # Excludes __pycache__, .pyc, .sqlite3
└── CLAUDE.md          # This file — codebase documentation
```

There are no test files, no separate modules, and no other source code.

---

## Tech Stack

| Component | Library/Version | Purpose |
|-----------|----------------|---------|
| Web framework | FastAPI 0.110.0 | REST API server |
| ASGI server | uvicorn 0.27.1 | Runs FastAPI |
| Browser automation | playwright 1.41.0 | Headless Chromium for booking form interaction |
| HTTP client | httpx 0.27.0 | Async proxy calls to Fidy REST API |
| Database | sqlite3 (stdlib) | Persists bookings and customer profiles |
| Deployment | Railway.app | Cloud hosting |

---

## main.py Internal Structure

The file is organized into logical sections (not separate modules):

| Lines | Section | Description |
|-------|---------|-------------|
| 1–115 | Imports & Config | Environment variables, Italian locale constants, FastAPI app init |
| 118–245 | Database Layer | SQLite init, booking log, customer upsert/get |
| 248–331 | Normalization Utils | Time/date/meal/sede string normalization |
| 361–443 | Date Resolution | `/resolve_date` endpoint; Italian relative-date parser |
| 451–509 | Pydantic Model | `RichiestaPrenotazione` — booking request model with validation |
| 516–957 | Playwright Helpers | All browser automation functions (prefixed `_`) |
| 958–1408 | API Routes | Health check, admin dashboard, `/book_table` endpoint |
| 1409–1550 | Fidy API Proxy | `/check_reservation`, `/find_reservation_for_cancel`, `/cancel_reservation`, `/update_covers`, `/add_note` |

---

## API Endpoints

### `GET /`
Health check. Returns `{"status": "ok"}`.

### `POST /resolve_date`
Converts Italian relative date expressions to ISO dates.

Input:
```json
{ "input_text": "domani sera" }
```
Output:
```json
{ "date_iso": "2026-03-09", "weekday_spoken": "Lunedì", "day_number": 9, "month_spoken": "Marzo", "requires_confirmation": true, "matched_rule": "domani" }
```

### `GET /time_now`
Returns current time in the configured timezone.

### `POST /book_table`
Main booking endpoint. The recommended flow uses **only `fase=book`** — it checks availability internally and returns `SOLD_OUT` if the slot is full, eliminating a redundant browser session.

`fase=availability` is still supported for backwards compatibility but is no longer used by the agent.

**`"book"`** (recommended): Checks availability and completes the full booking in a single browser session.

**`"availability"`** (deprecated in agent flow): Returns list of sedi with availability without booking.

Request body (`RichiestaPrenotazione`):
```json
{
  "fase": "book",
  "nome": "Mario",
  "cognome": "Rossi",
  "email": "mario@example.com",
  "telefono": "3331234567",
  "sede": "Talenti",
  "data": "2026-03-10",
  "orario": "20:30",
  "persone": 2,
  "seggiolini": 0,
  "note": ""
}
```

### `GET /_admin/dashboard`
Admin dashboard showing booking stats and customer history. Requires `Authorization: Bearer <ADMIN_TOKEN>` header.

### `GET /_admin/customer/{phone}`
Returns stored customer profile for a given phone number. Requires admin token.

---

## Fidy API Proxy Endpoints

These endpoints proxy requests to the Fidy REST API (`api.fidy.app`) with authentication. They handle operations on **existing** reservations (no Playwright involved).

All Fidy proxy endpoints accept `restaurant_id` as either a numeric ID or a sede name string (auto-converted via `SEDE_ID_MAP`).

**Sede ID mapping:** Talenti=1, Appia=2, Ostia Lido=3, Reggio Calabria=4, Palermo=5

### `GET /check_reservation`
Verifies if a reservation exists for the given sede, date, time, and phone.

Query params: `restaurant_id`, `date` (YYYY-MM-DD), `time` (HH:MM), `phone`

### `POST /find_reservation_for_cancel`
Searches for a reservation to cancel. Returns the associated phone number for confirmation before cancellation.

```json
{
  "reservation_code": "ABC123",
  "restaurant_id": 1,
  "date": "2026-03-14",
  "time": "20:00",
  "phone": "3331234567",
  "first_name": "Mario",
  "last_name": "Rossi"
}
```
All fields optional — provide as many as available. `reservation_code` takes priority.

### `POST /cancel_reservation`
Cancels an existing reservation. **Only `phone` and `date` are required** — `time` and `restaurant_id` are optional and sent only if available.

```json
{ "phone": "3331234567", "date": "2026-03-14" }
```
With optional fields:
```json
{ "phone": "3331234567", "date": "2026-03-14", "restaurant_id": 1, "time": "20:00", "note": "annullato dal cliente" }
```

**Giulia's cancellation flow (simplified):**
1. Ask for `telefono` (if not already given)
2. Ask for `data` (if not already given) — convert to YYYY-MM-DD internally; **do NOT call `resolve_date`**
3. Call `POST /cancel_reservation` directly with phone + date (+ sede/time if mentioned by customer)
4. **Do NOT call `find_reservation_for_cancel` first** — it is unreliable and unnecessary
5. **Do NOT ask for sede or time** — they are optional and only sent if the customer already mentioned them
6. **Do NOT say the year** when repeating the date back to the customer — say "il 10 marzo" not "il 10 marzo 2026"
7. **Do NOT call `resolve_date`** — past dates are valid for cancellations but `resolve_date` would advance them to the next year. Convert manually (e.g., "primo marzo" → "2026-03-01", "1 marzo" → "2026-03-01")

> **Note on date conversion for cancellations:** For cancellations, Giulia must convert the date herself (e.g., "primo marzo" → "2026-03-01") without calling `resolve_date`. Past dates are valid for cancellations; `resolve_date` would wrongly move them to the following year.

### `POST /update_covers`
Updates the party size of an existing reservation. If response contains `requires_rebooking: true`, the reservation must be cancelled and re-created.

```json
{ "restaurant_id": 1, "date": "2026-03-14", "time": "20:00", "phone": "3331234567", "new_covers": 4 }
```

### `POST /add_note`
Adds a note (allergy, special request, etc.) to an existing reservation.

```json
{ "restaurant_id": 1, "date": "2026-03-14", "time": "20:00", "phone": "3331234567", "note": "allergia al glutine" }
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BOOKING_URL` | `rione.fidy.app` | Target booking website hostname |
| `PW_TIMEOUT_MS` | `60000` | General Playwright timeout (ms) |
| `PW_NAV_TIMEOUT_MS` | `60000` | Page navigation timeout (ms) |
| `DISABLE_FINAL_SUBMIT` | `false` | If `true`, skips actual booking submission (test mode) |
| `DEBUG_ECHO_PAYLOAD` | `false` | Log incoming request payload |
| `DEBUG_LOG_AJAX_POST` | `false` | Log outgoing AJAX booking request/response |
| `ADMIN_TOKEN` | `""` | Bearer token required to access admin endpoints |
| `DATA_DIR` | `/tmp` | Directory where SQLite database is stored |
| `MAX_SLOT_RETRIES` | `2` | Max retries if selected time slot is full |
| `MAX_SUBMIT_RETRIES` | `1` | Max retries on final booking submission |
| `RETRY_TIME_WINDOW_MIN` | `90` | Window (minutes) for searching alternative time slots |
| `DEFAULT_EMAIL` | `default@prenotazioni.com` | Fallback email if none provided |
| `PW_CHROMIUM_EXECUTABLE` | `""` (auto-detect) | Custom path to Chromium binary for Playwright |
| `FIDY_API_BASE` | `https://api.fidy.app/api` | Base URL for Fidy REST API |
| `FIDY_API_KEY` | `derione_api_2026_super_secret` | API key sent as `X-API-Key` header to Fidy |
| `FIDY_TIMEOUT_S` | `20` | Timeout in seconds for Fidy API calls |

---

## Database Schema

SQLite database stored at `{DATA_DIR}/centralino.sqlite3`.

### `bookings` table
```sql
CREATE TABLE bookings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,          -- ISO timestamp
  nome TEXT,
  cognome TEXT,
  telefono TEXT,
  sede TEXT,
  data TEXT,
  orario TEXT,
  persone INTEGER,
  status TEXT,      -- "success" | "failed" | "timeout"
  detail TEXT       -- JSON with extra info
);
```

### `customers` table
```sql
CREATE TABLE customers (
  telefono TEXT PRIMARY KEY,
  nome TEXT,
  cognome TEXT,
  email TEXT,
  last_seen TEXT    -- ISO timestamp
);
```

---

## Playwright Automation Flow

### Availability Phase
1. Launch headless Chromium (blocking heavy assets like images/fonts/styles)
2. Navigate to `BOOKING_URL`
3. Dismiss cookie/consent banners (`_maybe_click_cookie`)
4. Wait for `.nCoperti` selector to confirm page is ready
5. Set party size (`_click_persone`) and highchairs (`_set_seggiolini`)
6. Set date (`_set_date`) and meal period (`_click_pasto`)
7. Scrape all sede availability data (`_scrape_sedi_availability`)
8. Return list to caller

### Booking Phase
9. Click selected sede (`_click_sede`)
10. Select meal turn if needed (`_maybe_select_turn`)
11. Select time slot with fallback to closest available (`_select_orario_or_retry`)
12. Fill notes in step 5 (`_fill_note_step5`)
13. Fill personal data form (`_fill_form`)
14. Submit booking and wait for AJAX response (`_wait_ajax_final`)
15. Parse confirmation and return result

---

## Sede (Location) Mapping

The booking system supports these restaurant locations:

| Internal Name | Aliases |
|--------------|---------|
| Talenti | talenti, roma talenti, rione talenti |
| Ostia Lido | ostia, lido, ostia lido |
| Appia | appia, roma appia, rione appia |
| Palermo | palermo |
| Reggio Calabria | reggio, reggio calabria, rc |

Normalization is handled by `_norm_sede()` and matching by `_click_sede()`.

---

## Date & Time Conventions

- All dates are in `YYYY-MM-DD` ISO format.
- All times are `HH:MM` (24-hour).
- Timezone: `Europe/Rome` (falls back to `CET` if `zoneinfo` unavailable).
- The Italian date parser (`/resolve_date`) handles: `oggi`, `domani`, `dopodomani`, `stasera`, `stanotte`, weekday names, `questo weekend`, `prossimo [weekday]`, etc.
- Meal periods: `pranzo` (lunch, before 15:30) / `cena` (dinner, from 17:00 onward) are inferred from `orario` by `_calcola_pasto()`.

---

## Deployment

### Docker
Built from `mcr.microsoft.com/playwright/python:v1.41.0-jammy`. Playwright Chromium and its system dependencies are pre-installed in the base image.

```bash
docker build -t centralino-webhook .
docker run -p 8080:8080 -e ADMIN_TOKEN=secret centralino-webhook
```

### Railway.app
Configured via `railway.json`. Uses NIXPACKS builder with explicit build commands to install Python deps and Playwright Chromium browser.

The app binds to `$PORT` (default 8080) with a single uvicorn worker.

---

## Development Conventions

### Code Style
- All internal helper functions are prefixed with `_` (e.g., `_norm_sede`, `_click_pasto`).
- API route handlers are defined at module level without prefix.
- Pydantic models use `model_validator` and `field_validator` for coercion.
- Italian strings and UI labels are kept as-is (no translation layer).

### Error Handling
- Playwright steps raise exceptions on timeout; these are caught at the route level.
- On error, a screenshot is saved to disk with a timestamped filename.
- All booking attempts (success and failure) are logged to the SQLite database.
- HTTP response codes: `200` (success), `422` (validation error), `500` (booking failure).

### Adding New Functionality
- All new code belongs in `main.py` (the project intentionally uses a single-file structure).
- New helper functions should be prefixed with `_` and placed in the relevant section.
- New API routes should be added at the bottom of the file near the existing routes.
- Environment variables should be read at module level with a sensible default.

### Testing
There are currently no automated tests. When testing manually:
- Set `DISABLE_FINAL_SUBMIT=true` to prevent actual bookings during development.
- Set `DEBUG_ECHO_PAYLOAD=true` and `DEBUG_LOG_AJAX_POST=true` for verbose logging.
- Use the `/resolve_date` and `/time_now` endpoints to test date logic without browser automation.

---

## Known Constraints

- **Single-file architecture**: All logic lives in `main.py`. Do not split into multiple files without explicit instruction.
- **Single worker**: The Railway deployment runs one uvicorn worker. Playwright is not thread-safe; concurrent booking requests may interfere.
- **No tests**: No testing framework is set up. Avoid breaking existing behavior without manual verification.
- **Italian-only UI**: The booking website is in Italian. All field names, labels, and parsing logic assume Italian text.
- **Ephemeral storage**: The SQLite database is stored in `/tmp` by default and will not persist across Railway deployments unless `DATA_DIR` is set to a persistent volume.
- **Fidy API reachability**: The 5 proxy endpoints (`/check_reservation`, `/cancel_reservation`, etc.) require `api.fidy.app` to be reachable from Railway. They were not testable from the local dev sandbox (no internet). Verify connectivity in production.
- **API key in config**: `FIDY_API_KEY` defaults to the hardcoded key from the ElevenLabs tool definitions. Set it via environment variable in Railway to avoid key exposure in source code.
