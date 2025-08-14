# main.py (root for now)
import os
from fastapi import FastAPI, Header, HTTPException

app = FastAPI()

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/admin/sync")
def admin_sync(x_auth_token: str | None = Header(default=None)):
    if x_auth_token != os.getenv("SYNC_AUTH_TOKEN"):
        raise HTTPException(status_code=401, detail="Unauthorized")

    # TODO: call your sync chain here, e.g.:
    # sync_players(); sync_gameweek_status(); sync_gw_stats(); ...
    # rebuild_standings(); evaluate_rules();

    return {"ok": True}
