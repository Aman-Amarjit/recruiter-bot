import os
import sys
import json
import logging
from datetime import datetime, timezone

# Add parent directory to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from scripts.db_client import (
    supabase,
    retry_api_call,
    logger
)
from scripts.resume_builder import build_tailored_resume, CANDIDATE_PROFILE

# System instructions for drafting emails
EMAIL_DRAFTER_SYSTEM_PROMPT = """
You are Aman Amarjit — a second-year B.Tech Computer Science student writing a cold outreach email directly yourself to seek an internship. Write entirely in first person, in your own voice, as if you are personally sending this message.

CRITICAL INSTRUCTIONS:
1. PERSONAL VOICE: Write as "I" — Aman Amarjit — directly. The reader must feel this is a genuine personal message from a real student, not a templated cold pitch.
2. INTERNSHIP INTENT: Clearly express that you are looking for an **internship** opportunity at this specific company. Reference something specific about the company or role to show you have done your research.
3. COMPANY-SPECIFIC: Mention the company or the role's domain by name. Show genuine interest in what they do and why you specifically want to work with them — not generically.
4. STUDENT CONTEXT: You are a second-year B.Tech CSE student (graduating 2029, IGIT Sarang). Highlight your academic foundation and projects to demonstrate depth and hands-on skills.
5. NO HALLUCINATION: Only use facts, projects, technologies, and skills present in the Candidate Profile. Never invent achievements, grades, or roles.
6. CONCISE: Keep the email strictly between 80 to 130 words. Every sentence must earn its place.
7. FORBIDDEN PHRASES: Never use:
   - "I hope this email finds you well"
   - "I am writing to express my interest"
   - "Please find my attached resume"
   - "I wanted to reach out"
   - Any agency, freelancer branding, or brand names (like "Reshape The Algorithm" or "RTA")
8. NATURAL CTA: End with a specific, low-commitment ask. Offer to share a portfolio link or say "Happy to share more if there's interest." Never ask to schedule or hop on a call.
9. FOOTER: Do NOT include a signature block or physical address (these are appended automatically by the sender).
10. Return BOTH a personalized, natural subject line and the email body. The output MUST start with "Subject: " on the very first line, followed by the subject line, then a blank line, and then the email body. Do not include markdown formatting or quotes around it.
11. SALUTATION: Always begin with a professional greeting on its own line. Use the contact's name if available (e.g. "Hi [Name],"). If the contact's name is not available or is generic, address the team or department specifically based on the company and role (e.g. "Hi Google AI/ML Team," or "Hello Google Engineering Team,"). Never use generic greetings like "Hi there".
12. INTRODUCTION: Immediately after the salutation, include one concise sentence introducing yourself: your name, and that you are a B.Tech CSE student at IGIT Sarang. Example: "I am Aman Amarjit, a second-year B.Tech CSE student at IGIT Sarang."
13. RESUME ATTACHMENT: State clearly that you have attached your tailored resume to the email (e.g., "I have attached my tailored resume to this email.") instead of linking to URLs.
14. SINGLE PROJECT FOCUS: Focus on exactly ONE highly relevant project from your Profile instead of listing multiple names. Describe this project with one concise sentence highlighting a concrete outcome (what you built, what it achieved, and the key technologies used).
15. FORMAL TONE: The tone MUST be polite, respectful, and professional. Avoid overly casual language, contractions (e.g., write "I am" instead of "I'm", "I have" instead of "I've", "do not" instead of "don't"), or slang. Ensure the email reads as a polished, formal business inquiry.
"""


# Consolidated critique prompt
CRITIQUE_SYSTEM_PROMPT = """
You are an independent safety auditor verifying job outreach emails and resume selections.
Your job is to rate the draft quality and verify absolute truthfulness against the candidate's profile.

Evaluate the following factors:
1. TRUTHFULNESS: Check if all technologies, projects, and claims in the email and resume selection exist in the Candidate Profile. Fail if anything is fabricated.
2. SPAM TRIGGERS: Check for generic template phrases, clickbaity headlines, or over-the-top sales language.
3. GENERICNESS: Score how specific and tailored the pitch is to the job description (10 is highly tailored, 1 is generic).
4. FORMAL TONE: Verify that the email maintains a highly professional, formal business tone and does not use informal contractions (like "I'm", "I've", "don't").

Return a JSON object:
{
  "score": 1-10,
  "verifiable_check": "Boolean: true if every claim matches profile.json, false otherwise",
  "reason": "Detailed one-line explanation of the score"
}
"""

@retry_api_call
def draft_cold_email(job_title: str, company: str, job_description: str, domain_tag: str, recruiter_name: str = None) -> str:
    """
    Calls the LLM (Groq with Gemini fallback) to draft a tailored cold email.
    """
    domain_profile = CANDIDATE_PROFILE["domains"].get(domain_tag, {})
    
    prompt = f"""
Candidate Name: {CANDIDATE_PROFILE['name']}
Recruiter Name: {recruiter_name or 'N/A'}
Candidate Context: Second-year B.Tech Computer Science & Engineering student at Indira Gandhi Institute of Technology (IGIT), Sarang (Class of 2029). Focusing on AI/ML and systems engineering, recently completed a full-stack analytics engine contract project for a client in Canada.
Candidate Profile (domain-specific): {json.dumps(domain_profile, indent=2)}

Job Opportunity:
Title: {job_title}
Company: {company}
Description:
{job_description}

Write a cold outreach email directly as Aman seeking an internship at {company}.
If Recruiter Name is provided and not 'N/A', address the recruiter by name in the greeting salutation (e.g., "Hi [Recruiter Name],"). Otherwise, address the specific team (e.g., "Hi Google Engineering Team,").
Be specific about the company and role. Follow all system instructions exactly.
"""
    api_key = os.getenv("GROQ_API_KEY")
    if api_key:
        try:
            from groq import Groq
            client = Groq(api_key=api_key)
            chat_completion = client.chat.completions.create(
                messages=[
                    {"role": "system", "content": EMAIL_DRAFTER_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                model="llama-3.3-70b-versatile",
                temperature=0.7
            )
            return chat_completion.choices[0].message.content.strip()
        except Exception as e:
            logger.warning(f"Groq email drafting failed: {e}. Falling back to Gemini.")
        
    # Fallback to Gemini
    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key:
        import google.generativeai as genai
        genai.configure(api_key=gemini_key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        response = model.generate_content([EMAIL_DRAFTER_SYSTEM_PROMPT, prompt])
        return response.text.strip()
        
    raise ValueError("No LLM API keys configured.")

@retry_api_call
def run_combined_critique(email_body: str, resume_selection: list, job_description: str) -> dict:
    """
    Runs a single consolidated critique call evaluating the truthfulness and spam rating of both assets.
    """
    prompt = f"""
Candidate Profile: {json.dumps(CANDIDATE_PROFILE, indent=2)}
Job Description: {job_description}

Email Draft:
{email_body}

Selected Resume Projects:
{json.dumps(resume_selection, indent=2)}

Rate and critique the assets according to the system instructions. Return strictly JSON.
"""
    api_key = os.getenv("GROQ_API_KEY")
    if api_key:
        try:
            from groq import Groq
            client = Groq(api_key=api_key)
            chat_completion = client.chat.completions.create(
                messages=[
                    {"role": "system", "content": CRITIQUE_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                model="llama-3.1-8b-instant", # Use lighter model for critique to save 70b limits
                temperature=0.1,
                response_format={"type": "json_object"}
            )
            return json.loads(chat_completion.choices[0].message.content)
        except Exception as e:
            logger.warning(f"Groq combined critique failed: {e}. Falling back to Gemini.")
        
    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key:
        import google.generativeai as genai
        genai.configure(api_key=gemini_key)
        model = genai.GenerativeModel('gemini-2.5-flash', generation_config={"response_mime_type": "application/json"})
        response = model.generate_content([CRITIQUE_SYSTEM_PROMPT, prompt])
        return json.loads(response.text)
        
    raise ValueError("No LLM API keys configured.")

def process_application(app):
    """
    Orchestrates drafting, resume build, and critique for a single application row.
    """
    app_id = app["id"]
    listing = app["listings"]
    contact = app["contacts"]
    
    job_title = listing["title"]
    company = listing["company"]
    job_description = listing["description"] or ""
    domain_tag = listing["domain_tag"]
    
    logger.info(f"Processing personalization for Application {app_id}: {job_title} at {company}")
    
    # 1. Draft tailored email
    try:
        recruiter_name = contact.get("name")
        email_body = draft_cold_email(job_title, company, job_description, domain_tag, recruiter_name=recruiter_name)
    except Exception as e:
        logger.error(f"Email drafting failed for App {app_id}: {e}")
        supabase.table("applications").update({"status": "failed"}).eq("id", app_id).execute()
        return
        
    # 2. Tailor resume (incorporates PDF compilation + local pypdf ATS validation)
    resume_url = build_tailored_resume(app_id, job_description, domain_tag)
    if not resume_url:
        logger.warning(f"Resume building failed/halted for App {app_id}. Halting application.")
        return
        
    # Retrieve updated application row with resume project selections for audit
    app_details = supabase.table("applications").select("*").eq("id", app_id).execute().data[0]
    resume_selection = app_details.get("resume_project_selection") or []
    
    # 3. Consolidated Critique Gate check
    try:
        critique = run_combined_critique(email_body, resume_selection, job_description)
        score = critique.get("score", 0)
        verifiable = critique.get("verifiable_check", False)
        reason = critique.get("reason", "No reason provided")
        
        logger.info(f"App {app_id} critique score: {score}. Verifiable: {verifiable}. Reason: {reason}")
        
        auto_approve = os.getenv("AUTO_APPROVE", "false").lower() == "true"
        
        if auto_approve or (score >= 7 and verifiable):
            # Succeeded & Approved
            supabase.table("applications").update({
                "status": "approved",
                "email_body": email_body,
                "critique_score": score
            }).eq("id", app_id).execute()
            if auto_approve:
                logger.info(f"Application {app_id} AUTO-APPROVED (AUTO_APPROVE is enabled).")
            else:
                logger.info(f"Application {app_id} APPROVED.")
        elif score in [4, 5, 6] and verifiable:
            # Regenerate once
            logger.info(f"Score {score} is borderline. Regenerating email once with critique feedback...")
            refined_system_prompt = f"{EMAIL_DRAFTER_SYSTEM_PROMPT}\nRefining instructions: Make draft less generic based on previous critique feedback: {reason}"
            
            # Re-draft
            email_retry = draft_cold_email(job_title, company, job_description, domain_tag)
            
            # Re-critique
            critique_retry = run_combined_critique(email_retry, resume_selection, job_description)
            score_retry = critique_retry.get("score", 0)
            verifiable_retry = critique_retry.get("verifiable_check", False)
            
            if auto_approve or (score_retry >= 7 and verifiable_retry):
                supabase.table("applications").update({
                    "status": "approved",
                    "email_body": email_retry,
                    "critique_score": score_retry
                }).eq("id", app_id).execute()
                logger.info(f"Application {app_id} APPROVED after retry.")
            else:
                supabase.table("applications").update({
                    "status": "held",
                    "email_body": email_retry,
                    "critique_score": score_retry
                }).eq("id", app_id).execute()
                logger.warning(f"Application {app_id} HELD after retry failure (Score: {score_retry}).")
        else:
            # Score < 4 or unverifiable claims detected
            supabase.table("applications").update({
                "status": "held",
                "email_body": email_body,
                "critique_score": score
            }).eq("id", app_id).execute()
            logger.warning(f"Application {app_id} HELD (Score: {score}, Verifiable: {verifiable}).")
            
    except Exception as e:
        logger.error(f"Critique call failed for App {app_id}: {e}")
        # Save as drafted but held for safety
        supabase.table("applications").update({
            "status": "held",
            "email_body": email_body,
            "critique_score": 1
        }).eq("id", app_id).execute()

def main():
    if not supabase:
        logger.error("Supabase client is not initialized.")
        return
        
    logger.info("Starting Personalization & Resume Tailoring Pipeline.")
    
    # 1. Select applications in drafting or recheck status, joining listing and contact details
    try:
        drafts_res = supabase.table("applications").select("*, listings(*), contacts(*)").in_("status", ["drafting", "recheck"]).execute()
        drafts = drafts_res.data or []
        
        if not drafts:
            logger.info("No applications in 'drafting' or 'recheck' status.")
            return
            
        logger.info(f"Found {len(drafts)} applications requiring personalization.")
        
        # 2. Lock active records by setting status to 'generating' for idempotency
        for app in drafts:
            supabase.table("applications").update({"status": "generating"}).eq("id", app["id"]).execute()
            
        # 3. Process each locked application
        for app in drafts:
            # Refresh app row (handling potential external status adjustments)
            current_app_res = supabase.table("applications").select("status").eq("id", app["id"]).execute()
            if current_app_res.data and current_app_res.data[0]["status"] == "generating":
                process_application(app)
                
        logger.info("Personalization & Resume Tailoring Pipeline completed.")
    except Exception as e:
        logger.error(f"Personalization pipeline crashed: {e}")

if __name__ == "__main__":
    main()
