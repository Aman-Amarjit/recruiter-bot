import os
import sys
from datetime import datetime, timezone
from dotenv import load_dotenv

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
load_dotenv()

from scripts.db_client import supabase, logger

def main():
    if not supabase:
        logger.error("Supabase client not initialized.")
        return

    logger.info("Identifying outdated applications for recheck...")

    # Threshold date: July 4th, 2026
    threshold_date = datetime(2026, 7, 4, 0, 0, 0, tzinfo=timezone.utc)

    # Fetch applications that are held, approved, or resume_failed
    res = supabase.table("applications").select("id, status, created_at, resume_url, contacts(email)").in_("status", ["held", "approved", "resume_failed", "drafting"]).execute()
    apps = res.data or []
    
    outdated_apps = []
    for app in apps:
        created_at_str = app.get("created_at")
        if created_at_str:
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            if created_at < threshold_date:
                outdated_apps.append(app)

    logger.info(f"Found {len(outdated_apps)} outdated applications pending in queue.")

    if not outdated_apps:
        logger.info("No outdated applications found in queue.")
        return

    # Update their status to 'recheck' so they will be automatically regenerated
    updated_count = 0
    for app in outdated_apps:
        app_id = app["id"]
        status = app["status"]
        recruiter_email = app.get("contacts", {}).get("email") if app.get("contacts") else "N/A"
        logger.info(f"Flagging Application {app_id} (status: {status}, to: {recruiter_email}, created: {app['created_at']}) for recheck.")
        
        try:
            supabase.table("applications").update({
                "status": "recheck",
                "failure_reason": None  # Clear any previous resume build failure details
            }).eq("id", app_id).execute()
            updated_count += 1
        except Exception as e:
            logger.error(f"Failed to update application {app_id}: {e}")

    logger.info(f"Successfully flagged {updated_count} applications for recheck.")

if __name__ == "__main__":
    main()
