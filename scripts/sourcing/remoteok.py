import sys
import os
import httpx

# Add parent directory to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from scripts.db_client import (
    supabase,
    retry_api_call,
    check_source_status,
    record_sourcing_success,
    log_scraper_failure,
    get_active_domain_tag,
    logger
)

SOURCE_NAME = "remoteok"

# Tags or words to look for inside listing titles or tag fields
DOMAIN_KEYWORDS = {
    "ai_ml": ["machine-learning", "ml", "ai", "artificial-intelligence", "data-science", "deep-learning", "nlp", "llm"],
    "cybersecurity": ["security", "cybersecurity", "infosec", "penetration", "pentest", "devsecops", "application-security"],
    "robotics": ["robotics", "embedded", "ros", "ros2", "firmware", "controls", "hardware"]
}

@retry_api_call
def fetch_remoteok_jobs():
    """
    Fetches job listings from the RemoteOK public JSON API.
    """
    url = "https://remoteok.com/api"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json"
    }
    response = httpx.get(url, headers=headers, timeout=25, follow_redirects=True)
    
    if response.status_code in [403, 429]:
        raise httpx.HTTPStatusError("Access Blocked: 429/403 returned by RemoteOK API", request=response.request, response=response)
        
    response.raise_for_status()
    return response.json()

def main():
    if not check_source_status(SOURCE_NAME):
        logger.info(f"Skipping {SOURCE_NAME} sourcing run due to active circuit breaker.")
        return
        
    domain_tag = get_active_domain_tag()
    keywords = DOMAIN_KEYWORDS.get(domain_tag, [])
    logger.info(f"Starting RemoteOK Sourcing for domain: {domain_tag}")
    
    new_listings_count = 0
    try:
        raw_data = fetch_remoteok_jobs()
        
        # The first entry of RemoteOK API is usually a legal/info text object, skip it if it's not a job
        jobs = [item for item in raw_data if isinstance(item, dict) and "id" in item]
        logger.info(f"Fetched {len(jobs)} total jobs from RemoteOK. Filtering for domain: {domain_tag}")
        
        for job in jobs:
            title = job.get("position", "")
            company = job.get("company", "Unknown Company")
            source_url = job.get("url", "")
            description = job.get("description", "")
            tags = [t.lower() for t in job.get("tags", [])]
            
            # Combine title + tags to search for matches
            search_pool = (title.lower() + " " + " ".join(tags))
            
            # Ensure it is an internship by checking for "intern" or "internship"
            if "intern" not in search_pool:
                continue
            
            # Check if any keyword matches with word boundaries (avoids 'email' matching 'ai', 'html' matching 'ml', etc.)
            import re
            is_match = False
            for keyword in keywords:
                pattern = r'\b' + re.escape(keyword).replace(r'\-', r'[\-\s]') + r'\b'
                if re.search(pattern, search_pool):
                    is_match = True
                    break
            
            if is_match and title and source_url:
                if supabase:
                    try:
                        supabase.table("listings").upsert({
                            "title": title,
                            "company": company,
                            "source_url": source_url,
                            "source": SOURCE_NAME,
                            "description": description[:3000],
                            "domain_tag": domain_tag
                        }, on_conflict="source_url").execute()
                        new_listings_count += 1
                    except Exception as e:
                        logger.warning(f"Error inserting RemoteOK listing {source_url}: {e}")
                        
        logger.info(f"RemoteOK sourcing completed successfully. Sourced {new_listings_count} listings.")
        record_sourcing_success(SOURCE_NAME)
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"RemoteOK Sourcing failed: {error_msg}")
        log_scraper_failure(SOURCE_NAME, error_msg)

if __name__ == "__main__":
    main()
