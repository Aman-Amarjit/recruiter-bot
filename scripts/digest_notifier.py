import os
import sys
import httpx
import logging
import datetime

# Add parent directory to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from scripts.db_client import (
    supabase,
    retry_api_call,
    logger
)
from scripts.resume_builder import CANDIDATE_PROFILE

def keep_warm_portfolio():
    """
    Triggers a GET request on the portfolio URL to spin up Render free tiers.
    """
    portfolio_url = os.getenv("PORTFOLIO_URL") or CANDIDATE_PROFILE["links"].get("portfolio")
    if not portfolio_url:
        logger.warning("No portfolio URL configured for keep-warm ping.")
        return
        
    try:
        logger.info(f"Firing keep-warm ping to: {portfolio_url}")
        res = httpx.get(portfolio_url, timeout=15)
        logger.info(f"Keep-warm status code: {res.status_code}")
    except Exception as e:
        logger.warning(f"Keep-warm ping failed: {e}")

def run_auto_cap_ramping() -> tuple:
    """
    Checks the trailing 20 sends to calculate bounce rate.
    Adjusts the daily send cap accordingly.
    Returns (new_cap, bounce_rate_pct, triggered_breaker)
    """
    if not supabase:
        return 5, 0.0, False
        
    try:
        # 1. Fetch trailing 20 sends
        res = supabase.table("send_log").select("bounced").order("sent_at", desc=True).limit(20).execute()
        sends = res.data or []
        
        if len(sends) < 10:
            # Not enough data to compute trailing stats fairly, preserve cap
            today_str = datetime.date.today().isoformat()
            current = supabase.table("daily_counters").select("cap").eq("date", today_str).execute()
            cap = current.data[0]["cap"] if current.data else 5
            return cap, 0.0, False
            
        bounces_count = sum(1 for s in sends if s["bounced"])
        bounce_rate = (bounces_count / len(sends)) * 100.0
        
        # 2. Retrieve current cap
        today_str = datetime.date.today().isoformat()
        current_res = supabase.table("daily_counters").select("cap").eq("date", today_str).execute()
        current_cap = current_res.data[0]["cap"] if current_res.data else 5
        
        new_cap = current_cap
        triggered_breaker = False
        
        if bounce_rate > 5.0:
            # Trigger Circuit Breaker: Reset to 5
            new_cap = 5
            triggered_breaker = True
            logger.warning(f"Trailing bounce rate is high ({bounce_rate:.1f}%). Tripping cap circuit breaker to 5 sends/day.")
        elif bounce_rate == 0.0 and current_cap == 5:
            # Ramp to 10
            new_cap = 10
            logger.info("Bounce rate is 0%. Ramping cap from 5 to 10 sends/day.")
        elif bounce_rate < 5.0 and current_cap == 10:
            # Ramp to 20
            new_cap = 20
            logger.info("Bounce rate is low. Ramping cap from 10 to 20 sends/day.")
            
        # 3. Update the database cap for today & future days
        supabase.table("daily_counters").update({
            "cap": new_cap,
            "bounce_rate_trailing20": bounce_rate
        }).eq("date", today_str).execute()
        
        return new_cap, bounce_rate, triggered_breaker
    except Exception as e:
        logger.error(f"Error running auto-cap ramping: {e}")
        return 5, 0.0, False

def get_funnel_metrics() -> dict:
    """
    Aggregates conversion rates and quantities for the last 24 hours.
    """
    metrics = {
        "sourced": 0, "enriched": 0, "drafted": 0,
        "held": 0, "sent": 0, "replied": 0
    }
    if not supabase:
        return metrics
        
    try:
        # Sourced listings in last 24h
        cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)).isoformat()
        
        res_source = supabase.table("listings").select("id", count="exact").gt("created_at", cutoff).execute()
        metrics["sourced"] = res_source.count or 0
        
        # Enriched contacts
        res_enrich = supabase.table("contacts").select("id", count="exact").gt("created_at", cutoff).execute()
        metrics["enriched"] = res_enrich.count or 0
        
        # Applications created
        res_apps = supabase.table("applications").select("status").gt("created_at", cutoff).execute()
        apps = res_apps.data or []
        
        metrics["drafted"] = len(apps)
        metrics["held"] = sum(1 for a in apps if a["status"] in ["held", "resume_failed"])
        metrics["sent"] = sum(1 for a in apps if a["status"] in ["sent"])
        metrics["replied"] = sum(1 for a in apps if a["status"] in ["replied"])
        
    except Exception as e:
        logger.error(f"Error compiling funnel metrics: {e}")
        
    return metrics

def get_scraper_health() -> str:
    """
    Renders the health statuses of the scrapers.
    """
    if not supabase:
        return "Supabase connection unavailable."
        
    try:
        res = supabase.table("source_status").select("*").execute()
        rows = res.data or []
        
        if not rows:
            return "All scrapers ACTIVE (no historical errors logged)."
            
        health_lines = []
        for r in rows:
            source = r["source"]
            disabled_until_str = r.get("disabled_until")
            
            if disabled_until_str:
                from datetime import datetime, timezone
                disabled_until = datetime.fromisoformat(disabled_until_str.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) < disabled_until:
                    health_lines.append(f"⚠️ <b>{source.upper()}</b>: COOLDOWN (Until {disabled_until_str})")
                    continue
            health_lines.append(f"✅ <b>{source.upper()}</b>: ACTIVE")
            
        return "\n".join(health_lines)
    except Exception as e:
        return f"Scraper health query failed: {e}"

def get_gemini_calls_count() -> int:
    """
    Estimates daily Gemini model operations (Personalize critiques + reply parsing).
    """
    if not supabase:
        return 0
    try:
        cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)).isoformat()
        # Count applications processed
        res = supabase.table("applications").select("id", count="exact").gt("created_at", cutoff).execute()
        apps_count = res.count or 0
        # 1 call per app personalize, plus check replies
        replies_res = supabase.table("applications").select("id", count="exact").eq("status", "replied").gt("created_at", cutoff).execute()
        replies_count = replies_res.count or 0
        
        return apps_count + replies_count
    except Exception:
        return 0

@retry_api_call
def send_telegram_digest(html_message: str):
    """
    Sends the digest report to Telegram.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    
    if not token or not chat_id:
        logger.warning("Telegram Bot Credentials missing. Printing report to logger instead.")
        print(html_message)
        return
        
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": html_message,
        "parse_mode": "HTML"
    }
    res = httpx.post(url, json=payload, timeout=15)
    res.raise_for_status()

def main():
    logger.info("Running daily keep-warm pings.")
    keep_warm_portfolio()
    
    logger.info("Evaluating auto-cap ramping stats.")
    new_cap, bounce_rate, triggered_breaker = run_auto_cap_ramping()
    
    logger.info("Compiling daily conversion metrics.")
    funnel = get_funnel_metrics()
    scraper_status = get_scraper_health()
    gemini_ops = get_gemini_calls_count()
    
    # 3. Assemble HTML Digest Message
    digest_msg = f"""
📈 <b>Freelancer Outreach Daily Digest</b>
Date: {datetime.date.today().strftime('%Y-%m-%d')}

📊 <b>Conversion Funnel (Last 24h):</b>
Sourced: {funnel['sourced']} | Enriched: {funnel['enriched']} | Drafted: {funnel['drafted']}
Gated (Held): {funnel['held']} | Sent: {funnel['sent']} | Replied: {funnel['replied']}

⚡ <b>Daily Send Pacing:</b>
- Current Cap: <b>{new_cap} sends/day</b>
- Trailing 20 Bounce Rate: <b>{bounce_rate:.1f}%</b>
{ "⚠️ <i>CAP CIRCUIT BREAKER TRIGGERED: Bounce rate > 5%</i>" if triggered_breaker else "" }

🔋 <b>API Quotas & Usage:</b>
- Gemini Estimated Ops: <b>{gemini_ops} calls today</b>
- Hunter.io: Max 1/day cap maintained

🛠️ <b>Scraper Health:</b>
{scraper_status}
"""
    
    logger.info("Publishing daily digest to Telegram.")
    try:
        send_telegram_digest(digest_msg)
        logger.info("Daily Digest published successfully.")
    except Exception as e:
        logger.error(f"Failed to send Telegram notification: {e}")

if __name__ == "__main__":
    main()
