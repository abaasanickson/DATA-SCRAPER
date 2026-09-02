import sqlite3
import pandas as pd

DB_NAME = "uganda_leads.db"


def _connect():
    return sqlite3.connect(DB_NAME, timeout=30)


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
            district TEXT,
            UNIQUE(company_name, physical_address)
        )
    """)
    existing = {row[1] for row in cur.execute("PRAGMA table_info(business_leads)").fetchall()}
    for name in ["search_query", "email", "district"]:
        if name not in existing:
            cur.execute(f"ALTER TABLE business_leads ADD COLUMN {name} TEXT")
    conn.commit()
    conn.close()


def _clean(v):
    if v is None or str(v).strip() == "":
        return "N/A"
    return str(v).strip()


def _merge(old, new):
    old, new = _clean(old), _clean(new)
    if new == "N/A":
        return old
    if old == "N/A":
        return new
    vals=[]
    for value in (old, new):
        for part in value.split("|"):
            part=part.strip()
            if part and part!="N/A" and part not in vals:
                vals.append(part)
    return " | ".join(vals) if vals else "N/A"


def _find_existing(cur, name, address, phone):
    # Prefer exact name+address. If an address is unavailable, fall back to name+phone.
    if address != "N/A":
        cur.execute("SELECT id FROM business_leads WHERE lower(company_name)=lower(?) AND lower(physical_address)=lower(?) LIMIT 1", (name,address))
        row=cur.fetchone()
        if row: return row[0]
    if phone != "N/A":
        phones=[p.strip() for p in phone.split("|") if p.strip()]
        for p in phones:
            cur.execute("SELECT id FROM business_leads WHERE lower(company_name)=lower(?) AND phone_contact LIKE ? LIMIT 1", (name, f"%{p}%"))
            row=cur.fetchone()
            if row: return row[0]
    return None


def save_and_deduplicate(records):
    if not records: return 0
    init_database(); conn=_connect(); cur=conn.cursor(); count=0
    keys=["region","search_query","category","business_deals_in","phone_contact","email","website","physical_address","rating","lat","lng","data_source","source_url","district"]
    for r in records:
        try:
            name=_clean(r.get("Company Name")); address=_clean(r.get("Physical Address")); phone=_clean(r.get("Phone Contact"))
            existing_id=_find_existing(cur,name,address,phone)
            values={
                "region":_clean(r.get("Region")),"search_query":_clean(r.get("Search Query")),"category":_clean(r.get("Category")),
                "business_deals_in":_clean(r.get("Business Deals In")),"phone_contact":phone,"email":_clean(r.get("Email")),
                "website":_clean(r.get("Website")),"physical_address":address,"rating":_clean(r.get("Rating")),
                "lat":_clean(r.get("Lat")),"lng":_clean(r.get("Lng")),"data_source":_clean(r.get("Data Source")),"source_url":_clean(r.get("Source URL")),"district":_clean(r.get("District")),
            }
            if existing_id:
                cur.execute("SELECT "+",".join(keys)+" FROM business_leads WHERE id=?",(existing_id,))
                old=dict(zip(keys,cur.fetchone()))
                merged={k:_merge(old.get(k),values.get(k)) for k in keys}
                cur.execute("UPDATE business_leads SET "+",".join(f"{k}=?" for k in keys)+" WHERE id=?", tuple(merged[k] for k in keys)+(existing_id,))
            else:
                cur.execute("INSERT INTO business_leads (company_name,"+",".join(keys)+") VALUES ("+",".join(["?"]*(1+len(keys)))+")", (name,)+tuple(values[k] for k in keys))
            count+=1
        except Exception:
            continue
    conn.commit(); conn.close(); return count


def get_leads(region=None, search_query=None):
    init_database(); conn=_connect(); clauses=[]; params=[]
    if region:
        clauses.append("lower(region)=lower(?)"); params.append(region)
    if search_query:
        clauses.append("(lower(search_query)=lower(?) OR instr(lower('|' || replace(search_query, ' | ', '|') || '|'), lower('|' || ? || '|')) > 0)")
        params.extend([search_query,search_query])
    where=" WHERE "+" AND ".join(clauses) if clauses else ""
    df=pd.read_sql_query("SELECT * FROM business_leads"+where+" ORDER BY id DESC",conn,params=params)
    conn.close(); return df


def get_all_leads(): return get_leads()
