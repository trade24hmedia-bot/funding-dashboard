#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
funding_scanner_v2.py  — quét funding 4 sàn, lọc cơ hội arbitrage, xuất JSON cho dashboard.
Delta-neutral: funding dương -> SHORT perp + LONG spot.
Public endpoint, KHÔNG cần API key.
"""
import os, time, math, json, concurrent.futures as cf
from datetime import datetime, timezone, timedelta
import requests, pandas as pd

# ============================ CẤU HÌNH ============================
EXCHANGES        = ["binance", "bybit", "okx", "bingx"]

SPOT_TAKER, SPOT_MAKER = 0.10, 0.10
PERP_TAKER, PERP_MAKER = 0.02, 0.05
FILL_MODE        = "taker"

HOLD_PERIODS     = 1
NORMALIZE_HOURS  = 4           # <-- chuẩn hóa & lọc theo mốc 4h (theo yêu cầu)
THRESHOLD_PCT    = 0.25        # lọc: rate quy về NORMALIZE_HOURS > 0.25%
REQUIRE_NET_POS  = True
POSITIVE_ONLY    = True

REQUIRE_SPOT     = True
MIN_SPOT_VOL_USDT = 1_000_000

HISTORY_DAYS     = 7
MIN_PCT_POSITIVE = 0.60

QUOTE            = "USDT"
OUT_XLSX, OUT_CSV = "funding_v2.xlsx", "funding_v2.csv"
OUT_JSON         = "docs/data.json"   # dashboard đọc file này
REQ_TIMEOUT, HEADERS = 15, {"User-Agent": "funding-scanner/2.1"}
# =================================================================

def round_trip_cost() -> float:
    s = SPOT_TAKER if FILL_MODE == "taker" else SPOT_MAKER
    p = PERP_TAKER if FILL_MODE == "taker" else PERP_MAKER
    return 2*s + 2*p

def _get(url, params=None, retries=3, backoff=1.2):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=REQ_TIMEOUT)
            r.raise_for_status(); return r.json()
        except Exception as e:
            last = e; time.sleep(backoff*(i+1))
    raise last

def norm(sym): return sym.upper().replace("-SWAP","").replace("_","").replace("-","")

def _ms(x):
    try:
        v = int(x); return v if v > 0 else 0
    except Exception:
        return 0

def _fmt(ms):
    return datetime.fromtimestamp(ms/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ms else None

# ==================== CURRENT FUNDING ====================
def cur_binance():
    rows = []; data = _get("https://fapi.binance.com/fapi/v1/premiumIndex")
    imap = {}
    try:
        for it in _get("https://fapi.binance.com/fapi/v1/fundingInfo"):
            imap[it["symbol"]] = int(it.get("fundingIntervalHours", 8))
    except Exception: pass
    for d in data:
        r = d.get("lastFundingRate")
        if r in (None,""): continue
        rows.append({"exchange":"binance","common":norm(d["symbol"]),"orig_symbol":d["symbol"],
                     "rate":float(r),"interval_h":imap.get(d["symbol"],8),"next_ms":_ms(d.get("nextFundingTime"))})
    return rows

def cur_bybit():
    rows = []; tick = _get("https://api.bybit.com/v5/market/tickers", {"category":"linear"})
    imap = {}
    try:
        info = _get("https://api.bybit.com/v5/market/instruments-info", {"category":"linear","limit":1000})
        for it in info.get("result",{}).get("list",[]):
            if it.get("fundingInterval"): imap[it["symbol"]] = int(it["fundingInterval"])/60.0
    except Exception: pass
    for t in tick.get("result",{}).get("list",[]):
        r = t.get("fundingRate")
        if r in (None,""): continue
        rows.append({"exchange":"bybit","common":norm(t["symbol"]),"orig_symbol":t["symbol"],
                     "rate":float(r),"interval_h":imap.get(t["symbol"],8),"next_ms":_ms(t.get("nextFundingTime"))})
    return rows

def _okx_one(instId):
    try:
        j = _get("https://www.okx.com/api/v5/public/funding-rate", {"instId":instId})
        d = (j.get("data") or [None])[0]
        if not d or d.get("fundingRate") in (None,""): return None
        ft, nft = _ms(d.get("fundingTime")), _ms(d.get("nextFundingTime"))
        iv = round((nft-ft)/3_600_000) if (ft and nft and nft>ft) else 8
        return {"exchange":"okx","common":norm(instId),"orig_symbol":instId,"rate":float(d["fundingRate"]),
                "interval_h":iv if iv in (1,2,4,8) else 8,"next_ms":nft}
    except Exception: return None

def cur_okx():
    inst = _get("https://www.okx.com/api/v5/public/instruments", {"instType":"SWAP"})
    ids = [it["instId"] for it in inst.get("data",[]) if it.get("settleCcy")==QUOTE]
    rows = []
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        for k, fut in enumerate(cf.as_completed({ex.submit(_okx_one,i):i for i in ids})):
            res = fut.result()
            if res: rows.append(res)
            if k % 15 == 0: time.sleep(0.3)
    return rows

def cur_bingx():
    rows = []; j = _get("https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex")
    for d in j.get("data",[]):
        r, sym = d.get("lastFundingRate"), d.get("symbol")
        if sym is None or r in (None,""): continue
        ft, nft = _ms(d.get("fundingTime") or d.get("time")), _ms(d.get("nextFundingTime"))
        iv = round((nft-ft)/3_600_000) if (ft and nft and nft>ft) else 8
        rows.append({"exchange":"bingx","common":norm(sym),"orig_symbol":sym,"rate":float(r),
                     "interval_h":iv if iv in (1,2,4,8) else 8,"next_ms":nft})
    return rows

# ==================== SPOT TICKERS ====================
def spot_binance():
    out = {}
    for t in _get("https://api.binance.com/api/v3/ticker/24hr"):
        if t.get("symbol","").endswith(QUOTE):
            try: out[norm(t["symbol"])] = float(t.get("quoteVolume",0))
            except Exception: pass
    return out
def spot_bybit():
    out = {}; j = _get("https://api.bybit.com/v5/market/tickers", {"category":"spot"})
    for t in j.get("result",{}).get("list",[]):
        if t.get("symbol","").endswith(QUOTE):
            try: out[norm(t["symbol"])] = float(t.get("turnover24h",0))
            except Exception: pass
    return out
def spot_okx():
    out = {}; j = _get("https://www.okx.com/api/v5/market/tickers", {"instType":"SPOT"})
    for t in j.get("data",[]):
        if t.get("instId","").endswith("-"+QUOTE):
            try: out[norm(t["instId"])] = float(t.get("volCcy24h",0))
            except Exception: pass
    return out
def spot_bingx():
    out = {}
    try:
        j = _get("https://open-api.bingx.com/openApi/spot/v1/ticker/24hr")
        for t in (j.get("data") if isinstance(j.get("data"), list) else []):
            sym = t.get("symbol","")
            if sym.replace("-","").endswith(QUOTE):
                try: out[norm(sym)] = float(t.get("quoteVolume",0) or t.get("volume",0))
                except Exception: pass
    except Exception: pass
    return out

# ==================== HISTORY 7d ====================
def hist_binance(sym):
    start = int((datetime.now(tz=timezone.utc)-timedelta(days=HISTORY_DAYS)).timestamp()*1000)
    j = _get("https://fapi.binance.com/fapi/v1/fundingRate", {"symbol":sym,"startTime":start,"limit":1000})
    return [float(x["fundingRate"]) for x in j] if isinstance(j,list) else []
def hist_bybit(sym):
    j = _get("https://api.bybit.com/v5/market/funding/history", {"category":"linear","symbol":sym,"limit":200})
    return [float(x["fundingRate"]) for x in j.get("result",{}).get("list",[])]
def hist_okx(instId):
    j = _get("https://www.okx.com/api/v5/public/funding-rate-history", {"instId":instId,"limit":100})
    return [float(x["realizedRate"]) for x in j.get("data",[]) if x.get("realizedRate") not in (None,"")]
def hist_bingx(sym):
    try:
        j = _get("https://open-api.bingx.com/openApi/swap/v2/quote/fundingRate", {"symbol":sym,"limit":200})
        return [float(x.get("fundingRate")) for x in j.get("data",[]) if x.get("fundingRate") not in (None,"")]
    except Exception: return []

CUR  = {"binance":cur_binance,"bybit":cur_bybit,"okx":cur_okx,"bingx":cur_bingx}
SPOT = {"binance":spot_binance,"bybit":spot_bybit,"okx":spot_okx,"bingx":spot_bingx}
HIST = {"binance":hist_binance,"bybit":hist_bybit,"okx":hist_okx,"bingx":hist_bingx}

def collect_current():
    rows = []
    for e in EXCHANGES:
        try:
            print(f"[+] funding: {e} ..."); r = CUR[e](); print(f"    -> {len(r)}"); rows += r
        except Exception as ex: print(f"[!] {e} lỗi (bỏ qua): {ex}")
    return rows

def collect_spot():
    spot = {}
    for e in EXCHANGES:
        try:
            print(f"[+] spot: {e} ..."); spot[e] = SPOT[e](); print(f"    -> {len(spot[e])}")
        except Exception as ex: print(f"[!] spot {e} lỗi (bỏ qua): {ex}"); spot[e] = {}
    return spot

def enrich_history(df):
    def job(row):
        try: rates = HIST[row["exchange"]](row["orig_symbol"])
        except Exception: rates = []
        if rates:
            return row.name, sum(rates)/len(rates)*100, sum(1 for x in rates if x>0)/len(rates), len(rates)
        return row.name, math.nan, math.nan, 0
    res = {}
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for idx, avg, pos, n in ex.map(job, [r for _, r in df.iterrows()]):
            res[idx] = (avg, pos, n)
    df["avg_7d_pct"]      = df.index.map(lambda i: res.get(i,(math.nan,))[0])
    df["pct_positive_7d"] = df.index.map(lambda i: res.get(i,(math.nan,math.nan))[1])
    df["n_periods_7d"]    = df.index.map(lambda i: res.get(i,(0,0,0))[2])
    return df

def build(rows, spot):
    df = pd.DataFrame(rows)
    if df.empty: return df, df
    df = df[df["common"].str.endswith(QUOTE)].copy()
    df["rate_pct"] = df["rate"]*100
    df["rate_norm_pct"] = df["rate_pct"]*(NORMALIZE_HOURS/df["interval_h"].clip(lower=0.5))
    df["next_funding"] = df["next_ms"].map(_fmt)
    df["spot_vol_usdt"] = df.apply(lambda r: spot.get(r["exchange"],{}).get(r["common"], math.nan), axis=1)
    df["has_spot"] = df["spot_vol_usdt"].notna()

    rt = round_trip_cost()
    df["round_trip_pct"] = rt
    df["funding_min_hold_pct"] = df["rate_pct"]*HOLD_PERIODS
    df["net_pct"] = df["funding_min_hold_pct"] - rt
    df["break_even_periods"] = df["rate_pct"].apply(lambda x: math.ceil(rt/x) if x>0 else math.inf)
    df["daily_yield_pct"] = df["rate_pct"]*(24.0/df["interval_h"].clip(lower=0.5))

    m = pd.Series(True, index=df.index)
    if POSITIVE_ONLY:    m &= df["rate_pct"] > 0
    if THRESHOLD_PCT:    m &= df["rate_norm_pct"] > THRESHOLD_PCT
    if REQUIRE_SPOT:     m &= df["has_spot"] & (df["spot_vol_usdt"] >= MIN_SPOT_VOL_USDT)
    if REQUIRE_NET_POS:  m &= df["net_pct"] > 0
    surv = df[m].sort_values("net_pct", ascending=False).copy()

    if not surv.empty:
        surv = enrich_history(surv)
        if MIN_PCT_POSITIVE:
            surv = surv[(surv["pct_positive_7d"].isna()) | (surv["pct_positive_7d"] >= MIN_PCT_POSITIVE)]
        surv = surv.sort_values("net_pct", ascending=False).reset_index(drop=True)
    return df, surv

COLS = ["exchange","orig_symbol","rate_pct","interval_h","rate_norm_pct","funding_min_hold_pct",
        "round_trip_pct","net_pct","break_even_periods","daily_yield_pct","avg_7d_pct",
        "pct_positive_7d","spot_vol_usdt","next_funding","next_ms"]

def export_excel(alldf, surv):
    info = pd.DataFrame({
        "Thông số":["Chạy lúc (UTC)","Fill mode","Round-trip %","Giữ (kỳ)",
                    f"Ngưỡng {NORMALIZE_HOURS}h %","Vol Spot min","% kỳ dương 7d","Tổng cặp","Đạt ĐK"],
        "Giá trị":[datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),FILL_MODE,round(round_trip_cost(),4),
                   HOLD_PERIODS,THRESHOLD_PCT,f"{MIN_SPOT_VOL_USDT:,.0f}",MIN_PCT_POSITIVE,len(alldf),len(surv)]})
    show = [c for c in COLS if c != "next_ms"]
    for c in show:
        if c not in surv.columns: surv[c] = math.nan
    with pd.ExcelWriter(OUT_XLSX, engine="openpyxl") as xw:
        info.to_excel(xw, sheet_name="Info", index=False)
        if surv.empty:
            pd.DataFrame({"Ghi chú":["Không có cặp đạt ĐK. Nới THRESHOLD_PCT / MIN_SPOT_VOL_USDT / MIN_PCT_POSITIVE."]}
                         ).to_excel(xw, sheet_name="CoHoi", index=False)
        else:
            surv[show].to_excel(xw, sheet_name="CoHoi", index=False)
        if not alldf.empty and "net_pct" in alldf.columns:
            tc = [c for c in show if c in alldf.columns] + (["has_spot"] if "has_spot" in alldf.columns else [])
            alldf.sort_values("net_pct", ascending=False)[tc].to_excel(xw, sheet_name="TatCa", index=False)
    surv[show].to_csv(OUT_CSV, index=False)

def export_json(alldf, surv):
    def clean(v):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)): return None
        return v
    recs = []
    src = surv if not surv.empty else pd.DataFrame(columns=COLS)
    for _, r in src.iterrows():
        recs.append({k: clean(r.get(k)) for k in COLS})
    payload = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "params": {"normalize_hours":NORMALIZE_HOURS,"threshold_pct":THRESHOLD_PCT,
                   "round_trip_pct":round(round_trip_cost(),4),"hold_periods":HOLD_PERIODS,
                   "fill_mode":FILL_MODE,"min_spot_vol_usdt":MIN_SPOT_VOL_USDT,
                   "min_pct_positive":MIN_PCT_POSITIVE},
        "count_total": int(len(alldf)),
        "count_opportunities": int(len(surv)),
        "rows": recs,
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

def main():
    rows = collect_current()
    spot = collect_spot()
    alldf, surv = build(rows, spot)
    export_json(alldf, surv)                 # luôn ghi JSON (kể cả rỗng) cho dashboard — ưu tiên cao nhất
    if not alldf.empty:
        try:
            export_excel(alldf, surv)
        except Exception as e:               # lỗi Excel KHÔNG được làm chết cả lượt chạy CI
            print(f"[!] Bỏ qua xuất Excel: {e}")
    print(f"\nĐạt điều kiện: {len(surv)} / {len(alldf)} cặp. Đã ghi {OUT_JSON}"
          + (f", {OUT_XLSX}, {OUT_CSV}" if not alldf.empty else ""))
    if not surv.empty:
        show = ["exchange","orig_symbol","rate_pct","interval_h","rate_norm_pct","net_pct",
                "break_even_periods","pct_positive_7d","next_funding"]
        with pd.option_context("display.width",200,"display.max_rows",None):
            print(surv[show].round(4).to_string(index=False))

if __name__ == "__main__":
    main()
