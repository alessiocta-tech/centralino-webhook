import os
import re
import asyncio
import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from pydantic import BaseModel, EmailStr, Field, validator

import phonenumbers
from phonenumbers.phonenumberutil import NumberParseException

from playwright.async_api import async_playwright


# --------------------------------------------------
# LOGGING
# --------------------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("uvicorn.error")


# --------------------------------------------------
# FASTAPI
# --------------------------------------------------

app = FastAPI(title="Centralino deRione AI")


# --------------------------------------------------
# MODELLI
# --------------------------------------------------

class BookingRequest(BaseModel):

    nome: str
    cognome: str
    email: EmailStr
    telefono: str

    persone: int = Field(ge=1, le=9)

    sede: str
    data: str
    ora: str

    # Campi extra NON bloccanti
    seggiolone: bool = False
    seggiolini: int = 0
    nota: str = ""
    referer: str = "AI"
    dry_run: bool = False


    # -------------------------
    # VALIDATORI
    # -------------------------

    @validator("telefono")
    def validate_phone(cls, v):

        try:
            phone = phonenumbers.parse(v, "IT")

            if not phonenumbers.is_valid_number(phone):
                raise ValueError()

            return phonenumbers.format_number(
                phone,
                phonenumbers.PhoneNumberFormat.E164
            )

        except (NumberParseException, ValueError):

            raise ValueError("Numero di telefono non valido")


    @validator("sede")
    def normalize_sede(cls, v):

        v = v.lower().strip()

        sedi = {
            "talenti": "Talenti",
            "ostia": "Ostia",
            "reggio": "Reggio Calabria",
            "reggio calabria": "Reggio Calabria"
        }

        return sedi.get(v, v.title())


    @validator("ora")
    def normalize_ora(cls, v):

        if re.match(r"^\d{1,2}$", v):
            return f"{int(v):02d}:00"

        if re.match(r"^\d{1,2}:\d{2}$", v):
            return v

        raise ValueError("Formato ora non valido")



# --------------------------------------------------
# ERROR HANDLER 422 (DEBUG PRODUZIONE)
# --------------------------------------------------

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError
):

    body = await request.body()

    logger.error("❌ 422 VALIDATION ERROR")
    logger.error("BODY: %s", body.decode("utf-8", errors="ignore"))
    logger.error("DETAILS: %s", exc.errors())

    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors()}
    )


# --------------------------------------------------
# HEALTH CHECK
# --------------------------------------------------

@app.get("/")
async def health():

    return {
        "status": "online",
        "service": "centralino-derione"
    }



# --------------------------------------------------
# PLAYWRIGHT BOT
# --------------------------------------------------

async def submit_booking(data: BookingRequest):

    url = "https://rione.fidy.app/prenew.php?referer=AI"


    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox"]
        )

        context = await browser.new_context()
        page = await context.new_page()

        try:

            await page.goto(url, timeout=30000)

            await page.fill("#nome", data.nome)
            await page.fill("#cognome", data.cognome)
            await page.fill("#email", data.email)
            await page.fill("#telefono", data.telefono)

            await page.fill("#persone", str(data.persone))
            await page.fill("#data", data.data)
            await page.fill("#ora", data.ora)

            await page.select_option("#sede", label=data.sede)

            if data.nota:
                await page.fill("#note", data.nota)

            await page.click("#submit")

            await page.wait_for_timeout(3000)

            return True


        except Exception as e:

            logger.error("❌ PLAYWRIGHT ERROR: %s", str(e))

            return False


        finally:

            await browser.close()



# --------------------------------------------------
# API ENDPOINT
# --------------------------------------------------

@app.post("/book_table")
async def book_table(req: BookingRequest):

    logger.info("📥 Nuova prenotazione: %s %s", req.nome, req.cognome)

    # DRY RUN (TEST)
    if req.dry_run:

        return {
            "status": "ok",
            "message": "Test completato",
            "data": req.dict()
        }


    # Retry automatico (3 tentativi)
    for attempt in range(1, 4):

        logger.info("🔁 Tentativo %s/3", attempt)

        success = await submit_booking(req)

        if success:

            logger.info("✅ Prenotazione completata")

            return {
                "status": "ok",
                "message": "Prenotazione confermata"
            }

        await asyncio.sleep(2)


    logger.error("❌ Prenotazione fallita dopo 3 tentativi")

    raise HTTPException(
        status_code=500,
        detail="Errore temporaneo nella prenotazione"
    )



# --------------------------------------------------
# AVVIO LOCALE
# --------------------------------------------------

if __name__ == "__main__":

    import uvicorn

    port = int(os.environ.get("PORT", 8080))

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        workers=1
    )
