# -*- coding: utf-8 -*-
"""
KSD open API (data.go.kr 한국예탁결제원_파생결합증권정보서비스_GW) client.

Operations used (all take serviceKey, pageNo, numOfRows, isin):
  getErlyRedELSInfo        - early-redemption date of an ELS (redemption cross-check)
  getDerivCombiIsinInfoN1  - basic info (issue date, maturity, name)  * public issues only
  getRedCondiInfoN1        - redemption conditions (evaluation start/end dates, pay dates, barrier)
  getAssetXrcInfoN1        - underlying exercise info (base ratio, base price) -> official 최초기준가
  getAssetInfoN1           - underlying asset names/codes

Daily traffic is limited (100 calls per operation on a development account), so a
per-day usage ledger is kept in data/ksd_usage.json and the caller prioritises.
Field names are not documented here, so the first raw record of each operation is
saved to data/ksd_debug.json and extraction uses tolerant key matching.
"""
import os, re, json, time
from datetime import date, datetime

import requests

KEY = os.environ.get("KSD_API_KEY", "").strip()
ENDPOINT = os.environ.get("KSD_ENDPOINT", "").strip().rstrip("/")
CANDIDATE_BASES = [
    "https://apis.data.go.kr/B552481/DerivesSvc",   # KSD 파생결합증권정보서비스_GW (confirmed End Point)
    "http://apis.data.go.kr/B552481/DerivesSvc",
]
DAILY_LIMIT = int(os.environ.get("KSD_DAILY_LIMIT", "95"))

_state = {"base": None, "usage": None, "usage_path": None, "debug": {}, "log": None}

def _log(msg):
    if _state["log"]:
        _state["log"]("[ksd] " + msg)

def setup(data_dir, logf=None):
    _state["log"] = logf
    _state["usage_path"] = os.path.join(data_dir, "ksd_usage.json")
    _state["debug_path"] = os.path.join(data_dir, "ksd_debug.json")
    try:
        with open(_state["usage_path"], encoding="utf-8") as f:
            _state["usage"] = json.load(f)
    except Exception:
        _state["usage"] = {}
    today = date.today().isoformat()
    if _state["usage"].get("date") != today:
        _state["usage"] = {"date": today, "ops": {}}
    if ENDPOINT:
        _state["base"] = ENDPOINT
    return bool(KEY)

def save():
    try:
        with open(_state["usage_path"], "w", encoding="utf-8") as f:
            json.dump(_state["usage"], f, ensure_ascii=False, indent=1)
        with open(_state["debug_path"], "w", encoding="utf-8") as f:
            json.dump(_state["debug"], f, ensure_ascii=False, indent=1)
    except Exception:
        pass

def remaining(op):
    used = _state["usage"]["ops"].get(op, 0)
    return max(0, DAILY_LIMIT - used)

def _count(op):
    _state["usage"]["ops"][op] = _state["usage"]["ops"].get(op, 0) + 1

def _items_from(payload):
    """Extract item list from the usual data.go.kr envelope (json)."""
    try:
        body = payload["response"]["body"]
        items = body.get("items")
        if items is None:
            return [], body
        if isinstance(items, dict):
            items = items.get("item", [])
        if isinstance(items, dict):
            items = [items]
        return items or [], body
    except Exception:
        return [], {}

def _xml_items(text):
    import xml.etree.ElementTree as ET
    out = []
    try:
        root = ET.fromstring(text)
        for it in root.iter("item"):
            out.append({c.tag: (c.text or "").strip() for c in it})
        hdr = {c.tag: (c.text or "") for c in root.iter("header") for c in c}
        return out, hdr
    except Exception:
        return [], {}

def call(op, isin=None, rows=100, page=1, extra=None):
    """Returns (items, ok). ok=False means quota/transport/auth problem (not 'no data')."""
    if not KEY:
        return [], False
    if remaining(op) <= 0:
        return [], False
    bases = [_state["base"]] if _state["base"] else CANDIDATE_BASES
    params = {"serviceKey": KEY, "pageNo": page, "numOfRows": rows, "resultType": "json"}
    if isin:
        params["isin"] = isin
    if extra:
        params.update(extra)
    for base in bases:
        url = f"{base}/{op}"
        try:
            r = requests.get(url, params=params, timeout=30)
            _count(op)
            txt = r.text
            items, hdr = [], {}
            if txt.lstrip().startswith("{"):
                try:
                    j = r.json()
                    items, body = _items_from(j)
                    hdr = j.get("response", {}).get("header", {})
                except Exception:
                    pass
            else:
                items, hdr = _xml_items(txt)
            code = str(hdr.get("resultCode", "")) if isinstance(hdr, dict) else ""
            if r.status_code == 200 and (items or code in ("00", "0", "03")):
                if not _state["base"]:
                    _state["base"] = base
                    _log(f"endpoint resolved: {base}")
                if items and op not in _state["debug"]:
                    _state["debug"][op] = {"sample_isin": isin, "items": items[:30], "count": len(items)}
                return items, True
            if r.status_code == 200 and code and code not in ("00", "0"):
                msg = hdr.get("resultMsg", "") if isinstance(hdr, dict) else ""
                _log(f"{op} {isin or ''} resultCode={code} {msg}")
                if code in ("22", "30", "31", "32", "99"):  # quota / key problems
                    return [], False
                return [], True
            _log(f"{op} @{base.rsplit('/',1)[-1]} http={r.status_code} code={code or '-'} body={txt[:140]!r}")
            if _state["base"]:
                return [], False
        except Exception as e:
            _log(f"{op} error at {base}: {e}")
            if _state["base"]:
                return [], False
    if not _state["base"]:
        _log("no endpoint base worked; set KSD_ENDPOINT secret to the End Point URL shown on data.go.kr")
    return [], False

# ---------------------------------------------------------------------------
# tolerant field extraction
# ---------------------------------------------------------------------------
def _find(item, patterns):
    for k, v in item.items():
        kl = k.lower()
        for pat in patterns:
            if re.search(pat, kl):
                return v
    return None

def _dt(v):
    if v is None:
        return None
    s = re.sub(r"\D", "", str(v))
    if len(s) >= 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return None

def _num(v):
    try:
        return float(str(v).replace(",", "").replace("%", ""))
    except Exception:
        return None

def basic_info(isin):
    """getDerivCombiIsinInfoN1 — public issues only (NODATA for private placements)."""
    items, ok = call("getDerivCombiIsinInfoN1", isin, rows=10)
    if not items:
        return None, ok
    it = items[0]
    return {
        "name": it.get("korSecnNm") or _find(it, [r"nm$"]),
        "issue_date": _dt(it.get("issuDt") or _find(it, [r"issu.*dt"])),
        "contract_date": _dt(it.get("contDt")),
        "maturity": _dt(it.get("xpirDt") or _find(it, [r"xpir.*dt"])),
        "redeemed_date": _dt(it.get("redDt")),
        "monthly": (it.get("mmPayYn") == "Y"),
        "asset_count": _num(it.get("bassetCnt")),
        "principal_type": it.get("prcpPrsvTpcd"),
        "raw": it,
    }, ok

PCT_UP = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%\s*(?:이상|초과)")
PCT_LZ = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%\s*미만으로\s*하락한\s*적이\s*없")
PCT_KI = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%\s*미만으로\s*하락한\s*적이\s*있")
PCT_PAY = re.compile(r"[x×X]\s*(\d{1,3}(?:\.\d+)?)\s*%")

def red_conditions(isin):
    """getRedCondiInfoN1 → list of evaluations {n, eval_start, eval_end, pay_date, barrier, lizard, ki, payout, is_maturity, text}."""
    items, ok = call("getRedCondiInfoN1", isin, rows=100)
    if not items:
        return None, ok
    by_seq = {}
    ki_level = None
    for it in items:
        txt = (it.get("redCondiContent") or "")
        formula = (it.get("redFormulaContent") or "")
        seq = _num(it.get("valatNtimesSeq"))
        end = _dt(it.get("midValatExpryDt")); beg = _dt(it.get("midValatBeginDt")); pay = _dt(it.get("midValatPayDt"))
        m_ki = PCT_KI.search(txt)
        if m_ki:
            ki_level = float(m_ki.group(1))
        if not end and not beg:
            continue
        key = seq if seq is not None else end
        row = by_seq.setdefault(key, {"n": seq, "eval_start": beg, "eval_end": end or beg, "pay_date": pay,
                                      "barrier": None, "lizard": None, "payout": None, "is_maturity": ("만기" in txt),
                                      "tpcd": it.get("redCondiTpcd"), "text": []})
        row["text"].append(txt)
        m_up = PCT_UP.search(txt)
        m_lz = PCT_LZ.search(txt)
        m_pay = PCT_PAY.search(formula)
        payout = (float(m_pay.group(1)) - 100.0) if m_pay else None
        if m_lz:
            row["lizard"] = float(m_lz.group(1))
            if payout is not None and row["payout"] is None:
                row["payout"] = payout
        elif m_up:
            row["barrier"] = float(m_up.group(1))
            if payout is not None:
                row["payout"] = payout
        if "만기" in txt:
            row["is_maturity"] = True
    rows = sorted(by_seq.values(), key=lambda r: (r["eval_end"] or r["eval_start"]))
    for i, r in enumerate(rows):
        r["n"] = i + 1
        r["text"] = " / ".join(r["text"])[:400]
    return {"rows": rows, "ki": ki_level}, ok

def asset_exercise(isin):
    """getAssetXrcInfoN1 → {assetSeq: base_price} using xrcStdSeq==1 (최초기준가) rows."""
    items, ok = call("getAssetXrcInfoN1", isin, rows=100)
    if not items:
        return None, ok
    base = {}
    allrows = []
    for it in items:
        aseq = str(it.get("assetSeq") or "")
        xseq = str(it.get("xrcStdSeq") or "")
        prc = _num(it.get("xrcStdprc"))
        ratio = _num(it.get("xrcStdRatio"))
        allrows.append({"assetSeq": aseq, "xrcStdSeq": xseq, "ratio": ratio, "price": prc})
        if prc and (xseq == "1" or aseq not in base):
            if xseq == "1" or aseq not in base:
                base[aseq] = prc
    return {"base": base, "rows": allrows}, ok

def asset_info(isin):
    """getAssetInfoN1 → {assetSeq: {name, code}} (field names discovered at runtime)."""
    items, ok = call("getAssetInfoN1", isin, rows=50)
    if not items:
        return None, ok
    out = {}
    for i, it in enumerate(items):
        seq = str(it.get("assetSeq") or _find(it, [r"seq"]) or (i + 1))
        name = it.get("bassetNm") or it.get("assetNm") or _find(it, [r"asst.*nm", r"asset.*nm", r"nm$", r"name"])
        code = it.get("bassetIsin") or _find(it, [r"isin", r"cd$", r"code"])
        out[seq] = {"name": name, "code": code, "raw": it}
    return out, ok

def early_redeemed(isin):
    """getErlyRedELSInfo → (date or None, ok). None with ok=True means no early-redemption record."""
    items, ok = call("getErlyRedELSInfo", isin, rows=10)
    if not items:
        return None, ok
    it = items[0]
    d = _dt(it.get("redDt") or _find(it, [r"red.*dt", r"dt$"]))
    return (d or "unknown"), ok
