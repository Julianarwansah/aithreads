"""
LinkedIn Session Manager

Cookie-based auth menggunakan li_at — identik dengan pola Threads SessionManager.
Validasi via Voyager API (/voyager/api/me) bukan HTML scraping.
- NO automatic login retries
- Handles 429 correctly (RATE_LIMITED, not invalid)
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from curl_cffi.requests import Session as CurlSession

logger = logging.getLogger(__name__)

VOYAGER_ME_URL = "https://www.linkedin.com/voyager/api/me"

VOYAGER_HEADERS = {
    "accept": "application/vnd.linkedin.normalized+json+2.1",
    "accept-language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "x-restli-protocol-version": "2.0.0",
    "x-li-lang": "id_ID",
    "x-li-track": '{"clientVersion":"1.13.14914","mpVersion":"1.13.14914","osName":"web","timezoneOffset":7,"timezone":"Asia/Jakarta","deviceFormFactor":"DESKTOP","mpName":"voyager-web","displayDensity":1,"displayWidth":1920,"displayHeight":1080}',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}


class SessionStatus(Enum):
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
    profile_id: Optional[str] = None
    username: Optional[str] = None
    retry_after: Optional[int] = None
    debug_info: Dict[str, Any] = field(default_factory=dict)

    def is_final(self) -> bool:
        return self.status in [SessionStatus.VALID, SessionStatus.EXPIRED, SessionStatus.CORRUPTED]

    def should_retry(self) -> bool:
        return self.status in [SessionStatus.RATE_LIMITED, SessionStatus.UNKNOWN]


@dataclass
class LinkedInSessionData:
    li_at: str = ""
    jsessionid: str = ""        # dipakai sebagai csrf-token header (tanpa quotes)
    li_mc: str = ""             # opsional, bantu bypass geo-check
    li_rm: str = ""             # opsional

    def to_dict(self) -> Dict[str, str]:
        return {k: v for k, v in self.__dict__.items() if v}

    @classmethod
    def from_dict(cls, data: Dict[str, str]) -> "LinkedInSessionData":
        return cls(
            li_at=data.get("li_at", ""),
            jsessionid=data.get("JSESSIONID", data.get("jsessionid", "")),
            li_mc=data.get("li_mc", ""),
            li_rm=data.get("li_rm", ""),
        )

    def is_complete(self) -> bool:
        return bool(self.li_at)

    def get_cookie_dict(self) -> Dict[str, str]:
        cookies = {"li_at": self.li_at}
        if self.jsessionid:
            cookies["JSESSIONID"] = self.jsessionid
        if self.li_mc:
            cookies["li_mc"] = self.li_mc
        if self.li_rm:
            cookies["li_rm"] = self.li_rm
        return cookies

    def get_csrf_token(self) -> str:
        """JSESSIONID tanpa wrapping quotes dipakai sebagai csrf-token header."""
        return self.jsessionid.strip('"')


class LinkedInSessionManager:
    SESSION_FILE = "linkedin_session.json"
    VALIDATION_CACHE_SECONDS = 120
    RATE_LIMIT_COOLDOWN = 60

    def __init__(self, impersonate: str = "chrome124"):
        self.session: Optional[CurlSession] = None
        self.session_data: Optional[LinkedInSessionData] = None
        self._impersonate = impersonate
        self._validation_cache: Optional[tuple[SessionValidationResult, datetime]] = None
        self._last_rate_limited: Optional[datetime] = None
        load_dotenv()

    def load_session(self) -> bool:
        logger.info("[LI_SESSION] Loading session...")

        env_session = self._load_from_env()
        if env_session and env_session.is_complete():
            self.session_data = env_session
            logger.info("[LI_SESSION] Loaded from environment variables")
            return True

        file_session = self._load_from_file()
        if file_session and file_session.is_complete():
            self.session_data = file_session
            logger.info("[LI_SESSION] Loaded from linkedin_session.json")
            return True

        logger.warning("[LI_SESSION] No valid session found — set LINKEDIN_LI_AT in .env")
        return False

    def _load_from_env(self) -> Optional[LinkedInSessionData]:
        li_at = os.getenv("LINKEDIN_LI_AT", "")
        if not li_at:
            return None
        return LinkedInSessionData(
            li_at=li_at,
            jsessionid=os.getenv("LINKEDIN_JSESSIONID", ""),
            li_mc=os.getenv("LINKEDIN_LI_MC", ""),
            li_rm=os.getenv("LINKEDIN_LI_RM", ""),
        )

    def _load_from_file(self) -> Optional[LinkedInSessionData]:
        _dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(_dir, self.SESSION_FILE)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r") as f:
                data = json.load(f)
            if not data.get("li_at"):
                return None
            return LinkedInSessionData.from_dict(data)
        except Exception as e:
            logger.error(f"[LI_SESSION] Failed to load file: {e}")
            return None

    def save_session(self, data: Dict[str, str]) -> bool:
        _dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(_dir, self.SESSION_FILE)
        try:
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
            logger.info(f"[LI_SESSION] Saved to {self.SESSION_FILE}")
            return True
        except Exception as e:
            logger.error(f"[LI_SESSION] Failed to save: {e}")
            return False

    def inject_session(self) -> Optional[CurlSession]:
        if not self.session_data:
            logger.error("[LI_SESSION] No session data to inject")
            return None
        session = CurlSession(impersonate=self._impersonate)
        for k, v in self.session_data.get_cookie_dict().items():
            session.cookies.set(k, v, domain=".linkedin.com")
            session.cookies.set(k, v, domain="www.linkedin.com")
        logger.info(f"[LI_SESSION] Injected {len(self.session_data.get_cookie_dict())} cookies")
        self.session = session
        return session

    def get_request_headers(self) -> Dict[str, str]:
        headers = dict(VOYAGER_HEADERS)
        if self.session_data and self.session_data.get_csrf_token():
            headers["csrf-token"] = self.session_data.get_csrf_token()
        return headers

    def validate_session(self, max_retries: int = 2) -> SessionValidationResult:
        if not self.session_data:
            return SessionValidationResult(
                valid=False, status=SessionStatus.NOT_FOUND, reason="No session loaded"
            )

        if self._last_rate_limited:
            elapsed = (datetime.now() - self._last_rate_limited).total_seconds()
            if elapsed < self.RATE_LIMIT_COOLDOWN:
                remaining = self.RATE_LIMIT_COOLDOWN - int(elapsed)
                return SessionValidationResult(
                    valid=None, status=SessionStatus.RATE_LIMITED,
                    reason=f"Cooldown: {remaining}s", status_code=429, retry_after=remaining
                )

        if self._validation_cache:
            cached_result, cached_time = self._validation_cache
            if datetime.now() - cached_time < timedelta(seconds=self.VALIDATION_CACHE_SECONDS):
                logger.info("[LI_SESSION] Using cached validation")
                return cached_result

        if not self.session:
            session = self.inject_session()
            if not session:
                return SessionValidationResult(
                    valid=False, status=SessionStatus.CORRUPTED, reason="Failed to create session"
                )

        logger.info("[LI_SESSION] Validating session via Voyager API...")
        time.sleep(1)

        for attempt in range(1, max_retries + 1):
            result = self._do_validate()

            if result.status_code == 429:
                self._last_rate_limited = datetime.now()
                if attempt < max_retries:
                    wait_time = 10 * attempt
                    logger.warning(f"[LI_SESSION] RATE LIMITED, retry {attempt}/{max_retries} in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                else:
                    self._validation_cache = (result, datetime.now())
                    return result

            self._validation_cache = (result, datetime.now())

            if result.status == SessionStatus.VALID:
                logger.info(f"[LI_SESSION] VALID — profile_id: {result.profile_id}")
            elif result.status == SessionStatus.EXPIRED:
                logger.error("[LI_SESSION] EXPIRED — refresh li_at cookie")

            return result

        return SessionValidationResult(
            valid=None, status=SessionStatus.UNKNOWN, reason="Validation exhausted"
        )

    def _do_validate(self) -> SessionValidationResult:
        logger.info("=" * 60)
        logger.info("LINKEDIN SESSION VALIDATOR")
        logger.info("=" * 60)

        try:
            headers = self.get_request_headers()
            resp = self.session.get(
                VOYAGER_ME_URL,
                headers=headers,
                timeout=15,
            )
            code = resp.status_code
            logger.info(f"[LI_SESSION] GET /voyager/api/me → {code}")

            if code == 200:
                try:
                    data = resp.json()
                    # Voyager /me membungkus data dalam "data" key
                    me = data.get("data", data)
                    profile_id = me.get("miniProfile", {}).get("objectUrn", "")
                    username = me.get("miniProfile", {}).get("publicIdentifier", "")
                except Exception:
                    profile_id = ""
                    username = ""
                return SessionValidationResult(
                    valid=True, status=SessionStatus.VALID,
                    reason="Voyager /me returned 200",
                    status_code=200,
                    profile_id=profile_id,
                    username=username,
                )

            if code in (401, 403):
                return SessionValidationResult(
                    valid=False, status=SessionStatus.EXPIRED,
                    reason=f"Voyager /me returned {code} — li_at expired",
                    status_code=code,
                )

            if code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                return SessionValidationResult(
                    valid=None, status=SessionStatus.RATE_LIMITED,
                    reason=f"Rate limited (429)", status_code=429,
                    retry_after=retry_after,
                )

            return SessionValidationResult(
                valid=None, status=SessionStatus.UNKNOWN,
                reason=f"Unexpected status: {code}", status_code=code,
            )

        except Exception as e:
            logger.error(f"[LI_SESSION] Validation error: {e}")
            return SessionValidationResult(
                valid=None, status=SessionStatus.UNKNOWN,
                reason=str(e), status_code=0,
            )

    def get_session(self) -> Optional[CurlSession]:
        return self.session

    def clear_cache(self):
        self._validation_cache = None
        logger.info("[LI_SESSION] Validation cache cleared")


class LinkedInAuth:
    """High-level auth wrapper, identik dengan ThreadsAuth."""

    def __init__(self):
        self.session_manager = LinkedInSessionManager()
        self.session: Optional[CurlSession] = None
        self.is_authenticated = False
        self.profile_id: Optional[str] = None
        self.username: Optional[str] = None

    def load_session(self) -> bool:
        if not self.session_manager.load_session():
            return False
        result = self.session_manager.validate_session()
        if result.status == SessionStatus.VALID:
            self.session = self.session_manager.get_session()
            self.is_authenticated = True
            self.profile_id = result.profile_id
            self.username = result.username
            logger.info(f"[LI_AUTH] Session VALID — @{self.username}")
            return True
        if result.status == SessionStatus.EXPIRED:
            logger.error(f"[LI_AUTH] Session EXPIRED: {result.reason}")
        elif result.status == SessionStatus.RATE_LIMITED:
            logger.warning(f"[LI_AUTH] Session RATE LIMITED: {result.reason}")
        self.is_authenticated = False
        return False

    def get_authenticated_session(self) -> Optional[CurlSession]:
        return self.session if self.is_authenticated else None

    def get_request_headers(self) -> Dict[str, str]:
        return self.session_manager.get_request_headers()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print("\nLINKEDIN SESSION VALIDATOR")
    print("Pastikan LINKEDIN_LI_AT sudah di-set di .env\n")
    auth = LinkedInAuth()
    if not auth.load_session():
        print("\nSESSION INVALID — copy li_at dari browser dan update .env\n")
    else:
        print(f"\nSession VALID! username=@{auth.username}, profile_id={auth.profile_id}\n")
