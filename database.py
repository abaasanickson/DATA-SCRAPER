import sqlite3
import pandas as pd

DB_NAME = "uganda_leads.db"


def _connect():
    return sqlite3.connect(DB_NAME)


def init_database():
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS business_leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT,
            region TEXT,
            search_query TEXT,
            category TEXT,
            business_deals_in TEXT,
            phone_contact TEXT,
            email TEXT,
            website TEXT,
            physical_address TEXT,
            rating TEXT,
            lat TEXT,
            lng TEXT,
            data_source TEXT,
            source_url TEXT,
            UNIQUE(company_name, physical_address)
        )
    """)
    existing = {row[1] for row in cur.execute("PRAGMA table_info(business_leads)").fetchall()}
    additions = {
        "search_query": "TEXT",
        "email": "TEXT",
    }
    for name, typ in additions.items():
        if name not in existing:
            cur.execute(f"ALTER TABLE business_leads ADD COLUMN {name} {typ}")
    conn.commit()
    conn.close()


def _clean(v):
    if v is None or str(v).strip() == "":
        return "N/A"
    return str(v).strip()


def _merge(old, new):
    old, new = _clean(old), _clean(new)
    if old == "N/A": return new
    if new == "N/A" or new == old: return old
    parts = []
    for value in (old + " | " + new).split("|"):
        value = value.strip()
        if value and value != "N/A" and value not in parts:
            parts.append(value)
    return " | ".join(parts) if parts else "N/A"


def _find_existing(cur, name, address):
    cur.execute(
        "SELECT id FROM business_leads WHERE lower(company_name)=lower(?) AND lower(physical_address)=lower(?) LIMIT 1",
        (name, address),
    )
    row = cur.fetchone()
    return row[0] if row else None


def save_and_deduplicate(records):
    if not records:
        return 0
    init_database()
    conn = _connect()
    cur = conn.cursor()
    count = 0
    for r in records:
        try:
            name = _clean(r.get("Company Name"))
            address = _clean(r.get("Physical Address"))
            existing_id = _find_existing(cur, name, address)
            values = {
                "region": _clean(r.get("Region")),
                "search_query": _clean(r.get("Search Query")),
                "category": _clean(r.get("Category")),
                "business_deals_in": _clean(r.get("Business Deals In")),
                "phone_contact": _clean(r.get("Phone Contact")),
                "email": _clean(r.get("Email")),
                "website": _clean(r.get("Website")),
                "physical_address": address,
                "rating": _clean(r.get("Rating")),
                "lat": _clean(r.get("Lat")),
                "lng": _clean(r.get("Lng")),
                "data_source": _clean(r.get("Data Source")),
                "source_url": _clean(r.get("Source URL")),
            }
            if existing_id:
                cur.execute("SELECT region,search_query,category,business_deals_in,phone_contact,email,website,physical_address,rating,lat,lng,data_source,source_url FROM business_leads WHERE id=?", (existing_id,))
                row = cur.fetchone()
                keys = ["region","search_query","category","business_deals_in","phone_contact","email","website","physical_address","rating","lat","lng","data_source","source_url"]
                old = dict(zip(keys, row))
                merged = {k: _merge(old.get(k), values.get(k)) for k in keys}
                # Region remains the primary physical region; if multiple searches hit the same business, preserve all source/search metadata.
                cur.execute("""
                    UPDATE business_leads SET region=?,search_query=?,category=?,business_deals_in=?,phone_contact=?,email=?,website=?,physical_address=?,rating=?,lat=?,lng=?,data_source=?,source_url=? WHERE id=?
                """, tuple(merged[k] for k in keys) + (existing_id,))
            else:
                cur.execute("""
                    INSERT INTO business_leads (
                        company_name,region,search_query,category,business_deals_in,
                        phone_contact,email,website,physical_address,rating,lat,lng,
                        data_source,source_url
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    name, values["region"], values["search_query"], values["category"],
                    values["business_deals_in"], values["phone_contact"], values["email"],
                    values["website"], values["physical_address"], values["rating"],
                    values["lat"], values["lng"], values["data_source"], values["source_url"],
                ))
            count += 1
        except Exception:
            continue
    conn.commit()
    conn.close()
    return count


def get_leads(region=None, search_query=None):
    init_database()
    conn = _connect()
    clauses, params = [], []
    if region:
        clauses.append("lower(region)=lower(?)")
        params.append(region)
    if search_query:
        # search_query may contain multiple searches separated by '|'. Match the requested search as a token.
        clauses.append("instr(lower('|' || replace(search_query, ' | ', '|') || '|'), lower('|' || ? || '|')) > 0")
        params.append(search_query)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    df = pd.read_sql_query(
        "SELECT * FROM business_leads" + where + " ORDER BY id DESC",
        conn,
        params=params,
    )
    conn.close()
    return df


def get_all_leads():
    return get_leads()
