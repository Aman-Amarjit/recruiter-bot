import sys
import os
import httpx
import logging

# Add the parent directory to sys.path so we can import db_client
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

SOURCE_NAME = "github"

# Define query keywords per domain
DOMAIN_KEYWORDS = {
    "ai_ml": ["machine learning internship", "ai internship", "computer vision internship"],
    "cybersecurity": ["cybersecurity internship", "security analyst internship", "penetration testing"],
    "robotics": ["robotics internship", "ros2 internship", "control systems internship"]
}

@retry_api_call
def fetch_github_issues(query_str: str):
    """
    Fetches open issues containing keywords from GitHub API.
    """
    url = f"https://api.github.com/search/issues?q={query_str}+state:open+type:issue"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Freelancer-AutoApply-System"
    }
    
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
        
    response = httpx.get(url, headers=headers, timeout=15)
    
    if response.status_code == 429:
        raise httpx.HTTPStatusError("Rate limited by GitHub API", request=response.request, response=response)
    response.raise_for_status()
    return response.json()

def main():
    if not check_source_status(SOURCE_NAME):
        logger.info(f"Skipping {SOURCE_NAME} sourcing run due to active circuit breaker.")
        return

    domain_tag = get_active_domain_tag()
    keywords = DOMAIN_KEYWORDS.get(domain_tag, [])
    logger.info(f"Starting GitHub Jobs Sourcing for domain: {domain_tag}")

    new_listings_count = 0
    try:
        for keyword in keywords:
            # Format query for search API
            query_str = f'"{keyword}"'
            logger.info(f"Searching GitHub issues for: {keyword}")
            data = fetch_github_issues(query_str)
            
            items = data.get("items", [])
            logger.info(f"Found {len(items)} issues matching search.")

            for item in items:
                title = item.get("title")
                source_url = item.get("html_url")
                body = item.get("body") or ""
                # Guess company name from issue title or repo name
                # E.g. "[Company] Software Engineer Intern" or repo owner
                repo_url = item.get("repository_url", "")
                company = "GitHub Community"
                if "/repos/" in repo_url:
                    parts = repo_url.split("/repos/")
                    if len(parts) > 1:
                        company = parts[1].split("/")[0].capitalize()

                # Clean body description text (truncate if too long)
                description = body[:3000] if body else ""

                if not title or not source_url:
                    continue

                # Insert/Upsert into listings
                if supabase:
                    try:
                        supabase.table("listings").upsert({
                            "title": title,
                            "company": company,
                            "source_url": source_url,
                            "source": SOURCE_NAME,
                            "description": description,
                            "domain_tag": domain_tag
                        }, on_conflict="source_url").execute()
                        new_listings_count += 1
                    except Exception as e:
                        # Log error but don't fail the whole loop
                        logger.warning(f"Error inserting listing {source_url}: {e}")

        logger.info(f"GitHub sourcing completed successfully. Sourced {new_listings_count} listings.")
        record_sourcing_success(SOURCE_NAME)

    except Exception as e:
        error_msg = str(e)
        logger.error(f"GitHub Sourcing failed: {error_msg}")
        log_scraper_failure(SOURCE_NAME, error_msg)

if __name__ == "__main__":
    main()
