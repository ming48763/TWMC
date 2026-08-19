"""Streamlit 連 TWMC FastAPI 後端。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent


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
    return bool(base_url())


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


def health_payload():
    """回傳 /health JSON；連不上則 None。單次探測，避免登入頁卡住。"""
    try:
        res = requests.get(_url("/health"), timeout=10)
        if not res.ok:
            return None
        data = res.json()
        return data if isinstance(data, dict) else {"ok": True}
    except Exception:
        return None


def health():
    payload = health_payload()
    return bool(payload and payload.get("ok"))


def _raise_detail(res):
    try:
        detail = res.json().get("detail")
    except Exception:
        detail = res.text
    if isinstance(detail, list):
        detail = "；".join(str(x) for x in detail)
    raise RuntimeError(str(detail or f"HTTP {res.status_code}"))


def _post_auth(path, username, password):
    payload = {
        "username": str(username or "").strip(),
        "password": str(password or "").strip(),
    }
    last_exc = None
    for attempt in range(3):
        try:
            res = requests.post(_url(path), json=payload, timeout=30)
            if res.status_code in {502, 503, 504} and attempt < 2:
                time.sleep(4)
                continue
            if not res.ok:
                _raise_detail(res)
            return res.json()
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(4)
                continue
            raise RuntimeError(
                "連不到後端（Render 免費版休眠後第一次可能要等約 1 分鐘）。請再試一次。"
            ) from last_exc
    raise RuntimeError("連不到後端，請稍後再試。")


def login(username, password):
    return _post_auth("/auth/login", username, password)


def register(username, password):
    return _post_auth("/auth/register", username, password)


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
