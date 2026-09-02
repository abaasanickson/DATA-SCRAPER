import re
import time
import urllib.parse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import threading
import os
from bs4 import BeautifulSoup

# Optional browser fallback. The scraper still works with requests if Playwright/Chromium
# is unavailable in the deployment environment.
try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None

SEARCH_BUDGET_SECONDS = 90
SOURCE_BUDGET_SECONDS = 15
REQUEST_TIMEOUT = 4
BROWSER_TIMEOUT = 7000
MAX_RECORDS_PER_SOURCE = 10000
MAX_TOTAL_RESULTS = 50000
MAX_PAGES_PER_SOURCE = 500
MAX_DETAIL_PAGES_PER_SOURCE = 500
MAX_OSM_ELEMENTS_PER_QUERY = 5000
ENABLE_BROWSER_FALLBACK = os.getenv("ENABLE_BROWSER_FALLBACK", "0") == "1"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"

SOURCE_NAMES = [
    "Yellow Uganda", "Find.ug", "Hotfrog Uganda", "FinderAfrica Uganda",
    "Yellow Pages Uganda", "National SME Portal", "KCCA Business Register",
    "OpenStreetMap"
]

# Search expansion matters: a selected region is not a single city. Western Uganda,
# for example, is searched through its major districts/cities rather than one Kampala bbox.
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
    "Kampala": REGION_CITIES["Kampala"] + ["central division", "kawempe", "nakawa", "makindye", "rubaga", "lubaga", "ntinda", "kololo", "bukoto", "muyenga", "kabalagala"],
    "Wakiso": REGION_CITIES["Wakiso"] + ["wakiso district", "kyaliwajjala", "buloba", "busabala", "zanna", "lubowa"],
    "Mukono": REGION_CITIES["Mukono"] + ["mukono district", "sonde", "namanve", "nakisunga"],
    "Masaka": REGION_CITIES["Masaka"] + ["masaka district", "bukakata", "kijjabwemi"],
    "Jinja": REGION_CITIES["Jinja"] + ["jinja district", "mpumudde", "kimaka", "budhumbuli", "masese"],
    "Western Uganda": REGION_CITIES["Western Uganda"] + ["western uganda", "western region", "western"],
}

# Major Western Uganda search areas. OSM is not a registry; this is deliberately a
# multi-area discovery layer rather than pretending one box covers all Western Uganda.
REGION_BBOXES = {
    "Kampala": ["0.25,32.45,0.42,32.70"],
    "Wakiso": ["0.05,32.25,0.60,32.80"],
    "Mukono": ["0.20,32.55,0.60,32.95"],
    "Masaka": ["-0.50,31.55,-0.15,31.90"],
    "Jinja": ["0.30,33.05,0.60,33.45"],
    "Western Uganda": [
        "-0.80,30.40,-0.45,31.00",   # Mbarara / Ntungamo / Bushenyi corridor
        "0.45,29.95,0.80,30.45",      # Fort Portal / Kyenjojo
        "-0.45,29.75,-0.10,30.30",    # Kabale / Kisoro / Rukungiri
        "-0.10,29.75,0.30,30.35",     # Kasese / Rubirizi
        "0.70,30.00,1.30,31.20",      # Hoima / Kibaale / Kagadi
        "0.05,30.75,0.45,31.45",      # Masindi / Buliisa side
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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})

_BROWSER_LOCAL = threading.local()

def browser_html(url):
    """Headless-browser fallback for JS/challenge pages; used only when direct HTTP fails."""
    if sync_playwright is None:
        return None, url
    try:
        state=getattr(_BROWSER_LOCAL,"state",None)
        if state is None:
            pw=sync_playwright().start()
            executable="/usr/bin/chromium" if os.path.exists("/usr/bin/chromium") else None
            browser=pw.chromium.launch(headless=True, executable_path=executable) if executable else pw.chromium.launch(headless=True)
            page=browser.new_page(user_agent=USER_AGENT)
            _BROWSER_LOCAL.state=(pw,browser,page)
            state=_BROWSER_LOCAL.state
        page=state[2]
        page.goto(url,wait_until="domcontentloaded",timeout=BROWSER_TIMEOUT)
        page.wait_for_timeout(350)
        return page.content(), page.url
    except Exception:
        return None, url


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


def phones_from(text):
    if not text or clean_text(text) == "N/A":
        return []
    t = str(text)
    patterns = [
        r"\+256\s*(?:\(?\d{2,3}\)?)[\s./-]*\d{3,4}[\s./-]*\d{3,4}",
        r"0\s*\d{2,3}[\s./-]*\d{3,4}[\s./-]*\d{3,4}",
        r"\b04\s*\d{2,3}[\s./-]*\d{3,4}[\s./-]*\d{3,4}\b",
    ]
    found = []
    for p in patterns:
        for m in re.finditer(p, t):
            raw = re.sub(r"\s+", " ", m.group(0)).strip(" -|,")
            digits = re.sub(r"\D", "", raw)
            if digits.startswith("256") and len(digits) in range(10, 13):
                val = "+" + digits
            elif digits.startswith("0") and len(digits) in range(9, 11):
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


def address_from_text(text):
    t = clean_text(text)
    labels = r"(?:Address|Location|Physical Address|Plot|P\.O\. Box|Contacts?|Phone|Email|Website|Category|Listing Description)"
    m = re.search(rf"(?:Address|Physical Address)\s*:\s*(.*?)(?=\s+{labels}\s*:|$)", t, re.I)
    if m and clean_text(m.group(1)) != "N/A":
        return clean_text(m.group(1))
    return "N/A"


def labeled(text, label):
    t = clean_text(text)
    labels = ["Address", "Physical Address", "Location", "Phone", "Contacts", "Email", "Website", "Category", "Listing Description", "Description", "Overview"]
    stops = [x for x in labels if x.lower() != label.lower()]
    stop = "|".join(re.escape(x) for x in stops)
    m = re.search(rf"{re.escape(label)}\s*:\s*(.*?)(?=\s+(?:{stop})\s*:|$)", t, re.I)
    return clean_text(m.group(1)) if m else "N/A"


def make_record(name, region, keyword, source, url, phone="N/A", website="N/A", address="N/A", category="N/A", deals="N/A", email="N/A", rating="N/A", lat="N/A", lng="N/A", district="N/A"):
    return {
        "Company Name": clean_text(name),
        "Region": region,
        "Search Query": clean_text(keyword),
        "Category": clean_text(category),
        "Business Deals In": clean_text(deals),
        "Phone Contact": clean_text(phone),
        "Email": clean_text(email),
        "Website": clean_text(website),
        "Physical Address": clean_text(address),
        "District": clean_text(district),
        "Rating": clean_text(rating),
        "Lat": clean_text(lat),
        "Lng": clean_text(lng),
        "Data Source": source,
        "Source URL": url,
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
        record.get("Company Name", ""), record.get("Category", ""),
        record.get("Business Deals In", ""), record.get("Physical Address", ""),
        record.get("Source URL", "")
    ]))
    # Match the complete phrase OR at least one meaningful token. This is intentional:
    # "building materials" can be indexed as "hardware" or vice versa on directories.
    if q in hay:
        return True
    tokens = [x for x in q.split() if len(x) > 2]
    return bool(tokens) and any(x in hay for x in tokens)


def http_html(url):
    try:
        r = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if r.status_code == 200 and len(r.text) > 300:
            return r.text, r.url
    except requests.RequestException:
        pass
    # Browser rendering is deliberately opt-in on Community Cloud because Chromium
    # can consume hundreds of MB of RAM and cause the whole Streamlit process to be
    # killed during a broad Western Uganda search.
    if ENABLE_BROWSER_FALLBACK:
        return browser_html(url)
    return None, url


def soup_from(html):
    return BeautifulSoup(html, "lxml")


def extract_jsonld(soup):
    rows = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            import json
            data = json.loads(s.string or s.get_text())
            if isinstance(data, list): rows.extend(data)
            else: rows.append(data)
        except Exception:
            continue
    return rows


def detail_enrich(url, page_cache, deadline):
    if time.monotonic() >= deadline or not url or url in page_cache:
        return page_cache.get(url, {})
    html, final_url = http_html(url)
    if not html:
        page_cache[url] = {}
        return {}
    soup = soup_from(html)
    text = clean_text(soup.get_text(" ", strip=True))
    phones = phones_from(text)
    emails = emails_from(text)
    address = address_from_text(text)
    website = "N/A"
    category = "N/A"
    deals = "N/A"
    # Prefer structured LocalBusiness/Organization data when a directory publishes it.
    for obj in extract_jsonld(soup):
        if not isinstance(obj, dict):
            continue
        items = [obj] + [x for x in obj.get("@graph", []) if isinstance(x, dict)] if isinstance(obj.get("@graph"), list) else [obj]
        for item in items:
            if item.get("telephone"): phones.extend(phones_from(item.get("telephone")))
            if item.get("email"): emails.extend(emails_from(item.get("email")))
            if address == "N/A" and isinstance(item.get("address"), dict):
                ad=item.get("address", {})
                address=clean_text(", ".join(str(ad.get(k)) for k in ["streetAddress","addressLocality","addressRegion","postalCode","addressCountry"] if ad.get(k)))
            if website == "N/A" and item.get("url"):
                website=clean_text(item.get("url"))
            if category == "N/A" and item.get("category"):
                category=clean_text(item.get("category"))
            if deals == "N/A" and item.get("description"):
                deals=clean_text(item.get("description"))
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if href.startswith("tel:"):
            phones.extend(phones_from(urllib.parse.unquote(href[4:])))
        if href.startswith("mailto:"):
            emails.extend(emails_from(urllib.parse.unquote(href[7:])))
        if href.startswith("http") and not any(x in href.lower() for x in ["yellow.ug", "find.ug", "hotfrog", "finderafrica", "yellowpages-uganda"]):
            if clean_text(a.get_text(" ", strip=True)).lower() in {"website", "visit website", "web site"}:
                website = href
                break
    if category == "N/A": category = labeled(text, "Category")
    if deals == "N/A": deals = labeled(text, "Listing Description")
    if deals == "N/A": deals = labeled(text, "Description")
    if deals == "N/A": deals = labeled(text, "Overview")
    if address == "N/A":
        # Common Uganda directory address forms when no explicit "Address:" label exists.
        candidates = re.findall(r"(?:Plot\s+[^|]{4,160}|P\.O\.\s*Box\s+[^|]{3,80}|[A-Za-z0-9 .'-]+\s+Road,?\s+(?:Kampala|Jinja|Wakiso|Mukono|Mbarara|Uganda)[^|]{0,100})", text, re.I)
        if candidates:
            address=clean_text(candidates[0])
    page_cache[url] = {
        "phone": phone_from(" | ".join(phones)),
        "email": email_from(" | ".join(emails)),
        "address": address,
        "website": website,
        "category": category,
        "deals": deals,
        "url": final_url,
    }
    return page_cache[url]


def listing_link_candidates(soup, base, markers):
    out=[]; seen=set()
    for a in soup.find_all("a", href=True):
        href=urllib.parse.urljoin(base,a.get("href",""))
        text=clean_text(a.get_text(" ",strip=True))
        low=href.lower()
        if any(m in low for m in markers) and len(text)>2 and href not in seen:
            seen.add(href); out.append((href,a))
    return out


def parse_card(a, region, keyword, source, base, page_cache, deadline):
    node=a
    best=None
    for _ in range(8):
        node=getattr(node,"parent",None)
        if not node: break
        txt=clean_text(node.get_text(" ",strip=True))
        if len(txt)>len(clean_text(a.get_text(" ",strip=True))) and len(txt)<3500:
            if any(x in txt.lower() for x in ["address", "phone", "contacts", "category", "listing description", "overview"]):
                best=node; break
    card=best or a.parent
    text=clean_text(card.get_text(" ",strip=True))
    name=clean_text(a.get_text(" ",strip=True))
    address=address_from_text(text)
    phone=phone_from(text)
    email=email_from(text)
    category=labeled(text,"Category")
    deals=labeled(text,"Listing Description")
    if deals=="N/A": deals=labeled(text,"Description")
    if deals=="N/A": deals=labeled(text,"Overview")
    website="N/A"
    for x in card.find_all("a",href=True):
        h=x.get("href","")
        if h.startswith("tel:"): phone=unique_join([phone,phone_from(urllib.parse.unquote(h[4:]))])
        if h.startswith("mailto:"): email=unique_join([email,email_from(urllib.parse.unquote(h[7:]))])
        if h.startswith("http") and clean_text(x.get_text(" ",strip=True)).lower() in {"website","visit website","web site"}:
            website=h
    if (phone=="N/A" or address=="N/A" or deals=="N/A") and time.monotonic()<deadline:
        d=detail_enrich(base if "/company/" in base else base, page_cache, deadline)
    else:
        d={}
    # In normal calls, base is the detail URL. The source parsers pass that explicitly.
    phone=unique_join([phone,d.get("phone")])
    email=unique_join([email,d.get("email")])
    address=d.get("address") if address=="N/A" else address
    website=d.get("website") if website=="N/A" else website
    category=d.get("category") if category=="N/A" else category
    deals=d.get("deals") if deals=="N/A" else deals
    return make_record(name,region,keyword,source,base,phone,website,address,category,deals,email)


def next_page(soup,current):
    for a in soup.find_all("a",href=True):
        txt=norm(a.get_text(" ",strip=True))
        rel=" ".join(a.get("rel",[])).lower()
        if txt in {"next","next page",">","→","older posts"} or "next" in rel:
            return urllib.parse.urljoin(current,a["href"])
    return None


def parse_yellow_page(soup, region, keyword, url, source="Yellow Uganda"):
    recs=[]
    links=listing_link_candidates(soup,url,["/company/"])
    for href,a in links[:MAX_RECORDS_PER_SOURCE]:
        card=a
        for _ in range(6):
            if not card.parent: break
            card=card.parent
            txt=clean_text(card.get_text(" ",strip=True))
            if len(txt)<2500 and ("address" in txt.lower() or "phone" in txt.lower()): break
        text=clean_text(card.get_text(" ",strip=True))
        name=clean_text(a.get_text(" ",strip=True))
        address=labeled(text,"Address")
        if address=="N/A": address=address_from_text(text)
        phone=phone_from(text)
        email=email_from(text)
        category=labeled(text,"Category")
        deals=labeled(text,"Listing Description")
        if deals=="N/A": deals=labeled(text,"Description")
        recs.append(make_record(name,region,keyword,source,href,phone,"N/A",address,category,deals,email))
    return recs


def yellow_category_candidates(region, keyword, page_cache, deadline):
    candidates=[]
    cities=REGION_CITIES.get(region,[slug(region)])
    q=norm(keyword)

    def inspect_city(city):
        if time.monotonic()>=deadline: return []
        u=f"https://www.yellow.ug/location/{slug(city)}/list%3Acategories"
        html,final=http_html(u)
        if not html: return []
        soup=soup_from(html); found=[]
        for a in soup.find_all("a",href=True):
            h=urllib.parse.urljoin(final,a["href"]); txt=norm(a.get_text(" ",strip=True))
            if "/category/" not in h.lower(): continue
            score=5 if q and q in txt else (3 if q and any(t in txt for t in q.split() if len(t)>2) else 0)
            if score: found.append((score,h))
        return found

    # Category indexes are independent. Parallel discovery prevents Western Uganda's
    # many towns from consuming the whole Yellow Uganda source time slice.
    workers=min(4,max(1,len(cities)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures=[ex.submit(inspect_city,c) for c in cities]
        for fut in as_completed(futures):
            try: candidates.extend(fut.result())
            except Exception: pass
            if time.monotonic()>=deadline: break
    direct=[]
    for city in cities:
        direct.append(f"https://www.yellow.ug/category/{slug(keyword)}/city%3A{slug(city)}")
    direct.append(f"https://www.yellow.ug/category/{slug(keyword)}")
    seen=set(); out=[]
    for _,u in sorted(candidates,key=lambda x:-x[0])+[(1,x) for x in direct]:
        if u not in seen:
            seen.add(u); out.append(u)
    return out[:20]


def scrape_yellow(region,keyword,deadline):
    out=[]; seen=set(); details={}
    starts=yellow_category_candidates(region,keyword,details,deadline)
    for start in starts:
        url=start; pages=0
        while url and pages<MAX_PAGES_PER_SOURCE and time.monotonic()<deadline and len(out)<MAX_RECORDS_PER_SOURCE:
            if url in seen: break
            seen.add(url); pages+=1
            html,final=http_html(url)
            if not html: break
            soup=soup_from(html)
            links=listing_link_candidates(soup,final,["/company/"])
            for href,a in links:
                if len(out)>=MAX_RECORDS_PER_SOURCE: break
                # Parse card first; enrich missing contacts/address from the actual profile URL.
                card=a
                for _ in range(7):
                    if not card.parent: break
                    card=card.parent
                    txt=clean_text(card.get_text(" ",strip=True))
                    if len(txt)<3000 and any(k in txt.lower() for k in ["address","phone","category"]): break
                text=clean_text(card.get_text(" ",strip=True))
                name=clean_text(a.get_text(" ",strip=True))
                address=labeled(text,"Address")
                phone=phone_from(text)
                email=email_from(text)
                category=labeled(text,"Category")
                deals=labeled(text,"Listing Description")
                if deals=="N/A": deals=labeled(text,"Description")
                if phone=="N/A" or address=="N/A" or deals=="N/A":
                    d=detail_enrich(href,details,deadline)
                    phone=unique_join([phone,d.get("phone")]); email=unique_join([email,d.get("email")])
                    if address=="N/A": address=d.get("address","N/A")
                    if category=="N/A": category=d.get("category","N/A")
                    if deals=="N/A": deals=d.get("deals","N/A")
                    website=d.get("website","N/A")
                else: website="N/A"
                out.append(make_record(name,region,keyword,"Yellow Uganda",href,phone,website,address,category,deals,email))
            nxt=next_page(soup,final)
            if nxt and nxt!=url: url=nxt
            else:
                # Yellow often uses explicit numbered pagination; find a higher page link.
                nums=[]
                for a in soup.find_all("a",href=True):
                    h=urllib.parse.urljoin(final,a["href"])
                    m=re.search(r"/(\d+)(?:/)?(?:\?|$)",h)
                    if "/category/" in h and m: nums.append((int(m.group(1)),h))
                bigger=[h for n,h in nums if n>pages]
                url=min(bigger,key=lambda h:int(re.search(r"/(\d+)(?:/)?(?:\?|$)",h).group(1))) if bigger else None
    return out


def generic_directory_crawl(start_urls, region, keyword, source, markers, deadline, detail_markers=True):
    out=[]; seen_pages=set(); seen_records=set(); details={}
    queue=list(start_urls)
    while queue and len(out)<MAX_RECORDS_PER_SOURCE and time.monotonic()<deadline and len(seen_pages)<MAX_PAGES_PER_SOURCE:
        url=queue.pop(0)
        if url in seen_pages: continue
        seen_pages.add(url)
        html,final=http_html(url)
        if not html: continue
        soup=soup_from(html)
        links=listing_link_candidates(soup,final,markers)
        for href,a in links:
            if len(out)>=MAX_RECORDS_PER_SOURCE: break
            if href in seen_records: continue
            low_href=href.lower()
            # Do not mistake category/tag/pagination pages for individual businesses.
            if any(x in low_href for x in ["/listings/category/", "/listings/tags/", "/listings/page/", "/listing-category/", "/category/"]):
                continue
            name=clean_text(a.get_text(" ",strip=True))
            if name.lower() in {"view profile","read more","details","home","contact","about us"}: continue
            # Build record directly from the listing card.
            card=a
            for _ in range(7):
                if not card.parent: break
                card=card.parent
                txt=clean_text(card.get_text(" ",strip=True))
                if len(txt)<3500 and any(k in txt.lower() for k in ["address","phone","contacts","category","overview"]): break
            text=clean_text(card.get_text(" ",strip=True))
            address=address_from_text(text)
            if address=="N/A": address=labeled(text,"Location")
            phone=phone_from(text)
            email=email_from(text)
            category=labeled(text,"Category")
            deals=labeled(text,"Listing Description")
            if deals=="N/A": deals=labeled(text,"Description")
            if deals=="N/A": deals=labeled(text,"Overview")
            website="N/A"
            # Enrich missing critical fields from profile page.
            if detail_markers and (phone=="N/A" or address=="N/A" or deals=="N/A") and time.monotonic()<deadline:
                d=detail_enrich(href,details,deadline)
                phone=unique_join([phone,d.get("phone")]); email=unique_join([email,d.get("email")])
                if address=="N/A": address=d.get("address","N/A")
                if category=="N/A": category=d.get("category","N/A")
                if deals=="N/A": deals=d.get("deals","N/A")
                website=d.get("website","N/A")
            rec=make_record(name,region,keyword,source,href,phone,website,address,category,deals,email)
            if keyword_match(rec,keyword) and (address=="N/A" or region_match(address,region) or region_match(text,region)):
                out.append(rec); seen_records.add(href)
        nxt=next_page(soup,final)
        if nxt and nxt not in seen_pages: queue.append(nxt)
        # Queue plausible category/listing pages found on the current page. This helps
        # sources whose search endpoint redirects to category pages.
        for a in soup.find_all("a",href=True):
            h=urllib.parse.urljoin(final,a["href"])
            txt=norm(a.get_text(" ",strip=True))
            if any(m in h.lower() for m in markers) and h not in seen_pages and h not in queue:
                if keyword_match({"Company Name":txt,"Category":txt,"Business Deals In":txt,"Physical Address":txt,"Source URL":h},keyword):
                    queue.append(h)
    return out


def scrape_find(region,keyword,deadline):
    q=urllib.parse.quote(keyword)
    starts=[
        f"https://find.ug/?s={q}",
        f"https://find.ug/listings/?s={q}",
        f"https://find.ug/listing-category/{slug(keyword)}/",
        "https://find.ug/all-listings/",
    ]
    return generic_directory_crawl(starts,region,keyword,"Find.ug",["/listing/"],deadline)


def scrape_hotfrog(region,keyword,deadline):
    out=[]
    for city in REGION_CITIES.get(region,[slug(region)]):
        if time.monotonic()>=deadline or len(out)>=MAX_RECORDS_PER_SOURCE: break
        base=f"https://www.hotfrog.ug/search/{slug(city)}/{slug(keyword)}"
        recs=generic_directory_crawl([base],region,keyword,"Hotfrog Uganda",["/company/"],deadline)
        out.extend(recs)
    return out[:MAX_RECORDS_PER_SOURCE]


def scrape_finder(region,keyword,deadline):
    starts=[
        f"https://finderafrica.com/?s={urllib.parse.quote(keyword)}",
        f"https://finderafrica.com/listing-category/{slug(keyword)}/",
        "https://finderafrica.com/location/business-directory-uganda/",
    ]
    return generic_directory_crawl(starts,region,keyword,"FinderAfrica Uganda",["/listing/"],deadline)


def scrape_yellowpages(region,keyword,deadline):
    starts=[
        f"https://www.yellowpages-uganda.com/?s={urllib.parse.quote(keyword)}",
        "https://www.yellowpages-uganda.com/location/",
    ]
    # Search category/tag links discovered from the location index, then crawl listing pages.
    html,final=http_html("https://www.yellowpages-uganda.com/location/")
    if html:
        soup=soup_from(html); q=norm(keyword)
        for a in soup.find_all("a",href=True):
            h=urllib.parse.urljoin(final,a["href"]); t=norm(a.get_text(" ",strip=True))
            if any(x in h.lower() for x in ["/listings/category/","/listings/tags/"]) and q and any(tok in t for tok in q.split() if len(tok)>2):
                starts.insert(0,h)
    return generic_directory_crawl(starts,region,keyword,"Yellow Pages Uganda",["/listings/"],deadline)


def scrape_sme(region,keyword,deadline):
    # National SME Portal is valuable because it exposes Region/Sub-region/District/Sector
    # filters and a paginated national business directory. Its visible table contains names,
    # sectors and districts; detail/contact enrichment is attempted when links are exposed.
    starts=[
        f"https://mybusiness.go.ug/Reports/SMEDirectory?Filters.SearchTerm={urllib.parse.quote(keyword)}",
        "https://mybusiness.go.ug/Reports/SMEDirectory",
    ]
    out=[]; seen=set(); pages=0
    for start in starts:
        url=start
        while url and pages<MAX_PAGES_PER_SOURCE and len(out)<MAX_RECORDS_PER_SOURCE and time.monotonic()<deadline:
            if url in seen: break
            seen.add(url); pages+=1
            html,final=http_html(url)
            if not html: break
            soup=soup_from(html)
            for row in soup.select("tr"):
                cells=[clean_text(x.get_text(" ",strip=True)) for x in row.find_all(["td","th"])]
                if len(cells)<3: continue
                name=cells[1] if cells[0].isdigit() else cells[0]
                sector=cells[2] if cells[0].isdigit() else cells[1]
                district=cells[3] if cells[0].isdigit() and len(cells)>3 else (cells[2] if len(cells)>2 else "N/A")
                text=" | ".join(cells)
                rec=make_record(name,region,keyword,"National SME Portal",final,"N/A","N/A","N/A",sector,sector,"N/A",district=district)
                district_norm=norm(district)
                allowed=REGION_DISTRICTS.get(region,set())
                region_ok=any(d and (d==district_norm or d in district_norm) for d in allowed)
                # The portal's SearchTerm is the authoritative keyword filter for this source.
                # If it is unavailable, retain rows whose name/sector contains the requested term.
                search_filtered = "Filters.SearchTerm=" in start and urllib.parse.unquote(start.split("Filters.SearchTerm=",1)[1]).strip() != ""
                if region_ok and (search_filtered or keyword_match(rec,keyword) or norm(keyword) in norm(sector) or norm(keyword) in norm(name)):
                    out.append(rec)
                    if len(out)>=MAX_RECORDS_PER_SOURCE: break
            # Follow explicit next page links.
            nxt=next_page(soup,final)
            if not nxt:
                # SME portal uses p=N query parameters.
                m=re.search(r"[?&]p=(\d+)",final)
                if m:
                    n=int(m.group(1))+1
                    nxt=re.sub(r"([?&])p=\d+", lambda m:m.group(1)+f"p={n}", final)
            url=nxt
    return out[:MAX_RECORDS_PER_SOURCE]


def scrape_kcca(region,keyword,deadline):
    if region!="Kampala" or time.monotonic()>=deadline: return []
    return generic_directory_crawl([
        f"https://kcca.go.ug/businesses?business_name={urllib.parse.quote(keyword)}&business_nature={urllib.parse.quote(keyword)}",
        "https://kcca.go.ug/businesses"
    ],region,keyword,"KCCA Business Register",["/businesses"],deadline,detail_markers=False)


def osm_query(bbox, keyword):
    """Build a compact Overpass query.

    The previous version generated hundreds/thousands of regex clauses for each
    Western Uganda bounding box. That was both slow and memory-heavy. We instead
    search the most useful free-text fields plus common category fields in a small
    number of queries.
    """
    q = norm(keyword)
    tokens = [t for t in q.split() if len(t) >= 3][:4]
    terms = list(dict.fromkeys([q] + tokens))
    blocks = []
    for term in terms:
        safe = re.sub(r"[^a-zA-Z0-9 _-]", "", term).strip()
        if not safe:
            continue
        pattern = re.escape(safe).replace(r"\ ", r"[ _-]+")
        for key in ["name", "brand", "operator", "description", "shop", "amenity", "office", "craft", "industrial", "healthcare", "tourism"]:
            blocks.append(f'nwr["{key}"~"{pattern}",i]({bbox});')
    return "[out:json][timeout:20];(\n" + "\n".join(blocks) + "\n);out center tags;"


def fetch_osm_grid_data(region_name, keyword):
    out = []
    seen = set()
    deadline = time.monotonic() + SEARCH_BUDGET_SECONDS

    for bbox in REGION_BBOXES.get(region_name, []):
        if time.monotonic() >= deadline or len(out) >= MAX_RECORDS_PER_SOURCE:
            break
        query = osm_query(bbox, keyword)
        for endpoint in OVERPASS:
            if time.monotonic() >= deadline:
                break
            try:
                remaining = max(5, min(25, int(deadline - time.monotonic())))
                r = requests.post(endpoint, data=query, headers={"User-Agent": USER_AGENT}, timeout=remaining)
                if r.status_code != 200:
                    continue
                elements = r.json().get("elements", [])[:MAX_OSM_ELEMENTS_PER_QUERY]
                for el in elements:
                    tags = el.get("tags", {})
                    name = clean_text(tags.get("name"))
                    if name == "N/A":
                        continue
                    c = el.get("center", {})
                    lat = el.get("lat", c.get("lat", "N/A"))
                    lng = el.get("lon", c.get("lon", "N/A"))
                    addr = ", ".join(str(tags[k]) for k in [
                        "addr:housenumber", "addr:street", "addr:place", "addr:suburb",
                        "addr:city", "addr:district", "addr:postcode"
                    ] if tags.get(k))
                    category = next((clean_text(tags.get(k)) for k in [
                        "shop", "amenity", "office", "craft", "industrial", "healthcare", "tourism"
                    ] if tags.get(k)), "N/A")
                    deals = clean_text(tags.get("description") or tags.get("operator") or tags.get("brand") or category)
                    district = clean_text(tags.get("addr:district") or tags.get("is_in:district") or "N/A")
                    rec = make_record(
                        name, region_name, keyword, "OpenStreetMap", endpoint,
                        tags.get("phone") or tags.get("contact:phone", "N/A"),
                        tags.get("website") or tags.get("contact:website", "N/A"),
                        addr or "N/A", category, deals,
                        tags.get("email") or tags.get("contact:email", "N/A"),
                        tags.get("rating", "N/A"), lat, lng, district
                    )
                    if not keyword_match(rec, keyword):
                        continue
                    key = (norm(name), norm(addr) or norm(f"{lat}|{lng}"))
                    if key not in seen:
                        seen.add(key)
                        out.append(rec)
                    if len(out) >= MAX_RECORDS_PER_SOURCE:
                        break
                # A successful endpoint is enough for this bbox; move to the next
                # geographic area instead of duplicating every object from both mirrors.
                break
            except Exception:
                continue

    fetch_osm_grid_data.last_count = len(out)
    return out[:MAX_RECORDS_PER_SOURCE]


fetch_osm_grid_data.last_count=0


def identity(r):
    name=norm(r.get("Company Name")); addr=norm(r.get("Physical Address")); phone=re.sub(r"\D","",clean_text(r.get("Phone Contact")))
    if not name: return None
    if addr and addr!="n a": return ("na",name,addr)
    if phone: return ("np",name,phone)
    return ("n",name)


def merge(a,b):
    for k in ["Phone Contact","Email","Website","Physical Address","Rating","Lat","Lng","Category","Business Deals In"]:
        if clean_text(b.get(k))!="N/A":
            a[k]=unique_join([a.get(k),b.get(k)])
    for k in ["Data Source","Source URL","Search Query"]:
        a[k]=unique_join([a.get(k),b.get(k)])
    return a


def deduplicate_records(records):
    unique=OrderedDict()
    for r in records:
        key=identity(r)
        if not key: continue
        unique[key]=merge(unique[key],r) if key in unique else dict(r)
    return list(unique.values())


def _run_source(name, fn, region, keyword):
    deadline=time.monotonic()+SOURCE_BUDGET_SECONDS
    try:
        return name, fn(region,keyword,deadline), None
    except Exception as exc:
        return name, [], str(exc)[:250]


def scrape_ugandan_directories(region_name,keyword):
    jobs=[
        ("Yellow Uganda",scrape_yellow),
        ("Find.ug",scrape_find),
        ("Hotfrog Uganda",scrape_hotfrog),
        ("FinderAfrica Uganda",scrape_finder),
        ("Yellow Pages Uganda",scrape_yellowpages),
        ("National SME Portal",scrape_sme),
        ("KCCA Business Register",scrape_kcca),
    ]
    all_records=[]; counts={s:0 for s in SOURCE_NAMES}; errors={}
    # Sources are independent. Run them concurrently so one slow site does not consume
    # the entire search window. Each worker still has its own hard source deadline.
    with ThreadPoolExecutor(max_workers=min(3, len(jobs))) as ex:
        futures=[ex.submit(_run_source,n,f,region_name,keyword) for n,f in jobs]
        for fut in as_completed(futures):
            name,recs,err=fut.result(); counts[name]=len(recs); all_records.extend(recs)
            if err: errors[name]=err
    # Never replace missing physical addresses with the region name.
    cleaned=[]
    for r in all_records:
        r["Physical Address"]=clean_text(r.get("Physical Address"))
        if keyword_match(r,keyword):
            # Keep records with N/A address if the source search itself was region-scoped.
            cleaned.append(r)
    result=deduplicate_records(cleaned)[:MAX_TOTAL_RESULTS]
    scrape_ugandan_directories.last_source_counts=counts
    scrape_ugandan_directories.last_source_errors=errors
    return result

scrape_ugandan_directories.last_source_counts={s:0 for s in SOURCE_NAMES}
scrape_ugandan_directories.last_source_errors={}
