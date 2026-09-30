"""Stage 7: the demo front end (Lab 7 A4).

    python -m regtech serve                    # terminal 1: the API on :8000
    streamlit run regtech/ui.py                # terminal 2: this page

Citations expand to show the source text: grounding the user cannot check is decoration.
"""
from __future__ import annotations

import json

import requests
import streamlit as st

st.set_page_config(page_title="RegTech Compliance Copilot", layout="centered")
API = st.sidebar.text_input("Service URL", "http://localhost:8000")
st.title("RegTech Compliance Copilot")
st.caption("For Indian NBFCs. Answers come only from 12 RBI Directions, 11 Fair Practices Codes and 13 RBI penalty "
           "orders, and every claim is cited. When the documents do not cover a question, it says so. "
           "Not legal advice.")

mode = st.radio("What do you need?", ["Ask the rules (fast)", "Ask the agent (policies, penalties, deadlines)"],
                horizontal=True)
agent_mode = mode.startswith("Ask the agent")
q = st.text_area("Question", placeholder=("Check Tata Capital's Code on the Key Facts Statement" if agent_mode
                                          else "Within how many days must property documents be returned after repayment?"))
upload_id = None
if agent_mode:
    f = st.file_uploader("Optional: a policy PDF to check (the check reports what the document says; it does not "
                         "verify that the document is genuine)", type=["pdf"])
    if f is not None:
        r = requests.post(f"{API}/upload", files={"file": (f.name, f.getvalue(), "application/pdf")}, timeout=60)
        if r.ok:
            upload_id = r.json()["upload_id"]
            st.caption(f"uploaded as `{upload_id}`")
        else:
            st.error(f"{r.status_code}: {r.text[:200]}")
stream = st.checkbox("Stream the answer", value=True, disabled=agent_mode)


def show_citations(cites: list[dict]) -> None:
    for c in cites:
        with st.expander(f"[{c['index']}] {c.get('label') or c['doc_id']}"):
            st.text(c["excerpt"])


def show_meta(latency_ms, cost_usd, cached, trace_id, extra: str = "") -> None:
    cols = st.columns(3)
    cols[0].metric("latency", f"{latency_ms / 1000:.1f} s")
    cols[1].metric("cost", f"${cost_usd:.4f}")
    cols[2].metric("cached", cached or "no")
    st.caption(f"trace `{trace_id}` {extra}")


if st.button("Ask", type="primary") and q.strip():
    if stream and not agent_mode:
        box, verdict, done = st.empty(), None, None
        draft = ""
        try:
            with requests.post(f"{API}/ask/stream", json={"question": q}, stream=True, timeout=120) as r:
                event = None
                for line in r.iter_lines(decode_unicode=True):
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data = json.loads(line[5:])
                        if event == "token":
                            draft += data["text"]
                            box.markdown(draft + " ▌\n\n*checking citations…*")
                        elif event == "verdict":
                            verdict = data
                        elif event == "done":
                            done = data
                        elif event == "error":
                            st.error(f"{data['status']}: {data['detail']} (trace {data.get('trace_id')})")
        except requests.RequestException as exc:
            st.error(f"service unreachable: {exc}")
            st.stop()
        if verdict:
            if verdict["retracted"]:
                st.warning("The streamed draft failed the citation check and was replaced.")
            (box.warning if verdict["refused"] else box.markdown)(verdict["answer"])
            show_citations(verdict["citations"])
        if done:
            show_meta(done["latency_ms"], done["cost_usd"], None, done["trace_id"],
                      f"· first token after {(done['ttft_ms'] or 0) / 1000:.1f} s")
    else:
        with st.spinner("working (the agent can take 10-40 s when it checks a policy)" if agent_mode else "thinking"):
            try:
                r = requests.post(f"{API}/ask", json={"question": q, "mode": "agent" if agent_mode else "qa",
                                                      "upload_id": upload_id}, timeout=180)
            except requests.RequestException as exc:
                st.error(f"service unreachable: {exc}")
                st.stop()
        if not r.ok:
            st.error(f"{r.status_code}: {r.json().get('detail', r.text[:200])}")
            st.stop()
        d = r.json()
        (st.warning if d["refused"] else st.markdown)(d["answer"])
        show_citations(d["citations"])
        if d["tool_calls"]:
            st.caption("tools: " + ", ".join(t["tool"] + ("" if t["ok"] else " (denied)") for t in d["tool_calls"]))
        for flag in d["flags"]:
            st.caption(f"⚑ {flag}")
        show_meta(d["latency_ms"], d["cost_usd"], d["cached"], d["trace_id"])
