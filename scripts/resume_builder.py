import os
import sys
import json
import uuid
import math
import logging
from jinja2 import Template
from pypdf import PdfReader

# Add parent directory to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from scripts.db_client import (
    supabase,
    retry_api_call,
    logger
)

# Optional imports for WeasyPrint (handle grace fallbacks if system packages are missing during testing)
try:
    from weasyprint import HTML
    WEASYPRINT_AVAILABLE = True
except Exception as e:
    WEASYPRINT_AVAILABLE = False
    logger.warning(f"WeasyPrint is not fully available in this local environment: {e}")

# Load profile.json as ground truth
PROFILE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "profile.json"))
with open(PROFILE_PATH, "r") as f:
    CANDIDATE_PROFILE = json.load(f)

# System prompt for LLM tailoring
RESUME_BUILDER_SYSTEM_PROMPT = """
You are an expert resume developer specialized in ATS optimization for student internship resumes.
Your job is to customize the candidate's projects and skills based on the provided job description.

CRITICAL CONSTRAINTS:
1. TRUTHFULNESS: Do NOT invent or add any projects, technologies, metrics, dates, or skills that are not present in the candidate's Profile JSON. If a technology is requested by the job description but missing from the candidate profile, do NOT include it.
2. SELECTION: Select the 3 most relevant projects from the candidate's profile.
3. BULLETS: Rephrase the description bullets of the selected projects to emphasize the technologies and methodologies requested in the job description. Start every bullet with a strong action verb. You may include the metrics from the candidate profile, but never invent any numbers or metrics.
4. FORMAT: Return ONLY a valid JSON object matching the requested schema. No markdown formatting blocks outside the JSON.
5. SUMMARY ALIGNMENT: The generated summary MUST align truthfully and proportionally with the candidate's actual background. Frame the candidate as a B.Tech Computer Science student focusing on systems, AI, or security, with practical full-stack software development contract experience. Do NOT claim the candidate is a professional 'cybersecurity specialist' or 'AI engineer'; instead, emphasize their academic research, software engineering base, and tracing/audit projects.
6. NO FABRICATED METRICS: Do NOT invent any percentage improvements, benchmark numbers, FPS values, latency reductions, or scale figures (e.g. "improved by 35%", "reduced latency by 40%", "handles 10k requests"). Only include metrics that are explicitly stated in the candidate's profile JSON. If no metric exists, describe the technique or design decision used instead — that is always credible and verifiable.

JSON Schema Output:
{
  "extracted_keywords": ["keyword1", "keyword2", ...],
  "matched_keywords": ["matching_keyword1", "matching_keyword2", ...],
  "summary": "Tailored 2-3 sentence summary emphasizing matched skills and projects.",
  "skills": "Comma-separated string of matched skills present in candidate profile.",
  "selected_projects": [
    {
      "name": "Project Name (must match profile exactly)",
      "technologies": ["Tech1", "Tech2"],
      "bullets": [
        "Rephrased bullet point 1 emphasizing keyword matches",
        "Rephrased bullet point 2 emphasizing keyword matches"
      ]
    }
  ]
}
"""

@retry_api_call
def call_llm_for_resume(job_description: str, domain_tag: str, limit_projects_count: int = 3) -> dict:
    """
    Calls Groq/Gemini to perform keyword extraction, selection, and bullet formatting in a single consolidated call.
    """
    # Fetch candidate sub-profile for active domain
    domain_profile = CANDIDATE_PROFILE["domains"].get(domain_tag, {})
    
    prompt = f"""
Candidate Name: {CANDIDATE_PROFILE['name']}
Candidate Base Profile: {json.dumps(domain_profile, indent=2)}
Education: {json.dumps(CANDIDATE_PROFILE['education'])}

Job Description:
{job_description}

Please select up to {limit_projects_count} projects and tailor the resume fields according to the instructions. Return strictly JSON.
"""

    api_key = os.getenv("GROQ_API_KEY")
    if api_key:
        try:
            from groq import Groq
            client = Groq(api_key=api_key)
            chat_completion = client.chat.completions.create(
                messages=[
                    {"role": "system", "content": RESUME_BUILDER_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}
                ],
                model="llama-3.3-70b-versatile",
                temperature=0.1,
                response_format={"type": "json_object"}
            )
            return json.loads(chat_completion.choices[0].message.content)
        except Exception as e:
            logger.warning(f"Groq resume building failed: {e}. Falling back to Gemini.")
    
    # Fallback to Gemini
    gemini_key = os.getenv("GEMINI_API_KEY")
    if gemini_key:
        import google.generativeai as genai
        genai.configure(api_key=gemini_key)
        model = genai.GenerativeModel('gemini-2.5-flash', generation_config={"response_mime_type": "application/json"})
        response = model.generate_content([RESUME_BUILDER_SYSTEM_PROMPT, prompt])
        return json.loads(response.text)

    raise ValueError("No LLM API keys configured.")

def run_ats_simulation(pdf_path: str, matched_keywords: list) -> tuple:
    """
    Parses the generated PDF file using pypdf and runs validation checks.
    Returns (is_valid, reason, match_rate)
    """
    try:
        reader = PdfReader(pdf_path)
        pages_count = len(reader.pages)
        
        # 1. Enforce 1-page budget
        if pages_count > 1:
            return False, f"Resume exceeds 1 page limit ({pages_count} pages)", 0.0
            
        text = ""
        for page in reader.pages:
            extracted = page.extract_text()
            if extracted:
                text += extracted
                
        # 2. Check for empty or broken rendering
        if len(text.strip()) < 200:
            return False, "Extracted text is too short (possible rendering failure)", 0.0
            
        # 3. Check for header identity details
        name = CANDIDATE_PROFILE["name"].lower()
        if name not in text.lower():
            return False, "Candidate name not found in parsed text", 0.0
            
        # 4. Check keyword match rate
        if not matched_keywords:
            return True, "No keywords targeted", 100.0
            
        matches_found = 0
        for kw in matched_keywords:
            if kw.lower() in text.lower():
                matches_found += 1
                
        match_rate = (matches_found / len(matched_keywords)) * 100.0
        if match_rate < 50.0:
            return False, f"Keyword match rate too low: {match_rate:.1f}% (required >= 50%)", match_rate
            
        return True, "Passed ATS simulation checks", match_rate
    except Exception as e:
        return False, f"ATS simulation crashed: {e}", 0.0

def upload_resume_to_storage(pdf_path: str) -> str:
    """
    Uploads the PDF to Supabase Storage with an obfuscated UUID path.
    Returns the public signed URL.
    """
    if not supabase:
        logger.warning("Supabase client is not available. Skipping storage upload.")
        return "https://mock-supabase-storage-url.com/resumes/mock.pdf"
        
    random_filename = f"{uuid.uuid4()}.pdf"
    storage_path = f"resumes/{random_filename}"
    
    with open(pdf_path, "rb") as f:
        file_data = f.read()
        
    # Upload binary file
    supabase.storage.from_("resumes").upload(
        path=random_filename, # Bucket config root path prefix uploads
        file=file_data,
        file_options={"content-type": "application/pdf"}
    )
    
    # Retrieve public url
    public_url = supabase.storage.from_("resumes").get_public_url(random_filename)
    return public_url

def render_resume_to_pdf(resume_data: dict, output_path: str):
    """
    Compiles the HTML template and renders it to a PDF using WeasyPrint.
    """
    template_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "templates", "resume_template.html"))
    with open(template_path, "r") as f:
        html_template = Template(f.read())
        
    projects = resume_data.get("selected_projects") or []
    for proj in projects:
        proj_name = proj.get("name")
        for domain_tag, domain in CANDIDATE_PROFILE.get("domains", {}).items():
            for p in domain.get("projects", []):
                if p.get("name") == proj_name:
                    proj["github_url"] = p.get("github_url")
                    break

    rendered_html = html_template.render(
        name=CANDIDATE_PROFILE["name"],
        city=CANDIDATE_PROFILE["city"],
        phone=CANDIDATE_PROFILE.get("phone", ""),
        location=CANDIDATE_PROFILE.get("location", ""),
        email=os.getenv("RESEND_SENDER_EMAIL", "applications@yourdomain.com"),
        links=CANDIDATE_PROFILE["links"],
        education=CANDIDATE_PROFILE["education"],
        experience=CANDIDATE_PROFILE.get("experience", []),
        certifications=CANDIDATE_PROFILE["domains"].get(domain_tag, {}).get("certifications", []),
        languages=CANDIDATE_PROFILE.get("languages", []),
        honors_awards=CANDIDATE_PROFILE.get("honors_awards", []),
        summary=resume_data.get("summary"),
        skills=resume_data.get("skills"),
        projects=projects
    )
    
    if WEASYPRINT_AVAILABLE:
        HTML(string=rendered_html).write_pdf(output_path)
    else:
        # Dry-run fallback mock file if WeasyPrint is missing locally
        logger.warning("WeasyPrint is unavailable. Writing HTML placeholder for validation bypass.")
        with open(output_path, "w") as mock_pdf:
            mock_pdf.write(f"PDF MOCK - Aman Amarjit {rendered_html}")

def build_tailored_resume(application_id: str, job_description: str, domain_tag: str) -> str:
    """
    Orchestrates the entire tailoring, rendering, and verification sequence.
    Returns the public resume URL, or None if validation fails twice.
    """
    logger.info(f"Starting resume tailoring for application: {application_id}")
    
    temp_pdf_path = f"/tmp/tailored_resume_{application_id}.pdf"
    
    # Attempt 1
    try:
        data = call_llm_for_resume(job_description, domain_tag, limit_projects_count=3)
        render_resume_to_pdf(data, temp_pdf_path)
        is_valid, reason, match_rate = run_ats_simulation(temp_pdf_path, data.get("matched_keywords", []))
        
        if is_valid:
            logger.info(f"First pass resume generation succeeded! Match rate: {match_rate:.1f}%")
            return save_resume_and_get_url(application_id, data, temp_pdf_path, match_rate)
            
        logger.warning(f"First pass validation failed: {reason}. Triggering retry fallback.")
        
        # Attempt 2 (Retry: drop lowest relevance project by requesting only 2 projects)
        data_retry = call_llm_for_resume(job_description, domain_tag, limit_projects_count=2)
        render_resume_to_pdf(data_retry, temp_pdf_path)
        is_valid_retry, reason_retry, match_rate_retry = run_ats_simulation(temp_pdf_path, data_retry.get("matched_keywords", []))
        
        if is_valid_retry:
            logger.info(f"Retry pass resume generation succeeded! Match rate: {match_rate_retry:.1f}%")
            return save_resume_and_get_url(application_id, data_retry, temp_pdf_path, match_rate_retry)
            
        # Hard Failure Exit
        logger.error(f"Resume validation failed retry pass: {reason_retry}. Marking application as resume_failed.")
        if supabase:
            supabase.table("applications").update({
                "status": "resume_failed",
                "critique_score": None
            }).eq("id", application_id).execute()
        return None
        
    except Exception as e:
        logger.error(f"Error during resume tailoring pipeline: {e}")
        if supabase:
            supabase.table("applications").update({
                "status": "resume_failed"
            }).eq("id", application_id).execute()
        return None
    finally:
        # Cleanup local temporary file
        if os.path.exists(temp_pdf_path):
            try:
                os.remove(temp_pdf_path)
            except Exception:
                pass

def save_resume_and_get_url(application_id: str, data: dict, pdf_path: str, match_rate: float) -> str:
    """
    Helper to upload file, retrieve storage URL, and write audit metadata back to the applications table.
    """
    resume_url = upload_resume_to_storage(pdf_path)
    
    if supabase:
        supabase.table("applications").update({
            "resume_url": resume_url,
            "resume_project_selection": data.get("selected_projects"),
            "extracted_keywords": data.get("extracted_keywords"),
            "matched_keywords": data.get("matched_keywords"),
            "ats_match_rate": match_rate
        }).eq("id", application_id).execute()
        
    return resume_url
