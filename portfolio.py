"""本機模擬／投資帳本：流水帳為唯一來源，持倉與損益每次重算。"""
from __future__ import annotations

import json
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path

PORTFOLIO_FILE = Path(__file__).parent / "portfolio.json"
DEFAULT_STARTING_CASH = 1_000_000.0
MODES = ("analyze", "simulated", "investment")


def _empty_store():
    return {
        "version": 1,
        "active_mode": "analyze",
        "simulated": {
            "starting_cash": DEFAULT_STARTING_CASH,
            "initialized": False,
            "transactions": [],
        },
        "investment": {
            "transactions": [],
        },
    }


def _clean_tx(raw):
    if not isinstance(raw, dict):
        return None
    side = raw.get("side")
    if side not in ("buy", "sell"):
        return None
    try:
        qty = float(raw.get("qty") or 0)
        price = float(raw.get("price") or 0)
        fee = float(raw.get("fee") or 0)
        tax = float(raw.get("tax") or 0)
    except (TypeError, ValueError):
        return None
    if qty <= 0 or price <= 0:
        return None
    if fee < 0:
        fee = 0.0
    if tax < 0:
        tax = 0.0
    code = str(raw.get("code") or "").strip()
    if not code:
        return None
    return {
        "id": str(raw.get("id") or uuid.uuid4()),
        "ts": str(raw.get("ts") or datetime.now().isoformat(timespec="seconds")),
        "code": code,
        "side": side,
        "qty": qty,
        "price": price,
        "fee": fee,
        "tax": tax,
        "note": str(raw.get("note") or ""),
    }


def normalize_store(raw):
    store = _empty_store()
    if not isinstance(raw, dict):
        return store, True

    migrated = False
    mode = raw.get("active_mode")
    if mode in MODES:
        store["active_mode"] = mode
    else:
        migrated = True

    sim = raw.get("simulated") if isinstance(raw.get("simulated"), dict) else {}
    try:
        cash = float(sim.get("starting_cash", DEFAULT_STARTING_CASH))
    except (TypeError, ValueError):
        cash = DEFAULT_STARTING_CASH
        migrated = True
    store["simulated"]["starting_cash"] = cash
    store["simulated"]["initialized"] = bool(sim.get("initialized", False))
    store["simulated"]["transactions"] = [
        tx for tx in (_clean_tx(item) for item in sim.get("transactions") or []) if tx
    ]

    inv = raw.get("investment") if isinstance(raw.get("investment"), dict) else {}
    store["investment"]["transactions"] = [
        tx for tx in (_clean_tx(item) for item in inv.get("transactions") or []) if tx
    ]
    return store, migrated


def save_store(store):
    payload = {
        "version": 1,
        "active_mode": store.get("active_mode", "analyze"),
        "simulated": store["simulated"],
        "investment": store["investment"],
    }
    tmp = PORTFOLIO_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    tmp.replace(PORTFOLIO_FILE)


def load_store():
    raw = None
    if PORTFOLIO_FILE.exists():
        try:
            with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            raw = None
    store, migrated = normalize_store(raw)
    if migrated or raw is None:
        save_store(store)
    return store


def make_transaction(code, side, qty, price, note="", ts=None, fee=0.0, tax=0.0):
    try:
        fee = float(fee or 0)
    except (TypeError, ValueError):
        fee = 0.0
    if fee < 0:
        fee = 0.0
    try:
        tax = float(tax or 0)
    except (TypeError, ValueError):
        tax = 0.0
    if tax < 0:
        tax = 0.0
    return {
        "id": str(uuid.uuid4()),
        "ts": ts or datetime.now().isoformat(timespec="seconds"),
        "code": str(code).strip(),
        "side": side,
        "qty": float(qty),
        "price": float(price),
        "fee": fee,
        "tax": tax,
        "note": str(note or ""),
    }


def _ordered(transactions):
    indexed = list(enumerate(transactions or []))
    indexed.sort(key=lambda pair: (pair[1].get("ts") or "", pair[0]))
    return [tx for _, tx in indexed]


def replay(transactions):
    """依時間重算持倉與已實現損益。
    買進：手續費／交易稅併入成本；賣出：兩者從已實現損益扣除。
    """
    holdings = {}
    realized = 0.0
    fees_paid = 0.0
    taxes_paid = 0.0
    for tx in _ordered(transactions):
        code = tx["code"]
        qty = float(tx["qty"])
        price = float(tx["price"])
        fee = float(tx.get("fee") or 0)
        tax = float(tx.get("tax") or 0)
        fees_paid += fee
        taxes_paid += tax
        charges = fee + tax
        h = holdings.setdefault(code, {"qty": 0.0, "cost": 0.0})
        if tx["side"] == "buy":
            h["qty"] += qty
            h["cost"] += qty * price + charges
        else:
            if h["qty"] <= 0:
                continue
            sell_qty = min(qty, h["qty"])
            avg = h["cost"] / h["qty"]
            realized += sell_qty * (price - avg) - charges
            h["qty"] -= sell_qty
            h["cost"] -= sell_qty * avg
            if h["qty"] <= 1e-9:
                h["qty"] = 0.0
                h["cost"] = 0.0
    return holdings, realized, fees_paid, taxes_paid


def exit_records(transactions):
    """依流水帳重建每次賣出（脫手）明細與該筆已實現損益。最新在前。"""
    holdings = {}
    records = []
    for tx in _ordered(transactions):
        if tx.get("side") != "sell":
            code = tx["code"]
            qty = float(tx["qty"])
            price = float(tx["price"])
            charges = float(tx.get("fee") or 0) + float(tx.get("tax") or 0)
            h = holdings.setdefault(code, {"qty": 0.0, "cost": 0.0})
            h["qty"] += qty
            h["cost"] += qty * price + charges
            continue

        code = tx["code"]
        qty = float(tx["qty"])
        price = float(tx["price"])
        fee = float(tx.get("fee") or 0)
        tax = float(tx.get("tax") or 0)
        charges = fee + tax
        h = holdings.setdefault(code, {"qty": 0.0, "cost": 0.0})
        if h["qty"] <= 0:
            continue
        sell_qty = min(qty, h["qty"])
        avg = h["cost"] / h["qty"]
        pnl = sell_qty * (price - avg) - charges
        cost_basis = sell_qty * avg
        records.append({
            "id": tx.get("id"),
            "ts": tx.get("ts") or "",
            "code": code,
            "qty": sell_qty,
            "sell_price": price,
            "avg_cost": avg,
            "cost": cost_basis,
            "proceeds": sell_qty * price,
            "fee": fee,
            "tax": tax,
            "realized": pnl,
            "note": tx.get("note") or "",
        })
        h["qty"] -= sell_qty
        h["cost"] -= sell_qty * avg
        if h["qty"] <= 1e-9:
            h["qty"] = 0.0
            h["cost"] = 0.0
    records.reverse()
    return records


def cash_from_transactions(starting_cash, transactions):
    cash = float(starting_cash)
    for tx in _ordered(transactions):
        amount = float(tx["qty"]) * float(tx["price"])
        charges = float(tx.get("fee") or 0) + float(tx.get("tax") or 0)
        if tx["side"] == "buy":
            cash -= amount + charges
        else:
            cash += amount - charges
    return cash


def holding_qty(holdings, code):
    return float((holdings.get(code) or {}).get("qty") or 0)


# 預估賣出成本（對齊常見券商／投資先生口徑）
SELL_FEE_RATE = 0.001425
SELL_FEE_MIN = 20.0
ETF_TAX_RATE = 0.001   # 一般 ETF 證交稅 0.1%
STOCK_TAX_RATE = 0.003  # 一般股票證交稅 0.3%


def is_etf_code(code):
    """台股 ETF 代號多以 00 開頭（含主動式如 00995A）。"""
    return str(code or "").strip().upper().startswith("00")


def estimate_exit_charges(code, market_value):
    """預估若現在賣出的手續費＋交易稅（元以下無條件捨去）。"""
    try:
        mkt = float(market_value or 0)
    except (TypeError, ValueError):
        return 0.0, 0.0, 0.0
    if mkt <= 0:
        return 0.0, 0.0, 0.0
    fee = max(SELL_FEE_MIN, float(int(mkt * SELL_FEE_RATE)))
    tax_rate = ETF_TAX_RATE if is_etf_code(code) else STOCK_TAX_RATE
    tax = float(int(mkt * tax_rate))
    return fee, tax, fee + tax


def compute_snapshot(transactions, quotes, starting_cash=None, net_of_exit_costs=False):
    """持倉快照。

    net_of_exit_costs=True 時，未實現損益改為扣除預估賣出手續費／交易稅
   （與投資先生等 App 的「預估處分損益」口徑接近）。
    """
    holdings, realized, fees_paid, taxes_paid = replay(transactions)
    rows = []
    market_value = 0.0
    unrealized = 0.0
    for code, h in holdings.items():
        qty = h["qty"]
        if qty <= 1e-9:
            continue
        avg = h["cost"] / qty if qty else 0.0
        mark = quotes.get(code)
        mkt = (mark * qty) if mark is not None else None
        u_gross = (mkt - h["cost"]) if mkt is not None else None
        exit_fee = exit_tax = exit_charges = 0.0
        u = u_gross
        if mkt is not None and net_of_exit_costs:
            exit_fee, exit_tax, exit_charges = estimate_exit_charges(code, mkt)
            u = u_gross - exit_charges
        if mkt is not None:
            market_value += mkt
            unrealized += u
        rows.append({
            "code": code,
            "qty": qty,
            "avg_cost": avg,
            "cost": h["cost"],
            "mark": mark,
            "market_value": mkt,
            "unrealized_gross": u_gross,
            "exit_fee": exit_fee,
            "exit_tax": exit_tax,
            "unrealized": u,
        })
    rows.sort(key=lambda row: row["code"])
    cash = cash_from_transactions(starting_cash, transactions) if starting_cash is not None else None
    equity = (cash + market_value) if cash is not None else market_value
    return {
        "holdings": rows,
        "realized": realized,
        "unrealized": unrealized,
        "market_value": market_value,
        "cash": cash,
        "equity": equity,
        "fees_paid": fees_paid,
        "taxes_paid": taxes_paid,
        "map": holdings,
    }


def validate_trade(transactions, code, side, qty, price, starting_cash=None, fee=0.0, tax=0.0):
    try:
        qty = float(qty)
        price = float(price)
        fee = float(fee or 0)
        tax = float(tax or 0)
    except (TypeError, ValueError):
        return "股數、價格、手續費與交易稅必須是數字"
    if qty <= 0:
        return "股數必須大於 0"
    if price <= 0:
        return "價格必須大於 0"
    if fee < 0:
        return "手續費不可為負數"
    if tax < 0:
        return "交易稅不可為負數"
    code = str(code or "").strip()
    if not code:
        return "請輸入股票代號"
    holdings, _, _, _ = replay(transactions)
    if side == "sell":
        available = holding_qty(holdings, code)
        if qty > available + 1e-9:
            return f"可賣股數不足（目前持有 {available:g} 股）"
    if starting_cash is not None and side == "buy":
        cash = cash_from_transactions(starting_cash, transactions)
        if qty * price + fee + tax > cash + 1e-6:
            return f"虛擬現金不足（可用 {cash:,.0f}）"
    return None


def append_trade(book, code, side, qty, price, note="", ts=None, starting_cash=None, fee=0.0, tax=0.0):
    book = deepcopy(book)
    txs = list(book.get("transactions") or [])
    err = validate_trade(txs, code, side, qty, price, starting_cash=starting_cash, fee=fee, tax=tax)
    if err:
        return None, err
    txs.append(make_transaction(code, side, qty, price, note=note, ts=ts, fee=fee, tax=tax))
    book["transactions"] = txs
    return book, None


def delete_trade(book, tx_id):
    book = deepcopy(book)
    book["transactions"] = [tx for tx in book.get("transactions") or [] if tx.get("id") != tx_id]
    return book
