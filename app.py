import os
import tempfile

import pandas as pd
import streamlit as st
from google import genai

import core
import db

MODEL = st.secrets.get("MODEL", "gemini-3.8-flash")
API_KEY = st.secrets.get("GEMINI_API_KEY", "")
DB_PATH = os.path.join(tempfile.gettempdir(), "realestate.db")

st.set_page_config(page_title="Real Estate AI", page_icon="🏠", layout="wide")
st.title("🏠 Real Estate AI Database")

if not API_KEY:
    st.error("GEMINI_API_KEY Streamlit secrets mein set nahi hai.")
    st.stop()

client = genai.Client(api_key=API_KEY)
con = db.connect(DB_PATH)

# ---------------- Sidebar: database backup / restore ----------------
with st.sidebar:
    s = db.stats(con)
    st.subheader("📦 Database")
    st.metric("Listings", f"{s['listings']:,}")
    st.caption(f"{s['locations']:,} locations · {s['messages_done']:,} entries processed · "
               f"size {os.path.getsize(DB_PATH) / 1e6:.2f} MB")
    st.warning("Streamlit free server restart hone par database mit jata hai. "
               "Har baar kaam ke baad **Backup download** karo, aur restart ke baad **Restore** karo.")
    if s["listings"]:
        con.commit()
        con.execute("VACUUM")
        with open(DB_PATH, "rb") as f:
            st.download_button("⬇️ Backup download (.db)", f.read(), "realestate.db",
                               "application/octet-stream", use_container_width=True)
    restore = st.file_uploader("⬆️ Backup restore karo (.db)", type=["db"], key="restore")
    if restore is not None and st.button("Restore karo", use_container_width=True):
        con.close()
        with open(DB_PATH, "wb") as f:
            f.write(restore.read())
        st.success("Restore ho gaya. Page refresh karo.")
        st.stop()

tab_chat, tab_add = st.tabs(["💬 Sawal pucho", "➕ Chat add karo"])

# ---------------- Tab 1: Q&A ----------------
with tab_chat:
    st.caption("Kisi bhi bhasha mein pucho. Jaise: *Nariman City mein 1 BHK flat batao* · "
               "*50 lakh tak ka plot RR Residency* · *Indore mein rent par 2 BHK*")
    if "history" not in st.session_state:
        st.session_state.history = []
    for role, content in st.session_state.history:
        with st.chat_message(role):
            if isinstance(content, pd.DataFrame):
                st.dataframe(content, use_container_width=True, hide_index=True)
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
                try:
                    flt = core.parse_question(client, MODEL, q)
                    rows, loc_ok = db.search(con, flt, limit=25)
                except Exception as e:
                    flt, rows, loc_ok = {}, [], True
                    st.error(f"Sawal samajhne mein dikkat: {str(e)[:150]}")
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
                    st.dataframe(df, use_container_width=True, hide_index=True)
                    st.session_state.history.append(("assistant", msg))
                    st.session_state.history.append(("assistant", df))

# ---------------- Tab 2: Add chats ----------------
with tab_add:
    uploaded = st.file_uploader("WhatsApp chat (.txt ya .zip, kai files ek saath bhi)",
                                type=["txt", "zip"], accept_multiple_files=True)
    c1, c2, c3 = st.columns(3)
    max_batches = c1.number_input("Ek baar mein max batches (1 batch = 40 messages)", 1, 500, 40,
                                  help="Free Gemini ki daily limit ke hisaab se. Baaki kaam agle din resume hota hai.")
    keep_source = c2.checkbox("Original message bhi save karo (size badhta hai)", value=False)
    if uploaded:
        msgs = core.parse_chat(core.read_uploads(uploaded))
        cands = [m for m in msgs if core.is_candidate(m)]
        st.write(f"Total messages: **{len(msgs):,}** · Property-jaise: **{len(cands):,}**")
        if st.button("🚀 Database mein daalo", type="primary"):
            bar = st.progress(0.0)
            errs = []
            res = core.ingest(
                con, client, MODEL, msgs, db, max_batches=int(max_batches), keep_source=keep_source,
                progress=lambda d, t: bar.progress(d / max(t, 1)), log=errs.append)
            con.commit()
            st.success(f"{res['new_listings']} nayi listings save hui "
                       f"({res['new_messages']} naye messages padhe).")
            if res["pending_messages"]:
                st.info(f"{res['pending_messages']:,} messages abhi baaki hain. "
                        "Wahi file dobara upload karke button dabao, ye wahin se aage badhega.")
            if res["failed_batches"]:
                st.warning(f"{res['failed_batches']} batch fail hue (limit/error). Dobara chalao, "
                           f"wo dobara try honge. Error: {errs[-1] if errs else ''}")
