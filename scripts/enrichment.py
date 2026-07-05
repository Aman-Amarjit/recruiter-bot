import sys
import os
import re
import httpx
import socket
from datetime import datetime, timezone

# Set default timeout for all socket connections (including DNS lookups) to prevent hangs
socket.setdefaulttimeout(3.0)

# Add parent directory to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from scripts.db_client import (
    supabase,
    retry_api_call,
    logger
)

# Email matching regex
EMAIL_REGEX = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'

def clean_company_name(name: str) -> str:
    """
    Cleans company names from junk suffixes.
    """
    cleaned = re.sub(r'\b(inc|llc|corp|co|ltd|gmbh|corporation|incorporated|limited)\.?\b', '', name.lower())
    cleaned = re.sub(r'[^\w\s]', '', cleaned)  # strip leftover punctuation (e.g. trailing '.')
    return cleaned.strip()

def get_company_domain(company_name: str) -> str:
    """
    Heuristically guesses or searches for the company website domain.
    """
    cleaned = clean_company_name(company_name)
    domain_guess = cleaned.replace(" ", "").replace("-", "") + ".com"
    
    # Try searching Google CSE first if API key is available
    api_key = os.getenv("GOOGLE_CSE_API_KEY")
    cx = os.getenv("GOOGLE_CSE_ENGINE_ID")
    if api_key and cx:
        try:
            url = "https://www.googleapis.com/customsearch/v1"
            params = {
                "key": api_key,
                "cx": cx,
                "q": f"{company_name} official website",
                "num": 3
            }
            response = httpx.get(url, params=params, timeout=10)
            if response.status_code == 200:
                items = response.json().get("items", [])
                for item in items:
                    link = item.get("link", "")
                    # Parse domain from URL
                    match = re.search(r'https?://([^/]+)', link)
                    if match:
                        domain = match.group(1).replace("www.", "")
                        try:
                            socket.gethostbyname(domain)
                            return domain
                        except socket.gaierror:
                            continue
        except Exception as e:
            logger.warning(f"Google CSE domain search failed for {company_name}: {e}")
            
    # Fallback to heuristic guess
    try:
        socket.gethostbyname(domain_guess)
        return domain_guess
    except socket.gaierror:
        pass
        
    return domain_guess

def is_valid_contact_email(email: str) -> bool:
    """
    Validates if an email is not in the blacklist of generic dead-end non-hiring addresses.
    """
    if not email or "@" not in email:
        return False
        
    email_lower = email.lower()
    if email_lower.endswith((".png", ".jpg", ".gif", "example.com", "wixpress.com")):
        return False
        
    prefix = email_lower.split("@")[0]
    blacklist = ["noreply", "no-reply", "donotreply", "support", "billing", "privacy", "dpo", "abuse", "legal", "security", "help", "careers", "jobs", "hr", "info", "contact", "recruitment"]
    if any(item == prefix or prefix.startswith(item + "-") or prefix.startswith(item + ".") for item in blacklist):
        return False
        
    return True

@retry_api_call
def query_google_cse_for_emails(company_name: str, domain: str) -> list:
    """
    Searches Google CSE for public email addresses listed on company pages.
    """
    api_key = os.getenv("GOOGLE_CSE_API_KEY")
    cx = os.getenv("GOOGLE_CSE_ENGINE_ID")
    if not api_key or not cx:
        return []
        
    query = f'site:{domain} "email" OR "careers" OR "contact" "@"'
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": api_key,
        "cx": cx,
        "q": query,
        "num": 5
    }
    
    response = httpx.get(url, params=params, timeout=15)
    response.raise_for_status()
    
    emails = []
    items = response.json().get("items", [])
    for item in items:
        snippet = item.get("snippet", "")
        html_snippet = item.get("htmlSnippet", "")
        # Search for email address matches inside text snippets
        matches = re.findall(EMAIL_REGEX, snippet + " " + html_snippet)
        for email in matches:
            if is_valid_contact_email(email):
                emails.append(email.lower())
            
    return list(set(emails))

@retry_api_call
def query_hunter_api(company_name: str, domain: str) -> tuple:
    """
    Queries Hunter.io API for the company domain.
    Returns (email, confidence_score, name) or (None, 0.0, None)
    """
    api_key = os.getenv("HUNTER_API_KEY")
    if not api_key:
        return None, 0.0, None
        
    url = f"https://api.hunter.io/v2/domain-search?domain={domain}&api_key={api_key}"
    response = httpx.get(url, timeout=15)
    response.raise_for_status()
    
    data = response.json().get("data", {})
    emails = data.get("emails", [])
    if emails:
        # Get the first verified email or the one with the highest confidence
        best_email = None
        best_confidence = 0.0
        best_name = None
        
        for item in emails:
            email_val = item.get("value")
            if not is_valid_contact_email(email_val):
                continue
            confidence = float(item.get("confidence", 0)) / 100.0
            first_name = item.get("first_name")
            last_name = item.get("last_name")
            name_val = f"{first_name} {last_name}".strip() if (first_name or last_name) else None
            
            # SMTP verified check
            verification = item.get("verification", {})
            status = verification.get("status")
            
            if status == "deliverable":
                return email_val, 0.8, name_val
            if confidence > best_confidence:
                best_email = email_val
                best_confidence = confidence
                best_name = name_val
                
        if best_email:
            # Map Hunter.io confidence to our standard (0.8 max)
            return best_email, min(0.8, best_confidence), best_name
            
    return None, 0.0, None

def verify_email_domain_has_mx(email: str) -> bool:
    """
    Quick verification that the email domain exists and is set up to receive mail.
    """
    try:
        domain = email.split("@")[1]
        import subprocess
        # Run host -t mx command to verify MX record presence
        result = subprocess.run(["host", "-t", "mx", domain], capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and "mail is handled by" in result.stdout:
            return True
        return False
    except Exception:
        return False

def enrich_listing(listing):
    """
    Processes a single listing, finds or creates a contact, and links them.
    """
    listing_id = listing["id"]
    company = listing["company"]
    
    logger.info(f"Enriching listing: {listing['title']} at {company}")
    
    # 1. Try finding if we already have an active contact for this company
    existing_contact = supabase.table("contacts").select("*").eq("company", company).eq("suppressed", False).execute()
    if existing_contact.data:
        contact = existing_contact.data[0]
        # Link contact with application
        link_listing_to_contact(listing_id, contact)
        return
        
    # 2. Extract company domain
    domain = get_company_domain(company)
    logger.info(f"Domain for {company} parsed as: {domain}")
    
    email = None
    confidence = 0.0
    name = None
    
    # 3. Try scraping search engine snippets for emails (Confidence = 1.0)
    try:
        found_emails = query_google_cse_for_emails(company, domain)
        # Filter for domains matching the company
        company_emails = [e for e in found_emails if e.endswith(f"@{domain}")]
        if company_emails:
            email = company_emails[0]
            confidence = 1.0
            logger.info(f"Email found via CSE: {email} (Confidence: {confidence})")
    except Exception as e:
        logger.warning(f"Google CSE email extraction failed: {e}")
        
    # 4. Try Hunter.io API (Confidence = 0.8 max, limit to 1 call per daily pipeline run)
    if not email and os.getenv("HUNTER_API_KEY"):
        try:
            email, confidence, name = query_hunter_api(company, domain)
            if email:
                logger.info(f"Email found via Hunter.io: {email} (Confidence: {confidence}, Name: {name})")
        except Exception as e:
            logger.warning(f"Hunter.io API domain search failed: {e}")
            
    # 5. Pattern-guess generic careers email (Confidence = 0.8 as it targets official recruiting mailboxes)
    if not email:
        generic_candidates = [
            f"careers@{domain}",
            f"jobs@{domain}",
            f"recruiting@{domain}",
            f"hr@{domain}"
        ]
        for candidate in generic_candidates:
            if verify_email_domain_has_mx(candidate):
                email = candidate
                confidence = 0.8
                logger.info(f"Email generated via generic pattern: {email} (Confidence: {confidence})")
                break
                
    # 6. Record findings
    if email and confidence >= 0.7:
        try:
            # Insert new contact into DB
            contact_res = supabase.table("contacts").upsert({
                "company": company,
                "email": email,
                "name": name,
                "source": listing["source"],
                "confidence": confidence,
                "status": "completed",
                "suppressed": False
            }, on_conflict="email").execute()
            
            if contact_res.data:
                link_listing_to_contact(listing_id, contact_res.data[0])
        except Exception as e:
            logger.error(f"Error inserting contact {email} for {company}: {e}")
    else:
        logger.warning(f"No valid contact found with confidence >= 0.7 for company: {company}")

def link_listing_to_contact(listing_id: str, contact: dict):
    """
    Links a listing to a contact by inserting a row in the applications table.
    Uses status = 'drafting' to register it for the personalization pipeline.
    """
    try:
        supabase.table("applications").upsert({
            "listing_id": listing_id,
            "contact_id": contact["id"],
            "status": "drafting",
            "email_body": "",
            "critique_score": None
        }, on_conflict="listing_id,contact_id").execute()
        logger.info(f"Created application link: Listing {listing_id} -> Contact {contact['email']}")
    except Exception as e:
        logger.warning(f"Error creating application link: {e}")

def main():
    if not supabase:
        logger.error("Supabase client is not initialized.")
        return
        
    logger.info("Starting Contact Enrichment phase.")
    
    # Find all listings that do not have any applications link (newly sourced)
    # Perform a left join or filter
    try:
        listings_res = supabase.table("listings").select("*").execute()
        if not listings_res.data:
            logger.info("No listings found in database.")
            return
            
        apps_res = supabase.table("applications").select("listing_id").execute()
        linked_listings = {app["listing_id"] for app in apps_res.data}
        
        unlinked_listings = [
            listing for listing in listings_res.data
            if listing["id"] not in linked_listings
        ]
        
        logger.info(f"Found {len(unlinked_listings)} unlinked listings requiring enrichment.")
        
        for listing in unlinked_listings:
            enrich_listing(listing)
            
        logger.info("Contact Enrichment phase completed successfully.")
        
    except Exception as e:
        logger.error(f"Enrichment pipeline crashed: {e}")

if __name__ == "__main__":
    main()
