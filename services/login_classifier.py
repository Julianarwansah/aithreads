"""
Login Rejection Classifier for Instagram/Threads Authentication

Diagnostic tool to classify WHY login fails based on:
- HTTP response
- JSON response
- Cookie state
- Session behavior
- Request flow signals

This is ONLY a diagnostic classifier - it does NOT attempt to fix login.
"""

import json
import logging
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class RejectionCategory(Enum):
    """Classification categories for login rejection."""
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    CHECKPOINT_REQUIRED = "CHECKPOINT_REQUIRED"
    RATE_LIMITED = "RATE_LIMITED"
    BOT_DETECTED = "BOT_DETECTED"
    CSRF_SESSION_MISMATCH = "CSRF_SESSION_MISMATCH"
    NETWORK_ERROR = "NETWORK_ERROR"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    UNKNOWN_FAILURE = "UNKNOWN_FAILURE"
    LOGIN_SUCCESS = "LOGIN_SUCCESS"


@dataclass
class ClassifierResult:
    """Structured result from login rejection classification."""
    category: RejectionCategory
    confidence: float  # 0.0 - 1.0
    reason: str
    signals: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category.value,
            "confidence": self.confidence,
            "reason": self.reason,
            "signals": self.signals,
            "recommendations": self.recommendations
        }


@dataclass
class LoginContext:
    """
    Context data for login classification.
    
    Collects all signals before and after login attempt.
    """
    # HTTP Response
    http_status: int = 0
    response_text: str = ""
    response_json: Dict[str, Any] = field(default_factory=dict)
    
    # Cookie State
    cookies_before_login: Dict[str, str] = field(default_factory=dict)
    cookies_after_login: Dict[str, str] = field(default_factory=dict)
    
    # CSRF State
    csrf_before: Optional[str] = None
    csrf_after: Optional[str] = None
    
    # Request Info
    endpoint_attempted: str = ""
    username: str = ""
    timestamp: Optional[int] = None
    
    # Additional Signals
    login_page_status: int = 0
    homepage_status: int = 0
    rate_limit_count: int = 0


def extract_cookie_delta(before: Dict, after: Dict) -> Tuple[List[str], List[str], List[str]]:
    """
    Extract cookie changes between before and after states.
    
    Returns:
        (added, removed, changed) cookie lists
    """
    before_keys = set(before.keys())
    after_keys = set(after.keys())
    
    added = list(after_keys - before_keys)
    removed = list(before_keys - after_keys)
    changed = [k for k in before_keys & after_keys if before[k] != after[k]]
    
    return added, removed, changed


def check_sessionid_presence(cookies: Dict[str, str]) -> bool:
    """Check if sessionid cookie exists in given cookie dict."""
    return "sessionid" in cookies


def analyze_response_json(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Analyze JSON response and extract key signals.
    
    Returns dict with extracted signals.
    """
    return {
        "status": data.get("status", ""),
        "authenticated": data.get("authenticated", False),
        "user_present": data.get("user") is not None,
        "error_type": data.get("error_type", ""),
        "message": data.get("message", ""),
        "checkpoint_url": data.get("checkpointUrl", ""),
        "is_vetted": data.get("is_vetted", True),
        "lock": data.get("lock", False),
        "feedback": data.get("feedback", ""),
        "spam": data.get("spam", False),
        "second_factor": data.get("second_factor", False),
        "authenticated_2fa": data.get("authenticated", False) and data.get("two_factor_info", False),
    }


def detect_csrf_anomaly(csrf_before: Optional[str], csrf_after: Optional[str], 
                        cookies_before: Dict, cookies_after: Dict) -> Optional[str]:
    """
    Detect CSRF-related anomalies.
    
    Returns anomaly description or None if no anomaly.
    """
    anomalies = []
    
    # CSRF disappeared
    if csrf_before and not csrf_after:
        anomalies.append("CSRF token disappeared after login")
    
    # CSRF changed unexpectedly
    if csrf_before and csrf_after and csrf_before != csrf_after:
        anomalies.append(f"CSRF token changed: {csrf_before[:10]}... -> {csrf_after[:10]}...")
    
    # Critical cookies disappeared (except expected changes)
    critical_cookies = ["ig_did", "mid", "csrftoken", "datr"]
    for cookie in critical_cookies:
        if cookie in cookies_before and cookie not in cookies_after:
            if cookie in ["ig_did", "mid", "datr"]:  # These should persist
                anomalies.append(f"Critical cookie '{cookie}' disappeared")
    
    return "; ".join(anomalies) if anomalies else None


class LoginRejectionClassifier:
    """
    Classifier for Instagram login rejections.
    
    Analyzes login attempt context and classifies rejection type.
    Deterministic - no AI guessing involved.
    """
    
    def __init__(self):
        self.context: Optional[LoginContext] = None
        self._signals: List[str] = []
        self._recommendations: List[str] = []
    
    def classify(self, context: LoginContext) -> ClassifierResult:
        """
        Classify login rejection based on provided context.
        
        Args:
            context: LoginContext with all request/response data
            
        Returns:
            ClassifierResult with category, confidence, and details
        """
        self.context = context
        self._signals = []
        self._recommendations = []
        
        # Log the classification attempt
        logger.info(f"[Classifier] Analyzing login attempt for: {context.username}")
        logger.info(f"[Classifier] HTTP Status: {context.http_status}")
        
        # Extract signals from response
        signals = analyze_response_json(context.response_json)
        
        # Check for various rejection types in order of specificity
        result = self._check_success(signals)
        if result:
            return result
            
        result = self._check_checkpoint_required(signals)
        if result:
            return result
            
        result = self._check_invalid_credentials(signals)
        if result:
            return result
            
        result = self._check_rate_limited(context, signals)
        if result:
            return result
            
        result = self._check_csrf_session_mismatch(context)
        if result:
            return result
            
        result = self._check_bot_detected(context, signals)
        if result:
            return result
            
        result = self._check_network_error(context)
        if result:
            return result
            
        result = self._check_session_expired(signals)
        if result:
            return result
        
        return self._unknown_failure(signals)
    
    def _add_signal(self, signal: str):
        """Add a signal to the list."""
        self._signals.append(signal)
        logger.debug(f"[Classifier] Signal: {signal}")
    
    def _add_recommendation(self, rec: str):
        """Add a recommendation to the list."""
        self._recommendations.append(rec)
    
    def _check_success(self, signals: Dict) -> Optional[ClassifierResult]:
        """Check if login was successful."""
        if signals["authenticated"] and check_sessionid_presence(self.context.cookies_after_login):
            return ClassifierResult(
                category=RejectionCategory.LOGIN_SUCCESS,
                confidence=1.0,
                reason="Login successful - sessionid cookie issued",
                signals=["authenticated: true", "sessionid present"],
                recommendations=["Store cookies for future use", "Test authenticated endpoints"]
            )
        return None
    
    def _check_invalid_credentials(self, signals: Dict) -> Optional[ClassifierResult]:
        """Check for invalid credentials rejection."""
        if signals["error_type"] == "UserInvalidCredentials":
            self._add_signal("error_type: UserInvalidCredentials")
            self._add_signal(f"user_present: {signals['user_present']}")
            
            # Determine confidence based on user presence
            confidence = 0.95 if signals["user_present"] else 0.7
            
            return ClassifierResult(
                category=RejectionCategory.INVALID_CREDENTIALS,
                confidence=confidence,
                reason="Username or password is incorrect",
                signals=self._signals.copy(),
                recommendations=[
                    "Verify username spelling",
                    "Check password (case-sensitive)",
                    "Ensure no extra spaces or special characters",
                    "Try resetting password via Instagram"
                ]
            )
        
        # Also check for explicit failure messages
        if signals["status"] == "fail" and not signals["authenticated"]:
            if "invalid" in signals["message"].lower() or "password" in signals["message"].lower():
                self._add_signal(f"failure_message: {signals['message']}")
                return ClassifierResult(
                    category=RejectionCategory.INVALID_CREDENTIALS,
                    confidence=0.9,
                    reason=f"Login failed: {signals['message']}",
                    signals=self._signals.copy(),
                    recommendations=["Verify credentials", "Try password reset"]
                )
        
        return None
    
    def _check_checkpoint_required(self, signals: Dict) -> Optional[ClassifierResult]:
        """Check if checkpoint (verification) is required."""
        checkpoint_url = signals.get("checkpoint_url", "")
        
        if signals["error_type"] == "checkpoint_required" or checkpoint_url:
            self._add_signal("checkpoint_required detected")
            if checkpoint_url:
                self._add_signal(f"checkpoint_url: {checkpoint_url}")
            
            # Check what type of verification
            verification_type = "email" if "email" in checkpoint_url else "phone" if "phone" in checkpoint_url else "unknown"
            self._add_signal(f"verification_type: {verification_type}")
            
            return ClassifierResult(
                category=RejectionCategory.CHECKPOINT_REQUIRED,
                confidence=1.0,
                reason="Account verification required (checkpoint)",
                signals=self._signals.copy(),
                recommendations=[
                    "Complete verification in browser first",
                    "Extract cookies AFTER completing verification",
                    "Use session injection method instead of login"
                ]
            )
        
        # Check for 2FA requirement
        if signals.get("second_factor") or signals.get("authenticated_2fa"):
            self._add_signal("2FA required")
            return ClassifierResult(
                category=RejectionCategory.CHECKPOINT_REQUIRED,
                confidence=1.0,
                reason="Two-factor authentication required",
                signals=self._signals.copy(),
                recommendations=[
                    "Use session injection with cookies from authenticated browser",
                    "Complete 2FA in browser first"
                ]
            )
        
        return None
    
    def _check_rate_limited(self, context: LoginContext, signals: Dict) -> Optional[ClassifierResult]:
        """Check for rate limiting."""
        # HTTP 429 status
        if context.http_status == 429:
            self._add_signal("HTTP 429 Rate Limited")
            self._add_recommendation("Wait before retrying (exponential backoff)")
            return ClassifierResult(
                category=RejectionCategory.RATE_LIMITED,
                confidence=1.0,
                reason="Too many requests - rate limited by Instagram",
                signals=self._signals.copy(),
                recommendations=[
                    "Wait 5-10 minutes before retry",
                    "Use exponential backoff",
                    "Consider session injection instead"
                ]
            )
        
        # Check for rate limit signals in response
        if signals.get("spam") or "rate" in signals.get("message", "").lower():
            self._add_signal(f"rate_limit_signal: {signals.get('message')}")
            return ClassifierResult(
                category=RejectionCategory.RATE_LIMITED,
                confidence=0.85,
                reason="Rate limiting detected from response",
                signals=self._signals.copy(),
                recommendations=["Reduce request frequency", "Use session injection"]
            )
        
        # Check for slow cookie updates (indicates server hesitation)
        added, removed, changed = extract_cookie_delta(
            context.cookies_before_login, 
            context.cookies_after_login
        )
        if len(added) < 2 and context.http_status != 200:
            self._add_signal(f"minimal_cookie_update: {len(added)} cookies added")
        
        return None
    
    def _check_csrf_session_mismatch(self, context: LoginContext) -> Optional[ClassifierResult]:
        """Check for CSRF or session state mismatch."""
        anomaly = detect_csrf_anomaly(
            context.csrf_before, 
            context.csrf_after,
            context.cookies_before_login,
            context.cookies_after_login
        )
        
        if anomaly:
            self._add_signal(f"csrf_anomaly: {anomaly}")
            
            # Check if CSRF was the issue
            if "CSRF" in anomaly:
                self._add_recommendation("Ensure single session object throughout request chain")
                return ClassifierResult(
                    category=RejectionCategory.CSRF_SESSION_MISMATCH,
                    confidence=0.9,
                    reason="CSRF token mismatch or disappearance",
                    signals=self._signals.copy(),
                    recommendations=[
                        "Use single session object for all requests",
                        "Extract CSRF immediately before login POST",
                        "Verify no session reset between bootstrap and login"
                    ]
                )
            
            return ClassifierResult(
                category=RejectionCategory.CSRF_SESSION_MISMATCH,
                confidence=0.75,
                reason="Session state inconsistency detected",
                signals=self._signals.copy(),
                recommendations=["Check session object lifecycle", "Verify cookie persistence"]
            )
        
        return None
    
    def _check_bot_detected(self, context: LoginContext, signals: Dict) -> Optional[ClassifierResult]:
        """
        Check if bot detection triggered.
        
        Key indicators:
        - authenticated: false
        - user: true (user exists)
        - status: ok (request reached server)
        - sessionid NOT created
        """
        # Core pattern for bot detection
        core_pattern = (
            not signals["authenticated"] and
            signals["user_present"] and
            signals["status"] == "ok" and
            not check_sessionid_presence(context.cookies_after_login)
        )
        
        if core_pattern:
            self._add_signal("core_bot_pattern: authenticated=false, user=true, no sessionid")
            
            # Check cookie changes for anomalies
            added, removed, changed = extract_cookie_delta(
                context.cookies_before_login,
                context.cookies_after_login
            )
            
            # Sessionid should be added but wasn't
            if "sessionid" not in added:
                self._add_signal("sessionid_not_created")
                self._add_recommendation("Session not issued - bot detection likely")
            
            # Check for suspicious cookie removal
            if len(removed) > 2:
                self._add_signal(f"excessive_cookie_removal: {removed}")
            
            # Check for lock
            if signals.get("lock"):
                self._add_signal("account_locked")
            
            # Determine confidence
            confidence = 0.8
            if not signals["is_vetted"]:
                confidence = 0.95
                self._add_signal("is_vetted: false (strong bot signal)")
            
            return ClassifierResult(
                category=RejectionCategory.BOT_DETECTED,
                confidence=confidence,
                reason="Bot detection triggered - session not issued",
                signals=self._signals.copy(),
                recommendations=[
                    "Use session injection with real browser cookies",
                    "Improve TLS fingerprinting",
                    "Add request delays between steps",
                    "Consider using a residential proxy",
                    "Complete login in browser and extract cookies"
                ]
            )
        
        return None
    
    def _check_network_error(self, context: LoginContext) -> Optional[ClassifierResult]:
        """Check for network or connection errors."""
        if context.http_status == 0 or context.http_status >= 500:
            self._add_signal(f"http_status: {context.http_status}")
            
            if context.http_status == 0:
                return ClassifierResult(
                    category=RejectionCategory.NETWORK_ERROR,
                    confidence=1.0,
                    reason="Connection failed or timeout",
                    signals=self._signals.copy(),
                    recommendations=["Check internet connection", "Retry with longer timeout"]
                )
            
            return ClassifierResult(
                category=RejectionCategory.NETWORK_ERROR,
                confidence=0.9,
                reason=f"Server error (HTTP {context.http_status})",
                signals=self._signals.copy(),
                recommendations=["Instagram server issue - retry later"]
            )
        
        return None
    
    def _check_session_expired(self, signals: Dict) -> Optional[ClassifierResult]:
        """Check if existing session has expired."""
        if "expired" in signals.get("message", "").lower():
            self._add_signal(f"expired_signal: {signals['message']}")
            return ClassifierResult(
                category=RejectionCategory.SESSION_EXPIRED,
                confidence=0.95,
                reason="Session or token has expired",
                signals=self._signals.copy(),
                recommendations=["Obtain fresh session cookies", "Login in browser again"]
            )
        
        return None
    
    def _unknown_failure(self, signals: Dict) -> ClassifierResult:
        """Handle unknown/unclassified failures."""
        self._add_signal(f"unclassified_response: {signals}")
        
        return ClassifierResult(
            category=RejectionCategory.UNKNOWN_FAILURE,
            confidence=0.5,
            reason="Login failure could not be classified",
            signals=self._signals.copy(),
            recommendations=[
                "Enable full debug logging",
                "Check for new Instagram API changes",
                "Try session injection method"
            ]
        )


def classify_login_result(
    http_status: int,
    response_text: str,
    cookies_before: Dict[str, str],
    cookies_after: Dict[str, str],
    csrf_before: Optional[str] = None,
    csrf_after: Optional[str] = None,
    endpoint: str = "",
    username: str = ""
) -> ClassifierResult:
    """
    Convenience function to classify a login result.
    
    Args:
        http_status: HTTP status code
        response_text: Raw response text
        cookies_before: Cookies before login attempt
        cookies_after: Cookies after login attempt
        csrf_before: CSRF token before login
        csrf_after: CSRF token after login
        endpoint: API endpoint attempted
        username: Username attempted
        
    Returns:
        ClassifierResult with classification
    """
    # Parse JSON if possible
    try:
        response_json = json.loads(response_text) if response_text else {}
    except json.JSONDecodeError:
        response_json = {}
    
    # Build context
    context = LoginContext(
        http_status=http_status,
        response_text=response_text,
        response_json=response_json,
        cookies_before_login=cookies_before,
        cookies_after_login=cookies_after,
        csrf_before=csrf_before,
        csrf_after=csrf_after,
        endpoint_attempted=endpoint,
        username=username
    )
    
    # Classify
    classifier = LoginRejectionClassifier()
    result = classifier.classify(context)
    
    return result


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    # Set up logging
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    
    print("\n" + "="*70)
    print("LOGIN REJECTION CLASSIFIER - EXAMPLE USAGE")
    print("="*70)
    
    # Example 1: Bot Detection Pattern
    print("\n[Example 1] Bot Detection Pattern")
    print("-"*50)
    
    example_response_1 = {
        "status": "ok",
        "authenticated": False,
        "user": {"id": "123456789"},
        "is_vetted": False,
        "error_type": "",
        "message": ""
    }
    
    context_1 = LoginContext(
        http_status=200,
        response_text=json.dumps(example_response_1),
        response_json=example_response_1,
        cookies_before_login={"csrftoken": "abc123", "ig_did": "xyz", "mid": "m123"},
        cookies_after_login={"csrftoken": "def456", "ig_did": "xyz", "mid": "m123", "ig_nrcb": "1"},
        csrf_before="abc123",
        csrf_after="def456",
        username="test_user"
    )
    
    classifier = LoginRejectionClassifier()
    result_1 = classifier.classify(context_1)
    print(f"Category: {result_1.category.value}")
    print(f"Confidence: {result_1.confidence:.2f}")
    print(f"Reason: {result_1.reason}")
    print(f"Signals: {result_1.signals}")
    print(f"Recommendations: {result_1.recommendations}")
    
    # Example 2: Invalid Credentials
    print("\n[Example 2] Invalid Credentials")
    print("-"*50)
    
    example_response_2 = {
        "status": "fail",
        "authenticated": False,
        "user": True,
        "error_type": "UserInvalidCredentials",
        "message": ""
    }
    
    context_2 = LoginContext(
        http_status=400,
        response_text=json.dumps(example_response_2),
        response_json=example_response_2,
        cookies_before_login={"csrftoken": "abc123"},
        cookies_after_login={"csrftoken": "abc123"},
        csrf_before="abc123",
        csrf_after="abc123",
        username="wrong_user"
    )
    
    result_2 = classifier.classify(context_2)
    print(f"Category: {result_2.category.value}")
    print(f"Confidence: {result_2.confidence:.2f}")
    print(f"Reason: {result_2.reason}")
    
    # Example 3: Using convenience function
    print("\n[Example 3] Convenience Function")
    print("-"*50)
    
    result_3 = classify_login_result(
        http_status=200,
        response_text='{"user":true,"authenticated":false,"status":"ok"}',
        cookies_before={"csrftoken": "x", "ig_did": "y"},
        cookies_after={"csrftoken": "x", "ig_did": "y"},
        csrf_before="x",
        csrf_after="x",
        username="test"
    )
    
    print(f"Category: {result_3.category.value}")
    print(f"Confidence: {result_3.confidence:.2f}")
    print(f"Full Result: {json.dumps(result_3.to_dict(), indent=2)}")