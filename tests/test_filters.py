import pytest
from scripts.sourcing.github_jobs import is_candidate_submission
from scripts.enrichment import is_candidate_email_domain, is_valid_contact_email

def test_is_candidate_email_domain():
    # 1. Standard Personal domains (blocked)
    assert is_candidate_email_domain("gmail.com") is True
    assert is_candidate_email_domain("yahoo.com") is True
    assert is_candidate_email_domain("outlook.com") is True
    
    # 2. Typos (blocked via Levenshtein)
    assert is_candidate_email_domain("gmail.comac") is True
    assert is_candidate_email_domain("gamil.com") is True
    assert is_candidate_email_domain("outlok.com") is True
    
    # 3. Short personal provider exact/close check (blocked)
    assert is_candidate_email_domain("aol.com") is True
    
    # 4. University/school domains (blocked via TLD or label matching)
    assert is_candidate_email_domain("student.edu.uiz.ac.ma") is True
    assert is_candidate_email_domain("mit.edu") is True
    assert is_candidate_email_domain("university.in") is True
    assert is_candidate_email_domain("some-college.org") is True
    
    # 5. Legitimate companies (not blocked)
    assert is_candidate_email_domain("universal-tech.com") is False
    assert is_candidate_email_domain("oldschool.io") is False
    assert is_candidate_email_domain("microsoft.com") is False
    assert is_candidate_email_domain("pony.ai") is False
    assert is_candidate_email_domain("abc.com") is False  # Checks short company domains pass

def test_is_valid_contact_email():
    # 1. Enclosed/trailing punctuation checks
    assert is_valid_contact_email("'recruiter@microsoft.com'") is True
    assert is_valid_contact_email("recruiter@microsoft.com.") is True
    assert is_valid_contact_email("<recruiter@microsoft.com>") is True
    
    # 2. Candidate email checks (blocked)
    assert is_valid_contact_email("candidate@gmail.com") is False
    assert is_valid_contact_email("student@mit.edu") is False
    assert is_valid_contact_email("somebody@gmail.comac") is False
    
    # 3. Invalid/multiple @ checks
    assert is_valid_contact_email("foo@bar@company.com") is False
    assert is_valid_contact_email("invalid-email") is False

def test_is_candidate_submission():
    # 1. Title-only (Blocked)
    is_sub, reason = is_candidate_submission("My homework submission", "")
    assert is_sub is True
    assert reason == "explicit_submission_keyword"
    
    is_sub, reason = is_candidate_submission("Cybersecurity Internship Task-2", "")
    assert is_sub is True
    assert reason == "numbered_task_with_candidate_context"
    
    # 2. Title-only (Not Blocked)
    is_sub, reason = is_candidate_submission("Asmae Misbah- AI Internship Test", "")
    assert is_sub is False  # Ambiguous without body
    
    is_sub, _ = is_candidate_submission("Test-driven development at Google", "")
    assert is_sub is False
    
    is_sub, _ = is_candidate_submission("Task-based roles in AI", "")
    assert is_sub is False
    
    # 3. Title + Body (Blocked)
    # Case A: Submitter header in body
    is_sub, reason = is_candidate_submission(
        "Asmae Misbah- AI Internship Test", 
        "## 🚀 AI Internship Project Submission\nCandidate Name: Asmae Misbah"
    )
    assert is_sub is True
    assert reason == "explicit_submission_keyword"
    
    # Case B: Borderline terms combined with code/repo submission contexts
    is_sub, reason = is_candidate_submission(
        "Internship Test",
        "Here is the link to my repository with the completed task."
    )
    assert is_sub is True
    assert reason == "intern_task_with_submission_context"
    
    # 4. Title + Body (Not Blocked)
    # Case C: Legit job post mentioning "coding test" but no submission context
    is_sub, _ = is_candidate_submission(
        "Software Engineer Intern at Google",
        "We are looking for an intern. The interview process includes a coding test."
    )
    assert is_sub is False
