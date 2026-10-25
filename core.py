"""Chat parsing + Gemini calls (extraction aur sawal samajhna)."""
import io
import json
import re
import time
import zipfile

from google.genai import types

LINE_RE = re.compile(
    r"^\[?(\d{1,2}/\d{1,2}/\d{2,4}),?\s+"
    r"(\d{1,2}:\d{2}(?::\d{2})?(?:\s?[APap][Mm])?)\]?\s*-?\s*"
    r"([^:]+?):\s(.*)$"
)

KEYWORDS = re.compile(
    r"(bhk|rk|flat|villa|plot|house|shop|office|rent|sale|sell|lakh|lac|cr\b|crore|"
    r"sq\.?\s?ft|sqft|sq\s?yd|gaj|kiraya|bikau|available|requirement|required|"
    r"square|feet|corner|garden|registry|direct deal|contact|"
    r"प्लॉट|प्लाट|बेचना|बिकाऊ|बेचने|बिक्री|किराए|किराये|किराया|मकान|घर|दुकान|फ्लैट|कोठी|जमीन|"
    r"ज़मीन|भूखंड|लाख|करोड़|वर्ग|फिट|फीट|उपलब्ध|आवश्यकता|चाहिए|संपर्क|"
    r"\d{10}|\d{5}\s?\d{5})",
    re.IGNORECASE,
)


def read_uploads(files):
    parts = []
    for f in files:
        data = f.read()
        if f.name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    for name in z.namelist():
                        if name.lower().endswith(".txt"):
                            parts.append(z.read(name).decode("utf-8", errors="ignore"))
            except zipfile.BadZipFile:
                pass
        else:
            parts.append(data.decode("utf-8", errors="ignore"))
    return "\n".join(parts)


def parse_chat(text):
    text = text.replace(" ", " ").replace("‎", "")
    msgs = []
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if m:
            d, t, sender, body = m.groups()
            msgs.append({"date": d, "sender": sender.strip(), "text": body})
        elif msgs:
            msgs[-1]["text"] += "\n" + line
    return msgs


def is_candidate(msg):
    t = msg["text"].strip()
    if "<Media omitted>" in t or len(t) < 25:
        return False
    if "chat.whatsapp.com" in t and len(t) < 160:
        return False
    return bool(KEYWORDS.search(t))


EXTRACT_PROMPT = """You extract real estate data from WhatsApp group messages written in ANY language
(Hindi, English, Hinglish, Marathi, Gujarati, etc.). One message can contain 1 property or 20 - output
one object per property. Return ONLY a JSON array.

Object fields:
{{
  "msg_id": int,
  "listing_type": "Sale" | "Rent" | "Requirement" | "Unknown",
  "property_type": "Flat" | "Villa" | "House" | "Plot" | "Shop" | "Office" | "Land" | "Other",
  "bhk": integer or null,
  "area_sqft": integer or null,
  "price_inr": integer or null,
  "price_type": "Total" | "Per sq ft" | "Per month" | null,
  "location": string or null,
  "contact_number": string or null,
  "notes": string or null
}}

Rules:
- Skip greetings, jokes, forwards and anything not about a property sale/rent/requirement.
- Never invent values. Unknown = null.
- Write location and notes in English (transliterate Hindi names), e.g. "नरीमन सिटी" -> "Nariman City".
- Reuse these existing location names exactly if the message means the same place: {known_locations}
- price_inr is a plain integer in rupees: 58 lakh = 5800000, 1.2 crore = 12000000, 15k = 15000.
- A bare number next to a plot size (e.g. "Rate 8100", "6000 with all dues") is a rate per sq ft -> price_type "Per sq ft".
- Plot sizes like 30*50 (feet) -> area_sqft = 1500.
- contact_number: digits only. notes: max 12 short words (facing, corner, registry, floor etc.).

Messages:
{messages}
"""

QUERY_PROMPT = """Convert this real estate question (any language) into a JSON search filter. Return ONLY JSON:
{{
  "listing_type": "Sale" | "Rent" | "Requirement" | null,
  "property_type": "Flat" | "Villa" | "House" | "Plot" | "Shop" | "Office" | "Land" | null,
  "bhk": integer or null,
  "location": string or null,
  "price_type": "Total" | "Per sq ft" | "Per month" | null,
  "min_price_inr": integer or null,
  "max_price_inr": integer or null,
  "min_area_sqft": integer or null,
  "max_area_sqft": integer or null
}}
Rules: location in English transliteration. "50 lakh tak" -> max_price_inr 5000000. "1 bhk" -> bhk 1.
If the user says "rent"/"kiraye" -> listing_type Rent; "sale"/"kharidna"/"bikau" -> Sale. Unknown fields = null.
Question: {q}
"""


def _gen(client, model, prompt):
    return client.models.generate_content(
        model=model, contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0))


def extract_batch(client, model, batch, known_locations, log=lambda m: None):
    payload = "\n\n".join(f"[id={m['id']}] sender={m['sender']}\n{m['text'][:1500]}" for m in batch)
    prompt = EXTRACT_PROMPT.format(known_locations=", ".join(known_locations) or "(none yet)", messages=payload)
    for attempt in range(3):
        try:
            data = json.loads(_gen(client, model, prompt).text)
            return data if isinstance(data, list) else None
        except Exception as e:
            if attempt == 2:
                log(str(e)[:200])
                return None
            time.sleep(10 * (attempt + 1))
    return None


def parse_question(client, model, q):
    data = json.loads(_gen(client, model, QUERY_PROMPT.format(q=q)).text)
    return data[0] if isinstance(data, list) and data else data


def ingest(con, client, model, msgs, db, batch_size=40, max_batches=50, sleep=4,
           keep_source=False, progress=lambda done, total: None, log=lambda m: None):
    """Sirf naye messages Gemini ko bhejta hai (purane skip). Har batch ke baad commit.
    Return: dict(new_listings, new_messages, pending, failed_batches)"""
    todo = []
    for i, m in enumerate(msgs):
        m["id"] = i
        if not is_candidate(m):
            continue
        m["h"] = db.h64("M", m["sender"], m["text"])
        if not db.is_seen(con, m["h"]):
            todo.append(m)
    # ek hi text baar-baar aaye (repost) to ek hi baar bhejo
    uniq, seen_h = [], set()
    for m in todo:
        if m["h"] not in seen_h:
            seen_h.add(m["h"]); uniq.append(m)
    todo = uniq

    n_batches = (len(todo) + batch_size - 1) // batch_size
    run = min(n_batches, max_batches)
    new_listings = new_msgs = failed = 0
    for b in range(run):
        batch = todo[b * batch_size:(b + 1) * batch_size]
        items = extract_batch(client, model, batch, db.top_locations(con), log)
        if items is None:
            failed += 1
            progress(b + 1, run)
            continue
        by_id = {m["id"]: m for m in batch}
        for it in items:
            src = by_id.get(it.get("msg_id"))
            if src and db.insert_listing(con, it, src["sender"], src["date"]):
                new_listings += 1
                if keep_source:
                    db.save_source(con, src["h"], src["text"])
        for m in batch:
            db.mark_seen(con, m["h"])
            new_msgs += 1
        con.commit()
        progress(b + 1, run)
        if b < run - 1:
            time.sleep(sleep)
    return {"new_listings": new_listings, "new_messages": new_msgs,
            "pending_messages": sum(len(todo[b * batch_size:(b + 1) * batch_size]) for b in range(run, n_batches)),
            "failed_batches": failed}
