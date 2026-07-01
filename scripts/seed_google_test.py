import os
import sys
from dotenv import load_dotenv
from supabase import create_client

# Load environment variables
load_dotenv(dotenv_path="/home/aman-amarjit/Desktop/internship/.env")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    print("Error: Supabase environment variables missing.")
    sys.exit(1)

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

contact_email = "amanamarjit243222@gmail.com"
contact_id = "b0383cfc-3301-41db-9ecc-b23d7b2aded6"
listing_id = "239592db-29cf-47d0-86f1-341c11477753"
app_id = "4080f87d-de27-4ebb-ba2f-1aff699238fc"

def seed():
    print(f"Updating contact {contact_email} to represent Google...")
    supabase.table("contacts").update({
        "company": "Google",
        "name": "Google Recruiter",
        "source": "recruiter_inbound",
        "suppressed": False,
        "status": "pending",
        "last_emailed_at": None
    }).eq("id", contact_id).execute()

    print(f"Updating listing {listing_id} to represent Google AI/ML role...")
    supabase.table("listings").update({
        "company": "Google",
        "title": "AI/ML Software Engineer Intern",
        "source_url": "https://careers.google.com/jobs/results/test-ai-ml-google",
        "source": "google_recruiter_test",
        "description": "About the role:\nWe are looking for an AI/ML software engineer intern who has experience building automated agentic workflows, custom NLP pipelines, and orchestrating LLMs.\nRequired skills:\n- Strong programming skills in Python\n- Hands-on experience with LLMs (GPT, Llama, Gemini) and RAG frameworks\n- Knowledge of Vector Databases and Groq API\n- Experience with Docker, PM2, and WebSockets is a plus\n",
        "domain_tag": "ai_ml"
    }).eq("id", listing_id).execute()

    print(f"Resetting application {app_id} status to drafting...")
    supabase.table("applications").update({
        "status": "drafting",
        "email_body": "",
        "critique_score": None,
        "resume_url": None,
        "resume_project_selection": None,
        "extracted_keywords": None,
        "matched_keywords": None,
        "ats_match_rate": None,
        "sent_at": None,
        "followup_at": None,
        "followup_sent": False,
        "reply_sentiment": None,
        "failure_reason": None
    }).eq("id", app_id).execute()

    print("Database successfully seeded for Google AI/ML Recruiter test!")

if __name__ == "__main__":
    seed()
