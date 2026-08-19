"""TWMC 後端：帳號登入＋每人一份卡片／持倉／筆記／主力資料。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "twmc.db"
JWT_SECRET = os.environ.get("JWT_SECRET") or "twmc-dev-change-me"
JWT_DAYS = int(os.environ.get("JWT_DAYS") or "30")
ALLOW_REGISTER = str(os.environ.get("ALLOW_REGISTER") or "true").lower() in {
    "1",
    "true",
    "yes",
}

app = FastAPI(title="TWMC API", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode((text + pad).encode("ascii"))


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return f"{salt.hex()}:{dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(dk_hex)
        got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
        return hmac.compare_digest(got, expected)
    except Exception:
        return False


def make_token(user_id: str, email: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "exp": int(time.time()) + JWT_DAYS * 86400,
    }
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(JWT_SECRET.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url(sig)}"


def read_token(token: str) -> dict:
    try:
        body, sig = token.split(".", 1)
        expect = hmac.new(JWT_SECRET.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expect, _b64url_decode(sig)):
            raise ValueError("bad sig")
        payload = json.loads(_b64url_decode(body))
        if int(payload.get("exp") or 0) < int(time.time()):
            raise ValueError("expired")
        return payload
    except Exception as exc:
        raise HTTPException(status_code=401, detail="登入已失效，請重新登入") from exc


@contextmanager
def db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS blobs (
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                code TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, kind, code)
            )
            """
        )


init_db()


class AuthIn(BaseModel):
    email: str
    password: str = Field(min_length=6, max_length=128)


class BlobIn(BaseModel):
    payload: dict


def current_user(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="請先登入")
    payload = read_token(authorization.split(" ", 1)[1].strip())
    with db() as conn:
        row = conn.execute(
            "SELECT id, email FROM users WHERE id = ?",
            (payload["sub"],),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="帳號不存在")
    return {"id": row["id"], "email": row["email"]}


@app.get("/")
def root():
    return {
        "ok": True,
        "service": "TWMC API",
        "health": "/health",
        "docs": "/docs",
        "login": "POST /auth/login",
        "register": "POST /auth/register",
    }


@app.get("/health")
def health():
    return {"ok": True, "service": "twmc"}


@app.post("/auth/register")
def register(body: AuthIn):
    if not ALLOW_REGISTER:
        raise HTTPException(status_code=403, detail="目前未開放註冊")
    email = str(body.email).strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status_code=422, detail="請輸入有效信箱")
    user_id = str(uuid.uuid4())
    with db() as conn:
        exists = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail="這個信箱已經註冊")
        conn.execute(
            "INSERT INTO users (id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (user_id, email, hash_password(body.password), int(time.time())),
        )
    token = make_token(user_id, email)
    return {"access_token": token, "token_type": "bearer", "user": {"id": user_id, "email": email}}


@app.post("/auth/login")
def login(body: AuthIn):
    email = str(body.email).strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=422, detail="請輸入有效信箱")
    with db() as conn:
        row = conn.execute(
            "SELECT id, email, password_hash FROM users WHERE email = ?",
            (email,),
        ).fetchone()
    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="信箱或密碼不正確")
    token = make_token(row["id"], row["email"])
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {"id": row["id"], "email": row["email"]},
    }


@app.get("/auth/me")
def me(user: dict = Depends(current_user)):
    return user


def _get_blob(user_id: str, kind: str, code: str = ""):
    with db() as conn:
        row = conn.execute(
            "SELECT payload, updated_at FROM blobs WHERE user_id = ? AND kind = ? AND code = ?",
            (user_id, kind, code),
        ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["payload"])
    except json.JSONDecodeError:
        payload = None
    return {"payload": payload, "updated_at": row["updated_at"]}


def _put_blob(user_id: str, kind: str, payload: dict, code: str = ""):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO blobs (user_id, kind, code, payload, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, kind, code) DO UPDATE SET
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            (user_id, kind, code, json.dumps(payload, ensure_ascii=False), int(time.time())),
        )
    return {"ok": True}


@app.get("/data/{kind}")
def get_data(kind: str, user: dict = Depends(current_user)):
    if kind not in {"watchlist", "portfolio", "notes"}:
        raise HTTPException(status_code=404, detail="未知資料類型")
    found = _get_blob(user["id"], kind)
    if not found:
        return {"payload": None}
    return found


@app.put("/data/{kind}")
def put_data(kind: str, body: BlobIn, user: dict = Depends(current_user)):
    if kind not in {"watchlist", "portfolio", "notes"}:
        raise HTTPException(status_code=404, detail="未知資料類型")
    return _put_blob(user["id"], kind, body.payload)


@app.get("/data/mainforce/{code}")
def get_mainforce(code: str, user: dict = Depends(current_user)):
    found = _get_blob(user["id"], "mainforce", str(code).strip().upper())
    if not found:
        return {"payload": None}
    return found


@app.put("/data/mainforce/{code}")
def put_mainforce(code: str, body: BlobIn, user: dict = Depends(current_user)):
    return _put_blob(user["id"], "mainforce", body.payload, str(code).strip().upper())
