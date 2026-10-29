# -*- coding: utf-8 -*-
"""
ELT (Premier ELT, 파생결합신탁) judgment
- Reads data/elt_master.json (product terms converted from the in-house issue sheet)
- Reuses the ELS judge() of collect.py with the in-house base prices as official reference
- Writes data/elt.json (site data, all ELT in one file)
Tax is intentionally not computed for ELT.
"""
import os, re, json
from datetime import date

MASTER = "elt_master.json"
OUT = "elt.json"

NOTE_TEXT = {
    "eval3_date_corrected": "3차 평가일: 원자료 2027-12-05(일요일) → 결제일(12-17) 기준 2027-12-15로 표기, 발행 조건 확인 중",
    "settle1_check": "1차 결제일: 원자료 확인 중(평가일 이후 결제일 미확정)",
    "terms_missing": "기초자산·최초기준가격 정보가 아직 등록되지 않았습니다(일정만 등록)",
}


def _clean(msg, has_coupon):
    msg = msg or ""
    if not has_coupon:
        msg = re.sub(r"\s*\(세전 약 [^)]*%\)", "", msg)
    msg = msg.replace("만기 수익 지급 가능", "만기 상환 조건 충족 가능")
    return msg


def build_product(m):
    no = m["no"]
    evals = m.get("evals") or []
    p = {
        "isin": f"ELT{no}", "code": str(no), "type": "ELT", "name": f"Premier ELT {no}호",
        "coupon": m.get("coupon"), "ladder": [e["barrier"] for e in evals] if evals and evals[0].get("barrier") is not None else None,
        "issue_date": m.get("trade_date"), "ref_date": m.get("trade_date"),
        "mat_eval": evals[-1]["date"] if evals else None, "mat_pay": m.get("maturity_date") or (evals[-1].get("settle") if evals else None),
        "evals": [(e["n"], e["date"]) for e in evals],
        "assets": list(m.get("assets") or []),
        "ksd_ref": {a: b for a, b in zip(m.get("assets") or [], m.get("base") or [])},
        "ki": {a: m["ki"] for a in (m.get("assets") or [])} if m.get("ki") is not None else {},
        "no_ki": m.get("ki") is None and bool(m.get("assets")),
        "lizard": False, "monthly": False, "principal_guard": False,
    }
    if evals:
        y = (date.fromisoformat(evals[-1]["date"]) - date.fromisoformat(m["trade_date"])).days / 365.25 if m.get("trade_date") else None
        p["mat_years"] = round(y * 2) / 2 if y else None
        if len(evals) > 1:
            mo = (date.fromisoformat(evals[1]["date"]) - date.fromisoformat(evals[0]["date"])).days / 30.4375
            p["period_months"] = int(round(mo))
    return p


def run(judge, build_schedule, data_dir, log, today, generated=None):
    path = os.path.join(data_dir, MASTER)
    if not os.path.exists(path):
        log("elt: no master file — skipped")
        return {"enabled": False}
    with open(path, encoding="utf-8") as f:
        master = json.load(f)
    items = []
    counts = {}
    for m in master.get("products", []):
        p = build_product(m)
        try:
            if not p["assets"]:
                r = build_schedule(p)
                out = {"isin": p["isin"], "code": p["code"], "type": "ELT", "name": p["name"], "coupon": p["coupon"],
                       "issue_date": p["issue_date"], "mat_eval": p["mat_eval"], "assets": [], "schedule": r, "flags": []}
                pending = p["ref_date"] and p["ref_date"] > today.isoformat()
                out["judgment"] = {"status": "pending" if pending else "nodata", "worst": None, "next": None, "expected": None,
                                   "knocked_in": None, "redeemed": None, "need_pct": None, "ki_room": None,
                                   "message": (f"최초기준가격 결정일 {p['ref_date']} 이후 판정됩니다. " if pending else "")
                                              + "기초자산·최초기준가격 정보 등록 전입니다."}
            else:
                out = judge(p, None)
        except Exception as e:  # never break the ELS run
            log(f"elt judge error {m.get('no')}: {e}")
            continue
        j = out.get("judgment", {})
        has_c = p.get("coupon") is not None
        j["message"] = _clean(j.get("message"), has_c)
        if not has_c:
            if j.get("expected"):
                j["expected"]["ret"] = None
            for ev in out.get("schedule", []):
                ev["payout"] = None
        out["flags"] = [f for f in out.get("flags", [])
                        if not f.startswith("예탁원") and not f.startswith("발행일·기준가")]
        # ELT extras: settlement date per evaluation, periodic pay dates, notes
        settle = {e["n"]: e.get("settle") for e in (m.get("evals") or [])}
        for ev in out.get("schedule", []):
            ev["settle"] = settle.get(ev["n"])
        out["trade_date"] = m.get("trade_date")
        out["effective_date"] = m.get("effective_date")
        out["maturity_date"] = m.get("maturity_date")
        out["pay_dates"] = m.get("pay_dates") or []
        out["ki_level"] = m.get("ki")
        out["no_ki"] = p["no_ki"]
        out["mat_years"] = p.get("mat_years")
        out["period_months"] = p.get("period_months")
        out["notes"] = [NOTE_TEXT.get(n, n) for n in (m.get("notes") or [])]
        for a in out.get("assets", []):
            a["ref_source"] = "elt_master"
        st = j.get("status")
        counts[st] = counts.get(st, 0) + 1
        items.append(out)
    res = {"generated": generated, "asof": today.isoformat(), "counts": counts, "products": items}
    with open(os.path.join(data_dir, OUT), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, separators=(",", ":"))
    log(f"elt: {len(items)} products, counts={counts}")
    return {"enabled": True, "products": len(items), "counts": counts}
