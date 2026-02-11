from fastapi import FastAPI, Request

app = FastAPI()

@app.get("/")
def home():
    return {"ok": True, "version": "TEST-RAW"}

@app.post("/book_table")
async def book_table(request: Request):
    body = await request.body()
    return {
        "ok": True,
        "len": len(body),
        "content_type": request.headers.get("content-type"),
        "raw": body.decode("utf-8", errors="ignore")[:2000],
    }
