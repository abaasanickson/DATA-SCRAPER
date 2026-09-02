import re
import time
import urllib.parse
from collections import OrderedDict
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

MAX_TOTAL_RESULTS = 500
MAX_RECORDS_PER_SOURCE = 250
MAX_PAGES_PER_SOURCE = 10
SEARCH_BUDGET_SECONDS = 90
SOURCE_BUDGET_SECONDS = 15
REQUEST_TIMEOUT = 6
BROWSER_TIMEOUT = 9000
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"

REGION_AREAS = {
    "Kampala": ["kampala", "central division", "kawempe", "nakawa", "makindye", "rubaga", "lubaga", "ntinda", "kololo", "bukoto", "muyenga", "kabalagala", "katwe", "makerere", "bugolobi"],
    "Wakiso": ["wakiso", "kira", "nansana", "entebbe", "kajansi", "namugongo", "kyaliwajjala", "bweyogerere", "buloba", "kasangati", "gayaza", "kajjansi", "busabala", "zanna", "lubowa", "kira town"],
    "Mukono": ["mukono", "seeta", "sonde", "namanve", "lugazi", "nakisunga", "nkokonjeru", "seeta town"],
    "Masaka": ["masaka", "nyendo", "bukakata", "kijjabwemi"],
    "Jinja": ["jinja", "bugembe", "mpumudde", "kimaka", "walukuba", "budhumbuli", "masese"],
    # Western Uganda is intentionally Mbarara-centred. These are the nearby
    # commercial districts/towns we want covered instead of crawling the whole
    # Western Region in one very large map request.
    "Western Uganda": [
        "mbarara", "mbarara city", "mbarara district", "rwampara",
        "sheema", "kabwohe", "bushenyi", "bushenyi-ishaka", "ishaka",
        "mitooma", "buhweju", "ibanda", "kiruhura", "isingiro", "ntungamo"
    ],
}

# Western Uganda uses several small Mbarara-centred OSM search zones.
# Keeping each request small prevents the Streamlit Community Cloud process
# from being overwhelmed by a single giant Overpass query.
REGION_BBOXES = {
    "Kampala": ["0.25,32.45,0.42,32.70"],
    "Wakiso": ["0.05,32.30,0.60,32.75"],
    "Mukono": ["0.20,32.60,0.55,32.90"],
    "Masaka": ["-0.45,31.60,-0.20,31.85"],
    "Jinja": ["0.35,33.15,0.55,33.35"],
    "Western Uganda": [
        "-0.70,30.55,-0.55,30.75",  # Mbarara City
        "-0.78,30.40,-0.63,30.58",  # Rwampara / western Mbarara belt
        "-0.82,30.00,-0.65,30.25",  # Sheema / Kabwohe
        "-0.78,29.70,-0.60,29.95",  # Bushenyi / Ishaka / Mitooma
        "-0.65,30.00,-0.48,30.20",  # Buhweju / eastern Sheema belt
        "-0.15,30.00,0.05,30.25",   # Ibanda / northern Mbarara belt
        "-0.55,30.55,-0.35,30.80",  # Kiruhura / eastern Mbarara belt
        "-0.95,30.95,-0.72,31.20",  # Isingiro / Ntungamo side
        "-1.05,30.35,-0.82,30.60",  # Ntungamo / western Ankole belt
    ],
}

OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

SOURCE_NAMES = ["Yellow Uganda", "Hotfrog Uganda", "FinderAfrica Uganda", "Yellow Pages Uganda", "KCCA Business Register", "OpenStreetMap"]


def clean_text(v):
    if v is None:
        return "N/A"
    v = re.sub(r"\s+", " ", str(v)).strip()
    return v if v else "N/A"


def norm(v):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", clean_text(v).lower())).strip()


def slug(v):
    return re.sub(r"-+", "-", norm(v).replace(" ", "-"))


def phone_from(text):
    text = clean_text(text)
    patterns = [
        r"\+256[\s()./-]*\d{2,3}[\s()./-]*\d{3,4}[\s()./-]*\d{3,4}",
        r"0\d{2,3}[\s()./-]*\d{3,4}[\s()./-]*\d{3,4}",
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return clean_text(m.group(0))
    return "N/A"


def email_from(text):
    m = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", clean_text(text), re.I)
    return m.group(0) if m else "N/A"


def make_record(name, region, keyword, source, url, phone="N/A", website="N/A", address="N/A", category="N/A", deals="N/A", email="N/A", rating="N/A", lat="N/A", lng="N/A"):
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
        "Rating": clean_text(rating),
        "Lat": clean_text(lat),
        "Lng": clean_text(lng),
        "Data Source": source,
        "Source URL": url,
    }


def region_match(address, region):
    a = norm(address)
    if a in {"", "n a"}:
        return True
    return any(norm(x) in a for x in REGION_AREAS.get(region, [region]))


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
        page.wait_for_timeout(700)
        return page.content()
    except Exception:
        return None


def get_html(page, url):
    return http_html(url) or browser_html(page, url)


def nearest_card(anchor, max_levels=7):
    node = anchor
    best = None
    for _ in range(max_levels):
        if not node.parent:
            break
        node = node.parent
        txt = clean_text(node.get_text(" ", strip=True))
        # A listing card normally contains the business name plus at least one field label.
        if len(txt) >= len(clean_text(anchor.get_text(" ", strip=True))) and len(txt) <= 2200:
            if any(label in txt.lower() for label in ["address", "phone", "category", "listing description", "view profile"]):
                best = node
                break
    return best or anchor.parent


def field_from_dom(card, label):
    target = label.lower().rstrip(":")
    for node in card.find_all(["div", "span", "p", "li", "td", "th"]):
        txt = clean_text(node.get_text(" ", strip=True))
        low = txt.lower()
        if low.startswith(target + ":"):
            value = clean_text(txt.split(":", 1)[1])
            if value and value != txt:
                return value
    return "N/A"


def labeled_value(text, label, stop_labels=None):
    t = clean_text(text)
    stops = stop_labels or ["Phone", "Address", "Email", "Website", "Category", "Listing Description", "Business profile", "Rating"]
    stop = "|".join(re.escape(x) for x in stops if x.lower() != label.lower())
    if stop:
        m = re.search(rf"{re.escape(label)}\s*:?\s*(.*?)(?=\s+(?:{stop})\s*:?)", t, re.I)
    else:
        m = re.search(rf"{re.escape(label)}\s*:?\s*(.*)$", t, re.I)
    return clean_text(m.group(1)) if m else "N/A"


def parse_yellow_cards(soup, region, keyword, source, page_url, base):
    records = []
    anchors = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = urllib.parse.urljoin(base, a.get("href", ""))
        text = clean_text(a.get_text(" ", strip=True))
        if "/company/" in href.lower() and len(text) > 2 and text.lower() not in {"view profile", "send enquiry"}:
            if href not in seen:
                seen.add(href); anchors.append((href, a))
    for href, a in anchors[:MAX_RECORDS_PER_SOURCE]:
        card = nearest_card(a)
        name = clean_text(a.get_text(" ", strip=True))
        text = clean_text(card.get_text(" ", strip=True))
        address = field_from_dom(card, "Address")
        category = field_from_dom(card, "Category")
        phone = phone_from(text)
        email = email_from(text)
        # Yellow Uganda listing cards commonly place a short description between the address and contact controls.
        deals = "N/A"
        chunks = [clean_text(x.get_text(" ", strip=True)) for x in card.find_all(["p","div","li","span"]) ]
        bad = {"verified", "updated", "e-mail", "map", "website", "view profile", "send enquiry", "photos", "reviews"}
        candidates = []
        for chunk in chunks:
            lc = chunk.lower()
            if not chunk or lc.startswith(("address:", "category:", "phone:", "email:", "website:")):
                continue
            if any(x in lc for x in bad) and len(chunk) < 80:
                continue
            if len(chunk) >= 18 and not phone_from(chunk):
                candidates.append(chunk)
        if candidates:
            deals = max(candidates, key=len)
        if deals == "N/A":
            deals = category if category != "N/A" else keyword
        records.append(make_record(name, region, keyword, source, href, phone, "N/A", address, category, deals, email))
    return records


def yellow_category_candidates(page, keyword, region):
    """Discover Yellow Uganda's real category slug instead of guessing /category/<keyword>."""
    candidates = []
    # First try the directory's region category index; it exposes many category links.
    city_slug = slug(region)
    index_urls = [
        f"https://www.yellow.ug/location/{city_slug}/list%3Acategories",
        "https://www.yellow.ug/location/",
    ]
    qtokens = set(norm(keyword).split())
    for u in index_urls:
        html = get_html(page, u)
        if not html:
            continue
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = urllib.parse.urljoin("https://www.yellow.ug", a["href"])
            txt = clean_text(a.get_text(" ", strip=True))
            if "/category/" not in href or not txt:
                continue
            score = len(qtokens.intersection(set(norm(txt).split())))
            if norm(keyword) in norm(txt) or norm(txt) in norm(keyword) or score > 0:
                candidates.append((score, href))
        if candidates:
            break
    # Always try direct category and all-Uganda category as fallbacks.
    candidates += [(3, f"https://www.yellow.ug/category/{slug(keyword)}/city%3A{city_slug}"),
                   (2, f"https://www.yellow.ug/category/{slug(keyword)}")]
    seen = set(); out = []
    for _, u in sorted(candidates, key=lambda x: -x[0]):
        if u not in seen:
            seen.add(u); out.append(u)
    return out[:8]


def scrape_yellow(page, region, keyword, deadline):
    out = []
    seen_urls = set()
    for start in yellow_category_candidates(page, keyword, region):
        if time.monotonic() >= deadline or len(out) >= MAX_RECORDS_PER_SOURCE:
            break
        for n in range(1, MAX_PAGES_PER_SOURCE + 1):
            if time.monotonic() >= deadline or len(out) >= MAX_RECORDS_PER_SOURCE:
                break
            if n == 1:
                url = start
            else:
                parts = urllib.parse.urlsplit(start)
                path = parts.path.rstrip("/")
                if "/city:" in path or "/city%3A" in path:
                    bits = path.rsplit("/", 1)
                    url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, f"{bits[0]}/{n}/{bits[1]}", "", ""))
                else:
                    url = f"{start.rstrip('/')}/{n}"
            if url in seen_urls:
                continue
            seen_urls.add(url)
            html = get_html(page, url)
            if not html:
                continue
            soup = BeautifulSoup(html, "html.parser")
            recs = parse_yellow_cards(soup, region, keyword, "Yellow Uganda", url, "https://www.yellow.ug")
            if not recs:
                break
            out.extend(recs)
    # Yellow's category pages are often broad; retain only businesses whose address actually matches the selected region.
    return [r for r in out if region_match(r["Physical Address"], region)][:MAX_RECORDS_PER_SOURCE]


def hotfrog_urls(region, keyword):
    q = urllib.parse.quote(slug(keyword))
    return [
        f"https://www.hotfrog.ug/search/{slug(region)}/{q}",
        f"https://www.hotfrog.ug/search/{q}/{slug(region)}",
        f"https://www.hotfrog.ug/search/{q}",
    ]


def parse_hotfrog(soup, region, keyword, page_url):
    records = []
    links = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = urllib.parse.urljoin(page_url, a["href"])
        text = clean_text(a.get_text(" ", strip=True))
        if "/company/" in href.lower() and len(text) > 2 and text.lower() not in {"call", "message", "claim this business"}:
            if href not in seen:
                seen.add(href); links.append((href, a))
    for href, a in links[:MAX_RECORDS_PER_SOURCE]:
        card = nearest_card(a)
        text = clean_text(card.get_text(" ", strip=True))
        name = clean_text(a.get_text(" ", strip=True))
        phone = field_from_dom(card, "Phone")
        if phone == "N/A": phone = phone_from(text)
        address = field_from_dom(card, "Address")
        category = field_from_dom(card, "Category")
        deals = "N/A"
        m = re.search(r"(?:Company is working in|working in|business activities)\s+(.+?)(?=\s+Description|$)", text, re.I)
        if m: deals = clean_text(m.group(1))
        if deals == "N/A": deals = category if category != "N/A" else keyword
        records.append(make_record(name, region, keyword, "Hotfrog Uganda", href, phone, "N/A", address, category, deals, email_from(text)))
    return records


def scrape_hotfrog(page, region, keyword, deadline):
    out=[]; seen=set()
    for start in hotfrog_urls(region, keyword):
        url=start
        for _ in range(MAX_PAGES_PER_SOURCE):
            if time.monotonic() >= deadline or len(out) >= MAX_RECORDS_PER_SOURCE or url in seen:
                break
            seen.add(url)
            html=get_html(page,url)
            if not html: break
            soup=BeautifulSoup(html,"html.parser")
            recs=parse_hotfrog(soup,region,keyword,url)
            out.extend(recs)
            nxt=None
            for a in soup.find_all("a",href=True):
                txt=norm(a.get_text(" ",strip=True))
                if txt in {"next","next page","next results"}:
                    nxt=urllib.parse.urljoin(url,a["href"]); break
            if not nxt: break
            url=nxt
    return [r for r in out if region_match(r["Physical Address"],region)][:MAX_RECORDS_PER_SOURCE]


def parse_yellowpages_page(soup, region, keyword, page_url):
    records=[]
    # Yellow Pages category/tag pages are server-rendered and expose clear labels.
    for heading in soup.find_all(["h2","h3","h4"]):
        name=clean_text(heading.get_text(" ",strip=True))
        if len(name)<2: continue
        if name.lower() in {"listing description","view profile","all listings"}: continue
        # Walk up to the first reasonable listing container.
        card=nearest_card(heading)
        text=clean_text(card.get_text(" ",strip=True))
        if "Category:" not in text and "Address:" not in text and "Listing Description:" not in text:
            continue
        address=labeled_value(text,"Address",["Email","Website","Facebook","Listing Description","Category"])
        category=labeled_value(text,"Category",["Address","Email","Website","Facebook","Listing Description"])
        desc=labeled_value(text,"Listing Description",["Category","Address","Email","Website","Facebook"])
        phone=phone_from(text)
        email=email_from(text)
        website="N/A"
        for a in card.find_all("a",href=True):
            h=a["href"]
            if h.startswith("http") and "yellowpages-uganda.com" not in h and "facebook.com" not in h:
                website=h; break
        if not region_match(address + " " + desc, region):
            continue
        # Require the keyword to occur in name/category/description so generic listings are not dumped into every search.
        hay=norm(" ".join([name,category,desc]))
        if norm(keyword) not in hay and not any(tok in hay for tok in norm(keyword).split() if len(tok)>2):
            continue
        records.append(make_record(name,region,keyword,"Yellow Pages Uganda",page_url,phone,website,address,category,desc,email))
        if len(records)>=MAX_RECORDS_PER_SOURCE: break
    return records


def yellowpages_category_links(page):
    html=get_html(page,"https://www.yellowpages-uganda.com/location/")
    if not html: return []
    soup=BeautifulSoup(html,"html.parser")
    out=[]
    for a in soup.find_all("a",href=True):
        href=urllib.parse.urljoin("https://www.yellowpages-uganda.com",a["href"])
        txt=clean_text(a.get_text(" ",strip=True))
        if "/listings/category/" in href and txt:
            out.append((txt,href))
    return out


def scrape_yellowpages(page, region, keyword, deadline):
    out=[]
    q=slug(keyword)
    candidates=[
        f"https://www.yellowpages-uganda.com/listings/tags/{q}/",
        f"https://www.yellowpages-uganda.com/listings/category/{q}/",
    ]
    # Discover the directory's real category slug. Example: "bank" maps to "banks-uganda".
    for txt,href in yellowpages_category_links(page):
        if time.monotonic()>=deadline: break
        t=norm(txt); k=norm(keyword)
        score=len(set(k.split()).intersection(set(t.split())))
        if k in t or t in k or score:
            candidates.insert(0,href)
    seen=set()
    for start in candidates[:8]:
        url=start
        for _ in range(MAX_PAGES_PER_SOURCE):
            if time.monotonic()>=deadline or len(out)>=MAX_RECORDS_PER_SOURCE or url in seen: break
            seen.add(url)
            html=get_html(page,url)
            if not html: break
            soup=BeautifulSoup(html,"html.parser")
            recs=parse_yellowpages_page(soup,region,keyword,url)
            out.extend(recs)
            nxt=None
            for a in soup.find_all("a",href=True):
                txt=norm(a.get_text(" ",strip=True))
                if txt in {"next","next page","→"}:
                    nxt=urllib.parse.urljoin(url,a["href"]); break
            if not nxt: break
            url=nxt
    return out[:MAX_RECORDS_PER_SOURCE]


def scrape_finder(page, region, keyword, deadline):
    # FinderAfrica URL structures vary, so use its site search first and then category fallback.
    candidates=[
        f"https://finderafrica.com/?s={urllib.parse.quote(keyword)}",
        f"https://finderafrica.com/listing-category/{slug(keyword)}/",
    ]
    out=[]; seen=set()
    for start in candidates:
        url=start
        for _ in range(MAX_PAGES_PER_SOURCE):
            if time.monotonic()>=deadline or len(out)>=MAX_RECORDS_PER_SOURCE or url in seen: break
            seen.add(url); html=get_html(page,url)
            if not html: break
            soup=BeautifulSoup(html,"html.parser")
            # Generic listing links; avoid using page-wide text as address.
            for a in soup.find_all("a",href=True):
                href=urllib.parse.urljoin(url,a["href"]); name=clean_text(a.get_text(" ",strip=True))
                if "/listing/" not in href.lower() or len(name)<2: continue
                card=nearest_card(a); text=clean_text(card.get_text(" ",strip=True))
                if not region_match(text,region): continue
                out.append(make_record(name,region,keyword,"FinderAfrica Uganda",href,phone_from(text),"N/A",labeled_value(text,"Address"),labeled_value(text,"Category"),text,email_from(text)))
                if len(out)>=MAX_RECORDS_PER_SOURCE: break
            if len(out)>=MAX_RECORDS_PER_SOURCE: break
            nxt=None
            for a in soup.find_all("a",href=True):
                if norm(a.get_text(" ",strip=True)) in {"next","next page"}:
                    nxt=urllib.parse.urljoin(url,a["href"]); break
            if not nxt: break
            url=nxt
    return out[:MAX_RECORDS_PER_SOURCE]


def scrape_kcca(page, region, keyword, deadline):
    if region != "Kampala" or time.monotonic() >= deadline:
        return []
    # KCCA exposes a licensed-business search page. Parse visible business rows/cards when present.
    url="https://kcca.go.ug/businesses"
    html=get_html(page,url)
    if not html: return []
    soup=BeautifulSoup(html,"html.parser")
    out=[]
    for row in soup.select("table tr"):
        text=clean_text(row.get_text(" ",strip=True))
        if norm(keyword) not in norm(text): continue
        cells=[clean_text(x.get_text(" ",strip=True)) for x in row.find_all(["td","th"])]
        if not cells: continue
        name=cells[0] if len(cells)>1 else cells[-1]
        address=" | ".join(cells[1:]) if len(cells)>1 else text
        out.append(make_record(name,region,keyword,"KCCA Business Register",url,phone_from(text),"N/A",address,keyword,text,email_from(text)))
        if len(out)>=MAX_RECORDS_PER_SOURCE: break
    return out


def osm_query(bbox, keyword):
    # Keep Overpass queries compact. The previous Western-Uganda search created
    # hundreds of node/way/relation clauses for every small area, which could
    # push Streamlit Community Cloud into the "Oh no" resource state.
    raw = norm(keyword)
    tokens = [t for t in raw.split() if len(t) >= 4]
    terms = list(dict.fromkeys([raw] + tokens[:2]))
    pattern = "|".join(re.escape(t) for t in terms if t)
    pattern = pattern.replace('"', '\\"')

    # Search names/descriptions and common business classification tags.
    blocks = [
        f'nwr["name"~"{pattern}",i]({bbox});',
        f'nwr["brand"~"{pattern}",i]({bbox});',
        f'nwr["description"~"{pattern}",i]({bbox});',
        f'nwr["shop"~"{pattern}",i]({bbox});',
        f'nwr["amenity"~"{pattern}",i]({bbox});',
        f'nwr["office"~"{pattern}",i]({bbox});',
        f'nwr["craft"~"{pattern}",i]({bbox});',
    ]
    return "[out:json][timeout:25];(\n" + "\n".join(blocks) + "\n);out center tags;"


def fetch_osm_grid_data(region_name, keyword):
    if region_name not in REGION_BBOXES:
        fetch_osm_grid_data.last_count = 0
        return []
    out=[]; seen=set()
    for bbox in REGION_BBOXES[region_name]:
        query=osm_query(bbox,keyword)
        for endpoint_index, endpoint in enumerate(OVERPASS):
            try:
                # Use the primary endpoint first. Only try the fallback endpoint
                # when the primary request actually fails; do not duplicate a
                # successful empty search.
                r=requests.post(endpoint,data=query,headers={"User-Agent":"UgandaBusinessLeadGenerator/2.0"},timeout=30)
                if r.status_code!=200: continue
                for el in r.json().get("elements",[]):
                    tags=el.get("tags",{}); name=clean_text(tags.get("name"))
                    if name=="N/A": continue
                    c=el.get("center",{}); lat=el.get("lat",c.get("lat","N/A")); lng=el.get("lon",c.get("lon","N/A"))
                    addr=", ".join(str(tags[k]) for k in ["addr:housenumber","addr:street","addr:place","addr:suburb","addr:city","addr:district"] if tags.get(k))
                    if not addr: addr=region_name
                    category=next((tags.get(k) for k in ["shop","amenity","office","craft","industrial","healthcare","tourism"] if tags.get(k)),"N/A")
                    deals=tags.get("description") or tags.get("operator") or category or keyword
                    rec=make_record(name,region_name,keyword,"OpenStreetMap",endpoint,tags.get("phone") or tags.get("contact:phone","N/A"),tags.get("website") or tags.get("contact:website","N/A"),addr,category,deals,tags.get("email") or tags.get("contact:email","N/A"),tags.get("rating","N/A"),lat,lng)
                    key=(norm(name),norm(addr))
                    if key not in seen:
                        seen.add(key); out.append(rec)
                    if len(out)>=MAX_RECORDS_PER_SOURCE: break
                if out: break
            except Exception:
                continue
        if len(out)>=MAX_RECORDS_PER_SOURCE: break
    fetch_osm_grid_data.last_count = len(out)
    return out[:MAX_RECORDS_PER_SOURCE]


fetch_osm_grid_data.last_count = 0


def identity(r):
    name=norm(r.get("Company Name")); addr=norm(r.get("Physical Address")); phone=re.sub(r"\D","",clean_text(r.get("Phone Contact")))
    if name and addr not in {"","n a"}: return ("name_address",name,addr[:180])
    if name and phone: return ("name_phone",name,phone)
    return ("name",name) if name else None


def merge(a,b):
    for k in ["Phone Contact","Email","Website","Physical Address","Rating","Lat","Lng","Category","Business Deals In"]:
        if clean_text(a.get(k))=="N/A" and clean_text(b.get(k))!="N/A":
            a[k]=b[k]
    for k in ["Data Source","Source URL"]:
        vals=[]
        for v in [a.get(k),b.get(k)]:
            for part in clean_text(v).split("|"):
                part=part.strip()
                if part and part!="N/A" and part not in vals: vals.append(part)
        a[k]=" | ".join(vals) if vals else "N/A"
    return a


def deduplicate_records(records):
    unique=OrderedDict()
    for r in records:
        key=identity(r)
        if not key: continue
        unique[key]=merge(unique[key],r) if key in unique else dict(r)
    return list(unique.values())


def scrape_ugandan_directories(region_name, keyword):
    deadline=time.monotonic()+SEARCH_BUDGET_SECONDS
    all_records=[]
    source_counts={s:0 for s in SOURCE_NAMES}
    try:
        with sync_playwright() as p:
            browser=None; context=None; page=None
            try:
                browser=p.chromium.launch(headless=True)
                context=browser.new_context(user_agent=USER_AGENT)
                page=context.new_page()
            except Exception:
                pass
            jobs=[
                ("Yellow Uganda",scrape_yellow),
                ("Hotfrog Uganda",scrape_hotfrog),
                ("FinderAfrica Uganda",scrape_finder),
                ("Yellow Pages Uganda",scrape_yellowpages),
                ("KCCA Business Register",scrape_kcca),
            ]
            for source,job in jobs:
                if time.monotonic() >= deadline:
                    break
                # Every source gets its own time slice. A slow/blocked directory
                # can no longer consume the entire search and starve the others.
                source_deadline = min(deadline, time.monotonic() + SOURCE_BUDGET_SECONDS)
                try:
                    recs=job(page,region_name,keyword,source_deadline)
                    source_counts[source]=len(recs)
                    all_records.extend(recs)
                except Exception:
                    source_counts[source]=0
            try:
                if context: context.close()
                if browser: browser.close()
            except Exception: pass
    except Exception:
        pass
    # Do not discard a record just because the source's address is missing; only reject explicit mismatches.
    filtered=[]
    for r in all_records:
        addr=r.get("Physical Address","N/A")
        if addr in {None,"","N/A"} or region_match(addr,region_name):
            filtered.append(r)
    result=deduplicate_records(filtered)[:MAX_TOTAL_RESULTS]
    # attach source counts for the app without changing record schema
    scrape_ugandan_directories.last_source_counts={s:0 for s in SOURCE_NAMES}
    for r in result:
        for s in SOURCE_NAMES:
            if s in clean_text(r.get("Data Source")):
                scrape_ugandan_directories.last_source_counts[s]+=1
    return result

scrape_ugandan_directories.last_source_counts={s:0 for s in SOURCE_NAMES}
