import os
import glob
from typing import List

# Path absolut menuju folder agency-agents-id-main
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AGENTS_DIR = os.path.join(BASE_DIR, 'agency-agents-id')

_agents_dict = {}
_is_scanned = False

def _scan_agents():
    global _is_scanned
    if _is_scanned:
        return
        
    if not os.path.exists(AGENTS_DIR):
        print(f"[AgentLoader] Folder {AGENTS_DIR} tidak ditemukan.")
        return

    # Scan seluruh file .md secara rekursif
    search_pattern = os.path.join(AGENTS_DIR, '**', '*.md')
    md_files = glob.glob(search_pattern, recursive=True)
    
    for filepath in md_files:
        filename = os.path.basename(filepath)
        name_without_ext, _ = os.path.splitext(filename)
        
        # Simpan pemetaan nama file
        _agents_dict[filename] = filepath
        _agents_dict[name_without_ext] = filepath
        
    _is_scanned = True

def load_agent(name: str) -> str:
    _scan_agents()
    filepath = _agents_dict.get(name)
    
    # Pencarian fleksibel (fallback) jika nama tidak persis sama
    if not filepath:
        name_lower = name.lower().replace(" ", "-")
        for key, path in _agents_dict.items():
            if name_lower in key.lower():
                filepath = path
                break
                
    if not filepath:
        return ""
        
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except Exception as e:
        print(f"[AgentLoader] Gagal membaca agent '{name}': {e}")
        return ""

def compose_agents(agent_names: List[str]) -> str:
    composed_prompt = []
    for name in agent_names:
        content = load_agent(name)
        if content:
            composed_prompt.append(f"=== PERSONA: {name.upper()} ===\n{content}")
        else:
            print(f"[AgentLoader] Peringatan: Agent '{name}' tidak ditemukan.")
            
    # Gabungkan seluruh markdown dengan jarak newline ganda
    return "\n\n".join(composed_prompt)
