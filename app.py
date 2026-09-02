import requests
import streamlit as st
import pandas as pd
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from scraper import fetch_osm_grid_data, scrape_ugandan_directories, deduplicate_records, SOURCE_NAMES
from database import init_database, save_and_deduplicate, get_leads

# ====================== PAGE CONFIG & CSS ======================
st.markdown("""
<style>
    .stApp { background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%); color: #e2e8f0; }
    section[data-testid="stSidebar"] { background: linear-gradient(180deg, #1e293b 0%, #0f172a 100%); border-right: 1px solid #334155; }
    h1, h2, h3 { color: #f8fafc !important; }
    div[data-testid="stMetric"] { background: rgba(30, 41, 59, 0.7); border: 1px solid #334155; border-radius: 12px; padding: 16px; }
    div[data-testid="stMetric"] label { color: #94a3b8 !important; }
    div[data-testid="stMetric"] div { color: #38bdf8 !important; font-weight: 600; }
    .stButton > button { background: linear-gradient(180deg, #808080, #4c5055); color: white; border: none; border-radius: 10px; padding: 0.6rem 1.4rem; font-weight: 700; }
    .stButton > button:hover { transform: translateY(-2px); box-shadow: 0 6px 20px rgba(59, 130, 246, 0.4); }
    .stDownloadButton > button { background: linear-gradient(180deg, #021024, #052659); color: white; border: none; border-radius: 10px; padding: 0.6rem 1.4rem; font-weight: 700; }
    .welcome-card { background: linear-gradient(135deg, #1e3a5f 0%, #0f172a 100%); border: 1px solid #334155; border-radius: 16px; padding: 24px 28px; margin-bottom: 24px; }
    .welcome-title { font-size: 1.6rem; font-weight: 700; color: #f8fafc; margin-bottom: 6px; }
    .welcome-subtitle { color: #94a3b8; font-size: 1rem; margin-bottom: 14px; }
    .quote { font-style: italic; color: #38bdf8; border-left: 4px solid #3b82f6; padding-left: 16px; margin-top: 12px; }
</style>
""", unsafe_allow_html=True)

@st.cache_data(ttl=60)
def get_live_quote():
    try:
        response = requests.get("https://zenquotes.io/api/random", timeout=3)
        if response.status_code == 200:
            data = response.json()
            if data and isinstance(data, list) and data[0].get("q"):
                return f'"{data[0]["q"]}" — {data[0].get("a", "Unknown")}'
    except Exception:
        pass
    return '"Dream big. Start small. Act now."'

def get_greeting():
    hour = datetime.now(timezone(timedelta(hours=3))).hour
    if 5 <= hour < 12: return "Good morning Alison"
    if 12 <= hour < 17: return "Good afternoon Alison"
    if 17 <= hour < 22: return "Good evening Alison"
    return "Hello Alison"

st.markdown(f"""
<div class="welcome-card">
    <div class="welcome-title">{get_greeting()}. Ready to generate leads?</div>
    <div class="welcome-subtitle">Full coverage across Kampala, Wakiso, Mukono & Regional Directories</div>
    <div class="quote">{get_live_quote()}</div>
</div>
""", unsafe_allow_html=True)

st.title("Full Region Business Lead Generator")
st.caption("Direct Uganda Directory, Registry & OpenStreetMap Search • Multi-Source Expansion • Deduplicated results")

st.sidebar.markdown("### ⚙️ Search Settings")
region = st.sidebar.selectbox("Select Region", ["Kampala", "Wakiso", "Mukono", "Western Uganda", "Masaka", "Jinja"])
search_query = st.sidebar.text_input("Business Type / Keyword", value="Hardware", help="Enter any business sector, service, company type or keyword. No fixed category list is required.")
st.sidebar.markdown("---")
st.sidebar.info("Searches Uganda public directories/registries + Playwright headless browser + OpenStreetMap. Source failures are isolated and reported instead of silently replacing other sources.")

init_database()

if not search_query.strip():
    st.warning("Enter a business keyword to start the directory search.")
else:
    with st.spinner(f"Searching {region} for '{search_query}' across all source groups and locations..."):
        # Run OSM and the directory/registry pipeline at the same time. This
        # keeps the broad geographic search from making the user wait for two
        # completely separate phases.
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                f_osm = pool.submit(fetch_osm_grid_data, region, search_query)
                f_dir = pool.submit(scrape_ugandan_directories, region, search_query)
                osm_data = f_osm.result()
                dir_data = f_dir.result()
        except Exception:
            osm_data = fetch_osm_grid_data(region, search_query)
            dir_data = scrape_ugandan_directories(region, search_query)
        osm_count = getattr(fetch_osm_grid_data, "last_count", len(osm_data))
        directory_counts = dict(getattr(scrape_ugandan_directories, "last_source_counts", {}))
        combined = deduplicate_records(osm_data + dir_data)
        if combined:
            save_and_deduplicate(combined)

    df = get_leads(region, search_query)
    if not df.empty:
        df.insert(0, "No.", range(1, len(df) + 1))
        source_counts = {source: 0 for source in SOURCE_NAMES}
        source_counts["OpenStreetMap"] = int(osm_count)
        for source, count in directory_counts.items():
            source_counts[source] = int(count)
        phones = int((df["phone_contact"].fillna("N/A") != "N/A").sum()) if "phone_contact" in df.columns else 0
        addresses = int((df["physical_address"].fillna("N/A") != "N/A").sum()) if "physical_address" in df.columns else 0
        emails = int((df["email"].fillna("N/A") != "N/A").sum()) if "email" in df.columns else 0
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total Unique Places", len(df))
        m2.metric("Phone Contacts", phones)
        m3.metric("Physical Addresses", addresses)
        m4.metric("Source Groups", len([v for v in source_counts.values() if v > 0]))
        st.caption(f"Contact coverage: {phones}/{len(df)} with phone • {addresses}/{len(df)} with address • {emails}/{len(df)} with email")

        st.markdown("---")
        st.subheader(f"Results for “{search_query}” in {region}")
        with st.expander("Source scan results", expanded=True):
            st.write(pd.DataFrame(sorted(source_counts.items(), key=lambda x: (-x[1], x[0])), columns=["Source", "Records returned"]))

        display = [
            "No.", "company_name", "category", "business_deals_in", "phone_contact", "email",
            "physical_address", "website", "data_source"
        ]
        display = [c for c in display if c in df.columns]
        st.dataframe(df[display], use_container_width=True, height=520)

        st.markdown("---")
        csv = df.drop(columns=["No.", "id"], errors="ignore").to_csv(index=False).encode("utf-8")
        st.download_button("📥 Export All Leads to CSV", data=csv, file_name=f"{region}_{search_query}_leads.csv", mime="text/csv", use_container_width=True)
    else:
        st.warning("No public records were returned by the accessible sources for this search. Try a broader keyword or another region.")
