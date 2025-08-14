import os
from fastapi import FastAPI, Header, HTTPException
from sqlmodel import SQLModel, create_engine

app = FastAPI()

DATABASE_URL = os.getenv("DATABASE_URL", "")
engine = create_engine(DATABASE_URL, echo=False) if DATABASE_URL else None

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/db-check")
def db_check():
    if not engine:
        return {"status": "no DATABASE_URL set"}
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1;")
        return {"status": "Database connected"}
    except Exception as e:
        return {"status": "Error", "details": str(e)}

@app.post("/admin/sync")
def admin_sync(x_auth_token: str | None = Header(default=None)):
    if x_auth_token != os.getenv("SYNC_AUTH_TOKEN"):
        raise HTTPException(status_code=401, detail="Unauthorized")

    # TODO: add your sync pipeline steps here (players, rosters, stats...)
    # Example placeholder:
    print("Sync started…")
    print("Sync finished.")
    return {"ok": True}
