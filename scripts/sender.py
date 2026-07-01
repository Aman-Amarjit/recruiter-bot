import os
import sys
import math
import httpx
import logging
import datetime
import smtplib
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Add parent directory to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from scripts.db_client import (
    supabase,
    retry_api_call,
    logger
)

@retry_api_call
def send_resend_email(to_email: str, subject: str, html_content: str) -> str:
    """
    Dispatches email using SMTP (Gmail) if configured, otherwise falls back to Resend API.
    """
    smtp_email = os.getenv("SMTP_EMAIL")
    smtp_password = os.getenv("SMTP_PASSWORD")
    
    if smtp_email and smtp_password:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = smtp_email
        msg["To"] = to_email
        
        # Attach HTML
        msg.attach(MIMEText(html_content, "html"))
        
        # Send via SSL
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(smtp_email, smtp_password)
            server.sendmail(smtp_email, [to_email], msg.as_string())
            
        return f"smtp-{uuid.uuid4()}"

    api_key = os.getenv("RESEND_API_KEY")
    sender = os.getenv("RESEND_SENDER_EMAIL", "applications@apply.yourdomain.com")
    
    if not api_key:
        raise ValueError("Resend API key is missing.")
        
    url = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "from": sender,
        "to": [to_email],
        "subject": subject,
        "html": html_content
    }
    
    response = httpx.post(url, headers=headers, json=payload, timeout=15)
    response.raise_for_status()
    return response.json().get("id")

def get_pacing_batch_size() -> int:
    """
    Calculates batch size dynamically based on slot pacing math.
    """
    if not supabase:
        return 0
        
    today_str = datetime.date.today().isoformat()
    
    # 1. Retrieve or initialize today's daily counter
    res = supabase.table("daily_counters").select("*").eq("date", today_str).execute()
    if not res.data:
        # Fetch yesterday's counter to preserve cap ramp
        prev_res = supabase.table("daily_counters").select("*").order("date", desc=True).limit(1).execute()
        active_cap = prev_res.data[0]["cap"] if prev_res.data else 5
        
        # Insert new counter
        supabase.table("daily_counters").insert({
            "date": today_str,
            "sends_today": 0,
            "cap": active_cap
        }).execute()
        
        sends_today = 0
        cap = active_cap
    else:
        sends_today = res.data[0]["sends_today"]
        cap = res.data[0]["cap"]
        
    remaining_sends = max(0, cap - sends_today)
    if remaining_sends <= 0:
        logger.info("Daily send cap reached for today.")
        return 0
        
    # 2. Determine remaining slots today (slots run at 05:30, 07:30, 09:30, 11:30, 13:30 UTC)
    current_hour_utc = datetime.datetime.utcnow().hour
    if current_hour_utc <= 5:
        remaining_slots = 5
    elif current_hour_utc <= 7:
        remaining_slots = 4
    elif current_hour_utc <= 9:
        remaining_slots = 3
    elif current_hour_utc <= 11:
        remaining_slots = 2
    else:
        remaining_slots = 1
        
    batch_size = int(math.ceil(remaining_sends / remaining_slots)) if remaining_slots > 0 else 0
    logger.info(f"Pacing metrics: Cap={cap}, Sends Today={sends_today}, Remaining Sends={remaining_sends}, Remaining Slots={remaining_slots} -> Batch Size={batch_size}")
    return batch_size

def process_send_queue():
    """
    Pulls approved drafts, checks suppression/cooldown gates, appends footer, and sends.
    """
    if not supabase:
        logger.error("Supabase client is not initialized.")
        return
        
    batch_size = get_pacing_batch_size()
    if batch_size <= 0:
        logger.info("No send budget remaining for this slot.")
        return
        
    # Fetch approved applications
    approved_res = supabase.table("applications").select("*, listings(*), contacts(*)").eq("status", "approved").limit(batch_size).execute()
    applications = approved_res.data or []
    
    if not applications:
        logger.info("No approved drafts found.")
        return
        
    logger.info(f"Acquired {len(applications)} applications to send in this batch.")
    
    send_disabled = os.getenv("SEND_DISABLED", "false").lower() == "true"
    supabase_url = os.getenv("SUPABASE_URL")
    
    for app in applications:
        app_id = app["id"]
        contact = app["contacts"]
        listing = app["listings"]
        
        email = contact["email"]
        company = contact["company"]
        role_title = listing["title"]
        
        # 1. Gate Checks: Suppression & Cooldown
        if contact["suppressed"]:
            logger.warning(f"Contact {email} is suppressed. Skipping application {app_id}.")
            supabase.table("applications").update({"status": "failed"}).eq("id", app_id).execute()
            continue
            
        # 30-day cooldown check
        last_emailed_str = contact.get("last_emailed_at")
        if last_emailed_str:
            last_emailed = datetime.datetime.fromisoformat(last_emailed_str.replace("Z", "+00:00"))
            time_since = datetime.datetime.now(datetime.timezone.utc) - last_emailed
            if time_since.days < 30:
                logger.warning(f"Contact {email} was emailed {time_since.days} days ago (cooldown limit is 30). Skipping App {app_id}.")
                supabase.table("applications").update({"status": "held"}).eq("id", app_id).execute()
                continue
                
        # 2. Lock application for idempotency
        supabase.table("applications").update({"status": "sending"}).eq("id", app_id).execute()
        
        # 3. Compile email body and footer signature
        # Generate unsubscribe URL pointing to our Supabase Edge Function
        opt_out_url = f"{supabase_url}/functions/v1/handle-email-events?email={email}"
        
        signature_footer = f"""
<br><br>
---<br>
Aman Amarjit<br>
Independent Software Developer & Freelancer<br>
Dhenkanal, Odisha, India<br>
<br>
<font size="1" color="#888888">
You are receiving this outreach as a careers contact for {company}. If you no longer wish to receive freelance proposals, you can unsubscribe instantly by clicking <a href="{opt_out_url}">here</a>.
</font>
"""
        email_body = f"{app['email_body']}{signature_footer}"
        subject = f"Freelance Collaboration / Internship - {role_title}"
        
        # 4. Dispatch Email
        if send_disabled:
            logger.info(f"DRY RUN: Would have sent email to {email} ({role_title} at {company}). Status updated to 'sent'.")
            # Update database in dry-run mode to simulate success
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            supabase.table("applications").update({"status": "sent", "sent_at": now_iso}).eq("id", app_id).execute()
            supabase.table("contacts").update({"last_emailed_at": now_iso}).eq("id", contact["id"]).execute()
            supabase.table("send_log").insert({"application_id": app_id, "sent_at": now_iso}).execute()
            
            # Increment sends count today
            today_str = datetime.date.today().isoformat()
            supabase.rpc("increment_sends_today", {"target_date": today_str}).execute()
        else:
            try:
                logger.info(f"Sending email to {email} for {role_title}...")
                send_id = send_resend_email(email, subject, email_body)
                logger.info(f"Email sent successfully. Resend ID: {send_id}")
                
                now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                supabase.table("applications").update({"status": "sent", "sent_at": now_iso}).eq("id", app_id).execute()
                supabase.table("contacts").update({"last_emailed_at": now_iso}).eq("id", contact["id"]).execute()
                supabase.table("send_log").insert({"application_id": app_id, "sent_at": now_iso}).execute()
                
                # Increment sends count today
                today_str = datetime.date.today().isoformat()
                supabase.rpc("increment_sends_today", {"target_date": today_str}).execute()
                
            except Exception as e:
                logger.error(f"Failed to dispatch email for Application {app_id} to {email}: {e}")
                supabase.table("applications").update({"status": "failed"}).eq("id", app_id).execute()

def main():
    logger.info("Starting Gated Sending Cycle.")
    process_send_queue()
    logger.info("Sending Cycle completed.")

if __name__ == "__main__":
    main()
