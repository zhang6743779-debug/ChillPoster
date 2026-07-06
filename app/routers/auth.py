# app/routers/auth.py
import uuid
import json
import os
from fastapi import APIRouter, HTTPException
from app.schemas import LoginRequest, ChangeAuthRequest
from core.configs import AUTH_FILE

router = APIRouter(tags=["Auth"])

def _default_auth_creds() -> dict:
    return {
        "username": os.getenv("CHILLPOSTER_ADMIN_USERNAME", "admin") or "admin",
        "password": os.getenv("CHILLPOSTER_ADMIN_PASSWORD", "password") or "password",
    }


def _ensure_auth_secret(creds: dict) -> dict:
    if not creds.get("secret"):
        creds["secret"] = uuid.uuid4().hex
        try:
            with open(AUTH_FILE, "w", encoding="utf-8") as f:
                json.dump(creds, f, ensure_ascii=False)
        except:
            pass
    return creds


def get_auth_creds():
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE, "r", encoding="utf-8") as f:
                return _ensure_auth_secret(json.load(f))
        except: pass
    return _ensure_auth_secret(_default_auth_creds())

@router.post("/api/login")
def login(req: LoginRequest):
    creds = get_auth_creds()
    if req.username == creds.get("username", "admin") and req.password == creds.get("password", "password"):
        return {"status": "ok", "token": str(uuid.uuid4()), "username": req.username}
    raise HTTPException(status_code=401, detail="Error")

@router.post("/api/change_auth")
def change_auth(req: ChangeAuthRequest):
    creds = get_auth_creds()
    if req.old_password != creds.get("password", "password"): 
        raise HTTPException(status_code=401, detail="Old password incorrect")
    try:
        with open(AUTH_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "username": req.new_username,
                "password": req.new_password,
                "secret": creds.get("secret") or uuid.uuid4().hex,
            }, f, ensure_ascii=False)
        return {"status": "ok"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/user_info")
def get_user_info():
    creds = get_auth_creds()
    return {"username": creds.get("username", "admin")}
