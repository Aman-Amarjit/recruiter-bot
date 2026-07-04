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

import html

def send_telegram_notification(to_email: str, subject: str, body_content: str, pdf_data: bytes = None):
    """
    Forwards a copy of the sent email metadata and body to Telegram, along with the PDF attachment if present.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
        
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    
    escaped_body = html.escape(body_content)
    text_message = f"✉️ <b>New Outreach Email Sent!</b>\n\n" \
                   f"<b>To:</b> {to_email}\n" \
                   f"<b>Subject:</b> {subject}\n"
    if pdf_data:
        text_message += f"<b>Attachment:</b> Aman_Amarjit_Resume.pdf\n"
    text_message += f"\n<b>Body:</b>\n{escaped_body}"
    
    # Trim to stay within Telegram 4096 character limit
    if len(text_message) > 4000:
        text_message = text_message[:3970] + "\n...[message truncated]"
        
    payload = {
        "chat_id": chat_id,
        "text": text_message,
        "parse_mode": "HTML"
    }
    try:
        httpx.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.warning(f"Failed to send Telegram text notification: {e}")
        
    # If pdf_data is present, upload the document as a file
    if pdf_data:
        doc_url = f"https://api.telegram.org/bot{token}/sendDocument"
        files = {
            "document": ("Aman_Amarjit_Resume.pdf", pdf_data, "application/pdf")
        }
        data = {
            "chat_id": chat_id,
            "caption": f"📄 Tailored Resume PDF for {to_email}"
        }
        try:
            httpx.post(doc_url, data=data, files=files, timeout=20)
        except Exception as e:
            logger.warning(f"Failed to send PDF document to Telegram: {e}")

@retry_api_call
def send_resend_email(to_email: str, subject: str, html_content: str, pdf_data: bytes = None) -> str:
    """
    Dispatches email using SMTP (Gmail) if configured, otherwise falls back to Resend API.
    """
    smtp_email = os.getenv("SMTP_EMAIL")
    smtp_password = os.getenv("SMTP_PASSWORD")
    
    if smtp_email and smtp_password:
        msg = MIMEMultipart("mixed")  # Use mixed to support attachments along with alternative HTML
        msg["Subject"] = subject
        msg["From"] = smtp_email
        msg["To"] = to_email
        
        # Attach HTML
        alternative = MIMEMultipart("alternative")
        alternative.attach(MIMEText(html_content, "html"))
        msg.attach(alternative)
        
        # Attach PDF if present
        if pdf_data:
            from email.mime.application import MIMEApplication
            part = MIMEApplication(pdf_data, Name="Aman_Amarjit_Resume.pdf")
            part['Content-Disposition'] = 'attachment; filename="Aman_Amarjit_Resume.pdf"'
            msg.attach(part)
        
        # Send via SSL
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(smtp_email, smtp_password)
            server.sendmail(smtp_email, [to_email], msg.as_string())
            
        return f"smtp-{uuid.uuid4()}"

    api_key = os.getenv("RESEND_API_KEY")
    sender = os.getenv("RESEND_SENDER_EMAIL") or os.getenv("SMTP_EMAIL") or "onboarding@resend.dev"
    
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
    
    if pdf_data:
        import base64
        payload["attachments"] = [
            {
                "content": base64.b64encode(pdf_data).decode("utf-8"),
                "filename": "Aman_Amarjit_Resume.pdf"
            }
        ]
    
    response = httpx.post(url, headers=headers, json=payload, timeout=15)
    response.raise_for_status()
    return response.json().get("id")

def get_pacing_batch_size() -> int:
    """
    Calculates batch size dynamically based on slot pacing math.
    """
    if not supabase:
        return 0
        
    today_str = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    
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
        
        # Download tailored PDF resume to attach directly
        pdf_data = None
        resume_url = app.get("resume_url")
        if resume_url:
            try:
                logger.info(f"Downloading tailored resume from {resume_url} to attach directly...")
                pdf_res = httpx.get(resume_url, timeout=15)
                if pdf_res.status_code == 200:
                    pdf_data = pdf_res.content
                    logger.info("Resume PDF downloaded successfully.")
                else:
                    logger.warning(f"Failed to download resume, status code: {pdf_res.status_code}")
            except Exception as e:
                logger.error(f"Error downloading resume PDF: {e}")

        # 3. Compile email body and footer signature
        signature_footer = f"""
<br><br>
---<br>
Aman Amarjit<br>
B.Tech Computer Science &amp; Engineering Student<br>
Indira Gandhi Institute of Technology (IGIT), Sarang<br>
Dhenkanal, Odisha, India<br>
Seeking AI/Backend Internships (Summer/Fall 2026)
"""
        raw_body = app['email_body'].strip()
        # Clean markdown code blocks if the LLM wrapped the output
        if raw_body.startswith("```"):
            newline_idx = raw_body.find("\n")
            if newline_idx != -1:
                raw_body = raw_body[newline_idx:].strip()
            else:
                raw_body = raw_body[3:].strip()
            if raw_body.endswith("```"):
                raw_body = raw_body[:-3].strip()

        if raw_body.startswith("Subject:"):
            parts = raw_body.split("\n", 1)
            subject = parts[0].replace("Subject:", "").strip()
            email_body = f"{parts[1].strip()}{signature_footer}"
        else:
            subject = f"Internship Inquiry - {role_title}"
            email_body = f"{raw_body}{signature_footer}"
        
        # 4. Dispatch Email
        if send_disabled:
            logger.info(f"DRY RUN: Would have sent email to {email} ({role_title} at {company}). Status updated to 'sent'.")
            # Update database in dry-run mode to simulate success
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            supabase.table("applications").update({"status": "sent", "sent_at": now_iso}).eq("id", app_id).execute()
            supabase.table("contacts").update({"last_emailed_at": now_iso}).eq("id", contact["id"]).execute()
            supabase.table("send_log").insert({"application_id": app_id, "sent_at": now_iso}).execute()
            
            # Increment sends count today
            today_str = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
            supabase.rpc("increment_sends_today", {"target_date": today_str}).execute()
        else:
            try:
                logger.info(f"Sending email to {email} for {role_title}...")
                send_id = send_resend_email(email, subject, email_body, pdf_data=pdf_data)
                logger.info(f"Email sent successfully. Resend ID: {send_id}")
                
                # Send copy to Telegram (including PDF file)
                send_telegram_notification(email, subject, email_body, pdf_data=pdf_data)
                
                now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                supabase.table("applications").update({"status": "sent", "sent_at": now_iso}).eq("id", app_id).execute()
                supabase.table("contacts").update({"last_emailed_at": now_iso}).eq("id", contact["id"]).execute()
                supabase.table("send_log").insert({"application_id": app_id, "sent_at": now_iso}).execute()
                
                # Increment sends count today
                today_str = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
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
