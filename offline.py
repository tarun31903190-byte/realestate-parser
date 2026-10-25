"""Offline model: bina internet/API ke message padhta hai.

Teacher = Gemini, Student = ye model. Student sirf wahi message nikalta hai jisme wo PAKKA hai
(ek hi location, ek hi type, size/price saaf). Baaki 'hard' list mein jaate hain, jise Gemini
solve karta hai, aur uske jawab se student naye shabd/location-naam seekhta hai (learn()).
"""
import difflib
import json
import re
import zlib

import db

WCH = r"\wऀ-ॿ"
SQFT = r"(?:sq\.?\s*ft\.?s?|sqft|sft|square\s*fee?t|square\s*foot|sq\.?\s*feet|वर्ग\s*फ[ी़]*ट|वर्गफुट)"

RE_PHONE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?([6-9]\d{4}[\s-]?\d{5})(?!\d)")
RE_DIM = re.compile(r"(?<!\d)(\d{1,3})\s*(?:[*xX×]|\bby\b)\s*(\d{1,3})(?!\d)(?:\s*=\s*(\d{3,6}))?")
RE_AREA_LIST = re.compile(r"((?:\d{3,5}\s*(?:,|&|/|\band\b|और)\s*)*\d{3,5})\s*" + SQFT, re.I)
RE_GAJ = re.compile(r"(\d{2,4})\s*(?:gaj|गज|sq\.?\s*y(?:ar)?d|sqyd)", re.I)
RE_CR = re.compile(r"(\d+(?:\.\d+)?)\s*(?:crore|cr\b|करोड़|करोड)", re.I)
RE_LAKH = re.compile(r"(\d+(?:\.\d+)?)\s*(?:lakh|lakhs|lacs?|लाख)", re.I)
RE_BHK = re.compile(r"(\d)\s*(?:bhk|बीएचके|b\.h\.k)", re.I)
RE_RATE = [  # per sq ft
    re.compile(r"(?:₹|rs\.?|inr)?\s*(\d[\d,]{2,6})\s*(?:/-)?\s*(?:/\s*|per\s*|प्रति\s*)(?:sq|sft|sqft|वर्ग|gaj|गज)", re.I),
    re.compile(r"(?<![\d,])(\d[\d,]{2,5})\s*\(?\s*with\s*all\s*dues", re.I),
    re.compile(r"\b(?:rate|रेट|भाव)\s*(?:only|:|-|है)?\s*(?:₹|rs\.?)?\s*(\d[\d,]{2,5})(?![\d,]*\s*(?:lakh|lac|cr|लाख|करोड))", re.I),
]
RE_RENT = re.compile(r"(?:₹|rs\.?)?\s*(\d[\d,]*(?:\.\d+)?)\s*(k)?\s*(?:/|per\s*)?\s*(?:month|pm\b|p\.m\.|महीना|माह|महीने)", re.I)
RE_TOTAL_RS = re.compile(r"(?:price|कीमत|total)\s*[:\-]?\s*(?:only\s*)?(?:₹|rs\.?)\s*(\d[\d,]{5,})", re.I)

SALE = re.compile(r"\bfor\s*sale\b|\bsale\b|\bsell(?:ing)?\b|\bbikau\b|\bbechna\b|बेचना|बेचने|बिक्री|बिकाऊ|विक्रय|बेचनी", re.I)
RENT = re.compile(r"\bfor\s*rent\b|\brent(?:al)?\b|\bkiraye\b|\bkiraya\b|किराए|किराये|किराया|\blease\b", re.I)
REQ = re.compile(r"\brequired\b|\brequirement\b|\bwanted\b|\bneeded?\b|चाहिए|चाहिये|आवश्यकता|\bjarurat\b|\bchahiye\b", re.I)
PT_RE = {
    4: re.compile(r"\bplots?\b|प्लॉट|प्लाट|भूखंड", re.I),
    7: re.compile(r"\bland\b|जमीन|ज़मीन", re.I),
    1: re.compile(r"\bflats?\b|फ्लैट|\bapartment\b", re.I),
    2: re.compile(r"\bvilla\b|\bvillas\b|विला", re.I),
    3: re.compile(r"\bhouse\b|\bbungalow\b|मकान|कोठी|\bkothi\b|\brow\s*house\b", re.I),
    5: re.compile(r"\bshop\b|दुकान|\bshowroom\b", re.I),
    6: re.compile(r"\boffice\b|ऑफिस|कार्यालय", re.I),
}
NOTE_RE = re.compile(r"(east|west|north|south)\s*(?:facing|face)|garden\s*(?:facing|face)|\bcorner\b|\bjodi\b|"
                     r"direct\s*deal|\bregistry\b|all\s*dues(?:\s*(?:included|paid))?|with\s*all\s*dues|road\s*touch", re.I)
LANDMARK_BEFORE = re.compile(r"\b(near|nearby|behind|opposite|opp\.?|beside|next\s*to|by)\s*$", re.I)
LANDMARK_AFTER = re.compile(r"^\s*(?:के\s*पास|ke\s*paas|ke\s*pass|के\s*सामने|के\s*पीछे)", re.I)
LT_N = {"Sale": 1, "Rent": 2, "Requirement": 3}


def leftover_numbers(t):
    """Message se samjhe hue hisse hata do; jo 3+ digit number bache wo 'unexplained' hain."""
    x = t
    for rx in (RE_PHONE, RE_AREA_LIST, RE_GAJ, RE_DIM, RE_CR, RE_LAKH, RE_RENT, RE_TOTAL_RS, RE_BHK, *RE_RATE):
        x = rx.sub(" ", x)
    x = re.sub(r"\d{1,2}\s*(?:ft|feet|fit|फीट)\b", " ", x, flags=re.I)
    return re.findall(r"(?<![\w.])\d{3,7}(?![\w])", x)


def norm_text(t):
    t = t.replace("‎", "").lower()
    return " " + re.sub(rf"[^{WCH}]+", " ", t).strip() + " "


def _num(s):
    return int(float(s.replace(",", "")))


class OfflineModel:
    def __init__(self, con):
        self.con = con
        self._load()

    def _load(self):
        con = self.con
        self.name_of = {}
        for (name,) in con.execute("SELECT name FROM loc"):
            main = db.split_location(name)[0]
            ph = norm_text(main).strip()
            if len(ph) >= 4:
                self.name_of.setdefault(ph, main)
        for ph, lid, n in con.execute("SELECT phrase, loc, n FROM alias WHERE n>=2"):
            r = con.execute("SELECT name FROM loc WHERE id=?", (lid,)).fetchone()
            if r and len(ph) >= 3:
                self.name_of[ph] = db.split_location(r[0])[0]
        phrases = sorted(self.name_of, key=len, reverse=True)
        self.loc_re = re.compile(rf"(?<![{WCH}])(" + "|".join(re.escape(p) for p in phrases) + rf")(?![{WCH}])") if phrases else None
        self.votes = {}
        for kind, tok, label, n in con.execute("SELECT kind,tok,label,n FROM vote"):
            self.votes.setdefault((kind, tok), {})[label] = n

    # ---------- learned classifier ----------
    def _vote(self, kind, text):
        toks = set(re.findall(r"[a-zA-Zऀ-ॿ]{3,}", text.lower()))
        score = {}
        for t in toks:
            d = self.votes.get((kind, t))
            if not d:
                continue
            tot = sum(d.values())
            if tot < 3:
                continue
            for lab, n in d.items():
                if n / tot >= 0.85:
                    score[lab] = score.get(lab, 0) + 1
        if not score:
            return None
        top = sorted(score.items(), key=lambda x: -x[1])
        if len(top) > 1 and top[0][1] < 2 * top[1][1]:
            return None
        return top[0][0]

    # ---------- main ----------
    def parse(self, text, sender=""):
        """Return ('ok', [items], verify_flag) | ('hard', reason) | ('junk', '')."""
        t = text.replace("‎", "")
        low = t.lower()

        # --- numbers ---
        sizes = []
        for m in RE_AREA_LIST.finditer(t):
            sizes += [_num(x) for x in re.findall(r"\d{3,5}", m.group(1))]
        for m in RE_GAJ.finditer(t):
            sizes.append(int(m.group(1)) * 9)
        dims = []
        for m in RE_DIM.finditer(t):
            a, b, eq = m.groups()
            prod = int(a) * int(b)
            if eq and abs(int(eq) - prod) <= max(10, prod * 0.03):
                dims.append(int(eq))
            elif 100 <= prod <= 20000 and int(a) >= 8 and int(b) >= 8:
                dims.append(prod)
        sizes = list(dict.fromkeys(sizes)) or list(dict.fromkeys(dims))
        bhk = RE_BHK.search(t)
        bhk_v = int(bhk.group(1)) if bhk else 0

        prices = []   # (value, type)
        for m in RE_CR.finditer(t):
            prices.append((int(float(m.group(1)) * 10_000_000), "Total"))
        for m in RE_LAKH.finditer(t):
            prices.append((int(float(m.group(1)) * 100_000), "Total"))
        for m in RE_TOTAL_RS.finditer(t):
            prices.append((_num(m.group(1)), "Total"))
        for m in RE_RENT.finditer(t):
            v = float(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1)
            prices.append((int(v), "Per month"))
        if not prices:
            for rx in RE_RATE:
                for m in rx.finditer(t):
                    v = _num(m.group(1))
                    if 300 <= v <= 200_000:
                        prices.append((v, "Per sq ft"))
                if prices:
                    break
        if len({p for p, _ in prices}) > 1:
            return ("hard", "kai alag price")
        price, ptype = prices[0] if prices else (0, None)

        phones = [re.sub(r"\D", "", m.group(1))[-10:] for m in RE_PHONE.finditer(t)]
        if not (sizes or bhk_v or price):
            return ("junk", "") if not phones else ("hard", "size/price nahi mila")
        if len(sizes) > 1 and price and len(prices) == 1 and ptype != "Per sq ft":
            return ("hard", "kai size, ek total price")
        if len(sizes) > 4:
            return ("hard", "bahut saare size")

        # --- listing type / property type ---
        verify = False
        flags = [k for k, rx in (("Sale", SALE), ("Rent", RENT), ("Requirement", REQ)) if rx.search(t)]
        lt = None
        if len(flags) == 1:
            lt = flags[0]
        elif len(flags) > 1:
            return ("hard", "sale/rent/requirement ek saath")
        else:
            v = self._vote("lt", t)
            if v:
                lt, verify = db.LT_R.get(v), True
        if not lt:
            return ("hard", "type samajh nahi aaya")
        if lt == "Sale" and ptype == "Per month":
            return ("hard", "sale par monthly price")
        pts = [k for k, rx in PT_RE.items() if rx.search(t)]
        if bhk_v and not pts:
            pts = [1]
        if 4 in pts and 7 in pts:
            pts.remove(7)
        if len(pts) == 1:
            pt = pts[0]
        elif not pts:
            v = self._vote("pt", t)
            if not v:
                return ("hard", "property type nahi mila")
            pt, verify = v, True
        else:
            return ("hard", "kai property type")
        if len(re.findall(r"for\s*sale|बेचना|for\s*rent|बिक्री", low)) > 1:
            return ("hard", "ek message mein kai listing")

        left = leftover_numbers(t)
        if left and not price:
            return ("hard", f"samjha nahi hua number: {left[0]}")
        if left:
            verify = True

        # --- location ---
        if not self.loc_re:
            return ("hard", "location dictionary khaali")
        n = norm_text(t)
        found = {}
        for m in self.loc_re.finditer(n):
            before, after = n[:m.start()], n[m.end():]
            if LANDMARK_BEFORE.search(before) or LANDMARK_AFTER.search(after):
                continue
            found[m.group(1)] = self.name_of[m.group(1)]
        names = set(found.values())
        if len(names) == 0:
            return ("hard", "location pehchana nahi")
        if len(names) > 1:
            return ("hard", "kai location")
        location = names.pop()

        # --- items ---
        notes = " ".join(dict.fromkeys(x.group(0).lower() for x in NOTE_RE.finditer(t)))[:120]
        phone = phones[0] if phones else ""
        base = {"listing_type": lt, "property_type": db.PT_R[pt], "bhk": bhk_v or None, "price_inr": price or None,
                "price_type": ptype, "location": location, "contact_number": phone, "notes": notes}
        items = []
        for sz in (sizes or [None]):
            it = dict(base); it["area_sqft"] = sz
            items.append(it)
        return ("ok", items, verify)


# ---------- learning from Gemini (teacher) ----------
def _lines(text):
    out = []
    for ln in text.splitlines():
        c = re.sub(rf"[^{WCH}\s&.\-]", " ", ln)
        c = re.sub(r"\s+", " ", c).strip(" .-&")
        if 3 <= len(c) <= 40:
            out.append(c)
    return out


def learn(con, text, items, sender="", date_str="", h=None):
    """Gemini ke nikale items se student ko seekhao."""
    if not items:
        return
    lts = {it.get("listing_type") for it in items}
    pts = {it.get("property_type") for it in items}
    toks = set(re.findall(r"[a-zA-Zऀ-ॿ]{3,}", text.lower()))
    for kind, vals, table in (("lt", lts, db.LT), ("pt", pts, db.PT)):
        if len(vals) == 1:
            lab = table.get(next(iter(vals)), 0)
            if lab:
                for tk in toks:
                    con.execute("INSERT INTO vote(kind,tok,label,n) VALUES(?,?,?,1) "
                                "ON CONFLICT(kind,tok,label) DO UPDATE SET n=n+1", (kind, tk, lab))
    # location alias: line jo dictionary se match nahi hui par Gemini ne location bataya
    locs = {db.split_location(it.get("location") or "")[0] for it in items if it.get("location")}
    if len(locs) == 1:
        loc = next(iter(locs))
        lid = db.loc_id(con, loc)
        want = norm_text(loc).strip()
        model_phrases = {norm_text(db.split_location(n)[0]).strip() for (n,) in con.execute("SELECT name FROM loc")}
        cands = []
        for ln in _lines(text):
            ph = norm_text(ln).strip()
            if ph == want or ph in model_phrases:
                cands = []          # pehle se dictionary mein hai
                break
            if SALE.search(ln) or RENT.search(ln) or REQ.search(ln) or any(r.search(ln) for r in PT_RE.values()):
                continue
            if re.search(r"contact|संपर्क|call|trusted|real estate|construction|sq|price|rate|dues|deal|facing", ln, re.I):
                continue
            if difflib.SequenceMatcher(None, ph, want).ratio() >= 0.75 or re.search(r"[ऀ-ॿ]", ln):
                cands.append(ph)
        if len(cands) == 1:
            con.execute("INSERT INTO alias(phrase,loc,n) VALUES(?,?,1) "
                        "ON CONFLICT(phrase,loc) DO UPDATE SET n=n+1", (cands[0], lid))
    if h is not None:
        z = zlib.compress(json.dumps({"t": text, "s": sender, "d": date_str, "i": items}, ensure_ascii=False).encode("utf-8"), 9)
        con.execute("INSERT OR REPLACE INTO teach(h,z) VALUES(?,?)", (h, z))
        con.execute("DELETE FROM teach WHERE id NOT IN (SELECT id FROM teach ORDER BY id DESC LIMIT 3000)")


def evaluate(con):
    """Gemini ke purane jawabon par offline model ko test karo: coverage aur sahi-pan."""
    model = OfflineModel(con)
    n = covered = correct = 0
    for (z,) in con.execute("SELECT z FROM teach"):
        ex = json.loads(zlib.decompress(z).decode("utf-8"))
        n += 1
        res = model.parse(ex["t"], ex.get("s", ""))
        if res[0] != "ok":
            continue
        covered += 1
        key = lambda it: (db.LT.get(it.get("listing_type"), 0), db.PT.get(it.get("property_type"), 0),
                          db.to_int(it.get("area_sqft")), db.to_int(it.get("price_inr")),
                          db.norm_key(db.split_location(it.get("location") or "")[0]))
        if sorted(map(key, res[1])) == sorted(map(key, ex["i"])):
            correct += 1
    return {"examples": n, "covered": covered, "correct": correct}
