"""
Threads/Instagram Authentication Module

DEPRECATED - Use services/session_manager.py instead

This module is kept for backwards compatibility only.
The current system uses Session-First Architecture via SessionManager.

ACTUAL AUTHENTICATION:
- Use SessionManager from services/session_manager.py
- Load session from env/file (.env or session.json)
- Validate before use
- NO login retry logic

This file is kept as a reference/stub only.
"""

import logging

logger = logging.getLogger(__name__)


class ThreadsAuth:
    """
    DEPRECATED: Authentication stub.
    
    Use services/session_manager.ThreadsAuth instead.
    
    This class exists only for backwards compatibility.
    Runtime uses SessionManager for session-first authentication.
    """
    
    def __init__(self):
        logger.warning("⚠️ DEPRECATED: services.threads_auth.ThreadsAuth")
        logger.warning("   Use services.session_manager.ThreadsAuth instead")
        self.session = None
        self.is_authenticated = False
        self.user_id = None
    
    def load_session(self) -> bool:
        """
        Stub method - use SessionManager instead.
        """
        logger.warning("⚠️ Use services.session_manager.SessionManager.load_session()")
        return False
    
    def get_authenticated_session(self):
        """
        Stub method - use SessionManager instead.
        """
        logger.warning("⚠️ Use services.session_manager.SessionManager.get_session()")
        return None
    
    def save_session(self, session):
        """
        Stub method - use SessionManager instead.
        """
        logger.warning("⚠️ Use services.session_manager.SessionManager.save_session()")
        return False


# Keep these for reference - DO NOT USE
def test_login(username: str, password: str) -> bool:
    """
    DEPRECATED: Test login function.
    
    ⚠️ DO NOT USE - Use SessionManager instead
    
    Login-based authentication is no longer supported.
    The system uses session-first architecture.
    """
    logger.error("⚠️ DEPRECATED: test_login() - Use SessionManager instead")
    logger.error("   Login flow causes trust-layer rejection and infinite loops")
    return False


if __name__ == "__main__":
    print("⚠️ DEPRECATED MODULE")
    print("Use services.session_manager instead")
    print("")
    print("Current authentication flow:")
    print("  1. Load session from .env or session.json")
    print("  2. Validate via SessionManager.validate_session()")
    print("  3. Use SessionManager.get_session() for requests")
