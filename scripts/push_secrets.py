import os
import sys
import base64
import httpx
from dotenv import load_dotenv
from nacl import encoding, public

# Load environment variables
load_dotenv(dotenv_path=os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env")))

# Configuration
TOKEN = os.getenv("GITHUB_TOKEN")
if not TOKEN:
    print("Error: GITHUB_TOKEN environment variable is not set. Please define it in your environment or .env file to push secrets.")
    sys.exit(1)

REPO = "Aman-Amarjit/recruiter-bot"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28"
}

# The secrets to upload
SECRET_KEYS = [
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
    "GROQ_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_CSE_API_KEY",
    "GOOGLE_CSE_ENGINE_ID",
    "SMTP_EMAIL",
    "SMTP_PASSWORD",
    "RESEND_API_KEY",
    "RESEND_SENDER_EMAIL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "PORTFOLIO_URL",
    "SEND_DISABLED"
]

def encrypt_secret(public_key: str, secret_value: str) -> str:
    """Encrypt a Unicode string using the public key with libsodium sealed box."""
    pub_key_obj = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(pub_key_obj)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    return encoding.Base64Encoder().encode(encrypted).decode("utf-8")

def run():
    print(f"Retrieving public key for GitHub repository: {REPO}...")
    url = f"https://api.github.com/repos/{REPO}/actions/secrets/public-key"
    
    resp = httpx.get(url, headers=HEADERS)
    if resp.status_code != 200:
        print(f"Failed to fetch public key: {resp.status_code} {resp.text}")
        sys.exit(1)
        
    pk_data = resp.json()
    key_id = pk_data["key_id"]
    public_key = pk_data["key"]
    print(f"Successfully retrieved public key (ID: {key_id}).")
    
    print("\nStarting secrets upload...")
    for key in SECRET_KEYS:
        val = os.getenv(key)
        if val is None:
            print(f"⚠️ Warning: {key} is not set in your local .env, skipping.")
            continue
            
        print(f"Encrypting and pushing secret {key}...")
        encrypted_val = encrypt_secret(public_key, val)
        
        secret_url = f"https://api.github.com/repos/{REPO}/actions/secrets/{key}"
        payload = {
            "encrypted_value": encrypted_val,
            "key_id": key_id
        }
        
        put_resp = httpx.put(secret_url, headers=HEADERS, json=payload)
        if put_resp.status_code in [201, 204]:
            status = "CREATED" if put_resp.status_code == 201 else "UPDATED"
            print(f"✅ Secret {key} successfully {status}.")
        else:
            print(f"❌ Failed to push secret {key}: {put_resp.status_code} {put_resp.text}")

    print("\nFinished pushing all secrets to GitHub Actions!")

if __name__ == "__main__":
    run()
