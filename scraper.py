import re
import time
import urllib.parse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# Coverage is deliberately capped for stability, not for normal results. The
# scraper stops when sources/pages are exhausted or a safety deadline is reached.
MAX_TOTAL_RESULTS = 3000
MAX_RECORDS_PER_SOURCE = 1500
MAX_PAGES_PER_SOURCE = 250
SEARCH_BUDGET_SECONDS = 120
SOURCE_BUDGET_SECONDS = 60
ENRICH_BUDGET_SECONDS = 45
REQUEST_TIMEOUT = 7
BROWSER_TIMEOUT = 10000
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"

# Commercial towns/areas used for location-by-location searching. This is the
# key regional-recall improvement: a region is never treated as one search box.
REGION_CITIES = {
    "Kampala": ["kampala", "kawempe", "nakawa", "makindye", "rubaga", "lubaga", "ntinda", "kololo", "bukoto", "muyenga", "kabalagala", "katwe", "makerere", "bugolobi"],
    "Wakiso": ["wakiso", "kira", "nansana", "entebbe", "kajansi", "namugongo", "kyaliwajjala", "bweyogerere", "buloba", "kasangati", "gayaza", "busabala", "zanna", "lubowa", "kajjansi", "matugga", "kakiri", "ssisa", "mukono road"],
    "Mukono": ["mukono", "seeta", "sonde", "namanve", "lugazi", "nakisunga", "nkokonjeru", "seeta town", "namawojjolo", "buikwe"],
    "Masaka": ["masaka", "nyendo", "bukakata", "kijjabwemi", "katwe masaka", "buwunga", "buwama"],
    "Jinja": ["jinja", "bugembe", "mpumudde", "kimaka", "walukuba", "budhumbuli", "masese", "kakira", "iganga road jinja"],
    "Western Uganda": ["mbarara", "mbarara city", "rwampara", "kinoni", "sheema", "kabwohe", "bushenyi", "bushenyi ishaka", "ishaka", "mitooma", "buhweju", "ibanda", "kiruhura", "isingiro", "ntungamo", "rushere", "rubindi", "nyakayojo"],
}

REGION_DISTRICTS = {
    "Kampala": ["Kampala", "Kampala City"],
    "Wakiso": ["Wakiso"],
    "Mukono": ["Mukono", "Buikwe"],
    "Masaka": ["Masaka", "Masaka City"],
    "Jinja": ["Jinja", "Jinja City", "Bugiri"],
    "Western Uganda": ["Mbarara", "Mbarara City", "Rwampara", "Sheema", "Bushenyi", "Mitooma", "Buhweju", "Ibanda", "Kiruhura", "Isingiro", "Ntungamo"],
}

REGION_AREAS = {k: list(dict.fromkeys(v + REGION_DISTRICTS.get(k, []))) for k, v in REGION_CITIES.items()}

REGION_BBOXES = {
    "Kampala": ["0.25,32.45,0.42,32.70"],
    "Wakiso": ["0.05,32.30,0.60,32.75"],
    "Mukono": ["0.20,32.60,0.55,32.90"],
    "Masaka": ["-0.45,31.60,-0.20,31.85"],
    "Jinja": ["0.35,33.15,0.55,33.35"],
    "Western Uganda": [
        "-0.70,30.55,-0.55,30.75", "-0.78,30.40,-0.63,30.58",
        "-0.82,30.00,-0.65,30.25", "-0.78,29.70,-0.60,29.95",
        "-0.65,30.00,-0.48,30.20", "-0.15,30.00,0.05,30.25",
        "-0.55,30.55,-0.35,30.80", "-0.95,30.95,-0.72,31.20",
        "-1.05,30.35,-0.82,30.60",
    ],
}

OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

SOURCE_NAMES = [
    "Yellow Uganda", "Find.ug", "FinderAfrica Uganda", "Yellow Pages Uganda",
    "Hotfrog Uganda", "National SME Portal", "BUBU Uganda Directory",
    "KCCA Business Register", "OpenStreetMap"
]

KEYWORD_EXPANSIONS = {
    "hardware": ["hardware", "building materials", "construction materials", "building supplies", "hardware stores"],
    "store": ["store", "shop", "retail", "supermarket", "general merchandise", "trading", "mart"],
    "shop": ["shop", "store", "retail", "trading", "general merchandise"],
    "pharmacy": ["pharmacy", "chemist", "drugstore", "drugs", "medical"],
    "school": ["school", "academy", "college", "education", "nursery", "primary", "secondary"],
    "restaurant": ["restaurant", "cafe", "food", "dining", "takeaway"],
    "hotel": ["hotel", "guest house", "lodge", "accommodation", "hostel"],
    "supermarket": ["supermarket", "grocery", "food retailer", "retail", "mart"],
    "clinic": ["clinic", "medical centre", "health centre", "hospital", "doctor"],
    "bank": ["bank", "microfinance", "financial", "credit", "savings"],
    "salon": ["salon", "beauty", "barber", "hair", "spa"],
    "garage": ["garage", "auto repair", "mechanic", "motor vehicle", "car repair"],
}

def keyword_terms(keyword):
    k = norm(keyword)
    base = KEYWORD_EXPANSIONS.get(k, [k])
    return list(dict.fromkeys([x for x in base if x]))


def clean_text(v):
    if v is None:
        return "N/A"
    v = re.sub(r"\s+", " ", str(v)).strip()
    return v if v else "N/A"


def clean_physical_address(v):
    text = clean_text(v)
    if text == "N/A":
        return "N/A"
    text = re.sub(r"^(?:physical\s+)?(?:address|location)\s*:\s*", "", text, flags=re.I)
    # Required client-facing rule: Uganda is the hard end of the address.
    m = re.search(r"\bUganda\b", text, flags=re.I)
    if m:
        text = text[:m.end()]
    else:
        text = re.split(r"\s+(?:View\s+Profile|View\s+Map|Get\s+Directions|Contact\s+number|Mobile\s+phone|Company\s+manager|Company\s+description|Listed\s+in\s+categories|Website\s+address|Working\s+hours)\b", text, maxsplit=1, flags=re.I)[0]
        text = re.split(r"\s+(?:tel:)?\+?256[\s()./-]*\d{2,3}[\s()./-]*\d{3,4}[\s()./-]*\d{3,4}\b", text, maxsplit=1, flags=re.I)[0]
        text = re.split(r"\s+0\d{2,3}[\s()./-]*\d{3,4}[\s()./-]*\d{3,4}\b", text, maxsplit=1)[0]
        text = re.split(r"\s+[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", text, maxsplit=1, flags=re.I)[0]
    text = re.sub(r"\s+", " ", text).strip(" ,;|-")
    return text if text else "N/A"


def norm(v):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", clean_text(v).lower())).strip()


def slug(v):
    return re.sub(r"-+", "-", norm(v).replace(" ", "-"))


def phones_from(text):
    text = clean_text(text)
    patterns = [
        r"\+256[\s()./-]*\d{2,3}[\s()./-]*\d{3,4}[\s()./-]*\d{3,4}",
        r"0\d{2,3}[\s()./-]*\d{3,4}[\s()./-]*\d{3,4}",
        r"\(0\d{2,3}\)[\s./-]*\d{3,4}[\s./-]*\d{3,4}",
    ]
    found = []
    for p in patterns:
        for m in re.finditer(p, text):
            v = clean_text(m.group(0)); digits = re.sub(r"\D", "", v)
            if len(digits) >= 9 and v not in found:
                found.append(v)
    return found


def phone_from(text):
    return " | ".join(phones_from(text)) or "N/A"


def phone_links(card):
    found = []
    for a in card.find_all("a", href=True):
        href = clean_text(a.get("href"))
        if href.lower().startswith("tel:"):
            for phone in phones_from(urllib.parse.unquote(href[4:])):
                if phone not in found:
                    found.append(phone)
    return " | ".join(found) or "N/A"


def email_from(text):
    m = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", clean_text(text), re.I)
    return m.group(0) if m else "N/A"


def make_record(name, region, keyword, source, url, phone="N/A", website="N/A", address="N/A", category="N/A", deals="N/A", email="N/A", rating="N/A", lat="N/A", lng="N/A"):
    return {
        "Company Name": clean_text(name), "Region": region, "Search Query": clean_text(keyword),
        "Category": clean_text(category), "Business Deals In": clean_text(deals),
        "Phone Contact": clean_text(phone), "Email": clean_text(email), "Website": clean_text(website),
        "Physical Address": clean_physical_address(address), "Rating": clean_text(rating),
        "Lat": clean_text(lat), "Lng": clean_text(lng), "Data Source": source, "Source URL": url,
    }


def region_match(text, region):
    a = norm(text)
    if not a or a == "n a":
        return True
    terms = [norm(x) for x in REGION_AREAS.get(region, [region]) if norm(x)]
    return any(t in a for t in terms)


def keyword_match(record, keyword):
    hay = norm(" ".join([record.get("Company Name", ""), record.get("Category", ""), record.get("Business Deals In", "")]))
    tokens = [t for term in keyword_terms(keyword) for t in norm(term).split() if len(t) >= 3]
    return any(t in hay for t in tokens) if tokens else True


def http_html(url):
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200 and len(r.text) > 500:
            return r.text
    except requests.RequestException:
        pass
    return None


def browser_html(page, url):
    if not page:
        return None
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
        page.wait_for_timeout(600)
        return page.content()
    except Exception:
        return None


def get_html(page, url):
    return http_html(url) or browser_html(page, url)


def nearest_card(anchor, max_levels=8):
    node = anchor; best = None
    for _ in range(max_levels):
        if not node.parent: break
        node = node.parent
        txt = clean_text(node.get_text(" ", strip=True))
        if len(txt) >= len(clean_text(anchor.get_text(" ", strip=True))) and len(txt) <= 2500:
            if any(label in txt.lower() for label in ["address", "phone", "category", "listing description", "view profile"]):
                best = node; break
    return best or anchor.parent


def field_from_dom(card, label):
    target = label.lower().rstrip(":")
    for node in card.find_all(["div", "span", "p", "li", "td", "th"]):
        txt = clean_text(node.get_text(" ", strip=True)); low = txt.lower()
        if low.startswith(target + ":"):
            return clean_text(txt.split(":", 1)[1]) or "N/A"
    return "N/A"


def labeled_value(text, label, stop_labels=None):
    t = clean_text(text)
    stops = stop_labels or ["Phone", "Address", "Email", "Website", "Category", "Listing Description", "Business profile", "Rating"]
    stop = "|".join(re.escape(x) for x in stops if x.lower() != label.lower())
    m = re.search(rf"{re.escape(label)}\s*:?\s*(.*?)(?=\s+(?:{stop})\s*:?)", t, re.I) if stop else re.search(rf"{re.escape(label)}\s*:?\s*(.*)$", t, re.I)
    return clean_text(m.group(1)) if m else "N/A"


def extract_deals(card, text, category="N/A"):
    # Prefer descriptive text/products over simply echoing the search keyword.
    candidates = []
    for node in card.find_all(["p", "div", "li", "span"]):
        chunk = clean_text(node.get_text(" ", strip=True))
        low = chunk.lower()
        if len(chunk) >= 20 and not phone_from(chunk) and not any(x in low for x in ["view profile", "send enquiry", "get directions", "verified"]):
            candidates.append(chunk)
    for label, stops in [
        ("Company description", ["Listed in categories", "Company manager", "Establishment year", "Employees", "Reviews"]),
        ("Products and Services", ["Company Info", "Location", "Opening Hours", "Reviews"]),
        ("Listing Description", ["Category", "Address", "Email", "Website", "Phone"]),
        ("Description", ["Category", "Address", "Email", "Website", "Phone"]),
        ("Products", ["Name", "Email Address", "In Business since"]),
    ]:
        v = labeled_value(text, label, stops)
        if v != "N/A": return v
    if candidates:
        return max(candidates, key=len)
    return category if category != "N/A" else "N/A"


def enrich_profile_http(record, deadline):
    if time.monotonic() >= deadline: return record
    url = record.get("Source URL", "N/A")
    if url in {"N/A", ""} or not any(x in url.lower() for x in ["/company/", "/listing/", "/places/"]):
        return record
    html = http_html(url)
    if not html: return record
    soup = BeautifulSoup(html, "html.parser")
    text = clean_text(soup.get_text(" ", strip=True))
    if clean_text(record.get("Phone Contact")) == "N/A":
        ph = phone_links(soup)
        if ph == "N/A": ph = phone_from(text)
        if ph != "N/A": record["Phone Contact"] = ph
    if clean_text(record.get("Physical Address")) == "N/A":
        addr = field_from_dom(soup, "Address")
        if addr == "N/A": addr = field_from_dom(soup, "Location")
        if addr == "N/A":
            m = re.search(r"(?:Address|Location)\s+(.+?)(?=\s+(?:View Map|Get Directions|Contact number|Mobile phone|Website address|Working hours|Company manager|Company description|Listed in categories))", text, re.I)
            if m: addr = m.group(1)
        if addr != "N/A": record["Physical Address"] = clean_physical_address(addr)
    if clean_text(record.get("Business Deals In")) == "N/A" or norm(record.get("Business Deals In")) == norm(record.get("Category")):
        d = extract_deals(soup, text, clean_text(record.get("Category")))
        if d != "N/A": record["Business Deals In"] = d
    if clean_text(record.get("Email")) == "N/A": record["Email"] = email_from(text)
    if clean_text(record.get("Website")) == "N/A":
        for a in soup.find_all("a", href=True):
            h = clean_text(a.get("href"))
            if h.startswith("http") and not any(x in h.lower() for x in ["yellow.ug", "hotfrog", "finderafrica", "yellowpages-uganda", "find.ug", "bubuonlinenews"]):
                record["Website"] = h; break
    record["Physical Address"] = clean_physical_address(record.get("Physical Address"))
    return record


def parse_yellow_cards(soup, region, keyword, page_url):
    records=[]; seen=set()
    for a in soup.find_all("a", href=True):
        href=urllib.parse.urljoin("https://www.yellow.ug", a.get("href", "")); name=clean_text(a.get_text(" ", strip=True))
        if "/company/" not in href.lower() or len(name)<3 or name.lower() in {"view profile", "send enquiry"} or href in seen: continue
        seen.add(href); card=nearest_card(a); text=clean_text(card.get_text(" ", strip=True))
        address=field_from_dom(card,"Address"); category=field_from_dom(card,"Category")
        phone=phone_links(card); phone=phone if phone!="N/A" else phone_from(text)
        deals=extract_deals(card,text,category)
        rec=make_record(name,region,keyword,"Yellow Uganda",href,phone,"N/A",address,category,deals,email_from(text))
        if keyword_match(rec,keyword) and region_match(address+" "+text,region): records.append(rec)
        if len(records)>=MAX_RECORDS_PER_SOURCE: break
    return records


def yellow_category_candidates(keyword, region):
    candidates=[]; qtokens=set(norm(keyword).split())
    for city in REGION_CITIES.get(region,[slug(region)]):
        candidates.append(f"https://www.yellow.ug/location/{slug(city)}/list%3Acategories")
        for term in keyword_terms(keyword):
            candidates.append(f"https://www.yellow.ug/category/{slug(term)}/city%3A{slug(city)}")
    # Generic fallbacks after location-specific URLs.
    for term in keyword_terms(keyword): candidates.append(f"https://www.yellow.ug/category/{slug(term)}")
    out=[]; seen=set()
    for u in candidates:
        if u not in seen: seen.add(u); out.append(u)
    return out[:90]


def scrape_yellow(page, region, keyword, deadline):
    out=[]; seen=set(); starts=yellow_category_candidates(keyword,region)
    for city in REGION_CITIES.get(region,[slug(region)]): starts.append(f"https://www.yellow.ug/location/{slug(city)}")
    for start in starts:
        for n in range(1,MAX_PAGES_PER_SOURCE+1):
            if time.monotonic()>=deadline or len(out)>=MAX_RECORDS_PER_SOURCE: break
            if n==1: url=start
            else:
                p=urllib.parse.urlsplit(start); path=p.path.rstrip("/")
                if "/city%3A" in path.lower() or "/city:" in path.lower():
                    bits=path.rsplit("/",1); path=f"{bits[0]}/{n}/{bits[1]}"
                else: path=f"{path}/{n}"
                url=urllib.parse.urlunsplit((p.scheme,p.netloc,path,"",""))
            if url in seen: continue
            seen.add(url); html=get_html(page,url)
            if not html: continue
            soup=BeautifulSoup(html,"html.parser"); recs=parse_yellow_cards(soup,region,keyword,url); out.extend(recs)
            # If a paginated page is empty, don't burn the remaining 249 pages.
            if not recs: break
    return deduplicate_records(out)[:MAX_RECORDS_PER_SOURCE]


def hotfrog_urls(region, keyword):
    q=urllib.parse.quote(slug(keyword)); cities=REGION_CITIES.get(region,[slug(region)])
    urls=[f"https://www.hotfrog.ug/search/{slug(region)}/{q}", f"https://www.hotfrog.ug/search/{q}/{slug(region)}", f"https://www.hotfrog.ug/search/{q}"]
    for city in cities[:12]: urls.append(f"https://www.hotfrog.ug/search/{slug(city)}/{q}")
    return list(dict.fromkeys(urls))


def parse_hotfrog(soup, region, keyword, page_url):
    records=[]; seen=set()
    for a in soup.find_all("a",href=True):
        href=urllib.parse.urljoin(page_url,a["href"]); name=clean_text(a.get_text(" ",strip=True))
        if "/company/" not in href.lower() or len(name)<3 or name.lower() in {"call","message","claim this business"} or href in seen: continue
        seen.add(href); card=nearest_card(a); text=clean_text(card.get_text(" ",strip=True)); phone=phone_links(card); phone=phone if phone!="N/A" else phone_from(text)
        address=field_from_dom(card,"Address"); category=field_from_dom(card,"Category")
        deals=extract_deals(card,text,category)
        rec=make_record(name,region,keyword,"Hotfrog Uganda",href,phone,"N/A",address,category,deals,email_from(text))
        if keyword_match(rec,keyword) and region_match(address+" "+text,region): records.append(rec)
    return records


def scrape_hotfrog(page, region, keyword, deadline):
    out=[]; seen=set()
    for start in hotfrog_urls(region,keyword):
        url=start
        for _ in range(MAX_PAGES_PER_SOURCE):
            if time.monotonic()>=deadline or len(out)>=MAX_RECORDS_PER_SOURCE or not url or url in seen: break
            seen.add(url); html=get_html(page,url)
            if not html: break
            soup=BeautifulSoup(html,"html.parser"); out.extend(parse_hotfrog(soup,region,keyword,url))
            nxt=None
            for a in soup.find_all("a",href=True):
                if norm(a.get_text(" ",strip=True)) in {"next","next page","next results","›","→"}:
                    nxt=urllib.parse.urljoin(url,a["href"]); break
            if not nxt: break
            url=nxt
    return deduplicate_records(out)[:MAX_RECORDS_PER_SOURCE]


def parse_yellowpages_page(soup, region, keyword, page_url):
    records=[]
    for heading in soup.find_all(["h2","h3","h4"]):
        name=clean_text(heading.get_text(" ",strip=True))
        if len(name)<2 or name.lower() in {"listing description","view profile","all listings"}: continue
        card=nearest_card(heading); text=clean_text(card.get_text(" ",strip=True))
        if not any(x in text for x in ["Category:","Address:","Listing Description:"]): continue
        address=labeled_value(text,"Address",["Email","Website","Facebook","Listing Description","Category"])
        category=labeled_value(text,"Category",["Address","Email","Website","Facebook","Listing Description"])
        desc=labeled_value(text,"Listing Description",["Category","Address","Email","Website","Facebook"])
        phone=phone_from(text); email=email_from(text); website="N/A"
        for a in card.find_all("a",href=True):
            h=clean_text(a["href"])
            if h.startswith("http") and "yellowpages-uganda.com" not in h and "facebook.com" not in h: website=h; break
        rec=make_record(name,region,keyword,"Yellow Pages Uganda",page_url,phone,website,address,category,desc if desc!="N/A" else category,email)
        if keyword_match(rec,keyword) and region_match(address+" "+desc,region): records.append(rec)
        if len(records)>=MAX_RECORDS_PER_SOURCE: break
    return records


def scrape_yellowpages(page, region, keyword, deadline):
    out=[]; q=slug(keyword); candidates=[f"https://www.yellowpages-uganda.com/listings/tags/{q}/",f"https://www.yellowpages-uganda.com/listings/category/{q}/"]
    html=get_html(page,"https://www.yellowpages-uganda.com/location/")
    if html:
        soup=BeautifulSoup(html,"html.parser")
        for a in soup.find_all("a",href=True):
            href=urllib.parse.urljoin("https://www.yellowpages-uganda.com",a["href"]); txt=clean_text(a.get_text(" ",strip=True))
            if "/listings/category/" in href and txt:
                t=norm(txt); k=norm(keyword)
                if k in t or t in k or set(k.split()).intersection(t.split()): candidates.insert(0,href)
    for start in list(dict.fromkeys(candidates))[:12]:
        url=start; seen=set()
        for _ in range(MAX_PAGES_PER_SOURCE):
            if time.monotonic()>=deadline or len(out)>=MAX_RECORDS_PER_SOURCE or not url or url in seen: break
            seen.add(url); html=get_html(page,url)
            if not html: break
            soup=BeautifulSoup(html,"html.parser"); out.extend(parse_yellowpages_page(soup,region,keyword,url))
            nxt=None
            for a in soup.find_all("a",href=True):
                if norm(a.get_text(" ",strip=True)) in {"next","next page","→","›"}:
                    nxt=urllib.parse.urljoin(url,a["href"]); break
            if not nxt: break
            url=nxt
    return deduplicate_records(out)[:MAX_RECORDS_PER_SOURCE]


def parse_generic_listing_page(soup, region, keyword, source, page_url, link_marker="/listing/"):
    records=[]; seen=set()
    for a in soup.find_all("a",href=True):
        href=urllib.parse.urljoin(page_url,a["href"]); name=clean_text(a.get_text(" ",strip=True))
        if link_marker not in href.lower() or len(name)<3 or href in seen or name.lower() in {"view profile","read more"}: continue
        seen.add(href); card=nearest_card(a); text=clean_text(card.get_text(" ",strip=True))
        if not keyword_match({"Company Name":name,"Category":field_from_dom(card,"Category"),"Business Deals In":text},keyword): continue
        address=labeled_value(text,"Address",["Phone","Email","Website","Category","Description","Listing Description"])
        cat=labeled_value(text,"Category"); desc=labeled_value(text,"Description",["Category","Address","Email","Website","Phone"])
        deals=desc if desc!="N/A" else extract_deals(card,text,cat)
        ph=phone_links(card); ph=ph if ph!="N/A" else phone_from(text)
        rec=make_record(name,region,keyword,source,href,ph,"N/A",address,cat,deals,email_from(text))
        if region_match(address+" "+text,region): records.append(rec)
    return records


def scrape_finder(page, region, keyword, deadline):
    out=[]; starts=[f"https://finderafrica.com/?s={urllib.parse.quote(keyword)}",f"https://finderafrica.com/listing-category/{slug(keyword)}/"]
    for city in REGION_CITIES.get(region,[])[:12]: starts.append(f"https://finderafrica.com/?s={urllib.parse.quote(keyword+' '+city)}")
    for start in list(dict.fromkeys(starts)):
        url=start; seen=set()
        for _ in range(MAX_PAGES_PER_SOURCE):
            if time.monotonic()>=deadline or len(out)>=MAX_RECORDS_PER_SOURCE or not url or url in seen: break
            seen.add(url); html=get_html(page,url)
            if not html: break
            soup=BeautifulSoup(html,"html.parser"); out.extend(parse_generic_listing_page(soup,region,keyword,"FinderAfrica Uganda",url,"/listing/"))
            nxt=None
            for a in soup.find_all("a",href=True):
                if norm(a.get_text(" ",strip=True)) in {"next","next page","older posts","›","→"}:
                    nxt=urllib.parse.urljoin(url,a["href"]); break
            if not nxt: break
            url=nxt
    return deduplicate_records(out)[:MAX_RECORDS_PER_SOURCE]


def scrape_findug(page, region, keyword, deadline):
    out=[]; starts=[]
    for term in keyword_terms(keyword): starts.append(f"https://find.ug/?s={urllib.parse.quote_plus(term)}")
    for city in REGION_CITIES.get(region,[]):
        for term in keyword_terms(keyword)[:5]: starts.append(f"https://find.ug/?s={urllib.parse.quote_plus(term+' '+city)}")
    for start in list(dict.fromkeys(starts)):
        url=start; seen=set()
        for _ in range(MAX_PAGES_PER_SOURCE):
            if time.monotonic()>=deadline or len(out)>=MAX_RECORDS_PER_SOURCE or not url or url in seen: break
            seen.add(url); html=get_html(page,url)
            if not html: break
            soup=BeautifulSoup(html,"html.parser"); out.extend(parse_generic_listing_page(soup,region,keyword,"Find.ug",url,"/listing/"))
            nxt=None
            for a in soup.find_all("a",href=True):
                if norm(a.get_text(" ",strip=True)) in {"next","next page","older posts","›","→"}:
                    nxt=urllib.parse.urljoin(url,a["href"]); break
            if not nxt: break
            url=nxt
    return deduplicate_records(out)[:MAX_RECORDS_PER_SOURCE]


def scrape_sme(page, region, keyword, deadline):
    out=[]; seen=set(); page_no=1; districts=[norm(x) for x in REGION_DISTRICTS.get(region,[region])]
    kt=[t for term in keyword_terms(keyword) for t in norm(term).split() if len(t)>2]
    while time.monotonic()<deadline and page_no<=300 and len(out)<MAX_RECORDS_PER_SOURCE:
        url=f"https://mybusiness.go.ug/Reports/SMEDirectory?Filters.PageSize=15&p={page_no}&q=BusinessNeed"
        html=get_html(page,url)
        if not html: break
        soup=BeautifulSoup(html,"html.parser"); rows=soup.select("table tr"); got=0
        for row in rows:
            cells=[clean_text(c.get_text(" ",strip=True)) for c in row.find_all(["td","th"])]
            if len(cells)<3 or cells[0].lower() in {"no.","no"}: continue
            name,sector,district=cells[1],cells[2],cells[3] if len(cells)>3 else "N/A"
            if name=="N/A": continue
            if kt and not any(t in norm(name+" "+sector) for t in kt): continue
            if not any(d in norm(district) or norm(district) in d for d in districts): continue
            key=(norm(name),norm(district))
            if key in seen: continue
            seen.add(key); got+=1
            # The portal gives district in the public table, not a street address.
            # Never label a district as a physical address.
            deals=sector if sector!="N/A" else "N/A"
            out.append(make_record(name,region,keyword,"National SME Portal",url,"N/A","N/A","N/A",sector,deals,"N/A"))
            if len(out)>=MAX_RECORDS_PER_SOURCE: break
        if got==0 and page_no>15: break
        page_no+=1
    return out[:MAX_RECORDS_PER_SOURCE]


def scrape_bubu(page, region, keyword, deadline):
    out=[]; seen=set(); starts=[f"https://bubuonlinenews.ug/places/?s={urllib.parse.quote_plus(t)}" for t in keyword_terms(keyword)]
    starts.append("https://bubuonlinenews.ug/places/")
    for start in starts:
        url=start
        for _ in range(MAX_PAGES_PER_SOURCE):
            if time.monotonic()>=deadline or len(out)>=MAX_RECORDS_PER_SOURCE or not url or url in seen: break
            seen.add(url); html=get_html(page,url)
            if not html: break
            soup=BeautifulSoup(html,"html.parser")
            for a in soup.find_all("a",href=True):
                href=urllib.parse.urljoin(url,a["href"]); name=clean_text(a.get_text(" ",strip=True))
                if "/places/" not in href.lower() or "/page/" in href.lower() or len(name)<3 or name.lower() in {"view all","read more","bubu directory"} or href in seen: continue
                card=nearest_card(a); text=clean_text(card.get_text(" ",strip=True))
                if not keyword_match({"Company Name":name,"Category":text,"Business Deals In":text},keyword): continue
                ph=phone_links(card); ph=ph if ph!="N/A" else phone_from(text)
                addr=labeled_value(text,"Address",["Phone","Email","Website","Business Category","Description"])
                cat=labeled_value(text,"Business Category",["Address","Phone","Email","Website","Description"])
                if cat=="N/A": cat=labeled_value(text,"Category")
                rec=make_record(name,region,keyword,"BUBU Uganda Directory",href,ph,"N/A",addr,cat,extract_deals(card,text,cat),email_from(text))
                if region_match(addr+" "+text,region): out.append(rec)
                if len(out)>=MAX_RECORDS_PER_SOURCE: break
            nxt=None
            for a in soup.find_all("a",href=True):
                if norm(a.get_text(" ",strip=True)) in {"next","next page","›","→"}:
                    nxt=urllib.parse.urljoin(url,a["href"]); break
            if not nxt:
                m=re.search(r"/places/page/(\d+)/?",url)
                if m: nxt=f"https://bubuonlinenews.ug/places/page/{int(m.group(1))+1}/"
                elif url.rstrip("/").endswith("/places"): nxt="https://bubuonlinenews.ug/places/page/2/"
            url=nxt
    return deduplicate_records(out)[:MAX_RECORDS_PER_SOURCE]


def scrape_kcca(page, region, keyword, deadline):
    if region!="Kampala" or time.monotonic()>=deadline: return []
    html=get_html(page,"https://kcca.go.ug/businesses")
    if not html: return []
    soup=BeautifulSoup(html,"html.parser"); out=[]
    for row in soup.select("table tr"):
        text=clean_text(row.get_text(" ",strip=True)); cells=[clean_text(x.get_text(" ",strip=True)) for x in row.find_all(["td","th"])]
        if not cells or not keyword_match({"Company Name":cells[0],"Category":text,"Business Deals In":text},keyword): continue
        name=cells[0] if len(cells)>1 else cells[-1]
        address=" | ".join(cells[1:]) if len(cells)>1 else "N/A"
        out.append(make_record(name,region,keyword,"KCCA Business Register","https://kcca.go.ug/businesses",phone_from(text),"N/A",address,keyword,text,email_from(text)))
        if len(out)>=MAX_RECORDS_PER_SOURCE: break
    return out


def osm_query(bbox, keyword):
    terms=[norm(keyword)] + [norm(x) for x in keyword_terms(keyword)]
    terms += [t for t in norm(keyword).split() if len(t)>=4]
    pattern="|".join(re.escape(t) for t in dict.fromkeys(terms) if t).replace('"','\\"')
    blocks=[
        f'nwr["name"~"{pattern}",i]({bbox});', f'nwr["brand"~"{pattern}",i]({bbox});',
        f'nwr["description"~"{pattern}",i]({bbox});', f'nwr["shop"~"{pattern}",i]({bbox});',
        f'nwr["amenity"~"{pattern}",i]({bbox});', f'nwr["office"~"{pattern}",i]({bbox});',
        f'nwr["craft"~"{pattern}",i]({bbox});',
    ]
    return "[out:json][timeout:20];(\n"+"\n".join(blocks)+"\n);out center tags;"


def fetch_osm_grid_data(region_name, keyword):
    if region_name not in REGION_BBOXES:
        fetch_osm_grid_data.last_count=0; return []
    deadline=time.monotonic()+45; bboxes=REGION_BBOXES[region_name]
    def one(bbox):
        local=[]; seen=set(); query=osm_query(bbox,keyword)
        for endpoint in OVERPASS:
            if time.monotonic()>=deadline: break
            try:
                r=requests.post(endpoint,data=query,headers={"User-Agent":"UgandaBusinessLeadGenerator/5.0"},timeout=10)
                if r.status_code!=200: continue
                for el in r.json().get("elements",[]):
                    tags=el.get("tags",{}); name=clean_text(tags.get("name"))
                    if name=="N/A": continue
                    c=el.get("center",{}); lat=el.get("lat",c.get("lat","N/A")); lng=el.get("lon",c.get("lon","N/A"))
                    addr=", ".join(str(tags[k]) for k in ["addr:housenumber","addr:street","addr:place","addr:suburb","addr:city","addr:district"] if tags.get(k)) or "N/A"
                    cat=next((tags.get(k) for k in ["shop","amenity","office","craft","industrial","healthcare","tourism"] if tags.get(k)),"N/A")
                    deals=tags.get("description") or tags.get("product") or tags.get("operator") or cat or "N/A"
                    rec=make_record(name,region_name,keyword,"OpenStreetMap",endpoint,tags.get("phone") or tags.get("contact:phone","N/A"),tags.get("website") or tags.get("contact:website","N/A"),addr,cat,deals,tags.get("email") or tags.get("contact:email","N/A"),"N/A",lat,lng)
                    if keyword_match(rec,keyword):
                        key=identity(rec)
                        if key not in seen: seen.add(key); local.append(rec)
                return local
            except Exception: continue
        return local
    out=[]
    try:
        with ThreadPoolExecutor(max_workers=min(6,len(bboxes))) as pool:
            futs=[pool.submit(one,b) for b in bboxes]
            for f in as_completed(futs,timeout=50):
                try: out.extend(f.result())
                except Exception: pass
    except Exception: pass
    out=deduplicate_records(out); fetch_osm_grid_data.last_count=len(out)
    return out[:MAX_RECORDS_PER_SOURCE]

fetch_osm_grid_data.last_count=0


def identity(r):
    name=norm(r.get("Company Name")); addr=norm(r.get("Physical Address")); phone=re.sub(r"\D","",clean_text(r.get("Phone Contact")))
    if name and addr not in {"","n a"}: return ("name_address",name,addr[:180])
    if name and phone: return ("name_phone",name,phone)
    return ("name",name) if name else None


def merge(a,b):
    # Prefer real descriptive fields and contacts from any source.
    for k in ["Phone Contact","Email","Website","Physical Address","Rating","Lat","Lng","Category"]:
        if clean_text(a.get(k))=="N/A" and clean_text(b.get(k))!="N/A": a[k]=b[k]
    old_deals=clean_text(a.get("Business Deals In")); new_deals=clean_text(b.get("Business Deals In"))
    if old_deals=="N/A" or (old_deals==clean_text(a.get("Category")) and new_deals!="N/A"): a["Business Deals In"]=new_deals
    for k in ["Data Source","Source URL"]:
        vals=[]
        for v in [a.get(k),b.get(k)]:
            for part in clean_text(v).split("|"):
                part=part.strip()
                if part and part!="N/A" and part not in vals: vals.append(part)
        a[k]=" | ".join(vals) if vals else "N/A"
    a["Physical Address"]=clean_physical_address(a.get("Physical Address"))
    return a


def deduplicate_records(records):
    unique=OrderedDict()
    for r in records:
        key=identity(r)
        if not key: continue
        unique[key]=merge(unique[key],r) if key in unique else dict(r)
    return list(unique.values())


def scrape_ugandan_directories(region_name, keyword):
    deadline=time.monotonic()+SEARCH_BUDGET_SECONDS; all_records=[]; source_counts={s:0 for s in SOURCE_NAMES}
    jobs=[
        ("Yellow Uganda",scrape_yellow),("Find.ug",scrape_findug),("FinderAfrica Uganda",scrape_finder),
        ("Yellow Pages Uganda",scrape_yellowpages),("Hotfrog Uganda",scrape_hotfrog),("National SME Portal",scrape_sme),
        ("BUBU Uganda Directory",scrape_bubu),("KCCA Business Register",scrape_kcca),
    ]
    # Directory workers use HTTP for list pages because it is lighter. If a list
    # page requires JavaScript, the profile-enrichment stage uses Playwright.
    try:
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures={pool.submit(job,None,region_name,keyword,min(deadline,time.monotonic()+SOURCE_BUDGET_SECONDS)):source for source,job in jobs}
            for f in as_completed(futures,timeout=SEARCH_BUDGET_SECONDS+5):
                source=futures[f]
                try: recs=f.result()
                except Exception: recs=[]
                source_counts[source]=len(recs); all_records.extend(recs)
    except Exception: pass

    unique=deduplicate_records(all_records)
    # Contact-first enrichment. HTTP profile enrichment is attempted in parallel;
    # then a small Playwright pass handles JavaScript-only profiles if Chromium is available.
    enrich_deadline=min(deadline,time.monotonic()+ENRICH_BUDGET_SECONDS)
    priority=sorted(unique,key=lambda r:(
        0 if clean_text(r.get("Phone Contact"))=="N/A" else 1,
        0 if clean_text(r.get("Physical Address"))=="N/A" else 1,
        0 if clean_text(r.get("Business Deals In"))=="N/A" else 1,
    ))
    try:
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures=[pool.submit(enrich_profile_http,r,enrich_deadline) for r in priority[:350]]
            for f in as_completed(futures):
                if time.monotonic()>=enrich_deadline: break
                try: f.result()
                except Exception: pass
    except Exception: pass

    # Use one headless browser for the remaining JavaScript-dependent profiles.
    try:
        if time.monotonic()<enrich_deadline:
            with sync_playwright() as p:
                browser=None; context=None
                try:
                    browser=p.chromium.launch(headless=True)
                    context=browser.new_context(user_agent=USER_AGENT)
                    page=context.new_page()
                    for r in priority[:120]:
                        if time.monotonic()>=enrich_deadline: break
                        if clean_text(r.get("Phone Contact"))!="N/A" and clean_text(r.get("Physical Address"))!="N/A" and clean_text(r.get("Business Deals In"))!="N/A": continue
                        url=r.get("Source URL","N/A")
                        if url in {"N/A",""}: continue
                        html=browser_html(page,url)
                        if not html: continue
                        soup=BeautifulSoup(html,"html.parser")
                        text=clean_text(soup.get_text(" ",strip=True))
                        if clean_text(r.get("Phone Contact"))=="N/A":
                            ph=phone_links(soup); ph=ph if ph!="N/A" else phone_from(text)
                            if ph!="N/A": r["Phone Contact"]=ph
                        if clean_text(r.get("Physical Address"))=="N/A":
                            addr=field_from_dom(soup,"Address")
                            if addr!="N/A": r["Physical Address"]=clean_physical_address(addr)
                        if clean_text(r.get("Business Deals In"))=="N/A": r["Business Deals In"]=extract_deals(soup,text,clean_text(r.get("Category")))
                finally:
                    try:
                        if context: context.close()
                        if browser: browser.close()
                    except Exception: pass
    except Exception: pass

    # Recalculate source counts after enrichment/dedup so the UI reports usable
    # unique records rather than raw duplicate cards.
    result=deduplicate_records([r for r in unique if not r.get("Physical Address") or r.get("Physical Address")=="N/A" or region_match(r.get("Physical Address"),region_name)])[:MAX_TOTAL_RESULTS]
    counts={s:0 for s in SOURCE_NAMES}
    for r in result:
        for s in SOURCE_NAMES:
            if s in clean_text(r.get("Data Source")): counts[s]+=1
    scrape_ugandan_directories.last_source_counts=counts
    scrape_ugandan_directories.last_contact_counts={
        "total":len(result), "phones":sum(clean_text(r.get("Phone Contact"))!="N/A" for r in result),
        "addresses":sum(clean_text(r.get("Physical Address"))!="N/A" for r in result),
        "emails":sum(clean_text(r.get("Email"))!="N/A" for r in result),
    }
    return result

scrape_ugandan_directories.last_source_counts={s:0 for s in SOURCE_NAMES}
scrape_ugandan_directories.last_contact_counts={"total":0,"phones":0,"addresses":0,"emails":0}
