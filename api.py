from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
import sqlite3
import json
import subprocess
from database import get_connection

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/scholarships")
def get_scholarships():
    conn = get_connection()
    cursor = conn.execute("SELECT * FROM scholarships ORDER BY id DESC")
    scholarships_raw = [dict(row) for row in cursor.fetchall()]
    
    cursor = conn.execute("SELECT * FROM field_evidence")
    evidence_raw = [dict(row) for row in cursor.fetchall()]
    conn.close()

    formatted_data = []
    for s in scholarships_raw:
        raw_breakdown = json.loads(s["confidence_breakdown"])
        breakdown_array = [{"label": k.replace("_", " ").title(), "points": v["points"], "max": v["max"], "detail": v["detail"]} for k, v in raw_breakdown.items()]
            
        evidence_array = [{"field": e["field_name"], "value": e["extracted_value"], "snippet": e["source_snippet"]} for e in evidence_raw if e["scholarship_id"] == s["id"]]

        formatted_data.append({
            "id": s["id"], "name": s["name"], "provider": s["provider"], "source_type": s["source_type"],
            "url": s["official_source_url"], "amount": s["amount_benefit"], "deadline": s["closing_date"],
            "income": s["income_criteria"], "education": s["education_level"], "status": s["status"],
            "score": s["confidence_score"], "breakdown": breakdown_array, "evidence": evidence_array
        })
    return formatted_data

def run_spider():
    subprocess.run(["scrapy", "crawl", "portal_spider"])

@app.post("/api/crawl")
def trigger_crawl(background_tasks: BackgroundTasks):
    background_tasks.add_task(run_spider)
    return {"message": "Crawler started"}