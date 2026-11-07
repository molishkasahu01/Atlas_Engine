import streamlit as st
import pandas as pd
import json
import sqlite3
from crawler import LiveWebCrawler
from database import get_connection, init_db

st.set_page_config(page_title="Atlas - Scholarship Intelligence Crawler", layout="wide", page_icon="🎓")

init_db()

# Custom Header
st.title("🎓 Atlas Scholarship Intelligence & Verification Engine")
st.caption("Automated discovery, verification, change-detection, and confidence scoring for Indian student funding opportunities.")

# Sidebar Controls
st.sidebar.header("Crawler Management Console")
run_mode = st.sidebar.radio("Crawler Mode", ["Standard Discovery Run", "Simulate Subsequent Run (Detect Changes)"])

if st.sidebar.button("▶ Trigger Crawler Now", use_container_width=True):
    with st.spinner("Executing Crawler Pipeline... (This takes about 60 seconds)"):
        crawler = LiveWebCrawler()
        crawler.run(simulate_changes=(run_mode == "Simulate Subsequent Run (Detect Changes)"))
        st.sidebar.success("Crawl cycle successfully finished!")
        st.rerun() # Refresh the page to show new data

st.sidebar.divider()
st.sidebar.info("Verification threshold: Confidence ≥ 95.0% required for VERIFIED label.")

# Load Data from Database
conn = get_connection()
df_all = pd.read_sql_query("SELECT * FROM scholarships", conn)
df_logs = pd.read_sql_query("""
    SELECT cl.id, s.name as scholarship_name, cl.field_name, cl.old_value, cl.new_value, cl.change_date, cl.evidence
    FROM change_logs cl
    JOIN scholarships s ON cl.scholarship_id = s.id
    ORDER BY cl.id DESC
""", conn)
df_runs = pd.read_sql_query("SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 5", conn)

if df_all.empty:
    st.warning("Database is empty. Click '▶ Trigger Crawler Now' in the sidebar to populate initial data.")
    st.stop()

# Key Evaluation Metrics
total_discovered = len(df_all)
verified_count = len(df_all[df_all["status"].isin(["ACTIVE", "EXPIRING_SOON"]) & (df_all["confidence_score"] >= 95.0)])
review_req_count = len(df_all[df_all["status"] == "REVIEW_REQUIRED"])
active_count = len(df_all[df_all["status"].isin(["ACTIVE", "EXPIRING_SOON"])])
expired_count = len(df_all[df_all["status"] == "EXPIRED"])
avg_confidence = df_all["confidence_score"].mean()

m1, m2, m3, m4, m5, m6 = st.columns(6)
m1.metric("Total Discovered", total_discovered)
m2.metric("Verified (≥95%)", verified_count)
m3.metric("Review Required", review_req_count)
m4.metric("Active / Expiring", active_count)
m5.metric("Expired / Stale", expired_count)
m6.metric("Avg Confidence", f"{avg_confidence:.1f}%")

st.divider()

# Tab Navigation
tab_repo, tab_changes, tab_runs = st.tabs(["📚 Scholarship Repository", "🔍 Detected Changes (Audit Trail)", "⚙️ Crawler Execution History"])

with tab_repo:
    # Filter Toolbar
    c1, c2, c3 = st.columns([2, 1, 1])
    search_q = c1.text_input("Search scholarship by name, provider or criteria", "")
    type_filter = c2.selectbox("Filter by Source Type", ["All"] + list(df_all["source_type"].unique()))
    status_filter = c3.selectbox("Filter by Status", ["All"] + list(df_all["status"].unique()))

    filtered_df = df_all.copy()
    if search_q:
        filtered_df = filtered_df[
            filtered_df["name"].str.contains(search_q, case=False, na=False) |
            filtered_df["provider"].str.contains(search_q, case=False, na=False)
        ]
    if type_filter != "All":
        filtered_df = filtered_df[filtered_df["source_type"] == type_filter]
    if status_filter != "All":
        filtered_df = filtered_df[filtered_df["status"] == status_filter]

    st.dataframe(
        filtered_df[[
            "id", "name", "provider", "source_type", "amount_benefit", "closing_date", 
            "status", "confidence_score", "last_verified"
        ]],
        use_container_width=True,
        hide_index=True
    )

    st.subheader("Deep-Dive Scholarship Inspector")
    selected_id = st.selectbox("Select a Scholarship ID to inspect official provenance:", filtered_df["id"].tolist())

    if selected_id:
        record = df_all[df_all["id"] == selected_id].iloc[0]
        
        col_left, col_right = st.columns([3, 2])
        with col_left:
            st.markdown(f"### {record['name']}")
            st.markdown(f"**Provider:** {record['provider']} &nbsp;|&nbsp; **Type:** `{record['source_type']}`")
            
            c_a, c_b = st.columns(2)
            c_a.markdown(f"**Official Source:** [{record['official_source_url']}]({record['official_source_url']})")
            if record['application_url']:
                c_b.markdown(f"**Application Portal:** [{record['application_url']}]({record['application_url']})")
            else:
                c_b.markdown("**Application Portal:** `Not specified`")

            st.markdown("#### Structured Eligibility Schema")
            schema_data = {
                "Education Level": record["education_level"],
                "Amount / Benefit": record["amount_benefit"],
                "Income Ceiling": record["income_criteria"],
                "Gender Criteria": record["gender_criteria"],
                "Category / Caste": record["category_criteria"],
                "Domicile / State": record["domicile_criteria"],
                "Application Window": f"{record['opening_date']} to {record['closing_date']}",
                "Selection Method": record["selection_process"],
                "Documents Needed": record["documents_required"]
            }
            st.table(pd.DataFrame(list(schema_data.items()), columns=["Field", "Normalized Extraction"]))

        with col_right:
            # Score card
            st.markdown("#### Verification & Trust Engine")
            status_color = "green" if record["status"] in ["ACTIVE", "EXPIRING_SOON"] else ("red" if record["status"] == "EXPIRED" else "orange")
            st.markdown(f"**Status:** :{status_color}[**{record['status']}**] &nbsp;|&nbsp; **Confidence:** `{record['confidence_score']}%`")

            # Breakdown
            st.markdown("**Why this score? (Traceable Score Breakdown):**")
            breakdown = json.loads(record["confidence_breakdown"])
            for cat, details in breakdown.items():
                pts = details["points"]
                mx = details["max"]
                st.write(f"- **{cat.replace('_', ' ').title()}**: `{pts}/{mx} pts` — *{details['detail']}*")

            # Field-level Evidence Provenance
            st.markdown("#### Traceable Evidence Snippets")
            evidence_records = pd.read_sql_query(
                "SELECT field_name, extracted_value, source_snippet, verified_at FROM field_evidence WHERE scholarship_id = ?",
                conn, params=(int(selected_id),)
            )
            if not evidence_records.empty:
                for _, ev in evidence_records.iterrows():
                    with st.expander(f"Field: {ev['field_name']} ➔ '{ev['extracted_value']}'"):
                        st.info(f"**Exact Source Evidence:** \"{ev['source_snippet']}\"")
                        st.caption(f"Verified At: {ev['verified_at']}")
            else:
                st.write("No field-level evidence snippets recorded.")

with tab_changes:
    st.subheader("Automated Change Detection & Audit Log")
    st.write("Demonstration that the crawler detects differences across runs rather than overwriting without a trace.")
    if df_logs.empty:
        st.info("No changes logged yet. Select 'Simulate Subsequent Run (Detect Changes)' in the sidebar and trigger the crawler.")
    else:
        st.dataframe(df_logs, use_container_width=True, hide_index=True)

with tab_runs:
    st.subheader("Crawl Run History")
    st.dataframe(df_runs, use_container_width=True, hide_index=True)

conn.close()