import os, asyncio
from fastapi import FastAPI, Header, HTTPException
from sync import sync_all

app = FastAPI()

@app.get("/health")
def health(): return {"status": "ok"}

@app.post("/admin/sync")
def admin_sync(x_auth_token: str | None = Header(default=None)):
    if x_auth_token != os.getenv("SYNC_AUTH_TOKEN"):
        raise HTTPException(status_code=401, detail="Unauthorized")
    asyncio.run(sync_all())
    return {"ok": True}
