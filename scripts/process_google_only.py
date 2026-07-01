import os
import sys
from dotenv import load_dotenv
from supabase import create_client

# Add parent directory to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

load_dotenv(dotenv_path="/home/aman-amarjit/Desktop/internship/.env")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

google_app_id = "4080f87d-de27-4ebb-ba2f-1aff699238fc"

def run():
    print("Resetting other generating applications to drafting...")
    # Fetch all generating applications
    generating_apps = supabase.table("applications").select("id").eq("status", "generating").execute().data
    for app in generating_apps:
        if app["id"] != google_app_id:
            supabase.table("applications").update({"status": "drafting"}).eq("id", app["id"]).execute()
            print(f"Reset application {app['id']} to drafting.")

    # Lock Google application to generating
    supabase.table("applications").update({"status": "generating"}).eq("id", google_app_id).execute()
    print(f"Locked Google application {google_app_id} to generating.")

    # Import llm_pipeline process_application
    from scripts.llm_pipeline import process_application
    
    # Fetch Google application details
    google_app = supabase.table("applications").select("*, listings(*), contacts(*)").eq("id", google_app_id).execute().data[0]
    
    # Process Google application
    print("Running personalization pipeline for Google application...")
    process_application(google_app)
    
    # Fetch updated details
    updated_app = supabase.table("applications").select("status, critique_score, email_body").eq("id", google_app_id).execute().data[0]
    print("\nResult:")
    print(updated_app)

if __name__ == "__main__":
    run()
