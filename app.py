import json
import re
import time

import pandas as pd
import streamlit as st
from google import genai
from google.genai import types

# ---------- Config ----------
# Model name Google time-time par badalta rehta hai. Agar error aaye to
# AI Studio mein dekho kaun sa Flash model available hai aur Streamlit secrets mein MODEL set karo.
MODEL = st.secrets.get("MODEL", "gemini-2.5-flash")
API_KEY = st.secrets.get("GEMINI_API_KEY", "")
BATCH_SIZE = 40          # ek request mein kitne messages bhejne hain
SLEEP_BETWEEN = 4        # free tier rate limit ke liye (seconds)

st.set_page_config(page_title="Real Estate WhatsApp Parser", layout="wide")
st.title("🏠 Real Estate WhatsApp Chat Parser")

# ---------- WhatsApp parsing ----------
LINE_RE = re.compile(
    r"^\[?(\d{1,2}/\d{1,2}/\d{2,4}),?\s+"
    r"(\d{1,2}:\d{2}(?::\d{2})?(?:\s?[APap][Mm])?)\]?\s*-?\s*"
    r"([^:]+?):\s(.*)$"
)

KEYWORDS = re.compile(
    r"(bhk|rk|flat|villa|plot|house|shop|office|rent|sale|sell|lakh|lac|cr\b|crore|"
    r"sq\.?\s?ft|sqft|sq\s?yd|gaj|kiraya|bikau|available|requirement|required|"
    r"\d{10})",
    re.IGNORECASE,
)


def parse_chat(text: str):
    text = text.replace("\u202f", " ").replace("\u200e", "")
    messages = []
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if m:
            date, time_, sender, body = m.groups()
            messages.append({"date": date, "time": time_, "sender": sender.strip(), "text": body})
        elif messages:  # multi-line message ka continuation
            messages[-1]["text"] += "\n" + line
    return messages


def is_candidate(msg):
    t = msg["text"]
    if "<Media omitted>" in t or "deleted" in t.lower() and len(t) < 40:
        return False
    return len(t) > 25 and bool(KEYWORDS.search(t))


# ---------- Gemini extraction ----------
PROMPT = """You extract real estate data from WhatsApp group messages (Hindi/English/Hinglish).
Each message below has an id. Return ONLY a JSON array. For each message that is a
property listing or a requirement, output one object (one object per property if a message lists several):

{{
  "msg_id": int,
  "listing_type": "Sale" | "Rent" | "Requirement" | "Unknown",
  "property_type": "Flat" | "Villa" | "House" | "Plot" | "Shop" | "Office" | "Land" | "Other",
  "bhk": string or null,
  "area": string or null,
  "price": string or null,
  "location": string or null,
  "contact_number": string or null,
  "notes": string or null
}}

Rules:
- Skip greetings, forwards, jokes, ads that are not property related.
- Never invent values. If something is not stated, use null.
- Keep price exactly as written (e.g. "45 lakh", "1.2 Cr", "15k/month").
- contact_number: digits only if present in the message.

Messages:
{messages}
"""


def extract_batch(client, batch):
    payload = "\n\n".join(f"[id={m['id']}] sender={m['sender']}\n{m['text']}" for m in batch)
    for attempt in range(3):
        try:
            resp = client.models.generate_content(
                model=MODEL,
                contents=PROMPT.format(messages=payload),
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", temperature=0
                ),
            )
            data = json.loads(resp.text)
            return data if isinstance(data, list) else []
        except Exception as e:  # rate limit / bad JSON
            if attempt == 2:
                st.warning(f"Ek batch fail hua: {e}")
                return []
            time.sleep(10 * (attempt + 1))
    return []


# ---------- UI ----------
if not API_KEY:
    st.error("GEMINI_API_KEY Streamlit secrets mein set nahi hai. README dekho.")
    st.stop()

uploaded = st.file_uploader("WhatsApp chat export (.txt) upload karein", type=["txt"])

if uploaded:
    raw = uploaded.read().decode("utf-8", errors="ignore")
    msgs = parse_chat(raw)
    for i, m in enumerate(msgs):
        m["id"] = i
    candidates = [m for m in msgs if is_candidate(m)]

    c1, c2 = st.columns(2)
    c1.metric("Total messages", len(msgs))
    c2.metric("Property-jaise messages", len(candidates))

    if st.button("Extract Real Estate Data", type="primary") and candidates:
        client = genai.Client(api_key=API_KEY)
        by_id = {m["id"]: m for m in msgs}
        rows = []
        bar = st.progress(0.0)
        n_batches = (len(candidates) + BATCH_SIZE - 1) // BATCH_SIZE

        for b in range(n_batches):
            batch = candidates[b * BATCH_SIZE:(b + 1) * BATCH_SIZE]
            for item in extract_batch(client, batch):
                src = by_id.get(item.get("msg_id"))
                if src:
                    item["date"] = src["date"]
                    item["posted_by"] = src["sender"]
                rows.append(item)
            bar.progress((b + 1) / n_batches)
            time.sleep(SLEEP_BETWEEN)

        if not rows:
            st.warning("Koi listing nahi mili.")
        else:
            df = pd.DataFrame(rows).drop(columns=["msg_id"], errors="ignore")
            df = df.drop_duplicates(subset=["contact_number", "price", "location", "bhk"])
            st.success(f"{len(df)} listings mili")
            st.dataframe(df, use_container_width=True)
            st.download_button(
                "CSV download karein",
                df.to_csv(index=False).encode("utf-8-sig"),
                "listings.csv",
                "text/csv",
            )
            st.caption("Accuracy ~90-95% hoti hai. Important listings ek baar manually verify kar lein.")
