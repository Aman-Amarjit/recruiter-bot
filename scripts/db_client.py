import os
import logging
from dotenv import load_dotenv
from supabase import create_client, Client
from tenacity import retry, stop_after_attempt, wait_exponential, before_sleep_log

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("RTA_AutoApply")

# Load environment variables
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    logger.warning("Supabase environment variables are missing.")

# Initialize client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY) if SUPABASE_URL and SUPABASE_SERVICE_KEY else None

# Resilience decorator: 3 attempts, exponential backoff starting at 2s up to 10s
retry_api_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    before_sleep=before_sleep_log(logger, logging.WARNING)
)

def check_source_status(source_name: str) -> bool:
    """
    Checks if a scraper source is currently active (not disabled by the circuit breaker).
    Returns True if allowed to run, False otherwise.
    """
    if not supabase:
        return True
    try:
        res = supabase.table("source_status").select("*").eq("source", source_name).execute()
        if res.data:
            from datetime import datetime, timezone
            disabled_until_str = res.data[0].get("disabled_until")
            if disabled_until_str:
                # Parse timestamp (e.g. 2026-07-01T15:00:00+00:00)
                # handle timezone offset
                disabled_until = datetime.fromisoformat(disabled_until_str.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) < disabled_until:
                    logger.info(f"Source '{source_name}' is disabled/paused until {disabled_until_str}")
                    return False
        return True
    except Exception as e:
        logger.error(f"Error checking source status for {source_name}: {e}")
        return True

def disable_source(source_name: str, reason: str, hours: int = 48):
    """
    Disables a scraper source for a specific duration under the circuit breaker.
    """
    if not supabase:
        return
    try:
        from datetime import datetime, timedelta, timezone
        disabled_until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
        
        # Upsert status
        supabase.table("source_status").upsert({
            "source": source_name,
            "disabled_until": disabled_until,
            "last_failure_reason": reason,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).execute()
        logger.warning(f"Circuit breaker tripped: Source '{source_name}' has been disabled for {hours}h. Reason: {reason}")
    except Exception as e:
        logger.error(f"Error setting source status for {source_name}: {e}")

def record_sourcing_success(source_name: str):
    """
    Resets the circuit breaker state on a successful scraper run.
    """
    if not supabase:
        return
    try:
        from datetime import datetime, timezone
        supabase.table("source_status").upsert({
            "source": source_name,
            "disabled_until": None,
            "consecutive_failures": 0,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).execute()
    except Exception as e:
        logger.error(f"Error updating sourcing success for {source_name}: {e}")

def log_scraper_failure(source_name: str, reason: str):
    """
    Logs a single failure attempt. Trip circuit breaker if consecutive failures reaches 2.
    """
    if not supabase:
        return
    try:
        from datetime import datetime, timezone
        res = supabase.table("source_status").select("*").eq("source", source_name).execute()
        consecutive = 1
        if res.data:
            consecutive = res.data[0].get("consecutive_failures", 0) + 1
        
        if consecutive >= 2:
            disable_source(source_name, f"Consecutive failures: {reason}")
        else:
            supabase.table("source_status").upsert({
                "source": source_name,
                "consecutive_failures": consecutive,
                "last_failure_reason": reason,
                "updated_at": datetime.now(timezone.utc).isoformat()
            }).execute()
            logger.info(f"Source '{source_name}' logged failure attempt #{consecutive}: {reason}")
    except Exception as e:
        logger.error(f"Error logging failure for {source_name}: {e}")

def get_active_domain_tag() -> str:
    """
    Returns the active domain tag according to the domain rotation schedule.
    - AI/ML (ai_ml): Mondays (0) and Thursdays (3)
    - Cybersecurity (cybersecurity): Tuesdays (1) and Fridays (4)
    - Robotics (robotics): Wednesdays (2) and Saturdays (5)
    - Fallback: Sundays (6) -> ai_ml
    """
    import datetime
    weekday = datetime.date.today().weekday()
    if weekday in [0, 3]:
        return "ai_ml"
    elif weekday in [1, 4]:
        return "cybersecurity"
    elif weekday in [2, 5]:
        return "robotics"
    else:
        return "ai_ml"

