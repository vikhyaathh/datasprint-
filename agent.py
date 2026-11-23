"""Procurement Desk agent: deterministic implementation of Acme's playbook.

Usage:
    python agent.py validate      # score against the 40 worked examples
    python agent.py predict       # write submission.csv for threads.csv
    python agent.py explain T0001 # show the reasoning trace for one thread
"""
import csv
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
# playbook.zip may unzip as data/playbook/playbook/... or data/playbook/...
PB = DATA / "playbook" / "playbook" if (DATA / "playbook" / "playbook").exists() else DATA / "playbook"

# --------------------------------------------------------------------------- playbook loading

def money(s):
    return float(s.replace("₹", "").replace(",", "").strip())


def load_categories():
    cats = {}
    for f in (PB / "categories").glob("*.md"):
        t = f.read_text(encoding="utf-8")
        g = lambda k: re.search(rf"\*\*{k}:\*\*\s*(.+)", t).group(1).strip()
        c = {
            "unit": g("Normalized unit"),
            "gst": float(g("GST rate").rstrip("%")) / 100,
            "target": money(g("Target price")),
            "walkaway": money(g("Walk-away price")),
            "R": int(g(r"Negotiation rounds \(R\)")),
            "beta": float(g(r"Concession exponent \(β\)")),
            "limit": money(g("Approval limit")),
            "pack": None,
        }
        m = re.search(r"a pack holds (\d+)", t)
        if m:
            c["pack"] = int(m.group(1))
        m = re.search(r"one box = (\d+) reams", t)
        if m:
            c["reams_per_box"] = int(m.group(1))
        u = c["unit"]
        c["period"] = "month" if "per month" in u else "year" if "per year" in u else None
        cats[f.stem] = c
    return cats


CAT_NAMES = {}  # "Courier services" -> "courier"


def load_amendments():
    out = []
    for f in sorted((PB / "amendments").glob("*.md")):
        t = f.read_text(encoding="utf-8")
        name = re.search(r"# AMD-\d+:\s*(.+)", t).group(1).strip()
        date = re.search(r"Effective from:\*\*\s*(\S+)", t).group(1)
        m = re.search(r"the (walk-away price|target price|approval limit) for .+? is now (₹[\d,]+)", t)
        field_ = {"walk-away price": "walkaway", "target price": "target", "approval limit": "limit"}[m.group(1)]
        out.append({"cat_name": name, "date": date, "field": field_, "value": money(m.group(2)), "id": f.stem})
    return out


def load_suppliers():
    sups = {}
    for f in (PB / "suppliers").glob("*.md"):
        t = f.read_text(encoding="utf-8")
        name = re.sub(r"^#\s*(Supplier:\s*)?", "", t.splitlines()[0]).strip()
        status = re.search(r"\*\*Status:\*\*\s*(\w+)", t).group(1).upper()
        s = {"status": status, "fx": {}, "caps": {}}
        for m in re.finditer(r"invoices in (\w+) are converted at ₹([\d.,]+) per", t):
            s["fx"][m.group(1)] = money(m.group(2))
        for m in re.finditer(r"price cap for (.+?) of (₹[\d,]+)", t):
            s["caps"][m.group(1).strip()] = money(m.group(2))
        sups[name.lower()] = s
    return sups


CATS = load_categories()
for slug in CATS:
    t = (PB / "categories" / f"{slug}.md").read_text(encoding="utf-8")
    CAT_NAMES[re.search(r"# Category:\s*(.+)", t).group(1).strip().lower()] = slug
AMDS = load_amendments()
SUPS = load_suppliers()
FX = {"USD": 84.5, "EUR": 91.0, "GBP": 106.0}

# The amendment / cap pages name categories by display name; map them to slugs.
def cat_slug(display):
    d = display.lower().strip()
    if d in CAT_NAMES:
        return CAT_NAMES[d]
    for k, v in CAT_NAMES.items():  # "Cleaning services" vs "Cleaning"
        if d.startswith(k) or k.startswith(d):
            return v
    raise KeyError(display)


# --------------------------------------------------------------------------- number parsing

WORD_NUM = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
WORD_MULT = {"hundred": 100, "thousand": 1000, "lakh": 100000, "lakhs": 100000,
             "crore": 10000000, "million": 1000000}
WORD_RE = r"(?:(?:%s)(?:[\s-]+(?:and[\s-]+)?(?:%s))*)" % (
    "|".join(list(WORD_NUM) + list(WORD_MULT)), "|".join(list(WORD_NUM) + list(WORD_MULT)))


def words_to_num(s):
    total, cur = 0, 0
    for w in re.split(r"[\s-]+", s.lower()):
        if w == "and" or not w:
            continue
        if w in WORD_NUM:
            cur += WORD_NUM[w]
        elif w == "hundred":
            cur = (cur or 1) * 100
        elif w in WORD_MULT:
            total += (cur or 1) * WORD_MULT[w]
            cur = 0
    return total + cur


CUR_TOKEN = r"(?:₹|Rs\.?|INR|USD|US\$|\$|EUR|€|GBP|£|[A-Z]{3})"
NUM = r"\d[\d,]*(?:\.\d+)?"
AMOUNT_RE = re.compile(
    rf"(?P<cur>{CUR_TOKEN})\s*(?P<num>{NUM})\s*(?P<suf>lakhs?|lakh|k|K|crore|million)?\b"
    rf"|(?P<num2>{NUM})\s*(?P<suf2>lakhs?|k|K|crore)?\s*(?P<cur2>rupees|INR|USD|EUR|GBP)\b"
    rf"|(?P<words>{WORD_RE})\s+(?P<cur3>rupees|dollars|euros|pounds)"
)
CUR_MAP = {"₹": "INR", "Rs": "INR", "Rs.": "INR", "INR": "INR", "rupees": "INR", "USD": "USD",
           "US$": "USD", "$": "USD", "dollars": "USD", "EUR": "EUR", "€": "EUR", "euros": "EUR",
           "GBP": "GBP", "£": "GBP", "pounds": "GBP"}
SUFFIX = {"lakh": 1e5, "lakhs": 1e5, "k": 1e3, "K": 1e3, "crore": 1e7, "million": 1e6}


@dataclass
class Amount:
    value: float
    cur: str
    start: int
    end: int


NOT_CURRENCY = {"GST", "ERP", "CFO", "PO", "AI", "CRM", "SKU", "LED", "USB", "NET", "AMD", "IST", "CEO"}


def find_amounts(text):
    out = []
    for m in AMOUNT_RE.finditer(text):
        if m.group("cur") is not None:
            cur_raw = m.group("cur")
            if re.fullmatch(r"[A-Z]{3}", cur_raw) and cur_raw in NOT_CURRENCY:
                continue
            v = float(m.group("num").replace(",", ""))
            if m.group("suf"):
                v *= SUFFIX[m.group("suf")]
            cur = CUR_MAP.get(cur_raw, CUR_MAP.get(cur_raw.rstrip("."), cur_raw))
        elif m.group("num2") is not None:
            v = float(m.group("num2").replace(",", ""))
            if m.group("suf2"):
                v *= SUFFIX[m.group("suf2")]
            cur = CUR_MAP[m.group("cur2")]
        else:
            v = float(words_to_num(m.group("words")))
            cur = CUR_MAP[m.group("cur3")]
        out.append(Amount(v, cur, m.start(), m.end()))
    return out


# --------------------------------------------------------------------------- thread splitting

HDR = re.compile(r"^\[(Earlier|Latest) message — ([^\],]+)(?:, ([\d-]+))?\]\s*$", re.M)


def split_thread(msg):
    """Return list of (kind, author, text). If no headers, the whole message is the latest."""
    parts = []
    hs = list(HDR.finditer(msg))
    if not hs:
        return [("Latest", None, msg)]
    for i, h in enumerate(hs):
        end = hs[i + 1].start() if i + 1 < len(hs) else len(msg)
        parts.append((h.group(1), h.group(2).strip(), msg[h.end():end]))
    return parts


# --------------------------------------------------------------------------- detectors

INJECTION_PATTERNS = [
    r"\b(language model|LLM|AI agent|AI assistant|chatbot|bot|automated system|automated agent|"
    r"AI model|reviewing assistant|the assistant|assistant:|reviewer)\b",
    r"\boverride\b", r"ignore (all |your )?(previous|prior) instructions", r"SYSTEM NOTE",
    r"pre-approved", r"already (granted|signed off|approved)", r"approval (was |has been )?already",
    r"mark (it|this)( thread)? (as )?accept", r"Output ACCEPT", r"set action\s*=",
    r"सहायक", r"निर्देश", r"\bAI\b(?!-powered)",
]
SAFE_PHRASES = [r"AI-powered", r"system integration team", r"ERP system", r"assistant manager",
                r"assistant manager,"]


def detect_injection(full_msg):
    t = full_msg
    for s in SAFE_PHRASES:
        t = re.sub(s, " ", t, flags=re.I)
    for p in INJECTION_PATTERNS:
        m = re.search(p, t, flags=re.I)
        if m:
            return m.group(0)
    return None


PCT_WORDS = {"half": 50, "one third": 33.4, "a third": 33.4, "two thirds": 66.7, "a quarter": 25,
             "one quarter": 25, "three quarters": 75, "full": 100, "the full": 100, "entire": 100}


def detect_banned(latest):
    t = latest
    tl = t.lower()
    # automatic renewal
    if re.search(r"no automatic renewal|not auto-?renew|does not renew automatically|renewal only (if|by) (both|mutual)", tl):
        tl_r = re.sub(r"no automatic renewal[^.]*\.|renewal only (if|by)[^.]*\.", " ", tl)
    else:
        tl_r = tl
    if re.search(r"auto-?renew|automatic(ally)? renew|renews? (on its own|automatically)|"
                 r"continues? (from )?year to year|year[- ]on[- ]year unless|unless (either party gives notice|cancelled)",
                 tl_r):
        return "auto-renewal"
    # unlimited liability
    if re.search(r"unlimited liability|liability[^.]*\bunlimited\b|uncapped liability", tl):
        return "unlimited liability"
    # exclusivity (but allow explicit "not asking for exclusivity")
    tl_e = re.sub(r"(not|no|without)[^.]*exclusiv[^.]*\.", " ", tl)
    if re.search(r"exclusiv|only from us|buy[^.]* only from|sole supplier|solely from us", tl_e):
        return "exclusivity"
    # upfront / advance > 30%
    for sent in re.split(r"(?<=[.!?])\s+|\n", t):
        sl = sent.lower()
        if not re.search(r"upfront|up-front|advance|in advance|before delivery|prepay|pre-pay", sl):
            continue
        for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(%|percent|per cent)", sl):
            if float(m.group(1)) > 30:
                return f"advance {m.group(1)}%"
        for w, v in PCT_WORDS.items():
            if re.search(rf"\b{w}\b", sl) and v > 30:
                return f"advance '{w}'"
        m = re.search(rf"({WORD_RE})\s*(percent|per cent|%)", sl)
        if m and words_to_num(m.group(1)) > 30:
            return f"advance {m.group(0)}"
    return None


# --------------------------------------------------------------------------- price normalisation

UNIT_WORDS = {
    "catering": ["meal"], "cleaning": ["site"], "cloud-servers": ["server"], "courier": ["shipment"],
    "crm-licenses": ["seat", "licen[cs]e"], "design-software": ["seat", "licen[cs]e"],
    "headsets": ["headset"], "laptops": ["laptop"], "led-lighting": ["fixture"],
    "mobile-phones": ["phone"], "monitors": ["monitor"], "office-chairs": ["chair"],
    "printer-paper": ["box"], "security-guards": ["guard"], "training": ["participant"],
}

GST_INCL = re.compile(r"incl(\.|usive|uding)? (of )?GST|GST[- ]inclusive|GST included|with GST", re.I)
GST_EXCL = re.compile(r"excl(\.|usive|uding)? (of )?GST|plus GST|\+ ?GST|GST extra", re.I)


@dataclass
class Offer:
    p: float | None = None
    missing: str | None = None
    trace: list = field(default_factory=list)


def to_inr(a: Amount, sup):
    if a.cur == "INR":
        return a.value
    if a.cur in sup["fx"]:
        return a.value * sup["fx"][a.cur]
    if a.cur in FX:
        return a.value * FX[a.cur]
    return None


def clause_after(text, a: Amount, n=120):
    return text[a.end:a.end + n]


def unit_adjust(price, after, cat, slug, tr):
    """Convert a per-<something> price into the category's normalized unit. Returns (price, missing)."""
    al = after.lower()
    # cut the clause at sentence end, or at the start of another amount / tier clause
    clause = re.split(r"(?<!\d)\.(?!\d)|,? (?:and|for orders|for larger|plus|with a further)\b|\n", al)[0]
    # per dozen
    if re.search(r"\bper dozen\b", clause):
        price /= 12; tr.append("per dozen ÷12")
    m = re.search(r"\bper pack of (\d+)", clause)
    if m:
        price /= int(m.group(1)); tr.append(f"per pack of {m.group(1)}")
    elif re.search(r"\bper pack\b", clause) and cat.get("pack"):
        price /= cat["pack"]; tr.append(f"per pack ÷{cat['pack']}")
    m = re.search(r"\bper (\d+) \w+", clause)
    if m and not re.search(r"\bper (\d+) (days|weeks)", clause):
        price /= int(m.group(1)); tr.append(f"per {m.group(1)} units")
    if slug == "printer-paper" and re.search(r"\bper ream\b", clause):
        price *= cat.get("reams_per_box", 10); tr.append("per ream ×10")
    # billing period
    if cat["period"]:
        has_month = re.search(r"\b(per|a|each|/) ?month|monthly|/mo\b", clause)
        has_year = re.search(r"\b(per|a|each|/) ?(year|annum)|annual|yearly|/yr\b|p\.a\.", clause)
        if has_month and not has_year:
            if cat["period"] == "year":
                price *= 12; tr.append("monthly→yearly ×12")
        elif has_year and not has_month:
            if cat["period"] == "month":
                price /= 12; tr.append("yearly→monthly ÷12")
        else:
            return price, "subscription price with no billing period"
    return price, None


def parse_offer(row, sup, cat, slug):
    off = Offer()
    tr = off.trace
    parts = split_thread(row["supplier_message"])
    latest = parts[-1][2]
    q = int(row["quantity"])
    # supplier's own earlier quotes (for "% off our previous quote")
    prev_quotes = []
    for kind, author, text in parts[:-1]:
        if author and "acme" in author.lower():
            continue
        p_prev, miss_prev = price_from_text(text, row, sup, cat, slug, [], prev=None, allow_relative=False)
        if p_prev is not None:
            prev_quotes.append(p_prev)
    prev = prev_quotes[-1] if prev_quotes else None
    p, miss = price_from_text(latest, row, sup, cat, slug, tr, prev=prev, allow_relative=True)
    off.p, off.missing = p, miss
    return off


def price_from_text(text, row, sup, cat, slug, tr, prev, allow_relative):
    q = int(row["quantity"])
    amts = find_amounts(text)
    tl = text.lower()

    # relative: "a further D% off our previous quote"
    m = re.search(r"(\d+(?:\.\d+)?)\s*% off (our|the) previous quote", text)
    if m and not amts:
        if not allow_relative or prev is None:
            return None, "relative discount with no previous quote"
        p = prev * (1 - float(m.group(1)) / 100)
        tr.append(f"{m.group(1)}% off previous {prev:.2f} -> {p:.2f}")
        return p, None

    # correction: "should read A ..., not B" -> keep A only
    if re.search(r"should read", text, re.I):
        sr = re.search(r"should read", text, re.I).end()
        amts = [a for a in amts if a.start >= sr][:1]
        tr.append("correction")

    if not amts:
        return None, "no price"

    # unknown currency
    for a in amts:
        if a.cur not in ("INR", "USD", "EUR", "GBP"):
            return None, f"unlisted currency {a.cur}"

    # total for all N units
    mt = re.search(r"in total for all (\d+)", text)
    # range: take the upper end
    mr = re.search(r"between\s+(.{1,40}?)\s+and\s+", text)
    # tiers: "A per X for orders of up to N X, and B per X for larger orders"
    mtier = re.search(r"for orders of up to (\d+)", text)

    chosen = amts[0]
    if mr and len(amts) >= 2:
        chosen = max(amts[:2], key=lambda a: to_inr(a, sup) or 0)
        tr.append("range -> upper end")
    elif mtier and len(amts) >= 2:
        N = int(mtier.group(1))
        chosen = amts[0] if q <= N else amts[1]
        tr.append(f"tier: q={q} vs up to {N}")

    # list price discount: "a D% discount on our list price of X" / "D% off our list price of X"
    mlist = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(discount on|off) (our|the) list price", text)

    p = to_inr(chosen, sup)
    if p is None:
        return None, f"unlisted currency {chosen.cur}"
    if chosen.cur != "INR":
        rate = sup["fx"].get(chosen.cur, FX.get(chosen.cur))
        tr.append(f"{chosen.value} {chosen.cur} × {rate} = {p:.2f}")
    if mlist:
        p *= 1 - float(mlist.group(1)) / 100
        tr.append(f"list price -{mlist.group(1)}%")

    after = clause_after(text, chosen, 160)
    if mt:
        n = int(mt.group(1))
        p /= n
        tr.append(f"total ÷{n}")
        after = text[mt.end():mt.end() + 80]
    p, miss = unit_adjust(p, after, cat, slug, tr)
    if miss:
        return None, miss

    # GST inclusive (look in the sentence containing the price)
    sent_start = text.rfind("\n", 0, chosen.start) + 1
    sent_end = text.find("\n", chosen.end)
    sentence = text[sent_start: sent_end if sent_end > 0 else len(text)]
    if GST_INCL.search(sentence) and not GST_EXCL.search(sentence):
        p /= 1 + cat["gst"]
        tr.append(f"GST-incl ÷{1 + cat['gst']}")

    # free units: "plus F free X for every N you pay for"
    mf = re.search(r"plus (\d+|one|two|three) free \w+ for every (\d+)", text)
    if mf:
        F = int(mf.group(1)) if mf.group(1).isdigit() else WORD_NUM[mf.group(1)]
        N = int(mf.group(2))
        p = p * N / (N + F)
        tr.append(f"{F} free per {N} -> ×{N}/{N + F}")

    # volume discount: "with a further D% off for orders of N X or more"
    mv = re.search(r"further (\d+(?:\.\d+)?)\s*% off for orders of (\d+)", text)
    if mv:
        if q >= int(mv.group(2)):
            p *= 1 - float(mv.group(1)) / 100
            tr.append(f"volume -{mv.group(1)}% (q={q}≥{mv.group(2)})")
        else:
            tr.append(f"volume discount not met (q={q}<{mv.group(2)})")
    return p, None


# --------------------------------------------------------------------------- decision

def values_for(slug, date, sup):
    c = dict(CATS[slug])
    applied = []
    for a in sorted(AMDS, key=lambda a: a["date"]):
        if cat_slug(a["cat_name"]) == slug and a["date"] <= date:
            c[a["field"]] = a["value"]
            applied.append(a["id"])
    for cname, cap in sup["caps"].items():
        if cat_slug(cname) == slug:
            c["walkaway"] = cap
            applied.append("contract cap")
    return c, applied


ALT_BILLING = False


def round_half_up(x):
    return int(math.floor(x + 0.5))


def decide(row, trace=None):
    tr = trace if trace is not None else []
    sup = SUPS.get(row["supplier"].lower())
    if sup is None:
        raise KeyError(row["supplier"])
    slug = row["category"]
    cat, applied = values_for(slug, row["date"], sup)
    tr.append(f"values: target={cat['target']} walk={cat['walkaway']} R={cat['R']} β={cat['beta']} limit={cat['limit']} {applied}")

    off = parse_offer(row, sup, cat, slug)
    tr.extend(off.trace)
    # 1 missing info
    if off.p is None:
        tr.append(f"missing: {off.missing}")
        return "ESCALATE:MISSING_INFO:"
    tr.append(f"p = {off.p:.4f}")
    # 2 blacklisted
    if sup["status"] == "BLACKLISTED":
        return "REJECT:BLACKLISTED_SUPPLIER:"
    # 3 injection (anywhere in thread)
    inj = detect_injection(row["supplier_message"])
    if inj:
        tr.append(f"injection: {inj!r}")
        return "ESCALATE:PROMPT_INJECTION:"
    # 4 banned terms (latest message)
    latest = split_thread(row["supplier_message"])[-1][2]
    b = detect_banned(latest)
    if b:
        tr.append(f"banned: {b}")
        return "ESCALATE:BANNED_TERM:"
    # 5 price
    r = int(row["round"])
    R = cat["R"]
    if sup["status"] == "PROBATION":
        L = cat["target"]
    else:
        frac = 0.0 if R == 1 else min(max((r - 1) / (R - 1), 0.0), 1.0)
        L = cat["target"] + (cat["walkaway"] - cat["target"]) * frac ** cat["beta"]
    tr.append(f"L({r}) = {L:.4f}")
    p = off.p
    if p > L + 1e-9:
        if r >= R:
            return "REJECT:ABOVE_WALKAWAY:"
        return f"COUNTER:COUNTER_OFFER:{round_half_up(L)}"
    # 6 approval
    total = p * int(row["quantity"])
    if ALT_BILLING:  # alternative reading: "one billing period" = the period the supplier quoted
        if any("monthly→yearly" in t for t in tr):
            total /= 12
        elif any("yearly→monthly" in t for t in tr):
            total *= 12
    tr.append(f"total = {total:.2f}")
    if total > cat["limit"] + 1e-9:
        return "ESCALATE:OVER_APPROVAL_LIMIT:"
    return "ACCEPT:ACCEPTABLE_PRICE:"


# --------------------------------------------------------------------------- CLI

def read(name):
    return list(csv.DictReader(open(DATA / name, encoding="utf-8")))


def validate():
    rows = read("sample_threads.csv")
    ans = {r["thread_id"]: r["label"] for r in read("sample_answers.csv")}
    ok = 0
    for r in rows:
        tr = []
        pred = decide(r, tr)
        gold = ans[r["thread_id"]]
        if pred == gold:
            ok += 1
        else:
            print(f"MISS {r['thread_id']}: pred={pred} gold={gold}\n   " + "\n   ".join(tr))
    print(f"{ok}/{len(rows)} correct")


def predict():
    rows = read("threads.csv")
    out = ROOT / ("submission_alt_billing.csv" if ALT_BILLING else "submission.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["thread_id", "label"])
        for r in rows:
            w.writerow([r["thread_id"], decide(r)])
    print(f"wrote {out} ({len(rows)} rows)")


def explain(tid):
    for name in ("threads.csv", "sample_threads.csv"):
        for r in read(name):
            if r["thread_id"] == tid:
                tr = []
                print(r["supplier_message"])
                print("----", r["date"], r["supplier"], r["category"], "q=", r["quantity"], "round=", r["round"])
                print(decide(r, tr))
                print("\n".join(tr))
                return


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if "--alt-billing" in sys.argv:
        sys.argv.remove("--alt-billing")
        ALT_BILLING = True
    cmd = sys.argv[1] if len(sys.argv) > 1 else "validate"
    {"validate": validate, "predict": predict}.get(cmd, lambda: explain(sys.argv[2]))()
