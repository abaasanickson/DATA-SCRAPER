import os
import re
import time
import json
import urllib.parse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

SEARCH_BUDGET_SECONDS = 90
DEFAULT_SOURCE_BUDGET_SECONDS = 25
YELLOW_SOURCE_BUDGET_SECONDS = 45
REQUEST_TIMEOUT = 5
BROWSER_TIMEOUT = 7000
MAX_RECORDS_PER_SOURCE = 10000
MAX_TOTAL_RESULTS = 50000
MAX_PAGES_PER_SOURCE = 1000
MAX_DETAIL_PAGES_PER_SOURCE = 750
MAX_OSM_ELEMENTS_PER_QUERY = 5000
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"

SOURCE_NAMES = [
    "Yellow Uganda", "Find.ug", "Hotfrog Uganda", "FinderAfrica Uganda",
    "Yellow Pages Uganda", "National SME Portal", "KCCA Business Register",
    "OpenStreetMap"
]

REGION_CITIES = {
    "Kampala": ["kampala"],
    "Wakiso": ["wakiso", "kira", "nansana", "entebbe", "kajjansi", "kasangati", "gayaza", "bweyogerere"],
    "Mukono": ["mukono", "seeta", "lugazi", "nkokonjeru"],
    "Masaka": ["masaka", "nyendo"],
    "Jinja": ["jinja", "bugembe", "walukuba"],
    "Western Uganda": [
        "mbarara", "fort-portal", "fort portal", "kabale", "kasese", "hoima",
        "bushenyi", "ibanda", "ntungamo", "rukungiri", "kanungu", "bundibugyo",
        "kisoro", "sheema", "rubirizi", "mitooma", "kibaale", "kagadi", "kyenjojo",
        "masindi"
    ],
}

REGION_DISTRICTS = {
    "Kampala": {"kampala"},
    "Wakiso": {"wakiso"},
    "Mukono": {"mukono"},
    "Masaka": {"masaka"},
    "Jinja": {"jinja"},
    "Western Uganda": {
        "mbarara", "mbarara city", "mbarara district", "mubende", "mubende district",
        "ibanda", "ibanda district", "bushenyi", "bushenyi district", "sheema", "sheema district",
        "mitooma", "mitooma district", "rubirizi", "rubirizi district", "ntungamo", "ntungamo district",
        "rukungiri", "rukungiri district", "kanungu", "kanungu district", "kabale", "kabale district",
        "kisoro", "kisoro district", "kasese", "kasese district", "bundibugyo", "bundibugyo district",
        "fort portal", "fort portal city", "kyenjojo", "kyenjojo district", "hoima", "hoima district",
        "kibaale", "kibaale district", "kagadi", "kagadi district", "masindi", "masindi district",
        "buliisa", "buliisa district", "kazo", "kazo district", "kiruhura", "kiruhura district",
    },
}

REGION_ALIASES = {
    "Kampala": REGION_CITIES["Kampala"] + ["central division", "kawempe", "nakawa", "makindye", "rubaga", "lubaga", "ntinda", "kololo", "bukoto", "muyenga", "kabalagala", "kampala city"],
    "Wakiso": REGION_CITIES["Wakiso"] + ["wakiso district", "kyaliwajjala", "buloba", "busabala", "zanna", "lubowa"],
    "Mukono": REGION_CITIES["Mukono"] + ["mukono district", "sonde", "namanve", "nakisunga"],
    "Masaka": REGION_CITIES["Masaka"] + ["masaka district", "bukakata", "kijjabwemi"],
    "Jinja": REGION_CITIES["Jinja"] + ["jinja district", "mpumudde", "kimaka", "budhumbuli", "masese"],
    "Western Uganda": REGION_CITIES["Western Uganda"] + ["western uganda", "western region", "western"],
}

REGION_BBOXES = {
    "Kampala": ["0.25,32.45,0.42,32.70"],
    "Wakiso": ["0.05,32.25,0.60,32.80"],
    "Mukono": ["0.20,32.55,0.60,32.95"],
    "Masaka": ["-0.50,31.55,-0.15,31.90"],
    "Jinja": ["0.30,33.05,0.60,33.45"],
    "Western Uganda": [
        "-0.80,30.40,-0.45,31.00", "0.45,29.95,0.80,30.45",
        "-0.45,29.75,-0.10,30.30", "-0.10,29.75,0.30,30.35",
        "0.70,30.00,1.30,31.20", "0.05,30.75,0.45,31.45",
    ],
}

OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

session = requests.Session()
session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.8,*/*;q=0.5",
})

# ---------- text / validation ----------

def clean_text(v):
    if v is None:
        return "N/A"
    v = re.sub(r"\s+", " ", str(v)).strip()
    return v if v else "N/A"


def norm(v):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", clean_text(v).lower())).strip()


def slug(v):
    return re.sub(r"-+", "-", norm(v).replace(" ", "-")).strip("-")


def unique_join(values):
    out = []
    for v in values:
        v = clean_text(v)
        if v == "N/A":
            continue
        for p in re.split(r"\s*\|\s*", v):
            p = clean_text(p)
            if p != "N/A" and p not in out:
                out.append(p)
    return " | ".join(out) if out else "N/A"


BAD_NAMES = {
    "n/a", "na", "home", "contact", "contact us", "about", "about us", "login", "register",
    "search", "categories", "category", "read more", "details", "view profile", "send enquiry",
    "send inquiry", "website", "facebook", "twitter", "instagram", "linkedin", "no reviews",
    "write a review", "favorite", "map", "get directions", "with 0 comments", "with 0 comment",
}

BAD_NAME_PATTERNS = [
    r"^with\s+\d+\s+comments?$",
    r"^\d+\s+reviews?$",
    r"^page\s+\d+$",
    r"^top\s+\d+",
    r"^view\s+profile$",
    r"^send\s+enquir",
    r"^search\s+results?$",
]


def valid_business_name(name, keyword=""):
    n = clean_text(name)
    low = n.lower().strip()
    if low in BAD_NAMES or low == "n/a":
        return False
    if any(re.search(p, low, re.I) for p in BAD_NAME_PATTERNS):
        return False
    if len(n) < 2 or len(n) > 220:
        return False
    # Reject obvious UI/category headings.
    if re.fullmatch(r"[\W_\d]+", n):
        return False
    if low in {"hardware stores", "hardware", "construction", "building materials", "manufacturing", "store"}:
        return False
    return True


def phones_from(text):
    if not text or clean_text(text) == "N/A":
        return []
    t = str(text)
    patterns = [
        r"\+256\s*(?:\(?\d{2,3}\)?)[\s./-]*\d{3,4}[\s./-]*\d{3,4}",
        r"\(?0\d{2,3}\)?[\s./-]*\d{3,4}[\s./-]*\d{3,4}",
        r"\b0?4\d{2}[\s./-]*\d{3,4}[\s./-]*\d{2,4}\b",
    ]
    found = []
    for p in patterns:
        for m in re.finditer(p, t):
            raw = re.sub(r"\s+", " ", m.group(0)).strip(" -|,.;")
            digits = re.sub(r"\D", "", raw)
            if digits.startswith("256") and 10 <= len(digits) <= 12:
                val = "+" + digits
            elif digits.startswith("0") and 9 <= len(digits) <= 10:
                val = digits
            else:
                val = raw
            if len(re.sub(r"\D", "", val)) >= 9 and val not in found:
                found.append(val)
    return found


def phone_from(text):
    vals = phones_from(text)
    return " | ".join(vals) if vals else "N/A"


def emails_from(text):
    return list(dict.fromkeys(re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", clean_text(text), re.I)))


def email_from(text):
    vals = emails_from(text)
    return " | ".join(vals) if vals else "N/A"


def labeled(text, label):
    t = clean_text(text)
    labels = ["Address", "Physical Address", "Location", "Phone", "Contact number", "Contacts", "Email", "E-mail address", "Website", "Website address", "Category", "Listed in categories", "Listing Description", "Description", "Overview", "Company name", "Business name"]
    stops = [x for x in labels if x.lower() != label.lower()]
    stop = "|".join(re.escape(x) for x in stops)
    m = re.search(rf"{re.escape(label)}\s*:?\s*(.*?)(?=\s+(?:{stop})\s*:|$)", t, re.I)
    return clean_text(m.group(1)) if m else "N/A"


def address_from_text(text):
    for label in ["Address", "Physical Address", "Location"]:
        value = labeled(text, label)
        if value != "N/A" and len(value) >= 4:
            return value
    patterns = [
        r"Plot\s+[^|]{4,180}",
        r"P\.O\.\s*Box\s+[^|]{3,100}",
        r"[^|]{3,120}\s+Road,?\s+[^|]{2,100}(?:Kampala|Jinja|Wakiso|Mukono|Mbarara|Uganda)[^|]{0,100}",
    ]
    t = clean_text(text)
    for p in patterns:
        m = re.search(p, t, re.I)
        if m:
            return clean_text(m.group(0))
    return "N/A"


# ---------- Business Deals In: one short activity ----------
GENERIC_ACTIVITY = {
    "store", "stores", "shop", "shops", "ltd", "limited", "company", "co", "factory",
    "factories", "manufacturing", "manufacturer", "manufacturers", "supplier", "suppliers",
    "trader", "traders", "trading", "enterprise", "enterprises", "business", "businesses",
    "services", "service", "group", "uganda", "ug", "general", "dealers", "dealer",
}

ACTIVITY_RULES = [
    (r"\b(building|construction|builder|builders|building materials|civil works)\b", "Construction"),
    (r"\b(plumb|pipes?|sanitary ware|bathroom fittings?)\b", "Plumbing"),
    (r"\b(electrical|electrician|wiring|solar|power equipment)\b", "Electrical"),
    (r"\b(roofing|roof|roof sheets?|tiles?\s+and\s+roof)\b", "Roofing"),
    (r"\b(furniture|sofa|chairs?|beds?|cabinet|joinery)\b", "Furniture"),
    (r"\b(steel|metal|metalwork|fabrication|iron)\b", "Steel"),
    (r"\b(paint|paints|decorating|decoration)\b", "Paint"),
    (r"\b(timber|wood|lumber|sawmill)\b", "Timber"),
    (r"\b(tools?|fasteners?|bolts?|nuts?|hardware)\b", "Hardware"),
    (r"\b(auto|automotive|garage|vehicle|motor|spares?|car parts?)\b", "Automotive"),
    (r"\b(pharmacy|pharmaceutical|drugs?|medical supplies?)\b", "Pharmaceuticals"),
    (r"\b(food|foods?|bakery|baking|beverages?|drinks?|restaurant|catering)\b", "Food"),
    (r"\b(hotel|lodg(e|ing)|accommodation|guest house|resort)\b", "Hospitality"),
    (r"\b(school|education|college|university|training)\b", "Education"),
    (r"\b(furniture|interior design)\b", "Furniture"),
    (r"\b(agro|agriculture|farm|seeds?|fertilizer|agrovet)\b", "Agriculture"),
    (r"\b(telecom|telecommunications|mobile money|internet|ict|software|computer)\b", "Technology"),
    (r"\b(logistics|transport|trucking|courier|freight)\b", "Transport"),
    (r"\b(insurance|insurer)\b", "Insurance"),
    (r"\b(bank|microfinance|finance|financial)\b", "Finance"),
]


def short_activity(keyword, category="N/A", description="", name=""):
    texts = [clean_text(description), clean_text(category), clean_text(name)]
    combined = " ".join(x for x in texts if x != "N/A").lower()
    for pattern, label in ACTIVITY_RULES:
        if re.search(pattern, combined, re.I):
            return label
    # A specific category is better than a generic directory label.
    c = norm(category)
    if c and c not in {"n a", "hardware stores", "store", "stores", "shop", "general"}:
        words = [w for w in c.split() if w not in GENERIC_ACTIVITY]
        if words:
            return words[0].capitalize()
    # For broad searches, use the keyword only as the final fallback.
    kwords = [w for w in norm(keyword).split() if w not in GENERIC_ACTIVITY]
    if kwords:
        return " ".join(w.capitalize() for w in kwords[:2])
    # Generic search like Store/Ltd/Factory has no inferable activity.
    return clean_text(keyword).title() if clean_text(keyword) != "N/A" else "N/A"


def make_record(name, region, keyword, source, url, phone="N/A", website="N/A", address="N/A", category="N/A", deals="N/A", email="N/A", rating="N/A", lat="N/A", lng="N/A", district="N/A", description="N/A"):
    activity = short_activity(keyword, category, description if description != "N/A" else deals, name)
    return {
        "Company Name": clean_text(name), "Region": region, "Search Query": clean_text(keyword),
        "Category": clean_text(category), "Business Deals In": activity,
        "Phone Contact": clean_text(phone), "Email": clean_text(email), "Website": clean_text(website),
        "Physical Address": clean_text(address), "District": clean_text(district), "Rating": clean_text(rating),
        "Lat": clean_text(lat), "Lng": clean_text(lng), "Data Source": source, "Source URL": url,
    }


def region_match(text, region):
    t = norm(text)
    if not t or t == "n a":
        return True
    aliases = [norm(x) for x in REGION_ALIASES.get(region, [region])]
    return any(a and a in t for a in aliases)


def keyword_match(record, keyword):
    q = norm(keyword)
    if not q:
        return True
    hay = norm(" ".join([
        record.get("Company Name", ""), record.get("Category", ""), record.get("Business Deals In", ""),
        record.get("Physical Address", ""), record.get("Source URL", "")
    ]))
    if q in hay:
        return True
    tokens = [x for x in q.split() if len(x) > 2 and x not in GENERIC_ACTIVITY]
    if not tokens:
        # Generic searches such as "store", "ltd", "factory" are intentionally broad.
        return True
    return any(x in hay for x in tokens)


# ---------- HTTP ----------
def http_html(url):
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if r.status_code == 200 and len(r.text) > 300:
            return r.text, r.url
    except requests.RequestException:
        pass
    return None, url


def soup_from(html):
    return BeautifulSoup(html, "lxml")


def extract_jsonld(soup):
    rows = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or s.get_text())
            rows.extend(data if isinstance(data, list) else [data])
        except Exception:
            continue
    return rows


def jsonld_items(soup):
    for obj in extract_jsonld(soup):
        if not isinstance(obj, dict):
            continue
        yield obj
        graph = obj.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                if isinstance(item, dict):
                    yield item


def profile_name(soup, url):
    # Structured business name first.
    for item in jsonld_items(soup):
        typ = str(item.get("@type", "")).lower()
        if item.get("name") and (typ in {"organization", "localbusiness", "corporation", "store", "place"} or "business" in typ):
            n = clean_text(item.get("name"))
            if valid_business_name(n):
                return n
    # Directory profile pages normally expose the business name in h1.
    for tag in soup.find_all(["h1", "h2"]):
        n = clean_text(tag.get_text(" ", strip=True))
        if valid_business_name(n):
            # Strip common location suffixes from titles/headings.
            n = re.sub(r"\s*[-–—]\s*(Kampala|Jinja|Wakiso|Mukono|Mbarara|Uganda).*?$", "", n, flags=re.I).strip()
            if valid_business_name(n):
                return n
    text = clean_text(soup.get_text(" ", strip=True))
    for label in ["Company name", "Business name"]:
        n = labeled(text, label)
        if valid_business_name(n):
            return n
    title = clean_text(soup.title.get_text(" ", strip=True) if soup.title else "")
    if title:
        n = re.split(r"\s+[-|]\s+", title, maxsplit=1)[0].strip()
        if valid_business_name(n):
            return n
    return "N/A"


def detail_enrich(url, page_cache, deadline):
    if time.monotonic() >= deadline or not url or url in page_cache:
        return page_cache.get(url, {})
    if sum(1 for v in page_cache.values() if v) >= MAX_DETAIL_PAGES_PER_SOURCE:
        return {}
    html, final_url = http_html(url)
    if not html:
        page_cache[url] = {}
        return {}
    soup = soup_from(html)
    text = clean_text(soup.get_text(" ", strip=True))
    phones, emails = phones_from(text), emails_from(text)
    address, website, category, description = address_from_text(text), "N/A", "N/A", "N/A"
    name = profile_name(soup, final_url)
    rating = "N/A"
    for item in jsonld_items(soup):
        if item.get("telephone"): phones.extend(phones_from(item.get("telephone")))
        if item.get("email"): emails.extend(emails_from(item.get("email")))
        if address == "N/A" and isinstance(item.get("address"), dict):
            ad = item["address"]
            address = clean_text(", ".join(str(ad.get(k)) for k in ["streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry"] if ad.get(k)))
        if website == "N/A" and item.get("url"):
            website = clean_text(item.get("url"))
        if category == "N/A" and item.get("category"):
            category = clean_text(item.get("category"))
        if description == "N/A" and item.get("description"):
            description = clean_text(item.get("description"))
        if rating == "N/A" and isinstance(item.get("aggregateRating"), dict):
            rating = clean_text(item["aggregateRating"].get("ratingValue"))
    for a in soup.find_all("a", href=True):
        href = urllib.parse.urljoin(final_url, a.get("href", ""))
        label = clean_text(a.get_text(" ", strip=True)).lower()
        if href.startswith("tel:"):
            phones.extend(phones_from(urllib.parse.unquote(href[4:])))
        elif href.startswith("mailto:"):
            emails.extend(emails_from(urllib.parse.unquote(href[7:])))
        elif href.startswith("http") and label in {"website", "visit website", "web site"}:
            website = href
    if category == "N/A":
        category = labeled(text, "Category")
        if category == "N/A":
            category = labeled(text, "Listed in categories")
    for label in ["Listing Description", "Description", "Overview", "Company description"]:
        if description == "N/A":
            description = labeled(text, label)
    if address == "N/A":
        candidates = re.findall(r"(?:Plot\s+[^|]{4,180}|P\.O\.\s*Box\s+[^|]{3,100})", text, re.I)
        if candidates:
            address = clean_text(candidates[0])
    page_cache[url] = {
        "name": name, "phone": phone_from(" | ".join(phones)), "email": email_from(" | ".join(emails)),
        "address": address, "website": website, "category": category, "description": description,
        "rating": rating, "url": final_url,
    }
    return page_cache[url]


def listing_link_candidates(soup, base, markers):
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = urllib.parse.urljoin(base, a.get("href", ""))
        text = clean_text(a.get_text(" ", strip=True))
        low = href.lower()
        if not any(m in low for m in markers):
            continue
        if href in seen or href.rstrip("/") == base.rstrip("/"):
            continue
        seen.add(href)
        out.append((href, a, text))
    return out


def next_page(soup, current):
    for a in soup.find_all("a", href=True):
        txt = norm(a.get_text(" ", strip=True))
        rel = " ".join(a.get("rel", [])).lower()
        if txt in {"next", "next page", ">", "→", "older posts"} or "next" in rel:
            return urllib.parse.urljoin(current, a["href"])
    return None


def build_record_from_profile(href, anchor_text, region, keyword, source, page_cache, deadline, card_text=""):
    # If the anchor itself is clearly a real business name, keep it initially.
    name = anchor_text if valid_business_name(anchor_text, keyword) else "N/A"
    phone = phone_from(card_text)
    email = email_from(card_text)
    address = address_from_text(card_text)
    category = labeled(card_text, "Category")
    deals_text = labeled(card_text, "Listing Description")
    if deals_text == "N/A": deals_text = labeled(card_text, "Description")
    d = {}
    # Always enrich when the name is invalid; otherwise enrich only when useful fields are missing.
    if name == "N/A" or phone == "N/A" or address == "N/A" or deals_text == "N/A":
        d = detail_enrich(href, page_cache, deadline)
    if name == "N/A": name = d.get("name", "N/A")
    if phone == "N/A": phone = d.get("phone", "N/A")
    else: phone = unique_join([phone, d.get("phone", "N/A")])
    email = unique_join([email, d.get("email", "N/A")])
    if address == "N/A": address = d.get("address", "N/A")
    if category == "N/A": category = d.get("category", "N/A")
    if deals_text == "N/A": deals_text = d.get("description", "N/A")
    website = d.get("website", "N/A")
    rating = d.get("rating", "N/A")
    if not valid_business_name(name, keyword):
        return None
    return make_record(name, region, keyword, source, d.get("url", href), phone, website, address, category, deals_text, email, rating, description=deals_text)


# ---------- Yellow Uganda ----------
def yellow_category_candidates(region, keyword, deadline):
    cities = REGION_CITIES.get(region, [slug(region)])
    q = norm(keyword)
    found = []

    def inspect_city(city):
        if time.monotonic() >= deadline:
            return []
        u = f"https://www.yellow.ug/location/{slug(city)}/list%3Acategories"
        html, final = http_html(u)
        if not html:
            return []
        soup = soup_from(html)
        local = []
        for a in soup.find_all("a", href=True):
            h = urllib.parse.urljoin(final, a["href"])
            txt = norm(a.get_text(" ", strip=True))
            if "/category/" not in h.lower():
                continue
            # Exact query, meaningful token, or closely related construction/hardware terms.
            score = 5 if q and q in txt else 0
            if not score and q:
                tokens = [x for x in q.split() if len(x) > 2 and x not in GENERIC_ACTIVITY]
                score = 3 if any(t in txt for t in tokens) else 0
            related = {"hardware": ["hardware stores", "building materials", "construction", "tools", "plumbing", "roofing"],
                       "store": ["stores", "shopping", "retail"],
                       "manufacturing": ["manufacturing", "industrial"],
                       "factory": ["manufacturing", "industrial"],
                       "ltd": []}.get(q, [])
            if not score and any(r in txt for r in related):
                score = 2
            if score:
                local.append((score, h))
        return local

    # Discovery is parallel but only for small category-index pages; business-page crawling is sequential.
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(cities)))) as ex:
        futures = [ex.submit(inspect_city, c) for c in cities]
        for fut in as_completed(futures):
            try:
                found.extend(fut.result())
            except Exception:
                pass
    direct = [f"https://www.yellow.ug/category/{slug(keyword)}/city%3A{slug(c)}" for c in cities]
    direct.append(f"https://www.yellow.ug/category/{slug(keyword)}")
    seen, out = set(), []
    for _, u in sorted(found, key=lambda x: -x[0]) + [(1, x) for x in direct]:
        if u not in seen:
            seen.add(u); out.append(u)
    return out


def scrape_yellow(region, keyword, deadline):
    out, seen_pages, seen_profiles, details = [], set(), set(), {}
    starts = yellow_category_candidates(region, keyword, deadline)
    for start in starts:
        url = start
        pages = 0
        while url and pages < MAX_PAGES_PER_SOURCE and time.monotonic() < deadline and len(out) < MAX_RECORDS_PER_SOURCE:
            if url in seen_pages:
                break
            seen_pages.add(url); pages += 1
            html, final = http_html(url)
            if not html:
                break
            soup = soup_from(html)
            links = listing_link_candidates(soup, final, ["/company/"])
            for href, a, anchor_text in links:
                if href in seen_profiles:
                    continue
                seen_profiles.add(href)
                card = a.parent
                for _ in range(8):
                    if not getattr(card, "parent", None): break
                    txt = clean_text(card.get_text(" ", strip=True))
                    if len(txt) <= 3500 and any(k in txt.lower() for k in ["address", "phone", "contact number", "view profile"]):
                        break
                    card = card.parent
                card_text = clean_text(card.get_text(" ", strip=True))
                rec = build_record_from_profile(href, anchor_text, region, keyword, "Yellow Uganda", details, deadline, card_text)
                page_scoped = norm(keyword) in norm(final) or slug(keyword) in norm(final)
                if rec and (keyword_match(rec, keyword) or page_scoped or norm(keyword) in norm(card_text)):
                    out.append(rec)
                if len(out) >= MAX_RECORDS_PER_SOURCE:
                    break
            nxt = next_page(soup, final)
            if nxt and nxt not in seen_pages:
                url = nxt
            else:
                # Yellow Uganda's category pagination is /category/slug/N/city%3Acity.
                nums = []
                for a in soup.find_all("a", href=True):
                    h = urllib.parse.urljoin(final, a["href"])
                    m = re.search(r"/category/[^/]+/(\d+)/city%3A", h, re.I)
                    if m:
                        nums.append((int(m.group(1)), h))
                bigger = [(n, h) for n, h in nums if n > pages]
                url = min(bigger, key=lambda x: x[0])[1] if bigger else None
    return out


# ---------- Other directories ----------
def generic_directory_crawl(start_urls, region, keyword, source, markers, deadline, detail_markers=True, exclude_patterns=None):
    out, seen_pages, seen_records, details = [], set(), set(), {}
    queue = list(start_urls)
    exclude_patterns = exclude_patterns or ["/category/", "/tags/", "/page/"]
    while queue and len(out) < MAX_RECORDS_PER_SOURCE and time.monotonic() < deadline and len(seen_pages) < MAX_PAGES_PER_SOURCE:
        url = queue.pop(0)
        if url in seen_pages:
            continue
        seen_pages.add(url)
        html, final = http_html(url)
        if not html:
            continue
        soup = soup_from(html)
        for href, a, anchor_text in listing_link_candidates(soup, final, markers):
            if len(out) >= MAX_RECORDS_PER_SOURCE or href in seen_records:
                continue
            low = href.lower()
            if any(p in low for p in exclude_patterns):
                continue
            card = a.parent
            for _ in range(8):
                if not getattr(card, "parent", None): break
                txt = clean_text(card.get_text(" ", strip=True))
                if len(txt) <= 3500 and any(k in txt.lower() for k in ["address", "phone", "contacts", "category", "overview", "description"]):
                    break
                card = card.parent
            card_text = clean_text(card.get_text(" ", strip=True))
            rec = build_record_from_profile(href, anchor_text, region, keyword, source, details, deadline, card_text)
            if not rec:
                continue
            page_scoped = norm(keyword) in norm(final) or slug(keyword) in norm(final)
            if (keyword_match(rec, keyword) or page_scoped) and (rec["Physical Address"] == "N/A" or region_match(rec["Physical Address"], region) or region_match(card_text, region)):
                out.append(rec)
                seen_records.add(href)
        nxt = next_page(soup, final)
        if nxt and nxt not in seen_pages:
            queue.append(nxt)
        # Queue only plausible search/category pages, never arbitrary internal links.
        for a in soup.find_all("a", href=True):
            h = urllib.parse.urljoin(final, a["href"])
            txt = norm(a.get_text(" ", strip=True))
            low = h.lower()
            if h in seen_pages or h in queue or not any(m in low for m in markers):
                continue
            if any(p in low for p in exclude_patterns):
                continue
            if txt and (norm(keyword) in txt or any(t in txt for t in norm(keyword).split() if len(t) > 2)):
                queue.append(h)
    return out


def scrape_find(region, keyword, deadline):
    q = urllib.parse.quote(keyword)
    return generic_directory_crawl([
        f"https://find.ug/?s={q}", f"https://find.ug/listings/?s={q}",
        f"https://find.ug/listing-category/{slug(keyword)}/", "https://find.ug/all-listings/"
    ], region, keyword, "Find.ug", ["/listing/"], deadline,
    exclude_patterns=["/listing-category/", "/category/", "/page/"])


def scrape_hotfrog(region, keyword, deadline):
    out = []
    for city in REGION_CITIES.get(region, [slug(region)]):
        if time.monotonic() >= deadline or len(out) >= MAX_RECORDS_PER_SOURCE:
            break
        base = f"https://www.hotfrog.ug/search/{slug(city)}/{slug(keyword)}"
        out.extend(generic_directory_crawl([base], region, keyword, "Hotfrog Uganda", ["/company/"], deadline, exclude_patterns=["/search/", "/category/", "/page/"]))
    return out[:MAX_RECORDS_PER_SOURCE]


def scrape_finder(region, keyword, deadline):
    q = urllib.parse.quote(keyword)
    return generic_directory_crawl([
        f"https://finderafrica.com/?s={q}", f"https://finderafrica.com/listing-category/{slug(keyword)}/",
        "https://finderafrica.com/location/business-directory-uganda/"
    ], region, keyword, "FinderAfrica Uganda", ["/listing/"], deadline,
    exclude_patterns=["/listing-category/", "/category/", "/page/"])


def scrape_yellowpages(region, keyword, deadline):
    q = urllib.parse.quote(keyword)
    starts = [f"https://www.yellowpages-uganda.com/?s={q}", "https://www.yellowpages-uganda.com/location/"]
    html, final = http_html("https://www.yellowpages-uganda.com/location/")
    if html:
        soup = soup_from(html)
        qn = norm(keyword)
        for a in soup.find_all("a", href=True):
            h = urllib.parse.urljoin(final, a["href"]); t = norm(a.get_text(" ", strip=True))
            if any(x in h.lower() for x in ["/listings/category/", "/listings/tags/"]) and (qn in t or any(tok in t for tok in qn.split() if len(tok) > 2)):
                starts.insert(0, h)
    return generic_directory_crawl(starts, region, keyword, "Yellow Pages Uganda", ["/listings/"], deadline,
                                   exclude_patterns=["/listings/category/", "/listings/tags/", "/listings/page/"])


def scrape_sme(region, keyword, deadline):
    starts = [
        f"https://mybusiness.go.ug/Reports/SMEDirectory?Filters.SearchTerm={urllib.parse.quote(keyword)}",
        "https://mybusiness.go.ug/Reports/SMEDirectory",
    ]
    out, seen, pages = [], set(), 0
    allowed = REGION_DISTRICTS.get(region, set())
    for start in starts:
        url = start
        while url and url not in seen and pages < MAX_PAGES_PER_SOURCE and len(out) < MAX_RECORDS_PER_SOURCE and time.monotonic() < deadline:
            seen.add(url); pages += 1
            html, final = http_html(url)
            if not html: break
            soup = soup_from(html)
            for row in soup.select("tr"):
                cells = [clean_text(x.get_text(" ", strip=True)) for x in row.find_all(["td", "th"])]
                if len(cells) < 3: continue
                offset = 1 if cells[0].isdigit() else 0
                name = cells[offset]
                sector = cells[offset + 1] if len(cells) > offset + 1 else "N/A"
                district = cells[offset + 2] if len(cells) > offset + 2 else "N/A"
                if not valid_business_name(name, keyword): continue
                district_norm = norm(district)
                region_ok = any(d and (d == district_norm or d in district_norm) for d in allowed)
                search_filtered = "Filters.SearchTerm=" in start
                if region_ok and (search_filtered or keyword_match({"Company Name": name, "Category": sector, "Business Deals In": sector, "Physical Address": district, "Source URL": final}, keyword)):
                    out.append(make_record(name, region, keyword, "National SME Portal", final, category=sector, deals=sector, district=district, description=sector))
            url = next_page(soup, final)
    return out[:MAX_RECORDS_PER_SOURCE]


def scrape_kcca(region, keyword, deadline):
    if region != "Kampala" or time.monotonic() >= deadline:
        return []
    # KCCA is a table, not a profile directory. Extract only rows with a plausible business name.
    urls = [f"https://kcca.go.ug/businesses?business_name={urllib.parse.quote(keyword)}&business_nature={urllib.parse.quote(keyword)}", "https://kcca.go.ug/businesses"]
    out = []
    for url in urls:
        html, final = http_html(url)
        if not html: continue
        soup = soup_from(html)
        for row in soup.select("tr"):
            cells = [clean_text(x.get_text(" ", strip=True)) for x in row.find_all(["td", "th"])]
            if len(cells) < 2: continue
            name = cells[1] if cells[0].isdigit() and len(cells) > 1 else cells[0]
            nature = cells[2] if cells[0].isdigit() and len(cells) > 2 else (cells[1] if len(cells) > 1 else "N/A")
            division = cells[3] if cells[0].isdigit() and len(cells) > 3 else "N/A"
            if valid_business_name(name, keyword) and (keyword_match({"Company Name": name, "Category": nature, "Business Deals In": nature, "Physical Address": division, "Source URL": final}, keyword) or norm(keyword) in norm(nature)):
                out.append(make_record(name, region, keyword, "KCCA Business Register", final, category=nature, deals=nature, district=division, description=nature))
        if out: break
    return out[:MAX_RECORDS_PER_SOURCE]


# ---------- OpenStreetMap ----------
def osm_query(bbox, keyword):
    q = norm(keyword)
    tokens = [t for t in q.split() if len(t) >= 3 and t not in GENERIC_ACTIVITY][:4]
    terms = list(dict.fromkeys(([q] if q else []) + tokens))
    blocks = []
    keys = ["name", "brand", "operator", "description", "shop", "amenity", "office", "craft", "industrial", "healthcare", "tourism"]
    for term in terms:
        safe = re.sub(r"[^a-zA-Z0-9 _-]", "", term).strip()
        if not safe: continue
        pattern = re.escape(safe).replace(r"\ ", r"[ _-]+")
        for key in keys:
            blocks.append(f'nwr["{key}"~"{pattern}",i]({bbox});')
    return "[out:json][timeout:20];(\n" + "\n".join(blocks) + "\n);out center tags;"


def fetch_osm_grid_data(region_name, keyword):
    out, seen = [], set()
    deadline = time.monotonic() + SEARCH_BUDGET_SECONDS
    for bbox in REGION_BBOXES.get(region_name, []):
        if time.monotonic() >= deadline or len(out) >= MAX_RECORDS_PER_SOURCE: break
        query = osm_query(bbox, keyword)
        for endpoint in OVERPASS:
            try:
                remaining = max(5, min(25, int(deadline - time.monotonic())))
                r = requests.post(endpoint, data=query, headers={"User-Agent": USER_AGENT}, timeout=remaining)
                if r.status_code != 200: continue
                for el in r.json().get("elements", [])[:MAX_OSM_ELEMENTS_PER_QUERY]:
                    tags = el.get("tags", {})
                    name = clean_text(tags.get("name"))
                    if not valid_business_name(name, keyword): continue
                    c = el.get("center", {})
                    lat, lng = el.get("lat", c.get("lat", "N/A")), el.get("lon", c.get("lon", "N/A"))
                    addr = ", ".join(str(tags[k]) for k in ["addr:housenumber", "addr:street", "addr:place", "addr:suburb", "addr:city", "addr:district", "addr:postcode"] if tags.get(k))
                    category = next((clean_text(tags.get(k)) for k in ["shop", "amenity", "office", "craft", "industrial", "healthcare", "tourism"] if tags.get(k)), "N/A")
                    description = clean_text(tags.get("description") or "N/A")
                    rec = make_record(name, region_name, keyword, "OpenStreetMap", endpoint, tags.get("phone") or tags.get("contact:phone", "N/A"), tags.get("website") or tags.get("contact:website", "N/A"), addr or "N/A", category, description, tags.get("email") or tags.get("contact:email", "N/A"), tags.get("rating", "N/A"), lat, lng, clean_text(tags.get("addr:district") or tags.get("is_in:district") or "N/A"), description)
                    if not keyword_match(rec, keyword): continue
                    key = (norm(name), norm(addr) or norm(f"{lat}|{lng}"))
                    if key not in seen:
                        seen.add(key); out.append(rec)
                    if len(out) >= MAX_RECORDS_PER_SOURCE: break
                break
            except Exception:
                continue
    fetch_osm_grid_data.last_count = len(out)
    return out[:MAX_RECORDS_PER_SOURCE]

fetch_osm_grid_data.last_count = 0


# ---------- final dedupe / orchestration ----------
def identity(r):
    name, addr = norm(r.get("Company Name")), norm(r.get("Physical Address"))
    phone = re.sub(r"\D", "", clean_text(r.get("Phone Contact")))
    if not name: return None
    return (name, addr if addr and addr != "n a" else "", phone if phone and phone != "n a" else "")


def _same_business(a, b):
    na, nb = norm(a.get("Company Name")), norm(b.get("Company Name"))
    if not na or na != nb:
        return False
    aa, ab = norm(a.get("Physical Address")), norm(b.get("Physical Address"))
    pa = {re.sub(r"\D", "", x) for x in clean_text(a.get("Phone Contact")).split("|") if re.sub(r"\D", "", x)}
    pb = {re.sub(r"\D", "", x) for x in clean_text(b.get("Phone Contact")).split("|") if re.sub(r"\D", "", x)}
    if aa and aa != "n a" and ab and ab != "n a":
        return aa == ab or bool(pa & pb)
    return True


def merge(a, b):
    for k in ["Phone Contact", "Email", "Website", "Physical Address", "Rating", "Lat", "Lng", "Category", "Business Deals In", "District"]:
        if clean_text(b.get(k)) != "N/A":
            if k == "Business Deals In":
                # Prefer the most specific one; never concatenate multiple activities.
                old = clean_text(a.get(k))
                new = clean_text(b.get(k))
                if old == "N/A" or old == "Hardware" or len(new.split()) < len(old.split()):
                    a[k] = new
            else:
                a[k] = unique_join([a.get(k), b.get(k)])
    for k in ["Data Source", "Source URL", "Search Query"]:
        a[k] = unique_join([a.get(k), b.get(k)])
    return a


def deduplicate_records(records):
    unique = OrderedDict()
    for r in records:
        if not valid_business_name(r.get("Company Name", ""), r.get("Search Query", "")):
            continue
        key = identity(r)
        if not key:
            continue
        matched_key = None
        # First look for an existing record with the same genuine business name.
        # If one side lacks address data, that record can be safely enriched. If both
        # sides have different addresses, only merge when phone evidence agrees.
        for existing_key, existing in unique.items():
            if _same_business(existing, r):
                matched_key = existing_key
                break
        if matched_key is None:
            unique[key] = dict(r)
        else:
            unique[matched_key] = merge(unique[matched_key], r)
    return list(unique.values())


def _run_source(name, fn, region, keyword, budget):
    deadline = time.monotonic() + budget
    try:
        return name, fn(region, keyword, deadline), None
    except Exception as exc:
        return name, [], str(exc)[:250]


def scrape_ugandan_directories(region_name, keyword):
    jobs = [
        ("Yellow Uganda", scrape_yellow, YELLOW_SOURCE_BUDGET_SECONDS),
        ("Find.ug", scrape_find, DEFAULT_SOURCE_BUDGET_SECONDS),
        ("Hotfrog Uganda", scrape_hotfrog, DEFAULT_SOURCE_BUDGET_SECONDS),
        ("FinderAfrica Uganda", scrape_finder, DEFAULT_SOURCE_BUDGET_SECONDS),
        ("Yellow Pages Uganda", scrape_yellowpages, DEFAULT_SOURCE_BUDGET_SECONDS),
        ("National SME Portal", scrape_sme, DEFAULT_SOURCE_BUDGET_SECONDS),
        ("KCCA Business Register", scrape_kcca, DEFAULT_SOURCE_BUDGET_SECONDS),
    ]
    all_records, counts, errors = [], {s: 0 for s in SOURCE_NAMES}, {}
    # Sequential source processing is deliberate: it protects Streamlit Cloud memory.
    # We give each source its own time budget so one blocked source cannot hold the others hostage.
    for name, fn, budget in jobs:
        source_name, recs, err = _run_source(name, fn, region_name, keyword, budget)
        recs = [r for r in recs if valid_business_name(r.get("Company Name", ""), keyword)]
        counts[source_name] = len(recs)
        all_records.extend(recs)
        if err:
            errors[source_name] = err
    result = deduplicate_records(all_records)[:MAX_TOTAL_RESULTS]
    scrape_ugandan_directories.last_source_counts = counts
    scrape_ugandan_directories.last_source_errors = errors
    return result

scrape_ugandan_directories.last_source_counts = {s: 0 for s in SOURCE_NAMES}
scrape_ugandan_directories.last_source_errors = {}
