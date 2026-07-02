import os
import sys
import datetime
import logging

# Add parent directory to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from scripts.db_client import (
    supabase,
    retry_api_call,
    logger
)
from scripts.sender import send_resend_email, send_telegram_notification
from scripts.llm_pipeline import CANDIDATE_PROFILE, run_combined_critique

# Follow-up email template / system prompt
FOLLOWUP_SYSTEM_PROMPT = """
You are drafting a polite, brief cold email follow-up for a B.Tech Computer Science student seeking an internship.

CONSTRAINTS:
1. BREVITY: Keep it extremely short (30 to 50 words).
2. TONE: Professional, helpful, and non-intrusive. Just checking in on the previous internship inquiry.
3. STUDENT STYLE: Frame candidate as a B.Tech Computer Science student. Never mention agency or freelance branding names.
4. NO attachments reference.
5. Return ONLY the raw draft content in your response. No formatting wrappers.
"""

@retry_api_call
def draft_followup_email(original_email_body: str, company: str, role_title: str) -> str:
    """
    Calls LLM to draft a follow-up email.
    """
    prompt = f"""
Company: {company}
Role: {role_title}
Original Outreach:
{original_email_body}

Draft a polite follow-up matching the instructions.
"""
    api_key = os.getenv("GROQ_API_KEY")
    if api_key:
        from groq import Groq
        client = Groq(api_key=api_key)
        chat_completion = client.chat.completions.create(
            messages=[
                {"role": "system", "content": FOLLOWUP_SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ],
            model="llama-3.1-8b-instant",
            temperature=0.7
        )
        return chat_completion.choices[0].message.content.strip()
        
    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key:
        import google.generativeai as genai
        genai.configure(api_key=gemini_key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        response = model.generate_content([FOLLOWUP_SYSTEM_PROMPT, prompt])
        return response.text.strip()
        
    raise ValueError("No LLM API keys configured.")

def check_and_send_followups():
    """
    Pulls applications sent > 7 days ago, validates, and drafts/sends follow-up.
    """
    if not supabase:
        logger.error("Supabase client is not initialized.")
        return
        
    # Calculate cutoff date (> 7 days ago)
    cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)).isoformat()
    
    # Select applications
    res = supabase.table("applications").select("*, listings(*), contacts(*)").eq("status", "sent").eq("followup_sent", False).lt("sent_at", cutoff).execute()
    apps_due = res.data or []
    
    if not apps_due:
        logger.info("No follow-ups currently due.")
        return
        
    logger.info(f"Found {len(apps_due)} applications due for follow-ups.")
    
    send_disabled = os.getenv("SEND_DISABLED", "false").lower() == "true"
    supabase_url = os.getenv("SUPABASE_URL")
    
    for app in apps_due:
        app_id = app["id"]
        contact = app["contacts"]
        listing = app["listings"]
        
        email = contact["email"]
        company = contact["company"]
        role_title = listing["title"]
        
        # 1. Critical Suppression Check
        if contact["suppressed"]:
            logger.warning(f"Contact {email} was suppressed after initial send. Cancelling follow-up for Application {app_id}.")
            supabase.table("applications").update({
                "status": "cancelled",
                "followup_sent": True,
                "followup_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
            }).eq("id", app_id).execute()
            continue
            
        # 2. Draft Follow-up
        try:
            logger.info(f"Drafting follow-up for {email}...")
            followup_body = draft_followup_email(app["email_body"], company, role_title)
        except Exception as e:
            logger.error(f"Failed to draft follow-up for Application {app_id}: {e}")
            continue
            
        # 3. Quality Critique check
        try:
            critique = run_combined_critique(followup_body, [], listing["description"] or "")
            score = critique.get("score", 0)
            verifiable = critique.get("verifiable_check", False)
            
            if score < 6 or not verifiable:
                logger.warning(f"Follow-up for {email} failed quality gate (Score: {score}). Skipping this cycle.")
                continue
        except Exception as e:
            logger.warning(f"Critique audit failed for follow-up on App {app_id}: {e}. Proceeding with caution.")
            
        # 4. Compile footer opt-out
        signature_footer = f"""
<br><br>
---<br>
Aman Amarjit<br>
B.Tech Computer Science &amp; Engineering Student<br>
Indira Gandhi Institute of Technology (IGIT), Sarang<br>
Dhenkanal, Odisha, India<br>
Seeking AI/Backend Internships (Summer/Fall 2026)
"""
        raw_followup = followup_body.strip()
        if raw_followup.startswith("Subject:"):
            parts = raw_followup.split("\n", 1)
            subject = parts[0].replace("Subject:", "").strip()
            full_followup_body = f"{parts[1].strip()}{signature_footer}"
        else:
            subject = f"Follow-up: Internship Inquiry - {role_title}"
            full_followup_body = f"{raw_followup}{signature_footer}"
        
        # 5. Dispatch
        if send_disabled:
            logger.info(f"DRY RUN: Would have sent follow-up to {email}. Updating follow-up status.")
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            supabase.table("applications").update({
                "followup_sent": True,
                "followup_at": now_iso
            }).eq("id", app_id).execute()
        else:
            try:
                send_id = send_resend_email(email, subject, full_followup_body)
                logger.info(f"Follow-up dispatched. Resend ID: {send_id}")
                
                # Send copy to Telegram
                send_telegram_notification(email, subject, full_followup_body)
                
                now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                supabase.table("applications").update({
                    "followup_sent": True,
                    "followup_at": now_iso
                }).eq("id", app_id).execute()
            except Exception as e:
                logger.error(f"Resend follow-up dispatch failed for App {app_id}: {e}")

def main():
    logger.info("Starting Gated Follow-up Pipeline.")
    check_and_send_followups()
    logger.info("Follow-up Pipeline completed.")

if __name__ == "__main__":
    main()
