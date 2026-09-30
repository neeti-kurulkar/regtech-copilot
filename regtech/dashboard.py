"""Stage 7: the operations dashboard, read from aip traces (Lab 7 Part C).

    streamlit run regtech/dashboard.py

Every request writes spans to .aip_traces/<run>.jsonl, all sharing the request's trace id (returned to the
caller in every response). This page answers "why did request X take 9 seconds?" (trace lookup), "which
stage should I optimise?" (latency by stage), and "is anything wrong right now?" (alerts).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aip.config import settings  # noqa: E402

REQUEST_SPANS = ("http.ask", "http.ask_stream")
P95_SLO_MS = {"qa": 6000, "agent": 40000, None: 6000}

st.set_page_config(page_title="RegTech Copilot · Ops", layout="wide")
st.title("RegTech Compliance Copilot · operations")

runs = sorted(settings.trace_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
if not runs:
    st.info(f"No traces yet in {settings.trace_dir}. Send some requests to the service first.")
    st.stop()
chosen = st.sidebar.multiselect("trace files (one per service run)", [p.stem for p in runs], default=[runs[0].stem])
rows = [json.loads(line) for p in runs if p.stem in chosen for line in p.open(encoding="utf-8") if line.strip()]
if not rows:
    st.stop()
df = pd.DataFrame(rows)
df["ts"] = pd.to_datetime(df["ts"], unit="s")
for col in ("cost_usd", "cached", "status_code", "mode", "refused", "trace_id", "error"):
    if col not in df:
        df[col] = None
req = df[df["name"].isin(REQUEST_SPANS)].copy()

# --- headline ---------------------------------------------------------------------------------------
c = st.columns(6)
c[0].metric("requests", len(req))
c[1].metric("total cost", f"${pd.to_numeric(req['cost_usd'], errors='coerce').fillna(0).sum():.4f}")
if len(req):
    c[2].metric("cost / request", f"${pd.to_numeric(req['cost_usd'], errors='coerce').fillna(0).mean():.4f}")
    c[3].metric("response-cache hits", f"{req['cached'].notna().mean():.0%}")
    c[4].metric("refusal rate", f"{req['refused'].fillna(False).astype(bool).mean():.0%}")
errors = df[df["name"] == "http.error"]
c[5].metric("errors", len(errors))

# --- alerts (Lab 7 C4) ---------------------------------------------------------------------------------
st.subheader("Alerts")
alerts = []
ok = req[req["status_code"] == 200].sort_values("ts")
if len(ok) >= 30:
    recent, before = ok.tail(20), ok.iloc[:-20].tail(100)
    r_now, r_before = recent["refused"].astype(bool).mean(), before["refused"].astype(bool).mean()
    if r_now >= max(2 * r_before, 0.2):
        alerts.append(f"Refusal rate {r_now:.0%} in the last 20 answers vs {r_before:.0%} before. This usually means "
                      "the index broke: check GET /health (chunk counts, corpus version), then open a recent refused "
                      "trace below and look at the retrieve.dense span and the sources it returned.")
for mode, g in ok.groupby(ok["mode"].fillna("qa")):
    last = g[g["ts"] >= g["ts"].max() - pd.Timedelta(minutes=5)]
    if len(last) >= 5 and last["duration_ms"].quantile(0.95) > P95_SLO_MS.get(mode, 6000):
        alerts.append(f"{mode}: p95 {last['duration_ms'].quantile(0.95):.0f} ms over the last 5 minutes is above the "
                      f"{P95_SLO_MS.get(mode, 6000)} ms SLO. Open the slowest trace below: a slow llm.call means the "
                      "provider (check llm.retry events for rate limits); a slow embed.batch means the query "
                      "embedding call.")
st.write("\n".join(f"- 🔴 {a}" for a in alerts) if alerts else "🟢 No alert: refusal rate stable, p95 within SLO.")

# --- latency by stage (Lab 7 B4 / C3) -----------------------------------------------------------------
st.subheader("Latency by stage")
stage = (df[df["duration_ms"] > 0].groupby("name")["duration_ms"]
         .agg(n="count", p50="median", p95=lambda s: s.quantile(0.95), total="sum")
         .sort_values("total", ascending=False).round(1))
st.dataframe(stage, width="stretch")
if len(req):
    st.line_chart(req.set_index("ts")["duration_ms"], height=200)

# --- cost over time --------------------------------------------------------------------------------------
st.subheader("Cumulative cost")
spend = df[df["name"].isin(["llm.call", "llm.stream"])].sort_values("ts")
if len(spend):
    spend = spend.assign(cum=pd.to_numeric(spend["cost_usd"], errors="coerce").fillna(0).cumsum())
    st.line_chart(spend.set_index("ts")["cum"], height=200)

# --- errors --------------------------------------------------------------------------------------------------
st.subheader("Errors")
if len(errors):
    st.dataframe(errors[["ts", "status_code", "error", "trace_id"]], width="stretch")
else:
    st.caption("none")

# --- trace lookup: "why did request X take 9 seconds?" ----------------------------------------------------
st.subheader("Trace lookup")
default = req.sort_values("duration_ms", ascending=False)["trace_id"].iloc[0] if len(req) else ""
tid = st.text_input("trace id (every API response returns one; default: the slowest request)", value=default)
if tid:
    spans = df[df["trace_id"] == tid].sort_values("ts")
    if spans.empty:
        st.warning("no spans with that trace id in the selected files")
    else:
        t0 = spans["ts"].min()
        view = spans.assign(start_ms=((spans["ts"] - t0).dt.total_seconds() * 1000).round(0))
        cols = [c for c in ("start_ms", "duration_ms", "name", "model", "cached", "tool", "cost_usd", "status",
                            "error") if c in view]
        st.dataframe(view[cols], width="stretch")
        top = view[view["name"].isin(["llm.call", "llm.stream", "embed.batch", "tool.call"])]
        if len(top):
            s = top.sort_values("duration_ms", ascending=False).iloc[0]
            st.caption(f"Largest single cost of time: {s['name']} {s.get('tool') or s.get('model') or ''} "
                       f"({s['duration_ms']:.0f} ms of {spans['duration_ms'].max():.0f} ms).")
