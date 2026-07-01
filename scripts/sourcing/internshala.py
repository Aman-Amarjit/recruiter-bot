import sys
import os
import httpx
from bs4 import BeautifulSoup
import urllib.parse

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

SOURCE_NAME = "internshala"

DOMAIN_KEYWORDS = {
    "ai_ml": ["machine learning", "data science", "artificial intelligence"],
    "cybersecurity": ["cyber security", "information security"],
    "robotics": ["robotics", "embedded systems", "ros"]
}

@retry_api_call
def scrape_internshala_page(keyword: str):
    """
    Fetches raw HTML from Internshala search page.
    """
    encoded_keyword = urllib.parse.quote(keyword)
    url = f"https://internshala.com/internships/keywords-{encoded_keyword}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
    }
    
    response = httpx.get(url, headers=headers, timeout=20, follow_redirects=True)
    
    # Check for CAPTCHA or Cloudflare protection triggers
    if response.status_code in [403, 429] or "cloudflare" in response.text.lower() or "captcha" in response.text.lower():
        raise httpx.HTTPStatusError("Access Blocked: CAPTCHA / 429 encountered", request=response.request, response=response)
        
    response.raise_for_status()
    return response.text

def parse_internships(html: str, domain_tag: str):
    """
    Parses internship details from the search page HTML.
    """
    soup = BeautifulSoup(html, "html.parser")
    listings = []
    
    # Internshala containers have class 'individual_internship'
    cards = soup.find_all("div", class_="individual_internship")
    for card in cards:
        try:
            # Title
            title_el = card.find("h3", class_="heading_4_5 profile") or card.find("a", href=True)
            if not title_el:
                continue
            title = title_el.text.strip()
            
            # Company
            company_el = card.find("a", class_="link_display_like_text company_name") or card.find("div", class_="company_name")
            company = company_el.text.strip() if company_el else "Unknown Company"
            
            # Source URL
            link_el = card.find("a", href=True)
            if not link_el:
                continue
            href = link_el["href"]
            if not href.startswith("http"):
                source_url = "https://internshala.com" + href
            else:
                source_url = href
                
            # Details description
            meta_div = card.find("div", class_="internship_meta")
            description = meta_div.text.strip() if meta_div else "Internship listing on Internshala"
            
            listings.append({
                "title": title,
                "company": company,
                "source_url": source_url,
                "source": SOURCE_NAME,
                "description": description,
                "domain_tag": domain_tag
            })
        except Exception as e:
            logger.warning(f"Error parsing single Internshala card: {e}")
            
    return listings

def main():
    if not check_source_status(SOURCE_NAME):
        logger.info(f"Skipping {SOURCE_NAME} sourcing run due to active circuit breaker.")
        return
        
    domain_tag = get_active_domain_tag()
    keywords = DOMAIN_KEYWORDS.get(domain_tag, [])
    logger.info(f"Starting Internshala Sourcing for domain: {domain_tag}")
    
    new_listings_count = 0
    try:
        for keyword in keywords:
            logger.info(f"Scraping Internshala for keyword: {keyword}")
            html = scrape_internshala_page(keyword)
            parsed = parse_internships(html, domain_tag)
            logger.info(f"Parsed {len(parsed)} internship openings.")
            
            for item in parsed:
                if supabase:
                    try:
                        supabase.table("listings").upsert(item, on_conflict="source_url").execute()
                        new_listings_count += 1
                    except Exception as e:
                        logger.warning(f"Error inserting Internshala listing {item['source_url']}: {e}")
                        
        logger.info(f"Internshala sourcing completed successfully. Sourced {new_listings_count} listings.")
        record_sourcing_success(SOURCE_NAME)
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Internshala Sourcing failed: {error_msg}")
        log_scraper_failure(SOURCE_NAME, error_msg)

if __name__ == "__main__":
    main()
