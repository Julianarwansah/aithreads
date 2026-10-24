import json
import re
import sys
import os
from typing import Dict

# Pastikan load dotenv sebelum memanggil SessionManager
from dotenv import load_dotenv
load_dotenv()

from services.session_manager import SessionManager, SessionStatus

# ==========================================
# CONFIG
# ==========================================
MEDIA_ID = "3921236769011062750"
# ==========================================

LIKE_ENDPOINT = "https://www.threads.com/graphql/query"
LIKE_DOC_ID = "24753372994365040"
LIKE_FRIENDLY_NAME = "useTHLikeMutationLikeMutation"
THREADS_HOME = "https://www.threads.com/"

BASE_HEADERS = {
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "origin": "https://www.threads.com",
    "referer": "https://www.threads.com/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "x-ig-app-id": "238260118697367",
    "x-asbd-id": "359341",
    "x-fb-friendly-name": LIKE_FRIENDLY_NAME,
}

def get_fresh_tokens(session) -> Dict[str, str]:
    tokens: Dict[str, str] = {"lsd": "", "fb_dtsg": ""}
    print(f"Membuka {THREADS_HOME} untuk extract token...")
    try:
        resp = session.get(THREADS_HOME, timeout=20)
        for pat in [r'"LSD",\[\],\{"token":"([^"]+)"\}', r'"lsd":"([^"]+)"']:
            m = re.search(pat, resp.text)
            if m: 
                tokens["lsd"] = m.group(1)
                break
        for pat in [r'"DTSGInitialData",\[\],\{"token":"([^"]+)"\}', r'"fb_dtsg":"([^"]+)"']:
            m = re.search(pat, resp.text)
            if m: 
                tokens["fb_dtsg"] = m.group(1)
                break
    except Exception as e:
        print(f"Error saat extract token: {e}")
    return tokens

def main():
    print("Memuat SessionManager...")
    sm = SessionManager()
    if not sm.load_session() or sm.validate_session().status != SessionStatus.VALID:
        print("[ERROR] Session invalid. Cek THREADS_SESSION_ID dll di .env")
        sys.exit(1)
    
    session = sm.inject_session()
    
    # Extract token dari halaman utama Threads
    tokens = get_fresh_tokens(session)
    if not tokens.get("lsd") or not tokens.get("fb_dtsg"):
        print("[ERROR] Gagal mendapatkan lsd atau fb_dtsg dari halaman Threads.")
        sys.exit(1)
        
    print(f"Token didapatkan: lsd={tokens['lsd'][:5]}... fb_dtsg={tokens['fb_dtsg'][:5]}...")

    # Set headers
    headers = BASE_HEADERS.copy()
    csrf = session.cookies.get("csrftoken")
    if csrf:
        headers["x-csrftoken"] = csrf

    # Set variables & payload
    variables = {
        "mediaID": MEDIA_ID,
        "requestData": {
            "container_module": "ig_text_feed_timeline"
        }
    }
    
    payload = {
        "lsd": tokens["lsd"],
        "fb_dtsg": tokens["fb_dtsg"],
        "doc_id": LIKE_DOC_ID,
        "variables": json.dumps(variables),
        "server_timestamps": "true",
        "fb_api_req_friendly_name": LIKE_FRIENDLY_NAME,
        "__user": "0",
        "__a": "1"
    }

    print(f"\nMengirim instruksi Like untuk MEDIA_ID: {MEDIA_ID}")
    try:
        # Menggunakan requests session.post dengan form data (otomatis set up Content-Type)
        response = session.post(LIKE_ENDPOINT, data=payload, headers=headers, timeout=20)
        
        print("\n=== HASIL RESPONSE ===")
        print(f"Status Code: {response.status_code}")
        print(f"Response Body: {response.text}")
        print("======================\n")
        
        # Validasi
        if response.status_code == 200:
            try:
                data = json.loads(response.text)
                if "errors" not in data and data.get("data") is not None:
                    print("[LIKE] Success")
                else:
                    print("[LIKE] Failed")
            except Exception:
                print("[LIKE] Failed")
        else:
            print("[LIKE] Failed")
            
    except Exception as e:
        print(f"Exception: {e}")
        print("[LIKE] Failed")

if __name__ == "__main__":
    main()
