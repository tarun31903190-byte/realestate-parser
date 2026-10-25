"""Chat parsing + Gemini calls (extraction aur sawal samajhna)."""
import io
import json
import re
import time
import zipfile

import zlib

from google.genai import types

import offline

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
- contact_number: digits only, a 10-digit Indian mobile number. If several numbers, use the one written next to "Contact"/"संपर्क". notes: max 12 short words (facing, corner, registry, floor etc.).
- location = ONLY the colony/township/area name. Put landmarks ("near airport", "behind mall") in notes, not in location.
- If one message lists several plots/flats (different sizes or rates), output a separate object for each one.
- A message that only asks "who has..." / "need..." is listing_type "Requirement".

Examples:
Message: "🏡 *प्लॉट बेचना है*\nNariman city\n22*45\n6000 (with all dues)\nDirect deal\nContact Suresh Soni 98263 43838"
Output: {{"listing_type":"Sale","property_type":"Plot","bhk":null,"area_sqft":990,"price_inr":6000,"price_type":"Per sq ft","location":"Nariman City","contact_number":"9826343838","notes":"direct deal, with all dues"}}
Message: "2 BHK flat for rent Vijay Nagar 15k/month furnished 9755012345"
Output: {{"listing_type":"Rent","property_type":"Flat","bhk":2,"area_sqft":null,"price_inr":15000,"price_type":"Per month","location":"Vijay Nagar","contact_number":"9755012345","notes":"furnished"}}
(In real output every object also has "msg_id".)

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
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0,
                                           max_output_tokens=32768))


def _extract(client, model, batch, known_locations, log):
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
            time.sleep(8 * (attempt + 1))
    return None


def extract_batch(client, model, batch, known_locations, log=lambda m: None):
    """Fail ho (jaise jawab bahut lamba ho kar kat gaya) to batch ko aadha-aadha karke dobara try."""
    items = _extract(client, model, batch, known_locations, log)
    if items is not None or len(batch) < 2:
        return items
    mid = len(batch) // 2
    a = extract_batch(client, model, batch[:mid], known_locations, log)
    b = extract_batch(client, model, batch[mid:], known_locations, log)
    if a is None or b is None:
        return None
    return a + b


STRONG = re.compile(r"(plot|bhk|flat|villa|house|sale|rent|shop|office|प्लॉट|बेचना|किराए|किराये|फ्लैट|मकान|दुकान)", re.I)


def parse_question(client, model, q):
    data = json.loads(_gen(client, model, QUERY_PROMPT.format(q=q)).text)
    return data[0] if isinstance(data, list) and data else data


def _gemini_phase(con, client, model, db, todo, batch_size, max_batches, sleep, keep_source,
                  double_check, progress, log):
    """Messages ko Gemini se padhwao, db mein daalo, aur offline model ko seekhao."""
    n_batches = (len(todo) + batch_size - 1) // batch_size
    run = min(n_batches, max_batches)
    total = min(len(todo), run * batch_size)
    new_listings = done = failed = rechecked = ok_msgs = 0
    resolved = set()
    t0 = time.time()
    for b in range(run):
        batch = todo[b * batch_size:(b + 1) * batch_size]
        items = extract_batch(client, model, batch, db.top_locations(con), log)
        if items is None:
            failed += 1
        else:
            if double_check:  # jin messages se kuch nahi nikla par wo property jaise lagte hain, dobara try
                got = {it.get("msg_id") for it in items}
                miss = [m for m in batch if m["id"] not in got and STRONG.search(m["text"]) and re.search(r"\d", m["text"])]
                if miss:
                    rechecked += len(miss)
                    again = extract_batch(client, model, miss, db.top_locations(con), log)
                    items = items + (again or [])
            by_id = {m["id"]: m for m in batch}
            per_msg = {}
            for it in items:
                src = by_id.get(it.get("msg_id"))
                if src is None:
                    continue
                per_msg.setdefault(src["id"], []).append(it)
                if db.insert_listing(con, it, src["sender"], src["date"], src["h"], keep_source):
                    new_listings += 1
                    if keep_source:
                        db.save_source(con, src["h"], src["text"])
            for mid, its in per_msg.items():          # student ko seekhao
                m = by_id[mid]
                offline.learn(con, m["text"], its, m["sender"], m["date"], m["h"])
            for m in batch:
                db.mark_seen(con, m["h"])
                resolved.add(m["h"])
            ok_msgs += len(batch)
            con.commit()
        done += len(batch)
        el = time.time() - t0
        progress({"phase": "Gemini", "pct": done / max(total, 1), "done": done, "total": total,
                  "listings": new_listings, "eta_s": el / done * (total - done) if done else 0})
        if b < run - 1:
            time.sleep(sleep)
    return {"new_listings": new_listings, "ok_msgs": ok_msgs, "failed": failed, "rechecked": rechecked,
            "left": max(0, len(todo) - run * batch_size), "resolved": resolved}


def ingest(con, client, model, msgs, db, mode="hybrid", batch_size=40, max_batches=50, sleep=4,
           keep_source=False, double_check=True, progress=lambda info: None, log=lambda m: None):
    """mode: 'hybrid' (pehle offline, hard wale Gemini se) | 'offline' (sirf offline, hard jama) | 'gemini'."""
    todo, seen_h = [], set()
    for i, m in enumerate(msgs):
        m["id"] = i
        if not is_candidate(m):
            continue
        m["h"] = db.h64("M", m["sender"], m["text"])
        if m["h"] in seen_h or db.is_seen(con, m["h"]):
            continue
        seen_h.add(m["h"])
        todo.append(m)

    res = {"offline_listings": 0, "offline_messages": 0, "junk": 0, "hard_saved": 0,
           "new_listings": 0, "new_messages": 0, "pending_messages": 0, "failed_batches": 0, "rechecked": 0}
    remaining = todo
    if mode != "gemini":
        model_o = offline.OfflineModel(con)
        remaining = []
        for k, m in enumerate(todo):
            r = model_o.parse(m["text"], m["sender"])
            if r[0] == "ok":
                added = 0
                for it in r[1]:
                    if db.insert_listing(con, it, m["sender"], m["date"], m["h"], keep_source, offline=True, verify=r[2]):
                        added += 1
                if added and keep_source:
                    db.save_source(con, m["h"], m["text"])
                db.mark_seen(con, m["h"])
                res["offline_listings"] += added
                res["offline_messages"] += 1
            elif r[0] == "junk":
                db.mark_seen(con, m["h"])
                res["junk"] += 1
            else:
                m["why"] = r[1]
                remaining.append(m)
            if k % 100 == 0 or k == len(todo) - 1:
                progress({"phase": "Offline", "pct": (k + 1) / max(len(todo), 1), "done": k + 1,
                          "total": len(todo), "listings": res["offline_listings"], "eta_s": 0})
        con.commit()
        if mode == "offline":
            for m in remaining:
                z = zlib.compress(m["text"].encode("utf-8"), 9)
                con.execute("INSERT OR REPLACE INTO hard(h,sender,d,why,z) VALUES(?,?,?,?,?)",
                            (m["h"], m["sender"], m["date"], m.get("why", ""), z))
                db.mark_seen(con, m["h"])
            res["hard_saved"] = len(remaining)
            con.commit()
            remaining = []

    if remaining:
        g = _gemini_phase(con, client, model, db, remaining, batch_size, max_batches, sleep,
                          keep_source, double_check, progress, log)
        res.update(new_listings=g["new_listings"], new_messages=g["ok_msgs"], pending_messages=g["left"],
                   failed_batches=g["failed"], rechecked=g["rechecked"])
    return res


def solve_hard(con, client, model, db, batch_size=40, max_batches=50, sleep=4, keep_source=False,
               double_check=True, progress=lambda info: None, log=lambda m: None):
    """'hard' list ke saare messages Gemini se padhwao; solve hue to list se hata do, offline model seekhe."""
    rows = con.execute("SELECT h, sender, d, z FROM hard").fetchall()
    todo = [{"id": i, "h": h, "sender": s, "date": d, "text": zlib.decompress(z).decode("utf-8")}
            for i, (h, s, d, z) in enumerate(rows)]
    if not todo:
        return {"new_listings": 0, "solved": 0, "left": 0, "failed_batches": 0}
    g = _gemini_phase(con, client, model, db, todo, batch_size, max_batches, sleep, keep_source,
                      double_check, progress, log)
    for h in g["resolved"]:
        con.execute("DELETE FROM hard WHERE h=?", (h,))
    con.commit()
    return {"new_listings": g["new_listings"], "solved": len(g["resolved"]),
            "left": con.execute("SELECT COUNT(*) FROM hard").fetchone()[0], "failed_batches": g["failed"]}


def export_hard(con):
    """Hard cases JSONL (Claude/developer ko dikhane ke liye taaki offline model ka code behtar ho)."""
    out = []
    for h, s, d, why, z in con.execute("SELECT h, sender, d, why, z FROM hard"):
        out.append(json.dumps({"why": why, "sender": s, "date": d, "text": zlib.decompress(z).decode("utf-8")},
                              ensure_ascii=False))
    return "\n".join(out)
