import os
import tempfile

import pandas as pd
import streamlit as st
from google import genai

import core
import db
import offline

MODEL = st.secrets.get("MODEL", "gemini-3.8-flash")
API_KEY = st.secrets.get("GEMINI_API_KEY", "")
DB_PATH = os.path.join(tempfile.gettempdir(), "realestate.db")

st.set_page_config(page_title="Real Estate AI", page_icon="🏠", layout="wide")
st.title("🏠 Real Estate AI Database")

if not API_KEY:
    st.error("GEMINI_API_KEY Streamlit secrets mein set nahi hai.")
    st.stop()

def fmt_eta(sec):
    sec = int(sec)
    return f"{sec // 60} min {sec % 60} sec" if sec >= 60 else f"{sec} sec"


client = genai.Client(api_key=API_KEY)
con = db.connect(DB_PATH)

# ---------------- Sidebar: database backup / restore ----------------
with st.sidebar:
    s = db.stats(con)
    st.subheader("📦 Database")
    st.metric("Listings", f"{s['listings']:,}")
    st.caption(f"{s['locations']:,} locations · size {os.path.getsize(DB_PATH) / 1e6:.2f} MB")
    if s["listings"]:
        st.caption(f"🧠 {s['offline_rows']:,} listings offline model ne nikali, "
                   f"{s['listings'] - s['offline_rows']:,} Gemini ne. Hard list: {s['hard']:,}")
        st.caption(f"⚠️ {s['flagged']:,} listings 'verify' flag ke saath "
                   f"({s['flagged'] / s['listings']:.0%}): number ya location adhura/shaky tha.")
    st.warning("Streamlit free server restart hone par database mit jata hai. "
               "Har baar kaam ke baad **Backup download** karo, aur restart ke baad **Restore** karo.")
    if s["listings"]:
        con.commit()
        con.execute("VACUUM")
        with open(DB_PATH, "rb") as f:
            st.download_button("⬇️ Backup download (.db)", f.read(), "realestate.db",
                               "application/octet-stream", width="stretch")
    restore = st.file_uploader("⬆️ Backup restore karo (.db)", type=["db"], key="restore")
    if restore is not None and st.button("Restore karo", width="stretch"):
        con.close()
        with open(DB_PATH, "wb") as f:
            f.write(restore.read())
        st.success("Restore ho gaya. Page refresh karo.")
        st.stop()

tab_chat, tab_add, tab_brain, tab_audit = st.tabs(["💬 Sawal pucho", "➕ Chat add karo", "🧠 Offline model", "🔍 Accuracy check"])

# ---------------- Tab 1: Q&A ----------------
with tab_chat:
    st.caption("Kisi bhi bhasha mein pucho. Jaise: *Nariman City mein 1 BHK flat batao* · "
               "*50 lakh tak ka plot RR Residency* · *Indore mein rent par 2 BHK*")
    if "history" not in st.session_state:
        st.session_state.history = []
    for role, content in st.session_state.history:
        with st.chat_message(role):
            if isinstance(content, pd.DataFrame):
                st.dataframe(content, width="stretch", hide_index=True)
            else:
                st.markdown(content)

    q = st.chat_input("Apna sawal likho...")
    if q:
        st.session_state.history.append(("user", q))
        with st.chat_message("user"):
            st.markdown(q)
        with st.chat_message("assistant"):
            if db.stats(con)["listings"] == 0:
                msg = "Database abhi khaali hai. Pehle **Chat add karo** tab mein chat upload karo."
                st.markdown(msg); st.session_state.history.append(("assistant", msg))
            else:
                bar = st.progress(0.05, text="5% · Sawal samajh raha hoon...")
                try:
                    flt = core.parse_question(client, MODEL, q)
                    bar.progress(0.55, text="55% · Database mein dhoondh raha hoon...")
                    rows, loc_ok = db.search(con, flt, limit=25)
                    bar.progress(0.9, text="90% · Jawab taiyaar kar raha hoon...")
                except Exception as e:
                    flt, rows, loc_ok = {}, [], True
                    st.error(f"Sawal samajhne mein dikkat: {str(e)[:150]}")
                bar.progress(1.0, text="100% · Ho gaya")
                bar.empty()
                if not loc_ok:
                    msg = f"**{flt.get('location')}** naam ki koi location database mein nahi mili."
                    st.markdown(msg); st.session_state.history.append(("assistant", msg))
                elif not rows:
                    msg = "Is filter se koi listing nahi mili. Price ya BHK thoda badal ke try karo."
                    st.markdown(msg); st.session_state.history.append(("assistant", msg))
                else:
                    shown = {k: v for k, v in flt.items() if v}
                    msg = f"**{len(rows)} listings mili** (naye pehle). Filter: `{shown}`"
                    st.markdown(msg)
                    df = pd.DataFrame(rows)
                    df = df.loc[:, (df != "").any(axis=0)]
                    st.dataframe(df, width="stretch", hide_index=True)
                    st.session_state.history.append(("assistant", msg))
                    st.session_state.history.append(("assistant", df))

# ---------------- Tab 2: Add chats ----------------
with tab_add:
    uploaded = st.file_uploader("WhatsApp chat (.txt ya .zip, kai files ek saath bhi)",
                                type=["txt", "zip"], accept_multiple_files=True)
    mode_options = ["Pehle offline, jo samajh na aaye wo Gemini se (recommended)",
                    "Sirf offline (Gemini/internet nahi) - jo na samjhe wo 'hard list' mein jama",
                    "Sirf Gemini (purana tarika)"]
    mode_label = st.radio("Kaise padhna hai?", mode_options, index=0)
    mode = ["hybrid", "offline", "gemini"][mode_options.index(mode_label)]
    c1, c2, c3 = st.columns(3)
    max_batches = c1.number_input("Gemini ke max batches (1 batch = 40 messages)", 1, 500, 40,
                                  help="Free Gemini ki daily limit ke hisaab se. Baaki kaam agle din resume hota hai.")
    keep_source = c2.checkbox("Original message bhi save karo (accuracy check ke liye)", value=True,
                              help="Size thoda badhta hai (~100 bytes/message) par 'Accuracy check' tab tabhi chalta hai.")
    double_check = c3.checkbox("Double-check (zyada accurate)", value=True,
                               help="Jin messages se kuch nahi nikla par wo property jaise lagte hain, unhe dobara padhta hai.")


    def make_progress(bar):
        def on_progress(i):
            if i["phase"] == "Offline":
                txt = f"Phase 1/2 · Offline padh raha hoon · {i['pct']:.0%} · {i['done']:,}/{i['total']:,} messages · {i['listings']:,} listings"
            else:
                txt = (f"Gemini · {i['pct']:.0%} · {i['done']:,}/{i['total']:,} messages · "
                       f"{i['listings']:,} listings · baaki ~{fmt_eta(i['eta_s'])}")
            bar.progress(min(i["pct"], 1.0), text=txt)
        return on_progress


    if uploaded:
        msgs = core.parse_chat(core.read_uploads(uploaded))
        cands = [m for m in msgs if core.is_candidate(m)]
        st.write(f"Total messages: **{len(msgs):,}** · Property-jaise: **{len(cands):,}**")
        if st.button("🚀 Database mein daalo", type="primary"):
            bar = st.progress(0.0, text="0% · shuru ho raha hai...")
            errs = []
            res = core.ingest(con, client, MODEL, msgs, db, mode=mode, max_batches=int(max_batches),
                              keep_source=keep_source, double_check=double_check,
                              progress=make_progress(bar), log=errs.append)
            con.commit()
            bar.progress(1.0, text="100% · Ho gaya ✅")
            st.success(f"Offline model: {res['offline_listings']} listings ({res['offline_messages']} messages) · "
                       f"Gemini: {res['new_listings']} listings ({res['new_messages']} messages)")
            if res["junk"]:
                st.caption(f"{res['junk']} messages mein na size, na price, na BHK tha, to chhod diye.")
            if res["hard_saved"]:
                st.info(f"{res['hard_saved']} messages offline model ko samajh nahi aaye, 'hard list' mein jama hue. "
                        "**🧠 Offline model** tab mein inhe Gemini se solve karwa sakte ho.")
            if res["pending_messages"]:
                st.info(f"{res['pending_messages']:,} messages abhi baaki hain. "
                        "Wahi file dobara upload karke button dabao, ye wahin se aage badhega.")
            if res["failed_batches"]:
                st.warning(f"{res['failed_batches']} batch fail hue (limit/error). Dobara chalao, "
                           f"wo dobara try honge. Error: {errs[-1] if errs else ''}")

# ---------------- Tab 3: offline model ----------------
with tab_brain:
    s = db.stats(con)
    st.markdown("**Kaise seekhta hai:** Gemini 'teacher' hai. Jab Gemini koi message padhta hai, offline model "
                "uske jawab se naye location-naam, shabd (jaise *बेचना* = Sale) aur Hindi spelling seekhta hai. "
                "Agli baar wahi tarah ka message wo bina Gemini ke nikaal leta hai.")
    c1, c2, c3 = st.columns(3)
    c1.metric("Offline ne nikali listings", f"{s['offline_rows']:,}")
    c2.metric("Hard list (samajh nahi aaye)", f"{s['hard']:,}")
    c3.metric("Seekhe hue shabd/votes", f"{con.execute('SELECT COUNT(*) FROM vote').fetchone()[0]:,}")

    if st.button("📊 Offline model ka score nikalo"):
        ev = offline.evaluate(con)
        if ev["examples"] == 0:
            st.info("Abhi Gemini ke jawab saved nahi hain. Pehle Gemini se kuch chat padhwao.")
        else:
            cov = ev["covered"] / ev["examples"]
            acc = ev["correct"] / ev["covered"] if ev["covered"] else 0
            st.success(f"Gemini ke {ev['examples']:,} purane jawabon par: offline model ne **{cov:.0%}** khud nikale, "
                       f"aur unme se **{acc:.0%}** Gemini se bilkul mile.")
            st.caption("Ye score unhi messages par hai jinse model ne seekha. Nayi chat par thoda kam aa sakta hai. "
                       "'Accuracy check' tab se asli jaanch karo.")

    if s["hard"]:
        st.divider()
        st.write(f"**Hard list mein {s['hard']:,} messages hain.** Inhe Gemini padhega aur offline model unse seekhega.")
        b1, b2 = st.columns(2)
        hb = b1.number_input("Max batches", 1, 500, 40, key="hb")
        if b1.button("🧑‍🏫 Gemini se hard list solve karo"):
            bar = st.progress(0.0, text="0% · shuru...")
            errs = []
            r = core.solve_hard(con, client, MODEL, db, max_batches=int(hb), progress=make_progress(bar), log=errs.append)
            bar.progress(1.0, text="100% · Ho gaya ✅")
            st.success(f"{r['solved']} messages solve hue, {r['new_listings']} nayi listings. "
                       f"Hard list mein baaki: {r['left']}")
            if r["failed_batches"]:
                st.warning(f"{r['failed_batches']} batch fail hue. Dobara chalao. {errs[-1] if errs else ''}")
        b2.download_button("⬇️ Hard cases download (.jsonl)", core.export_hard(con).encode("utf-8"),
                           "hard_cases.jsonl", "application/json",
                           help="Ye file developer/Claude ko dikhao taaki offline model ka code aur behtar ho.")

# ---------------- Tab 3: accuracy audit ----------------
with tab_audit:
    st.caption("Random listings ko unke asli WhatsApp message ke saath milao. 20 dekhne ke baad "
               "pata chal jata hai ki database kitna sahi hai.")
    if db.stats(con)["with_source"] == 0:
        st.info("Iske liye 'Original message bhi save karo' ON karke chat add karni hogi.")
    elif st.button("🎲 10 random listings dikhao"):
        for r in db.random_audit(con, 10):
            with st.container(border=True):
                left, right = st.columns(2)
                left.markdown("**Asli message**")
                left.text(r["original"][:600])
                right.markdown("**Database ne kya samjha**" + (" ⚠️" if r["Flag"] else ""))
                right.json({k: v for k, v in r.items() if k not in ("original", "Flag") and v != ""})
