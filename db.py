"""Compact real-estate database (SQLite) with dictionary encoding.

Har baar-baar aane wali cheez ek chhote integer se store hoti hai:
  location name -> loc.id      | sender name -> sender.id
  notes ke har word -> word.id (varint bytes)
  type / BHK / price / area / date / phone -> sirf integers
"""
import difflib
import hashlib
import re
import sqlite3
import zlib

LT = {"Unknown": 0, "Sale": 1, "Rent": 2, "Requirement": 3}
PT = {"Unknown": 0, "Flat": 1, "Villa": 2, "House": 3, "Plot": 4,
      "Shop": 5, "Office": 6, "Land": 7, "Other": 8}
PRT = {"Total": 1, "Per sq ft": 2, "Per month": 3}
LT_R = {v: k for k, v in LT.items()}
PT_R = {v: k for k, v in PT.items()}
PRT_R = {v: k for k, v in PRT.items()}

SCHEMA = """
CREATE TABLE IF NOT EXISTS loc(id INTEGER PRIMARY KEY, name TEXT NOT NULL, key TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS sender(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS word(id INTEGER PRIMARY KEY, w TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS seen(h INTEGER PRIMARY KEY) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS src(h INTEGER PRIMARY KEY, z BLOB);
CREATE TABLE IF NOT EXISTS listing(
  id INTEGER PRIMARY KEY,
  lt INTEGER, pt INTEGER, bhk INTEGER, area INTEGER,
  price INTEGER, prt INTEGER, loc INTEGER, phone INTEGER,
  snd INTEGER, d INTEGER, notes BLOB, fl INTEGER DEFAULT 0, sh INTEGER);
CREATE TABLE IF NOT EXISTS alias(phrase TEXT, loc INTEGER, n INTEGER DEFAULT 1, PRIMARY KEY(phrase, loc)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS vote(kind TEXT, tok TEXT, label INTEGER, n INTEGER DEFAULT 1, PRIMARY KEY(kind, tok, label)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS hard(h INTEGER PRIMARY KEY, sender TEXT, d TEXT, why TEXT, z BLOB);
CREATE TABLE IF NOT EXISTS teach(id INTEGER PRIMARY KEY, h INTEGER UNIQUE, z BLOB);
CREATE INDEX IF NOT EXISTS ix_listing_loc ON listing(loc, bhk);
"""


def connect(path):
    con = sqlite3.connect(path, check_same_thread=False)
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(listing)")}
    for c in ("fl INTEGER DEFAULT 0", "sh INTEGER"):  # purani backup file ko upgrade karo
        if c.split()[0] not in cols:
            con.execute(f"ALTER TABLE listing ADD COLUMN {c}")
    return con


# ---------- helpers ----------
def norm_key(s):
    return re.sub(r"[^\w]+", "", (s or "").lower())


def h64(*parts):
    raw = "\x1f".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big", signed=True)


def to_int(v):
    try:
        if v is None or v == "":
            return 0
        if isinstance(v, str):
            v = re.sub(r"[^\d.]", "", v)
            if not v:
                return 0
        return int(float(v))
    except Exception:
        return 0


def norm_phone(v):
    d = re.sub(r"\D", "", str(v or ""))
    if len(d) >= 10:
        d = d[-10:]
        if d[0] in "6789":
            return int(d)
    return 0


def parse_date(s):
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", s or "")
    if not m:
        return 0
    dd, mm, yy = map(int, m.groups())
    if yy < 100:
        yy += 2000
    return yy * 10000 + mm * 100 + dd


def fmt_date(d):
    return f"{d % 100:02d}/{(d // 100) % 100:02d}/{d // 10000}" if d else ""


def fmt_price(p, prt):
    if not p:
        return ""
    if p >= 10_000_000:
        s = f"{p / 10_000_000:g} Cr"
    elif p >= 100_000:
        s = f"{p / 100_000:g} Lakh"
    else:
        s = f"{p:,}"
    suffix = {2: " /sq ft", 3: " /month"}.get(prt, "")
    return f"₹{s}{suffix}"


# ---------- word dictionary for notes ----------
def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _unvarint(blob):
    n = shift = 0
    out = []
    for b in blob:
        n |= (b & 0x7F) << shift
        if b & 0x80:
            shift += 7
        else:
            out.append(n)
            n = shift = 0
    return out


def encode_notes(con, text):
    words = re.findall(r"\S+", (text or "")[:300])
    if not words:
        return None
    ids = []
    for w in words:
        w = w.lower()
        row = con.execute("SELECT id FROM word WHERE w=?", (w,)).fetchone()
        if row is None:
            row = (con.execute("INSERT INTO word(w) VALUES(?)", (w,)).lastrowid,)
        ids.append(row[0])
    return b"".join(_varint(i) for i in ids)


def decode_notes(con, blob):
    if not blob:
        return ""
    ids = _unvarint(blob)
    out = []
    for i in ids:
        r = con.execute("SELECT w FROM word WHERE id=?", (i,)).fetchone()
        out.append(r[0] if r else "?")
    return " ".join(out)


# ---------- dictionaries ----------
_LANDMARK = re.compile(r"\s*(?:,\s*)?\b(near|nearby|behind|opposite|opp\.?|beside|next to|paas)\b", re.I)


def split_location(name):
    """'RR Residency, Near Super Corridor' -> ('RR Residency', 'Near Super Corridor')."""
    name = (name or "").strip()
    m = _LANDMARK.search(name)
    if m and m.start() > 0:
        return name[:m.start()].strip(" ,-"), name[m.start():].strip(" ,-")
    return name, ""


def loc_id(con, name):
    key = norm_key(name)
    if not key:
        return 0
    r = con.execute("SELECT id FROM loc WHERE key=?", (key,)).fetchone()
    if r:
        return r[0]
    # milta-julta naam (spelling ka farak) ho to wahi location use karo
    keys = [k for (k,) in con.execute("SELECT key FROM loc")]
    for k in difflib.get_close_matches(key, keys, n=3, cutoff=0.88):
        if re.findall(r"\d+", k) == re.findall(r"\d+", key):
            return con.execute("SELECT id FROM loc WHERE key=?", (k,)).fetchone()[0]
    return con.execute("INSERT INTO loc(name,key) VALUES(?,?)", (name.strip().title()[:60], key)).lastrowid


def sanity(lt, pt, bhk, area, price, prt):
    """Gemini ke numbers ki jaanch. Return (bhk, area, price, prt, flag). flag=1 matlab 'ek baar check karo'."""
    fl = 0
    if bhk < 0 or bhk > 20:
        bhk, fl = 0, 1
    if area and (area < 50 or area > 5_000_000):
        area, fl = 0, 1
    if price:
        if prt == 0:
            prt, fl = (3 if lt == 2 else (1 if price >= 100_000 else 2)), 1
        if prt == 1 and price < 100_000:      # 'total' bataya par 1 lakh se kam -> rate/rent hoga
            prt, fl = (3 if lt == 2 else 2), 1
        elif prt == 2 and price > 500_000:    # per sq ft rate 5 lakh se zyada nahi hota
            prt, fl = 1, 1
        elif prt == 3 and price > 5_000_000:
            prt, fl = 1, 1
        if prt == 2 and price < 100:
            price, prt, fl = 0, 0, 1
    return bhk, area, price, prt, fl


def sender_id(con, name):
    name = (name or "").strip()[:60]
    if not name:
        return 0
    r = con.execute("SELECT id FROM sender WHERE name=?", (name,)).fetchone()
    return r[0] if r else con.execute("INSERT INTO sender(name) VALUES(?)", (name,)).lastrowid


def top_locations(con, n=150):
    rows = con.execute(
        "SELECT loc.name FROM listing JOIN loc ON loc.id=listing.loc "
        "GROUP BY loc.id ORDER BY COUNT(*) DESC LIMIT ?", (n,)).fetchall()
    return [r[0] for r in rows]


# ---------- seen / dedupe ----------
def is_seen(con, h):
    return con.execute("SELECT 1 FROM seen WHERE h=?", (h,)).fetchone() is not None


def mark_seen(con, h):
    con.execute("INSERT OR IGNORE INTO seen(h) VALUES(?)", (h,))


def save_source(con, h, text):
    con.execute("INSERT OR IGNORE INTO src(h,z) VALUES(?,?)", (h, zlib.compress(text.encode("utf-8"), 9)))


# ---------- insert ----------
def insert_listing(con, item, sender, date_str, src_hash=None, keep_source=False, offline=False, verify=False):
    """item = Gemini ka JSON object. True return karta hai agar nayi row save hui."""
    lt = LT.get(item.get("listing_type"), 0)
    pt = PT.get(item.get("property_type"), 0)
    bhk = to_int(item.get("bhk"))
    area = to_int(item.get("area_sqft"))
    price = to_int(item.get("price_inr"))
    prt = PRT.get(item.get("price_type"), 0) if price else 0
    bhk, area, price, prt, fl = sanity(lt, pt, bhk, area, price, prt)
    main_loc, landmark = split_location(item.get("location") or "")
    loc = loc_id(con, main_loc)
    notes_text = " ".join(x for x in (landmark, item.get("notes") or "") if x)
    phone = norm_phone(item.get("contact_number"))
    if not phone:
        phone = norm_phone(sender)
        if not phone:
            fl = 1
    if not loc:
        fl = 1
    if not (price or area):
        fl = 1
    if verify:
        fl |= 1
    if not (loc or area or price or bhk):
        return False
    if offline:
        fl |= 2          # bit 2 = offline model ne nikala
    key = h64("L", lt, pt, bhk, area, price, prt, loc, phone)
    if is_seen(con, key):
        return False
    mark_seen(con, key)
    con.execute(
        "INSERT INTO listing(lt,pt,bhk,area,price,prt,loc,phone,snd,d,notes,fl,sh) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (lt, pt, bhk, area, price, prt, loc, phone, sender_id(con, sender),
         parse_date(date_str), encode_notes(con, notes_text), fl, src_hash if keep_source else None))
    return True


# ---------- search ----------
def resolve_locations(con, text, cutoff=0.78):
    key = norm_key(text)
    if not key:
        return []
    rows = con.execute("SELECT id, key FROM loc").fetchall()

    def starts(k):  # "colony12" ko "colony125" se na milao, par "rrresidencynear..." se milao
        return k.startswith(key) and not (key[-1].isdigit() and len(k) > len(key) and k[len(key)].isdigit())

    ids = {i for i, k in rows if k == key or starts(k)}
    if not ids:
        ids = {i for i, k in rows if key in k or (len(k) > 3 and k in key)}
    if not ids:
        keys = [k for _, k in rows]
        close = set(difflib.get_close_matches(key, keys, n=5, cutoff=cutoff))
        ids = {i for i, k in rows if k in close}
    return list(ids)


def search(con, f, limit=25):
    where, args = [], []
    if f.get("listing_type") in LT and f["listing_type"] != "Unknown":
        where.append("lt=?"); args.append(LT[f["listing_type"]])
    if f.get("property_type") in PT and f["property_type"] != "Unknown":
        where.append("pt=?"); args.append(PT[f["property_type"]])
    if to_int(f.get("bhk")):
        where.append("bhk=?"); args.append(to_int(f["bhk"]))
    if f.get("location"):
        ids = resolve_locations(con, f["location"])
        if not ids:
            return notes_search(con, f, limit), True
        where.append(f"loc IN ({','.join('?' * len(ids))})"); args += ids
    if f.get("price_type") in PRT:
        where.append("prt=?"); args.append(PRT[f["price_type"]])
    if to_int(f.get("min_price_inr")):
        where.append("price>=?"); args.append(to_int(f["min_price_inr"]))
    if to_int(f.get("max_price_inr")):
        where.append("price>0 AND price<=?"); args.append(to_int(f["max_price_inr"]))
    if to_int(f.get("min_area_sqft")):
        where.append("area>=?"); args.append(to_int(f["min_area_sqft"]))
    if to_int(f.get("max_area_sqft")):
        where.append("area>0 AND area<=?"); args.append(to_int(f["max_area_sqft"]))
    sql = ("SELECT listing.id, lt, pt, bhk, area, price, prt, loc.name, phone, sender.name, d, notes, fl "
           "FROM listing LEFT JOIN loc ON loc.id=listing.loc LEFT JOIN sender ON sender.id=listing.snd")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY d DESC, listing.id DESC LIMIT ?"
    rows = con.execute(sql, args + [limit]).fetchall()
    out = []
    for r in rows:
        out.append({
            "Type": LT_R.get(r[1], ""), "Property": PT_R.get(r[2], ""),
            "BHK": r[3] or "", "Area (sq ft)": r[4] or "",
            "Price": fmt_price(r[5], r[6]), "Location": r[7] or "",
            "Phone": r[8] or "", "Posted by": r[9] or "",
            "Date": fmt_date(r[10]), "Notes": decode_notes(con, r[11]),
            "Check": "⚠️ verify" if (r[12] or 0) & 1 else "",
        })
    return out, True


def notes_search(con, f, limit=25, scan_limit=200_000):
    """Location dictionary mein naam na mile (jaise sirf landmark ho) to notes ke words mein dhoondo."""
    toks = [t.lower() for t in re.findall(r"\w+", f.get("location") or "")]
    wid = []
    for t in toks:
        r = con.execute("SELECT id FROM word WHERE w=? OR w LIKE ?", (t, t + "%")).fetchall()
        if not r:
            return []
        wid.append({x[0] for x in r})
    g = dict(f); g["location"] = None
    rows, _ = search(con, g, limit=scan_limit)
    out = []
    for r in rows:
        words = set(re.findall(r"\w+", (r["Notes"] or "").lower()))
        if all(any(w.startswith(t) for w in words) for t in toks):
            out.append(r)
        if len(out) >= limit:
            break
    return out


def random_audit(con, n=10):
    """Random listings + unka original message (sirf jinke liye 'original save' on tha)."""
    rows = con.execute(
        "SELECT listing.id, lt, pt, bhk, area, price, prt, loc.name, phone, notes, fl, src.z "
        "FROM listing LEFT JOIN loc ON loc.id=listing.loc JOIN src ON src.h=listing.sh "
        "ORDER BY RANDOM() LIMIT ?", (n,)).fetchall()
    out = []
    for r in rows:
        out.append({"Type": LT_R.get(r[1], ""), "Property": PT_R.get(r[2], ""), "BHK": r[3] or "",
                    "Area (sq ft)": r[4] or "", "Price": fmt_price(r[5], r[6]), "Location": r[7] or "",
                    "Phone": r[8] or "", "Notes": decode_notes(con, r[9]), "Flag": bool((r[10] or 0) & 1), "Source": "offline" if (r[10] or 0) & 2 else "Gemini",
                    "original": zlib.decompress(r[11]).decode("utf-8")})
    return out


def stats(con):
    q = lambda s: con.execute(s).fetchone()[0]
    return {"listings": q("SELECT COUNT(*) FROM listing"),
            "locations": q("SELECT COUNT(*) FROM loc"),
            "words": q("SELECT COUNT(*) FROM word"),
            "flagged": q("SELECT COUNT(*) FROM listing WHERE fl & 1"),
            "offline_rows": q("SELECT COUNT(*) FROM listing WHERE fl & 2"),
            "hard": q("SELECT COUNT(*) FROM hard"),
            "with_source": q("SELECT COUNT(*) FROM listing WHERE sh IS NOT NULL"),
            "messages_done": q("SELECT COUNT(*) FROM seen")}
