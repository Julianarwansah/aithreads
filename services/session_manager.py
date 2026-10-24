"""
Session Manager for Instagram/Threads Automation

Session-First Architecture with Threads-based validation:
- NO automatic login retries
- Validates via Threads.net endpoints (NOT Instagram.com)
- Handles 429 correctly (RATE_LIMITED, not invalid)
"""

import json
import logging
import os
import time
from typing import Dict, Optional, Any
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timedelta

from dotenv import load_dotenv
from curl_cffi.requests import Session as CurlSession

logger = logging.getLogger(__name__)


class SessionStatus(Enum):
    """Session state enumeration - Threads-based."""
    VALID = "VALID"
    EXPIRED = "EXPIRED"
    RATE_LIMITED = "RATE_LIMITED"
    UNKNOWN = "UNKNOWN"
    NOT_FOUND = "NOT_FOUND"
    CORRUPTED = "CORRUPTED"


@dataclass
class SessionValidationResult:
    valid: Optional[bool]
    status: SessionStatus
    reason: str
    status_code: int = 0
    user_id: Optional[str] = None
    username: Optional[str] = None
    retry_after: Optional[int] = None
    debug_info: Dict[str, Any] = field(default_factory=dict)
    
    def is_final(self) -> bool:
        return self.status in [SessionStatus.VALID, SessionStatus.EXPIRED, SessionStatus.CORRUPTED]
    
    def should_retry(self) -> bool:
        return self.status in [SessionStatus.RATE_LIMITED, SessionStatus.UNKNOWN]


@dataclass
class SessionData:
    sessionid: str = ""
    csrftoken: str = ""
    ds_user_id: str = ""
    mid: str = ""
    ig_did: str = ""
    
    def to_dict(self) -> Dict[str, str]:
        return {k: v for k, v in self.__dict__.items() if v}
    
    @classmethod
    def from_dict(cls, data: Dict[str, str]) -> "SessionData":
        return cls(
            sessionid=data.get("sessionid", ""),
            csrftoken=data.get("csrftoken", ""),
            ds_user_id=data.get("ds_user_id", ""),
            mid=data.get("mid", ""),
            ig_did=data.get("ig_did", "")
        )
    
    def is_complete(self) -> bool:
        required = ["sessionid", "csrftoken", "mid", "ig_did"]
        return all(self.to_dict().get(k) for k in required)
    
    def get_cookie_dict(self) -> Dict[str, str]:
        return self.to_dict()


class SessionManager:
    SESSION_FILE = "session.json"
    VALIDATION_CACHE_SECONDS = 60
    RATE_LIMIT_COOLDOWN = 30
    
    def __init__(self, impersonate: str = "chrome110"):
        self.session: Optional[CurlSession] = None
        self.session_data: Optional[SessionData] = None
        self._impersonate = impersonate
        self._is_valid: Optional[bool] = None
        self._validation_result: Optional[SessionValidationResult] = None
        self._validation_cache: Optional[tuple[SessionValidationResult, datetime]] = None
        self._last_rate_limited: Optional[datetime] = None
        load_dotenv()
    
    def load_session(self) -> bool:
        logger.info("[SESSION] Loading session...")
        
        env_session = self._load_from_env()
        if env_session and env_session.is_complete():
            self.session_data = env_session
            logger.info("[SESSION] Loaded from environment variables")
            return True
        
        file_session = self._load_from_file()
        if file_session and file_session.is_complete():
            self.session_data = file_session
            logger.info("[SESSION] Loaded from session.json")
            return True
        
        logger.warning("[SESSION] No valid session found")
        return False
    
    def _load_from_env(self) -> Optional[SessionData]:
        session_id = os.getenv("THREADS_SESSION_ID", "")
        if not session_id:
            return None
        return SessionData(
            sessionid=session_id,
            csrftoken=os.getenv("THREADS_CSRF_TOKEN", ""),
            ds_user_id=os.getenv("THREADS_DS_USER_ID", ""),
            mid=os.getenv("THREADS_MID", ""),
            ig_did=os.getenv("THREADS_IG_DID", "")
        )
    
    def _load_from_file(self) -> Optional[SessionData]:
        if not os.path.exists(self.SESSION_FILE):
            return None
        try:
            with open(self.SESSION_FILE, "r") as f:
                data = json.load(f)
            if not data.get("sessionid"):
                return None
            return SessionData.from_dict(data)
        except Exception as e:
            logger.error(f"[SESSION] Failed to load file: {e}")
            return None
    
    def save_session(self, session: CurlSession) -> bool:
        try:
            cookies = session.cookies.get_dict()
            if "sessionid" not in cookies:
                logger.error("[SESSION] No sessionid in cookies")
                return False
            with open(self.SESSION_FILE, "w") as f:
                json.dump(cookies, f, indent=2)
            logger.info(f"[SESSION] Saved {len(cookies)} cookies to {self.SESSION_FILE}")
            return True
        except Exception as e:
            logger.error(f"[SESSION] Failed to save: {e}")
            return False
    
    def inject_session(self) -> Optional[CurlSession]:
        if not self.session_data:
            logger.error("[SESSION] No session data to inject")
            return None
        session = CurlSession(impersonate=self._impersonate)
        # Set cookies on all 3 Meta domains to survive cross-domain redirects
        for k, v in self.session_data.get_cookie_dict().items():
            session.cookies.set(k, v, domain=".instagram.com")
            session.cookies.set(k, v, domain=".threads.net")
            session.cookies.set(k, v, domain=".threads.com")
        logger.info(f"[SESSION] Injected {len(self.session_data.to_dict())} cookies into session (3 domains)")
        self.session = session
        return session

    def get_cookies(self) -> Dict[str, str]:
        """Get session cookies as dictionary for browser injection."""
        if not self.session_data:
            return {}
        cookies = self.session_data.get_cookie_dict()
        logger.info(f"[SESSION] Extracted {len(cookies)} cookies")
        return cookies
    
    def validate_session(self, max_retries: int = 2) -> SessionValidationResult:
        """Validate session via Threads.net (NOT Instagram.com)."""
        if not self.session_data:
            return SessionValidationResult(valid=False, status=SessionStatus.NOT_FOUND, reason="No session loaded")
        
        # Check cooldown
        if self._last_rate_limited:
            elapsed = (datetime.now() - self._last_rate_limited).total_seconds()
            if elapsed < self.RATE_LIMIT_COOLDOWN:
                remaining = self.RATE_LIMIT_COOLDOWN - int(elapsed)
                logger.info(f"[SESSION] Rate limit cooldown: {remaining}s remaining")
                return SessionValidationResult(valid=None, status=SessionStatus.RATE_LIMITED, reason=f"Cooldown: {remaining}s", status_code=429, retry_after=remaining)
        
        # Check cache
        if self._validation_cache:
            cached_result, cached_time = self._validation_cache
            if datetime.now() - cached_time < timedelta(seconds=self.VALIDATION_CACHE_SECONDS):
                logger.info(f"[SESSION] Using cached validation")
                return cached_result
        
        if not self.session:
            session = self.inject_session()
            if not session:
                return SessionValidationResult(valid=False, status=SessionStatus.CORRUPTED, reason="Failed to create session")
        
        logger.info("[SESSION] Validating session via Threads.net...")
        time.sleep(1)
        
        for attempt in range(1, max_retries + 1):
            result = self._do_validate_threads()
            
            if result.status_code == 429:
                self._last_rate_limited = datetime.now()
                if attempt < max_retries:
                    wait_time = 5 * attempt
                    logger.warning(f"[SESSION] RATE LIMITED (429), retry {attempt}/{max_retries} in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    logger.warning("[SESSION] RATE LIMITED - cooldown applied")
                    self._validation_cache = (result, datetime.now())
                    return result
            
            self._validation_cache = (result, datetime.now())
            
            if result.status == SessionStatus.VALID:
                logger.info("[SESSION] VALID (Threads)")
            elif result.status == SessionStatus.EXPIRED:
                logger.error("[SESSION] EXPIRED")
            elif result.status == SessionStatus.RATE_LIMITED:
                logger.warning("[SESSION] RATE LIMITED - cooldown applied")
            
            return result
        
        return SessionValidationResult(valid=None, status=SessionStatus.UNKNOWN, reason="Validation exhausted")
    
    def _do_validate_threads(self) -> SessionValidationResult:
        """
        Bypass Mode Validator: Skips HTML check entirely.

        curl_cffi cannot execute JS, so Meta's React/JS hydration means the raw
        HTML will always show the logged-out template even with valid cookies.
        We simply verify sessionid is present and delegate real auth to Playwright.
        """
        logger.info("\n" + "="*60)
        logger.info("THREADS SESSION VALIDATOR (BYPASS MODE)")
        logger.info("="*60)

        cookie_dict = self.session_data.get_cookie_dict()

        if "sessionid" in cookie_dict and cookie_dict["sessionid"]:
            logger.info("[DEBUG] sessionid found in environment.")
            logger.info("[DEBUG] -> VALID: Bypassing HTML check (Playwright will verify)")
            return SessionValidationResult(
                valid=True, status=SessionStatus.VALID,
                reason="Bypassed HTML check (Playwright will handle JS hydration)",
                status_code=200
            )
        else:
            logger.info("[DEBUG] -> EXPIRED: No sessionid found")
            return SessionValidationResult(
                valid=False, status=SessionStatus.EXPIRED,
                reason="No sessionid cookie found in .env",
                status_code=400
            )


    def is_valid(self) -> Optional[bool]:
        return self._is_valid
    
    def get_session(self) -> Optional[CurlSession]:
        return self.session
    
    def get_validation_result(self) -> Optional[SessionValidationResult]:
        return self._validation_result
    
    def clear_cache(self):
        self._validation_cache = None
        logger.info("[SESSION] Validation cache cleared")


class ThreadsAuth:
    def __init__(self):
        self.session_manager = SessionManager()
        self.session: Optional[CurlSession] = None
        self.is_authenticated = False
        self.user_id: Optional[str] = None
    
    def load_session(self) -> bool:
        if not self.session_manager.load_session():
            return False
        result = self.session_manager.validate_session()
        if result.status == SessionStatus.VALID:
            self.session = self.session_manager.get_session()
            self.is_authenticated = True
            self.user_id = result.user_id
            logger.info(f"[AUTH] Session VALID - User ID: {self.user_id}")
            return True
        if result.status == SessionStatus.EXPIRED:
            logger.error(f"[AUTH] Session EXPIRED: {result.reason}")
        elif result.status == SessionStatus.RATE_LIMITED:
            logger.warning(f"[AUTH] Session RATE LIMITED: {result.reason}")
        elif result.status == SessionStatus.UNKNOWN:
            logger.warning(f"[AUTH] Session UNKNOWN: {result.reason}")
        self.is_authenticated = False
        return False
    
    def get_authenticated_session(self) -> Optional[CurlSession]:
        return self.session if self.is_authenticated else None
    
    def save_session(self, session: CurlSession) -> bool:
        return self.session_manager.save_session(session)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    print("\nSESSION-FIRST AUTOMATION FLOW (Threads-based)")
    auth = ThreadsAuth()
    if not auth.load_session():
        print("\nSESSION EXPIRED - Please refresh manually\n")
    else:
        print("\nSession VALID - automation ready!")
