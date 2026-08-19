"""Streamlit 連 TWMC FastAPI 後端。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
# 暫時拔掉 Streamlit ↔ FastAPI，卡片／持倉改走本機 JSON，不必登入。
STREAMLIT_API_ENABLED = False


def _secret(key, default=""):
    val = str(os.environ.get(key) or "").strip()
    if val:
        return val
    try:
        import streamlit as st
        return str(st.secrets.get(key, default) or default).strip()
    except Exception:
        return str(default or "").strip()


def base_url():
    return _secret("API_BASE_URL").rstrip("/")


def is_loopback():
    url = (base_url() or "").lower()
    return "127.0.0.1" in url or "localhost" in url or "0.0.0.0" in url


def enabled():
    return STREAMLIT_API_ENABLED and bool(base_url())


def _token():
    try:
        import streamlit as st
        return str(st.session_state.get("api_token") or "").strip()
    except Exception:
        return ""


def logged_in():
    return enabled() and bool(_token())


def _headers():
    token = _token()
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _url(path):
    return f"{base_url()}{path}"


def health():
    try:
        res = requests.get(_url("/health"), timeout=6)
        return res.ok
    except Exception:
        return False


def _raise_detail(res):
    try:
        detail = res.json().get("detail")
    except Exception:
        detail = res.text
    if isinstance(detail, list):
        detail = "；".join(str(x) for x in detail)
    raise RuntimeError(str(detail or f"HTTP {res.status_code}"))


def login(username, password):
    res = requests.post(
        _url("/auth/login"),
        json={"username": username, "password": password},
        timeout=12,
    )
    if not res.ok:
        _raise_detail(res)
    return res.json()


def register(username, password):
    res = requests.post(
        _url("/auth/register"),
        json={"username": username, "password": password},
        timeout=12,
    )
    if not res.ok:
        _raise_detail(res)
    return res.json()


def get_blob(kind):
    if not logged_in():
        return None
    res = requests.get(_url(f"/data/{kind}"), headers=_headers(), timeout=12)
    if res.status_code == 401:
        return None
    if not res.ok:
        return None
    payload = (res.json() or {}).get("payload")
    return payload if isinstance(payload, dict) else None


def put_blob(kind, payload):
    if not logged_in() or not isinstance(payload, dict):
        return False
    try:
        res = requests.put(
            _url(f"/data/{kind}"),
            headers=_headers(),
            json={"payload": payload},
            timeout=12,
        )
        return res.ok
    except Exception:
        return False


def get_mainforce(code):
    if not logged_in():
        return None
    code = str(code or "").strip().upper()
    if not code:
        return None
    try:
        res = requests.get(
            _url(f"/data/mainforce/{code}"),
            headers=_headers(),
            timeout=12,
        )
        if not res.ok:
            return None
        payload = (res.json() or {}).get("payload")
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def put_mainforce(code, payload):
    if not logged_in() or not isinstance(payload, dict):
        return False
    code = str(code or "").strip().upper()
    if not code:
        return False
    try:
        res = requests.put(
            _url(f"/data/mainforce/{code}"),
            headers=_headers(),
            json={"payload": payload},
            timeout=12,
        )
        return res.ok
    except Exception:
        return False


def _read_json(path):
    path = Path(path)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def seed_from_local_if_empty():
    """第一次登入：後端沒資料就把本機 JSON 上傳。"""
    if not logged_in():
        return
    mapping = {
        "watchlist": ROOT / "watchlist.json",
        "portfolio": ROOT / "portfolio.json",
        "notes": ROOT / "investment_notes.json",
    }
    for kind, path in mapping.items():
        if get_blob(kind):
            continue
        local = _read_json(path)
        if local:
            put_blob(kind, local)
    mf_dir = ROOT / "mainforce_history"
    if mf_dir.exists():
        for path in mf_dir.glob("*.json"):
            code = path.stem.upper()
            if get_mainforce(code):
                continue
            local = _read_json(path)
            if local:
                put_mainforce(code, local)
