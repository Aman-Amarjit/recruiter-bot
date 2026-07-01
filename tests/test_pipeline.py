import sys
import os
import pytest
from datetime import datetime

# Add parent directory to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.db_client import get_active_domain_tag
from scripts.enrichment import clean_company_name, verify_email_domain_has_mx
from scripts.resume_builder import run_ats_simulation

def test_domain_rotation():
    """
    Verifies that domain rotation schedule maps correctly to allowed categories.
    """
    active_tag = get_active_domain_tag()
    assert active_tag in ["ai_ml", "cybersecurity", "robotics"]

def test_clean_company_name():
    """
    Verifies suffix removal from company names.
    """
    assert clean_company_name("Google Inc") == "google"
    assert clean_company_name("Acme Corp.") == "acme"
    assert clean_company_name("CyberSec LLC") == "cybersec"
    assert clean_company_name("RoboTech Ltd.") == "robotech"

def test_verify_email_domain():
    """
    Tests email domain MX verification helper.
    """
    # Real domains should return True
    assert verify_email_domain_has_mx("test@gmail.com") is True
    # Fake domain should return False
    assert verify_email_domain_has_mx("test@nonexistentdomainname123456.xyz") is False

def test_ats_simulation_validation(tmp_path):
    """
    Tests ATS simulation verification checks.
    """
    # Create a mock text file and rename it to .pdf to trigger validation checks
    # (Checking that pypdf handles it, or test mock logic directly)
    mock_pdf = tmp_path / "test.pdf"
    
    # We will test the validation rules logic by mocking PdfReader if necessary,
    # or verifying basic flow checks.
    # If run_ats_simulation is called with an invalid file, it should return False
    is_valid, reason, rate = run_ats_simulation(str(mock_pdf), ["python"])
    assert is_valid is False
    assert "crashed" in reason or "broken" in reason
