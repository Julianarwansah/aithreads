import sys
from dotenv import load_dotenv

# Pastikan load dotenv di awal
load_dotenv()

from services.session_manager import SessionManager, SessionStatus

# Import langsung fungsionalitas fetch dari commenter yang sudah ada
from auto_commenter import (
    load_latest_query_config,
    get_fresh_tokens,
    fetch_timeline,
    extract_posts
)

def main():
    print("Memuat SessionManager...")
    sm = SessionManager()
    if not sm.load_session() or sm.validate_session().status != SessionStatus.VALID:
        print("[ERROR] Session invalid. Cek THREADS_SESSION_ID dll di .env")
        sys.exit(1)
    
    session = sm.inject_session()

    print("Mengambil config query timeline...")
    try:
        query_config = load_latest_query_config()
        
        # --- FIX REFRESH TIMELINE (RECURSIVE CLEANER) ---
        import json
        import random
        import string
        import uuid
        
        vars_dict = json.loads(query_config['variables'])
        is_modified = False
        
        def clean_variables(d):
            nonlocal is_modified
            if isinstance(d, dict):
                # Kunci yang harus dihapus sepenuhnya
                keys_to_delete = ['cursor', 'max_id', 'paging_token', 'after', 'before', 'client_cache_key']
                for k in keys_to_delete:
                    if k in d:
                        del d[k]
                        is_modified = True
                        
                # Kunci yang harus dirandomize (jika ada)
                if 'client_session_id' in d:
                    d['client_session_id'] = str(uuid.uuid4())
                    is_modified = True
                if 'client_fetch_id' in d:
                    d['client_fetch_id'] = str(uuid.uuid4())
                    is_modified = True
                    
                # Rekursi ke anak
                for k, v in list(d.items()):
                    clean_variables(v)
            elif isinstance(d, list):
                for item in d:
                    clean_variables(item)
                    
        clean_variables(vars_dict)
                
        if is_modified:
            query_config['variables'] = json.dumps(vars_dict)
            print(" -> [INFO] Cache/Cursor keys berhasil di-wipe secara rekursif!")
            
        print(f" -> doc_id: {query_config.get('doc_id')}")
        print(f" -> Variables (Sent): {query_config['variables']}")
        # ----------------------------
    except Exception as e:
        print(f"[ERROR] Gagal load query config: {e}")
        sys.exit(1)

    print("Mengambil fresh tokens (lsd & fb_dtsg)...")
    tokens = get_fresh_tokens(session)
    print(f" -> lsd: {tokens.get('lsd')} | fb_dtsg: {tokens.get('fb_dtsg', '')[:10]}...")

    print("Request ke GraphQL Timeline Endpoint...")
    raw = fetch_timeline(session, tokens, query_config)
    
    if not raw:
        print("[ERROR] Gagal mendapatkan respons dari endpoint timeline (atau respons bukan 200 OK).")
        sys.exit(1)
        
    posts = extract_posts(raw)
    if not posts:
        print("[WARN] Berhasil fetch tapi tidak ada post yang ditemukan (timeline kosong/struktur berubah).")
        # Jika Anda butuh melihat respons raw saat kosong, uncomment line ini:
        # print("Raw response:", raw)
        sys.exit(0)
        
    print(f"\nDitemukan {len(posts)} posts secara total. Menampilkan 10 post pertama:\n")
    print("=" * 60)
    for i, p in enumerate(posts[:10], 1):
        username = p.get("username", "(unknown)")
        post_id = p.get("post_id", "?")
        caption = p.get("caption", "")
        
        # Potong caption jika terlalu panjang untuk debugging
        short_caption = caption[:100].replace("\n", " ")
        if len(caption) > 100:
            short_caption += "..."
            
        print(f"[{i}] Media ID: {post_id}")
        print(f"    Username: @{username}")
        print(f"    Caption : {short_caption}")
        print("-" * 60)

if __name__ == "__main__":
    main()
