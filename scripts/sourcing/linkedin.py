import sys
import os
import httpx
import re

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

SOURCE_NAME = "linkedin"

# Search queries per domain for LinkedIn jobs
DOMAIN_QUERIES = {
    "ai_ml": 'site:linkedin.com/jobs/view/ ("machine learning intern" OR "ai intern" OR "data science intern")',
    "cybersecurity": 'site:linkedin.com/jobs/view/ ("cybersecurity intern" OR "security intern" OR "information security intern")',
    "robotics": 'site:linkedin.com/jobs/view/ ("robotics intern" OR "ros intern" OR "embedded firmware intern")'
}

@retry_api_call
def query_google_cse(query: str):
    """
    Queries Google Custom Search API.
    """
    api_key = os.getenv("GOOGLE_CSE_API_KEY")
    cx = os.getenv("GOOGLE_CSE_ENGINE_ID")
    if not api_key or not cx:
        raise ValueError("Google CSE API credentials missing.")
        
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": api_key,
        "cx": cx,
        "q": query,
        "num": 10  # Max results per request
    }
    response = httpx.get(url, params=params, timeout=20)
    response.raise_for_status()
    return response.json()

@retry_api_call
def query_serpapi_fallback(query: str):
    """
    SerpAPI fallback for Google Search query.
    """
    api_key = os.getenv("SERPAPI_API_KEY")
    if not api_key:
        raise ValueError("SerpAPI key is missing.")
        
    url = "https://serpapi.com/search.json"
    params = {
        "engine": "google",
        "q": query,
        "api_key": api_key,
        "num": 10
    }
    response = httpx.get(url, params=params, timeout=20)
    response.raise_for_status()
    return response.json()

@retry_api_call
def fetch_linkedin_job_via_jina(job_url: str) -> str:
    """
    Fetches a LinkedIn job description via LinkedIn's unofficial guest API endpoint,
    then uses Jina AI Reader to extract clean text from the HTML response.
    - Guest API (/jobs-guest/...) returns actual job HTML without requiring login.
    - Jina strips the HTML and returns clean readable text.
    - Completely free, zero API keys needed.
    """
    job_id = extract_job_id_from_url(job_url)
    if not job_id:
        return ""

    # LinkedIn guest API returns the real job posting HTML without auth
    guest_url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
    jina_url = f"https://r.jina.ai/{guest_url}"
    headers = {
        "Accept": "text/plain",
        "X-Return-Format": "text"
    }
    response = httpx.get(jina_url, headers=headers, timeout=30, follow_redirects=True)
    response.raise_for_status()
    text = response.text.strip()

    # Detect login wall or expired job page — fall back to CSE snippet
    blocked_signals = ["sign in to", "join now", "no longer accepting applications", "sign in with email"]
    if any(signal in text.lower() for signal in blocked_signals):
        return ""

    return text[:3000] if len(text) > 200 else ""

def extract_job_id_from_url(job_url: str) -> str:
    """
    Extracts the numeric job ID from a LinkedIn job URL.
    e.g. https://linkedin.com/jobs/view/1234567890 -> '1234567890'
    """
    match = re.search(r'/jobs/view/(\d+)', job_url)
    return match.group(1) if match else ""


def parse_title_and_company(raw_title: str) -> tuple:
    """
    Robustly extracts job title and company from a Google CSE/SerpAPI result title.
    Handles formats like:
      'Machine Learning Intern at Nvidia | LinkedIn'
      'AI Research Intern - Scale AI | LinkedIn Jobs'
    """
    # Strip source suffix ('| LinkedIn', '- LinkedIn', etc. case-insensitively)
    clean = re.split(r'\s*[\-|]\s*linkedin', raw_title, flags=re.IGNORECASE)[0].strip()

    # Try ' at ' split (most common LinkedIn format)
    if ' at ' in clean:
        parts = clean.split(' at ', 1)
        return parts[0].strip(), parts[1].strip()

    # Try ' - ' split as fallback (e.g. 'AI Intern - Scale AI')
    if ' - ' in clean:
        parts = clean.split(' - ', 1)
        return parts[0].strip(), parts[1].strip()

    return clean, "Unknown Company"

def main():
    if not check_source_status(SOURCE_NAME):
        logger.info(f"Skipping {SOURCE_NAME} sourcing run due to active circuit breaker.")
        return
        
    domain_tag = get_active_domain_tag()
    query = DOMAIN_QUERIES.get(domain_tag, "")
    logger.info(f"Starting LinkedIn CSE Sourcing for domain: {domain_tag}")
    
    new_listings_count = 0
    links = []
    
    # 1. Search via Google Custom Search Engine
    try:
        logger.info(f"Executing Google CSE Search: {query}")
        results = query_google_cse(query)
        items = results.get("items", [])
        for item in items:
            link = item.get("link", "")
            title = item.get("title", "")
            snippet = item.get("snippet", "")
            links.append((link, title, snippet))
        logger.info(f"CSE returned {len(links)} results.")
    except Exception as e:
        logger.warning(f"Google CSE search failed, trying SerpAPI fallback: {e}")
        # 2. Try SerpAPI Fallback
        try:
            results = query_serpapi_fallback(query)
            items = results.get("organic_results", [])
            for item in items:
                link = item.get("link", "")
                title = item.get("title", "")
                snippet = item.get("snippet", "")
                links.append((link, title, snippet))
            logger.info(f"SerpAPI returned {len(items)} results.")
        except Exception as err:
            logger.error(f"Search fallbacks exhausted: {err}")
            log_scraper_failure(SOURCE_NAME, f"Search failure: {err}")
            return

    # 3. Process search links
    successful_scrapes = 0
    failed_scrapes = 0
    
    for link, raw_title, snippet in links:
        # Ensure we only parse actual job view pages
        if "/jobs/view/" not in link:
            continue
            
        # Parse company and clean title robustly
        title, company = parse_title_and_company(raw_title)
            
        logger.info(f"Processing listing: {title} at {company}")
        
        description = snippet  # default: CSE snippet (~150 chars)
        try:
            # Fetch full description via Jina Reader (free, no API key, bypasses IP blocks)
            logger.info(f"Fetching full description via Jina Reader for: {link}")
            full_desc = fetch_linkedin_job_via_jina(link)
            if full_desc:
                description = full_desc
                successful_scrapes += 1
                logger.info(f"Jina fetch succeeded ({len(full_desc)} chars).")
            else:
                logger.info("Jina returned no usable content. Falling back to CSE snippet.")
                successful_scrapes += 1  # title + company + snippet is still usable
        except Exception as e:
            logger.warning(f"Jina fetch failed for {link}: {e}")
            failed_scrapes += 1
            log_scraper_failure(SOURCE_NAME, f"Jina fetch error: {e}")
                
        # Insert/Upsert listing
        if supabase:
            try:
                supabase.table("listings").upsert({
                    "title": title.strip(),
                    "company": company.strip(),
                    "source_url": link,
                    "source": SOURCE_NAME,
                    "description": description[:3000],
                    "domain_tag": domain_tag
                }, on_conflict="source_url").execute()
                new_listings_count += 1
            except Exception as e:
                logger.warning(f"Error inserting LinkedIn listing {link}: {e}")

    logger.info(f"LinkedIn sourcing completed. Scraped {new_listings_count} listings. Success: {successful_scrapes}, Failed: {failed_scrapes}")
    if successful_scrapes > 0:
        record_sourcing_success(SOURCE_NAME)

if __name__ == "__main__":
    main()
