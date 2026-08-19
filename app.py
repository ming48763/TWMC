import streamlit as st
import streamlit.components.v1 as components
from streamlit_echarts import st_echarts
import pandas as pd
import requests
import json
import os
from datetime import datetime, timedelta, date, timezone
import html
import re
from xml.etree import ElementTree as ET
from urllib.parse import quote
from pathlib import Path
import hmac
import importlib
import api_client as api
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import portfolio as pf
pf = importlib.reload(pf)

# 報價連線重複使用，減少每次握手延遲（Streamlit Cloud 在海外時特別明顯）
_HTTP = requests.Session()
_HTTP.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json,text/plain,*/*",
})
_HTTP.mount(
    "https://",
    HTTPAdapter(
        pool_connections=8,
        pool_maxsize=8,
        max_retries=Retry(total=1, backoff_factor=0.15, status_forcelist=(502, 503, 504)),
    ),
)

# 台北時間（固定 UTC+8，免依賴 tzdata）
TPE_TZ = timezone(timedelta(hours=8))

# 台股代號：2330、0050、006208、00995A（主動式 ETF 等）
STOCK_CODE_RE = re.compile(r"^[0-9]{3,6}[A-Za-z]{0,2}$")


def is_stock_code(text):
    return bool(STOCK_CODE_RE.fullmatch((text or "").strip()))

# 設定網頁標題與排版
st.set_page_config(
    page_title="台股分析工具 (TWMC)",
    layout="wide",
    initial_sidebar_state="collapsed"
)


def _secret_text(*keys):
    for key in keys:
        val = str(os.environ.get(key) or "").strip()
        if val:
            return val
        try:
            val = str(st.secrets.get(key, "") or "").strip()
        except Exception:
            val = ""
        if val:
            return val
    return ""


def _is_streamlit_cloud():
    return Path("/mount/src").exists() or bool(
        os.environ.get("STREAMLIT_RUNTIME") == "cloud"
        or os.environ.get("STREAMLIT_SHARING_MODE")
    )


def _finish_api_login(result):
    st.session_state.api_token = result.get("access_token") or ""
    st.session_state.api_user = result.get("user") or {}
    try:
        api.seed_from_local_if_empty()
    except Exception:
        pass
    st.rerun()


_APP_PASSWORD = _secret_text("APP_PASSWORD")
if api.enabled():
    if not api.logged_in():
        st.markdown(
            """
            <style>
            .twmc-login-h { text-align:center; font-size:2rem; font-weight:800; margin: 1.5rem 0 0.3rem; }
            .twmc-login-s { text-align:center; color:#bdbdbd; margin-bottom:1.2rem; }
            </style>
            <div class="twmc-login-h">TWMC</div>
            <div class="twmc-login-s">登入後端帳號後，卡片與持倉只會是你的</div>
            """,
            unsafe_allow_html=True,
        )
        if api.is_loopback() and _is_streamlit_cloud():
            st.error(
                "雲端 App 無法連你電腦上的 http://127.0.0.1:8000。"
                "請把後端部署到 Railway／Render，再把 Secrets 的 API_BASE_URL 改成公開網址。"
                "若要在本機登入，請開 http://localhost:8501 而不是 .streamlit.app。"
            )
        else:
            info = api.health_payload()
            if not info:
                st.error(
                    f"連不到後端：{api.base_url()}。"
                    "Render 免費版休眠後第一次連線可能要等約 1 分鐘，請重新整理再試。"
                )
            elif not info.get("db_persistent") and not info.get("users"):
                st.warning(
                    "這個後端目前沒有任何帳號。本機註冊的帳號不會出現在 Render；"
                    "服務休眠或重新部署也會清空 SQLite。請先用「註冊」建立帳號。"
                )
            elif not info.get("users"):
                st.warning("這個後端目前沒有帳號，請先註冊。")
        mode = st.radio("動作", ["登入", "註冊"], horizontal=True, label_visibility="collapsed")
        st.caption("帳號請用英文、數字或底線（3～32 字）。另一台裝置必須連同一個後端網址。")
        with st.form("twmc_api_login"):
            email = st.text_input("帳號")
            password = st.text_input("密碼", type="password")
            confirm = ""
            if mode == "註冊":
                confirm = st.text_input("再輸入一次密碼", type="password")
            submitted = st.form_submit_button(mode, type="primary", use_container_width=True)
        if submitted:
            try:
                if not email or not password:
                    st.error("請輸入帳號與密碼")
                elif mode == "註冊" and password != confirm:
                    st.error("兩次密碼不一致")
                elif mode == "註冊":
                    _finish_api_login(api.register(email.strip(), password.strip()))
                else:
                    _finish_api_login(api.login(email.strip(), password.strip()))
            except Exception as exc:
                st.error(str(exc))
        st.stop()
elif _is_streamlit_cloud() and not _APP_PASSWORD:
    st.error("雲端請設定 API_BASE_URL（後端登入）或 APP_PASSWORD（單一共用密碼）。")
    st.code(
        'API_BASE_URL = "https://你的後端網址"\nAPP_PASSWORD = "暫時密碼"',
        language="toml",
    )
    st.stop()
elif _APP_PASSWORD:
    if not st.session_state.get("twmc_unlocked"):
        st.markdown("### TWMC")
        st.caption("此為私人工具，請輸入密碼。")
        with st.form("twmc_gate"):
            entered = st.text_input("密碼", type="password")
            submitted = st.form_submit_button("進入", type="primary")
        if submitted:
            if hmac.compare_digest(entered, _APP_PASSWORD):
                st.session_state.twmc_unlocked = True
                st.rerun()
            st.error("密碼不正確")
        st.stop()

# 透過 CSS 放大整個介面的文字大小
st.markdown("""
    <style>
    /* 放大整體基礎字體 */
    html, body, [class*="css"]  { font-size: 18px !important; }
    /* 放大 Markdown 標題 */
    h1 { font-size: 2.5rem !important; }
    h2 { font-size: 2rem !important; }
    h3 { font-size: 1.75rem !important; }

    /* 每個圖表區塊加上白色外框，方便視覺上區分 */
    [data-testid="stBidiComponentRegular"] {
        border: 1px solid #ffffff;
        border-radius: 6px;
        overflow: hidden;
    }
    /* 元件的 iframe 預設是 display:inline，會沿著文字基線多出幾 px 的空白，
       讓外框底部有縫、也讓下方元素的位置難以預測，改成 block 才是實際高度 */
    iframe[data-testid="stCustomComponentV1"] {
        display: block;
    }
    /* 觀察清單卡片同樣使用白色外框 */
    [data-testid="stVerticalBlockBorderWrapper"]:has(> div > [data-testid="stVerticalBlock"]) {
        border-color: #ffffff;
    }
    </style>
""", unsafe_allow_html=True)


# ==========================================
# 0. 觀察清單 (1 x 5 佈局，滿 5 檔自動往下換列)
# ==========================================
COLUMNS_PER_ROW = 5

# 觀察清單是自訂元件：現成的拖曳元件只吃純文字、也不會回報點擊，
# 沒辦法同時做到「點卡片切換標的」和「拖曳排序」，所以自己寫前端。
watchlist_component = components.declare_component(
    "twmc_watchlist",
    path=str(Path(__file__).parent / "components" / "watchlist")
)
mode_switch_component = components.declare_component(
    "twmc_mode_switch",
    path=str(Path(__file__).parent / "components" / "mode_switch")
)

WATCHLIST_FILE = Path(__file__).parent / "watchlist.json"
NOTES_FILE = Path(__file__).parent / "investment_notes.json"
AI_HISTORY_DIR = Path(__file__).parent / "ai_analysis_history"
AI_HISTORY_INDEX = AI_HISTORY_DIR / "index.json"
AI_HISTORY_MAX_ITEMS = 300
MAINFORCE_DIR = Path(__file__).parent / "mainforce_history"
ALL_GROUP_ID = "all"
DEFAULT_GROUP_NAME = "自定義1"
MODE_KEYS = ("analyze", "simulated", "investment")
MODE_ACCENTS = {
    "analyze": "#ffffff",
    "simulated": "#b042ff",
    "investment": "#ff66cc",
}


def _hex_to_rgb(hex_color):
    value = str(hex_color or "").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        return (255, 255, 255)
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return (255, 255, 255)


def _mix_hex(color_a, color_b, weight_a):
    """在 sRGB 空間混色，取代 CSS color-mix()。"""
    ra, ga, ba = _hex_to_rgb(color_a)
    rb, gb, bb = _hex_to_rgb(color_b)
    w = max(0.0, min(1.0, float(weight_a)))
    mix = tuple(round(a * w + b * (1 - w)) for a, b in ((ra, rb), (ga, gb), (ba, bb)))
    return "#{:02x}{:02x}{:02x}".format(*mix)


def _empty_box(items=None, active_code=None):
    codes = list(items or [])
    return {
        "groups": [{"id": "custom-1", "name": DEFAULT_GROUP_NAME, "items": codes}],
        "active_group_id": "custom-1",
        "active_code": active_code or (codes[0] if codes else None),
    }


def _empty_mode_store(legacy_items=None):
    analyze = _empty_box(legacy_items or ["2330", "6182"])
    return {
        "version": 3,
        "modes": {
            "analyze": analyze,
            "simulated": _empty_box([]),
            "investment": _empty_box([]),
        },
    }


def _normalize_box(raw_box, fallback_items=None):
    if not isinstance(raw_box, dict):
        return _empty_box(fallback_items or [])
    groups = []
    for group in raw_box.get("groups") or []:
        gid = str(group.get("id") or "").strip()
        name = str(group.get("name") or "").strip()
        items = [str(code) for code in group.get("items") or [] if str(code).strip()]
        if not gid or gid == ALL_GROUP_ID:
            continue
        groups.append({"id": gid, "name": name or DEFAULT_GROUP_NAME, "items": items})
    if not groups:
        groups = _empty_box(fallback_items or [])["groups"]
    all_codes = []
    for group in groups:
        for code in group["items"]:
            if code not in all_codes:
                all_codes.append(code)
    active_group_id = raw_box.get("active_group_id")
    if active_group_id != ALL_GROUP_ID and active_group_id not in [g["id"] for g in groups]:
        active_group_id = groups[0]["id"]
    active_code = raw_box.get("active_code")
    if active_code not in all_codes:
        active_code = all_codes[0] if all_codes else None
    return {
        "groups": groups,
        "active_group_id": active_group_id,
        "active_code": active_code,
    }


def _next_group_id(groups):
    nums = []
    for group in groups:
        gid = str(group.get("id", ""))
        if gid.startswith("custom-") and gid.split("-")[-1].isdigit():
            nums.append(int(gid.split("-")[-1]))
    return f"custom-{(max(nums) + 1) if nums else 1}"


def _next_group_name(groups):
    nums = []
    for group in groups:
        name = group.get("name", "")
        if name.startswith("自定義") and name[3:].isdigit():
            nums.append(int(name[3:]))
    return f"自定義{(max(nums) + 1) if nums else 1}"


def _normalize_store(raw):
    # v1: flat list
    if isinstance(raw, list):
        codes = [str(code) for code in raw if str(code).strip()]
        return _empty_mode_store(codes), True
    if not isinstance(raw, dict):
        return _empty_mode_store(["2330", "6182"]), True

    # v3: per-mode boxes
    if raw.get("version") == 3 or isinstance(raw.get("modes"), dict):
        modes = {}
        for key in MODE_KEYS:
            fallback = ["2330", "6182"] if key == "analyze" else []
            modes[key] = _normalize_box((raw.get("modes") or {}).get(key), fallback)
        return {"version": 3, "modes": modes}, False

    # v2: single shared groups → migrate into analyze only
    legacy = _normalize_box(raw, ["2330", "6182"])
    store = _empty_mode_store([])
    store["modes"]["analyze"] = legacy
    return store, True


def save_watchlist_store(store):
    payload = {
        "version": 3,
        "modes": store["modes"],
    }
    tmp = WATCHLIST_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(WATCHLIST_FILE)
    api.put_blob("watchlist", payload)


def load_watchlist_store():
    raw = api.get_blob("watchlist") if api.logged_in() else None
    if raw is None and WATCHLIST_FILE.exists():
        try:
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            raw = None
    store, migrated = _normalize_store(raw if raw is not None else ["2330", "6182"])
    if migrated or raw is None:
        save_watchlist_store(store)
    elif api.logged_in() and api.get_blob("watchlist") is None:
        save_watchlist_store(store)
    return store


def apply_mode_box(mode=None):
    mode = mode or st.session_state.app_mode
    box = st.session_state.mode_boxes[mode]
    st.session_state.groups = box["groups"]
    st.session_state.active_group_id = box["active_group_id"]
    st.session_state.active_code = box["active_code"]


def persist_card_boxes():
    st.session_state.mode_boxes[st.session_state.app_mode] = {
        "groups": st.session_state.groups,
        "active_group_id": st.session_state.active_group_id,
        "active_code": st.session_state.active_code,
    }
    save_watchlist_store({"modes": st.session_state.mode_boxes})


NOTE_SECTIONS = (
    ("fundamental", "基本面", "產業、財報、估值、題材…"),
    ("technical", "技術面", "支撐壓力、型態、均線、進出點…"),
    ("chips", "籌碼面", "法人、主力、融資券、股東結構…"),
    ("summary", "總結", "進出理由、風險、後續觀察重點…"),
)


def _empty_discipline():
    """個股進出場紀律：條列條件＋可選參考價。"""
    return {
        "hold_ok": [],
        "get_conservative": [],
        "stop_loss": None,
        "take_profit": None,
        "note": "",
    }


def _normalize_discipline_list(val):
    """相容舊版字串（依換行拆條列）。"""
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    text = str(val or "").strip()
    if not text:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def _normalize_discipline(raw):
    base = _empty_discipline()
    if not isinstance(raw, dict):
        return base
    base["hold_ok"] = _normalize_discipline_list(raw.get("hold_ok"))
    base["get_conservative"] = _normalize_discipline_list(raw.get("get_conservative"))
    base["note"] = str(raw.get("note") or "").strip()
    for key in ("stop_loss", "take_profit"):
        val = raw.get(key)
        if val is None or val == "":
            base[key] = None
            continue
        try:
            num = float(val)
            base[key] = num if pd.notna(num) else None
        except (TypeError, ValueError):
            base[key] = None
    return base


def _discipline_filled(disc):
    disc = _normalize_discipline(disc)
    return bool(
        disc["hold_ok"]
        or disc["get_conservative"]
        or disc["note"]
        or disc["stop_loss"] is not None
        or disc["take_profit"] is not None
    )


def _discipline_for_gemini(disc):
    """轉成給 Gemini 的中文鍵；空白不送。（目前預設不餵 AI，保留相容）。"""
    disc = _normalize_discipline(disc)
    if not _discipline_filled(disc):
        return None
    out = {}
    if disc["hold_ok"]:
        out["續抱／加碼仍成立"] = disc["hold_ok"]
    if disc["get_conservative"]:
        out["該更保守／減碼"] = disc["get_conservative"]
    if disc["stop_loss"] is not None:
        out["停損參考價"] = round(float(disc["stop_loss"]), 2)
    if disc["take_profit"] is not None:
        out["停利參考價"] = round(float(disc["take_profit"]), 2)
    if disc["note"]:
        out["補充"] = disc["note"]
    return out


def _discipline_price_flags(disc, price):
    """依現價標示是否碰到參考停損／停利（僅提示，非自動下單）。"""
    disc = _normalize_discipline(disc)
    if price is None:
        return []
    try:
        px = float(price)
    except (TypeError, ValueError):
        return []
    flags = []
    if disc["stop_loss"] is not None and px <= float(disc["stop_loss"]):
        flags.append(f"現價已觸及／低於停損參考 {disc['stop_loss']:,.2f}")
    if disc["take_profit"] is not None and px >= float(disc["take_profit"]):
        flags.append(f"現價已觸及／高於停利參考 {disc['take_profit']:,.2f}")
    return flags


def _parse_discipline_price(raw):
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _discipline_ref_metrics(ref_price, mark_price, qty=None, avg_cost=None):
    """
    未填參考價 → 差距／預計損益皆顯示 0。
    差距：相對現價；預計損益：相對成本均價 × 股數（無持倉則 0）。
    """
    if ref_price is None:
        return "0", "0", None, None
    gap_txt = "0"
    gap_val = None
    if mark_price is not None:
        try:
            mark = float(mark_price)
            if mark != 0:
                diff = float(ref_price) - mark
                pct = diff / mark * 100.0
                gap_val = diff
                gap_txt = f"{diff:+.2f}（{pct:+.2f}%）"
        except (TypeError, ValueError):
            gap_txt = "0"
    pnl_txt = "0"
    pnl_val = None
    try:
        q = float(qty or 0)
        avg = float(avg_cost) if avg_cost is not None else None
    except (TypeError, ValueError):
        q, avg = 0.0, None
    if q > 0 and avg is not None:
        pnl_val = q * (float(ref_price) - avg)
        pnl_txt = f"{pnl_val:+,.0f}"
    return gap_txt, pnl_txt, gap_val, pnl_val


def _discipline_bullet_editor(code, field, label, placeholder, initial_items):
    """條列輸入：每列一條件，＋ 新增、× 刪除。回傳清理後的條列。"""
    state_key = f"disc_bullets_{field}_{code}"
    boot_key = f"{state_key}_booted"
    if not st.session_state.get(boot_key):
        st.session_state[state_key] = list(initial_items) if initial_items else [""]
        st.session_state[boot_key] = True
        for i, val in enumerate(st.session_state[state_key]):
            st.session_state[f"{state_key}_r{i}"] = val

    rows = st.session_state[state_key]
    n = len(rows)
    st.markdown(f"**{label}**")
    delete_idx = None
    for i in range(n):
        rk = f"{state_key}_r{i}"
        if rk not in st.session_state:
            st.session_state[rk] = rows[i] if i < len(rows) else ""
        c1, c2 = st.columns([18, 1])
        with c1:
            st.text_input(
                f"{label} {i + 1}",
                key=rk,
                placeholder=placeholder if i == 0 else "繼續輸入條件…",
                label_visibility="collapsed",
            )
        with c2:
            if st.button("×", key=f"{state_key}_x{i}", use_container_width=True):
                delete_idx = i

    def _collect(n_rows):
        return [st.session_state.get(f"{state_key}_r{i}", "") for i in range(n_rows)]

    def _rebind(vals):
        old_n = len(st.session_state.get(state_key) or [])
        for i in range(max(old_n, len(vals)) + 2):
            st.session_state.pop(f"{state_key}_r{i}", None)
            st.session_state.pop(f"{state_key}_x{i}", None)
        st.session_state[state_key] = vals if vals else [""]
        for i, val in enumerate(st.session_state[state_key]):
            st.session_state[f"{state_key}_r{i}"] = val

    if delete_idx is not None:
        vals = [v for i, v in enumerate(_collect(n)) if i != delete_idx]
        _rebind(vals)
        st.rerun()

    add_col, _ = st.columns([1, 8])
    with add_col:
        if st.button("＋", key=f"{state_key}_add", use_container_width=True, help="新增一列"):
            vals = _collect(n)
            vals.append("")
            _rebind(vals)
            st.rerun()

    return [v.strip() for v in _collect(len(st.session_state[state_key])) if str(v).strip()]


def _discipline_ref_panel(label, code, field, initial, mark_price, qty, avg_cost):
    """停損／停利單欄：輸入＋與現價差距、預計損益（未填則為 0）。"""
    input_key = f"disc_ref_{field}_{code}"
    boot_key = f"{input_key}_booted"
    if not st.session_state.get(boot_key):
        st.session_state[input_key] = (
            "" if initial is None else f"{float(initial):g}"
        )
        st.session_state[boot_key] = True

    st.markdown(f"**{label}**")
    st.text_input(
        label,
        key=input_key,
        placeholder="選填，例如 48.5",
        label_visibility="collapsed",
    )
    ref = _parse_discipline_price(st.session_state.get(input_key))
    gap_txt, pnl_txt, _, pnl_val = _discipline_ref_metrics(
        ref, mark_price, qty=qty, avg_cost=avg_cost
    )
    pnl_color = "#fafafa"
    if pnl_val is not None:
        if pnl_val > 0:
            pnl_color = "#ef232a"
        elif pnl_val < 0:
            pnl_color = "#14b143"
    st.markdown(
        f"<div class='twmc-disc-ref-meta'>"
        f"<div>與現價差距：<b>{html.escape(gap_txt)}</b></div>"
        f"<div>預計損益：<b style='color:{pnl_color}'>{html.escape(pnl_txt)}</b></div>"
        f"</div>",
        unsafe_allow_html=True,
    )
    return ref


def _empty_note():
    return {
        "tags": [],
        "fundamental": "",
        "technical": "",
        "chips": "",
        "summary": "",
        "discipline": _empty_discipline(),
        "updated_at": "",
    }


def _normalize_note_entry(entry):
    """相容舊版單一 body：沒有分區時把內容遷到總結。"""
    if not isinstance(entry, dict):
        return _empty_note()
    tags = [str(t).strip() for t in (entry.get("tags") or []) if str(t).strip()]
    note = _empty_note()
    note["tags"] = tags
    note["updated_at"] = str(entry.get("updated_at") or "")
    note["discipline"] = _normalize_discipline(entry.get("discipline"))
    legacy_body = str(entry.get("body") or "")
    for key, _, _ in NOTE_SECTIONS:
        val = str(entry.get(key) or "")
        note[key] = val
    # 舊資料只有 body、尚未寫過分區 → 遷到總結
    if legacy_body and not any(note[k] for k, _, _ in NOTE_SECTIONS):
        note["summary"] = legacy_body
    return note


def _empty_notes_store():
    return {"version": 2, "notes": {}}


def load_investment_notes():
    if api.logged_in():
        remote = api.get_blob("notes")
        if isinstance(remote, dict) and isinstance(remote.get("notes"), dict):
            notes = {}
            for code, entry in remote["notes"].items():
                notes[str(code)] = _normalize_note_entry(entry)
            return {"version": 2, "notes": notes}
    if NOTES_FILE.exists():
        try:
            with open(NOTES_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and isinstance(raw.get("notes"), dict):
                notes = {}
                for code, entry in raw["notes"].items():
                    notes[str(code)] = _normalize_note_entry(entry)
                return {"version": 2, "notes": notes}
        except Exception:
            pass
    return _empty_notes_store()


def save_investment_notes(store=None):
    payload = store or st.session_state.investment_notes
    # 存檔一律用分區格式，不再寫 body
    cleaned = {"version": 2, "notes": {}}
    for code, entry in (payload.get("notes") or {}).items():
        note = _normalize_note_entry(entry)
        cleaned["notes"][code] = {
            "tags": note["tags"],
            "fundamental": note["fundamental"],
            "technical": note["technical"],
            "chips": note["chips"],
            "summary": note["summary"],
            "discipline": _normalize_discipline(note.get("discipline")),
            "updated_at": note["updated_at"],
        }
    tmp = NOTES_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)
    tmp.replace(NOTES_FILE)
    api.put_blob("notes", cleaned)
    if store is None:
        st.session_state.investment_notes = cleaned
    return cleaned


def get_note(code):
    entry = (st.session_state.investment_notes.get("notes") or {}).get(code)
    return _normalize_note_entry(entry)


def update_note(code, tags=None, sections=None, discipline=None):
    notes = st.session_state.investment_notes.setdefault("notes", {})
    note = _normalize_note_entry(notes.get(code))
    if tags is not None:
        note["tags"] = list(tags)
    if sections is not None:
        for key, _, _ in NOTE_SECTIONS:
            if key in sections:
                note[key] = str(sections[key] or "")
    if discipline is not None:
        note["discipline"] = _normalize_discipline(discipline)
    note["updated_at"] = datetime.now().isoformat(timespec="seconds")
    notes[code] = note
    save_investment_notes()


def close_investment_notes():
    st.session_state.notes_open = False
    st.session_state.notes_focus_section = None
    # 清掉紀律編輯器暫存，下次打開依存檔重載
    for key in list(st.session_state.keys()):
        if (
            str(key).startswith("disc_bullets_")
            or str(key).startswith("disc_ref_")
            or str(key).startswith("disc_note_")
        ):
            del st.session_state[key]


def switch_app_mode(next_mode):
    if next_mode == st.session_state.app_mode:
        return
    persist_card_boxes()
    st.session_state.app_mode = next_mode
    st.session_state.show_add = False
    st.session_state.trade_error = ""
    st.session_state.last_event = None
    close_investment_notes()
    apply_mode_box(next_mode)
    persist_portfolio()


def visible_codes():
    if st.session_state.active_group_id == ALL_GROUP_ID:
        seen = []
        for group in st.session_state.groups:
            for code in group["items"]:
                if code not in seen:
                    seen.append(code)
        return seen
    for group in st.session_state.groups:
        if group["id"] == st.session_state.active_group_id:
            return list(group["items"])
    return []


def box_scoped_transactions(transactions):
    """目前卡片盒範圍內的交易；「全部」不過濾。"""
    if st.session_state.active_group_id == ALL_GROUP_ID:
        return list(transactions or [])
    allowed = set(visible_codes())
    return [tx for tx in (transactions or []) if tx.get("code") in allowed]


def current_group():
    for group in st.session_state.groups:
        if group["id"] == st.session_state.active_group_id:
            return group
    return None


def all_codes():
    seen = []
    for group in st.session_state.groups:
        for code in group["items"]:
            if code not in seen:
                seen.append(code)
    return seen


_store = load_watchlist_store()
_pf_store = pf.load_store()
if api.logged_in():
    remote_pf = api.get_blob("portfolio")
    if isinstance(remote_pf, dict):
        _pf_store, _ = pf.normalize_store(remote_pf)
if "app_mode" not in st.session_state:
    st.session_state.app_mode = _pf_store.get("active_mode", "analyze")
if "mode_boxes" not in st.session_state:
    st.session_state.mode_boxes = _store["modes"]
if "groups" not in st.session_state:
    apply_mode_box(st.session_state.app_mode)
if "add_error" not in st.session_state:
    st.session_state.add_error = ""
if "show_add" not in st.session_state:
    st.session_state.show_add = False
if "last_event" not in st.session_state:
    st.session_state.last_event = None
if "portfolio" not in st.session_state:
    st.session_state.portfolio = _pf_store
if "trade_error" not in st.session_state:
    st.session_state.trade_error = ""
if "notes_open" not in st.session_state:
    st.session_state.notes_open = False
if "notes_focus_section" not in st.session_state:
    st.session_state.notes_focus_section = None
if "investment_notes" not in st.session_state:
    st.session_state.investment_notes = load_investment_notes()
if "notes_just_saved" not in st.session_state:
    st.session_state.notes_just_saved = False


def persist_portfolio():
    st.session_state.portfolio["active_mode"] = st.session_state.app_mode
    pf.save_store(st.session_state.portfolio)
    book = st.session_state.portfolio
    api.put_blob(
        "portfolio",
        {
            "version": 1,
            "active_mode": book.get("active_mode", "analyze"),
            "simulated": book["simulated"],
            "investment": book["investment"],
        },
    )


@st.cache_data(ttl=60 * 60)
def get_institutional_data_2m(code):
    """取得法人買賣超（約可涵蓋 1 年交易日，供宏觀視窗）。"""
    end_date = datetime.today()
    start_date = end_date - timedelta(days=400)
    url = "https://api.finmindtrade.com/api/v4/data"
    params = {
        "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
        "data_id": code,
        "start_date": start_date.strftime("%Y-%m-%d"),
        "end_date": end_date.strftime("%Y-%m-%d")
    }
    try:
        res = requests.get(url, params=params, timeout=15).json()
        if "data" in res and res["data"]:
            df = pd.DataFrame(res["data"])
            df['net'] = df['buy'] - df['sell']
            df = df.pivot_table(index='date', columns='name', values='net', aggfunc='sum').fillna(0)
            
            res_df = pd.DataFrame()
            res_df['外資買賣超'] = df.get('Foreign_Investor', 0)
            res_df['投信買賣超'] = df.get('Investment_Trust', 0)
            # 自營商包含自行買賣與避險
            res_df['自營商買賣超'] = df.get('Dealer_self', 0) + df.get('Dealer_Hedging', 0) + df.get('Foreign_Dealer_Self', 0)
            res_df['三大法人合計'] = res_df['外資買賣超'] + res_df['投信買賣超'] + res_df['自營商買賣超']
            
            return res_df.sort_index(ascending=False).reset_index()
    except Exception:
        pass
    return pd.DataFrame()


# 一般股法人動向：依時間維度套用不同真值表（key = 外資, 投信, 自營商；1=買 -1=賣）
INSTITUTIONAL_FLOW_RULES_DAY = {
    (1, 1, 1): {
        "name": "當日狂熱長紅",
        "analysis": "突發重大利多，三大熱錢同時點火。容易收實體大紅K或漲停。",
        "strategy": "今日強勢無虞，可順勢打短單。",
        "tone": "buy",
    },
    (1, 1, -1): {
        "name": "實質買盤進駐",
        "analysis": "外資投信真實買進，自營商多為當沖獲利了結或避險。",
        "strategy": "過濾了短線雜訊，今日上漲為實質強勢，可留倉。",
        "tone": "buy",
    },
    (1, -1, 1): {
        "name": "外資點火自營跟單",
        "analysis": "外資主導買盤，自營商進場搶短，投信可能在調節或應付贖回。",
        "strategy": "容易開高走低，今日不宜追高。",
        "tone": "warning",
    },
    (1, -1, -1): {
        "name": "外資左手接右手",
        "analysis": "內資全面倒貨，外資逢低承接。當日股價多半壓抑震盪。",
        "strategy": "買盤動能抵銷，今日適合觀望，不動作。",
        "tone": "warning",
    },
    (-1, 1, 1): {
        "name": "內資逆勢點火",
        "analysis": "外資大賣提款，內資進場扛盤。中小型股容易出現逆勢拉抬。",
        "strategy": "今日強弱分明，大型股避開，中小型股可跟隨投信作多。",
        "tone": "warning",
    },
    (-1, 1, -1): {
        "name": "投信獨自硬扛",
        "analysis": "外資與自營商倒貨，當日盤勢偏弱，投信可能被迫吃貨（ETF建倉）。",
        "strategy": "上方賣壓極重，今日絕對不要進場接刀。",
        "tone": "sell",
    },
    (-1, -1, 1): {
        "name": "極短線假反彈",
        "analysis": "長中線主力大賣，自營商進場搶極短線反彈或權證避險。",
        "strategy": "當日的反彈皆是逃命波，趁拉高趕快停損。",
        "tone": "sell",
    },
    (-1, -1, -1): {
        "name": "當日恐慌大屠殺",
        "analysis": "情緒崩潰，大戶全面結帳倒貨。極易出現長黑K或跌停鎖死。",
        "strategy": "無條件停損，多殺多成型，絕對空手。",
        "tone": "sell",
    },
}

INSTITUTIONAL_FLOW_RULES_SHORT = {
    (1, 1, 1): {
        "name": "波段主升段發動",
        "analysis": "三方資金短期內極度共識，股價沿著 5 日線強勢噴出。",
        "strategy": "波段最強攻擊型態，勇敢買進並沿 5 日線抱緊。",
        "tone": "buy",
    },
    (1, 1, -1): {
        "name": "最健康波段上漲",
        "analysis": "外資投信聯手發動波段，自營商獲利了結剛好洗清短期浮額。",
        "strategy": "籌碼極度乾淨，波段趨勢明確，逢拉回 10 日線即是買點。",
        "tone": "buy",
    },
    (1, -1, 1): {
        "name": "外資短線推土機",
        "analysis": "外資主導短期趨勢，大型權值股緩步墊高；留意投信對中小型股的賣壓。",
        "strategy": "波段偏多，但僅限於操作外資認養的大型股。",
        "tone": "buy",
    },
    (1, -1, -1): {
        "name": "外資獨木難支",
        "analysis": "外資雖買，但內資完全不認同短線行情，上漲動能被內資賣壓抵銷。",
        "strategy": "短期極易陷入橫盤整理，資金運用效率低，暫不進場。",
        "tone": "warning",
    },
    (-1, 1, 1): {
        "name": "中小型飆漲期",
        "analysis": "外資沒興趣，但投信連續認養發動波段攻擊，自營商跟風。",
        "strategy": "標準的投信作帳行情，專注操作投信高持股的中小型股。",
        "tone": "buy",
    },
    (-1, 1, -1): {
        "name": "投信結帳未爆彈",
        "analysis": "投信可能被套牢或硬扛作帳，短線籌碼極度不良。",
        "strategy": "若投信放棄認養隨時引發波段崩跌，極度危險，盡速避開。",
        "tone": "sell",
    },
    (-1, -1, 1): {
        "name": "波段誘多陷阱",
        "analysis": "長中線資金持續撤出，自營商的短期連買只是在做價差或避險。",
        "strategy": "波段趨勢向下，切勿被幾天的紅K誘騙進場。",
        "tone": "sell",
    },
    (-1, -1, -1): {
        "name": "波段空頭主跌段",
        "analysis": "籌碼徹底渙散，均線下彎，法人短期內一致看壞。",
        "strategy": "趨勢形成完美空頭，絕對不宜進場搶短，可考慮做空。",
        "tone": "sell",
    },
}

INSTITUTIONAL_FLOW_RULES_LONG = {
    (1, 1, 1): {
        "name": "超級大牛股誕生",
        "analysis": "企業迎來產業大爆發或基本面大轉機，法人長線資金全面進駐。",
        "strategy": "擁有最強大的長線保護傘，任何單日的大跌都是極佳買點。",
        "tone": "buy",
    },
    (1, 1, -1): {
        "name": "安心長抱定存股",
        "analysis": "外資與投信長期鎖碼，自營商短線進出不影響大局，籌碼極度安定。",
        "strategy": "適合大資金長線重壓，不看盤也能安心睡覺的標的。",
        "tone": "buy",
    },
    (1, -1, 1): {
        "name": "國際資金長線認同",
        "analysis": "外資長線看好，但國內投信因基金規模限制或策略不同無法長期佈局。",
        "strategy": "長線趨勢仍屬多頭，以外資動向為唯一進出依據。",
        "tone": "buy",
    },
    (1, -1, -1): {
        "name": "大型權值股常態",
        "analysis": "外資長線持有（如台積電），內資資金過小，長線影響力微乎其微。",
        "strategy": "忽略投信與自營商的數據，長線死守外資成本線。",
        "tone": "buy",
    },
    (-1, 1, 1): {
        "name": "長線土洋拉鋸戰",
        "analysis": "典型的「外資提款、國家隊/內資長線護盤」。大型股長線疲軟。",
        "strategy": "長線大盤通常處於震盪打底期，大資金不宜在此時重壓。",
        "tone": "warning",
    },
    (-1, 1, -1): {
        "name": "被動型ETF殭屍股",
        "analysis": "長線籌碼已全敗壞，投信買超僅因高股息ETF的被動設定而機械式持有。",
        "strategy": "基本面轉弱但有ETF買盤撐著，缺乏上漲動能，避開為妙。",
        "tone": "sell",
    },
    (-1, -1, 1): {
        "name": "長線衰退避險標的",
        "analysis": "基本面長期衰退。自營商長線買超多為發行衍生性商品（權證）的被動避險。",
        "strategy": "自營商不是看好才買，長期趨勢走空，任何反彈皆是賣點。",
        "tone": "sell",
    },
    (-1, -1, -1): {
        "name": "夕陽產業長線破底",
        "analysis": "產業步入長線衰退，大戶資金徹底撤離，股價沿季線長期陰跌。",
        "strategy": "毫無懸念的長線空頭，直接從觀察清單永久刪除。",
        "tone": "sell",
    },
}

INSTITUTIONAL_FLOW_RULES_BY_WINDOW = {
    "day": INSTITUTIONAL_FLOW_RULES_DAY,
    "short": INSTITUTIONAL_FLOW_RULES_SHORT,
    "long": INSTITUTIONAL_FLOW_RULES_LONG,
}

# ETF（00 開頭）：外資（套利巨鯨）× 自營商（造市供給）；key = (外資, 自營商)；1=買 -1=賣
ETF_FLOW_RULES_DAY = {
    (-1, -1): {
        "name": "單日瘋狂溢價",
        "analysis": "散戶看到新聞當天瘋狂搶進，自營商狂發便當，外資逮到溢價機會高價倒貨套利。",
        "strategy": "絕對不買！當天市價必定遠超淨值，進場就是當盤子，等溢價收斂再說。",
        "tone": "sell",
    },
    (-1, 1): {
        "name": "外資單日砸盤",
        "analysis": "美股昨晚大跌，外資當天把台灣 ETF 當提款機狂倒貨，自營商被迫硬扛吸收。",
        "strategy": "不要接刀，但也無須恐慌。如果是國際利空，等外資這波單日提款結束後再觀察。",
        "tone": "warning",
    },
    (1, -1): {
        "name": "外資單日建倉",
        "analysis": "散戶穩穩存，但外資看好底層成分股，當天用大資金進場掃貨。",
        "strategy": "今日強勢無虞。外資真金白銀推升淨值，可順勢單筆買進或安心扣款。",
        "tone": "buy",
    },
    (1, 1): {
        "name": "單日黃金超跌坑",
        "analysis": "散戶當日極度恐慌賤賣，導致嚴重折價。外資與自營商聯手在低檔快樂撿屍。",
        "strategy": "天上掉下來的禮物！看到這種雙紅買超加上折價，閉著眼睛跟著大戶撿便宜。",
        "tone": "buy",
    },
}

ETF_FLOW_RULES_SHORT = {
    (-1, -1): {
        "name": "短線高檔韭菜田",
        "analysis": "新聞熱潮延燒一週，散戶持續追高，溢價久久不退，大戶連續一週都在逢高割韭菜。",
        "strategy": "極度危險的過熱區！熱度隨時會崩潰，空手觀望，甚至可考慮獲利了結一趟。",
        "tone": "sell",
    },
    (-1, 1): {
        "name": "短線空頭提款機",
        "analysis": "外資連續一週將資金撤出台灣市場，散戶也在停損，自營商滿手套牢籌碼。",
        "strategy": "波段趨勢向下。大環境資金正在退潮，底層成分股承壓，暫時停止單筆加碼。",
        "tone": "sell",
    },
    (1, -1): {
        "name": "短線健康多頭",
        "analysis": "散戶穩定定期定額，外資波段資金持續湧入台股，推升 ETF 淨值。",
        "strategy": "波段最安心的賺錢時刻。籌碼穩定且趨勢向上，保持現有部位讓獲利自然奔跑。",
        "tone": "buy",
    },
    (1, 1): {
        "name": "短線絕佳落底區",
        "analysis": "散戶連續一週恐慌人踩人被洗出場，籌碼徹底沉澱，大戶在底部吸飽了便宜籌碼。",
        "strategy": "準備迎接大反彈！浮額已清洗乾淨，這是波段絕佳的「左側建倉」買點。",
        "tone": "buy",
    },
}

ETF_FLOW_RULES_LONG = {
    (-1, -1): {
        "name": "泡沫破裂的夕陽",
        "analysis": "這檔 ETF 的主題（如某特定產業）長線大衰退，大戶資金撤出，只剩散戶還在死忠攤平。",
        "strategy": "徹底看錯趨勢，果斷換股。ETF 也是會跌到下市的，請檢視成分股，該停損就停損。",
        "tone": "sell",
    },
    (-1, 1): {
        "name": "宏觀資金長線轉移",
        "analysis": "外資進行長達一季的全球資產重新配置（如賣台股買美股），導致台股長線疲軟，內資被迫護盤。",
        "strategy": "資金效率極低。長線處於逆風局，大筆資金不宜投入，僅適合小額定期定額攤平。",
        "tone": "warning",
    },
    (1, -1): {
        "name": "長線完美護城河",
        "analysis": "外資長線極度看好底層資產（如台積電），散戶的穩定需求由自營商消化，價格緊貼淨值上漲。",
        "strategy": "無腦長抱的傳家寶。長線籌碼擁有最強防禦力，任何短線的單日大跌，都是重壓買點。",
        "tone": "buy",
    },
    (1, 1): {
        "name": "長線價值浮現",
        "analysis": "經過漫長的熊市，外資與造市商在底部默默囤積了數個月的籌碼，散戶已全數躺平無感。",
        "strategy": "大牛市爆發前的寧靜。極度罕見的長線大底，勇敢進場佈局，耐心等待主升段到來。",
        "tone": "buy",
    },
}

ETF_FLOW_RULES_BY_WINDOW = {
    "day": ETF_FLOW_RULES_DAY,
    "short": ETF_FLOW_RULES_SHORT,
    "long": ETF_FLOW_RULES_LONG,
}


def is_etf_code(code):
    """00 開頭視為 ETF。"""
    return str(code or "").strip().upper().startswith("00")


def _flow_side(value):
    """買賣超：>0 買、≤0 賣（含平盤／零視為未買進）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return -1
    if pd.isna(num):
        return -1
    return 1 if num > 0 else -1


def _etf_side_word(side, window_key="day"):
    """依時間維度顯示買／連買／大買（或賣）。"""
    if side > 0:
        return {"day": "買", "short": "連買", "long": "大買"}.get(window_key, "買")
    return {"day": "賣", "short": "連賣", "long": "大賣"}.get(window_key, "賣")


# 法人動向時間視窗：
# (頁籤, 視窗鍵, 交易日, 個股現象標, 個股策略標, ETF現象標, ETF策略標)
INSTITUTIONAL_FLOW_WINDOWS = (
    (
        "微觀(當天)",
        "day",
        1,
        "當日盤面現象",
        "當日實戰解析與策略",
        "當日盤面隱藏真相",
        "當日實戰解析與策略",
    ),
    (
        "短線(5~10日)",
        "short",
        10,
        "短期波段現象",
        "短期實戰解析與策略",
        "短期波段隱藏真相",
        "短期實戰解析與策略",
    ),
    (
        "宏觀(1年)",
        "long",
        250,
        "長期趨勢現象",
        "長期實戰解析與策略",
        "長期趨勢隱藏真相",
        "長期實戰解析與策略",
    ),
)


def aggregate_institutional_window(inst_df, days):
    """加總最近 N 個交易日的法人買賣超；回傳單列 dict。"""
    if inst_df is None or inst_df.empty:
        return None
    days = max(int(days or 1), 1)
    window = inst_df.head(days).copy()
    if window.empty:
        return None
    dates = [str(d) for d in window["date"].tolist()]
    date_end = dates[0]
    date_start = dates[-1]
    date_label = date_end if days == 1 or date_start == date_end else f"{date_start}～{date_end}"
    return {
        "date": date_label,
        "date_start": date_start,
        "date_end": date_end,
        "window_days": int(len(window)),
        "外資買賣超": float(window["外資買賣超"].sum()),
        "投信買賣超": float(window["投信買賣超"].sum()),
        "自營商買賣超": float(window["自營商買賣超"].sum()),
        "三大法人合計": float(window["三大法人合計"].sum()) if "三大法人合計" in window.columns else 0.0,
    }


def classify_institutional_flow(
    row,
    code=None,
    window_days=1,
    period_label="當日",
    window_key="day",
    lab_scene=None,
    lab_strategy=None,
):
    """一般股／ETF 各依時間維度套用對應真值表。"""
    foreign_val = float(row.get("外資買賣超") or 0)
    trust_val = float(row.get("投信買賣超") or 0)
    dealer_val = float(row.get("自營商買賣超") or 0)
    date = str(row.get("date") or row.get("日期") or "")
    days = max(int(window_days or 1), 1)

    if is_etf_code(code):
        foreign = _flow_side(foreign_val)
        dealer = _flow_side(dealer_val)
        rules = ETF_FLOW_RULES_BY_WINDOW.get(window_key) or ETF_FLOW_RULES_DAY
        rule = rules.get((foreign, dealer))
        if not rule:
            return None
        return {
            "kind": "etf",
            **rule,
            "foreign": foreign,
            "dealer": dealer,
            "foreign_val": foreign_val,
            "dealer_val": dealer_val,
            "date": date,
            "window_days": days,
            "period_label": period_label,
            "window_key": window_key,
            "lab_scene": lab_scene or "盤面隱藏真相",
            "lab_strategy": lab_strategy or "實戰解析與策略",
        }

    foreign = _flow_side(foreign_val)
    trust = _flow_side(trust_val)
    dealer = _flow_side(dealer_val)
    rules = INSTITUTIONAL_FLOW_RULES_BY_WINDOW.get(window_key) or INSTITUTIONAL_FLOW_RULES_DAY
    rule = rules.get((foreign, trust, dealer))
    if not rule:
        return None
    return {
        "kind": "stock",
        **rule,
        "foreign": foreign,
        "trust": trust,
        "dealer": dealer,
        "foreign_val": foreign_val,
        "trust_val": trust_val,
        "dealer_val": dealer_val,
        "date": date,
        "window_days": days,
        "period_label": period_label,
        "window_key": window_key,
        "lab_scene": lab_scene or "盤面現象",
        "lab_strategy": lab_strategy or "實戰解析與策略",
    }


def _render_institutional_flow_card(flow, code=None):
    """渲染單一視窗的法人動向卡片。"""
    if not flow:
        st.info("此期間法人買賣超組合無法對應規則。")
        return

    tone = flow.get("tone") or "warning"
    days = max(int(flow.get("window_days") or 1), 1)
    period_label = flow.get("period_label") or "當日"
    window_key = flow.get("window_key") or "day"
    lab_scene = flow.get("lab_scene") or "盤面現象"
    lab_strategy = flow.get("lab_strategy") or "實戰解析與策略"

    if flow.get("kind") == "etf":
        def etf_side_html(label, side, value):
            word = _etf_side_word(side, window_key)
            color = "#ef232a" if side > 0 else "#14b143"
            return (
                f"<div class='twmc-flow-side'>"
                f"<div class='k'>{html.escape(label)}</div>"
                f"<div class='v' style='color:{color}'>{html.escape(word)}</div>"
                f"<div class='n' style='color:{color}'>{value:+,.0f}</div>"
                f"</div>"
            )

        sides = (
            etf_side_html("外資（套利／巨鯨）", flow["foreign"], flow["foreign_val"])
            + etf_side_html("自營商（造市供給）", flow["dealer"], flow["dealer_val"])
        )
        footer = (
            f"{html.escape(period_label)}累計　資料：{html.escape(flow['date'] or '—')}　"
            f"（ETF：買賣超 &gt; 0 視為買，≤ 0 視為賣；實際 {days} 個交易日）"
        )
        sides_class = "twmc-flow-sides etf"
    else:
        def side_html(label, side, value):
            word = "買" if side > 0 else "賣"
            color = "#ef232a" if side > 0 else "#14b143"
            return (
                f"<div class='twmc-flow-side'>"
                f"<div class='k'>{html.escape(label)}</div>"
                f"<div class='v' style='color:{color}'>{word}</div>"
                f"<div class='n' style='color:{color}'>{value:+,.0f}</div>"
                f"</div>"
            )

        sides = (
            side_html("外資", flow["foreign"], flow["foreign_val"])
            + side_html("投信", flow["trust"], flow["trust_val"])
            + side_html("自營商", flow["dealer"], flow["dealer_val"])
        )
        footer = (
            f"{html.escape(period_label)}累計　資料：{html.escape(flow['date'] or '—')}　"
            f"（買賣超 &gt; 0 視為買，≤ 0 視為賣；實際 {days} 個交易日）"
        )
        sides_class = "twmc-flow-sides"

    st.markdown(
        f"<div class='twmc-flow-box tone-{html.escape(tone)}'>"
        f"<div class='twmc-flow-title'>{html.escape(flow['name'])}</div>"
        f"<div class='{sides_class}'>{sides}</div>"
        f"<div class='twmc-flow-block'><span class='lab'>{html.escape(lab_scene)}</span>"
        f"{html.escape(flow['analysis'])}</div>"
        f"<div class='twmc-flow-block'><span class='lab'>{html.escape(lab_strategy)}</span>"
        f"{html.escape(flow['strategy'])}</div>"
        f"<div class='twmc-flow-date'>{footer}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )


def _classify_flow_for_window(inst_df, code, window_spec):
    """依單一時間視窗規格產出 flow dict（或缺資料時回傳 None）。"""
    (
        period_label,
        window_key,
        days,
        stock_lab_scene,
        stock_lab_strategy,
        etf_lab_scene,
        etf_lab_strategy,
    ) = window_spec
    agg = aggregate_institutional_window(inst_df, days)
    if not agg:
        return None
    if is_etf_code(code):
        lab_scene, lab_strategy = etf_lab_scene, etf_lab_strategy
    else:
        lab_scene, lab_strategy = stock_lab_scene, stock_lab_strategy
    return classify_institutional_flow(
        agg,
        code=code,
        window_days=agg.get("window_days") or days,
        period_label=period_label,
        window_key=window_key,
        lab_scene=lab_scene,
        lab_strategy=lab_strategy,
    )


def _render_institutional_flow_overview(inst_df, code=None):
    """總覽：三個時間維度標題並列。"""
    cards = []
    for window_spec in INSTITUTIONAL_FLOW_WINDOWS:
        period_label = window_spec[0]
        flow = _classify_flow_for_window(inst_df, code, window_spec)
        if not flow:
            cards.append(
                f"<div class='twmc-flow-overview-card tone-warning'>"
                f"<div class='ov-k'>{html.escape(period_label)}</div>"
                f"<div class='ov-v'>資料不足</div>"
                f"</div>"
            )
            continue
        tone = flow.get("tone") or "warning"
        cards.append(
            f"<div class='twmc-flow-overview-card tone-{html.escape(tone)}'>"
            f"<div class='ov-k'>{html.escape(period_label)}</div>"
            f"<div class='ov-v'>{html.escape(flow.get('name') or '—')}</div>"
            f"</div>"
        )
    st.markdown(
        f"<div class='twmc-flow-overview'>{''.join(cards)}</div>",
        unsafe_allow_html=True,
    )


def render_institutional_flow(inst_df, code=None):
    """籌碼面／法人資訊：總覽＋微觀／短線／宏觀頁籤。"""
    if inst_df is None or inst_df.empty:
        st.info("目前無法判斷法人動向。")
        return

    tab_labels = ["總覽"] + [item[0] for item in INSTITUTIONAL_FLOW_WINDOWS]
    flow_tabs = st.tabs(tab_labels)
    with flow_tabs[0]:
        _render_institutional_flow_overview(inst_df, code=code)

    for tab, window_spec in zip(flow_tabs[1:], INSTITUTIONAL_FLOW_WINDOWS):
        with tab:
            period_label = window_spec[0]
            flow = _classify_flow_for_window(inst_df, code, window_spec)
            if not flow:
                st.info(f"尚無足夠資料判斷{period_label}法人動向。")
                continue
            _render_institutional_flow_card(flow, code=code)


def _prepare_volume_frame(df):
    """日線 OHLCV → 附加 VMA5／VMA20／OBV。"""
    if df is None or df.empty:
        return pd.DataFrame()
    need = {"Close", "Volume"}
    if not need.issubset(df.columns):
        return pd.DataFrame()
    d = df.copy()
    d = d.dropna(subset=["Close", "Volume"])
    if d.empty:
        return pd.DataFrame()
    d["VMA5"] = d["Volume"].rolling(5).mean()
    d["VMA20"] = d["Volume"].rolling(20).mean()
    price_chg = d["Close"].diff()
    signed = price_chg.apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    d["OBV"] = (signed * d["Volume"]).cumsum()
    return d


def analyze_volume(df):
    """
    彙整交易量三大類：
    1) 基準狀態：量縮／量增／爆量
    2) 均量線：VMA、OBV
    3) 量價配合：齊揚／背離／天量天價／地量地價
    """
    d = _prepare_volume_frame(df)
    if len(d) < 25:
        return None

    latest = d.iloc[-1]
    prev = d.iloc[-2]
    vol = float(latest["Volume"] or 0)
    vma5 = float(latest["VMA5"] or 0)
    vma20 = float(latest["VMA20"] or 0)
    close = float(latest["Close"] or 0)
    prev_close = float(prev["Close"] or 0)
    obv = float(latest["OBV"] or 0)
    date = latest.name
    date_str = date.strftime("%Y-%m-%d") if hasattr(date, "strftime") else str(date)[:10]

    if vma20 <= 0:
        return None

    ratio20 = vol / vma20
    price_up = close > prev_close
    price_down = close < prev_close
    lots = vol / 1000.0

    # —— 一、基準狀態 ——
    if ratio20 >= 2.0:
        base_name = "爆量"
        base_tone = "warning"
        if price_up:
            base_analysis = "成交量遠高於 20 日均量，且收紅。籌碼劇烈換手，偏攻擊訊號，但仍需確認能否續強。"
            base_strategy = "可偏多觀察，避免在第一根爆量末端盲目追高。"
        elif price_down:
            base_analysis = "爆量長黑，賣方力道極重，常見於恐慌殺盤或主力倒貨。"
            base_strategy = "慎防出貨或多殺多，不宜急著進場接刀。"
            base_tone = "sell"
        else:
            base_analysis = "量能異常放大但價格變化不大，市場分歧升高。"
            base_strategy = "等待方向確認後再動作。"
    elif ratio20 >= 1.3:
        base_name = "量增"
        base_tone = "buy" if price_up else ("sell" if price_down else "warning")
        base_analysis = (
            "成交量明顯高於 20 日均量，市場熱度上升、資金凝聚力增強。"
            if price_up
            else "成交量放大但價格承壓，代表換手或賣壓增加。"
        )
        base_strategy = (
            "帶量上攻通常較健康，可順勢留意續航力。"
            if price_up
            else "量增下跌偏弱，宜降低追價意願。"
        )
    elif ratio20 <= 0.7:
        base_name = "量縮"
        base_tone = "buy" if price_down else "warning"
        if price_down:
            base_analysis = "多頭回檔搭配量縮，賣壓減輕、浮額沉澱，通常是相對健康的整理。"
            base_strategy = "偏多格局下可觀察支撐與回升時機，不宜恐慌殺出。"
        elif price_up:
            base_analysis = "上漲但量能萎縮，追價意願不足，常見於無量過高。"
            base_strategy = "提防回檔，不宜追高。"
            base_tone = "warning"
        else:
            base_analysis = "盤整中量縮，市場缺乏主流共識，多空都在等方向。"
            base_strategy = "空手可觀望，有倉位宜縮小操作節奏。"
    else:
        base_name = "量能平穩"
        base_tone = "warning"
        base_analysis = "成交量接近均量，未出現明顯量縮或量增，熱度中性。"
        base_strategy = "以價格結構與其他籌碼訊號為主，量能暫非主導。"

    # —— 二、均量線 / OBV ——
    above_vma20 = vol > vma20
    above_vma5 = vol > vma5 if vma5 > 0 else False
    lookback = d.tail(60)
    price_near_high = close >= float(lookback["Close"].max()) * 0.98
    obv_near_high = obv >= float(lookback["OBV"].max()) * 0.98
    price_near_low = close <= float(lookback["Close"].min()) * 1.02
    obv_near_low = obv <= float(lookback["OBV"].min()) * 1.02

    if (not price_near_high) and obv_near_high:
        obv_name = "OBV 領先創高"
        obv_tone = "buy"
        obv_analysis = "股價尚未明顯創高，但 OBV 已近高點，常見主力默默吸籌、量先行於價。"
        obv_strategy = "偏多關注，可搭配回檔量縮尋找進場節奏。"
    elif (not price_near_low) and obv_near_low:
        obv_name = "OBV 領先轉弱"
        obv_tone = "sell"
        obv_analysis = "股價未明顯破底，但 OBV 已近低點，人氣與資金流出偏先行。"
        obv_strategy = "提高警戒，避免在弱勢反彈末段進場。"
    elif price_near_high and obv_near_high:
        obv_name = "價量同步偏多"
        obv_tone = "buy"
        obv_analysis = "近兩月價格與 OBV 同處高檔區，資金與人氣大致同向。"
        obv_strategy = "趨勢偏多，仍留意爆量後的過熱風險。"
    elif price_near_low and obv_near_low:
        obv_name = "價量同步偏空"
        obv_tone = "sell"
        obv_analysis = "近兩月價格與 OBV 同處低檔區，賣壓與人氣退潮並存。"
        obv_strategy = "偏空或觀望，等量縮止跌再評估。"
    else:
        obv_name = "OBV 中性"
        obv_tone = "warning"
        obv_analysis = "近期 OBV 與價格未出現明顯領先背離。"
        obv_strategy = "以 VMA 帶量與否與量價配合為主。"

    if above_vma20:
        vma_note = "今日成交量站上 20 日均量（帶量），短線資金活躍度偏高。"
    else:
        vma_note = "今日成交量低於 20 日均量，短線資金活躍度偏弱。"

    # —— 三、量價配合 ——
    year = d.tail(min(240, len(d)))
    vol_max = float(year["Volume"].max() or 0)
    vol_min = float(year["Volume"].min() or 0)
    close_max = float(year["Close"].max() or 0)
    close_min = float(year["Close"].min() or 0)
    sky_vol = vol_max > 0 and vol >= vol_max * 0.95
    ground_vol = vol_max > vol_min and vol <= vol_min + (vol_max - vol_min) * 0.08
    sky_price = close_max > 0 and close >= close_max * 0.98
    ground_price = close_max > close_min and close <= close_min * 1.02

    if sky_vol and sky_price:
        pv_name = "天量天價"
        pv_tone = "sell"
        pv_analysis = "近一年高檔區出現歷史級巨量，常見散戶追高、主力趁機出貨，見高風險升高。"
        pv_strategy = "不宜追價；有獲利可減碼，空手觀望溢價與熱度退潮。"
    elif ground_vol and ground_price:
        pv_name = "地量地價"
        pv_tone = "buy"
        pv_analysis = "低檔區量能極度萎縮，殺盤力道近竭、籌碼相對乾淨，常是波段底部醞釀期。"
        pv_strategy = "可列入觀察／分批左側布局清單，等待止跌與量能回溫確認。"
    elif price_up and ratio20 >= 1.2:
        pv_name = "量價齊揚"
        pv_tone = "buy"
        pv_analysis = "價漲且明顯量增，買方追價意願強，屬較強勢的多頭攻擊訊號。"
        pv_strategy = "偏多操作，沿短線均線抱持；留意後續是否轉為爆量過熱。"
    elif price_up and ratio20 <= 0.8:
        pv_name = "價漲量縮（量價背離）"
        pv_tone = "warning"
        pv_analysis = "股價上漲但量能不足，追價無力，無量過高後回檔機率升高。"
        pv_strategy = "勿追高；若持有宜提高停利警戒。"
    elif price_down and ratio20 >= 1.2:
        pv_name = "價跌量增（量價背離）"
        pv_tone = "sell"
        pv_analysis = "下跌伴隨放量，恐慌或主力倒貨特徵明顯，賣方力道重。"
        pv_strategy = "避免抄底接刀，等量縮止跌再評估。"
    elif price_down and ratio20 <= 0.8:
        pv_name = "價跌量縮"
        pv_tone = "buy"
        pv_analysis = "回檔量縮，賣壓較輕，若位處多頭格局多屬健康整理。"
        pv_strategy = "可觀察支撐與均線，不宜因單日下跌過度恐慌。"
    else:
        pv_name = "量價中性"
        pv_tone = "warning"
        pv_analysis = "價格與量能尚未形成明確齊揚或背離訊號。"
        pv_strategy = "維持既有部位紀律，等待更清楚的量價組合。"

    return {
        "date": date_str,
        "volume": vol,
        "lots": lots,
        "vma5": vma5,
        "vma20": vma20,
        "ratio20": ratio20,
        "obv": obv,
        "close": close,
        "baseline": {
            "name": base_name,
            "tone": base_tone,
            "analysis": base_analysis,
            "strategy": base_strategy,
        },
        "vma_obv": {
            "name": obv_name,
            "tone": obv_tone,
            "analysis": f"{vma_note} {obv_analysis}",
            "strategy": obv_strategy,
            "above_vma5": above_vma5,
            "above_vma20": above_vma20,
        },
        "price_volume": {
            "name": pv_name,
            "tone": pv_tone,
            "analysis": pv_analysis,
            "strategy": pv_strategy,
        },
    }


def _volume_card_html(cat_title, block, extra_html=""):
    tone = block.get("tone") or "warning"
    return (
        f"<div class='twmc-vol-card tone-{html.escape(tone)}'>"
        f"<div class='vol-cat'>{_annotate_volume_terms(cat_title)}</div>"
        f"<div class='vol-name'>{_annotate_volume_terms(block.get('name') or '—')}</div>"
        f"{extra_html}"
        f"<div class='vol-block'><span class='lab'>解讀</span>"
        f"{_annotate_volume_terms(block.get('analysis') or '')}</div>"
        f"<div class='vol-block'><span class='lab'>策略</span>"
        f"{_annotate_volume_terms(block.get('strategy') or '')}</div>"
        f"</div>"
    )


# 交易量專有名詞：滑鼠懸浮說明（定義 + 舉例）
VOLUME_TERM_GLOSSARY = {
    "VMA20": (
        "成交量移動平均線（20 日）",
        "把最近 20 個交易日的成交量平均，用來判斷是否「帶量」。例：今日 8 萬張、VMA20 4 萬張 → 約為均量的 2 倍，屬明顯放量。",
    ),
    "VMA5": (
        "成交量移動平均線（5 日）",
        "近 5 日成交量平均，反映更短線的熱度。例：今日量低於 VMA5，常稱為短線量縮。",
    ),
    "VMA": (
        "成交量移動平均線（Volume Moving Average）",
        "把過去幾日成交量平均畫成線，用來定義真正的量增／量縮。例：量站上 VMA20 稱為「帶量」。",
    ),
    "OBV": (
        "能量潮（On-Balance Volume）",
        "上漲日把成交量加上去、下跌日減下來，累積成「人氣線」。例：股價還沒創新高，但 OBV 先創高，常解讀為資金暗中進場。",
    ),
    "帶量": (
        "成交量高於均量",
        "通常指量能站上 VMA20。例：上漲且帶量，較像真攻擊；上漲但不帶量，較像無量過高。",
    ),
    "相對均量": (
        "今日量 ÷ 20 日均量",
        "用來量化量增／量縮程度。例：150% 代表今日量約為均量的 1.5 倍。",
    ),
    "量縮": (
        "成交量明顯小於近期均量",
        "市場觀望、交投清淡。例：多頭回檔出現量縮，常代表賣壓減輕；盤整量縮則多半在等方向。",
    ),
    "量增": (
        "成交量明顯大於近期均量",
        "資金變活躍、換手增加。例：突破平台伴隨量增，較具攻擊意味。",
    ),
    "爆量": (
        "成交量暴增、遠高於均量",
        "市場分歧或資金大舉進出。例：爆量長紅偏攻擊；爆量長黑要小心出貨或恐慌殺盤。",
    ),
    "量價齊揚": (
        "價漲且量增",
        "最強勢的多頭量價組合之一。例：股價沿五日線上攻，同時量能持續放大。",
    ),
    "量價背離": (
        "價格與量能方向不一致",
        "常見兩種：價漲量縮（追價無力）、價跌量增（賣壓沉重）。例：創新高卻量能逐日萎縮，常是回檔警訊。",
    ),
    "價漲量縮": (
        "股價上漲但成交量縮小",
        "追價意願不足，俗稱無量過高。例：連續紅 K 但量能一天比一天少。",
    ),
    "價跌量增": (
        "股價下跌且成交量放大",
        "恐慌或主力倒貨特徵。例：長黑搭配遠高於均量的成交，不宜急著接刀。",
    ),
    "天量天價": (
        "高檔出現歷史級巨量",
        "常伴隨散戶追高、主力出貨。例：股價創一年新高當天，成交量也創一年新高。",
    ),
    "地量地價": (
        "低檔量能極度萎縮",
        "殺盤力道近竭、籌碼較乾淨。例：股價跌至波段低點，成交量也縮到近期最低水準。",
    ),
    "均量": (
        "一段期間的平均成交量",
        "實務上多用 VMA5／VMA20 代表。例：用今日量對照 20 日均量，判斷量增或量縮。",
    ),
}

_VOLUME_TERM_RE = re.compile(
    "|".join(re.escape(k) for k in sorted(VOLUME_TERM_GLOSSARY.keys(), key=len, reverse=True))
)


def _vol_tip_html(term):
    """單一專有名詞的懸浮說明 HTML。"""
    meta = VOLUME_TERM_GLOSSARY.get(term)
    if not meta:
        return html.escape(term)
    title, example = meta
    return (
        f'<span class="twmc-tip" tabindex="0">{html.escape(term)}'
        f'<span class="twmc-tip-box" role="tooltip">'
        f"<b>{html.escape(title)}</b>"
        f'<span class="ex">{html.escape(example)}</span>'
        f"</span></span>"
    )


def _annotate_volume_terms(text):
    """將文案中的專有名詞包成滑鼠懸浮提示。"""
    if text is None:
        return ""
    raw = str(text)
    if not raw:
        return ""
    parts = []
    last = 0
    for m in _VOLUME_TERM_RE.finditer(raw):
        parts.append(html.escape(raw[last:m.start()]))
        parts.append(_vol_tip_html(m.group(0)))
        last = m.end()
    parts.append(html.escape(raw[last:]))
    return "".join(parts)


def render_volume_analysis(df, title=None):
    """獨立區段：交易量三大類彙整。title 有值時才顯示小標題。"""
    if title:
        st.markdown(
            f"<div class='twmc-chips-h'>{html.escape(title)}</div>",
            unsafe_allow_html=True,
        )
    result = analyze_volume(df)
    if not result:
        st.info("成交量資料不足，無法判斷量價狀態（至少需約 25 個交易日）。")
        return

    metrics = (
        f"<div class='twmc-vol-metrics'>"
        f"<div><span>資料日</span><b>{html.escape(result['date'])}</b></div>"
        f"<div><span>成交量</span><b>{result['lots']:,.0f} 張</b></div>"
        f"<div><span>{_vol_tip_html('VMA5')}</span><b>{result['vma5'] / 1000:,.0f} 張</b></div>"
        f"<div><span>{_vol_tip_html('VMA20')}</span><b>{result['vma20'] / 1000:,.0f} 張</b></div>"
        f"<div><span>{_vol_tip_html('相對均量')}</span><b>{result['ratio20']:.0%}</b></div>"
        f"</div>"
    )
    vma = result["vma_obv"]
    flags = (
        f"<div class='vol-flags'>"
        f"<span class='{'on' if vma.get('above_vma5') else 'off'}'>"
        f"{'●' if vma.get('above_vma5') else '○'} 站上 {_vol_tip_html('VMA5')}</span>"
        f"<span class='{'on' if vma.get('above_vma20') else 'off'}'>"
        f"{'●' if vma.get('above_vma20') else '○'} 站上 {_vol_tip_html('VMA20')}（{_vol_tip_html('帶量')}）</span>"
        f"</div>"
    )
    cards = (
        _volume_card_html("一、基準狀態：量縮 vs 量增", result["baseline"])
        + _volume_card_html("二、均量線：VMA／OBV", result["vma_obv"], flags)
        + _volume_card_html("三、量價配合：真偽與轉折", result["price_volume"])
    )
    st.markdown(
        f"{metrics}<div class='twmc-vol-grid'>{cards}</div>",
        unsafe_allow_html=True,
    )


@st.cache_data(ttl=60 * 60)
def get_margin_data_2m(code):
    """取得過去兩個月的信用交易資料 (透過 FinMind API)"""
    end_date = datetime.today()
    start_date = end_date - timedelta(days=60)
    url = "https://api.finmindtrade.com/api/v4/data"
    params = {
        "dataset": "TaiwanStockMarginPurchaseShortSale",
        "data_id": code,
        "start_date": start_date.strftime("%Y-%m-%d"),
        "end_date": end_date.strftime("%Y-%m-%d")
    }
    try:
        res = requests.get(url, params=params, timeout=10).json()
        if "data" in res and res["data"]:
            df = pd.DataFrame(res["data"])
            # 確保型態
            for col in ['MarginPurchaseTodayBalance', 'MarginPurchaseLimit', 'ShortSaleTodayBalance', 'ShortSaleLimit']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
            
            res_df = pd.DataFrame()
            res_df['date'] = df['date']
            res_df['融資餘額'] = df.get('MarginPurchaseTodayBalance', 0)
            # 融資使用率 = 融資餘額 / 限額 * 100
            res_df['融資占比(%)'] = round((df.get('MarginPurchaseTodayBalance', 0) / df.get('MarginPurchaseLimit', 1).replace(0, 1)) * 100, 2)
            
            res_df['融券餘額'] = df.get('ShortSaleTodayBalance', 0)
            # 融券使用率 = 融券餘額 / 限額 * 100
            res_df['融券占比(%)'] = round((df.get('ShortSaleTodayBalance', 0) / df.get('ShortSaleLimit', 1).replace(0, 1)) * 100, 2)
            
            return res_df.sort_index(ascending=True) # ECharts 需要時間遞增
    except Exception:
        pass
    return pd.DataFrame()

NAME_MAP_FILE = Path(__file__).parent / "name_map.json"
NAME_MAP_MAX_AGE_DAYS = 7
FINMIND_DATA_URL = "https://api.finmindtrade.com/api/v4/data"


def _finmind_token():
    """FinMind token：環境變數 FINMIND_TOKEN 或 Streamlit secrets。"""
    token = str(os.environ.get("FINMIND_TOKEN") or "").strip()
    if token:
        return token
    try:
        return str(st.secrets.get("FINMIND_TOKEN", "") or "").strip()
    except Exception:
        return ""


def _finmind_request(dataset, data_id=None, start_date=None, end_date=None):
    """呼叫 FinMind API，回傳 (data, msg)。"""
    params = {"dataset": dataset}
    if data_id:
        params["data_id"] = str(data_id)
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    token = _finmind_token()
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        params["token"] = token
    try:
        response = requests.get(
            FINMIND_DATA_URL, params=params, headers=headers, timeout=20
        )
        payload = response.json()
        return payload.get("data") or [], str(payload.get("msg") or "")
    except Exception as exc:
        return [], str(exc)


def _finmind_data(dataset, data_id=None, start_date=None, end_date=None):
    """統一取得 FinMind 資料；失敗時回傳空串列，不改用其他資料源。"""
    data, _msg = _finmind_request(dataset, data_id, start_date, end_date)
    return data


def _fetch_name_map_from_finmind():
    """從 FinMind 股票清單建立中文名稱對照表。"""
    name_to_code, code_to_name = {}, {}
    for row in _finmind_data("TaiwanStockInfo"):
        code = str(row.get("stock_id") or "").strip().upper()
        name = str(row.get("stock_name") or "").strip()
        if code and name and is_stock_code(code):
            name_to_code[name] = code
            code_to_name[code] = name
    return name_to_code, code_to_name


def _load_name_map_file():
    if not NAME_MAP_FILE.exists():
        return None
    try:
        with open(NAME_MAP_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        if payload.get("source") != "FinMind":
            return None
        updated = datetime.fromisoformat(payload.get("updated_at", "1970-01-01"))
        age_days = (datetime.now() - updated).days
        name_to_code = payload.get("name_to_code", {})
        code_to_name = payload.get("code_to_name", {})
        if name_to_code and code_to_name:
            return name_to_code, code_to_name, age_days
    except Exception:
        pass
    return None


def _save_name_map_file(name_to_code, code_to_name):
    with open(NAME_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "source": "FinMind",
                "name_to_code": name_to_code,
                "code_to_name": code_to_name,
            },
            f,
            ensure_ascii=False,
        )


def load_name_map():
    """載入股票中文名稱對照表。

    優先讀本機 name_map.json（幾乎瞬間）；只有檔案不存在或超過 7 天，
    才會向 FinMind 更新清單，所以不要每次啟動都打。
    不 cache 回傳值，以免更新 name_map.json 後 Streamlit 還拿舊表。
    """
    cached = _load_name_map_file()
    if cached:
        name_to_code, code_to_name, age_days = cached
        if age_days < NAME_MAP_MAX_AGE_DAYS:
            return name_to_code, code_to_name

    name_to_code, code_to_name = _fetch_name_map_from_finmind()
    if name_to_code and code_to_name:
        _save_name_map_file(name_to_code, code_to_name)
        return name_to_code, code_to_name

    # 網路失敗時，退回本機舊檔（即使已過期）
    if cached:
        return cached[0], cached[1]
    return {}, {}


NAME_TO_CODE, CODE_TO_NAME = load_name_map()

@st.cache_data(ttl=60 * 60)
def get_chip_distribution_data(code, total_shares):
    """
    取得當下籌碼分布 (以張為單位)
    董監、外資、投信、自營商、融資、融券
    """
    end_date = datetime.today()
    url = "https://api.finmindtrade.com/api/v4/data"
    
    # 外資
    res_foreign = requests.get(url, params={"dataset": "TaiwanStockShareholding", "data_id": code, "start_date": (end_date - timedelta(days=30)).strftime("%Y-%m-%d")}).json()
    foreign_shares = 0
    if res_foreign.get("data"):
        foreign_shares = int(res_foreign["data"][-1].get("ForeignInvestmentShares", 0)) // 1000

    # 投信 & 自營商 (近一年累計買賣超當作持股估算)
    res_inst = requests.get(url, params={"dataset": "TaiwanStockInstitutionalInvestorsBuySell", "data_id": code, "start_date": (end_date - timedelta(days=365)).strftime("%Y-%m-%d")}).json()
    trust_shares = 0
    dealer_shares = 0
    if res_inst.get("data"):
        df_inst = pd.DataFrame(res_inst["data"])
        df_inst['net'] = df_inst['buy'] - df_inst['sell']
        df_grouped = df_inst.groupby('name')['net'].sum()
        trust_shares = int(df_grouped.get('Investment_Trust', 0)) // 1000
        dealer_shares = int(df_grouped.get('Dealer_self', 0) + df_grouped.get('Dealer_Hedging', 0) + df_grouped.get('Foreign_Dealer_Self', 0)) // 1000
        # 如果累計小於 0 則視為 0
        trust_shares = max(0, trust_shares)
        dealer_shares = max(0, dealer_shares)

    # 融資 & 融券
    res_margin = requests.get(url, params={"dataset": "TaiwanStockMarginPurchaseShortSale", "data_id": code, "start_date": (end_date - timedelta(days=10)).strftime("%Y-%m-%d")}).json()
    margin_shares = 0
    short_shares = 0
    if res_margin.get("data"):
        latest_margin = res_margin["data"][-1]
        margin_shares = int(latest_margin.get('MarginPurchaseTodayBalance', 0))
        short_shares = int(latest_margin.get('ShortSaleTodayBalance', 0))
        
    return {
        "外資": foreign_shares,
        "投信": trust_shares,
        "自營商": dealer_shares,
        "融資": margin_shares,
        "融券": short_shares
    }

@st.cache_data(ttl=60 * 60)
def get_chip_concentration_data(code, df, inst_df, total_shares, days=1):
    """計算指定天數的籌碼集中度指標（單列）。"""
    date_col = '日期' if '日期' in inst_df.columns else 'date'
    inst_df = inst_df.sort_values(date_col, ascending=False)

    vol = int(df['Volume'].tail(days).sum() / 1000) if not df.empty else 0
    net_buy_shares = inst_df['三大法人合計'].head(days).sum() if not inst_df.empty else 0
    net_buy = int(net_buy_shares / 1000)

    return pd.DataFrame([{
        "籌碼集中(張)": f"{net_buy:,}",
        "籌碼集中(%)": f"{round((net_buy / vol * 100), 2)}%" if vol else "0%",
        "成交量(張)": f"{vol:,}",
        "佔股本比重(%)": f"{round((net_buy_shares / total_shares * 100), 2)}%" if total_shares else "0%",
        "區間周轉率(%)": f"{round(((vol * 1000) / total_shares * 100), 2)}%" if total_shares else "0%"
    }])


def get_total_shares(code):
    url = "https://api.finmindtrade.com/api/v4/data"
    end_date = datetime.today()
    res = requests.get(url, params={"dataset": "TaiwanStockShareholding", "data_id": code, "start_date": (end_date - timedelta(days=30)).strftime("%Y-%m-%d")}).json()
    if res.get("data"):
        return res["data"][-1].get("NumberOfSharesIssued", 0)
    return 0


def _holding_level_min_shares(level):
    """解析 FinMind HoldingSharesLevel 的下限股數。"""
    text = str(level or "").replace(",", "").replace(" ", "").lower()
    if not text or "total" in text or "合計" in text or "總計" in text:
        return None
    nums = [int(x) for x in re.findall(r"\d+", text)]
    if not nums:
        return None
    if "超過" in text or "morethan" in text.replace(" ", "") or "以上" in text:
        return nums[0]
    return nums[0]


def _is_thousand_lot_level(level):
    """千張大戶：持股 >= 1,000 張（= 1,000,000 股）。"""
    minimum = _holding_level_min_shares(level)
    return minimum is not None and minimum >= 1_000_000


@st.cache_data(ttl=60 * 60)
def get_thousand_lot_holder_ratio(code):
    """FinMind 股權分級表加總千張大戶持股比例。

    資料集 TaiwanStockHoldingSharesPer 需進階權限；可設 FINMIND_TOKEN。
    """
    start = (datetime.today() - timedelta(days=180)).strftime("%Y-%m-%d")
    rows, msg = _finmind_request(
        "TaiwanStockHoldingSharesPer", code, start_date=start
    )
    if not rows:
        return {
            "ratio": None,
            "date": "",
            "people": None,
            "msg": msg or "查無資料",
            "levels": [],
        }
    latest_date = max(str(row.get("date") or "") for row in rows)
    latest = [row for row in rows if str(row.get("date") or "") == latest_date]
    selected = [
        row for row in latest
        if _is_thousand_lot_level(row.get("HoldingSharesLevel"))
    ]
    ratio = round(sum(float(row.get("percent") or 0) for row in selected), 2)
    people = sum(int(float(row.get("people") or 0)) for row in selected)
    return {
        "ratio": ratio,
        "date": latest_date,
        "people": people,
        "msg": "success",
        "levels": selected,
    }


def _chip_signed_html(val, suffix=""):
    """買超紅、賣超綠。"""
    try:
        num = float(val)
    except (TypeError, ValueError):
        return "—"
    if pd.isna(num):
        return "—"
    color = "#ef232a" if num > 0 else ("#14b143" if num < 0 else "#fafafa")
    return (
        f"<span style='color:{color}; font-weight:700'>"
        f"{num:,.0f}{html.escape(suffix)}</span>"
    )


def _style_buy_sell_df(df, columns):
    """DataFrame 買賣超欄位正紅負綠並置中。"""
    display = df.copy()
    for col in columns:
        if col in display.columns:
            display[col] = display[col].apply(
                lambda x: f"{int(float(x)):,}" if pd.notna(x) else "—"
            )

    def color_buy_sell(val):
        try:
            v = int(str(val).replace(",", ""))
            if v > 0:
                return "color: #ef232a; text-align: center; font-weight: 700;"
            if v < 0:
                return "color: #14b143; text-align: center; font-weight: 700;"
        except Exception:
            pass
        return "text-align: center;"

    subset = [c for c in columns if c in display.columns]
    styled = display.style
    if subset:
        styled = styled.map(color_buy_sell, subset=subset)
    return styled.set_properties(**{"text-align": "center"}).set_table_styles(
        [dict(selector="th", props=[("text-align", "center")])]
    )


def parse_mainforce_history_text(raw_text, fallback_code=None):
    """
    解析「主力動向歷史數據」文字表。
    回傳 dict: code, title, start, end, rows[...]
    """
    text = str(raw_text or "").replace("\r\n", "\n").replace("\r", "\n")
    code = str(fallback_code or "").strip().upper() or None
    title = ""
    start = ""
    end = ""
    head = re.search(
        r"【(?P<title>[^】]*?)(?P<code>\d{3,6}[A-Za-z]{0,2})】[^(\n]*\((?P<start>[^)\-]+)\s*[-–~～]\s*(?P<end>[^)]+)\)",
        text,
    )
    if head:
        title = (head.group("title") or "").strip()
        code = str(head.group("code") or code or "").strip().upper()
        start = str(head.group("start") or "").strip()
        end = str(head.group("end") or "").strip()

    rows = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("=") or line.startswith("-"):
            continue
        if "日期" in line and "買賣超" in line:
            continue
        if "|" not in line:
            continue
        parts = [p.strip().replace(",", "") for p in line.split("|")]
        if len(parts) < 5:
            continue
        if not re.match(r"^\d{4}/\d{1,2}/\d{1,2}$", parts[0]):
            continue
        try:
            rows.append({
                "date": parts[0].replace("/", "-"),
                "date_display": parts[0],
                "買賣超(張)": int(float(parts[1])),
                "家數差": int(float(parts[2])),
                "5日集中(%)": float(parts[3]),
                "20日集中(%)": float(parts[4]),
            })
        except (TypeError, ValueError):
            continue

    rows_sorted = sorted(rows, key=lambda r: r["date"], reverse=True)
    return {
        "code": code,
        "title": title,
        "start": start,
        "end": end,
        "rows": rows_sorted,
        "updated_at": now_taipei().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "manual_txt",
    }


def mainforce_store_path(code):
    code = str(code or "").strip().upper()
    MAINFORCE_DIR.mkdir(parents=True, exist_ok=True)
    return MAINFORCE_DIR / f"{code}.json"


def save_mainforce_history(code, payload):
    code = str(code or "").strip().upper()
    if not code:
        raise ValueError("缺少股票代號")
    data = dict(payload or {})
    data["code"] = code
    data["updated_at"] = now_taipei().strftime("%Y-%m-%d %H:%M:%S")
    path = mainforce_store_path(code)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)
    api.put_mainforce(code, data)
    return data


def load_mainforce_history(code):
    code = str(code or "").strip().upper()
    if api.logged_in():
        remote = api.get_mainforce(code)
        if isinstance(remote, dict):
            rows = remote.get("rows") or []
            if not isinstance(rows, list):
                rows = []
            remote["rows"] = rows
            remote["code"] = code
            return remote
    path = mainforce_store_path(code)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return None
        rows = raw.get("rows") or []
        if not isinstance(rows, list):
            rows = []
        raw["rows"] = rows
        raw["code"] = code
        return raw
    except Exception:
        return None


def upsert_mainforce_row(code, *, trade_date, net_lots, house_diff, conc5, conc20, title=None):
    """
    單筆新增／覆蓋同日主力動向。
    trade_date: date / datetime / 'YYYY-MM-DD' / 'YYYY/MM/DD'
    """
    code = str(code or "").strip().upper()
    if not code:
        raise ValueError("缺少股票代號")

    if hasattr(trade_date, "strftime"):
        date_iso = trade_date.strftime("%Y-%m-%d")
    else:
        raw = str(trade_date or "").strip().replace("/", "-")
        date_iso = raw[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_iso):
        raise ValueError("日期格式需為 YYYY-MM-DD")
    y, m, d = date_iso.split("-")
    date_display = f"{y}/{int(m):02d}/{int(d):02d}"

    new_row = {
        "date": date_iso,
        "date_display": date_display,
        "買賣超(張)": int(net_lots),
        "家數差": int(house_diff),
        "5日集中(%)": float(conc5),
        "20日集中(%)": float(conc20),
    }

    existing = load_mainforce_history(code)
    if existing is None:
        existing = {
            "code": code,
            "title": title or CODE_TO_NAME.get(code, "") or "",
            "start": "",
            "end": "",
            "rows": [],
            "source": "manual_entry",
        }
    else:
        existing = dict(existing)
        if title:
            existing["title"] = title
        elif not existing.get("title"):
            existing["title"] = CODE_TO_NAME.get(code, "") or ""

    rows = [r for r in (existing.get("rows") or []) if str(r.get("date") or "") != date_iso]
    rows.append(new_row)
    rows = sorted(rows, key=lambda r: r.get("date") or "", reverse=True)
    existing["rows"] = rows
    if rows:
        dates = [r["date"] for r in rows if r.get("date")]
        existing["start"] = min(dates).replace("-", "/")
        existing["end"] = max(dates).replace("-", "/")
    existing["source"] = existing.get("source") or "manual_entry"
    return save_mainforce_history(code, existing)


def mainforce_history_df(payload):
    rows = (payload or {}).get("rows") or []
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    if "date_display" in df.columns:
        df = df.rename(columns={"date_display": "日期"})
    elif "date" in df.columns:
        df["日期"] = df["date"].astype(str).str.replace("-", "/", regex=False)
    cols = ["日期", "買賣超(張)", "家數差", "5日集中(%)", "20日集中(%)"]
    keep = [c for c in cols if c in df.columns]
    return df[keep] if keep else df


def _gemini_mainforce_snapshot(code):
    """濃縮主力動向給 Gemini（有資料才回傳）。"""
    payload = load_mainforce_history(code)
    if not payload or not (payload.get("rows") or []):
        return None
    rows = payload["rows"]
    latest = rows[0]
    last5 = rows[:5]
    last20 = rows[:20]
    sum5 = sum(int(r.get("買賣超(張)") or 0) for r in last5)
    sum20 = sum(int(r.get("買賣超(張)") or 0) for r in last20)
    inst_df = get_institutional_data_2m(code)
    hist_df = get_finmind_price_history(
        code,
        (datetime.today() - timedelta(days=120)).strftime("%Y-%m-%d"),
    )
    lights = classify_mainforce_lights(payload, inst_df=inst_df, hist_df=hist_df)
    pb = lights.get("playbook") or {}
    return {
        "資料來源": "使用者匯入主力動向歷史",
        "標的": payload.get("code") or code,
        "名稱": payload.get("title") or CODE_TO_NAME.get(code, ""),
        "資料區間": f"{payload.get('start') or '—'} ~ {payload.get('end') or '—'}",
        "更新時間": payload.get("updated_at"),
        "燈號判讀": {
            "主燈": (lights.get("primary") or {}).get("label"),
            "階段": (lights.get("primary") or {}).get("stage"),
            "理由": (lights.get("primary") or {}).get("reason"),
            "法人彙整": lights.get("inst_summary"),
            "主力快查": {
                "行為": pb.get("行為"),
                "價量狀態": pb.get("價量狀態"),
                "家數差": pb.get("家數差"),
                "實戰定調": pb.get("定調"),
                "應對策略": pb.get("策略"),
            } if pb else None,
            "觸發訊號": [
                {"名稱": s.get("label"), "階段": s.get("stage"), "理由": s.get("reason")}
                for s in (lights.get("signals") or [])
            ],
        },
        "最新一日": {
            "日期": latest.get("date_display") or latest.get("date"),
            "買賣超(張)": latest.get("買賣超(張)"),
            "家數差": latest.get("家數差"),
            "5日集中(%)": latest.get("5日集中(%)"),
            "20日集中(%)": latest.get("20日集中(%)"),
        },
        "近5日買賣超合計(張)": sum5,
        "近20日買賣超合計(張)": sum20,
        "近15日明細": [
            {
                "日期": r.get("date_display") or r.get("date"),
                "買賣超(張)": r.get("買賣超(張)"),
                "家數差": r.get("家數差"),
                "5日集中(%)": r.get("5日集中(%)"),
                "20日集中(%)": r.get("20日集中(%)"),
            }
            for r in rows[:15]
        ],
        "說明": "主力為匯入數據；燈號彙整 FinMind 外資／投信與價量快查定調，屬啟發式規則。",
    }


def _mf_median_abs(values):
    vals = sorted(abs(float(v)) for v in values if v is not None)
    if not vals:
        return 500.0
    mid = len(vals) // 2
    if len(vals) % 2:
        return float(vals[mid])
    return (vals[mid - 1] + vals[mid]) / 2.0


def _mf_streak(rows, pred):
    n = 0
    for row in rows:
        if pred(row):
            n += 1
        else:
            break
    return n


def _inst_days_lots(inst_df):
    """法人日資料（新→舊），單位轉成張（FinMind 為股）。"""
    if inst_df is None or getattr(inst_df, "empty", True):
        return []
    out = []
    for _, row in inst_df.iterrows():
        out.append({
            "date": str(row.get("date") or ""),
            "foreign": float(row.get("外資買賣超") or 0) / 1000.0,
            "trust": float(row.get("投信買賣超") or 0) / 1000.0,
        })
    return out


# 主力進出貨快查表（實戰定調／應對策略）
MAINFORCE_PLAYBOOK = {
    "連續買進": {
        "行為": "連續買進",
        "價量預設": "量縮盤整／緩漲",
        "家數差預設": "負（集中）",
        "定調": "波段建倉",
        "策略": "納入自選，右側突破時跟單",
        "stage": 1,
        "tone": "build",
        "emoji": "🟢",
    },
    "單日大買": {
        "行為": "單日大買",
        "價量預設": "爆量長紅突破",
        "家數差預設": "負（集中）",
        "定調": "攻擊點火",
        "策略": "追強買進（需防隔日沖倒貨）",
        "stage": 2,
        "tone": "attack",
        "emoji": "🚀",
    },
    "量縮小賣": {
        "行為": "量縮小賣",
        "價量預設": "跌破短均線但無量",
        "家數差預設": "不明顯",
        "定調": "健康洗盤",
        "策略": "支撐不破偏多續抱／左側低接",
        "stage": 3,
        "tone": "wash",
        "emoji": "🟡",
    },
    "高檔大賣": {
        "行為": "高檔大賣",
        "價量預設": "高檔爆天量收黑",
        "家數差預設": "正（發散）",
        "定調": "主力出貨",
        "策略": "絕對警戒，立刻停損／停利",
        "stage": 4,
        "tone": "danger",
        "emoji": "🚨",
    },
    "利多大賣": {
        "行為": "利多大賣",
        "價量預設": "利多見報，開高走低",
        "家數差預設": "正（發散）",
        "定調": "利多結帳",
        "策略": "嚴禁追高，持股者跟著出脫",
        "stage": 4,
        "tone": "danger",
        "emoji": "🚨",
    },
    "連續賣出": {
        "行為": "連續賣出",
        "價量預設": "均線下彎，陰跌不斷",
        "家數差預設": "正（發散）",
        "定調": "徹底棄守",
        "策略": "刪除自選，嚴禁向下接刀攤平",
        "stage": 4,
        "tone": "danger",
        "emoji": "🚨",
    },
}


def _price_volume_snapshot(hist_df):
    """從日 K 粗估價量狀態（供主力快查對照）。"""
    empty = {
        "ok": False,
        "vol_ratio": None,
        "below_ma5": False,
        "ma_bear": False,
        "long_red": False,
        "close_black": False,
        "open_high_fade": False,
        "near_high": False,
        "slow_up": False,
        "shrink_vol": False,
        "explode_vol": False,
        "label": "價量資料不足",
    }
    if hist_df is None or getattr(hist_df, "empty", True):
        return empty
    df = hist_df.copy()
    if "Close" not in df.columns or "Volume" not in df.columns:
        return empty
    df = df.sort_index()
    if len(df) < 6:
        return empty
    last = df.iloc[-1]
    prev = df.iloc[-2]
    close = float(last["Close"])
    open_ = float(last["Open"]) if "Open" in df.columns else close
    high = float(last["High"]) if "High" in df.columns else close
    low = float(last["Low"]) if "Low" in df.columns else close
    vol = float(last["Volume"] or 0)
    vma20 = float(df["Volume"].tail(20).mean() or 0) or 1.0
    vol_ratio = vol / vma20
    ma5 = float(df["Close"].tail(5).mean())
    ma20 = float(df["Close"].tail(20).mean()) if len(df) >= 20 else ma5
    ma5_prev = float(df["Close"].iloc[-6:-1].mean()) if len(df) >= 6 else ma5
    range_ = max(high - low, 1e-9)
    body = close - open_
    long_red = body > 0 and abs(body) >= range_ * 0.55 and close >= prev["Close"]
    close_black = close < open_
    open_high_fade = open_ >= float(prev["Close"]) * 1.01 and close_black and close <= (open_ + high) / 2
    lookback = df["Close"].tail(60) if len(df) >= 60 else df["Close"]
    near_high = close >= float(lookback.max()) * 0.97
    slow_up = close > float(df["Close"].iloc[-6]) and vol_ratio < 1.15
    shrink_vol = vol_ratio < 0.75
    explode_vol = vol_ratio >= 2.0
    below_ma5 = close < ma5
    ma_bear = ma5 < ma20 and ma5 < ma5_prev
    if explode_vol and long_red:
        label = f"爆量長紅（量比 {vol_ratio:.1f}x）"
    elif explode_vol and close_black and near_high:
        label = f"高檔爆量收黑（量比 {vol_ratio:.1f}x）"
    elif open_high_fade:
        label = f"開高走低收黑（量比 {vol_ratio:.1f}x）"
    elif shrink_vol and below_ma5:
        label = f"跌破短均但無量（量比 {vol_ratio:.1f}x）"
    elif ma_bear and close_black:
        label = f"均線下彎陰跌（量比 {vol_ratio:.1f}x）"
    elif slow_up or (shrink_vol and close >= open_):
        label = f"量縮盤整／緩漲（量比 {vol_ratio:.1f}x）"
    else:
        label = f"量比 {vol_ratio:.1f}x｜收盤{'紅' if close >= open_ else '黑'}"
    return {
        "ok": True,
        "vol_ratio": vol_ratio,
        "below_ma5": below_ma5,
        "ma_bear": ma_bear,
        "long_red": bool(long_red),
        "close_black": bool(close_black),
        "open_high_fade": bool(open_high_fade),
        "near_high": bool(near_high),
        "slow_up": bool(slow_up),
        "shrink_vol": bool(shrink_vol),
        "explode_vol": bool(explode_vol),
        "label": label,
    }


def resolve_mainforce_playbook(payload, hist_df=None):
    """
    依快查表六種主力行為，對照籌碼＋價量，產出「實戰定調／應對策略」指標。
    """
    rows = list((payload or {}).get("rows") or [])
    if len(rows) < 3:
        return None
    nets = [int(r.get("買賣超(張)") or 0) for r in rows]
    diffs = [int(r.get("家數差") or 0) for r in rows]
    abs_med = max(300.0, _mf_median_abs(nets))
    large = max(1500.0, abs_med * 2.0)
    nuke = max(3000.0, abs_med * 3.0)
    small = max(250.0, abs_med * 0.7)
    latest_net = nets[0]
    latest_diff = diffs[0]
    buy_streak = _mf_streak(rows, lambda r: int(r.get("買賣超(張)") or 0) > 0)
    sell_streak = _mf_streak(rows, lambda r: int(r.get("買賣超(張)") or 0) < 0)
    small_buy_streak = _mf_streak(
        rows, lambda r: 0 < int(r.get("買賣超(張)") or 0) <= small * 1.3
    )
    pv = _price_volume_snapshot(hist_df)
    house_txt = (
        f"負（集中 {latest_diff:+}）" if latest_diff < 0
        else (f"正（發散 {latest_diff:+}）" if latest_diff > 0 else "不明顯（0）")
    )

    key = None
    # 優先序：出貨類 → 攻擊 → 建倉 → 洗盤
    if latest_net <= -large and latest_diff > 0 and pv.get("open_high_fade"):
        key = "利多大賣"
    elif latest_net <= -nuke or (
        latest_net <= -large and latest_diff > 0 and (pv.get("explode_vol") or pv.get("near_high"))
    ):
        key = "高檔大賣"
    elif sell_streak >= 5 or (sell_streak >= 3 and (pv.get("ma_bear") or latest_diff > 0)):
        key = "連續賣出"
    elif latest_net >= large and (latest_diff <= 0 or pv.get("long_red") or pv.get("explode_vol")):
        key = "單日大買"
    elif small_buy_streak >= 5 or (buy_streak >= 5 and latest_diff < 0):
        key = "連續買進"
    elif -small * 1.3 <= latest_net < 0 and (
        abs(latest_diff) < max(60, abs_med * 0.15) or pv.get("shrink_vol")
    ):
        key = "量縮小賣"
    elif buy_streak >= 3 and latest_diff < 0:
        key = "連續買進"
    elif latest_net < 0 and latest_diff > 0:
        key = "連續賣出" if sell_streak >= 2 else "高檔大賣"
    elif latest_net > 0:
        key = "連續買進" if buy_streak >= 2 else "單日大買"
    else:
        return None

    base = dict(MAINFORCE_PLAYBOOK[key])
    px_label = pv.get("label") if pv.get("ok") else base["價量預設"]
    # 若實測價量與預設明顯不符，仍顯示實測並註記
    return {
        "行為": base["行為"],
        "價量狀態": px_label,
        "價量預設": base["價量預設"],
        "家數差": house_txt,
        "家數差預設": base["家數差預設"],
        "定調": base["定調"],
        "策略": base["策略"],
        "stage": base["stage"],
        "tone": base["tone"],
        "emoji": base["emoji"],
        "match_key": key,
    }


# 主燈訊號 → 快查表行為（讓快查與主燈同一套定調）
_SIGNAL_TO_PLAYBOOK_KEY = {
    "dump_nuke": "高檔大賣",
    "dump_diverge": "高檔大賣",
    "dump_news_like": "利多大賣",
    "dump_drip": "連續賣出",
    "dump_inst_sync": "連續賣出",
    "dump_foreign_lead": "連續賣出",
    "mild_risk": "連續賣出",
    "attack_ignite": "單日大買",
    "attack_resonance": "單日大買",
    "attack_push": "單日大買",
    "build_drip": "連續買進",
    "build_foreign_drip": "連續買進",
    "build_trap": "連續買進",
    "mild_build": "連續買進",
    "wash_healthy": "量縮小賣",
    "wash_churn": "量縮小賣",
    "wash_diverge": "量縮小賣",
}


def _playbook_from_key(key, payload, hist_df, behavior_override=None):
    """依快查表 key 組出顯示用 playbook（含實測價量／家數差）。"""
    if key not in MAINFORCE_PLAYBOOK:
        return None
    rows = list((payload or {}).get("rows") or [])
    latest_diff = int(rows[0].get("家數差") or 0) if rows else 0
    house_txt = (
        f"負（集中 {latest_diff:+}）" if latest_diff < 0
        else (f"正（發散 {latest_diff:+}）" if latest_diff > 0 else "不明顯（0）")
    )
    pv = _price_volume_snapshot(hist_df)
    base = dict(MAINFORCE_PLAYBOOK[key])
    px_label = pv.get("label") if pv.get("ok") else base["價量預設"]
    return {
        "行為": behavior_override or base["行為"],
        "價量狀態": px_label,
        "價量預設": base["價量預設"],
        "家數差": house_txt,
        "家數差預設": base["家數差預設"],
        "定調": base["定調"],
        "策略": base["策略"],
        "stage": base["stage"],
        "tone": base["tone"],
        "emoji": base["emoji"],
        "match_key": key,
    }


def _playbook_aligned_to_primary(primary, payload, hist_df, fallback_playbook):
    """快查表跟主燈對齊；無對應時才用獨立快查結果。"""
    pid = (primary or {}).get("id") or ""
    if pid.startswith("pb_") and fallback_playbook:
        return fallback_playbook
    key = _SIGNAL_TO_PLAYBOOK_KEY.get(pid)
    if not key:
        return fallback_playbook
    override = None
    if pid == "build_trap":
        override = "假跌破真大買"
    elif pid == "attack_resonance":
        override = "土洋連續共振"
    elif pid == "wash_diverge":
        override = "主力／法人背離"
    return _playbook_from_key(key, payload, hist_df, behavior_override=override)


def classify_mainforce_lights(payload, inst_df=None, hist_df=None):
    """
    依四階段主力劇本做啟發式燈號；彙整匯入主力＋FinMind 外資／投信＋價量快查。
    回傳 {primary, signals, stages_status, inst_summary, playbook}
    """
    rows = list((payload or {}).get("rows") or [])
    empty = {
        "primary": {
            "id": "watch",
            "stage": 0,
            "label": "觀察中",
            "emoji": "⚪",
            "tone": "neutral",
            "reason": "資料不足，暫不判斷。",
        },
        "signals": [],
        "stages_status": {
            1: False,
            2: False,
            3: False,
            4: False,
        },
        "inst_summary": "法人資料暫無，燈號僅依主力動向。",
        "playbook": None,
    }
    if len(rows) < 3:
        return empty

    nets = [int(r.get("買賣超(張)") or 0) for r in rows]
    diffs = [int(r.get("家數差") or 0) for r in rows]
    c5s = [float(r.get("5日集中(%)") or 0) for r in rows]
    abs_med = max(300.0, _mf_median_abs(nets))
    large = max(1500.0, abs_med * 2.0)
    nuke = max(3000.0, abs_med * 3.0)
    small = max(250.0, abs_med * 0.7)

    latest_net = nets[0]
    latest_diff = diffs[0]
    latest_c5 = c5s[0]
    prev_c5 = c5s[1] if len(c5s) > 1 else latest_c5

    buy_streak = _mf_streak(rows, lambda r: int(r.get("買賣超(張)") or 0) > 0)
    sell_streak = _mf_streak(rows, lambda r: int(r.get("買賣超(張)") or 0) < 0)
    neg_house = _mf_streak(rows, lambda r: int(r.get("家數差") or 0) < 0)
    pos_house = _mf_streak(rows, lambda r: int(r.get("家數差") or 0) > 0)
    small_buy_streak = _mf_streak(
        rows,
        lambda r: 0 < int(r.get("買賣超(張)") or 0) <= small * 1.3,
    )

    # 近 4 日買賣交錯
    flips = 0
    for i in range(min(3, len(nets) - 1)):
        if nets[i] == 0 or nets[i + 1] == 0:
            continue
        if (nets[i] > 0) != (nets[i + 1] > 0):
            flips += 1

    pv = _price_volume_snapshot(hist_df)
    playbook = resolve_mainforce_playbook(payload, hist_df=hist_df)

    inst_days = _inst_days_lots(inst_df)
    foreign_buy_streak = _mf_streak(inst_days, lambda d: d["foreign"] > 0)
    trust_buy_streak = _mf_streak(inst_days, lambda d: d["trust"] > 0)
    sync_buy_streak = _mf_streak(
        inst_days, lambda d: d["foreign"] > 0 and d["trust"] > 0
    )
    sync_sell_streak = _mf_streak(
        inst_days, lambda d: d["foreign"] < 0 and d["trust"] < 0
    )
    foreign_sell_streak = _mf_streak(inst_days, lambda d: d["foreign"] < 0)
    f3 = sum(d["foreign"] for d in inst_days[:3]) if inst_days else 0.0
    t3 = sum(d["trust"] for d in inst_days[:3]) if inst_days else 0.0
    f1 = inst_days[0]["foreign"] if inst_days else 0.0
    t1 = inst_days[0]["trust"] if inst_days else 0.0
    if inst_days:
        inst_summary = (
            f"外資近3日 {f3:+,.0f} 張｜投信近3日 {t3:+,.0f} 張｜"
            f"土洋連買 {sync_buy_streak} 日／連賣 {sync_sell_streak} 日"
        )
    else:
        inst_summary = empty["inst_summary"]

    signals = []

    # —— 階段四：出貨（優先警示）——
    if latest_net <= -nuke and (latest_diff > 0 or latest_c5 < prev_c5 - 2):
        signals.append({
            "id": "dump_nuke",
            "stage": 4,
            "label": "高檔爆量大賣",
            "emoji": "🚨",
            "tone": "danger",
            "reason": f"最新賣超 {latest_net:,} 張，家數差 {latest_diff:+}，集中度惡化，偏倒貨逃命。",
            "priority": 100,
        })
    if sell_streak >= 5 and sum(nets[:min(5, sell_streak)]) < -small:
        signals.append({
            "id": "dump_drip",
            "stage": 4,
            "label": "連續小賣棄養",
            "emoji": "🚨",
            "tone": "danger",
            "reason": f"連續 {sell_streak} 日賣超，偏長線溫水出貨／棄養。",
            "priority": 90,
        })
    if latest_net <= -large and pos_house >= 1 and (
        latest_c5 < -3 or pv.get("open_high_fade")
    ):
        is_news_like = bool(pv.get("open_high_fade"))
        signals.append({
            "id": "dump_news_like" if is_news_like else "dump_diverge",
            "stage": 4,
            "label": "利多大賣結帳" if is_news_like else "大賣＋籌碼發散",
            "emoji": "🚨",
            "tone": "danger",
            "reason": (
                f"開高走低＋大賣 {latest_net:,} 張、家數差為正，偏利多結帳。"
                if is_news_like
                else f"大賣 {latest_net:,} 張且家數差為正、5日集中 {latest_c5:+.2f}%，偏結帳／倒貨。"
            ),
            "priority": 96 if is_news_like else 95,
        })
    if sync_sell_streak >= 3:
        signals.append({
            "id": "dump_inst_sync",
            "stage": 4,
            "label": "土洋同步撤退",
            "emoji": "🚨",
            "tone": "danger",
            "reason": (
                f"外資與投信連續 {sync_sell_streak} 日同步賣超"
                f"（近3日外資 {f3:+,.0f}／投信 {t3:+,.0f} 張），偏法人結帳。"
            ),
            "priority": 92,
        })
    elif foreign_sell_streak >= 5 and latest_net < 0:
        signals.append({
            "id": "dump_foreign_lead",
            "stage": 4,
            "label": "外資連賣主導",
            "emoji": "🚨",
            "tone": "danger",
            "reason": f"外資連續 {foreign_sell_streak} 日賣超且主力亦賣，偏提款／棄養。",
            "priority": 86,
        })

    # —— 階段一：建倉 ——
    if small_buy_streak >= 5 and neg_house >= 3:
        reason = f"連續 {small_buy_streak} 日小買，家數差連負，偏溫水建倉。"
        if foreign_buy_streak >= 5 or trust_buy_streak >= 5:
            reason += (
                f" 法人亦偏買（外資連買 {foreign_buy_streak}／投信連買 {trust_buy_streak}）。"
            )
        if pv.get("slow_up") or pv.get("shrink_vol"):
            reason += f" 價量：{pv.get('label')}。"
        signals.append({
            "id": "build_drip",
            "stage": 1,
            "label": "連續小買建倉",
            "emoji": "🟢",
            "tone": "build",
            "reason": reason,
            "priority": 55 + (8 if foreign_buy_streak >= 5 or trust_buy_streak >= 5 else 0),
        })
    prev_pressure = len(nets) > 2 and sum(nets[1:4]) < 0
    if latest_net >= large and latest_diff < 0 and prev_pressure:
        signals.append({
            "id": "build_trap",
            "stage": 1,
            "label": "假跌破真大買",
            "emoji": "🟢",
            "tone": "build",
            "reason": f"近幾日偏賣後，今日大買 {latest_net:,} 張且家數差仍負，偏破底吃貨。",
            "priority": 70,
        })
    if (
        foreign_buy_streak >= 5
        and trust_buy_streak >= 3
        and sync_buy_streak < 3
        and latest_net >= 0
    ):
        signals.append({
            "id": "build_foreign_drip",
            "stage": 1,
            "label": "外資連續吃貨",
            "emoji": "🟢",
            "tone": "build",
            "reason": (
                f"外資連買 {foreign_buy_streak} 日、投信連買 {trust_buy_streak} 日，"
                "尚未達土洋大共振，偏法人默默建倉。"
            ),
            "priority": 58,
        })

    # —— 階段二：攻擊 ——
    if sync_buy_streak >= 3:
        signals.append({
            "id": "attack_resonance",
            "stage": 2,
            "label": "土洋連續共振",
            "emoji": "🚀",
            "tone": "attack",
            "reason": (
                f"外資與投信連續 {sync_buy_streak} 日同步買超"
                f"（近3日外資 {f3:+,.0f}／投信 {t3:+,.0f} 張），右側最強推土機。"
            ),
            "priority": 88,
        })
    if latest_net >= large and not prev_pressure:
        pri = 75
        reason = f"單日買超 {latest_net:,} 張，偏攻擊點火（仍需對照是否隔日沖）。"
        if f1 > 0 and t1 > 0:
            pri = 82
            reason = (
                f"主力單日大買 {latest_net:,} 張，且當日外資／投信同向買超"
                f"（{f1:+,.0f}／{t1:+,.0f} 張），點火可信度較高。"
            )
        elif f1 < 0 and t1 < 0:
            pri = 62
            reason = (
                f"主力單日大買 {latest_net:,} 張，但外資／投信當日賣超"
                f"（{f1:+,.0f}／{t1:+,.0f} 張），需防隔日沖或對倒雜訊。"
            )
        if pv.get("explode_vol") or pv.get("long_red"):
            pri += 4
            reason += f" 價量：{pv.get('label')}。"
        signals.append({
            "id": "attack_ignite",
            "stage": 2,
            "label": "單日大買點火",
            "emoji": "🚀",
            "tone": "attack",
            "reason": reason,
            "priority": pri,
        })
    if buy_streak >= 3 and sum(nets[:3]) >= max(2500.0, large * 1.5):
        signals.append({
            "id": "attack_push",
            "stage": 2,
            "label": "連續大買推升",
            "emoji": "🚀",
            "tone": "attack",
            "reason": f"近 {buy_streak} 日連續買超，三日合計 {sum(nets[:3]):,} 張，偏多頭攻擊。",
            "priority": 72 + (6 if sync_buy_streak >= 2 else 0),
        })

    # —— 階段三：洗盤 ——
    if -small <= latest_net < 0 and abs(latest_diff) < max(60, abs_med * 0.15):
        reason = f"小賣 {latest_net:,} 張、家數差波動不大，偏健康洗盤（仍需看支撐）。"
        if f3 > 0 or t3 > 0:
            reason += f" 法人近3日仍偏買（外資 {f3:+,.0f}／投信 {t3:+,.0f}），洗盤可信度較高。"
        if pv.get("shrink_vol") or pv.get("below_ma5"):
            reason += f" 價量：{pv.get('label')}。"
        signals.append({
            "id": "wash_healthy",
            "stage": 3,
            "label": "量縮小賣洗盤",
            "emoji": "🟡",
            "tone": "wash",
            "reason": reason,
            "priority": 45,
        })
    if flips >= 3 and max(abs(n) for n in nets[:4]) >= large * 0.8:
        signals.append({
            "id": "wash_churn",
            "stage": 3,
            "label": "高檔買賣交錯",
            "emoji": "🟡",
            "tone": "wash",
            "reason": "近幾日大買大賣交錯，籌碼鬆動，操作難度升高。",
            "priority": 60,
        })
    if latest_net > large * 0.8 and (f1 < 0 or t1 < 0) and sync_buy_streak == 0:
        signals.append({
            "id": "wash_diverge",
            "stage": 3,
            "label": "主力／法人背離",
            "emoji": "🟡",
            "tone": "wash",
            "reason": (
                f"主力偏買但法人不同向（當日外資 {f1:+,.0f}／投信 {t1:+,.0f} 張），"
                "籌碼訊號分歧，宜縮小部位。"
            ),
            "priority": 65,
        })

    # 去重同 id
    uniq = {}
    for sig in signals:
        prev = uniq.get(sig["id"])
        if prev is None or sig["priority"] > prev["priority"]:
            uniq[sig["id"]] = sig
    signals = sorted(uniq.values(), key=lambda s: s["priority"], reverse=True)

    if signals:
        primary = dict(signals[0])
    else:
        # 沒明確訊號：用最新淨流向／法人給中性燈
        if sync_buy_streak >= 2 or (latest_net > 0 and foreign_buy_streak >= 3):
            primary = {
                "id": "mild_build",
                "stage": 1,
                "label": "偏建倉觀察",
                "emoji": "🟢",
                "tone": "build",
                "reason": "主力或法人偏買，但連續性／力道尚不足以定調攻擊。",
                "priority": 22,
            }
        elif latest_net > 0 and neg_house >= 1:
            primary = {
                "id": "mild_build",
                "stage": 1,
                "label": "偏建倉觀察",
                "emoji": "🟢",
                "tone": "build",
                "reason": "最新為買超且家數差偏集中，但連續性／力道尚不足以定調。",
                "priority": 20,
            }
        elif sync_sell_streak >= 2 or (latest_net < 0 and pos_house >= 1):
            primary = {
                "id": "mild_risk",
                "stage": 4,
                "label": "偏發散警戒",
                "emoji": "🚨",
                "tone": "danger",
                "reason": "主力或法人偏賣／家數差偏正，需提高警戒但未達爆量倒貨門檻。",
                "priority": 25,
            }
        elif playbook:
            primary = {
                "id": f"pb_{playbook['match_key']}",
                "stage": playbook["stage"],
                "label": playbook["定調"],
                "emoji": playbook["emoji"],
                "tone": playbook["tone"],
                "reason": f"依快查表定調「{playbook['定調']}」：{playbook['策略']}",
                "priority": 18,
            }
        else:
            primary = empty["primary"]

    # 快查表與主燈對齊，避免「主燈建倉、快查攻擊」互相打架
    playbook = _playbook_aligned_to_primary(primary, payload, hist_df, playbook)

    # 四階段燈：只亮主燈對應那一盞（細部訊號仍可在 expander 看）
    stages_status = {1: False, 2: False, 3: False, 4: False}
    primary_stage = int(primary.get("stage") or 0)
    if primary_stage in stages_status:
        stages_status[primary_stage] = True

    return {
        "primary": primary,
        "signals": signals[:5],
        "stages_status": stages_status,
        "inst_summary": inst_summary,
        "playbook": playbook,
    }


def _mainforce_lights_html(lights):
    """四階段燈號列＋主力快查定調＋主判讀＋法人彙整。"""
    primary = (lights or {}).get("primary") or {}
    status = (lights or {}).get("stages_status") or {}
    pb = (lights or {}).get("playbook") or {}
    stages = [
        (1, "建倉吃貨", "🟢", "build", status.get(1)),
        (2, "攻擊點火", "🚀", "attack", status.get(2)),
        (3, "洗盤震盪", "🟡", "wash", status.get(3)),
        (4, "出貨結帳", "🚨", "danger", status.get(4)),
    ]
    pills = []
    for _num, name, emoji, tone, on in stages:
        cls = f"twmc-mf-pill twmc-mf-{tone}" + (" is-on" if on else " is-off")
        pills.append(
            f"<span class='{cls}'>{emoji} {html.escape(name)}</span>"
        )
    reason = html.escape(str(primary.get("reason") or ""))
    label = html.escape(str(primary.get("label") or "觀察中"))
    emoji = html.escape(str(primary.get("emoji") or "⚪"))
    inst_line = html.escape(str((lights or {}).get("inst_summary") or ""))

    pb_html = ""
    if pb:
        tone = html.escape(str(pb.get("tone") or "neutral"))
        pb_html = (
            f"<div class='twmc-mf-playbook twmc-mf-pb-{tone}'>"
            f"<div class='twmc-mf-pb-title'>"
            f"{html.escape(str(pb.get('emoji') or ''))} 主力快查｜"
            f"{html.escape(str(pb.get('行為') or ''))} → "
            f"<b>{html.escape(str(pb.get('定調') or ''))}</b>"
            f"</div>"
            f"<div class='twmc-mf-pb-grid'>"
            f"<div><span>價量狀態</span><b>{html.escape(str(pb.get('價量狀態') or '—'))}</b></div>"
            f"<div><span>家數差</span><b>{html.escape(str(pb.get('家數差') or '—'))}</b></div>"
            f"<div class='twmc-mf-pb-wide'><span>應對策略</span>"
            f"<b>{html.escape(str(pb.get('策略') or '—'))}</b></div>"
            f"</div></div>"
        )

    return (
        "<div class='twmc-mf-lights'>"
        f"<div class='twmc-mf-pills'>{''.join(pills)}</div>"
        f"{pb_html}"
        f"<div class='twmc-mf-primary'>"
        f"<span class='twmc-mf-primary-label'>{emoji} 目前主燈：{label}</span>"
        f"<span class='twmc-mf-primary-reason'>{reason}</span>"
        f"<span class='twmc-mf-inst'>{inst_line}</span>"
        "</div></div>"
    )


def render_mainforce_panel(code):
    """分析模式籌碼面／主力資訊：匯入文字檔／單筆新增＋表格。"""
    st.markdown("##### 主力動向歷史")
    st.caption(
        "可上傳文字表，或單筆新增（日期、買賣超張、家數差、5／20日集中）。"
        "同日期會覆蓋。資料會驅動燈號並提供給個股 AI 分析。"
    )
    existing = load_mainforce_history(code)

    with st.expander("單筆新增", expanded=not (existing and existing.get("rows"))):
        with st.form(f"mainforce_single_{code}", clear_on_submit=True):
            d1, d2, d3 = st.columns(3)
            with d1:
                entry_date = st.date_input(
                    "日期",
                    value=datetime.today().date(),
                    key=f"mf_date_{code}",
                )
            with d2:
                net_lots = st.number_input(
                    "買賣超(張)",
                    step=1,
                    value=0,
                    format="%d",
                    key=f"mf_net_{code}",
                )
            with d3:
                house_diff = st.number_input(
                    "家數差",
                    step=1,
                    value=0,
                    format="%d",
                    key=f"mf_house_{code}",
                )
            e1, e2 = st.columns(2)
            with e1:
                conc5 = st.number_input(
                    "5日集中(%)",
                    step=0.01,
                    value=0.0,
                    format="%.2f",
                    key=f"mf_c5_{code}",
                )
            with e2:
                conc20 = st.number_input(
                    "20日集中(%)",
                    step=0.01,
                    value=0.0,
                    format="%.2f",
                    key=f"mf_c20_{code}",
                )
            add_btn = st.form_submit_button(
                "新增／覆蓋此日",
                type="primary",
                use_container_width=True,
            )
            if add_btn:
                try:
                    upsert_mainforce_row(
                        code,
                        trade_date=entry_date,
                        net_lots=int(net_lots),
                        house_diff=int(house_diff),
                        conc5=float(conc5),
                        conc20=float(conc20),
                    )
                    st.success(
                        f"已儲存 {entry_date.strftime('%Y/%m/%d')}："
                        f"買賣超 {int(net_lots):+,} 張、家數差 {int(house_diff):+,}"
                    )
                    st.rerun()
                except Exception as exc:
                    st.error(f"儲存失敗：{exc}")

    with st.expander("檔案匯入", expanded=False):
        uploaded = st.file_uploader(
            "上傳主力動向 .txt",
            type=["txt"],
            key=f"mainforce_upload_{code}",
            label_visibility="collapsed",
        )
        c1, c2 = st.columns([1, 1])
        with c1:
            import_btn = st.button(
                "匯入並覆蓋此標的資料",
                type="primary",
                use_container_width=True,
                key=f"mainforce_import_{code}",
                disabled=uploaded is None,
            )
        with c2:
            clear_btn = st.button(
                "清除此標的主力資料",
                use_container_width=True,
                key=f"mainforce_clear_{code}",
                disabled=existing is None,
            )

        if clear_btn and existing is not None:
            path = mainforce_store_path(code)
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
            st.success("已清除。")
            st.rerun()

        if import_btn and uploaded is not None:
            try:
                raw = uploaded.getvalue().decode("utf-8-sig")
            except UnicodeDecodeError:
                raw = uploaded.getvalue().decode("big5", errors="ignore")
            parsed = parse_mainforce_history_text(raw, fallback_code=code)
            if not parsed.get("rows"):
                st.error("辨識不到資料列，請確認格式（日期｜買賣超｜家數差｜5日集中｜20日集中）。")
            else:
                file_code = parsed.get("code") or code
                if str(file_code).upper() != str(code).strip().upper():
                    st.warning(
                        f"檔案代號為 {file_code}，目前標的為 {code}；仍將存到目前標的 {code}。"
                    )
                save_mainforce_history(code, parsed)
                st.success(f"已匯入 {len(parsed['rows'])} 筆主力動向。")
                st.rerun()

    payload = load_mainforce_history(code)
    if not payload or not payload.get("rows"):
        st.info("尚未有此標的的主力動向資料，請單筆新增或上傳檔案。")
        return

    hist_df = get_finmind_price_history(
        code,
        (datetime.today() - timedelta(days=120)).strftime("%Y-%m-%d"),
    )
    lights = classify_mainforce_lights(
        payload,
        inst_df=get_institutional_data_2m(code),
        hist_df=hist_df,
    )
    st.markdown(_mainforce_lights_html(lights), unsafe_allow_html=True)
    if lights.get("signals"):
        with st.expander("觸發的細部訊號", expanded=False):
            for sig in lights["signals"]:
                st.markdown(
                    f"- {sig.get('emoji')} **{sig.get('label')}**：{sig.get('reason')}"
                )

    meta = (
        f"區間：{payload.get('start') or '—'} ~ {payload.get('end') or '—'}　"
        f"共 {len(payload.get('rows') or [])} 筆　"
        f"更新：{payload.get('updated_at') or '—'}"
    )
    st.caption(meta)
    df = mainforce_history_df(payload)
    if df.empty:
        st.info("資料為空。")
        return

    latest = payload["rows"][0]
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("最新買賣超(張)", f"{int(latest.get('買賣超(張)') or 0):+,}")
    s2.metric("家數差", f"{int(latest.get('家數差') or 0):+,}")
    s3.metric("5日集中(%)", f"{float(latest.get('5日集中(%)') or 0):+.2f}")
    s4.metric("20日集中(%)", f"{float(latest.get('20日集中(%)') or 0):+.2f}")

    style_cols = ["買賣超(張)", "家數差"]
    with st.container(key="mainforce_table"):
        st.dataframe(
            _style_buy_sell_df(df, style_cols),
            use_container_width=True,
            hide_index=True,
            height=min(520, 56 + 36 * min(len(df), 14)),
        )
    st.caption("※ 正值紅、負值綠；燈號＝主力快查定調＋FinMind 外資／投信＋價量，啟發式非投資建議。")


@st.fragment
def render_concentration(code, hist_df, shares):
    """拉桿只重跑此區塊，避免整頁刷新。"""
    st.markdown(
        "<div class='twmc-chips-h'>區間籌碼集中度估算</div>",
        unsafe_allow_html=True,
    )
    days = st.select_slider(
        "區間天數",
        options=[1, 5, 10, 20, 60],
        value=1,
        format_func=lambda d: f"{d}日",
        key=f"conc_days_{code}"
    )
    inst = get_institutional_data_2m(code)
    if not inst.empty and hist_df is not None and not hist_df.empty:
        conc_df = get_chip_concentration_data(code, hist_df, inst, shares, days=days)
        with st.container(key=f"conc_table_{code}"):
            st.dataframe(conc_df, use_container_width=True, hide_index=True, height=180)
        st.caption("※ 註：由於無法直接取得主力分點資料，此處「籌碼集中」以三大法人合計買賣超做為估算基準。")
    else:
        st.info("無法計算籌碼集中度 (缺少法人或成交量資料)。")


@st.cache_data(ttl=60)
def get_quote(code):
    """FinMind 最新日收盤與前一交易日收盤（非盤中即時報價）。"""
    rows = _finmind_data(
        "TaiwanStockPrice",
        code,
        (datetime.today() - timedelta(days=14)).strftime("%Y-%m-%d"),
    )
    if not rows:
        return None
    rows = sorted(rows, key=lambda row: row.get("date", ""))
    latest = rows[-1]
    latest_close = latest.get("close")
    if latest_close is None:
        return None
    if len(rows) >= 2:
        previous_close = rows[-2].get("close", latest_close)
    else:
        previous_close = float(latest_close) - float(latest.get("spread") or 0)
    return float(latest_close), float(previous_close)


@st.cache_data(ttl=60 * 60)
def get_finmind_price_history(code, start_date, end_date=None):
    """FinMind 日 OHLCV，欄位對齊既有圖表。"""
    rows = _finmind_data("TaiwanStockPrice", code, start_date, end_date)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    required = {"date", "open", "max", "min", "close", "Trading_Volume"}
    if not required.issubset(df.columns):
        return pd.DataFrame()
    df = df.rename(columns={
        "open": "Open",
        "max": "High",
        "min": "Low",
        "close": "Close",
        "Trading_Volume": "Volume",
    })
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    for days in (5, 20, 60):
        df[f"MA{days}"] = df["Close"].rolling(days).mean()
    return df.dropna(subset=["Open", "High", "Low", "Close"])


@st.cache_data(ttl=60 * 60)
def resolve_yahoo_symbol(code):
    """Yahoo 台股後綴：上市 .TW、上櫃 .TWO。"""
    code = str(code or "").strip().upper()
    if not code:
        return None
    headers = {"User-Agent": "Mozilla/5.0"}
    for suffix in (".TW", ".TWO"):
        symbol = f"{code}{suffix}"
        try:
            resp = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                params={"interval": "1d", "range": "5d"},
                headers=headers,
                timeout=12,
            )
            payload = resp.json()
            result = (payload.get("chart") or {}).get("result") or []
            if result and (result[0].get("meta") or {}).get("regularMarketPrice") is not None:
                return symbol
        except Exception:
            continue
    return None


def now_taipei():
    return datetime.now(TPE_TZ)


def taiwan_equity_session_status(when=None):
    """台股現貨：週一～五 09:00–13:30（台北）。"""
    now = when or now_taipei()
    if now.tzinfo is None:
        now = now.replace(tzinfo=TPE_TZ)
    else:
        now = now.astimezone(TPE_TZ)
    weekday = now.weekday()  # 0=Mon
    t = now.time()
    open_t = datetime.strptime("09:00", "%H:%M").time()
    close_t = datetime.strptime("13:30", "%H:%M").time()
    is_weekday = weekday < 5
    is_open = is_weekday and open_t <= t <= close_t
    if not is_weekday:
        phase = "週末休市"
    elif t < open_t:
        phase = "開盤前"
    elif t <= close_t:
        phase = "盤中"
    else:
        phase = "收盤後"
    return {
        "台北時間": now.strftime("%Y-%m-%d %H:%M:%S"),
        "星期": ["一", "二", "三", "四", "五", "六", "日"][weekday],
        "是否開盤中": is_open,
        "時段": phase,
    }


@st.cache_data(ttl=60 * 60)
def resolve_mis_ex_ch(code):
    """證交所 MIS ex_ch：上市 tse_xxx.tw、上櫃 otc_xxx.tw。"""
    code = str(code or "").strip().upper()
    if not code:
        return None
    # 加權指數
    if code in ("TAIEX", "T00", "^TWII"):
        return "tse_t00.tw"
    yahoo = resolve_yahoo_symbol(code)
    if yahoo and yahoo.endswith(".TWO"):
        return f"otc_{code}.tw"
    if yahoo and yahoo.endswith(".TW"):
        return f"tse_{code}.tw"
    # Yahoo 解不到時兩市都試
    return f"tse_{code}.tw"


def _mis_parse_price(row):
    """MIS：有成交用 z；無成交（z='-'）用買賣一檔中價。"""
    def _f(val):
        text = str(val or "").strip()
        if not text or text == "-":
            return None
        try:
            return float(text)
        except ValueError:
            return None

    last = _f(row.get("z"))
    if last is not None:
        return last, "last"
    bid0 = _f((str(row.get("b") or "").split("_") or [""])[0])
    ask0 = _f((str(row.get("a") or "").split("_") or [""])[0])
    if bid0 is not None and ask0 is not None:
        return (bid0 + ask0) / 2.0, "mid"
    if bid0 is not None:
        return bid0, "bid"
    if ask0 is not None:
        return ask0, "ask"
    return None, None


@st.cache_data(ttl=3)
def fetch_mis_quotes(codes):
    """
    證交所／櫃買 MIS 近即時報價（非 Yahoo 的 20 分延遲）。
    回傳 {code: {price, prev, time, source_detail, raw_ex}}。
    """
    codes = [str(c).strip().upper() for c in (codes or []) if str(c or "").strip()]
    if not codes:
        return {}
    # 一次同時問上市＋上櫃，避免先打 Yahoo 判斷板塊而變慢
    ex_list = []
    seen = set()
    def _add(ex):
        if ex and ex not in seen:
            seen.add(ex)
            ex_list.append(ex)
    for code in codes:
        if code in ("TAIEX", "T00", "^TWII"):
            _add("tse_t00.tw")
            continue
        _add(f"tse_{code}.tw")
        _add(f"otc_{code}.tw")
    ex_param = "|".join(ex_list)
    try:
        resp = _HTTP.get(
            "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
            params={"ex_ch": ex_param, "json": 1, "delay": 0},
            headers={"Referer": "https://mis.twse.com.tw/stock/fibest.jsp"},
            timeout=(2.5, 5),
        )
        payload = resp.json()
    except Exception:
        return {}
    by_code = {}
    for row in payload.get("msgArray") or []:
        code = str(row.get("c") or "").strip().upper()
        if not code or code == "T00":
            # 指數另外處理
            if code == "T00" or str(row.get("i") or "") == "tidx.tw":
                by_code["TAIEX"] = row
            continue
        # 同一代號若兩市都回，取有成交量／有 z 的優先
        prev = by_code.get(code)
        if prev is None:
            by_code[code] = row
            continue
        prev_z = str(prev.get("z") or "-")
        cur_z = str(row.get("z") or "-")
        if prev_z == "-" and cur_z != "-":
            by_code[code] = row
        elif int(str(row.get("v") or "0").replace(",", "") or 0) > int(
            str(prev.get("v") or "0").replace(",", "") or 0
        ):
            by_code[code] = row

    out = {}
    for code in codes:
        row = by_code.get(code)
        if not row:
            continue
        price, how = _mis_parse_price(row)
        prev_close = None
        try:
            y = str(row.get("y") or "").strip()
            if y and y != "-":
                prev_close = float(y)
        except ValueError:
            prev_close = None
        if price is None:
            continue
        out[code] = {
            "price": float(price),
            "prev": float(prev_close) if prev_close is not None else None,
            "time": str(row.get("t") or row.get("%") or ""),
            "date": str(row.get("d") or ""),
            "volume": row.get("v"),
            "price_mode": how,
            "ex": str(row.get("ex") or ""),
        }
    # 指數
    idx = by_code.get("TAIEX")
    if idx is not None:
        price, how = _mis_parse_price(idx)
        try:
            prev_close = float(idx.get("y")) if idx.get("y") not in (None, "", "-") else None
        except (TypeError, ValueError):
            prev_close = None
        if price is not None:
            out["TAIEX"] = {
                "price": float(price),
                "prev": float(prev_close) if prev_close is not None else None,
                "time": str(idx.get("t") or ""),
                "date": str(idx.get("d") or ""),
                "volume": idx.get("v"),
                "price_mode": how,
                "ex": "tse",
            }
    return out


@st.cache_data(ttl=3)
def get_live_quote(code):
    """盤中報價優先 MIS；失敗再 Yahoo（Yahoo 台股約延遲 20 分）。回傳 (price, prev) 或 None。"""
    code = str(code or "").strip().upper()
    if not code:
        return None
    mis = fetch_mis_quotes([code]).get(code)
    if mis and mis.get("price") is not None:
        prev = mis.get("prev")
        if prev is None:
            # 補昨收：FinMind／Yahoo
            yq, _ = get_yahoo_intraday(code)
            prev = yq[1] if yq else None
            if prev is None:
                fq = get_quote(code)
                prev = fq[1] if fq else mis["price"]
        return float(mis["price"]), float(prev)
    yq, _ = get_yahoo_intraday(code)
    return yq


@st.cache_data(ttl=15)
def get_yahoo_intraday(code):
    """Yahoo 當日 1 分鐘盤（走勢圖用）。台股報價本身約延遲 20 分鐘。回傳 (quote, df)。"""
    symbol = resolve_yahoo_symbol(code)
    if not symbol:
        return None, pd.DataFrame()
    try:
        resp = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"interval": "1m", "range": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        payload = resp.json()
        result = ((payload.get("chart") or {}).get("result") or [None])[0]
        if not result:
            return None, pd.DataFrame()
        meta = result.get("meta") or {}
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose")
        if prev is None:
            prev = meta.get("previousClose")
        quote = None
        if price is not None and prev is not None:
            quote = (float(price), float(prev))

        timestamps = result.get("timestamp") or []
        quote_block = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        if not timestamps or not quote_block:
            return quote, pd.DataFrame()
        df = pd.DataFrame({
            "Open": quote_block.get("open"),
            "High": quote_block.get("high"),
            "Low": quote_block.get("low"),
            "Close": quote_block.get("close"),
            "Volume": quote_block.get("volume"),
        }, index=pd.to_datetime(timestamps, unit="s"))
        # Yahoo 時間戳為 UTC，轉台北時間方便對齊 09:00–13:30
        try:
            df.index = df.index.tz_localize("UTC").tz_convert("Asia/Taipei").tz_localize(None)
        except Exception:
            pass
        df = df.dropna(subset=["Close"], how="all")
        return quote, df
    except Exception:
        return None, pd.DataFrame()


@st.cache_data(ttl=3)
def get_index_live_summary():
    """加權指數近即時（MIS t00）；失敗才退 Yahoo（約 20 分延遲）。"""
    mis = fetch_mis_quotes(["TAIEX"]).get("TAIEX")
    if mis and mis.get("price") is not None and mis.get("prev"):
        price = float(mis["price"])
        prev = float(mis["prev"])
        change = price - prev
        pct = (change / prev * 100.0) if prev else None
        tone = "中性"
        if pct is not None:
            if pct >= 0.35:
                tone = "偏多"
            elif pct <= -0.35:
                tone = "偏空"
            elif pct >= 0.1:
                tone = "微幅偏多"
            elif pct <= -0.1:
                tone = "微幅偏空"
        return {
            "資料來源": "證交所 MIS",
            "指數代碼": "TAIEX",
            "行情時間": mis.get("time") or "",
            "抓取時間": now_taipei().strftime("%Y-%m-%d %H:%M:%S"),
            "現價": round(price, 2),
            "昨收": round(prev, 2),
            "漲跌點": round(change, 2),
            "漲跌幅(%)": round(pct, 2) if pct is not None else None,
            "大盤環境判讀": tone,
        }
    # Yahoo fallback
    try:
        resp = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/^TWII",
            params={"interval": "1m", "range": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=12,
        )
        payload = resp.json()
        result = ((payload.get("chart") or {}).get("result") or [None])[0]
        if not result:
            return {}
        meta = result.get("meta") or {}
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose")
        if prev is None:
            prev = meta.get("previousClose")
        if price is None or prev is None:
            return {}
        price = float(price)
        prev = float(prev)
        change = price - prev
        pct = (change / prev * 100.0) if prev else None
        tone = "中性"
        if pct is not None:
            if pct >= 0.35:
                tone = "偏多"
            elif pct <= -0.35:
                tone = "偏空"
            elif pct >= 0.1:
                tone = "微幅偏多"
            elif pct <= -0.1:
                tone = "微幅偏空"
        return {
            "資料來源": "Yahoo（約延遲20分）",
            "指數代碼": "^TWII",
            "抓取時間": now_taipei().strftime("%Y-%m-%d %H:%M:%S"),
            "現價": round(price, 2),
            "昨收": round(prev, 2),
            "漲跌點": round(change, 2),
            "漲跌幅(%)": round(pct, 2) if pct is not None else None,
            "大盤環境判讀": tone,
        }
    except Exception:
        return {}


def clear_live_quote_caches():
    for fn in (fetch_mis_quotes, get_live_quote, get_index_live_summary, get_yahoo_intraday):
        try:
            fn.clear()
        except Exception:
            pass


def record_analyze_mis_tick(code):
    """盤中把 MIS 現價累積成本機軌跡，供分析模式分時圖近即時更新。"""
    code = str(code or "").strip().upper()
    if not code:
        return None
    day = now_taipei().strftime("%Y%m%d")
    key = f"mis_intraday_path_{code}_{day}"
    path = list(st.session_state.get(key) or [])
    info = None
    try:
        info = (fetch_mis_quotes([code]) or {}).get(code)
    except Exception:
        info = None
    if info and info.get("price") is not None:
        mis_t = str(info.get("time") or "")
        stamp = now_taipei().strftime("%Y-%m-%d %H:%M:%S")
        # 同一 MIS 時間只留一點，避免重複
        if path and path[-1].get("mis_t") == mis_t and mis_t:
            path[-1] = {
                "t": stamp,
                "mis_t": mis_t,
                "price": float(info["price"]),
                "prev": info.get("prev"),
            }
        else:
            path.append({
                "t": stamp,
                "mis_t": mis_t,
                "price": float(info["price"]),
                "prev": info.get("prev"),
            })
        # 最多留約半天點位
        if len(path) > 2500:
            path = path[-2500:]
        st.session_state[key] = path
    return info


def build_analyze_intraday_frame(code):
    """
    分析模式分時：Yahoo 1 分 K 當底（可能延遲），末端用 MIS 軌跡接上近即時。
    回傳 (live_quote, df, source_label)。
    """
    code = str(code or "").strip().upper()
    mis_info = record_analyze_mis_tick(code)
    mis_quote = get_live_quote(code)
    yahoo_quote, yahoo_df = get_yahoo_intraday(code)
    live = mis_quote or yahoo_quote

    day = now_taipei().strftime("%Y%m%d")
    path = list(st.session_state.get(f"mis_intraday_path_{code}_{day}") or [])
    mis_df = pd.DataFrame()
    if path:
        mis_df = pd.DataFrame(path)
        mis_df["t"] = pd.to_datetime(mis_df["t"], errors="coerce")
        mis_df = mis_df.dropna(subset=["t", "price"]).set_index("t").sort_index()
        mis_df = mis_df.rename(columns={"price": "Close"})
        mis_df["Open"] = mis_df["Close"]
        mis_df["High"] = mis_df["Close"]
        mis_df["Low"] = mis_df["Close"]
        mis_df["Volume"] = None

    df = yahoo_df.copy() if yahoo_df is not None and not yahoo_df.empty else pd.DataFrame()
    if not mis_df.empty:
        if df.empty:
            df = mis_df[["Open", "High", "Low", "Close", "Volume"]]
        else:
            # 砍掉比 MIS 最早點還新的 Yahoo 延遲尾巴，改接 MIS
            cut = mis_df.index.min()
            df = df[df.index < cut]
            df = pd.concat([df, mis_df[["Open", "High", "Low", "Close", "Volume"]]])
            df = df[~df.index.duplicated(keep="last")].sort_index()
        # 最後一點強制對齊最新 MIS 現價
        if mis_quote and mis_quote[0] is not None and not df.empty:
            df.iloc[-1, df.columns.get_loc("Close")] = float(mis_quote[0])
            for col in ("Open", "High", "Low"):
                if col in df.columns:
                    df.iloc[-1, df.columns.get_loc(col)] = float(mis_quote[0])

    src = "證交所 MIS 近即時"
    if mis_quote and not (yahoo_df is None or yahoo_df.empty):
        src = "證交所 MIS 近即時＋Yahoo 分時底圖"
    elif mis_quote:
        src = "證交所 MIS 近即時軌跡"
    elif yahoo_quote:
        src = "Yahoo（約延遲 20 分）"
    meta = {
        "source": src,
        "mis_time": (mis_info or {}).get("time") if mis_info else "",
        "fetched_at": now_taipei().strftime("%H:%M:%S"),
    }
    return live, df, meta


@st.cache_data(ttl=15)
def get_yahoo_index_live(symbol="^TWII"):
    """相容舊呼叫：轉到 get_index_live_summary。"""
    return get_index_live_summary()


def quote_color(change):
    """台股習慣：漲紅、跌綠、平盤白。"""
    if change > 0:
        return "#ef232a", "▲"
    if change < 0:
        return "#14b143", "▼"
    return "#ffffff", ""


def _price_flash_class(store_key, value, digits=2):
    """數字有變才回傳閃爍 class；並寫入 session 供下次比對。"""
    try:
        cur = None if value is None else round(float(value), digits)
    except (TypeError, ValueError):
        cur = value
    prev = st.session_state.get(store_key)
    st.session_state[store_key] = cur
    if prev is None or cur is None or prev == cur:
        return "", ""
    # nonce 迫使 DOM 有差異，動畫才會每次重播
    nonce = now_taipei().strftime("%H%M%S%f")
    return " twmc-price-flash", f" data-flash='{nonce}'"


def add_item():
    raw = st.session_state.get("add_input", "").strip()
    st.session_state.add_error = ""
    if not raw:
        return
    if st.session_state.active_group_id == ALL_GROUP_ID:
        st.session_state.add_error = "請先切換到自訂卡片盒再新增個股"
        st.session_state.show_add = False
        return

    code = raw.upper() if is_stock_code(raw) else NAME_TO_CODE.get(raw)
    if not code:
        st.session_state.add_error = f"找不到「{raw}」，請改用股票代號"
        return
    if get_quote(code) is None:
        st.session_state.add_error = f"查不到 {code} 的報價"
        return

    group = current_group()
    if group is None:
        st.session_state.add_error = "找不到目前的卡片盒"
        return
    if code not in group["items"]:
        group["items"].append(code)
    st.session_state.active_code = code
    st.session_state.show_add = False
    persist_card_boxes()


def card_data(code, quote):
    """整理成卡片要顯示的三行文字與漲跌顏色。"""
    name = CODE_TO_NAME.get(code, "")
    if not quote:
        return {"code": code, "name": name, "price": "－", "change": "無報價", "color": "#fafafa"}

    price, prev = quote
    change = price - prev
    pct = (change / prev) * 100 if prev else 0
    color, arrow = quote_color(change)
    return {
        "code": code,
        "name": name,
        "price": f"{price:.2f}",
        "change": f"{arrow}{abs(change):.2f} ({pct:+.2f}%)",
        "color": color,
    }


def build_watchlist_cards(prefer_live=True):
    """組卡片資料；盤中優先證交所 MIS 近即時報價。"""
    codes = visible_codes()
    quotes_map = {}
    if prefer_live and codes:
        try:
            mis = fetch_mis_quotes(list(codes)) or {}
        except Exception:
            mis = {}
        for code in codes:
            row = mis.get(str(code).strip().upper()) or mis.get(code)
            if row and row.get("price") is not None:
                prev = row.get("prev")
                if prev is None:
                    try:
                        live = get_live_quote(code)
                        prev = live[1] if live else row["price"]
                    except Exception:
                        prev = row["price"]
                quotes_map[code] = (float(row["price"]), float(prev))
                continue
            try:
                live = get_live_quote(code) or get_quote(code)
            except Exception:
                live = get_quote(code)
            if live:
                quotes_map[code] = live
    else:
        for code in codes:
            q = get_quote(code)
            if q:
                quotes_map[code] = q

    tag_cache = st.session_state.setdefault("card_rule_tags", {})
    # 代號集合變了就清掉已不在清單的快取
    alive = set(codes)
    for stale in [k for k in tag_cache if k not in alive]:
        tag_cache.pop(stale, None)

    cards = []
    for code in codes:
        item = card_data(code, quotes_map.get(code))
        if st.session_state.app_mode == "analyze":
            if code not in tag_cache:
                try:
                    tag_cache[code] = get_analysis_rule_tags(code)
                except Exception:
                    tag_cache[code] = []
            item["tags"] = list(tag_cache.get(code) or [])
        cards.append(item)
    return cards


# 分析模式卡片標籤規則。數值由 FinMind 日線／法人買賣超計算。
RULE_INSTITUTION_MIN_DAYS = 3
RULE_LARGE_NET_BUY_VOLUME_RATIO = 0.02
RULE_MA20_PROXIMITY = 0.015
RULE_TAG_LIMIT = 4


def _consecutive_sign_days(values, sign):
    """從最新一日開始計算連續正／負值天數。"""
    days = 0
    for value in values:
        if (sign > 0 and value > 0) or (sign < 0 and value < 0):
            days += 1
        else:
            break
    return days


def _safe_float(value):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(num):
        return None
    return num


@st.cache_data(ttl=15 * 60)
def get_analysis_rule_tags(code):
    """依可解釋規則產生分析模式卡片標籤。"""
    tags = []
    inst = get_institutional_data_2m(code)
    hist = get_finmind_price_history(
        code, (datetime.today() - timedelta(days=180)).strftime("%Y-%m-%d")
    )

    if not inst.empty:
        values = pd.to_numeric(inst["三大法人合計"], errors="coerce").fillna(0).tolist()
        for sign, action, tone in ((1, "買", "buy"), (-1, "賣", "sell")):
            days = _consecutive_sign_days(values, sign)
            if days < RULE_INSTITUTION_MIN_DAYS:
                continue
            net_shares = abs(sum(values[:days]))
            volumes = hist["Volume"].tail(days) if not hist.empty else pd.Series(dtype=float)
            volume_ratio = (net_shares / float(volumes.sum())) if volumes.sum() else 0
            size = "大" if volume_ratio >= RULE_LARGE_NET_BUY_VOLUME_RATIO else ""
            tags.append({
                "text": f"法人{days}日{size}{action} {net_shares / 1000:,.0f}張",
                "detail": (
                    f"三大法人連續 {days} 日{'買超' if sign > 0 else '賣超'}；"
                    f"累計 {net_shares / 1000:,.0f} 張，占同期成交量 {volume_ratio:.1%}"
                ),
                "tone": tone,
            })
            break

    if len(hist) >= 23:
        latest = hist.iloc[-1]
        prev = hist.iloc[-2]
        close = _safe_float(latest.get("Close"))
        prev_close = _safe_float(prev.get("Close"))
        ma5 = _safe_float(latest.get("MA5"))
        ma20 = _safe_float(latest.get("MA20"))
        ma60 = _safe_float(latest.get("MA60"))
        prev_ma5 = _safe_float(prev.get("MA5"))
        prev_ma20 = _safe_float(prev.get("MA20"))
        prev_ma60 = _safe_float(prev.get("MA60"))
        ma5_three_days_ago = _safe_float(hist["MA5"].iloc[-4])

        # 月線賣壓天花板：日線上升但逼近／卡在月線下方
        if (
            ma5 is not None
            and ma20 is not None
            and ma5_three_days_ago is not None
            and ma5 > ma5_three_days_ago
            and ma5 < ma20
        ):
            gap = abs(ma5 - ma20) / ma20 if ma20 else None
            if gap is not None and gap <= RULE_MA20_PROXIMITY:
                tags.append({
                    "text": "月線賣壓天花板",
                    "detail": (
                        f"MA5 近 3 日上升，仍在 MA20 下方；"
                        f"距離月線 {gap:.1%}（MA5 {ma5:.2f}／MA20 {ma20:.2f}）"
                    ),
                    "tone": "warning",
                })

        # 跌破月線／季線：昨日收在均線上（含觸及），今日收在均線下
        if (
            close is not None
            and prev_close is not None
            and ma20 is not None
            and prev_ma20 is not None
            and prev_close >= prev_ma20
            and close < ma20
        ):
            tags.append({
                "text": "跌破月線",
                "detail": (
                    f"收盤由月線上方轉至下方："
                    f"昨收 {prev_close:.2f}／昨 MA20 {prev_ma20:.2f} → "
                    f"今收 {close:.2f}／MA20 {ma20:.2f}"
                ),
                "tone": "sell",
            })

        if (
            close is not None
            and prev_close is not None
            and ma60 is not None
            and prev_ma60 is not None
            and prev_close >= prev_ma60
            and close < ma60
        ):
            tags.append({
                "text": "跌破季線",
                "detail": (
                    f"收盤由季線上方轉至下方："
                    f"昨收 {prev_close:.2f}／昨 MA60 {prev_ma60:.2f} → "
                    f"今收 {close:.2f}／MA60 {ma60:.2f}"
                ),
                "tone": "sell",
            })

        # 黃金交叉：短均線由下往上穿越長均線（可同時出現兩種）
        if (
            ma5 is not None
            and ma20 is not None
            and prev_ma5 is not None
            and prev_ma20 is not None
            and prev_ma5 <= prev_ma20
            and ma5 > ma20
        ):
            tags.append({
                "text": "黃金交叉 MA5↑MA20",
                "detail": (
                    f"日線上穿月線："
                    f"昨 MA5 {prev_ma5:.2f}／MA20 {prev_ma20:.2f} → "
                    f"今 MA5 {ma5:.2f}／MA20 {ma20:.2f}"
                ),
                "tone": "buy",
            })
        if (
            ma20 is not None
            and ma60 is not None
            and prev_ma20 is not None
            and prev_ma60 is not None
            and prev_ma20 <= prev_ma60
            and ma20 > ma60
        ):
            tags.append({
                "text": "黃金交叉 MA20↑MA60",
                "detail": (
                    f"月線上穿季線："
                    f"昨 MA20 {prev_ma20:.2f}／MA60 {prev_ma60:.2f} → "
                    f"今 MA20 {ma20:.2f}／MA60 {ma60:.2f}"
                ),
                "tone": "buy",
            })

    return tags[:RULE_TAG_LIMIT]


MODE_LABELS = ["分析模式", "模擬模式", "投資模式"]
MODE_TO_KEY = {"分析模式": "analyze", "模擬模式": "simulated", "投資模式": "investment"}
KEY_TO_MODE = {v: k for k, v in MODE_TO_KEY.items()}
MODE_HINTS = {
    "analyze": "看盤・籌碼・走勢",
    "simulated": "虛擬資金練功",
    "investment": "實盤交易紀錄",
}


def resolve_trade_code(raw):
    raw = (raw or "").strip()
    if not raw:
        return None, "請輸入股票代號或名稱"
    code = raw.upper() if is_stock_code(raw) else NAME_TO_CODE.get(raw)
    if not code:
        return None, f"找不到「{raw}」，請改用股票代號"
    return code, None


def pnl_html(val):
    if val is None:
        return "—"
    color = "#ef232a" if val > 0 else ("#14b143" if val < 0 else "#fafafa")
    return f"<span style='color:{color}'>{val:,.0f}</span>"


def quotes_for_codes(codes, prefer_live=False):
    """批量報價。prefer_live=True 時優先證交所 MIS，再 Yahoo，最後 FinMind 日收盤。"""
    quotes = {}
    sources = {}
    uniq = [str(c).strip().upper() for c in {str(c).strip() for c in (codes or []) if c}]
    mis_map = {}
    if prefer_live and uniq:
        try:
            mis_map = fetch_mis_quotes(uniq) or {}
        except Exception:
            mis_map = {}
    for code in uniq:
        if prefer_live:
            mis = mis_map.get(code)
            if mis and mis.get("price") is not None:
                quotes[code] = float(mis["price"])
                sources[code] = "mis"
                continue
            live = get_live_quote(code)
            if live and live[0] is not None:
                quotes[code] = float(live[0])
                # get_live_quote 內部已優先 MIS；走到這裡多半是 Yahoo
                sources[code] = "yahoo_delayed"
                continue
        q = get_quote(code)
        if q:
            quotes[code] = float(q[0])
            sources[code] = "finmind_daily"
    return quotes, sources


def _holding_label(row):
    name = CODE_TO_NAME.get(row["code"], "")
    return f"{row['code']} {name}".strip()


def _pie_option(title, data, colors=None, accent="#ef232a", subtext=None):
    """深色主題圓餅圖；data = [{name, value}, ...]。"""
    items = [item for item in (data or []) if float(item.get("value") or 0) > 0]
    if not items:
        return None
    palette = colors or [
        accent, "#ff8a8a", "#ffb347", "#ffd166", "#f4a261",
        "#e9c46a", "#f7a1c4", "#c77dff", "#90e0ef", "#80ed99",
    ]
    series_data = []
    for idx, item in enumerate(items):
        series_data.append({
            "name": item["name"],
            "value": round(float(item["value"]), 2),
            "itemStyle": {"color": palette[idx % len(palette)]},
        })
    total = sum(float(item["value"]) for item in items)
    return {
        "backgroundColor": "transparent",
        "title": {
            "text": title,
            "left": "center",
            "top": 8,
            "textStyle": {"color": "#f0f0f0", "fontSize": 18, "fontWeight": 700},
            "subtext": subtext if subtext is not None else f"合計 {total:,.0f}",
            "subtextStyle": {"color": accent, "fontSize": 14},
        },
        "tooltip": {
            "trigger": "item",
            "backgroundColor": "rgba(30,30,30,0.95)",
            "borderColor": "#444",
            "textStyle": {"color": "#fff", "fontSize": 14},
            "formatter": "{b}<br/>{c}（{d}%）",
        },
        "legend": {"show": False},
        "series": [{
            "type": "pie",
            "radius": ["34%", "62%"],
            "center": ["50%", "56%"],
            "avoidLabelOverlap": True,
            "itemStyle": {"borderRadius": 4, "borderColor": "#111", "borderWidth": 2},
            "label": {
                "color": "#eee",
                "fontSize": 11,
                "formatter": "{b}\n{d}%",
            },
            "labelLine": {"lineStyle": {"color": "#666"}},
            "data": series_data,
        }],
    }


def _rank_board_html(title, rows, accent, empty_text):
    """右側排行榜 HTML。rows = [{label, value, pct}, ...]。"""
    if not rows:
        return (
            f"<div class='twmc-rank-board'>"
            f"<div class='twmc-rank-title' style='color:{accent};'>{title}</div>"
            f"<div class='twmc-rank-empty'>{empty_text}</div>"
            f"</div>"
        )
    lines = [
        "<div class='twmc-rank-board'>",
        f"<div class='twmc-rank-title' style='color:{accent};'>{title}</div>",
        "<div class='twmc-rank-grid'>",
    ]
    for i, row in enumerate(rows, start=1):
        lines.append(
            f"<span class='twmc-rank-idx' style='color:{accent};'>#{i}</span>"
            f"<span class='twmc-rank-name'>{row['label']}</span>"
            f"<span class='twmc-rank-val' style='color:{accent};'>{row['value']:+,.0f}</span>"
            f"<span class='twmc-rank-pct' style='color:{accent};'>({row['pct']:.1f}%)</span>"
        )
    lines.append("</div></div>")
    return "".join(lines)


def render_dashboard_pies(holdings, total_cost, market_value):
    """儀表板圓餅改直向：左圖右資訊（摘要／前五排行）。"""
    gains = []
    losses = []
    for row in holdings or []:
        u = row.get("unrealized")
        if u is None:
            continue
        label = _holding_label(row)
        if u > 0:
            gains.append({"name": label, "value": float(u), "raw": float(u)})
        elif u < 0:
            losses.append({"name": label, "value": abs(float(u)), "raw": float(u)})

    gains_sorted = sorted(gains, key=lambda x: x["raw"], reverse=True)
    losses_sorted = sorted(losses, key=lambda x: x["raw"])
    gain_total = sum(item["raw"] for item in gains_sorted) or 0.0
    loss_total = sum(abs(item["raw"]) for item in losses_sorted) or 0.0

    cost = float(total_cost or 0)
    mkt = float(market_value or 0)
    pie_pnl = mkt - cost
    est_pnl = sum(float(row.get("unrealized") or 0) for row in (holdings or []))
    if cost > 0:
        ratio_txt = f"{est_pnl / cost * 100:+.2f}%"
    else:
        ratio_txt = "—"
    pnl_color = "#ef232a" if est_pnl > 0 else ("#14b143" if est_pnl < 0 else "#fafafa")

    if pie_pnl >= 0:
        cost_mkt = [
            {"name": "總成本", "value": cost},
            {"name": "獲利", "value": pie_pnl},
        ]
        cost_colors = ["#9aa0a6", "#ef232a"]
        cost_accent = "#ef232a"
    else:
        cost_mkt = [
            {"name": "持有股票總市值", "value": mkt},
            {"name": "虧損", "value": abs(pie_pnl)},
        ]
        cost_colors = ["#9aa0a6", "#14b143"]
        cost_accent = "#14b143"

    components.html(
        """
        <script>
        (function () {
          const doc = window.parent.document;
          const id = "twmc-rank-board-style";
          let style = doc.getElementById(id);
          if (!style) {
            style = doc.createElement("style");
            style.id = id;
            doc.head.appendChild(style);
          }
          style.textContent = `
            .twmc-rank-board, .twmc-dash-summary {
              padding: 4px 4px 4px 2px;
              color: #e8e8e8;
              box-sizing: border-box;
              max-width: 100%;
              overflow: hidden;
            }
            /* 交易區有「字體放大三倍」的規則，選擇器比較長，
               這裡必須用同樣長度的前綴才蓋得過去。 */
            [data-testid="stMarkdownContainer"] .twmc-rank-title,
            [data-testid="stMarkdownContainer"] .twmc-dash-summary-title,
            [class*="st-key-twmc_trading"] [data-testid="stMarkdownContainer"] .twmc-rank-title,
            [class*="st-key-twmc_trading"] [data-testid="stMarkdownContainer"] .twmc-dash-summary-title {
              font-size: 32px !important;
              font-weight: 700;
              line-height: 1.25 !important;
              margin-bottom: 0.55rem;
            }
            .twmc-dash-summary-grid {
              display: grid;
              grid-template-columns: max-content max-content;
              justify-content: start;
              column-gap: 1.75rem;
              align-items: baseline;
              width: fit-content;
              max-width: 100%;
            }
            .twmc-dash-summary-grid > span {
              padding: 0.28rem 0;
              border-bottom: 1px solid rgba(255,255,255,0.08);
            }
            .twmc-dash-summary-grid .k { color: #bdbdbd; white-space: nowrap; }
            .twmc-dash-summary-grid .v { font-weight: 600; }
            .twmc-rank-grid {
              display: grid;
              grid-template-columns: max-content minmax(0, auto) max-content max-content;
              justify-content: start;
              column-gap: 0.55rem;
              align-items: baseline;
              width: fit-content;
              max-width: 100%;
            }
            .twmc-rank-grid > span {
              padding: 0.28rem 0;
              border-bottom: 1px solid rgba(255,255,255,0.08);
            }
            .twmc-rank-idx {
              font-weight: 700;
              white-space: nowrap;
            }
            .twmc-rank-name {
              color: #f0f0f0;
              overflow: hidden;
              text-overflow: ellipsis;
              white-space: nowrap;
              min-width: 0;
            }
            [data-testid="stMarkdownContainer"] .twmc-rank-board span,
            [data-testid="stMarkdownContainer"] .twmc-dash-summary-grid span,
            [class*="st-key-twmc_trading"] [data-testid="stMarkdownContainer"] .twmc-rank-board span,
            [class*="st-key-twmc_trading"] [data-testid="stMarkdownContainer"] .twmc-dash-summary-grid span {
              font-size: 32px !important;
              line-height: 1.3 !important;
            }
            .twmc-rank-val,
            .twmc-rank-pct,
            .twmc-dash-summary-grid .v {
              font-weight: 700;
              white-space: nowrap;
              text-align: right;
              font-variant-numeric: tabular-nums;
              font-feature-settings: "tnum" 1;
              font-family: "Cascadia Mono", "Consolas", "Courier New", monospace;
            }
            .twmc-rank-pct { font-weight: 500; opacity: 0.9; }
            .twmc-rank-empty { color: #888; padding-top: 0.5rem; }
            [class*="st-key-twmc_trading"] [data-testid="stMarkdownContainer"] .twmc-rank-empty {
              font-size: 32px !important;
            }
          `;
        })();
        </script>
        """,
        height=0,
    )

    left, right = st.columns([0.504, 0.496], gap="small")
    with left:
        opt = _pie_option(
            "成本／市值損益比",
            cost_mkt,
            colors=cost_colors,
            accent=cost_accent,
            subtext=f"損益比 {ratio_txt}",
        )
        if opt:
            st_echarts(options=opt, height="320px", key="dash_pie_cost_mkt")
        else:
            st.caption("成本／市值損益比：目前沒有資料")
    with right:
        st.markdown(
            "<div class='twmc-dash-summary'>"
            "<div class='twmc-dash-summary-title'>成本／市值摘要</div>"
            "<div class='twmc-dash-summary-grid'>"
            f"<span class='k'>總成本</span><span class='v'>{cost:,.0f}</span>"
            f"<span class='k'>市值</span><span class='v'>{mkt:,.0f}</span>"
            f"<span class='k'>預估損益</span>"
            f"<span class='v' style='color:{pnl_color};'>{est_pnl:+,.0f}</span>"
            f"<span class='k'>損益比</span>"
            f"<span class='v' style='color:{pnl_color};'>{ratio_txt}</span>"
            "</div></div>",
            unsafe_allow_html=True,
        )

    left, right = st.columns([0.504, 0.496], gap="small")
    with left:
        opt = _pie_option(
            "紅字收益佔比",
            [{"name": g["name"], "value": g["value"]} for g in gains_sorted],
            colors=[
                "#ef232a", "#ff4d4f", "#ff7875", "#ffa39e", "#ffccc7",
                "#cf1322", "#a8071a", "#ff85c0", "#eb2f96", "#c41d7f",
            ],
            accent="#ef232a",
        )
        if opt:
            st_echarts(options=opt, height="320px", key="dash_pie_gains")
        else:
            st.caption("紅字收益佔比：目前沒有獲利檔")
    with right:
        top_gains = []
        for item in gains_sorted[:5]:
            pct = (item["raw"] / gain_total * 100) if gain_total else 0.0
            top_gains.append({"label": item["name"], "value": item["raw"], "pct": pct})
        st.markdown(
            _rank_board_html("獲利前五名", top_gains, "#ef232a", "目前沒有獲利檔"),
            unsafe_allow_html=True,
        )

    left, right = st.columns([0.504, 0.496], gap="small")
    with left:
        opt = _pie_option(
            "綠字損失佔比",
            [{"name": g["name"], "value": g["value"]} for g in losses_sorted],
            colors=[
                "#14b143", "#52c41a", "#73d13d", "#95de64", "#b7eb8f",
                "#389e0d", "#237804", "#5cdbd3", "#13c2c2", "#08979c",
            ],
            accent="#14b143",
        )
        if opt:
            st_echarts(options=opt, height="320px", key="dash_pie_losses")
        else:
            st.caption("綠字損失佔比：目前沒有虧損檔")
    with right:
        top_losses = []
        for item in losses_sorted[:5]:
            pct = (abs(item["raw"]) / loss_total * 100) if loss_total else 0.0
            top_losses.append({"label": item["name"], "value": item["raw"], "pct": pct})
        st.markdown(
            _rank_board_html("虧損前五名", top_losses, "#14b143", "目前沒有虧損檔"),
            unsafe_allow_html=True,
        )


def render_holdings_table(rows):
    """st.dataframe 是 canvas 繪製、CSS 改不到字級，所以持倉表自己輸出 HTML。"""
    if not rows:
        st.info("目前沒有持倉。")
        return
    headers = ["代號", "名稱", "股數", "均價", "持有成本", "現價", "市值", "預估損益", "報酬率"]
    body = []
    for row in rows:
        name = CODE_TO_NAME.get(row["code"], "")
        mark = row["mark"]
        mkt = row["market_value"]
        u = row["unrealized"]
        pct = (u / row["cost"] * 100) if u is not None and row["cost"] else None
        qty = row["qty"]
        qty_txt = f"{int(round(qty))}" if abs(qty - round(qty)) < 1e-9 else f"{qty:g}"
        pnl_color = "#fafafa"
        if u is not None:
            pnl_color = "#ef232a" if u > 0 else ("#14b143" if u < 0 else "#fafafa")
        cells = [
            row["code"],
            name,
            qty_txt,
            f"{row['avg_cost']:.2f}",
            f"{row['cost']:,.0f}",
            f"{mark:.2f}" if mark is not None else "—",
            f"{mkt:,.0f}" if mkt is not None else "—",
            f"<span style='color:{pnl_color}'>{u:,.0f}</span>" if u is not None else "—",
            f"<span style='color:{pnl_color}'>{pct:.2f}%</span>" if pct is not None else "—",
        ]
        body.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
    head = "<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>"
    st.markdown(
        "<div class='twmc-holdings-table'><table>"
        f"<thead>{head}</thead><tbody>{''.join(body)}</tbody>"
        "</table></div>",
        unsafe_allow_html=True,
    )


def render_exit_table(records):
    """脫手（已賣出）紀錄表。"""
    if not records:
        st.info("目前沒有脫手紀錄。")
        return
    headers = [
        "日期", "代號", "名稱", "股數", "賣出價", "成本均價",
        "賣出金額", "手續費", "交易稅", "已實現損益", "報酬率",
    ]
    body = []
    for row in records:
        name = CODE_TO_NAME.get(row["code"], "")
        qty = row["qty"]
        qty_txt = f"{int(round(qty))}" if abs(qty - round(qty)) < 1e-9 else f"{qty:g}"
        pnl = row["realized"]
        pct = (pnl / row["cost"] * 100) if row.get("cost") else None
        pnl_color = "#ef232a" if pnl > 0 else ("#14b143" if pnl < 0 else "#fafafa")
        ts = (row.get("ts") or "")[:10]
        cells = [
            ts,
            row["code"],
            name,
            qty_txt,
            f"{row['sell_price']:.2f}",
            f"{row['avg_cost']:.2f}",
            f"{row['proceeds']:,.0f}",
            f"{row['fee']:,.0f}",
            f"{row['tax']:,.0f}",
            f"<span style='color:{pnl_color}'>{pnl:,.0f}</span>",
            f"<span style='color:{pnl_color}'>{pct:.2f}%</span>" if pct is not None else "—",
        ]
        body.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
    head = "<tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr>"
    st.markdown(
        "<div class='twmc-holdings-table'><table>"
        f"<thead>{head}</thead><tbody>{''.join(body)}</tbody>"
        "</table></div>",
        unsafe_allow_html=True,
    )


def render_trade_history(transactions, allow_delete=False, book_key="investment"):
    if not transactions:
        st.caption("尚無交易紀錄。")
        return
    ordered = sorted(transactions, key=lambda tx: (tx.get("ts") or ""), reverse=True)
    for tx in ordered:
        side_label = "買進" if tx["side"] == "buy" else "賣出"
        name = CODE_TO_NAME.get(tx["code"], "")
        qty_txt = (
            f"{int(round(tx['qty']))}"
            if abs(float(tx["qty"]) - round(float(tx["qty"]))) < 1e-9
            else f"{tx['qty']:g}"
        )
        fee = float(tx.get("fee") or 0)
        tax = float(tx.get("tax") or 0)
        line = f"{tx['ts'][:16]}　{side_label}　{tx['code']} {name}　{qty_txt} 股　@{tx['price']:.2f}"
        if fee > 0:
            line += f"　手續費 {fee:,.0f}"
        if tax > 0:
            line += f"　交易稅 {tax:,.0f}"
        if tx.get("note"):
            line += f"　{tx['note']}"
        if allow_delete:
            left, right = st.columns([8, 1])
            with left:
                st.write(line)
            with right:
                if st.button("刪除", key=f"del_{book_key}_{tx['id']}"):
                    st.session_state.portfolio[book_key] = pf.delete_trade(
                        st.session_state.portfolio[book_key], tx["id"]
                    )
                    persist_portfolio()
                    st.rerun()
        else:
            st.write(line)


# 僅在爬到的中文原文中「出現過」才列為題材關鍵字，不自行發明
_THEME_KEYWORD_CANDIDATES = (
    "矽晶圓", "方型矽晶圓", "磊晶", "化合物半導體", "GaN", "SOI",
    "半導體材料", "半導體", "晶圓", "材料", "漲價", "營收", "獲利",
    "轉換債", "可轉債", "設備投資", "AI", "記憶體", "PCB", "先進製程",
    "功率元件", "車用", "先進封裝", "HBM", "CoWoS",
)
_COMPANY_PROFILE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}
_FINANCIAL_NEWS_SOURCES = (
    "工商時報", "經濟日報", "自由財經", "鉅亨", "MoneyDJ", "財訊",
    "今周刊", "財經新報", "UDN",
)


def _finmind_stock_meta(code):
    """FinMind 產業／名稱（中文）。"""
    rows = _finmind_data("TaiwanStockInfo", code)
    # 同代號可能有多筆產業分類，取第一筆非空
    name = ""
    industry = ""
    for row in rows:
        if str(row.get("stock_id")) != str(code):
            continue
        if not name:
            name = (row.get("stock_name") or "").strip()
        cat = (row.get("industry_category") or "").strip()
        if cat and not industry:
            industry = cat
        if name and industry:
            break
    return {"name": name, "industry": industry}


def _fetch_text(url):
    try:
        response = requests.get(url, headers=_COMPANY_PROFILE_HEADERS, timeout=20)
        if response.status_code != 200:
            return ""
        response.encoding = response.apparent_encoding or "utf-8"
        return response.text
    except Exception:
        return ""


def _fetch_company_business(code):
    """Yahoo／鉅亨的中文公司資料，只作題材的公司產業補充。"""
    for suffix in (".TW", ".TWO"):
        text = _fetch_text(f"https://tw.stock.yahoo.com/quote/{code}{suffix}/profile")
        match = re.search(r"主要經營業務</span></span><div[^>]*>([^<]+)</div>", text)
        if match:
            return match.group(1).strip(), "Yahoo股市公司資料"

    text = _fetch_text(f"https://www.cnyes.com/twstock/{code}/company/profile")
    match = re.search(r"主要經營業務</p></div><div[^>]*><p[^>]*>([^<]+)</p>", text)
    if match:
        return match.group(1).strip().rstrip("。"), "鉅亨網公司簡介"
    return "", ""


@st.cache_data(ttl=60 * 60)
def fetch_financial_news_columns(name, code):
    """從 Google News RSS 收錄指定財經媒體的個股專欄，不採納論壇或盤中速報。"""
    query = f'"{name}" 財經' if name else str(code)
    url = (
        "https://news.google.com/rss/search"
        f"?q={quote(query)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    )
    text = _fetch_text(url)
    if not text:
        return []
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        return []

    articles = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not source or not any(pub in source for pub in _FINANCIAL_NEWS_SOURCES):
            continue
        if name and name not in title and str(code) not in title:
            continue
        if any(marker in title for marker in ("盤中速報", "注意交易資訊", "轉交換價格")):
            continue
        articles.append({"title": title, "source": source, "link": link})
    return articles[:8]


def _theme_keywords_from_texts(texts):
    blob = "\n".join(str(x) for x in texts if x)
    found = []
    for kw in _THEME_KEYWORD_CANDIDATES:
        if kw in blob and kw not in found:
            found.append(kw)
    return found


@st.cache_data(ttl=60 * 60)
def fetch_stock_theme(code):
    """FinMind 產業分類 + 中文公司資料 + 財經媒體專欄整理題材。"""
    # cache-bust: 產業改走帶 token 的 FinMind 請求
    _cache_ver = 2
    code = str(code or "").strip().upper()
    name = CODE_TO_NAME.get(code, "")
    theme = {
        "name": name,
        "industry": "",
        "business": "",
        "keywords": [],
        "headlines": [],
        "sources": [],
        "_v": _cache_ver,
    }
    if not code:
        return theme

    meta = _finmind_stock_meta(code)
    if meta.get("name") and not theme["name"]:
        theme["name"] = meta["name"]
        name = theme["name"]
    if meta.get("industry"):
        theme["industry"] = meta["industry"]
        theme["sources"].append("FinMind")

    business, business_source = _fetch_company_business(code)
    if business:
        theme["business"] = business
        theme["sources"].append(business_source)

    articles = fetch_financial_news_columns(name, code)
    theme["headlines"] = [article["title"] for article in articles]
    theme["articles"] = articles
    if articles:
        theme["sources"].append("Google News 財經媒體專欄")
    theme["keywords"] = _theme_keywords_from_texts(
        [theme["industry"], theme["business"], *theme["headlines"]]
    )
    theme["sources"] = list(dict.fromkeys(theme["sources"]))
    theme.pop("_v", None)
    return theme


@st.cache_data(ttl=60 * 60)
def fetch_finmind_quarterly_income(code):
    """FinMind 台股季報：營收／營業利益／歸屬母公司淨利。"""
    start = (datetime.today() - timedelta(days=365 * 3)).strftime("%Y-%m-%d")
    rows = _finmind_data("TaiwanStockFinancialStatements", code, start)
    if not rows:
        return pd.DataFrame()
    # 對齊畫圖使用的通用欄位名；稅後淨利優先 IncomeAfterTaxes
    type_map = {
        "Revenue": "Total Revenue",
        "OperatingIncome": "Operating Income",
        "IncomeAfterTaxes": "Net Income",
        "EquityAttributableToOwnersOfParent": "Net Income",
    }
    by_date = {}
    for row in rows:
        key = type_map.get(row.get("type"))
        if not key:
            continue
        # IncomeAfterTaxes 優先於綜合損益歸母
        if (
            key == "Net Income"
            and "Net Income" in by_date.get(row.get("date"), {})
            and row.get("type") == "EquityAttributableToOwnersOfParent"
        ):
            continue
        by_date.setdefault(row.get("date"), {})[key] = row.get("value")
    if not by_date:
        return pd.DataFrame()
    metric_order = ["Total Revenue", "Operating Income", "Net Income"]
    df = pd.DataFrame(by_date).reindex(index=metric_order)
    df.columns = pd.to_datetime(df.columns)
    return df.sort_index(axis=1)


@st.cache_data(ttl=60 * 60)
def fetch_finmind_valuation(code):
    """FinMind 本益比、股價淨值比、殖利率與近四季 EPS。"""
    start = (datetime.today() - timedelta(days=365 * 2)).strftime("%Y-%m-%d")
    per_rows = _finmind_data("TaiwanStockPER", code, start)
    statement_rows = _finmind_data("TaiwanStockFinancialStatements", code, start)
    latest_per = sorted(per_rows, key=lambda row: row.get("date", ""))[-1] if per_rows else {}
    eps_rows = [
        row for row in statement_rows
        if row.get("type") == "EPS" and row.get("value") is not None
    ]
    latest_eps = sorted(eps_rows, key=lambda row: row.get("date", ""))[-4:]
    trailing_eps = sum(float(row["value"]) for row in latest_eps) if latest_eps else None
    book_rows = [
        row for row in _finmind_data("TaiwanStockBalanceSheet", code, start)
        if row.get("type") == "EquityAttributableToOwnersOfParent_per"
        and row.get("value") is not None
    ]
    latest_book = sorted(book_rows, key=lambda row: row.get("date", ""))[-1] if book_rows else {}
    return {
        "trailingEps": trailing_eps,
        "trailingPE": latest_per.get("PER"),
        "priceToBook": latest_per.get("PBR"),
        "dividendYield": latest_per.get("dividend_yield"),
        "bookValuePerShare": latest_book.get("value"),
        "bookValueDate": latest_book.get("date"),
        "valuationDate": latest_per.get("date"),
        "forwardEps": None,
        "forwardPE": None,
    }


@st.cache_data(ttl=60 * 60)
def fetch_finmind_month_revenue(code):
    """FinMind 月營收。抓約兩年，供年增率對照。"""
    start = (datetime.today() - timedelta(days=800)).strftime("%Y-%m-%d")
    rows = _finmind_data("TaiwanStockMonthRevenue", code, start)
    if not rows:
        return []
    cleaned = []
    for row in sorted(rows, key=lambda item: (
        int(item.get("revenue_year") or 0),
        int(item.get("revenue_month") or 0),
        str(item.get("date") or ""),
    )):
        try:
            revenue = float(row.get("revenue"))
        except (TypeError, ValueError):
            continue
        if pd.isna(revenue):
            continue
        cleaned.append({
            "date": str(row.get("date") or ""),
            "revenue_year": int(row.get("revenue_year") or 0),
            "revenue_month": int(row.get("revenue_month") or 0),
            "revenue": revenue,
        })
    return cleaned


@st.cache_data(ttl=60 * 60)
def get_investment_reference_data(code):
    """投資筆記展開頁需要的基本面／新聞資料。網路失敗時回傳空資料。"""
    data = {
        "info": {},
        "quarterly": pd.DataFrame(),
        "month_revenue": [],
        "theme": {},
    }
    data["quarterly"] = fetch_finmind_quarterly_income(code)
    data["info"] = fetch_finmind_valuation(code)
    # 先對完整窗口算年增，再只保留近 14 個月給 UI／快照
    data["month_revenue"] = _month_revenue_yoy(fetch_finmind_month_revenue(code))[-14:]
    data["theme"] = fetch_stock_theme(code)
    return data


def _month_revenue_label(item):
    year = item.get("revenue_year") or 0
    month = item.get("revenue_month") or 0
    if year and month:
        return f"{year}/{month:02d}"
    return str(item.get("date") or "—")[:7]


def _month_revenue_yoy(items):
    """依年月對齊計算年增率。"""
    by_key = {
        (int(x.get("revenue_year") or 0), int(x.get("revenue_month") or 0)): x
        for x in items
        if x.get("revenue_year") and x.get("revenue_month")
    }
    result = []
    for item in items:
        key = (int(item.get("revenue_year") or 0), int(item.get("revenue_month") or 0))
        prev = by_key.get((key[0] - 1, key[1]))
        yoy = None
        if prev and prev.get("revenue"):
            try:
                yoy = (float(item["revenue"]) / float(prev["revenue"]) - 1.0) * 100.0
            except (TypeError, ValueError, ZeroDivisionError):
                yoy = None
        result.append({**item, "yoy_pct": yoy})
    return result


def _quarter_label(ts):
    """日期 → 2024Q3。"""
    try:
        dt = pd.Timestamp(ts)
        return f"{dt.year}Q{((dt.month - 1) // 3) + 1}"
    except Exception:
        return str(ts)[:10]


def _format_tw_amount(val):
    """財報數字改成中文單位：千／萬／億。"""
    try:
        num = float(val)
    except (TypeError, ValueError):
        return "—"
    if pd.isna(num):
        return "—"
    sign = "-" if num < 0 else ""
    abs_num = abs(num)
    if abs_num >= 1e8:
        return f"{sign}{abs_num / 1e8:.2f}億"
    if abs_num >= 1e4:
        return f"{sign}{abs_num / 1e4:.2f}萬"
    if abs_num >= 1e3:
        return f"{sign}{abs_num / 1e3:.2f}千"
    return f"{sign}{abs_num:,.0f}"


def _fin_amount_cell(val):
    """財報儲存格：正紅負綠（台股慣例）。"""
    text = _format_tw_amount(val)
    try:
        num = float(val)
    except (TypeError, ValueError):
        return f"<td>{html.escape(text)}</td>"
    if pd.isna(num) or text == "—":
        return f"<td>{html.escape(text)}</td>"
    if num > 0:
        cls = "twmc-fin-pos"
    elif num < 0:
        cls = "twmc-fin-neg"
    else:
        cls = ""
    if cls:
        return f"<td class='{cls}'>{html.escape(text)}</td>"
    return f"<td>{html.escape(text)}</td>"


def render_fundamental_panel(code, *, chart_key=None, show_theme=True):
    """共用基本面面板：估值、季報、月營收、題材。"""
    ref = get_investment_reference_data(code)
    info = ref["info"] or {}
    quote = get_quote(code)
    price = float(quote[0]) if quote else None
    eps = info.get("trailingEps")
    pe = info.get("trailingPE")
    pbr = info.get("priceToBook")
    div_yield = info.get("dividendYield")
    book = info.get("bookValuePerShare")
    fair = (float(eps) * float(pe)) if eps not in (None, 0) and pe not in (None, 0) else None
    chart_key = chart_key or f"fin_chart_{code}"

    st.markdown("<div class='twmc-ref-h'>估值／EPS／淨值</div>", unsafe_allow_html=True)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("現價", f"{price:,.2f}" if price is not None else "—")
    c2.metric("近四季 EPS", f"{float(eps):,.2f}" if eps is not None else "—")
    c3.metric("本益比", f"{float(pe):,.2f}" if pe is not None else "—")
    c4.metric("EPS × 本益比估值", f"{fair:,.2f}" if fair is not None else "—")
    d1, d2, d3, d4 = st.columns(4)
    d1.metric("股價淨值比", f"{float(pbr):,.2f}" if pbr is not None else "—")
    d2.metric(
        "每股淨值",
        f"{float(book):,.2f}" if book is not None else "—",
        help=f"資料日：{info.get('bookValueDate') or '—'}",
    )
    d3.metric(
        "現金殖利率(%)",
        f"{float(div_yield):,.2f}" if div_yield is not None else "—",
    )
    d4.metric("估值資料日", str(info.get("valuationDate") or "—"))

    st.markdown("<div class='twmc-ref-h'>財報與近兩年季收益</div>", unsafe_allow_html=True)
    quarterly = ref["quarterly"]
    if not quarterly.empty:
        wanted = [x for x in ("Total Revenue", "Operating Income", "Net Income") if x in quarterly.index]
        if wanted:
            q = quarterly.copy()
            q.columns = pd.to_datetime(q.columns)
            q = q.sort_index(axis=1)
            cols = list(q.columns[-8:])
            raw = q.loc[wanted, cols].T.copy()
            raw.index = [_quarter_label(x) for x in raw.index]
            raw = raw.rename(columns={
                "Total Revenue": "營收",
                "Operating Income": "營業利益",
                "Net Income": "稅後淨利",
            })
            table = raw.T
            metric_order = [m for m in ("營收", "營業利益", "稅後淨利") if m in table.index]
            rows_html = []
            header = "".join(
                f"<th>{html.escape(c)}</th>" for c in ["科目", *table.columns.tolist()]
            )
            rows_html.append(f"<tr>{header}</tr>")
            for metric in metric_order:
                cells = [f"<th scope='row'>{html.escape(metric)}</th>"]
                cells.extend(_fin_amount_cell(table.loc[metric, qtr]) for qtr in table.columns)
                rows_html.append("<tr>" + "".join(cells) + "</tr>")
            st.markdown(
                "<div class='twmc-fin-table'><table>"
                + "".join(rows_html)
                + "</table></div>",
                unsafe_allow_html=True,
            )

            chart_series = []
            legend_names = []
            colors = {"營收": "#00bfff", "營業利益": "#ff9900", "稅後淨利": "#ef232a"}
            for col in ("營收", "營業利益", "稅後淨利"):
                if col not in raw.columns:
                    continue
                values = [
                    round(float(v) / 1e8, 2) if pd.notna(v) else None
                    for v in raw[col].tolist()
                ]
                legend_names.append(col)
                chart_series.append({
                    "name": col,
                    "type": "bar",
                    "barMaxWidth": 28,
                    "data": values,
                    "itemStyle": {"color": colors[col], "opacity": 0.45},
                })
                chart_series.append({
                    "name": col,
                    "type": "line",
                    "smooth": True,
                    "showSymbol": True,
                    "data": values,
                    "lineStyle": {"width": 2, "color": colors[col]},
                    "itemStyle": {"color": colors[col]},
                })
            if chart_series:
                option = {
                    "backgroundColor": "transparent",
                    "tooltip": {"trigger": "axis"},
                    "legend": {
                        "data": legend_names,
                        "textStyle": {"color": "#ddd"},
                    },
                    "grid": {"left": "8%", "right": "4%", "top": "16%", "bottom": "12%"},
                    "xAxis": {
                        "type": "category",
                        "data": list(raw.index),
                        "axisLabel": {"color": "#ccc"},
                        "axisLine": {"lineStyle": {"color": "#888"}},
                    },
                    "yAxis": {
                        "type": "value",
                        "name": "億",
                        "splitLine": {"lineStyle": {"color": "#333"}},
                        "axisLabel": {"color": "#ccc"},
                    },
                    "series": chart_series,
                }
                st_echarts(options=option, height="360px", key=chart_key)
        else:
            st.info("目前找不到可用的季度損益欄位。")
    else:
        st.info("目前無法取得季度財報。")

    st.markdown("<div class='twmc-ref-h'>月營收</div>", unsafe_allow_html=True)
    month_items = ref.get("month_revenue") or []
    recent_months = month_items[-12:]
    if recent_months:
        labels = [_month_revenue_label(x) for x in recent_months]
        rev_yi = [
            round(float(x["revenue"]) / 1e8, 2) if x.get("revenue") is not None else None
            for x in recent_months
        ]
        yoy = [
            round(float(x["yoy_pct"]), 1) if x.get("yoy_pct") is not None else None
            for x in recent_months
        ]
        m1, m2, m3 = st.columns(3)
        latest = recent_months[-1]
        m1.metric("最新月營收", _format_tw_amount(latest.get("revenue")))
        m2.metric(
            "營收年月",
            _month_revenue_label(latest),
        )
        m3.metric(
            "年增率",
            f"{float(latest['yoy_pct']):+.1f}%" if latest.get("yoy_pct") is not None else "—",
        )
        month_option = {
            "backgroundColor": "transparent",
            "tooltip": {"trigger": "axis"},
            "legend": {"data": ["月營收(億)", "年增率(%)"], "textStyle": {"color": "#ddd"}},
            "grid": {"left": "6%", "right": "6%", "top": "16%", "bottom": "12%"},
            "xAxis": {
                "type": "category",
                "data": labels,
                "axisLabel": {"color": "#ccc"},
                "axisLine": {"lineStyle": {"color": "#888"}},
            },
            "yAxis": [
                {
                    "type": "value",
                    "name": "億",
                    "splitLine": {"lineStyle": {"color": "#333"}},
                    "axisLabel": {"color": "#ccc"},
                },
                {
                    "type": "value",
                    "name": "%",
                    "splitLine": {"show": False},
                    "axisLabel": {"color": "#ccc"},
                },
            ],
            "series": [
                {
                    "name": "月營收(億)",
                    "type": "bar",
                    "data": rev_yi,
                    "barMaxWidth": 26,
                    "itemStyle": {"color": "#00bfff", "opacity": 0.7},
                },
                {
                    "name": "年增率(%)",
                    "type": "line",
                    "yAxisIndex": 1,
                    "data": yoy,
                    "smooth": True,
                    "showSymbol": True,
                    "lineStyle": {"width": 2, "color": "#ff9900"},
                    "itemStyle": {"color": "#ff9900"},
                },
            ],
        }
        st_echarts(options=month_option, height="320px", key=f"{chart_key}_month_rev")
    else:
        st.info("目前無法取得月營收。")

    if not show_theme:
        return

    st.markdown("<div class='twmc-ref-h'>題材</div>", unsafe_allow_html=True)
    theme = ref.get("theme") or {}
    if not theme:
        with st.spinner("正在整理題材…"):
            theme = fetch_stock_theme(code)
    industry = str(theme.get("industry") or "").strip()
    business = str(theme.get("business") or "").strip()
    keywords = theme.get("keywords") or []
    headlines = theme.get("headlines") or []
    articles = theme.get("articles") or []
    sources = theme.get("sources") or []
    if industry or business or keywords or headlines:
        lines = []
        if industry:
            lines.append(f"產業：{industry}")
        if business:
            lines.append(f"主要業務：{business}")
        if keywords:
            lines.append("題材關鍵字：" + "、".join(keywords))
        if headlines:
            lines.append("財經媒體專欄：")
            for article in articles:
                title = html.escape(str(article.get("title") or ""))
                source = html.escape(str(article.get("source") or ""))
                link = str(article.get("link") or "").strip()
                if link:
                    lines.append(
                        f"・<a href='{html.escape(link, quote=True)}' target='_blank' "
                        f"rel='noopener noreferrer'>{title}</a>　{source}"
                    )
                else:
                    lines.append(f"・{title}　{source}")
        if sources:
            lines.append("資料來源：" + "、".join(sources))
        st.markdown(
            "<div class='twmc-theme-box'>"
            + "<br>".join(
                line if line.startswith("・<a ") else html.escape(line)
                for line in lines
            )
            + "</div>",
            unsafe_allow_html=True,
        )
    else:
        st.write("目前無法從台股中文來源取得題材資料。")


def render_note_fundamental(code):
    render_fundamental_panel(code, chart_key=f"notes_fin_chart_{code}", show_theme=True)


def render_analyze_fundamental(code):
    """分析模式：基本面獨立區段。"""
    with st.container(key="twmc_analyze_fundamental"):
        st.caption("資料來源：FinMind（財報、月營收、PER／PBR／殖利率、每股淨值）。不含法人成本與目標價。")
        with st.spinner("正在彙整基本面…"):
            render_fundamental_panel(
                code,
                chart_key=f"analyze_fin_chart_{code}",
                show_theme=True,
            )
@st.cache_data(ttl=60 * 60)
def get_note_technical_data(code):
    """FinMind 近兩年日 K 與 5／20／60 日均線。"""
    return get_finmind_price_history(
        code, (datetime.today() - timedelta(days=730)).strftime("%Y-%m-%d")
    )


def render_note_technical(code):
    df = get_note_technical_data(code)
    if df.empty:
        st.info("目前無法取得 K 線資料。")
        return
    df = df.tail(240)
    dates = df.index.strftime("%Y-%m-%d").tolist()
    k_data = [[round(float(v), 2) for v in row] for row in df[["Open", "Close", "Low", "High"]].values]
    clean = lambda series: [round(float(x), 2) if pd.notna(x) else "-" for x in series]
    option = {
        "backgroundColor": "transparent",
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "cross"}},
        "legend": {"data": ["K線", "5日均線", "20日均線", "60日均線"], "textStyle": {"color": "#ddd"}},
        "grid": {"left": "6%", "right": "5%", "top": "14%", "bottom": "12%"},
        "xAxis": {"type": "category", "data": dates, "scale": True, "boundaryGap": False,
                  "axisLine": {"lineStyle": {"color": "#888"}}, "axisLabel": {"color": "#ccc"}},
        "yAxis": {"scale": True, "splitLine": {"lineStyle": {"color": "#333"}},
                  "axisLabel": {"color": "#ccc"}},
        "dataZoom": [{"type": "inside"}, {"type": "slider"}],
        "series": [
            {"name": "K線", "type": "candlestick", "data": k_data,
             "itemStyle": {"color": "#ef232a", "color0": "#14b143", "borderColor": "#ef232a", "borderColor0": "#14b143"}},
            {"name": "5日均線", "type": "line", "data": clean(df["MA5"]), "showSymbol": False, "lineStyle": {"color": "#ffff00"}},
            {"name": "20日均線", "type": "line", "data": clean(df["MA20"]), "showSymbol": False, "lineStyle": {"color": "#b042ff"}},
            {"name": "60日均線", "type": "line", "data": clean(df["MA60"]), "showSymbol": False, "lineStyle": {"color": "#00ffff"}},
        ],
    }
    st_echarts(options=option, height="520px", key=f"notes_technical_chart_{code}")


def render_note_chips(code):
    inst = get_institutional_data_2m(code)
    margin = get_margin_data_2m(code)
    shares = get_total_shares(code)
    dist = get_chip_distribution_data(code, shares) if shares else {}
    large = get_thousand_lot_holder_ratio(code)

    def chips_h(title):
        st.markdown(
            f"<div class='twmc-chips-h'>{html.escape(title)}</div>",
            unsafe_allow_html=True,
        )

    chips_h("三大法人")
    if not inst.empty:
        latest = inst.iloc[0]
        labels = ("外資買賣超", "投信買賣超", "自營商買賣超", "三大法人合計")
        cols = st.columns(4)
        for col, label in zip(cols, labels):
            col.markdown(
                f"<div class='twmc-note-metric'>"
                f"<div class='k'>{html.escape(label)}</div>"
                f"<div class='v'>{_chip_signed_html(latest.get(label, 0))}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )
        with st.container(key=f"notes_inst_table_{code}"):
            st.dataframe(
                _style_buy_sell_df(inst.head(10), list(labels)),
                use_container_width=True,
                hide_index=True,
            )
        st.caption("※ 單位：股；正數買超（紅）、負數賣超（綠）；資料來源：FinMind")
    else:
        st.info("目前無法取得三大法人資料。")

    chips_h("千張大戶持股比例")
    if large.get("ratio") is not None:
        c1, c2, c3 = st.columns(3)
        c1.metric("千張大戶持股", f"{large['ratio']:.2f}%")
        c2.metric("人數", f"{int(large.get('people') or 0):,}")
        c3.metric("資料日", large.get("date") or "—")
        st.caption("※ 加總持股級距下限 ≥ 1,000,000 股（千張）之比例；資料來源：FinMind TaiwanStockHoldingSharesPer")
    else:
        hint = large.get("msg") or ""
        if "level is free" in hint.lower() or "sponsor" in hint.lower():
            st.info("千張大戶資料需 FinMind 進階權限。請設定環境變數 FINMIND_TOKEN 或 secrets 後再試。")
        else:
            st.info("目前無法取得千張大戶持股比例。")

    chips_h("融資券比例")
    if not margin.empty:
        latest = margin.iloc[-1]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("融資餘額", f"{float(latest.get('融資餘額', 0)):,.0f}")
        c2.metric("融資使用率", f"{float(latest.get('融資占比(%)', 0)):.2f}%")
        c3.metric("融券餘額", f"{float(latest.get('融券餘額', 0)):,.0f}")
        c4.metric("融券使用率", f"{float(latest.get('融券占比(%)', 0)):.2f}%")
    else:
        st.info("目前無法取得融資券資料。")

    chips_h("股東結構")
    if dist:
        lots = {key: round(float(value), 0) for key, value in dist.items()}
        st.bar_chart(pd.DataFrame.from_dict(lots, orient="index", columns=["張數"]))
        st.caption("外資為 FinMind 外資持股；投信與自營商為近一年累計買賣超估算。")
    else:
        st.info("目前無法取得股東結構資料。")


def render_investment_notes(code):
    """投資模式：個股基本資訊、標籤與四區記事本，取代儀表板／持倉／交易區。"""
    note = get_note(code)
    tags = list(note.get("tags") or [])
    name = CODE_TO_NAME.get(code, "")
    title = f"{code} {name}".strip()

    # 持倉基本資訊（成本／市值／損益）
    book = st.session_state.portfolio.get("investment") or {}
    quotes, _ = quotes_for_codes([code], prefer_live=True)
    snap = pf.compute_snapshot(
        [tx for tx in (book.get("transactions") or []) if tx.get("code") == code],
        quotes,
        starting_cash=None,
        net_of_exit_costs=True,
    )
    row = next((r for r in snap.get("holdings") or [] if r.get("code") == code), None)

    with st.container(key="twmc_notes"):
        st.markdown(
            f"<div class='twmc-notes-title'>{html.escape(title)}　投資筆記</div>",
            unsafe_allow_html=True,
        )

        # 與儀表板摘要同字級；預估損益保留紅綠
        def _note_metric(label, value_html):
            return (
                f"<div class='twmc-note-metric'>"
                f"<div class='k'>{html.escape(label)}</div>"
                f"<div class='v'>{value_html}</div>"
                f"</div>"
            )

        m1, m2, m3, m4 = st.columns(4)
        if row:
            cost = float(row.get("cost") or 0)
            mkt = row.get("market_value")
            pnl = row.get("unrealized")
            qty = float(row.get("qty") or 0)
            avg = float(row.get("avg_cost") or 0)
            m1.markdown(_note_metric("持有成本", f"{cost:,.0f}"), unsafe_allow_html=True)
            m2.markdown(
                _note_metric("市值", f"{mkt:,.0f}" if mkt is not None else "—"),
                unsafe_allow_html=True,
            )
            m3.markdown(_note_metric("預估損益", pnl_html(pnl)), unsafe_allow_html=True)
            m4.markdown(
                _note_metric("股數／均價", f"{qty:,.0f}／{avg:,.2f}"),
                unsafe_allow_html=True,
            )
        else:
            m1.markdown(_note_metric("持有成本", "—"), unsafe_allow_html=True)
            m2.markdown(_note_metric("市值", "—"), unsafe_allow_html=True)
            m3.markdown(_note_metric("預估損益", "—"), unsafe_allow_html=True)
            m4.markdown(_note_metric("股數／均價", "—"), unsafe_allow_html=True)
            st.caption("目前沒有這檔的持倉；仍可寫筆記。")

        if tags:
            n_cols = min(len(tags), 6)
            cols = st.columns(n_cols)
            for i, tag in enumerate(tags):
                with cols[i % n_cols]:
                    if st.button(f"{tag}  ×", key=f"notes_del_tag_{code}_{i}", use_container_width=True):
                        update_note(code, tags=[t for j, t in enumerate(tags) if j != i])
                        st.rerun()
        else:
            st.caption("尚未加上標籤")

        with st.form(f"notes_add_tag_{code}", clear_on_submit=True):
            t1, t2 = st.columns([4, 1])
            new_tag = t1.text_input(
                "新增標籤",
                label_visibility="collapsed",
                placeholder="輸入標籤後按新增，例如 長期、觀察中",
            )
            if t2.form_submit_button("新增", use_container_width=True):
                t = (new_tag or "").strip()
                if t and t not in tags:
                    update_note(code, tags=tags + [t])
                    st.rerun()

        # —— 個股紀律設定 ——
        disc = _normalize_discipline(note.get("discipline"))
        mark_price = row.get("mark") if row else None
        if mark_price is None and quotes:
            mark_price = quotes.get(code)
        hold_qty = float(row.get("qty") or 0) if row else 0.0
        hold_avg = float(row.get("avg_cost") or 0) if row else None
        if hold_avg is not None and hold_avg <= 0:
            hold_avg = None

        st.markdown("<div class='twmc-notes-section-title'>紀律設定</div>", unsafe_allow_html=True)
        st.caption(
            "續抱／保守用條列整理；停損／停利會對照現價顯示差距與預計損益。"
            "不會自動下單。"
        )
        flags = _discipline_price_flags(disc, mark_price)
        for flag in flags:
            st.warning(flag)

        b1, b2 = st.columns(2)
        with b1:
            hold_items = _discipline_bullet_editor(
                code,
                "hold_ok",
                "續抱／加碼仍成立",
                "例：收盤站上成本均價",
                disc.get("hold_ok") or [],
            )
        with b2:
            cons_items = _discipline_bullet_editor(
                code,
                "get_conservative",
                "該更保守／減碼",
                "例：跌破停損參考價",
                disc.get("get_conservative") or [],
            )

        s1, s2 = st.columns(2)
        with s1:
            stop_loss = _discipline_ref_panel(
                "停損參考價",
                code,
                "stop_loss",
                disc.get("stop_loss"),
                mark_price,
                hold_qty,
                hold_avg,
            )
        with s2:
            take_profit = _discipline_ref_panel(
                "停利參考價",
                code,
                "take_profit",
                disc.get("take_profit"),
                mark_price,
                hold_qty,
                hold_avg,
            )

        note_key = f"disc_note_{code}"
        if f"{note_key}_booted" not in st.session_state:
            st.session_state[note_key] = disc.get("note") or ""
            st.session_state[f"{note_key}_booted"] = True
        st.text_area(
            "補充（選填）",
            key=note_key,
            height=68,
            placeholder="部位上限、分批規則…",
        )

        _, save_d = st.columns([5, 1])
        with save_d:
            if st.button("儲存紀律", use_container_width=True, key=f"notes_disc_save_{code}"):
                update_note(code, discipline={
                    "hold_ok": hold_items,
                    "get_conservative": cons_items,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "note": st.session_state.get(note_key) or "",
                })
                st.session_state.notes_just_saved = True
                st.rerun()

        focus = st.session_state.notes_focus_section
        section_renderers = {
            "fundamental": render_note_fundamental,
            "technical": render_note_technical,
            "chips": render_note_chips,
        }

        def _render_notes_editor(area_height=180, only_key=None):
            sections = (
                [item for item in NOTE_SECTIONS if item[0] == only_key]
                if only_key
                else list(NOTE_SECTIONS)
            )
            with st.form(f"notes_body_{code}_{only_key or 'all'}"):
                section_values = {}
                if only_key:
                    # 展開資料時，右側只顯示對應那一區
                    key, label, placeholder = sections[0]
                    if st.form_submit_button(
                        label,
                        key=f"notes_reference_{code}_{key}",
                        use_container_width=True,
                    ):
                        st.session_state.notes_focus_section = None
                        st.rerun()
                    section_values[key] = st.text_area(
                        label,
                        value=note.get(key) or "",
                        height=area_height,
                        label_visibility="collapsed",
                        placeholder=placeholder,
                        key=f"notes_area_{code}_{key}",
                    )
                else:
                    r1c1, r1c2 = st.columns(2)
                    r2c1, r2c2 = st.columns(2)
                    section_cols = [r1c1, r1c2, r2c1, r2c2]
                    for (key, label, placeholder), col in zip(NOTE_SECTIONS, section_cols):
                        with col:
                            if st.form_submit_button(
                                label,
                                key=f"notes_reference_{code}_{key}",
                                use_container_width=True,
                            ):
                                st.session_state.notes_focus_section = (
                                    None if st.session_state.notes_focus_section == key else key
                                )
                                st.rerun()
                            section_values[key] = st.text_area(
                                label,
                                value=note.get(key) or "",
                                height=area_height,
                                label_visibility="collapsed",
                                placeholder=placeholder,
                                key=f"notes_area_{code}_{key}",
                            )
                _, save_col = st.columns([5, 1])
                with save_col:
                    saved = st.form_submit_button("儲存筆記", use_container_width=True)
                if saved:
                    # 只更新目前顯示的欄位，其餘維持原內容
                    merged = {
                        k: note.get(k) or ""
                        for k, _, _ in NOTE_SECTIONS
                    }
                    merged.update(section_values)
                    update_note(code, sections=merged)
                    st.session_state.notes_just_saved = True

        # 基本面（及其他資料區）展開：隱藏「筆記」大標；左資料、右僅對應撰寫區
        if focus in section_renderers:
            left, right = st.columns([1.25, 0.95], gap="medium")
            with left:
                with st.container(key="twmc_notes_reference"):
                    label = next(label for key, label, _ in NOTE_SECTIONS if key == focus)
                    st.markdown(
                        f"<div class='twmc-notes-reference-title'>{html.escape(label)}資料</div>",
                        unsafe_allow_html=True,
                    )
                    section_renderers[focus](code)
            with right:
                _render_notes_editor(area_height=420, only_key=focus)
        else:
            st.markdown("<div class='twmc-notes-section-title'>筆記</div>", unsafe_allow_html=True)
            _render_notes_editor(area_height=180)

        if st.session_state.notes_just_saved:
            st.success("已儲存筆記")
            st.session_state.notes_just_saved = False



# --- Gemini AI 分析（函式） ---
def _gemini_api_key():
    """只從伺服器端環境變數或 Streamlit secrets 讀取 API 金鑰。"""
    key = str(os.environ.get("GEMINI_API_KEY") or "").strip()
    if key:
        return key
    try:
        return str(st.secrets.get("GEMINI_API_KEY", "") or "").strip()
    except Exception:
        return ""


def _gemini_num(value, digits=2):
    try:
        value = float(value)
        return round(value, digits) if pd.notna(value) else None
    except (TypeError, ValueError):
        return None


def _gemini_fundamental_snapshot(code):
    """整理基本面摘要供 Gemini 使用（不含法人成本／目標價）。"""
    ref = get_investment_reference_data(code)
    info = ref.get("info") or {}
    theme = ref.get("theme") or {}
    eps = info.get("trailingEps")
    pe = info.get("trailingPE")
    fair = None
    try:
        if eps not in (None, 0) and pe not in (None, 0):
            fair = float(eps) * float(pe)
    except (TypeError, ValueError):
        fair = None

    quarterly_rows = []
    quarterly = ref.get("quarterly")
    if isinstance(quarterly, pd.DataFrame) and not quarterly.empty:
        q = quarterly.copy()
        q.columns = pd.to_datetime(q.columns)
        q = q.sort_index(axis=1)
        for col in list(q.columns[-8:]):
            row = {"季度": _quarter_label(col)}
            mapping = {
                "Total Revenue": "營收",
                "Operating Income": "營業利益",
                "Net Income": "稅後淨利",
            }
            for src, label in mapping.items():
                if src in q.index:
                    row[label] = _gemini_num(q.loc[src, col], 0)
            quarterly_rows.append(row)

    month_rows = []
    for item in (ref.get("month_revenue") or [])[-6:]:
        month_rows.append({
            "年月": _month_revenue_label(item),
            "營收": _gemini_num(item.get("revenue"), 0),
            "年增率(%)": _gemini_num(item.get("yoy_pct"), 1),
        })

    return {
        "估值": {
            "資料日": info.get("valuationDate"),
            "近四季EPS": _gemini_num(eps),
            "本益比": _gemini_num(pe),
            "股價淨值比": _gemini_num(info.get("priceToBook")),
            "現金殖利率(%)": _gemini_num(info.get("dividendYield")),
            "每股淨值": _gemini_num(info.get("bookValuePerShare")),
            "每股淨值資料日": info.get("bookValueDate"),
            "EPS×本益比估值": _gemini_num(fair),
        },
        "近8季財報": quarterly_rows,
        "近6月營收": month_rows,
        "題材": {
            "產業": theme.get("industry") or None,
            "主要業務": (str(theme.get("business") or "").strip()[:280] or None),
            "關鍵字": (theme.get("keywords") or [])[:8],
        },
        "資料來源說明": {
            "已納入": ["季報", "月營收與年增", "PER/PBR/殖利率/每股淨值", "產業與題材"],
            "本來就沒有、勿當成缺失抱怨": ["法人成本", "券商目標價"],
        },
    }


def _investment_mode_codes():
    """投資模式卡片盒內的全部代號。"""
    box = (st.session_state.get("mode_boxes") or {}).get("investment") or {}
    seen = []
    for group in box.get("groups") or []:
        for code in group.get("items") or []:
            c = str(code or "").strip()
            if c and c not in seen:
                seen.append(c)
    return seen


def _gemini_holding_snapshot(code, quote_data=None):
    """若個股在投資模式卡片盒，且投資帳本仍持有，回傳持股狀態；否則 None。"""
    code = str(code or "").strip()
    if not code or code not in _investment_mode_codes():
        return None
    book = (st.session_state.get("portfolio") or {}).get("investment") or {}
    txs = [
        tx for tx in (book.get("transactions") or [])
        if str(tx.get("code") or "").strip() == code
    ]
    if not txs:
        return None
    mark = None
    if quote_data:
        try:
            mark = float(quote_data[0])
        except (TypeError, ValueError, IndexError):
            mark = None
    quotes = {code: mark} if mark is not None else quotes_for_codes([code], prefer_live=True)[0]
    if code not in quotes:
        quotes.update(quotes_for_codes([code], prefer_live=True)[0])
    snap = pf.compute_snapshot(
        txs,
        quotes,
        starting_cash=None,
        net_of_exit_costs=True,
    )
    row = next((r for r in snap.get("holdings") or [] if r.get("code") == code), None)
    if not row:
        return None
    qty = float(row.get("qty") or 0)
    if qty <= 1e-9:
        return None
    avg = float(row.get("avg_cost") or 0)
    cost = float(row.get("cost") or 0)
    price = row.get("mark")
    mkt = row.get("market_value")
    pnl = row.get("unrealized")
    pnl_gross = row.get("unrealized_gross")
    ret_pct = None
    dist_pct = None
    if price is not None and avg > 0:
        dist_pct = (float(price) / avg - 1.0) * 100.0
    if pnl is not None and cost > 0:
        ret_pct = float(pnl) / cost * 100.0
    result = {
        "是否持有": True,
        "來源": "投資模式持倉",
        "股數": _gemini_num(qty, 0),
        "成本均價": _gemini_num(avg),
        "持有成本": _gemini_num(cost, 0),
        "現價": _gemini_num(price),
        "市值": _gemini_num(mkt, 0),
        "預估損益(已扣預估賣出成本)": _gemini_num(pnl, 0),
        "未扣賣出成本之損益": _gemini_num(pnl_gross, 0),
        "報酬率(%)": _gemini_num(ret_pct, 2),
        "現價相對成本(%)": _gemini_num(dist_pct, 2),
        "狀態": (
            "獲利中" if (pnl is not None and pnl > 0)
            else ("虧損中" if (pnl is not None and pnl < 0) else "持平／尚無報價")
        ),
    }
    return result


@st.cache_data(ttl=15 * 60)
def _fetch_taiex_market_summary():
    """FinMind 加權指數摘要：近況與均線位置。"""
    start = (datetime.today() - timedelta(days=140)).strftime("%Y-%m-%d")
    df = get_finmind_price_history("TAIEX", start)
    if df is None or df.empty or len(df) < 2:
        return {}
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    close = float(latest["Close"])
    prev_close = float(prev["Close"])
    def _ret(days):
        if len(df) <= days:
            return None
        base = float(df.iloc[-(days + 1)]["Close"])
        if base == 0:
            return None
        return (close / base - 1.0) * 100.0
    ma20 = float(latest["MA20"]) if pd.notna(latest.get("MA20")) else None
    ma60 = float(latest["MA60"]) if pd.notna(latest.get("MA60")) else None
    tone = "中性"
    if ma20 is not None and ma60 is not None:
        if close >= ma20 and close >= ma60:
            tone = "偏多"
        elif close < ma20 and close < ma60:
            tone = "偏空"
        elif close >= ma20:
            tone = "短多中性"
        else:
            tone = "短空中性"
    return {
        "資料日": latest.name.strftime("%Y-%m-%d"),
        "收盤": _gemini_num(close, 2),
        "日漲跌點": _gemini_num(close - prev_close, 2),
        "日漲跌幅(%)": _gemini_num((close - prev_close) / prev_close * 100, 2) if prev_close else None,
        "近5日漲跌幅(%)": _gemini_num(_ret(5), 2),
        "近20日漲跌幅(%)": _gemini_num(_ret(20), 2),
        "20日均線": _gemini_num(ma20, 2),
        "60日均線": _gemini_num(ma60, 2),
        "相對20日均線": (
            "之上" if ma20 is not None and close >= ma20
            else ("之下" if ma20 is not None else None)
        ),
        "相對60日均線": (
            "之上" if ma60 is not None and close >= ma60
            else ("之下" if ma60 is not None else None)
        ),
        "大盤環境判讀": tone,
    }


@st.cache_data(ttl=10 * 60)
def _fetch_tx_night_summary():
    """FinMind 台指期近月夜盤摘要。"""
    start = (datetime.today() - timedelta(days=12)).strftime("%Y-%m-%d")
    rows = _finmind_data("TaiwanFuturesDaily", "TX", start)
    if not rows:
        return {}
    night = [
        row for row in rows
        if str(row.get("trading_session") or "") == "after_market"
        and "/" not in str(row.get("contract_date") or "")
        and float(row.get("volume") or 0) > 0
        and float(row.get("close") or 0) > 0
    ]
    if not night:
        return {}
    # 同一日取成交量最大的近月約
    by_date = {}
    for row in night:
        d = str(row.get("date") or "")
        cur = by_date.get(d)
        if cur is None or float(row.get("volume") or 0) > float(cur.get("volume") or 0):
            by_date[d] = row
    ordered = [by_date[k] for k in sorted(by_date.keys())]
    latest = ordered[-1]
    close = float(latest.get("close") or 0)
    spread = latest.get("spread")
    spread_per = latest.get("spread_per")
    if spread is None and len(ordered) >= 2:
        prev_close = float(ordered[-2].get("close") or 0)
        if prev_close:
            spread = close - prev_close
            spread_per = (close / prev_close - 1.0) * 100.0
    tone = "中性"
    try:
        sp = float(spread_per) if spread_per is not None else None
    except (TypeError, ValueError):
        sp = None
    if sp is not None:
        if sp >= 0.35:
            tone = "偏多"
        elif sp <= -0.35:
            tone = "偏空"
        elif sp >= 0.1:
            tone = "微幅偏多"
        elif sp <= -0.1:
            tone = "微幅偏空"
    return {
        "資料日": str(latest.get("date") or ""),
        "合約": str(latest.get("contract_date") or ""),
        "夜盤收盤": _gemini_num(close, 0),
        "夜盤漲跌點": _gemini_num(spread, 0),
        "夜盤漲跌幅(%)": _gemini_num(spread_per, 2),
        "夜盤成交量": _gemini_num(latest.get("volume"), 0),
        "最高": _gemini_num(latest.get("max"), 0),
        "最低": _gemini_num(latest.get("min"), 0),
        "夜盤環境判讀": tone,
        "說明": "個股通常無完整夜盤；此為台指期近月夜盤，作為隔日開盤環境參考。",
    }


def _gemini_market_environment_snapshot(include_live=True):
    """大盤＋台指期夜盤；盤中可加 Yahoo 加權即時。"""
    session = taiwan_equity_session_status()
    taiex = {}
    night = {}
    live = {}
    try:
        taiex = _fetch_taiex_market_summary() or {}
    except Exception:
        taiex = {}
    try:
        night = _fetch_tx_night_summary() or {}
    except Exception:
        night = {}
    if include_live:
        try:
            live = get_index_live_summary() or {}
        except Exception:
            live = {}
    out = {
        "交易時段": session,
        "台股大盤": taiex or {"取得失敗": True},
        "台指期夜盤": night or {"取得失敗": True},
    }
    if live:
        out["加權指數即時"] = live
        # 盤中以即時判讀覆蓋「目前環境」提示
        if session.get("是否開盤中") and live.get("大盤環境判讀"):
            out["目前大盤環境（盤中）"] = live.get("大盤環境判讀")
        elif taiex.get("大盤環境判讀"):
            out["目前大盤環境"] = taiex.get("大盤環境判讀")
    elif taiex.get("大盤環境判讀"):
        out["目前大盤環境"] = taiex.get("大盤環境判讀")
    return out


def _gemini_snapshot(code, name, quote_data, prices, institutions):
    """建立只包含目前可見分析資訊的結構化資料快照。"""
    result = {
        "標的": {
            "代號": str(code),
            "名稱": str(name or "—"),
            "類型": "ETF" if is_etf_code(code) else "個股",
            "產生時間": datetime.now().strftime("%Y-%m-%d %H:%M"),
        },
        "市場環境": {},
        "報價": {},
        "技術與量價": {},
        "法人": {},
        "基本面": {},
        "持股狀態": {"是否持有": False},
    }
    try:
        result["市場環境"] = _gemini_market_environment_snapshot()
    except Exception:
        result["市場環境"] = {"取得失敗": True}
    if quote_data:
        current, previous = quote_data
        current, previous = _gemini_num(current), _gemini_num(previous)
        result["報價"] = {
            "現價": current,
            "昨收": previous,
            "漲跌幅(%)": _gemini_num((current - previous) / previous * 100)
            if current is not None and previous else None,
        }
    daily = _prepare_volume_frame(prices)
    if not daily.empty:
        latest = daily.iloc[-1]
        technical = {
            "日K資料日": latest.name.strftime("%Y-%m-%d"),
            "收盤": _gemini_num(latest.get("Close")),
            "MA5": _gemini_num(latest.get("MA5")),
            "MA20": _gemini_num(latest.get("MA20")),
            "MA60": _gemini_num(latest.get("MA60")),
            "近20日最高": _gemini_num(daily["High"].tail(20).max()),
            "近20日最低": _gemini_num(daily["Low"].tail(20).min()),
        }
        volume = analyze_volume(daily)
        if volume:
            technical.update({
                "成交量(張)": _gemini_num(volume["lots"], 0),
                "VMA20(張)": _gemini_num(volume["vma20"] / 1000, 0),
                "相對20日均量": f"{volume['ratio20']:.0%}",
                "量能判讀": volume["baseline"]["name"],
                "OBV/VMA判讀": volume["vma_obv"]["name"],
                "量價判讀": volume["price_volume"]["name"],
            })
        result["技術與量價"] = technical
    if institutions is not None and not institutions.empty:
        row = institutions.iloc[0]
        window_payload = {}
        for window_spec in INSTITUTIONAL_FLOW_WINDOWS:
            period_label, window_key, days, *_rest = window_spec
            agg = aggregate_institutional_window(institutions, days)
            flow = _classify_flow_for_window(institutions, code, window_spec)
            if not agg:
                window_payload[period_label] = None
                continue
            entry = {
                "期間": agg.get("date"),
                "交易日數": agg.get("window_days"),
                "外資買賣超(股)": _gemini_num(agg.get("外資買賣超"), 0),
                "投信買賣超(股)": _gemini_num(agg.get("投信買賣超"), 0),
                "自營商買賣超(股)": _gemini_num(agg.get("自營商買賣超"), 0),
                "三大法人合計(股)": _gemini_num(agg.get("三大法人合計"), 0),
                "外資(張)": _gemini_num((agg.get("外資買賣超") or 0) / 1000, 0),
                "投信(張)": _gemini_num((agg.get("投信買賣超") or 0) / 1000, 0),
                "自營商(張)": _gemini_num((agg.get("自營商買賣超") or 0) / 1000, 0),
                "三大法人合計(張)": _gemini_num((agg.get("三大法人合計") or 0) / 1000, 0),
                "判讀": (flow or {}).get("name"),
            }
            window_payload[period_label] = entry
        result["法人"] = {
            "說明": (
                "含微觀(1日)、短線(約10交易日累計)、宏觀(約250交易日累計)之三大法人買賣超；"
                "短線累計可作為短線籌碼依據，需與價量一併解讀，不可單獨下結論。"
            ),
            "最新單日": {
                "資料日": str(row.get("date") or "—"),
                "外資買賣超(股)": _gemini_num(row.get("外資買賣超"), 0),
                "投信買賣超(股)": _gemini_num(row.get("投信買賣超"), 0),
                "自營商買賣超(股)": _gemini_num(row.get("自營商買賣超"), 0),
                "三大法人合計(股)": _gemini_num(row.get("三大法人合計"), 0),
            },
            "視窗": window_payload,
            "微觀法人判讀": (window_payload.get("微觀(當天)") or {}).get("判讀"),
            "短線法人判讀": (window_payload.get("短線(5~10日)") or {}).get("判讀"),
            "宏觀法人判讀": (window_payload.get("宏觀(1年)") or {}).get("判讀"),
        }
    try:
        result["基本面"] = _gemini_fundamental_snapshot(code)
    except Exception:
        result["基本面"] = {"資料來源說明": {"本來就沒有、勿當成缺失抱怨": ["法人成本", "券商目標價"]}, "取得失敗": True}
    try:
        holding = _gemini_holding_snapshot(code, quote_data)
        if holding:
            result["持股狀態"] = holding
    except Exception:
        result["持股狀態"] = {"是否持有": False, "取得失敗": True}
    try:
        mainforce = _gemini_mainforce_snapshot(code)
        if mainforce:
            result["主力動向"] = mainforce
    except Exception:
        pass
    return result


def _request_gemini(snapshot, model):
    key = _gemini_api_key()
    if not key:
        return None, "尚未設定 GEMINI_API_KEY。"
    holding = snapshot.get("持股狀態") or {}
    has_holding = bool(holding.get("是否持有"))
    holding_rules = ""
    holding_section = ""
    word_limit = "約 500～700 字"
    if has_holding:
        word_limit = "約 550～800 字"
        holding_rules = (
            "- 快照含「持股狀態」：這檔在投資模式且目前有持股，必須加寫「持股狀態」段，"
            "並把成本均價、現價、預估損益一起納入判斷；續抱／加碼／減碼要依「價×量×籌碼」淨優勢選，不要預設保守。\n"
        )
        holding_section = """
## 持股狀態
（僅在有持股時輸出本段）白話、最多 4 點：
1. 用你的成本均價對比現價：目前偏賺／偏虧、大約差多少
2. 以目前盤面為主，擇一：偏續抱／偏逢回可加／偏先不加碼／偏考慮減碼（必須對齊一句話結論；並用一句帶過量能或短線法人是否支持）
3. 一個「看法仍成立／可更積極」的條件（最好同時含價位＋量能）
4. 一個「看法失效／該更保守」的條件（最好同時含價位＋量能）
禁止保證獲利、禁止叫人一次出清或一次加滿。
"""
    prompt = f"""你是台股複盤助理。只依下列資料快照分析，不可捏造新聞、法人成本、券商目標價或其他未提供資料。
空值就略過，不要寫長段「資料不足」。法人成本／券商目標價本來就沒有，完全不要提。
若「持股狀態.是否持有」為 false，不要虛構持股，也不要輸出「持股狀態」段。

核心任務：
把「市場環境（台股大盤、台指期夜盤）」「價格／均線」「成交量」「法人籌碼（尤其微觀與短線約10日）」交叉驗證後，寫成可執行劇本。
若快照有「主力動向」（使用者匯入），一併交叉驗證買賣超／家數差／5日與20日集中；與法人或價量矛盾時要明講。
不要只寫純價位；也不要把大盤／夜盤／量能／法人只丟在後面當點綴。

交叉驗證規則：
- 先看市場環境：大盤偏空時，個股偏多理由要更嚴格；夜盤大跌而個股日盤偏強，要降信心或改偏中性。
- 個股相對大盤：同向加分；逆勢走強可寫「相對強勢，但環境逆風」；逆勢走弱要提高風險權重。
- 夜盤是隔日開盤環境參考（非個股夜盤）；有缺資料就略過，不要捏造。
- 跌破均線或支撐：必須判斷「量縮跌破」還是「量增／爆量跌破」。
- 突破均線或壓力：必須判斷有無「量增配合」；無量突破要降信心。
- 短線法人（約10日）與價格同向 → 可提高信心；背離 → 降信心或改中性，並明講背離。
- 單日法人大買／大賣只能當點火，要對照短線累計與量價。
- 基本面可當加分／減分，但短線行動仍以環境×價×量×籌碼為主。

立場規則：
- 先比較偏多／偏空強度，結論跟淨優勢一致；多空接近才用中性。
- 禁止預設「觀望」「偏空」；證據偏多就要寫偏多／中性偏多，並給積極條件。
- 「怎麼做」至少要有一條轉強可更積極的路徑，也要有一條失效路徑。

寫作規則：
- 白話、少術語。均線寫「5日／20日／60日均線」。
- 成交量用「比最近20日平均量大／小」；可附張數，少用縮寫。
- 法人用「外資／投信／自營／三大法人」與「近10日合計買超／賣超」。
- 總字數約 {word_limit}；短句條列；不重複。
{holding_rules}
資料快照：
{json.dumps(snapshot, ensure_ascii=False, indent=2)}

嚴格依下列 Markdown 標題輸出（不要自創其他大標題），勿中途停止：

## 一句話結論
第一句：偏多／中性偏多／中性／中性偏空／偏空＋信心低／中／高。
接著用 2～4 句「融合說明」，必須同時點到（有缺才明講缺哪一塊）：
1) 大盤或夜盤環境（哪個比較關鍵就寫哪個，兩者都有更好）
2) 價格／均線現況
3) 量能（量增或量縮，相對20日均量）
4) 短線法人是否同向／背離
目標語氣接近：「大盤／夜盤…；個股價格…，量能…，短線法人…，因此…」。

## 好壞各看什麼
### 偏多理由
最多 3 點。優先寫環境同向、價量同向、價與籌碼同向。
### 偏空／需小心
最多 3 點。優先寫環境逆風、價量背離、價與籌碼背離。不要寫得比偏多理由更長、更聳動。

## 接下來盯這幾件事
正好 3 點。每點格式：看什麼 → 怎樣算轉強／轉弱。
規則：
- 至少 1 點是轉強條件，且同時含「價位＋量能」
- 至少 1 點是失效／轉弱條件，且同時含「價位＋量能」
- 至少 1 點提到短線法人或大盤／夜盤環境變化

## 怎麼做（教育用途）
用條件句，禁止保證獲利、全倉、槓桿、直接下單指令。
固定三小點，第 1 點依結論選，不可無故選觀望：
1. 現在：偏積極留意／偏持有觀察／偏條件式布局／偏暫時觀望（四選一＋一句理由；理由要提到環境、量或籌碼其一）
2. 若出現…（價＋量，必要時加大盤／夜盤或法人）可考慮分批更積極
3. 若出現…（價＋量，必要時加大盤／夜盤或法人）就當作看法失效／應更保守
{holding_section}
最後一行固定：本分析僅依有限且可能延遲的資料產生，不構成投資建議。"""
    return _gemini_generate(prompt, model)


def _gemini_generate(prompt, model):
    """呼叫 Gemini generateContent，回傳 (text, error)。"""
    key = _gemini_api_key()
    if not key:
        return None, "尚未設定 GEMINI_API_KEY。"
    # thinking 會佔用輸出額度，導致回覆被截斷；Flash 用低思考，Pro 保留基本推理
    model_id = str(model or "")
    generation_config = {
        "temperature": 0.4,
        "maxOutputTokens": 8192,
    }
    if model_id.startswith("gemini-2.5"):
        generation_config["thinkingConfig"] = {"thinkingBudget": 0}
    else:
        generation_config["thinkingConfig"] = {"thinkingLevel": "low"}

    try:
        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{quote(model_id, safe='')}:generateContent",
            params={"key": key},
            json={
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": generation_config,
            },
            timeout=90,
        )
        data = response.json()
    except requests.RequestException as exc:
        return None, f"Gemini 連線失敗：{exc}"
    except ValueError:
        return None, "Gemini 回傳格式無法解析。"
    if not response.ok:
        detail = (data.get("error") or {}).get("message") or f"HTTP {response.status_code}"
        if response.status_code == 429 or "prepayment credits are depleted" in str(detail).lower():
            return None, (
                "Gemini 額度已用完。請到 AI Studio 管理專案與帳單後再試："
                "https://aistudio.google.com/"
            )
        if response.status_code == 404 or "no longer available" in str(detail).lower():
            return None, (
                f"此模型目前不可用（{model}）。請改選清單中其他模型。"
            )
        return None, f"Gemini API 錯誤：{detail}"

    candidate = (data.get("candidates") or [{}])[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    # 略過 thought 區塊，只取實際回覆文字
    text = "".join(
        str(part.get("text") or "")
        for part in parts
        if not part.get("thought")
    ).strip()
    if not text:
        return None, "Gemini 沒有產生分析內容。"

    finish = str(candidate.get("finishReason") or "")
    if finish in ("MAX_TOKENS", "LENGTH"):
        text += "\n\n> ⚠️ 回應仍被長度上限截斷，請再按一次分析。"
    return text, None


def _empty_ai_history_store():
    return {"version": 1, "items": []}


def load_ai_analysis_history():
    """讀取 AI 分析歷史索引。"""
    if AI_HISTORY_INDEX.exists():
        try:
            with open(AI_HISTORY_INDEX, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and isinstance(raw.get("items"), list):
                items = []
                for entry in raw["items"]:
                    if not isinstance(entry, dict):
                        continue
                    items.append({
                        "id": str(entry.get("id") or ""),
                        "stock_id": str(entry.get("stock_id") or ""),
                        "stock_name": str(entry.get("stock_name") or ""),
                        "model": str(entry.get("model") or ""),
                        "created_at": str(entry.get("created_at") or ""),
                        "text": str(entry.get("text") or ""),
                        "md_file": str(entry.get("md_file") or ""),
                        "snapshot": entry.get("snapshot") if isinstance(entry.get("snapshot"), dict) else {},
                    })
                return {"version": 1, "items": [x for x in items if x["id"] and x["text"]]}
        except Exception:
            pass
    return _empty_ai_history_store()


def save_ai_analysis_history(store):
    """寫入 AI 分析歷史索引（原子寫入）。"""
    AI_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    cleaned = {"version": 1, "items": []}
    for entry in (store or {}).get("items") or []:
        if not isinstance(entry, dict):
            continue
        item_id = str(entry.get("id") or "").strip()
        text = str(entry.get("text") or "").strip()
        if not item_id or not text:
            continue
        cleaned["items"].append({
            "id": item_id,
            "stock_id": str(entry.get("stock_id") or ""),
            "stock_name": str(entry.get("stock_name") or ""),
            "model": str(entry.get("model") or ""),
            "created_at": str(entry.get("created_at") or ""),
            "text": text,
            "md_file": str(entry.get("md_file") or ""),
            "snapshot": entry.get("snapshot") if isinstance(entry.get("snapshot"), dict) else {},
        })
    cleaned["items"] = cleaned["items"][:AI_HISTORY_MAX_ITEMS]
    tmp = AI_HISTORY_INDEX.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)
    tmp.replace(AI_HISTORY_INDEX)
    return cleaned


def append_ai_analysis_history(stock_id, stock_name, model, text, snapshot=None):
    """新增一筆分析歷史，並同步寫出 Markdown 檔。"""
    now = datetime.now()
    stamp = now.strftime("%Y%m%d_%H%M%S")
    code = re.sub(r"[^\w\-]+", "", str(stock_id or "stock")) or "stock"
    model_tag = re.sub(r"[^\w\.\-]+", "", str(model or "gemini")) or "gemini"
    item_id = f"{stamp}_{code}"
    md_name = f"{item_id}_{model_tag}.md"
    AI_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    md_path = AI_HISTORY_DIR / md_name
    title = f"{code} {stock_name or ''}".strip()
    md_body = (
        f"# AI 分析：{title}\n\n"
        f"- 時間：{now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"- 模型：{model}\n"
        f"- 代號：{code}\n\n"
        f"---\n\n"
        f"{text.strip()}\n"
    )
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_body)

    store = load_ai_analysis_history()
    entry = {
        "id": item_id,
        "stock_id": str(stock_id),
        "stock_name": str(stock_name or ""),
        "model": str(model or ""),
        "created_at": now.strftime("%Y-%m-%d %H:%M"),
        "text": text.strip(),
        "md_file": md_name,
        "snapshot": snapshot if isinstance(snapshot, dict) else {},
    }
    store["items"] = [entry] + [
        x for x in store.get("items") or [] if str(x.get("id")) != item_id
    ]
    save_ai_analysis_history(store)
    return entry


def delete_ai_analysis_history(item_id):
    """刪除一筆歷史（含對應 Markdown）。"""
    item_id = str(item_id or "").strip()
    if not item_id:
        return
    store = load_ai_analysis_history()
    kept = []
    for entry in store.get("items") or []:
        if str(entry.get("id")) != item_id:
            kept.append(entry)
            continue
        md_name = str(entry.get("md_file") or "").strip()
        if md_name:
            md_path = AI_HISTORY_DIR / Path(md_name).name
            try:
                if md_path.exists():
                    md_path.unlink()
            except OSError:
                pass
    store["items"] = kept
    save_ai_analysis_history(store)


GEMINI_MODEL_OPTIONS = [
    ("gemini-3.5-flash", "3.5 Flash（建議／快速）"),
    ("gemini-3.7-flash", "3.7 Flash"),
    ("gemini-3.1-pro-preview", "3.1 Pro Preview（較深入）"),
    ("gemini-2.5-flash", "2.5 Flash"),
]


def render_ai_analysis_history(current_code):
    """分析模式：顯示可回顧的 AI 分析歷史（供分頁使用）。"""
    store = load_ai_analysis_history()
    items = store.get("items") or []
    if not items:
        st.caption("尚無歷史紀錄；成功產生分析後會自動存檔。")
        return

    scope = st.radio(
        "歷史範圍",
        options=["目前標的", "全部標的"],
        horizontal=True,
        key="ai_history_scope",
    )
    filtered = items
    if scope == "目前標的":
        filtered = [x for x in items if str(x.get("stock_id")) == str(current_code)]
    if not filtered:
        st.caption("此範圍尚無歷史紀錄。")
        return

    labels = []
    id_map = {}
    for entry in filtered:
        label = (
            f"{entry.get('created_at') or '—'}　"
            f"{entry.get('stock_id') or '—'} "
            f"{entry.get('stock_name') or ''}　"
            f"[{entry.get('model') or '—'}]"
        ).strip()
        labels.append(label)
        id_map[label] = entry

    picked = st.selectbox("選擇歷史紀錄", options=labels, key="ai_history_pick")
    entry = id_map.get(picked) or {}
    if not entry:
        return

    c1, c2 = st.columns([3, 1])
    with c1:
        st.caption(
            f"檔案：`ai_analysis_history/{entry.get('md_file') or '—'}`"
        )
    with c2:
        if st.button("刪除此筆", use_container_width=True, key=f"ai_hist_del_{entry.get('id')}"):
            delete_ai_analysis_history(entry.get("id"))
            st.rerun()

    with st.container(key="twmc_ai_result_hist"):
        st.markdown(entry.get("text") or "")
    snap = entry.get("snapshot") or {}
    if snap:
        with st.expander("當時傳送的資料快照"):
            st.json(snap)


def render_ai_analysis_panel(stock_id, stock_name, active_quote, volume_df=None, inst_df=None):
    """AI 分析外框＋分頁：本次分析／歷史紀錄。"""
    if volume_df is None or (isinstance(volume_df, pd.DataFrame) and volume_df.empty):
        end = datetime.today()
        start = end - timedelta(days=800)
        volume_df = get_finmind_price_history(
            stock_id,
            start.strftime("%Y-%m-%d"),
            end.strftime("%Y-%m-%d"),
        )
    if inst_df is None or not isinstance(inst_df, pd.DataFrame):
        inst_df = get_institutional_data_2m(stock_id)
    elif inst_df.empty:
        inst_df = get_institutional_data_2m(stock_id)

    with st.container(key="twmc_ai_analysis"):
        st.markdown(
            "<div class='twmc-ai-title'>AI 分析（Gemini）</div>",
            unsafe_allow_html=True,
        )
        tab_live, tab_history = st.tabs(["AI 分析", "歷史紀錄"])

        with tab_live:
            snapshot = _gemini_snapshot(
                stock_id,
                stock_name,
                active_quote,
                volume_df,
                inst_df,
            )
            holding = snapshot.get("持股狀態") or {}
            if holding.get("是否持有"):
                st.caption(
                    "此標的在投資模式且目前有持股：Gemini 會多做「持股狀態」分析"
                    "（成本、損益、續抱／保守條件）；不含法人成本與目標價，也不上傳截圖。"
                )
                h1, h2, h3, h4 = st.columns(4)
                h1.metric("持股股數", f"{holding.get('股數') or 0:,.0f}")
                h2.metric(
                    "成本均價",
                    f"{holding.get('成本均價'):,.2f}" if holding.get("成本均價") is not None else "—",
                )
                h3.metric(
                    "現價相對成本",
                    (
                        f"{holding.get('現價相對成本(%)'):+.2f}%"
                        if holding.get("現價相對成本(%)") is not None else "—"
                    ),
                )
                h4.metric(
                    "預估損益",
                    (
                        f"{holding.get('預估損益(已扣預估賣出成本)'):+,.0f}"
                        if holding.get("預估損益(已扣預估賣出成本)") is not None else "—"
                    ),
                )
            else:
                st.caption(
                    "Gemini 會收到市場環境（台股大盤、台指期夜盤）、技術、量價、法人與基本面；"
                    "若已匯入主力動向，也會一併引用。"
                    "不含法人成本與目標價，也不上傳截圖。若此股在投資模式且有持股，會自動加「持股狀態」。"
                )
            snapshot_id = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
            if not _gemini_api_key():
                st.info(
                    "請在 `.streamlit/secrets.toml` 加上 "
                    '`GEMINI_API_KEY = "你的 Gemini API Key"`，重新啟動後即可使用。'
                )
            else:
                ai_col, action_col = st.columns([1, 3])
                with ai_col:
                    model_labels = {mid: label for mid, label in GEMINI_MODEL_OPTIONS}
                    model = st.selectbox(
                        "Gemini 模型",
                        options=[mid for mid, _ in GEMINI_MODEL_OPTIONS],
                        format_func=lambda mid: model_labels.get(mid, mid),
                        help="已移除對新帳號不可用的 gemini-2.5-pro。",
                    )
                with action_col:
                    run_ai = st.button(
                        "以 Gemini 分析目前標的",
                        type="primary",
                        use_container_width=True,
                    )
                if run_ai:
                    with st.spinner("Gemini 正在產生條件式分析..."):
                        answer, error = _request_gemini(snapshot, model)
                    if error:
                        st.error(error)
                    else:
                        created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
                        st.session_state["gemini_analysis"] = {
                            "stock_id": stock_id,
                            "snapshot_id": snapshot_id,
                            "model": model,
                            "time": created_at,
                            "text": answer,
                        }
                        try:
                            append_ai_analysis_history(
                                stock_id,
                                stock_name,
                                model,
                                answer,
                                snapshot=snapshot,
                            )
                            st.success("已存入 AI 分析歷史。")
                        except Exception as exc:
                            st.warning(f"分析已產生，但歷史存檔失敗：{exc}")
                saved = st.session_state.get("gemini_analysis") or {}
                if (
                    saved.get("stock_id") == stock_id
                    and saved.get("snapshot_id") == snapshot_id
                ):
                    st.caption(
                        f"模型：{saved.get('model')}　產生時間：{saved.get('time')}"
                    )
                    with st.container(key="twmc_ai_result_live"):
                        st.markdown(saved.get("text") or "")
                with st.expander("查看本次傳送給 Gemini 的資料快照"):
                    st.json(snapshot)

        with tab_history:
            render_ai_analysis_history(stock_id)


def _portfolio_ai_scope():
    """投資模式 AI：目前卡片盒範圍的代號與顯示名稱。"""
    gid = st.session_state.active_group_id
    if gid == ALL_GROUP_ID:
        return "PORTFOLIO_ALL", "全部卡片盒"
    for group in st.session_state.groups or []:
        if group.get("id") == gid:
            return f"PORTFOLIO_{gid}", str(group.get("name") or "目前卡片盒")
    return f"PORTFOLIO_{gid}", "目前卡片盒"


def _gemini_portfolio_snapshot(snap, scope_label):
    """整理投資儀表板＋持倉，供整體部位 AI 分析。"""
    holdings = snap.get("holdings") or []
    total_cost = sum(float(row.get("cost") or 0) for row in holdings)
    total_mkt = float(snap.get("market_value") or 0)
    realized = float(snap.get("realized") or 0)
    unrealized = float(snap.get("unrealized") or 0)
    fee_tax = float(snap.get("fees_paid") or 0) + float(snap.get("taxes_paid") or 0)
    session = snap.get("session") or taiwan_equity_session_status()
    quote_mode = snap.get("quote_mode") or "日收盤（FinMind）"
    quote_sources = snap.get("quote_sources") or {}

    rows = []
    for row in holdings:
        code = str(row.get("code") or "")
        cost = float(row.get("cost") or 0)
        mkt = row.get("market_value")
        pnl = row.get("unrealized")
        qty = float(row.get("qty") or 0)
        avg = float(row.get("avg_cost") or 0)
        mark = row.get("mark")
        ret = (float(pnl) / cost * 100.0) if pnl is not None and cost > 0 else None
        day_chg = None
        day_pct = None
        try:
            live = get_live_quote(code)
            if live and live[0] is not None and live[1]:
                day_chg = float(live[0]) - float(live[1])
                day_pct = (float(live[0]) / float(live[1]) - 1.0) * 100.0
        except Exception:
            pass
        item = {
            "代號": code,
            "名稱": CODE_TO_NAME.get(code, ""),
            "股數": _gemini_num(qty, 0),
            "成本均價": _gemini_num(avg),
            "持有成本": _gemini_num(cost, 0),
            "現價": _gemini_num(mark),
            "報價來源": quote_sources.get(code) or "unknown",
            "今日漲跌": _gemini_num(day_chg),
            "今日漲跌幅(%)": _gemini_num(day_pct, 2),
            "市值": _gemini_num(mkt, 0),
            "預估損益": _gemini_num(pnl, 0),
            "報酬率(%)": _gemini_num(ret, 2),
            "佔總成本(%)": _gemini_num((cost / total_cost * 100.0), 1) if total_cost > 0 else None,
            "佔市值(%)": (
                _gemini_num((float(mkt) / total_mkt * 100.0), 1)
                if mkt is not None and total_mkt > 0 else None
            ),
        }
        rows.append(item)
    rows_sorted = sorted(rows, key=lambda item: abs(item.get("持有成本") or 0), reverse=True)
    top3 = rows_sorted[:3]
    max_share = max((item.get("佔總成本(%)") or 0) for item in rows_sorted) if rows_sorted else 0
    winners = sorted(
        [item for item in rows_sorted if (item.get("預估損益") or 0) > 0],
        key=lambda item: item.get("預估損益") or 0,
        reverse=True,
    )[:3]
    losers = sorted(
        [item for item in rows_sorted if (item.get("預估損益") or 0) < 0],
        key=lambda item: item.get("預估損益") or 0,
    )[:3]
    try:
        market_env = _gemini_market_environment_snapshot(include_live=True)
    except Exception:
        market_env = {"取得失敗": True}
    return {
        "分析類型": "投資模式／整體部位",
        "範圍": scope_label,
        "產生時間": now_taipei().strftime("%Y-%m-%d %H:%M"),
        "報價模式": quote_mode,
        "報價抓取時間": snap.get("quote_fetched_at") or now_taipei().strftime("%Y-%m-%d %H:%M:%S"),
        "交易時段": session,
        "市場環境": market_env,
        "儀表板": {
            "總成本": _gemini_num(total_cost, 0),
            "持有股票總市值": _gemini_num(total_mkt, 0),
            "持有檔數": len(holdings),
            "已實現損益": _gemini_num(realized, 0),
            "預估損益": _gemini_num(unrealized, 0),
            "合計損益": _gemini_num(realized + unrealized, 0),
            "累計手續費與稅": _gemini_num(fee_tax, 0),
            "預估報酬率(%)": (
                _gemini_num(unrealized / total_cost * 100.0, 2) if total_cost > 0 else None
            ),
        },
        "持倉明細": rows_sorted,
        "結構摘要": {
            "成本占比前三大": [
                {
                    "代號": item.get("代號"),
                    "名稱": item.get("名稱"),
                    "佔總成本(%)": item.get("佔總成本(%)"),
                    "預估損益": item.get("預估損益"),
                }
                for item in top3
            ],
            "最大單一持股成本占比(%)": _gemini_num(max_share, 1),
            "預估獲利前三大": winners,
            "預估虧損前三大": losers,
        },
        "資料說明": [
            "投資模式優先使用證交所 MIS 近即時報價重算市值與預估損益；失敗才用 Yahoo（約延遲20分）或 FinMind 日收盤",
            "盤中分析屬「當下快照」，不是逐秒串流；可按「重新抓取盤中報價」後再跑 AI",
            "預估損益已扣除預估賣出手續費與交易稅",
            "此分析不含個股完整技術／法人／財報細節，請以部位配置與風險管理為主",
            "不含法人成本與券商目標價",
        ],
    }


def _request_gemini_portfolio(snapshot, model):
    """投資組合整體部位分析。"""
    session = (snapshot.get("交易時段") or {})
    phase = session.get("時段") or "未知"
    is_open = bool(session.get("是否開盤中"))
    prompt = f"""你是台股「投資組合」複盤助理。只依下列部位快照分析，不可捏造未提供的個股技術、法人、財報、目標價或法人成本。
重點是整體部位健康度與調整建議，不是單一股票深挖。
目前交易時段：{phase}（盤中={'是' if is_open else '否'}）。若為盤中，請以「當下快照」語氣分析，並交叉參考市場環境與各股今日漲跌；勿假裝你有逐秒串流。

立場規則（避免過度保守）：
- 結論要跟部位淨結果一致：合計損益為正、多數持股報酬為正時，不要寫成偏弱或一味收斂風險。
- 禁止預設「觀望」「減碼」；獲利部位可以建議續抱或條件式加碼，虧損部位才談控管。
- 「調整建議」必須同時包含：可更積極的方向，以及需控管的方向；不要整段都在叫人縮手。
- 集中度高是風險提醒，不自動等於看空整體部位。
- 大盤偏空時，偏多／加碼建議要更嚴格；大盤偏多時，不要無故改成保守。

寫作規則：
- 用一般投資人看得懂的白話；少用術語。
- 先講整體，再點名少數關鍵持股（最多各 3 檔）。
- 總字數約 500～750 字；短句、條列；不要重複。
- 禁止保證獲利、全倉、槓桿、一次出清／一次加滿等指令。

資料快照：
{json.dumps(snapshot, ensure_ascii=False, indent=2)}

嚴格依下列 Markdown 標題輸出（不要自創其他大標題），勿中途停止：

## 一句話結論
第一句：部位偏強／中性偏強／中性／中性偏弱／偏弱＋信心低／中／高。
接著 2～3 句說明為什麼（用總成本、市值、合計損益、集中度、必要時大盤／今日漲跌），好壞並陳但結論對齊淨優勢。

## 部位現況
### 做得好的地方
最多 3 點（獲利來源、結構健康處優先）。
### 需要留意
最多 3 點（過度集中、虧損拖累、與大盤背離等）；不要寫得比「做得好」更長。

## 調整建議（教育用途）
用條件句，固定 4 點，且第 1 點不可無故選最保守：
1. 整體：偏維持並尋找加碼機會／偏持有觀察／偏調整結構／偏收斂風險（四選一＋一句理由）
2. 可考慮更積極的方向（點名最多 2 檔，寫轉強／加碼條件；若整體偏弱才可寫暫不加）
3. 可考慮控管的方向（點名最多 2 檔，寫減碼或停損條件）
4. 部位層級紀律：同時給「續抱／加碼仍成立」與「該更保守」各一個簡單條件

## 接下來盯這幾件事
正好 3 點。格式：看什麼 → 怎樣算部位轉強／轉弱。
其中至少 1 點要描述轉強條件。
若目前為盤中，至少 1 點要跟「今日剩餘盤勢／大盤」有關。

最後一行固定：本分析僅依有限且可能延遲的資料產生，不構成投資建議。"""
    return _gemini_generate(prompt, model)


def render_portfolio_ai_history(scope_id):
    """投資模式：整體部位 AI 歷史。"""
    store = load_ai_analysis_history()
    items = [
        entry for entry in (store.get("items") or [])
        if str(entry.get("stock_id") or "").startswith("PORTFOLIO")
    ]
    if not items:
        st.caption("尚無部位分析歷史；成功產生後會自動存檔。")
        return

    scope = st.radio(
        "歷史範圍",
        options=["目前範圍", "全部部位分析"],
        horizontal=True,
        key="portfolio_ai_history_scope",
    )
    filtered = items
    if scope == "目前範圍":
        filtered = [entry for entry in items if str(entry.get("stock_id")) == str(scope_id)]
    if not filtered:
        st.caption("此範圍尚無歷史紀錄。")
        return

    labels = []
    id_map = {}
    for entry in filtered:
        label = (
            f"{entry.get('created_at') or '—'}　"
            f"{entry.get('stock_name') or entry.get('stock_id') or '—'}　"
            f"[{entry.get('model') or '—'}]"
        ).strip()
        labels.append(label)
        id_map[label] = entry

    picked = st.selectbox("選擇歷史紀錄", options=labels, key="portfolio_ai_history_pick")
    entry = id_map.get(picked) or {}
    if not entry:
        return

    c1, c2 = st.columns([3, 1])
    with c1:
        st.caption(f"檔案：`ai_analysis_history/{entry.get('md_file') or '—'}`")
    with c2:
        if st.button("刪除此筆", use_container_width=True, key=f"pf_ai_del_{entry.get('id')}"):
            delete_ai_analysis_history(entry.get("id"))
            st.rerun()

    with st.container(key="twmc_ai_result_portfolio_hist"):
        st.markdown(entry.get("text") or "")
    snap = entry.get("snapshot") or {}
    if snap:
        with st.expander("當時傳送的資料快照"):
            st.json(snap)


def render_portfolio_ai_panel(snap):
    """投資模式：整體部位 AI 分析面板。"""
    scope_id, scope_label = _portfolio_ai_scope()
    snapshot = _gemini_portfolio_snapshot(snap, scope_label)
    snapshot_id = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
    dash = snapshot.get("儀表板") or {}

    with st.container(key="twmc_ai_analysis"):
        st.markdown(
            "<div class='twmc-ai-title'>AI 部位分析（Gemini）</div>",
            unsafe_allow_html=True,
        )
        st.caption(
            f"範圍：{scope_label}。報價：{snapshot.get('報價模式') or '—'}；"
            f"時段：{(snapshot.get('交易時段') or {}).get('時段') or '—'}。"
            "會餵入儀表板、持倉今日漲跌與市場環境；盤中可先「重新抓取盤中報價」再分析。"
            "不含個股完整技術／法人／財報，也不含法人成本與目標價。"
        )
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("總成本", f"{dash.get('總成本') or 0:,.0f}")
        m2.metric("持有市值", f"{dash.get('持有股票總市值') or 0:,.0f}")
        m3.metric("持有檔數", str(dash.get("持有檔數") or 0))
        ret = dash.get("預估報酬率(%)")
        m4.metric("預估報酬率", f"{ret:+.2f}%" if ret is not None else "—")

        tab_live, tab_history = st.tabs(["部位分析", "歷史紀錄"])
        with tab_live:
            if not snap.get("holdings"):
                st.info("目前此範圍沒有持倉，無法做部位分析。")
            elif not _gemini_api_key():
                st.info(
                    "請在 `.streamlit/secrets.toml` 加上 "
                    '`GEMINI_API_KEY = "你的 Gemini API Key"`，重新啟動後即可使用。'
                )
            else:
                ai_col, action_col = st.columns([1, 3])
                with ai_col:
                    model_labels = {mid: label for mid, label in GEMINI_MODEL_OPTIONS}
                    model = st.selectbox(
                        "Gemini 模型",
                        options=[mid for mid, _ in GEMINI_MODEL_OPTIONS],
                        format_func=lambda mid: model_labels.get(mid, mid),
                        key="portfolio_gemini_model",
                    )
                with action_col:
                    run_ai = st.button(
                        "以 Gemini 分析目前部位",
                        type="primary",
                        use_container_width=True,
                        key="portfolio_gemini_run",
                    )
                if run_ai:
                    with st.spinner("Gemini 正在產生整體部位分析..."):
                        answer, error = _request_gemini_portfolio(snapshot, model)
                    if error:
                        st.error(error)
                    else:
                        created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
                        st.session_state["gemini_portfolio_analysis"] = {
                            "scope_id": scope_id,
                            "snapshot_id": snapshot_id,
                            "model": model,
                            "time": created_at,
                            "text": answer,
                        }
                        try:
                            append_ai_analysis_history(
                                scope_id,
                                f"部位／{scope_label}",
                                model,
                                answer,
                                snapshot=snapshot,
                            )
                            st.success("已存入 AI 部位分析歷史。")
                        except Exception as exc:
                            st.warning(f"分析已產生，但歷史存檔失敗：{exc}")

                saved = st.session_state.get("gemini_portfolio_analysis") or {}
                if (
                    saved.get("scope_id") == scope_id
                    and saved.get("snapshot_id") == snapshot_id
                ):
                    st.caption(
                        f"模型：{saved.get('model')}　產生時間：{saved.get('time')}"
                    )
                    with st.container(key="twmc_ai_result_portfolio_live"):
                        st.markdown(saved.get("text") or "")
                with st.expander("查看本次傳送給 Gemini 的部位快照"):
                    st.json(snapshot)

        with tab_history:
            render_portfolio_ai_history(scope_id)


def render_trading_workspace(mode):
    """模擬模式與投資模式共用持倉／損益畫面，交易規則不同。"""
    is_sim = mode == "simulated"
    book_key = "simulated" if is_sim else "investment"
    book = st.session_state.portfolio[book_key]

    # 整塊工作區字體放大三倍（標題、指標、表單、持倉表、交易紀錄）
    # 直接寫進 parent document，避免 Streamlit 吃掉 markdown 裡的 <style>
    components.html(
        """
        <script>
        (function () {
          const doc = window.parent.document;
          const id = "twmc-trading-font-style";
          let style = doc.getElementById(id);
          if (!style) {
            style = doc.createElement("style");
            style.id = id;
            doc.head.appendChild(style);
          }
          style.textContent = `
            [class*="st-key-twmc_trading"] [data-testid="stMarkdownContainer"],
            [class*="st-key-twmc_trading"] [data-testid="stMarkdownContainer"] *,
            [class*="st-key-twmc_trading"] [data-testid="stCaptionContainer"],
            [class*="st-key-twmc_trading"] [data-testid="stCaptionContainer"] *,
            [class*="st-key-twmc_trading"] [data-testid="stMetricLabel"],
            [class*="st-key-twmc_trading"] [data-testid="stMetricLabel"] *,
            [class*="st-key-twmc_trading"] [data-testid="stMetricValue"],
            [class*="st-key-twmc_trading"] [data-testid="stMetricValue"] *,
            [class*="st-key-twmc_trading"] [data-testid="stWidgetLabel"],
            [class*="st-key-twmc_trading"] [data-testid="stWidgetLabel"] *,
            [class*="st-key-twmc_trading"] label,
            [class*="st-key-twmc_trading"] label *,
            [class*="st-key-twmc_trading"] input,
            [class*="st-key-twmc_trading"] textarea,
            [class*="st-key-twmc_trading"] button,
            [class*="st-key-twmc_trading"] [data-baseweb="select"],
            [class*="st-key-twmc_trading"] [data-baseweb="select"] *,
            [class*="st-key-twmc_trading"] [data-baseweb="input"],
            [class*="st-key-twmc_trading"] [data-baseweb="input"] *,
            [class*="st-key-twmc_trading"] [data-baseweb="base-input"],
            [class*="st-key-twmc_trading"] [data-baseweb="base-input"] *,
            [class*="st-key-twmc_trading"] [data-testid="stDataFrame"],
            [class*="st-key-twmc_trading"] [data-testid="stDataFrame"] *,
            [class*="st-key-twmc_trading"] [data-testid="stAlert"] *,
            [class*="st-key-twmc_trading"] [data-testid="stText"],
            [class*="st-key-twmc_trading"] [data-testid="stText"] * {
              font-size: 32px !important;
              line-height: 1.25 !important;
            }
            [class*="st-key-twmc_trading"] input,
            [class*="st-key-twmc_trading"] textarea,
            [class*="st-key-twmc_trading"] button,
            [class*="st-key-twmc_trading"] [data-baseweb="select"] > div {
              min-height: 56px !important;
              height: 56px !important;
              box-sizing: border-box !important;
            }
            /* 各種輸入元件「有邊框的那一層」高度不同（日期 56、文字／數字／下拉 45），
               所以直接對這幾層統一高度，日期才會跟其他框對齊。 */
            [class*="st-key-twmc_trading"] [data-testid="stDateInput"] [data-baseweb="input"],
            [class*="st-key-twmc_trading"] [data-testid="stTextInputRootElement"],
            [class*="st-key-twmc_trading"] [data-testid="stNumberInputContainer"],
            [class*="st-key-twmc_trading"] [class*="react-aria-ComboBox"] > div {
              height: 56px !important;
              min-height: 56px !important;
              max-height: 56px !important;
              box-sizing: border-box !important;
              display: flex !important;
              align-items: center !important;
              padding-top: 0 !important;
              padding-bottom: 0 !important;
            }
            [class*="st-key-twmc_trading"] [data-testid="stDateInput"] [data-baseweb="base-input"],
            [class*="st-key-twmc_trading"] [data-testid="stDateInput"] input,
            [class*="st-key-twmc_trading"] [data-testid="stTextInputRootElement"] input,
            [class*="st-key-twmc_trading"] [data-testid="stNumberInputContainer"] input,
            [class*="st-key-twmc_trading"] [class*="react-aria-ComboBox"] input {
              height: 54px !important;
              min-height: 54px !important;
              max-height: 54px !important;
              padding-top: 0 !important;
              padding-bottom: 0 !important;
            }
            /* 持倉表格（自行輸出的 HTML 表）：字放大三倍、水平垂直置中 */
            .twmc-holdings-table table {
              width: 100% !important;
              border-collapse: collapse !important;
              table-layout: auto !important;
            }
            .twmc-holdings-table th,
            .twmc-holdings-table td {
              font-size: 48px !important;
              line-height: 1.25 !important;
              text-align: center !important;
              vertical-align: middle !important;
              padding: 18px 14px !important;
              border-bottom: 1px solid rgba(255,255,255,0.12) !important;
              white-space: nowrap !important;
            }
            .twmc-holdings-table th {
              font-weight: 700 !important;
              color: #d8d8d8 !important;
              background: rgba(255,255,255,0.05) !important;
            }
            .twmc-holdings-table span {
              font-size: 48px !important;
            }
          `;
        })();
        </script>
        """,
        height=0,
    )

    with st.container(key="twmc_trading"):
        st.subheader("模擬模式" if is_sim else "投資模式")
        if is_sim:
            st.caption("虛擬資金、免手續費／交易稅，股數以整股輸入。資料只存在本機，不會送出真實委託。")
            if not book.get("initialized"):
                with st.form("sim_start_cash"):
                    cash = st.number_input(
                        "起始虛擬資金",
                        min_value=0.0,
                        step=10000.0,
                        value=float(book.get("starting_cash") or pf.DEFAULT_STARTING_CASH),
                        format="%.0f",
                    )
                    if st.form_submit_button("開始模擬"):
                        book["starting_cash"] = float(cash)
                        book["initialized"] = True
                        persist_portfolio()
                        st.rerun()
                return
        else:
            st.caption("這裡只記錄你實際投入市場的交易，不會串接券商或下單。買入手續費／交易稅併入成本；持倉「預估損益」會再扣預估賣出手續費（0.1425%、最低 20 元）與證交稅（股票 0.3%、ETF 0.1%）。")

        codes = [tx["code"] for tx in book.get("transactions") or []]
        if st.session_state.active_code:
            codes.append(st.session_state.active_code)
        # 投資模式優先盤中 Yahoo 現價；模擬模式維持 FinMind 日收盤
        prefer_live = not is_sim
        quotes, quote_sources = quotes_for_codes(codes, prefer_live=prefer_live)
        session = taiwan_equity_session_status()
        starting = float(book["starting_cash"]) if is_sim else None
        # 儀表板／持倉／脫手／交易紀錄依目前卡片盒過濾；「全部」顯示全部
        view_txs = box_scoped_transactions(book.get("transactions") or [])
        snap = pf.compute_snapshot(
            view_txs,
            quotes,
            starting_cash=starting,
            net_of_exit_costs=not is_sim,
        )
        snap["session"] = session
        snap["quote_sources"] = quote_sources
        live_n = sum(1 for c in quotes if quote_sources.get(c) == "mis")
        yahoo_n = sum(1 for c in quotes if quote_sources.get(c) == "yahoo_delayed")
        if prefer_live and live_n > 0:
            snap["quote_mode"] = "盤中近即時（證交所 MIS）"
        elif prefer_live and yahoo_n > 0:
            snap["quote_mode"] = "Yahoo（約延遲20分）"
        elif quotes:
            snap["quote_mode"] = "日收盤（FinMind）"
        else:
            snap["quote_mode"] = "無報價"
        snap["quote_fetched_at"] = now_taipei().strftime("%Y-%m-%d %H:%M:%S")
        # 模擬模式的現金仍以整本帳本計算，避免切換卡片盒時帳戶現金被拆開
        if is_sim:
            full_snap = pf.compute_snapshot(
                book.get("transactions") or [],
                quotes,
                starting_cash=starting,
                net_of_exit_costs=False,
            )
            snap["cash"] = full_snap["cash"]
            snap["equity"] = (full_snap["cash"] or 0) + snap["market_value"]

        if not is_sim:
            src_cap = snap.get("quote_mode") or "—"
            st.caption(
                f"報價：{src_cap}　抓取：{snap.get('quote_fetched_at')}　"
                f"時段：{session.get('時段')}（{session.get('台北時間')}）　"
                "Yahoo 台股官方延遲約 20 分，持倉現價改走證交所 MIS。"
            )
            if st.button("重新抓取盤中報價", key="invest_refresh_live_quotes"):
                clear_live_quote_caches()
                st.rerun()

        if is_sim:
            with st.container(key="twmc_sim_dashboard"):
                st.markdown("##### 模擬儀表板")
                m1, m2, m3, m4, m5 = st.columns(5)
                m1.metric("虛擬現金", f"{snap['cash']:,.0f}")
                m2.metric("持倉市值", f"{snap['market_value']:,.0f}")
                m3.metric("帳戶總值", f"{snap['equity']:,.0f}")
                total_pnl = snap["equity"] - float(book["starting_cash"])
                m4.markdown(f"**總損益**<br>{pnl_html(total_pnl)}", unsafe_allow_html=True)
                m5.markdown(
                    f"**已實現 / 未實現**<br>{pnl_html(snap['realized'])} / {pnl_html(snap['unrealized'])}",
                    unsafe_allow_html=True,
                )
        else:
            with st.container(key="twmc_invest_dashboard"):
                st.markdown(
                    "<div style='text-align:center;font-size:1.25rem;font-weight:600;"
                    "margin:0.2rem 0 0.75rem 0;padding-bottom:0.55rem;"
                    "border-bottom:1px solid #ff66cc;'>投資儀表板</div>",
                    unsafe_allow_html=True,
                )
                total_cost = sum(float(row.get("cost") or 0) for row in snap["holdings"])
                # 兩列共用 4 欄，讓「市值↔預估損益」「持有檔數↔合計損益」垂直對齊
                r1c1, r1c2, r1c3, r1c4 = st.columns(4)
                r1c1.metric("總成本", f"{total_cost:,.0f}")
                r1c2.metric("持有股票總市值", f"{snap['market_value']:,.0f}")
                r1c3.metric("持有檔數", str(len(snap["holdings"])))
                r1c4.write("")
                r2c1, r2c2, r2c3, r2c4 = st.columns(4)
                r2c1.markdown(f"已實現損益<br>{pnl_html(snap['realized'])}", unsafe_allow_html=True)
                r2c2.markdown(f"預估損益<br>{pnl_html(snap['unrealized'])}", unsafe_allow_html=True)
                r2c3.markdown(
                    f"合計損益<br>{pnl_html(snap['realized'] + snap['unrealized'])}",
                    unsafe_allow_html=True,
                )
                r2c4.metric(
                    "累計手續費／稅",
                    f"{snap.get('fees_paid', 0) + snap.get('taxes_paid', 0):,.0f}",
                )
                render_dashboard_pies(
                    snap["holdings"],
                    total_cost,
                    snap["market_value"],
                )

        default_code = st.session_state.active_code or ""
        default_price = 0.0
        if st.session_state.active_code:
            q = get_quote(st.session_state.active_code)
            if q:
                default_price = float(q[0])

        with st.container(key="twmc_holdings"):
            st.markdown(
                "<div style='text-align:center;font-size:1.25rem;font-weight:600;"
                "margin:0.2rem 0 0.75rem 0;padding-bottom:0.55rem;"
                "border-bottom:1px solid var(--twmc-mode-accent, #ff66cc);'>持倉</div>",
                unsafe_allow_html=True,
            )
            render_holdings_table(snap["holdings"])

    if not is_sim:
        render_portfolio_ai_panel(snap)

    with st.container(key="twmc_trading_more"):
        # 脫手只在「全部」卡片盒顯示
        if st.session_state.active_group_id == ALL_GROUP_ID:
            with st.container(key="twmc_exits"):
                st.markdown(
                    "<div style='text-align:center;font-size:1.25rem;font-weight:600;"
                    "margin:0.2rem 0 0.75rem 0;padding-bottom:0.55rem;"
                    "border-bottom:1px solid var(--twmc-mode-accent, #ff66cc);'>脫手</div>",
                    unsafe_allow_html=True,
                )
                render_exit_table(pf.exit_records(view_txs))

        st.markdown("##### 買進 / 賣出" if is_sim else "##### 新增實際交易紀錄")
        fee = 0.0
        tax = 0.0
        note = ""
        with st.form(f"{book_key}_trade_form", clear_on_submit=False):
            if is_sim:
                c1, c2, c3, c4, c5 = st.columns([2, 1, 1, 1, 2])
                raw_code = c1.text_input("代號或名稱", value=default_code)
                side_label = c2.selectbox("方向", ["買進", "賣出"])
                qty = c3.number_input("股數", min_value=0, step=1, value=1, format="%d")
                price = c4.number_input("價格", min_value=0.0, step=0.01, value=default_price, format="%.2f")
                note = c5.text_input("備註", value="")
                trade_ts = None
            else:
                c1, c2, c3, c4, c5, c6, c7 = st.columns([1.3, 1.5, 0.9, 0.9, 1, 1, 1.4])
                trade_day = c1.date_input("交易日期", value=date.today())
                raw_code = c2.text_input("代號或名稱", value=default_code)
                side_label = c3.selectbox("方向", ["買進", "賣出"])
                qty = c4.number_input("股數", min_value=0, step=1, value=1, format="%d")
                price = c5.number_input("成交價", min_value=0.0, step=0.01, value=default_price, format="%.2f")
                fee = c6.number_input("手續費", min_value=0.0, step=1.0, value=0.0, format="%.0f")
                tax = c7.number_input("交易稅", min_value=0.0, step=1.0, value=0.0, format="%.0f")
                trade_ts = datetime.combine(trade_day, datetime.min.time()).isoformat(timespec="seconds")
            _, btn_col = st.columns([5, 1])
            with btn_col:
                submitted = st.form_submit_button(
                    "送出模擬單" if is_sim else "記錄交易",
                    use_container_width=True,
                )

        if submitted:
            code, err = resolve_trade_code(raw_code)
            side = "buy" if side_label == "買進" else "sell"
            if err:
                st.session_state.trade_error = err
            else:
                updated, err = pf.append_trade(
                    book,
                    code,
                    side,
                    qty,
                    price,
                    note=note if is_sim else "",
                    ts=trade_ts,
                    starting_cash=starting,
                    fee=0.0 if is_sim else fee,
                    tax=0.0 if is_sim else tax,
                )
                if err:
                    st.session_state.trade_error = err
                else:
                    st.session_state.portfolio[book_key] = updated
                    st.session_state.active_code = code
                    st.session_state.trade_error = ""
                    persist_portfolio()
                    if code in all_codes():
                        persist_card_boxes()
                    st.rerun()

        if st.session_state.trade_error:
            st.warning(st.session_state.trade_error)

        st.markdown("##### 交易紀錄")
        render_trade_history(
            view_txs,
            allow_delete=not is_sim,
            book_key=book_key,
        )


st.markdown("""
    <style>
    /* 卡片區外框顏色依模式切換：分析白、模擬紫、投資粉 */
    .st-key-watchlist_box {
        border: 1px solid var(--twmc-mode-accent, #ffffff);
        border-radius: 6px;
        padding: 8px;
        box-shadow: 0 0 0 1px var(--twmc-mode-accent-soft, rgba(255, 255, 255, 0.18));
        transition: border-color .25s ease, box-shadow .25s ease;
    }
    /* 投資／模擬儀表板：模式色外框 */
    .st-key-twmc_invest_dashboard {
        border: 1px solid #ff66cc;
        border-radius: 6px;
        padding: 12px 14px 8px 14px;
        margin-bottom: 0.75rem;
        box-shadow: 0 0 0 1px rgba(255, 102, 204, 0.18);
        overflow: hidden;
    }
    .st-key-twmc_holdings {
        border: 1px solid var(--twmc-mode-accent, #ff66cc);
        border-radius: 6px;
        padding: 12px 14px 10px 14px;
        margin: 0.5rem 0 0.9rem 0;
        box-shadow: 0 0 0 1px var(--twmc-mode-accent-soft, rgba(255, 102, 204, 0.18));
    }
    .st-key-twmc_exits {
        border: 1px solid var(--twmc-mode-accent, #ff66cc);
        border-radius: 6px;
        padding: 12px 14px 10px 14px;
        margin: 0.5rem 0 0.9rem 0;
        box-shadow: 0 0 0 1px var(--twmc-mode-accent-soft, rgba(255, 102, 204, 0.18));
    }
    .st-key-twmc_notes {
        border: 1px solid #ff66cc;
        border-radius: 6px;
        padding: 12px 14px 14px 14px;
        margin: 0.5rem 0 0.9rem 0;
        box-shadow: 0 0 0 1px rgba(255, 102, 204, 0.18);
    }
    /* AI 分析區塊外框（分析模式白框） */
    .st-key-twmc_ai_analysis {
        border: 1px solid var(--twmc-mode-accent, #ffffff);
        border-radius: 6px;
        padding: 12px 14px 14px 14px;
        margin: 0.75rem 0 1rem 0;
        box-shadow: 0 0 0 1px var(--twmc-mode-accent-soft, rgba(255, 255, 255, 0.18));
    }
    .st-key-twmc_ai_analysis .twmc-ai-title {
        font-size: 1.35rem !important;
        font-weight: 700;
        line-height: 1.3 !important;
        margin: 0.15rem 0 0.35rem 0;
        padding-bottom: 0.45rem;
        border-bottom: 1px solid var(--twmc-mode-accent, rgba(255, 255, 255, 0.45));
    }
    /* AI 回傳結果：全子孫統一字級，避免 li/p/strong 互相覆蓋造成大小不一 */
    [class*="st-key-twmc_ai_result"] [data-testid="stMarkdownContainer"],
    [class*="st-key-twmc_ai_result"] [data-testid="stMarkdownContainer"] * {
        font-size: 20px !important;
        line-height: 1.65 !important;
    }
    [class*="st-key-twmc_ai_result"] [data-testid="stMarkdownContainer"] h1 {
        font-size: 28px !important;
        line-height: 1.35 !important;
        margin-top: 0.85rem !important;
    }
    [class*="st-key-twmc_ai_result"] [data-testid="stMarkdownContainer"] h2 {
        font-size: 24px !important;
        line-height: 1.4 !important;
        margin-top: 0.8rem !important;
    }
    [class*="st-key-twmc_ai_result"] [data-testid="stMarkdownContainer"] h3 {
        font-size: 22px !important;
        line-height: 1.4 !important;
        margin-top: 0.7rem !important;
    }
    [class*="st-key-twmc_ai_result"] [data-testid="stMarkdownContainer"] ul,
    [class*="st-key-twmc_ai_result"] [data-testid="stMarkdownContainer"] ol {
        margin-top: 0.35rem !important;
        margin-bottom: 0.55rem !important;
    }
    [class*="st-key-twmc_ai_result"] [data-testid="stMarkdownContainer"] li + li {
        margin-top: 0.25rem !important;
    }
    .st-key-twmc_notes .twmc-notes-title {
        text-align: center;
        font-size: 40px !important;
        font-weight: 700;
        line-height: 1.25 !important;
        margin: 0.2rem 0 0.85rem 0;
        padding-bottom: 0.55rem;
        border-bottom: 1px solid #ff66cc;
    }
    .st-key-twmc_notes .twmc-notes-section-title {
        text-align: center;
        font-size: 32px !important;
        font-weight: 700;
        line-height: 1.25 !important;
        margin: 0.75rem 0 0.55rem 0;
    }
    .st-key-twmc_notes .twmc-disc-ref-meta {
        margin: 0.35rem 0 0.85rem 0;
        padding: 0.55rem 0.75rem;
        border: 1px solid rgba(255, 102, 204, 0.35);
        border-radius: 6px;
        background: rgba(255, 102, 204, 0.06);
        font-size: 22px !important;
        line-height: 1.45 !important;
        color: #e8e8e8;
    }
    .st-key-twmc_notes .twmc-disc-ref-meta b {
        font-weight: 700;
    }
    .st-key-twmc_notes [class*="st-key-notes_reference_"] button {
        font-size: 28px !important;
        font-weight: 700 !important;
        min-height: 48px !important;
        border: 0 !important;
        background: transparent !important;
        color: #fafafa !important;
    }
    .st-key-twmc_notes [class*="st-key-notes_reference_"] button:hover {
        color: #ff66cc !important;
        background: rgba(255, 102, 204, 0.10) !important;
    }
    .st-key-twmc_notes_reference {
        border: 1px solid rgba(255, 102, 204, 0.6);
        border-radius: 6px;
        padding: 12px;
        margin: 0.25rem 0 0.8rem 0;
        background: rgba(255, 102, 204, 0.04);
    }
    .st-key-twmc_notes .twmc-notes-reference-title {
        text-align: center;
        font-size: 32px !important;
        font-weight: 700;
        margin: 0.1rem 0 0.6rem 0;
    }
    .st-key-twmc_notes_reference .twmc-ref-h {
        text-align: center;
        font-size: 24px !important;
        font-weight: 700;
        margin: 0.9rem 0 0.55rem 0;
    }
    .st-key-twmc_analyze_fundamental {
        border: 1px solid var(--twmc-mode-accent, #ffffff);
        border-radius: 6px;
        padding: 12px 14px 14px 14px;
        margin: 0.75rem 0 1rem 0;
        box-shadow: 0 0 0 1px var(--twmc-mode-accent-soft, rgba(255, 255, 255, 0.18));
    }
    .st-key-twmc_analyze_fundamental .twmc-ref-h {
        text-align: center;
        font-size: 24px !important;
        font-weight: 700;
        margin: 0.9rem 0 0.55rem 0;
    }
    .st-key-twmc_notes_reference .twmc-chips-h,
    .st-key-twmc_notes .twmc-chips-h,
    .twmc-chips-h {
        text-align: center;
        font-size: 24px !important;
        font-weight: 700;
        margin: 0.9rem 0 0.55rem 0;
        color: #fafafa;
    }
    .twmc-mf-lights {
        margin: 0.45rem 0 1rem 0;
        padding: 18px 18px 16px 18px;
        border: 1px solid rgba(255,255,255,0.16);
        border-radius: 10px;
        background: rgba(255,255,255,0.03);
    }
    .twmc-mf-pills {
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        justify-content: center;
        margin-bottom: 0.9rem;
    }
    .twmc-mf-pill {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 10px 18px;
        border-radius: 999px;
        font-size: 20px;
        font-weight: 700;
        letter-spacing: 0.02em;
        border: 1px solid rgba(255,255,255,0.18);
        background: rgba(0,0,0,0.25);
        color: #cfcfcf;
        opacity: 0.42;
        transition: opacity 0.15s ease, box-shadow 0.15s ease;
    }
    .twmc-mf-pill.is-on {
        opacity: 1;
        box-shadow: 0 0 0 1px rgba(255,255,255,0.08);
    }
    .twmc-mf-pill.twmc-mf-build.is-on {
        border-color: rgba(46, 204, 113, 0.7);
        color: #b8f5d0;
        background: rgba(46, 204, 113, 0.14);
    }
    .twmc-mf-pill.twmc-mf-attack.is-on {
        border-color: rgba(255, 99, 71, 0.75);
        color: #ffd0c4;
        background: rgba(255, 99, 71, 0.14);
    }
    .twmc-mf-pill.twmc-mf-wash.is-on {
        border-color: rgba(255, 209, 102, 0.75);
        color: #ffe7a8;
        background: rgba(255, 209, 102, 0.14);
    }
    .twmc-mf-pill.twmc-mf-danger.is-on {
        border-color: rgba(255, 71, 87, 0.85);
        color: #ffb3ba;
        background: rgba(255, 71, 87, 0.16);
    }
    .twmc-mf-primary {
        text-align: center;
        line-height: 1.5;
    }
    .twmc-mf-primary-label {
        display: block;
        font-size: 28px;
        font-weight: 800;
        color: #f5f5f5;
        margin-bottom: 0.4rem;
    }
    .twmc-mf-primary-reason {
        display: block;
        font-size: 18px;
        color: #d0d0d0;
        margin-bottom: 0.45rem;
    }
    .twmc-mf-inst {
        display: block;
        font-size: 16px;
        color: #a8a8a8;
        margin-top: 0.15rem;
    }
    .twmc-mf-playbook {
        margin: 0.15rem 0 0.85rem 0;
        padding: 14px 16px;
        border-radius: 10px;
        border: 1px solid rgba(255,255,255,0.2);
        background: rgba(0,0,0,0.28);
    }
    .twmc-mf-playbook.twmc-mf-pb-build {
        border-color: rgba(46, 204, 113, 0.55);
        background: rgba(46, 204, 113, 0.08);
    }
    .twmc-mf-playbook.twmc-mf-pb-attack {
        border-color: rgba(255, 99, 71, 0.55);
        background: rgba(255, 99, 71, 0.08);
    }
    .twmc-mf-playbook.twmc-mf-pb-wash {
        border-color: rgba(255, 209, 102, 0.55);
        background: rgba(255, 209, 102, 0.08);
    }
    .twmc-mf-playbook.twmc-mf-pb-danger {
        border-color: rgba(255, 71, 87, 0.6);
        background: rgba(255, 71, 87, 0.1);
    }
    .twmc-mf-pb-title {
        text-align: center;
        font-size: 22px;
        font-weight: 800;
        color: #f2f2f2;
        margin-bottom: 0.55rem;
    }
    .twmc-mf-pb-title b {
        color: #ffffff;
    }
    .twmc-mf-pb-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 10px 14px;
    }
    .twmc-mf-pb-grid > div {
        text-align: center;
        padding: 8px 6px;
        border-radius: 8px;
        background: rgba(0,0,0,0.22);
    }
    .twmc-mf-pb-grid > div.twmc-mf-pb-wide {
        grid-column: 1 / -1;
    }
    .twmc-mf-pb-grid span {
        display: block;
        font-size: 14px;
        color: #a8a8a8;
        margin-bottom: 4px;
    }
    .twmc-mf-pb-grid b {
        display: block;
        font-size: 18px;
        font-weight: 700;
        color: #f0f0f0;
        line-height: 1.35;
    }
    .twmc-flow-box {
        border: 1px solid rgba(255,255,255,0.18);
        border-radius: 8px;
        padding: 14px 16px 12px 16px;
        margin: 0.35rem 0 1rem 0;
        background: rgba(255,255,255,0.03);
    }
    .twmc-flow-box.tone-buy { border-color: rgba(239, 35, 42, 0.55); }
    .twmc-flow-box.tone-sell { border-color: rgba(20, 177, 67, 0.55); }
    .twmc-flow-box.tone-warning { border-color: rgba(255, 209, 102, 0.55); }
    .twmc-flow-title {
        text-align: center;
        font-size: 22px;
        font-weight: 700;
        margin: 0 0 0.75rem 0;
        color: #fafafa;
    }
    .twmc-flow-sides {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 10px;
        margin-bottom: 0.85rem;
    }
    .twmc-flow-sides.etf {
        grid-template-columns: repeat(2, 1fr);
    }
    .twmc-flow-side {
        text-align: center;
        padding: 8px 6px;
        border-radius: 6px;
        background: rgba(0,0,0,0.22);
    }
    .twmc-flow-side .k {
        color: #bdbdbd;
        font-size: 14px;
        margin-bottom: 4px;
    }
    .twmc-flow-side .v {
        font-size: 28px;
        font-weight: 700;
        line-height: 1.15;
    }
    .twmc-flow-side .n {
        font-size: 14px;
        font-weight: 600;
        margin-top: 2px;
    }
    .twmc-flow-block {
        font-size: 22px;
        line-height: 1.65;
        color: #e8e8e8;
        margin: 0.55rem 0;
    }
    .twmc-flow-block .lab {
        display: block;
        color: #b0b0b0;
        font-size: 18px;
        font-weight: 700;
        margin-bottom: 0.3rem;
    }
    .twmc-flow-date {
        margin-top: 0.65rem;
        color: #9a9a9a;
        font-size: 13px;
        text-align: center;
    }
    .twmc-flow-overview {
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 12px;
        margin: 0.35rem 0 1rem 0;
    }
    .twmc-flow-overview-card {
        border: 1px solid rgba(255,255,255,0.18);
        border-radius: 8px;
        padding: 16px 12px;
        text-align: center;
        background: rgba(255,255,255,0.03);
        min-height: 96px;
        display: flex;
        flex-direction: column;
        justify-content: center;
        gap: 8px;
    }
    .twmc-flow-overview-card.tone-buy { border-color: rgba(239, 35, 42, 0.55); }
    .twmc-flow-overview-card.tone-sell { border-color: rgba(20, 177, 67, 0.55); }
    .twmc-flow-overview-card.tone-warning { border-color: rgba(255, 209, 102, 0.55); }
    .twmc-flow-overview-card .ov-k {
        color: #bdbdbd;
        font-size: 14px;
        font-weight: 600;
    }
    .twmc-flow-overview-card .ov-v {
        color: #fafafa;
        font-size: 20px;
        font-weight: 700;
        line-height: 1.35;
    }
    @media (max-width: 900px) {
        .twmc-flow-overview {
            grid-template-columns: 1fr;
        }
    }
    .twmc-vol-metrics {
        display: grid;
        grid-template-columns: repeat(5, 1fr);
        gap: 10px;
        margin: 0.25rem 0 0.85rem 0;
    }
    .twmc-vol-metrics > div {
        text-align: center;
        padding: 10px 8px;
        border-radius: 6px;
        background: rgba(255,255,255,0.04);
        border: 1px solid rgba(255,255,255,0.12);
    }
    .twmc-vol-metrics span {
        display: block;
        color: #bdbdbd;
        font-size: 13px;
        margin-bottom: 4px;
    }
    .twmc-vol-metrics b {
        color: #fafafa;
        font-size: 18px;
    }
    .twmc-vol-grid {
        display: grid;
        grid-template-columns: 1fr;
        gap: 12px;
        margin: 0 0 1rem 0;
    }
    .twmc-vol-card {
        border: 1px solid rgba(255,255,255,0.18);
        border-radius: 8px;
        padding: 14px 16px 12px 16px;
        background: rgba(255,255,255,0.03);
    }
    .twmc-vol-card.tone-buy { border-color: rgba(239, 35, 42, 0.55); }
    .twmc-vol-card.tone-sell { border-color: rgba(20, 177, 67, 0.55); }
    .twmc-vol-card.tone-warning { border-color: rgba(255, 209, 102, 0.55); }
    .twmc-vol-card .vol-cat {
        color: #b0b0b0;
        font-size: 15px;
        font-weight: 700;
        margin-bottom: 0.35rem;
    }
    .twmc-vol-card .vol-name {
        color: #fafafa;
        font-size: 22px;
        font-weight: 700;
        margin-bottom: 0.55rem;
    }
    .twmc-vol-card .vol-flags {
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
        margin: 0 0 0.55rem 0;
        font-size: 14px;
    }
    .twmc-vol-card .vol-flags .on { color: #ef232a; }
    .twmc-vol-card .vol-flags .off { color: #888; }
    .twmc-vol-card .vol-block {
        font-size: 18px;
        line-height: 1.6;
        color: #e8e8e8;
        margin: 0.4rem 0;
    }
    .twmc-vol-card .vol-block .lab {
        display: block;
        color: #b0b0b0;
        font-size: 14px;
        font-weight: 700;
        margin-bottom: 0.2rem;
    }
    .twmc-vol-metrics,
    .twmc-vol-grid,
    .twmc-vol-card {
        overflow: visible;
    }
    .twmc-tip {
        position: relative;
        display: inline;
        border-bottom: 1px dashed rgba(255, 255, 255, 0.45);
        cursor: help;
        color: inherit;
    }
    .twmc-tip .twmc-tip-box {
        visibility: hidden;
        opacity: 0;
        position: absolute;
        z-index: 1000;
        left: 50%;
        bottom: calc(100% + 10px);
        transform: translateX(-50%);
        width: min(300px, 72vw);
        padding: 10px 12px;
        border-radius: 8px;
        background: #1a1a1a;
        border: 1px solid rgba(255, 255, 255, 0.28);
        box-shadow: 0 10px 28px rgba(0, 0, 0, 0.55);
        color: #eaeaea;
        font-size: 13px;
        font-weight: 400;
        line-height: 1.55;
        text-align: left;
        white-space: normal;
        pointer-events: none;
        transition: opacity 0.12s ease;
    }
    .twmc-tip .twmc-tip-box::after {
        content: "";
        position: absolute;
        left: 50%;
        top: 100%;
        transform: translateX(-50%);
        border: 7px solid transparent;
        border-top-color: rgba(255, 255, 255, 0.28);
    }
    .twmc-tip .twmc-tip-box b {
        display: block;
        color: #ffffff;
        font-size: 14px;
        font-weight: 700;
        margin-bottom: 6px;
    }
    .twmc-tip .twmc-tip-box .ex {
        display: block;
        color: #c4c4c4;
    }
    .twmc-tip:hover .twmc-tip-box,
    .twmc-tip:focus .twmc-tip-box,
    .twmc-tip:focus-within .twmc-tip-box {
        visibility: visible;
        opacity: 1;
    }
    @media (max-width: 900px) {
        .twmc-vol-metrics {
            grid-template-columns: repeat(2, 1fr);
        }
    }
    .st-key-twmc_notes_reference .twmc-theme-box {
        font-size: 16px;
        line-height: 1.65;
        color: #eee;
        margin: 0.2rem 0 0.6rem 0;
        white-space: normal;
    }
    .st-key-twmc_analyze_fundamental .twmc-theme-box {
        font-size: 16px;
        line-height: 1.65;
        color: #eee;
        margin: 0.2rem 0 0.6rem 0;
        white-space: normal;
    }
    .st-key-twmc_notes_reference .twmc-fin-table,
    .st-key-twmc_analyze_fundamental .twmc-fin-table {
        overflow-x: auto;
        margin: 0.4rem 0 0.8rem 0;
        width: 100%;
    }
    .st-key-twmc_notes_reference .twmc-fin-table table,
    .st-key-twmc_analyze_fundamental .twmc-fin-table table {
        border-collapse: collapse;
        width: 100%;
        table-layout: fixed;
        text-align: left;
    }
    .st-key-twmc_notes_reference .twmc-fin-table th,
    .st-key-twmc_notes_reference .twmc-fin-table td,
    .st-key-twmc_analyze_fundamental .twmc-fin-table th,
    .st-key-twmc_analyze_fundamental .twmc-fin-table td {
        text-align: left !important;
        padding: 0.45rem 0.6rem;
        border-bottom: 1px solid rgba(255,255,255,0.12);
        white-space: nowrap;
        font-size: 18px;
    }
    .st-key-twmc_notes_reference .twmc-fin-table th,
    .st-key-twmc_analyze_fundamental .twmc-fin-table th {
        color: #d0d0d0;
        font-weight: 700;
    }
    .st-key-twmc_notes_reference .twmc-fin-table th[scope="row"],
    .st-key-twmc_analyze_fundamental .twmc-fin-table th[scope="row"] {
        width: 6.5rem;
        color: #eee;
    }
    .st-key-twmc_notes_reference .twmc-fin-pos,
    .st-key-twmc_analyze_fundamental .twmc-fin-pos {
        color: #ef232a !important;
        font-weight: 600;
    }
    .st-key-twmc_notes_reference .twmc-fin-neg,
    .st-key-twmc_analyze_fundamental .twmc-fin-neg {
        color: #14b143 !important;
        font-weight: 600;
    }
    .st-key-twmc_notes textarea,
    .st-key-twmc_notes [data-testid="stTextArea"] textarea {
        font-size: 28px !important;
        line-height: 1.35 !important;
    }
    .st-key-twmc_notes .twmc-note-metric {
        padding: 0.15rem 0 0.55rem 0;
    }
    .st-key-twmc_notes .twmc-note-metric .k {
        font-size: 32px !important;
        line-height: 1.25 !important;
        color: rgba(250, 250, 250, 0.6);
        margin-bottom: 0.15rem;
    }
    .st-key-twmc_notes .twmc-note-metric .v,
    .st-key-twmc_notes .twmc-note-metric .v span {
        font-size: 32px !important;
        line-height: 1.25 !important;
        font-weight: 600;
        color: #fafafa;
    }
    .st-key-twmc_sim_dashboard {
        border: 1px solid #b042ff;
        border-radius: 6px;
        padding: 12px 14px 8px 14px;
        margin-bottom: 0.75rem;
        box-shadow: 0 0 0 1px rgba(176, 66, 255, 0.18);
    }
    .st-key-mode_switch_wrap {
        margin-bottom: 0.4rem;
    }
    /* 滾過卡片區後，個股名稱固定在上方。
       sticky 要掛在外層的 stLayoutWrapper，它的父層才是撐滿整頁高度的區塊；
       掛在內層 .st-key-* 上因為父層只有自身高度，會完全沒有可停留的空間。
       top 要留 Streamlit 固定頁首（約 68px）的高度，否則會被壓在頁首底下看不到。 */
    [data-testid="stLayoutWrapper"]:has(> .st-key-active_stock_bar) {
        position: sticky;
        top: 68px;
        z-index: 999999;
        background: #0e1117;
        border-bottom: 1px solid var(--twmc-mode-accent-line, #444444);
        padding: 0.7rem 0.9rem 1.1rem 0.9rem;
        margin-bottom: 0.75rem;
    }
    /* 行高要收緊，預設 1.6 會讓字的行框比容器還高、文字被推到貼近底線 */
    .twmc-bar {
        display: flex;
        align-items: baseline;
        flex-wrap: wrap;
        gap: 0.55rem;
        line-height: 1.2;
    }
    .twmc-bar span { line-height: 1.2; }
    .twmc-bar .twmc-name {
        font-size: 1.9rem;
        font-weight: 700;
        color: #fafafa;
    }
    .twmc-bar .twmc-price { font-size: 1.7rem; font-weight: 700; }
    .twmc-bar .twmc-change { font-size: 1.25rem; font-weight: 600; }
    /* 報價更新時短閃一下 */
    @keyframes twmc-price-flash {
        0% {
            filter: brightness(2.4);
            text-shadow: 0 0 14px currentColor;
            transform: scale(1.08);
        }
        55% {
            filter: brightness(1.35);
            text-shadow: 0 0 6px currentColor;
            transform: scale(1.03);
        }
        100% {
            filter: brightness(1);
            text-shadow: none;
            transform: scale(1);
        }
    }
    .twmc-price-flash {
        display: inline-block;
        animation: twmc-price-flash 0.65s ease-out;
    }
    .twmc-live-price-block.twmc-price-flash {
        display: block;
    }
    /* Streamlit 會給 markdown 容器 margin-bottom:-18px，
       導致外層量到的高度比文字還矮，padding-bottom 被文字吃掉、看起來貼底 */
    .st-key-active_stock_bar [data-testid="stMarkdownContainer"] {
        margin-bottom: 0 !important;
    }
    /* 已吸附在頂端時（由下方的 JS 加上 twmc-pinned）文字改為置中 */
    [data-testid="stLayoutWrapper"].twmc-pinned .twmc-bar {
        justify-content: center;
    }
    /* 偵測吸附狀態用的隱形元件，不要佔位也不要外框 */
    [class*="st-key-pin_watcher"] {
        height: 0 !important;
        min-height: 0 !important;
        overflow: hidden !important;
        margin: 0 !important;
        padding: 0 !important;
    }
    [class*="st-key-pin_watcher"] iframe {
        height: 0 !important;
        border: none !important;
    }
    /* 籌碼面表格：40pt、置中、加高列 */
    [class*="st-key-conc_table"] [data-testid="stDataFrame"] td,
    [class*="st-key-conc_table"] [data-testid="stDataFrame"] th,
    [class*="st-key-conc_table"] [data-testid="stDataFrame"] div,
    [class*="st-key-inst_table"] [data-testid="stDataFrame"] td,
    [class*="st-key-inst_table"] [data-testid="stDataFrame"] th,
    [class*="st-key-inst_table"] [data-testid="stDataFrame"] div {
        font-size: 40pt !important;
        text-align: center !important;
        justify-content: center !important;
        line-height: 1.25 !important;
    }
    [class*="st-key-conc_table"] [data-testid="stDataFrame"] [role="gridcell"],
    [class*="st-key-conc_table"] [data-testid="stDataFrame"] [role="columnheader"],
    [class*="st-key-inst_table"] [data-testid="stDataFrame"] [role="gridcell"],
    [class*="st-key-inst_table"] [data-testid="stDataFrame"] [role="columnheader"] {
        min-height: 64px !important;
        align-items: center !important;
    }
    </style>
""", unsafe_allow_html=True)

# 依模式注入主色。柔光與分隔線色在此先算成 rgba／hex，
# 避免使用 color-mix()（截圖用的 html2canvas 無法解析新式色彩語法）
_mode_accent = MODE_ACCENTS.get(st.session_state.app_mode, "#ffffff")
_accent_rgb = _hex_to_rgb(_mode_accent)
_mode_accent_soft = f"rgba({_accent_rgb[0]}, {_accent_rgb[1]}, {_accent_rgb[2]}, 0.18)"
_mode_accent_line = _mix_hex(_mode_accent, "#444444", 0.55)
st.markdown(
    f"<style>:root {{"
    f" --twmc-mode-accent: {_mode_accent};"
    f" --twmc-mode-accent-soft: {_mode_accent_soft};"
    f" --twmc-mode-accent-line: {_mode_accent_line};"
    f" }}</style>",
    unsafe_allow_html=True,
)

codes = visible_codes()
_cards_session = taiwan_equity_session_status()
_cards_live_every = (
    timedelta(seconds=2) if _cards_session.get("是否開盤中") else None
)

if api.logged_in():
    user_name = (st.session_state.get("api_user") or {}).get("username") or ""
    acc, btn = st.columns([5, 1])
    with acc:
        st.caption(f"已登入 {user_name}")
    with btn:
        if st.button("登出", use_container_width=True, key="twmc_logout"):
            for key in ("api_token", "api_user"):
                st.session_state.pop(key, None)
            st.rerun()

with st.container(key="mode_switch_wrap"):
    mode_event = mode_switch_component(
        active=st.session_state.app_mode,
        key="mode_switch",
        default=None,
    )
if mode_event and mode_event in MODE_KEYS and mode_event != st.session_state.app_mode:
    switch_app_mode(mode_event)
    st.rerun()


def _handle_watchlist_event(event):
    if not event or event == st.session_state.last_event:
        return
    st.session_state.last_event = event
    action = event.get("action")
    group_id = event.get("group_id") or st.session_state.active_group_id

    if action == "select_group":
        st.session_state.active_group_id = group_id
        st.session_state.show_add = False
        close_investment_notes()
        persist_card_boxes()
    elif action == "create_group":
        new_id = _next_group_id(st.session_state.groups)
        new_name = _next_group_name(st.session_state.groups)
        st.session_state.groups.append({"id": new_id, "name": new_name, "items": []})
        st.session_state.active_group_id = new_id
        st.session_state.show_add = False
        close_investment_notes()
        persist_card_boxes()
    elif action == "rename_group":
        target = event.get("target")
        name = (event.get("name") or "").strip()
        if target and name and name != "全部":
            for group in st.session_state.groups:
                if group["id"] == target:
                    group["name"] = name
                    persist_card_boxes()
                    break
    elif action == "delete_group":
        target = event.get("target")
        st.session_state.groups = [g for g in st.session_state.groups if g["id"] != target]
        if not st.session_state.groups:
            st.session_state.groups = [{"id": "custom-1", "name": DEFAULT_GROUP_NAME, "items": []}]
        if st.session_state.active_group_id == target:
            st.session_state.active_group_id = st.session_state.groups[0]["id"]
        st.session_state.show_add = False
        close_investment_notes()
        persist_card_boxes()
    elif action == "select":
        code = event.get("active")
        if st.session_state.app_mode == "investment":
            if st.session_state.notes_open and code == st.session_state.active_code:
                close_investment_notes()
            else:
                st.session_state.active_code = code
                st.session_state.notes_open = True
                st.session_state.notes_focus_section = None
                persist_card_boxes()
        else:
            st.session_state.active_code = code
            persist_card_boxes()
    elif action == "add":
        if st.session_state.active_group_id != ALL_GROUP_ID:
            st.session_state.show_add = not st.session_state.show_add
    elif action in ("reorder", "remove") and group_id != ALL_GROUP_ID:
        group = next((g for g in st.session_state.groups if g["id"] == group_id), None)
        if group is not None:
            if action == "reorder":
                order = [code for code in event.get("order", []) if code in group["items"]]
                leftovers = [code for code in group["items"] if code not in order]
                if order or leftovers:
                    group["items"] = order + leftovers
            else:
                target = event.get("target")
                if target in group["items"]:
                    group["items"].remove(target)
            persist_card_boxes()
    st.rerun()


def _render_watchlist_block():
    cards = build_watchlist_cards(prefer_live=True)
    groups_now = [{"id": g["id"], "name": g["name"]} for g in st.session_state.groups]
    with st.container(key="watchlist_box"):
        event = watchlist_component(
            items=cards,
            groups=groups_now,
            active=st.session_state.active_code,
            active_group_id=st.session_state.active_group_id,
            columns=COLUMNS_PER_ROW,
            accent=MODE_ACCENTS.get(st.session_state.app_mode, "#ffffff"),
            allow_reselect=st.session_state.app_mode == "investment",
            key="watchlist_cards",
            default=None,
        )

        if st.session_state.show_add and st.session_state.active_group_id != ALL_GROUP_ID:
            with st.form("add_form", clear_on_submit=True):
                field, submit = st.columns([4, 1])
                with field:
                    st.text_input(
                        "新增個股",
                        key="add_input",
                        label_visibility="collapsed",
                        placeholder="輸入股票代號或名稱，例如 2330 或 台積電",
                    )
                with submit:
                    st.form_submit_button("加入", use_container_width=True, on_click=add_item)
    _handle_watchlist_event(event)


if _cards_live_every is not None:
    @st.fragment(run_every=_cards_live_every)
    def _watchlist_live():
        _render_watchlist_block()

    _watchlist_live()
else:
    _render_watchlist_block()

if st.session_state.add_error:
    st.warning(st.session_state.add_error)

codes = visible_codes()
universe = all_codes()
if not universe and st.session_state.app_mode == "analyze":
    st.info("觀察清單是空的，請先切換到自訂卡片盒，再按「＋ 新增個股」加入。")
    st.stop()
if not universe and st.session_state.app_mode != "analyze":
    st.info("這個模式的卡片盒還是空的，可以新增個股，或直接在下方記錄交易。")

if universe and st.session_state.active_code not in universe:
    st.session_state.active_code = universe[0]
    close_investment_notes()
    persist_card_boxes()

st.caption(
    "操作提示",
    help="左側卡片盒可切換群組；點擊卡片切換分析標的。自訂卡片盒內可拖曳排序、新增與移除。「全部」只顯示去重後的卡片，不會改動各盒內容。投資／模擬模式下，儀表板、持倉、脫手與交易紀錄會依目前卡片盒過濾；切到「全部」則顯示全部。盤中卡片報價每 2 秒以證交所 MIS 更新。",
)

stock_id = st.session_state.active_code
stock_name = CODE_TO_NAME.get(stock_id, "") if stock_id else ""
_analyze_session = taiwan_equity_session_status()
_analyze_live_every = (
    timedelta(seconds=2)
    if (
        stock_id
        and st.session_state.app_mode == "analyze"
        and _analyze_session.get("是否開盤中")
    )
    else None
)

# 分析模式優先證交所 MIS 近即時；投資筆記／其他模式另處理
if stock_id and st.session_state.app_mode == "analyze":
    active_quote = get_live_quote(stock_id) or get_quote(stock_id)
    st.session_state["analyze_live_quote"] = active_quote
else:
    active_quote = get_quote(stock_id) if stock_id else None


def _render_active_stock_bar(quote_data, code, name, live_meta=None):
    bar_price = ""
    if quote_data:
        price, prev = quote_data
        change = price - prev
        pct = (change / prev) * 100 if prev else 0
        bar_color, arrow = quote_color(change)
        flash, flash_attr = _price_flash_class(f"flash_bar_price_{code}", price)
        bar_price = (
            f"<span class='twmc-price{flash}'{flash_attr} style='color:{bar_color};'>{price:.2f}</span>"
            f"<span class='twmc-change{flash}'{flash_attr} style='color:{bar_color};'>"
            f"{arrow}{abs(change):.2f} ({pct:+.2f}%)</span>"
        )
    title = f"{code} {name}".strip()
    st.markdown(
        f"<div class='twmc-bar'>"
        f"<span class='twmc-name'>{title}</span>"
        f"{bar_price}"
        f"</div>",
        unsafe_allow_html=True,
    )
    if live_meta:
        st.caption(live_meta)


# 滾過卡片區後，固定顯示目前分析標的（投資模式不需要）
if stock_id and st.session_state.app_mode != "investment":
    with st.container(key="active_stock_bar"):
        if st.session_state.app_mode == "analyze" and _analyze_live_every is not None:

            @st.fragment(run_every=_analyze_live_every)
            def _analyze_pinned_live_bar():
                q = get_live_quote(stock_id) or get_quote(stock_id)
                st.session_state["analyze_live_quote"] = q
                mis = (fetch_mis_quotes([stock_id]) or {}).get(stock_id) or {}
                meta = (
                    f"近即時（證交所 MIS）"
                    f"{'　行情 ' + mis['time'] if mis.get('time') else ''}"
                    f"　每 2 秒更新　{_analyze_session.get('時段')}"
                )
                _render_active_stock_bar(q, stock_id, stock_name, meta)
                b1, b2 = st.columns([1, 5])
                with b1:
                    if st.button("重新抓取", key="analyze_refresh_live_bar", use_container_width=True):
                        clear_live_quote_caches()
                        st.rerun()

            _analyze_pinned_live_bar()
            active_quote = st.session_state.get("analyze_live_quote") or active_quote
        else:
            meta = None
            if st.session_state.app_mode == "analyze":
                meta = f"報價：證交所 MIS（優先）　時段：{_analyze_session.get('時段')}"
            _render_active_stock_bar(active_quote, stock_id, stock_name, meta)
            if st.session_state.app_mode == "analyze":
                if st.button("重新抓取盤中報價", key="analyze_refresh_live_bar_closed"):
                    clear_live_quote_caches()
                    st.rerun()

    # CSS 沒有「已吸附」的選擇器，所以用一個隱形元件注入 JS，
    # 比對固定列與 sticky top 的距離來切換 .twmc-pinned
    with st.container(key="pin_watcher"):
        components.html(
            """
            <script>
            (function () {
                const doc = window.parent.document;
                if (doc.__twmcPinWatcher) return;
                doc.__twmcPinWatcher = true;
                setInterval(function () {
                    const bar = doc.querySelector('.st-key-active_stock_bar');
                    if (!bar) return;
                    const wrap = bar.closest('[data-testid="stLayoutWrapper"]');
                    if (!wrap) return;
                    const stickyTop = parseFloat(getComputedStyle(wrap).top) || 0;
                    const pinned = wrap.getBoundingClientRect().top <= stickyTop + 1;
                    wrap.classList.toggle('twmc-pinned', pinned);
                }, 150);
            })();
            </script>
            """,
            height=0,
        )

if st.session_state.app_mode in ("simulated", "investment"):
    if (
        st.session_state.app_mode == "investment"
        and st.session_state.notes_open
        and st.session_state.active_code
    ):
        render_investment_notes(st.session_state.active_code)
    else:
        render_trading_workspace(st.session_state.app_mode)
    st.stop()

if not stock_id:
    st.info("請先在卡片盒選擇或新增個股。")
    st.stop()



def render_analyze_export_toolbar(stock_id):
    """分析模式：匯出目前頁面截圖（排除卡片區；分頁僅擷取目前可見內容）。"""
    code = re.sub(r"[^\w\-]+", "", str(stock_id or "stock")) or "stock"
    stamp = datetime.today().strftime("%Y%m%d_%H%M")
    filename = f"TWMC_分析_{code}_{stamp}.png"
    components.html(
        f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<style>
  html, body {{
    margin: 0; padding: 0; background: transparent;
    font-family: "Segoe UI", "Microsoft JhengHei", sans-serif;
  }}
  .wrap {{
    display: flex; align-items: center; justify-content: flex-end;
    gap: 10px; padding: 2px 0 6px 0;
  }}
  button {{
    appearance: none; border: 1px solid rgba(255,255,255,0.35);
    background: rgba(255,255,255,0.06); color: #f5f5f5;
    border-radius: 8px; padding: 8px 14px; font-size: 14px;
    font-weight: 600; cursor: pointer;
  }}
  button:hover {{ background: rgba(255,255,255,0.12); }}
  button:disabled {{ opacity: 0.55; cursor: wait; }}
  .msg {{ color: #bdbdbd; font-size: 12px; min-height: 1em; }}
  .msg.err {{ color: #ff8a80; }}
  .msg.ok {{ color: #80cbc4; }}
</style>
</head>
<body>
  <div class="wrap">
    <span class="msg" id="msg"></span>
    <button type="button" id="btn">匯出分析截圖</button>
  </div>
  <script>
  (function () {{
    const btn = document.getElementById("btn");
    const msg = document.getElementById("msg");
    const fileName = {json.dumps(filename)};
    const H2C_SRC = "https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js";
    const MAX_PIXELS = 40e6;

    function setMsg(text, cls) {{
      msg.textContent = text || "";
      msg.className = "msg" + (cls ? (" " + cls) : "");
    }}

    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

    function loadHtml2Canvas(win) {{
      return new Promise((resolve, reject) => {{
        if (win.html2canvas) return resolve(win.html2canvas);
        const existing = win.document.querySelector("script[data-twmc-h2c]");
        if (existing) {{
          existing.addEventListener("load", () => resolve(win.html2canvas));
          existing.addEventListener("error", () => reject(new Error("截圖元件載入失敗")));
          return;
        }}
        const s = win.document.createElement("script");
        s.src = H2C_SRC;
        s.async = true;
        s.dataset.twmcH2c = "1";
        s.onload = () => resolve(win.html2canvas);
        s.onerror = () => reject(new Error("截圖元件載入失敗"));
        win.document.head.appendChild(s);
      }});
    }}

    function canvasToBlob(canvas) {{
      return new Promise((resolve, reject) => {{
        try {{
          canvas.toBlob(
            (blob) => (blob && blob.size > 0 ? resolve(blob) : reject(new Error("產生的圖片是空的"))),
            "image/png"
          );
        }} catch (err) {{
          reject(err);
        }}
      }});
    }}

    function downloadBlob(win, doc, blob, name) {{
      const url = win.URL.createObjectURL(blob);
      const a = doc.createElement("a");
      a.href = url;
      a.download = name;
      a.rel = "noopener";
      a.style.display = "none";
      doc.body.appendChild(a);
      a.click();
      setTimeout(() => {{
        a.remove();
        win.URL.revokeObjectURL(url);
      }}, 2000);
    }}

    const BAD_TOKENS = ["oklch", "oklab", "lab(", "lch(", "color-mix", "hwb(", "light-dark", "color("];
    const COLOR_PROPS = [
      "color", "backgroundColor", "borderTopColor", "borderRightColor",
      "borderBottomColor", "borderLeftColor", "outlineColor",
      "textDecorationColor", "caretColor", "columnRuleColor",
      "webkitTextStrokeColor", "webkitTextFillColor", "fill", "stroke",
    ];

    const toKebab = (p) => p.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase()).replace(/^webkit-/, "-webkit-");

    function hasBadColor(value) {{
      if (!value) return false;
      const s = String(value).toLowerCase();
      return BAD_TOKENS.some((t) => s.indexOf(t) !== -1);
    }}

    function makeColorResolver(doc) {{
      const cv = doc.createElement("canvas");
      cv.width = 1;
      cv.height = 1;
      const cx = cv.getContext("2d", {{ willReadFrequently: true }});
      return function (value) {{
        try {{
          cx.clearRect(0, 0, 1, 1);
          cx.fillStyle = "#000000";
          cx.fillStyle = value;
          cx.fillRect(0, 0, 1, 1);
          const d = cx.getImageData(0, 0, 1, 1).data;
          return "rgba(" + d[0] + "," + d[1] + "," + d[2] + "," + (d[3] / 255).toFixed(3) + ")";
        }} catch (e) {{
          return "rgba(0,0,0,0)";
        }}
      }};
    }}

    // html2canvas 1.4 不支援 oklch()／color-mix() 等新式色彩，先在複製出的文件換成 rgba
    function sanitizeColors(clonedDoc) {{
      try {{
        const view = clonedDoc.defaultView || window.parent;
        const resolve = makeColorResolver(clonedDoc);
        clonedDoc.querySelectorAll("*").forEach((el) => {{
          let cs = null;
          try {{
            cs = view.getComputedStyle(el);
          }} catch (e) {{
            return;
          }}
          if (!cs) return;
          COLOR_PROPS.forEach((prop) => {{
            const v = cs[prop];
            if (hasBadColor(v)) {{
              el.style.setProperty(toKebab(prop), resolve(v), "important");
            }}
          }});
          if (hasBadColor(cs.backgroundImage)) {{
            el.style.setProperty("background-image", "none", "important");
          }}
          if (hasBadColor(cs.boxShadow)) {{
            el.style.setProperty("box-shadow", "none", "important");
          }}
        }});
        const style = clonedDoc.createElement("style");
        style.textContent = "*::before,*::after{{box-shadow:none !important;}}";
        if (clonedDoc.head) clonedDoc.head.appendChild(style);
      }} catch (e) {{
        console.warn("[TWMC export] sanitize skipped", e);
      }}
    }}

    // ECharts 等圖表畫在 iframe 內的 <canvas>，html2canvas 抓不到；同源時直接複製像素。
    function paintIframeCanvases(doc, out, rootRect, cropTop, scale) {{
      const ctx = out.getContext("2d");
      doc.querySelectorAll("iframe").forEach((iframe) => {{
        const fr = iframe.getBoundingClientRect();
        if (fr.width < 8 || fr.height < 8) return;
        let idoc = null;
        try {{
          idoc = iframe.contentDocument;
        }} catch (e) {{
          return;
        }}
        if (!idoc || !idoc.body) return;
        if (idoc.getElementById("btn")) return;
        idoc.querySelectorAll("canvas").forEach((cv) => {{
          if (!cv.width || !cv.height) return;
          const cr = cv.getBoundingClientRect();
          const x = (fr.left - rootRect.left + cr.left) * scale;
          const y = (fr.top - rootRect.top + cr.top) * scale - cropTop;
          const w = cr.width * scale;
          const h = cr.height * scale;
          if (y + h < 0 || y > out.height) return;
          try {{
            ctx.drawImage(cv, x, y, w, h);
          }} catch (e) {{}}
        }});
      }});
    }}

    async function exportShot() {{
      const win = window.parent;
      const doc = win.document;
      btn.disabled = true;
      setMsg("截圖產生中…");

      const prevScrollX = win.scrollX;
      const prevScrollY = win.scrollY;
      const restore = [];
      try {{
        const html2canvas = await loadHtml2Canvas(win);
        if (!html2canvas) throw new Error("截圖元件不可用");

        const start =
          doc.querySelector(".st-key-active_stock_bar") ||
          doc.querySelector(".st-key-twmc_analyze_export_start");
        const end = doc.querySelector(".st-key-twmc_analyze_export_end");
        const root =
          doc.querySelector("[data-testid='stMainBlockContainer']") ||
          doc.querySelector("[data-testid='stAppViewContainer']");
        if (!start || !end || !root) throw new Error("找不到可匯出的分析內容");

        // 只隱藏匯出工具列本身，用 visibility 保留版面高度（不動任何祖先節點）
        const toolbar = doc.querySelector(".st-key-twmc_export_toolbar");
        if (toolbar) {{
          restore.push([toolbar, toolbar.style.visibility]);
          toolbar.style.visibility = "hidden";
        }}

        // 捲到頂端量測，避免 sticky 標的列造成座標偏移
        win.scrollTo(0, 0);
        await sleep(320);

        const rootRect = root.getBoundingClientRect();
        const startTop = Math.max(0, Math.round(start.getBoundingClientRect().top - rootRect.top));
        const endTop = Math.round(end.getBoundingClientRect().top - rootRect.top);
        const cropHeight = Math.max(40, endTop - startTop);
        const width = Math.max(320, Math.round(root.scrollWidth));

        let scale = 1;
        if (width * cropHeight * scale * scale > MAX_PIXELS) {{
          scale = Math.max(0.5, Math.sqrt(MAX_PIXELS / (width * cropHeight)));
        }}

        const full = await html2canvas(root, {{
          backgroundColor: "#0e1117",
          scale: scale,
          logging: false,
          useCORS: true,
          allowTaint: false,
          removeContainer: true,
          scrollX: 0,
          scrollY: 0,
          windowWidth: width,
          windowHeight: Math.round(root.scrollHeight),
          onclone: sanitizeColors,
        }});
        if (!full || full.width < 10 || full.height < 10) throw new Error("截圖尺寸異常");

        const cropTop = Math.round(startTop * scale);
        const out = doc.createElement("canvas");
        out.width = full.width;
        out.height = Math.max(1, Math.min(full.height - cropTop, Math.round(cropHeight * scale)));
        const ctx = out.getContext("2d");
        ctx.fillStyle = "#0e1117";
        ctx.fillRect(0, 0, out.width, out.height);
        ctx.drawImage(full, 0, cropTop, full.width, out.height, 0, 0, out.width, out.height);

        paintIframeCanvases(doc, out, rootRect, cropTop, scale);

        const blob = await canvasToBlob(out);
        downloadBlob(win, doc, blob, fileName);
        setMsg("已下載（" + Math.round(blob.size / 1024) + " KB）", "ok");
      }} catch (err) {{
        console.error("[TWMC export]", err);
        const tip = (err && err.message) ? String(err.message) : "截圖失敗，請重試";
        setMsg(tip.slice(0, 48), "err");
      }} finally {{
        restore.forEach(([node, v]) => {{
          node.style.visibility = v;
        }});
        win.scrollTo(prevScrollX, prevScrollY);
        btn.disabled = false;
      }}
    }}

    btn.addEventListener("click", exportShot);
  }})();
  </script>
</body>
</html>
        """,
        height=52,
    )


# 日期選擇
today = datetime.today()
start_date = datetime(1990, 1, 1)
end_date = today


# ==========================================
# 匯出工具列（不含卡片區）
# ==========================================
with st.container(key="twmc_export_toolbar"):
    render_analyze_export_toolbar(stock_id)

with st.container(key="twmc_analyze_export_start"):
    st.markdown(
        "<div class='twmc-export-anchor' aria-hidden='true'></div>",
        unsafe_allow_html=True,
    )


# ==========================================
# AI 分析（Gemini）— 置於卡片盒／標的列下方，方便切換後立即操作
# ==========================================
active_quote = st.session_state.get("analyze_live_quote") or active_quote
render_ai_analysis_panel(stock_id, stock_name, active_quote)

# ==========================================
# 1. 即時股價與今日走勢圖 (折線圖)
# ==========================================
def render_analyze_intraday_section(stock_id):
    """分析模式：MIS 近即時現價＋分時（Yahoo 底圖接 MIS 軌跡）。"""
    st.subheader(f"{stock_id} 今日即時走勢")

    with st.spinner("載入近即時走勢中..."):
        live_quote, df_today, live_meta = build_analyze_intraday_frame(stock_id)
        if live_quote:
            st.session_state["analyze_live_quote"] = live_quote

    if live_quote:
        current_price, prev_close = live_quote
        change = current_price - prev_close
        pct_change = (change / prev_close * 100) if prev_close else 0
        price_color, arrow = quote_color(change)
        flash, flash_attr = _price_flash_class(f"flash_intraday_price_{stock_id}", current_price)
        st.markdown(
            f"<div class='twmc-live-price-block{flash}'{flash_attr} "
            f"style='color:{price_color}; font-size:2.6rem; font-weight:700; line-height:1.2;'>"
            f"{current_price:.2f}"
            f"<span style='font-size:1.5rem; font-weight:600; margin-left:0.4rem;'>"
            f"{arrow}{abs(change):.2f} ({pct_change:+.2f}%)</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
        mis_t = live_meta.get("mis_time") or ""
        st.caption(
            f"現價／分時：{live_meta.get('source') or '—'}　"
            f"{('行情 ' + mis_t + '　') if mis_t else ''}"
            f"抓取 {live_meta.get('fetched_at') or ''}　"
            "日 K／籌碼仍以 FinMind 為準。"
        )
        r1, r2 = st.columns([1, 5])
        with r1:
            if st.button("重新抓取", key=f"analyze_intraday_refresh_{stock_id}", use_container_width=True):
                clear_live_quote_caches()
                st.rerun()
    else:
        st.info("目前無即時報價。")

    if not df_today.empty and live_quote:
        base = round(float(live_quote[1]), 2)

        # 收盤價序列，去掉沒有成交的空值
        points = [
            (ts, round(float(p), 2))
            for ts, p in df_today["Close"].items()
            if pd.notna(p)
        ]
        if not points:
            st.info("目前無今日即時交易資料（可能尚未開盤或抓取失敗）。")
            return

        # 在股價穿越昨收價的地方插入「精確交叉點」，
        # 讓紅段與綠段在昨收價上完全接合，不會出現斷線。
        up, down = [], []

        def add(ts, value):
            t = ts.strftime("%Y-%m-%d %H:%M:%S")
            # 剛好等於昨收價的點同時屬於兩條線，避免線段在平盤處斷開
            up.append([t, value if value >= base else None])
            down.append([t, value if value <= base else None])

        for i, (ts, price) in enumerate(points):
            if i > 0:
                prev_ts, prev_price = points[i - 1]
                straddles = (prev_price - base) * (price - base) < 0
                if straddles:
                    ratio = (base - prev_price) / (price - prev_price)
                    cross_ts = prev_ts + (ts - prev_ts) * ratio
                    cross_t = cross_ts.strftime("%Y-%m-%d %H:%M:%S")
                    # 交叉點同時給紅線和綠線，兩段就能在此接合
                    up.append([cross_t, base])
                    down.append([cross_t, base])
            add(ts, price)

        # X 軸固定顯示台股盤中 09:00 ~ 13:30
        session_day = points[0][0].strftime("%Y-%m-%d")

        # 高／低點要取自實際畫出來的收盤序列，否則標註會離線很遠
        high_ts, high_val = max(points, key=lambda item: item[1])
        low_ts, low_val = min(points, key=lambda item: item[1])
        open_val = float(points[0][1])
        close_val = float(points[-1][1])

        def annotation_series(name, value, ts, position, color):
            # 文字水平置中對齊該時間點，避免看起來往右偏
            return {
                "name": name,
                "type": "scatter",
                "clip": False,
                "data": [[
                    pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
                    round(float(value), 2),
                ]],
                "symbolSize": 1,
                "itemStyle": {"color": "rgba(0,0,0,0)"},
                "label": {
                    "show": True,
                    "position": position,
                    "distance": 22,
                    "align": "center",
                    "verticalAlign": "bottom" if position == "top" else "top",
                    "color": color,
                    "fontSize": 15,
                    "fontWeight": "bold",
                    "formatter": f"{name} {float(value):.2f}",
                },
                "labelLayout": {"hideOverlap": False},
                "tooltip": {"show": False},
                "silent": True,
                "z": 10,
            }

        annotations = [
            annotation_series("高點", high_val, high_ts, "top", "#ef232a"),
            annotation_series("低點", low_val, low_ts, "bottom", "#14b143"),
        ]

        today_option = {
            "backgroundColor": "#111111",
            "tooltip": {
                "trigger": "axis",
                "axisPointer": {"type": "cross"},
                "backgroundColor": "rgba(50, 50, 50, 0.9)",
                "textStyle": {"color": "#fff"},
            },
            "grid": {"left": "12%", "right": "12%", "top": "20%", "bottom": "20%"},
            "xAxis": {
                "type": "time",
                "min": f"{session_day} 09:00:00",
                "max": f"{session_day} 13:30:00",
                "axisLine": {"lineStyle": {"color": "#888"}},
                "axisLabel": {"color": "#ccc"},
                "splitLine": {"show": False},
            },
            "yAxis": {
                "type": "value",
                "scale": True,
                "splitLine": {"lineStyle": {"color": "#333"}},
                "axisLabel": {"color": "#ccc", "margin": 10},
            },
            "series": [
                {
                    "name": "上漲",
                    "type": "line",
                    "data": up,
                    "symbol": "none",
                    "connectNulls": False,
                    "lineStyle": {"color": "#ef232a", "width": 3},
                    "areaStyle": {
                        "color": "rgba(239, 35, 42, 0.2)",
                        "origin": base,
                    },
                },
                {
                    "name": "下跌",
                    "type": "line",
                    "data": down,
                    "symbol": "none",
                    "connectNulls": False,
                    "lineStyle": {"color": "#14b143", "width": 3},
                    "areaStyle": {
                        "color": "rgba(20, 177, 67, 0.2)",
                        "origin": base,
                    },
                },
                {
                    "name": "昨收價",
                    "type": "line",
                    "data": [
                        [f"{session_day} 09:00:00", base],
                        [f"{session_day} 13:30:00", base],
                    ],
                    "symbol": "none",
                    "lineStyle": {"color": "#888", "width": 1, "type": "dashed"},
                    "markLine": {
                        "symbol": "none",
                        "silent": True,
                        "animation": False,
                        "data": [
                            {
                                "yAxis": round(open_val, 2),
                                # 線寬設 0 時 ECharts 不建立元素，標籤會一起消失
                                "lineStyle": {"color": "rgba(0,0,0,0)", "width": 1},
                                "label": {
                                    "show": True,
                                    "position": "start",
                                    "distance": 6,
                                    "color": "#ffcc00",
                                    "fontSize": 13,
                                    "fontWeight": "bold",
                                    "formatter": f"開 {open_val:.2f}",
                                },
                            },
                            {
                                "yAxis": round(close_val, 2),
                                "lineStyle": {"color": "rgba(0,0,0,0)", "width": 1},
                                "label": {
                                    "show": True,
                                    "position": "end",
                                    "distance": 6,
                                    "color": "#00bfff",
                                    "fontSize": 13,
                                    "fontWeight": "bold",
                                    "formatter": f"收 {close_val:.2f}",
                                },
                            },
                        ],
                    },
                },
                *annotations,
            ],
        }

        chart_key = f"intraday_{stock_id}_{now_taipei().strftime('%H%M%S')}"
        st_echarts(options=today_option, height="350px", key=chart_key)
    elif live_quote:
        st.info("目前無今日即時交易資料（可能尚未開盤或抓取失敗）。")



if _analyze_live_every is not None:
    @st.fragment(run_every=_analyze_live_every)
    def _analyze_intraday_live():
        render_analyze_intraday_section(stock_id)

    _analyze_intraday_live()
else:
    render_analyze_intraday_section(stock_id)


# ==========================================
# 2. 歷史走勢圖 (日線 K線與 MACD) - 使用 ECharts
# ==========================================
@st.cache_data(ttl=3600) # 加入快取時間，且有時舊版快取會卡住，改一下設定強制更新
def load_data(code, start, end):
    """FinMind 歷史日線。"""
    return get_finmind_price_history(
        code,
        start.strftime("%Y-%m-%d"),
        end.strftime("%Y-%m-%d"),
    )

st.subheader(f"{stock_id} 歷史走勢圖")

# K 線週期與顯示區間並排，放在圖表上方
ctrl_left, ctrl_right = st.columns([2, 1])
with ctrl_left:
    interval_choice = st.radio(
        "選擇 K 線週期",
        options=["日線", "週線", "月線"],
        horizontal=True,
        label_visibility="collapsed"
    )

# 每個週期各自能顯示的區間，數值代表「要顯示幾根 K 棒」
RANGE_BARS = {
    "日線": {
        "當日": 1, "三日": 3, "一週": 5, "兩週": 10,
        "近半年": 120, "近一年": 240, "近三年": 720, "近五年": 1200
    },
    "週線": {"近半年": 26, "近一年": 52, "近三年": 156, "近五年": 260},
    "月線": {"近半年": 6, "近一年": 12, "近三年": 36, "近五年": 60},
}

range_options = list(RANGE_BARS[interval_choice].keys()) + ["上市至今"]
with ctrl_right:
    time_range = st.selectbox(
        "顯示區間",
        range_options,
        index=range_options.index("近半年"),
        label_visibility="collapsed"
    )

with st.spinner(f"正在載入 {stock_id} 的歷史資料..."):
    df = load_data(stock_id, start_date, end_date)

if not df.empty:
    if interval_choice == "週線":
        df = df.resample('W').agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}).dropna()
    elif interval_choice == "月線":
        df = df.resample('ME').agg({'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'}).dropna()
        
    df['MA5'] = df['Close'].rolling(window=5).mean()
    df['MA20'] = df['Close'].rolling(window=20).mean()
    df['MA60'] = df['Close'].rolling(window=60).mean()

    ema12 = df['Close'].ewm(span=12, adjust=False).mean()
    ema26 = df['Close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = ema12 - ema26
    df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
    df['Histogram'] = df['MACD'] - df['Signal']

    # 準備 ECharts 需要的資料格式
    dates = df.index.strftime('%Y-%m-%d').tolist()
    # K 線資料格式：[Open, Close, Lowest, Highest]，NaN 會讓 ECharts 渲染失敗，一律換成 '-'
    k_data = [
        [round(float(v), 2) if pd.notna(v) else '-' for v in row]
        for row in df[['Open', 'Close', 'Low', 'High']].values
    ]
    
    # 處理成交量資料，顏色與 K 線同步 (紅漲綠跌)
    volumes = []
    for i, row in enumerate(df.itertuples()):
        # row[1] 是 Open, row[4] 是 Close (根據上面 resample 後的欄位順序)
        open_price = row.Open
        close_price = row.Close
        # 判斷顏色：收盤 >= 開盤 為紅 (上漲/平盤)，否則為綠 (下跌)
        color = "#ef232a" if close_price >= open_price else "#14b143"
        volumes.append({
            "value": int(row.Volume) if pd.notna(row.Volume) else 0,
            "itemStyle": {"color": color}
        })
    
    # 將 NaN 轉為 '-' 讓 ECharts 忽略斷點
    def clean_nan(series):
        return [round(x, 2) if pd.notna(x) else '-' for x in series]
    
    ma5 = clean_nan(df['MA5'])
    ma20 = clean_nan(df['MA20'])
    ma60 = clean_nan(df['MA60'])
    macd = clean_nan(df['MACD'])
    signal = clean_nan(df['Signal'])
    
    # 處理 MACD 柱狀圖顏色，並轉為 ECharts 接受的格式
    hist_data = []
    for val in df['Histogram']:
        if pd.notna(val):
            color = "#ef232a" if val >= 0 else "#14b143"
            hist_data.append({"value": round(val, 3), "itemStyle": {"color": color}})
        else:
            hist_data.append("-")

    # 依照選擇的區間，換算出 dataZoom 的起點百分比，X 軸就會自動縮到該範圍
    zoom_end = 100
    zoom_start = 0  # 上市至今：顯示全部
    total_bars = len(df)
    bars_to_show = RANGE_BARS[interval_choice].get(time_range)

    if bars_to_show and total_bars > 0:
        bars_to_show = min(bars_to_show, total_bars)
        zoom_start = max(0, 100 - (bars_to_show / total_bars * 100))


    # ECharts 設定檔 (JSON)
    option = {
        "backgroundColor": "#111111",
        "tooltip": {
            "trigger": "axis",
            "axisPointer": {"type": "cross"},
            "backgroundColor": "rgba(50, 50, 50, 0.9)",
            "textStyle": {"color": "#fff", "fontSize": 14}
        },
        "legend": {
            "data": ["K線", "5日均線", "20日均線", "60日均線"],
            "top": 10, "left": 10,
            "textStyle": {"color": "#ccc", "fontSize": 14}
        },
        # grid 分成三塊：0是K線(55%)，1是成交量(15%)，2是MACD(15%)
        "grid": [
            {"left": "5%", "right": "5%", "top": "10%", "height": "50%"},
            {"left": "5%", "right": "5%", "top": "63%", "height": "15%"},
            {"left": "5%", "right": "5%", "top": "81%", "height": "15%"}
        ],
        "xAxis": [
            {
                "type": "category", "data": dates, "scale": True,
                "boundaryGap": False, "axisLine": {"onZero": False, "lineStyle": {"color": "#888"}},
                "splitLine": {"show": False}, "min": "dataMin", "max": "dataMax",
                "axisLabel": {"show": False} # 隱藏最上方K線的X軸日期
            },
            {
                "type": "category", "gridIndex": 1, "data": dates, "scale": True,
                "boundaryGap": False, "axisLine": {"onZero": False, "lineStyle": {"color": "#888"}},
                "axisTick": {"show": False}, "splitLine": {"show": False},
                "axisLabel": {"show": False}, "min": "dataMin", "max": "dataMax" # 隱藏成交量的X軸日期
            },
            {
                "type": "category", "gridIndex": 2, "data": dates, "scale": True,
                "boundaryGap": False, "axisLine": {"onZero": False, "lineStyle": {"color": "#888"}},
                "axisTick": {"show": False}, "splitLine": {"show": False},
                "min": "dataMin", "max": "dataMax" # MACD (最下方) 才顯示日期
            }
        ],
        "yAxis": [
            {
                "scale": True, "splitArea": {"show": False},
                "splitLine": {"lineStyle": {"color": "#333"}},
                "axisLabel": {"color": "#ccc"}
            },
            {
                "scale": True, "gridIndex": 1, "splitNumber": 2,
                "axisLabel": {"show": False}, "axisLine": {"show": False},
                "axisTick": {"show": False}, "splitLine": {"show": False}
            },
            {
                "scale": True, "gridIndex": 2, "splitNumber": 2,
                "axisLabel": {"show": False}, "axisLine": {"show": False},
                "axisTick": {"show": False}, "splitLine": {"show": False}
            }
        ],
        "dataZoom": [
            {
                "type": "inside", "xAxisIndex": [0, 1, 2], # 讓縮放同時連動三個圖表
                "start": zoom_start, "end": zoom_end,
                "filterMode": "filter"
            }
        ],
        "series": [
            {
                "name": "K線", "type": "candlestick", "data": k_data,
                "itemStyle": {
                    "color": "#ef232a", "color0": "#14b143",
                    "borderColor": "#ef232a", "borderColor0": "#14b143"
                }
            },
            {"name": "5日均線", "type": "line", "data": ma5, "smooth": True, "showSymbol": False, "lineStyle": {"color": "#ffff00", "width": 2}},
            {"name": "20日均線", "type": "line", "data": ma20, "smooth": True, "showSymbol": False, "lineStyle": {"color": "#b042ff", "width": 2}},
            {"name": "60日均線", "type": "line", "data": ma60, "smooth": True, "showSymbol": False, "lineStyle": {"color": "#00ffff", "width": 2}},
            
            # 成交量
            {"name": "成交量", "type": "bar", "xAxisIndex": 1, "yAxisIndex": 1, "data": volumes},

            # MACD 指標
            {"name": "MACD", "type": "bar", "xAxisIndex": 2, "yAxisIndex": 2, "data": hist_data},
            {"name": "快線", "type": "line", "xAxisIndex": 2, "yAxisIndex": 2, "data": macd, "showSymbol": False, "lineStyle": {"color": "orange", "width": 1.5}},
            {"name": "慢線", "type": "line", "xAxisIndex": 2, "yAxisIndex": 2, "data": signal, "showSymbol": False, "lineStyle": {"color": "blue", "width": 1.5}}
        ]
    }

    # 渲染 ECharts (為了容納三個圖，把高度從 800px 稍微拉高到 850px)
    st_echarts(options=option, height="850px")

else:
    st.warning("查無資料，請確認股票代號是否正確。")

# ==========================================
# 3. 交易量（與歷史走勢、籌碼面同級）
# ==========================================
st.subheader("交易量")
with st.spinner("正在彙整交易量分析..."):
    volume_df = load_data(stock_id, start_date, end_date)
    render_volume_analysis(volume_df)

# ==========================================
# 4. 籌碼面
# ==========================================
st.subheader("籌碼面")

tab1, tab2, tab3, tab4 = st.tabs(["法人資訊", "籌碼資訊", "信用交易", "主力資訊"])

def _chips_section_title(title):
    st.markdown(
        f"<div class='twmc-chips-h'>{html.escape(title)}</div>",
        unsafe_allow_html=True,
    )

with tab1:
    with st.spinner("正在查詢法人資料..."):
        inst_df = get_institutional_data_2m(stock_id)
        _chips_section_title("法人動向")
        if not inst_df.empty:
            render_institutional_flow(inst_df, code=stock_id)
        else:
            st.info("目前無法判斷法人動向。")

        _chips_section_title("近期三大法人買賣超 (近兩個月)")
        if not inst_df.empty:
            # 依據買進(>0)與賣出(<0)設定長條圖顏色
            inst_dates = inst_df['date'].tolist()
            inst_total = inst_df['三大法人合計'].tolist()
            
            inst_series_data = []
            for val in inst_total:
                color = "#ef232a" if val > 0 else "#14b143"
                inst_series_data.append({
                    "value": val,
                    "itemStyle": {"color": color}
                })
            
            inst_option = {
                "backgroundColor": "#111111",
                "tooltip": {
                    "trigger": "axis",
                    "axisPointer": {"type": "shadow"},
                    "backgroundColor": "rgba(50, 50, 50, 0.9)",
                    "textStyle": {"color": "#fff"}
                },
                "grid": {"left": "5%", "right": "5%", "top": "15%", "bottom": "15%"},
                "xAxis": {
                    "type": "category",
                    "data": inst_dates,
                    "axisLine": {"lineStyle": {"color": "#888"}},
                    "axisLabel": {"color": "#ccc"}
                },
                "yAxis": {
                    "type": "value",
                    "name": "三大法人合計買賣超 (股)",
                    "splitLine": {"lineStyle": {"color": "#333"}},
                    "axisLabel": {"color": "#ccc"}
                },
                "dataZoom": [{"type": "inside"}, {"type": "slider"}],
                "series": [
                    {
                        "name": "三大法人合計",
                        "type": "bar",
                        "data": inst_series_data
                    }
                ]
            }
            st_echarts(options=inst_option, height="400px")
            
            _chips_section_title("詳細數據")
            buy_sell_cols = ['外資買賣超', '投信買賣超', '自營商買賣超', '三大法人合計']
            display_df = inst_df.copy()
            if "date" in display_df.columns:
                display_df.rename(columns={"date": "日期"}, inplace=True)
            with st.container(key="inst_table"):
                st.dataframe(
                    _style_buy_sell_df(display_df, buy_sell_cols),
                    use_container_width=True,
                    hide_index=True,
                    height=420
                )
            st.caption("※ 單位：股；買超紅字、賣超綠字；資料來源：FinMind")
        else:
            st.info(f"查無 {stock_id} 近期的三大法人資料。")
    
with tab2:
    _chips_section_title("千張大戶持股比例")
    large = get_thousand_lot_holder_ratio(stock_id)
    if large.get("ratio") is not None:
        c1, c2, c3 = st.columns(3)
        c1.metric("千張大戶持股", f"{large['ratio']:.2f}%")
        c2.metric("人數", f"{int(large.get('people') or 0):,}")
        c3.metric("資料日", large.get("date") or "—")
        st.caption("※ 加總持股級距下限 ≥ 1,000,000 股（千張）；資料來源：FinMind TaiwanStockHoldingSharesPer")
    else:
        hint = large.get("msg") or ""
        if "level is free" in hint.lower() or "sponsor" in hint.lower():
            st.info("千張大戶資料需 FinMind 進階權限。請設定 FINMIND_TOKEN 後再試。")
        else:
            st.info("目前無法取得千張大戶持股比例。")

    _chips_section_title("當下籌碼分布持股長條圖")
    with st.spinner("正在計算籌碼分布與集中度..."):
        total_shares = get_total_shares(stock_id)
        total_lots = total_shares / 1000 if total_shares else 0

        # 1. 籌碼分布（以佔股本 % 顯示）
        dist_data = get_chip_distribution_data(stock_id, total_shares)

        def to_pct(lots):
            return round((lots / total_lots) * 100, 2) if total_lots else 0.0

        dist_pct = {k: to_pct(v) for k, v in dist_data.items()}
        colors = {
            "外資": "#00bfff", "投信": "#ff66cc",
            "自營商": "#ffcc00", "融資": "#ef232a", "融券": "#14b143"
        }
        # 相容舊鍵名
        if "董監" in dist_pct and "董監" not in colors:
            colors["董監"] = "#ff9900"

        dist_option = {
            "backgroundColor": "#111111",
            "tooltip": {
                "trigger": "axis",
                "axisPointer": {"type": "shadow"},
                "backgroundColor": "rgba(50, 50, 50, 0.9)",
                "textStyle": {"color": "#fff"}
            },
            "grid": {"left": "15%", "right": "8%", "top": "5%", "bottom": "15%"},
            "xAxis": {
                "type": "value",
                "name": "佔股本 (%)",
                "min": 0,
                "splitLine": {"lineStyle": {"color": "#333"}},
                "axisLabel": {"color": "#ccc", "formatter": "{value}%"}
            },
            "yAxis": {
                "type": "category",
                "data": list(dist_pct.keys()),
                "axisLine": {"lineStyle": {"color": "#888"}},
                "axisLabel": {"color": "#ccc"}
            },
            "series": [
                {
                    "name": "持股占比",
                    "type": "bar",
                    "data": [
                        {
                            "value": dist_pct[k],
                            "itemStyle": {"color": colors.get(k, "#00bfff")},
                        }
                        for k in dist_pct
                    ],
                    "label": {
                        "show": True,
                        "position": "right",
                        "color": "#fff",
                        "formatter": "{c}%"
                    }
                }
            ]
        }
        st_echarts(options=dist_option, height="350px")
        st.caption("※ 投信與自營商持股以近一年累計買賣超估算。橫軸為佔股本比重。")

        render_concentration(stock_id, df, total_shares)


with tab3:
    _chips_section_title("近期信用交易 (融資、融券)")
    with st.spinner("正在查詢信用交易資料..."):
        margin_df = get_margin_data_2m(stock_id)
        if not margin_df.empty:
            dates = margin_df['date'].tolist()
            
            # 使用 ECharts 畫長條圖 (融資餘額) 與 占比折線圖 (融資占比)
            margin_option = {
                "backgroundColor": "#111111",
                "tooltip": {
                    "trigger": "axis",
                    "axisPointer": {"type": "cross"},
                    "backgroundColor": "rgba(50, 50, 50, 0.9)",
                    "textStyle": {"color": "#fff"}
                },
                "legend": {
                    "data": ["融資餘額 (張)", "融資使用率 (%)", "融券餘額 (張)", "融券使用率 (%)"],
                    "textStyle": {"color": "#ccc"}
                },
                "grid": {"left": "5%", "right": "5%", "top": "15%", "bottom": "15%"},
                "xAxis": {
                    "type": "category",
                    "data": dates,
                    "axisLine": {"lineStyle": {"color": "#888"}},
                    "axisLabel": {"color": "#ccc"}
                },
                "yAxis": [
                    {
                        "type": "value",
                        "name": "餘額 (張)",
                        "splitLine": {"lineStyle": {"color": "#333"}},
                        "axisLabel": {"color": "#ccc"}
                    },
                    {
                        "type": "value",
                        "name": "使用率 (%)",
                        "position": "right",
                        "splitLine": {"show": False},
                        "axisLabel": {"color": "#ccc", "formatter": "{value} %"}
                    }
                ],
                "dataZoom": [{"type": "inside"}, {"type": "slider"}],
                "series": [
                    {
                        "name": "融資餘額 (張)",
                        "type": "bar",
                        "data": margin_df['融資餘額'].tolist(),
                        "itemStyle": {"color": "#ef232a"}
                    },
                    {
                        "name": "融資使用率 (%)",
                        "type": "line",
                        "yAxisIndex": 1,
                        "data": margin_df['融資占比(%)'].tolist(),
                        "lineStyle": {"color": "#ff9900", "width": 2},
                        "symbol": "none"
                    },
                    {
                        "name": "融券餘額 (張)",
                        "type": "bar",
                        "data": margin_df['融券餘額'].tolist(),
                        "itemStyle": {"color": "#14b143"}
                    },
                    {
                        "name": "融券使用率 (%)",
                        "type": "line",
                        "yAxisIndex": 1,
                        "data": margin_df['融券占比(%)'].tolist(),
                        "lineStyle": {"color": "#00ffff", "width": 2},
                        "symbol": "none"
                    }
                ]
            }
            
            st_echarts(options=margin_option, height="400px")
            st.caption("※ 單位：張；資料來源：FinMind")
        else:
            st.info(f"查無 {stock_id} 近期的信用交易資料。")

with tab4:
    render_mainforce_panel(stock_id)


# ==========================================
# 5. 基本面
# ==========================================
st.subheader("基本面")
render_analyze_fundamental(stock_id)




with st.container(key="twmc_analyze_export_end"):
    st.markdown(
        "<div class='twmc-export-end' style='height:1px;margin:0;padding:0;' aria-hidden='true'></div>",
        unsafe_allow_html=True,
    )