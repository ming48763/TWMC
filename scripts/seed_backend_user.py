"""把本機卡片／持倉／筆記／主力上傳到已登入的 TWMC 後端。"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
BASE = (os.environ.get("API_BASE_URL") or "https://twmc-backend.onrender.com").rstrip("/")


def load_json(path: Path):
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else None


def wait_health():
    last = None
    for _ in range(12):
        try:
            res = requests.get(f"{BASE}/health", timeout=20)
            if res.ok:
                return
            last = res.text
        except Exception as exc:
            last = str(exc)
        time.sleep(5)
    raise SystemExit(f"後端還沒醒：{BASE} ({last})")


def main():
    username = (os.environ.get("SEED_USER") or os.environ.get("SEED_EMAIL") or "").strip()
    password = os.environ.get("SEED_PASSWORD") or ""
    if not username or not password:
        raise SystemExit("請設定環境變數 SEED_USER、SEED_PASSWORD")

    wait_health()
    auth = None
    payload = {"username": username, "password": password}
    reg = requests.post(
        f"{BASE}/auth/register",
        json=payload,
        timeout=30,
    )
    if reg.status_code == 200:
        auth = reg.json()
        print("已註冊新帳號")
    elif reg.status_code == 409:
        login = requests.post(
            f"{BASE}/auth/login",
            json=payload,
            timeout=30,
        )
        if not login.ok:
            raise SystemExit("帳號已存在，但密碼不對，無法覆蓋上傳。")
        auth = login.json()
        print("帳號已存在，改為登入後上傳")
    else:
        raise SystemExit(reg.text)

    token = auth["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    mapping = {
        "watchlist": ROOT / "watchlist.json",
        "portfolio": ROOT / "portfolio.json",
        "notes": ROOT / "investment_notes.json",
    }
    for kind, path in mapping.items():
        payload = load_json(path)
        if not payload:
            print(f"略過 {kind}（本機沒有檔案）")
            continue
        res = requests.put(
            f"{BASE}/data/{kind}",
            headers=headers,
            json={"payload": payload},
            timeout=30,
        )
        print(f"上傳 {kind}: {res.status_code}")
        if not res.ok:
            print(res.text)

    mf_dir = ROOT / "mainforce_history"
    if mf_dir.exists():
        for path in mf_dir.glob("*.json"):
            payload = load_json(path)
            if not payload:
                continue
            code = path.stem.upper()
            res = requests.put(
                f"{BASE}/data/mainforce/{code}",
                headers=headers,
                json={"payload": payload},
                timeout=30,
            )
            print(f"上傳主力 {code}: {res.status_code}")


if __name__ == "__main__":
    sys.exit(main() or 0)
