import os
import sys
import pytest
from dotenv import load_dotenv

# Add parent directory to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.db_client import supabase
import google.generativeai as genai

load_dotenv(dotenv_path=os.path.abspath(os.path.join(os.path.dirname(__file__), "../.env")))

def analyze_sentiment_python(text: str) -> str:
    """
    Python implementation of the sentiment analysis logic from Deno Edge Function.
    """
    gemini_key = os.getenv("GEMINI_API_KEY")
    if not gemini_key:
        return "neutral"
    
    genai.configure(api_key=gemini_key)
    model = genai.GenerativeModel('gemini-2.5-flash')
    
    prompt = f"Analyze the sentiment of this recruiter's reply to a cold application. Categorize it strictly as one of: positive, negative, or neutral. Return only the single word. \n\nReply:\n{text}"
    try:
        response = model.generate_content(prompt)
        sentiment = response.text.strip().lower()
        if "positive" in sentiment:
            return "positive"
        if "negative" in sentiment:
            return "negative"
        if "neutral" in sentiment:
            return "neutral"
        return "neutral"
    except Exception as e:
        print(f"Sentiment analysis call failed: {e}")
        return "neutral"

def process_inbound_reply_simulation(from_email: str, body_text: str):
    """
    Simulates the inbound reply webhook logic of handle-email-events Deno function.
    """
    # 1. Resolve contact by email
    contact_res = supabase.table("contacts").select("id").eq("email", from_email.lower().strip()).execute()
    assert contact_res.data, f"Contact with email {from_email} not found in database."
    contact_id = contact_res.data[0]["id"]
    
    # 2. Get latest application for this contact
    app_res = supabase.table("applications").select("id").eq("contact_id", contact_id).order("created_at", desc=True).limit(1).execute()
    assert app_res.data, f"No applications found for contact_id {contact_id}."
    app_id = app_res.data[0]["id"]
    
    # 3. Analyze sentiment of body_text
    sentiment = analyze_sentiment_python(body_text)
    
    # 4. Update application status to replied and reply_sentiment
    supabase.table("applications").update({
        "status": "replied",
        "reply_sentiment": sentiment
    }).eq("id", app_id).execute()
    
    # Return details for validation
    return app_id, sentiment

def test_recruiter_sentiment_analysis():
    """
    Verifies that the Gemini sentiment classifier correctly classifies recruiter messages.
    """
    pos_reply = "Hi Aman, we loved your projects (Knowledge-Synthesizer, discord-ai). We would love to hop on a call to discuss the internship next Tuesday at 10 AM."
    neg_reply = "Hi Aman, thank you for reaching out. Unfortunately, we are not hiring AI/ML interns at this moment. We will keep your resume on file."
    neu_reply = "Thanks for the email. Your message has been received."
    
    assert analyze_sentiment_python(pos_reply) == "positive"
    assert analyze_sentiment_python(neg_reply) == "negative"
    assert analyze_sentiment_python(neu_reply) == "neutral"

def test_recruiter_webhook_update_db():
    """
    Verifies that simulating a positive recruiter response from amanamarjit243222@gmail.com updates the db correctly.
    """
    email = "amanamarjit243222@gmail.com"
    reply_body = "We are interested in your profile for the Google AI/ML role! Let's schedule a call."
    
    # Resolve contact and reset status to 'sent' first so we can verify the transition to 'replied'
    contact_res = supabase.table("contacts").select("id").eq("email", email).execute()
    assert contact_res.data, f"Required contact {email} not found. Please run seed script first."
    contact_id = contact_res.data[0]["id"]
    
    app_res = supabase.table("applications").select("id").eq("contact_id", contact_id).order("created_at", desc=True).limit(1).execute()
    assert app_res.data, f"No applications found for contact {email}. Run llm_pipeline first."
    app_id = app_res.data[0]["id"]
    
    # Set status back to 'sent' to verify transition
    supabase.table("applications").update({"status": "sent", "reply_sentiment": None}).eq("id", app_id).execute()
    
    # Simulate inbound reply webhook
    sim_app_id, sentiment = process_inbound_reply_simulation(email, reply_body)
    
    assert sim_app_id == app_id
    assert sentiment == "positive"
    
    # Fetch updated application from database
    updated_app = supabase.table("applications").select("*").eq("id", app_id).execute().data[0]
    assert updated_app["status"] == "replied"
    assert updated_app["reply_sentiment"] == "positive"
