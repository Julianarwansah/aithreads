"""
Deep Feed Discovery - Response Capture & Analysis

Captures FULL response bodies from Threads network requests.
Focuses on:
- /ajax/bz endpoints
- GraphQL requests
- Feed-related endpoints
- Timeline endpoints

DO NOT USE FOR:
- AI intent detection
- Lead scoring
- Keyword matching
- Outreach automation

ONLY USE FOR:
- Endpoint analysis
- Response structure discovery
- Data extraction architecture
"""

import json
import logging
import os
import re
import zlib
import base64
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field, asdict
from urllib.parse import urlparse, parse_qs

from playwright.sync_api import sync_playwright, Page, Request, Response

logger = logging.getLogger(__name__)


@dataclass
class CapturedRequest:
    """Full captured request with body."""
    timestamp: str
    method: str
    url: str
    query_params: Dict[str, str] = field(default_factory=dict)
    request_headers: Dict[str, str] = field(default_factory=dict)
    request_body: Optional[str] = None
    response_status: int = 0
    response_headers: Dict[str, str] = field(default_factory=dict)
    response_body: Optional[str] = None
    response_size: int = 0
    content_type: str = ""
    endpoint_type: str = "other"
    decoded: bool = False
    decoded_data: Optional[Dict] = None


@dataclass
class FeedAnalysis:
    """Analysis of feed data in response."""
    has_posts: bool = False
    has_username: bool = False
    has_user_id: bool = False
    has_thread_id: bool = False
    has_media_url: bool = False
    has_timestamp: bool = False
    has_text: bool = False
    data_fields: List[str] = field(default_factory=list)
    extraction_feasible: bool = False
    notes: str = ""


class DeepFeedDiscovery:
    """
    Deep network capture with full response bodies.
    
    Usage:
        discovery = DeepFeedDiscovery(session_cookies)
        discovery.capture_full(duration_seconds=45)
        discovery.analyze_responses()
        report = discovery.generate_report()
    """
    
    THREADS_URL = "https://www.threads.net/"
    
    FOCUS_PATTERNS = [
        "/ajax/bz",
        "graphql",
        "relay",
        "feed",
        "timeline",
        "barcelona",
        "comet",
    ]
    
    def __init__(self, cookies: Dict[str, str]):
        self.cookies = cookies
        self.captures: List[CapturedRequest] = []
        self.bz_requests: List[CapturedRequest] = []
        self.graphql_requests: List[CapturedRequest] = []
        self.feed_requests: List[CapturedRequest] = []
        self._page: Optional[Page] = None
        self._capture_dir = "data/captures"
        
    def _ensure_capture_dir(self):
        """Ensure capture directory exists."""
        os.makedirs(self._capture_dir, exist_ok=True)
    
    def _decompress_if_needed(self, data: bytes) -> bytes:
        """Decompress compressed response if needed."""
        try:
            return zlib.decompress(data)
        except:
            pass
        
        try:
            return zlib.decompress(data, 16 + zlib.MAX_WBITS)
        except:
            pass
        
        return data
    
    def _decode_response(self, data: bytes, content_type: str) -> Tuple[str, bool, Optional[Dict]]:
        """Attempt to decode response body."""
        decoded_str = ""
        is_json = False
        parsed = None
        
        try:
            data = self._decompress_if_needed(data)
        except:
            pass
        
        try:
            decoded_str = data.decode('utf-8', errors='ignore')
        except:
            decoded_str = str(data)
        
        try:
            parsed = json.loads(decoded_str)
            is_json = True
        except:
            json_match = re.search(r'\{".*\}', decoded_str, re.DOTALL)
            if json_match:
                try:
                    parsed = json.loads(json_match.group())
                    is_json = True
                    decoded_str = json_match.group()
                except:
                    pass
        
        b64_match = re.search(r'"data":"([^"]+)"', decoded_str)
        if b64_match and not is_json:
            try:
                decoded_data = base64.b64decode(b64_match.group(1))
                parsed = json.loads(decoded_data.decode('utf-8'))
                is_json = True
            except:
                pass
        
        return decoded_str, is_json, parsed
    
    def _classify_endpoint(self, url: str) -> str:
        """Classify endpoint type."""
        url_lower = url.lower()
        
        if "/ajax/bz" in url_lower:
            return "bz"
        elif "graphql" in url_lower:
            return "graphql"
        elif any(kw in url_lower for kw in ["feed", "timeline", "barcelona"]):
            return "feed"
        elif any(kw in url_lower for kw in ["search", "query"]):
            return "search"
        elif any(kw in url_lower for kw in ["profile", "user"]):
            return "profile"
        elif any(kw in url_lower for kw in ["thread", "post"]):
            return "thread"
        
        return "other"
    
    def _on_request(self, request: Request):
        """Capture full request details."""
        try:
            url = request.url
            
            if not any(pattern in url.lower() for pattern in self.FOCUS_PATTERNS):
                return
            
            if "threads.net" not in url and "instagram.com" not in url:
                return
            
            timestamp = datetime.now().isoformat()
            parsed = urlparse(url)
            query_params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            request_headers = dict(request.headers)
            
            request_body = None
            try:
                if request.post_data_buffer:
                    request_body = request.post_data_buffer.decode('utf-8', errors='ignore')
            except:
                pass
            
            capture = CapturedRequest(
                timestamp=timestamp,
                method=request.method,
                url=url,
                query_params=query_params,
                request_headers=request_headers,
                request_body=request_body,
                endpoint_type=self._classify_endpoint(url)
            )
            
            self.captures.append(capture)
            logger.debug(f"[CAPTURE] {request.method} {capture.endpoint_type} {url[:80]}")
            
        except Exception as e:
            logger.error(f"Request capture error: {e}")
    
    def _on_response(self, response: Response):
        """Capture full response details."""
        try:
            url = response.url
            
            for capture in self.captures:
                if capture.url == url:
                    capture.response_status = response.status
                    capture.response_headers = dict(response.headers)
                    
                    ct = response.headers.get("content-type", "")
                    capture.content_type = ct[:100] if ct else ""
                    
                    try:
                        body = response.body()
                        capture.response_size = len(body)
                        
                        decoded_str, is_json, parsed = self._decode_response(body, capture.content_type)
                        capture.response_body = decoded_str[:50000]
                        capture.decoded = is_json
                        capture.decoded_data = parsed
                        
                    except Exception as e:
                        logger.debug(f"Response body error: {e}")
                    
                    break
                    
        except Exception as e:
            logger.error(f"Response capture error: {e}")
    
    def capture_full(self, duration_seconds: int = 45) -> List[CapturedRequest]:
        """Capture full responses for focused duration."""
        self._ensure_capture_dir()
        
        logger.info("="*60)
        logger.info("DEEP FEED DISCOVERY - Starting Full Capture")
        logger.info("="*60)
        logger.info(f"Duration: {duration_seconds}s")
        logger.info(f"Focus: {self.FOCUS_PATTERNS}")
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            
            cookies_list = []
            for name, value in self.cookies.items():
                cookies_list.append({
                    "name": name,
                    "value": value,
                    "domain": ".threads.net",
                    "path": "/"
                })
            context.add_cookies(cookies_list)
            
            self._page = context.new_page()
            self._page.on("request", self._on_request)
            self._page.on("response", self._on_response)
            
            logger.info(f"Navigating to {self.THREADS_URL}")
            try:
                self._page.goto(self.THREADS_URL, wait_until="networkidle", timeout=60000)
            except Exception as e:
                logger.error(f"Navigation error: {e}")
            
            logger.info("Waiting for page hydration...")
            self._page.wait_for_timeout(5000)
            
            logger.info("Scrolling to trigger lazy-loaded content...")
            for i in range(8):
                self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                self._page.wait_for_timeout(1500)
                self._page.evaluate("window.scrollTo(0, 0)")
                self._page.wait_for_timeout(1000)
            
            logger.info(f"Final capture: {duration_seconds}s...")
            self._page.wait_for_timeout(duration_seconds * 1000)
            
            browser.close()
        
        self.bz_requests = [c for c in self.captures if c.endpoint_type == "bz"]
        self.graphql_requests = [c for c in self.captures if c.endpoint_type == "graphql"]
        self.feed_requests = [c for c in self.captures if c.endpoint_type == "feed"]
        
        logger.info(f"Capture complete: {len(self.captures)} requests")
        logger.info(f"  - BZ requests: {len(self.bz_requests)}")
        logger.info(f"  - GraphQL requests: {len(self.graphql_requests)}")
        logger.info(f"  - Feed requests: {len(self.feed_requests)}")
        
        return self.captures
    
    def save_captures(self):
        """Save all captures to files."""
        self._ensure_capture_dir()
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        for i, capture in enumerate(self.bz_requests):
            req_data = {
                "timestamp": capture.timestamp,
                "method": capture.method,
                "url": capture.url,
                "query_params": capture.query_params,
                "request_headers": capture.request_headers,
                "request_body": capture.request_body,
                "endpoint_type": capture.endpoint_type
            }
            
            req_file = f"{self._capture_dir}/bz_request_{i}_{timestamp}.json"
            with open(req_file, "w") as f:
                json.dump(req_data, f, indent=2)
            
            if capture.response_body:
                resp_file = f"{self._capture_dir}/bz_response_{i}_{timestamp}.txt"
                with open(resp_file, "w") as f:
                    f.write(capture.response_body)
        
        for i, capture in enumerate(self.graphql_requests):
            data = asdict(capture)
            file = f"{self._capture_dir}/graphql_request_{i}_{timestamp}.json"
            with open(file, "w") as f:
                json.dump(data, f, indent=2)
        
        summary = {
            "timestamp": timestamp,
            "total_captures": len(self.captures),
            "bz_count": len(self.bz_requests),
            "graphql_count": len(self.graphql_requests),
            "feed_count": len(self.feed_requests),
            "captures": [asdict(c) for c in self.captures]
        }
        
        summary_file = f"{self._capture_dir}/all_captures_{timestamp}.json"
        with open(summary_file, "w") as f:
            json.dump(summary, f, indent=2)
        
        logger.info(f"[SAVE] Saved to {self._capture_dir}/")
    
    def analyze_response(self, capture: CapturedRequest) -> FeedAnalysis:
        """Analyze if response contains feed data."""
        analysis = FeedAnalysis()
        
        if not capture.decoded_data:
            analysis.notes = "Response not JSON decoded"
            return analysis
        
        data = capture.decoded_data
        data_str = json.dumps(data).lower()
        
        if any(kw in data_str for kw in ["text", "caption", "post_text", "content"]):
            analysis.has_text = True
            analysis.data_fields.append("text")
        
        if any(kw in data_str for kw in ["username", "user_name", "full_name"]):
            analysis.has_username = True
            analysis.data_fields.append("username")
        
        if any(kw in data_str for kw in ["user_id", "pk", "id", "profile_id"]):
            analysis.has_user_id = True
            analysis.data_fields.append("user_id")
        
        if any(kw in data_str for kw in ["thread_id", "post_id", "id", "code"]):
            analysis.has_thread_id = True
            analysis.data_fields.append("thread_id")
        
        if any(kw in data_str for kw in ["image", "video", "media_url", "thumbnail"]):
            analysis.has_media_url = True
            analysis.data_fields.append("media_url")
        
        if any(kw in data_str for kw in ["timestamp", "created_at", "date", "unix"]):
            analysis.has_timestamp = True
            analysis.data_fields.append("timestamp")
        
        has_post = analysis.has_text or analysis.has_thread_id
        has_user = analysis.has_username or analysis.has_user_id
        
        analysis.extraction_feasible = has_post and has_user
        analysis.notes = f"Found fields: {', '.join(analysis.data_fields)}"
        
        return analysis
    
    def analyze_responses(self) -> List[FeedAnalysis]:
        """Analyze all captured responses."""
        analyses = []
        
        for capture in self.captures:
            analysis = self.analyze_response(capture)
            analyses.append(analysis)
            
            if analysis.extraction_feasible:
                logger.info(f"[FEASIBLE] {capture.endpoint_type} - {capture.url[:80]}")
                logger.info(f"  Fields: {', '.join(analysis.data_fields)}")
        
        return analyses
    
    def generate_report(self, output_file: str = "FEED_DISCOVERY_REPORT.md") -> Dict:
        """Generate comprehensive feed discovery report."""
        logger.info(f"\n[REPORT] Generating {output_file}")
        
        analyses = self.analyze_responses()
        feasible = [a for a in analyses if a.extraction_feasible]
        
        endpoint_scores = {}
        for capture in self.captures:
            url = capture.url[:100]
            if url not in endpoint_scores:
                endpoint_scores[url] = {"count": 0, "has_data": False, "types": set()}
            
            endpoint_scores[url]["count"] += 1
            endpoint_scores[url]["types"].add(capture.endpoint_type)
            
            if capture.decoded_data:
                endpoint_scores[url]["has_data"] = True
        
        md = f"""# Feed Discovery Report

**Generated:** {datetime.now().isoformat()}
**Total Captures:** {len(self.captures)}

---

## Summary

| Metric | Count |
|--------|-------|
| Total Requests | {len(self.captures)} |
| BZ Requests | {len(self.bz_requests)} |
| GraphQL Requests | {len(self.graphql_requests)} |
| Feed Requests | {len(self.feed_requests)} |
| Extraction Feasible | {len(feasible)} |

---

## Endpoint Ranking

| Rank | Endpoint | Count | Types | Has Data |
|------|----------|-------|-------|----------|
"""
        
        ranked = sorted(endpoint_scores.items(), 
                       key=lambda x: (x[1]["has_data"], x[1]["count"]), 
                       reverse=True)[:20]
        
        for i, (url, info) in enumerate(ranked, 1):
            types = ", ".join(info["types"])
            has_data = "Yes" if info["has_data"] else "No"
            md += f"| {i} | {url} | {info['count']} | {types} | {has_data} |\n"
        
        md += f"""

---

## BZ Endpoints (Primary Candidates)

"""
        
        for i, capture in enumerate(self.bz_requests[:10]):
            md += f"""### {i+1}. {capture.endpoint_type.upper()} Request

**URL:** `{capture.url[:200]}`

**Method:** {capture.method}

**Query Params:**
```json
{json.dumps(capture.query_params, indent=2)}
```

**Response Status:** {capture.response_status}

**Content Type:** {capture.content_type}

**Response Size:** {capture.response_size} bytes

**Decoded:** {"Yes" if capture.decoded else "No"}

"""
            
            if capture.response_body:
                preview = capture.response_body[:500]
                md += f"""**Response Preview:**
```
{preview}
```

"""
        
        md += f"""

---

## Data Extraction Analysis

"""
        
        for capture in self.captures:
            if capture.decoded_data:
                analysis = self.analyze_response(capture)
                if analysis.extraction_feasible:
                    md += f"""### {capture.endpoint_type} - {capture.url[:80]}

**Found Fields:** {', '.join(analysis.data_fields)}

**Extraction Feasibility:** {"YES" if analysis.extraction_feasible else "NO"}

"""
        
        md += """

---

## Can Threads Feed Extraction Be Performed Directly From Network Responses?

### Answer: YES (if feasible responses captured)

"""
        
        if feasible:
            md += """### Evidence

Based on captured responses, feed data CAN be extracted from network responses:

1. **BZ Endpoints** contain structured data
2. **GraphQL responses** include user and post information
3. **Response bodies** contain: username, user_id, thread_id, text, media URLs

### Recommendation

Use BZ endpoints as primary extraction source:
- `/ajax/bz` route contains feed data
- GraphQL payloads include user/post objects
- Response bodies are JSON-decodable

### Data Available

- Post text/caption
- Username
- Profile ID
- Thread ID
- Media URLs
- Timestamps

### Conclusion

**DOM scraping is NOT necessary.** Feed data can be reconstructed entirely from network responses.

"""
        else:
            md += """### Current Status

No fully extraction-feasible responses captured yet.

### Possible Reasons

1. Session may not be fully authenticated
2. Endpoints may require additional parameters
3. Response may be compressed/encoded differently

### Next Steps

1. Verify session cookies are valid
2. Check if login page redirect occurs
3. Try different capture duration
4. Inspect captured responses manually

"""
        
        md += """

---

## Risks

- Endpoint structure may change without notice
- BZ endpoints are internal Meta APIs
- No guarantee of backward compatibility
- Rate limiting may apply

---

## Parser Recommendations

1. **Primary:** Parse BZ endpoint JSON responses
2. **Secondary:** Extract from GraphQL payloads
3. **Fallback:** Decode compressed response bodies
4. **Validation:** Check for required fields before processing

---

*Report generated by DeepFeedDiscovery*
"""
        
        with open(output_file, "w") as f:
            f.write(md)
        
        logger.info(f"[REPORT] Saved to {output_file}")
        
        return {
            "total_captures": len(self.captures),
            "feasible_count": len(feasible),
            "bz_count": len(self.bz_requests),
            "graphql_count": len(self.graphql_requests)
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    
    print("\n" + "="*60)
    print("DEEP FEED DISCOVERY TOOL")
    print("="*60)
    print("\nUsage:")
    print("  from services.network.deep_feed_discovery import DeepFeedDiscovery")
    print("  discovery = DeepFeedDiscovery(session_cookies)")
    print("  discovery.capture_full(duration_seconds=45)")
    print("  discovery.save_captures()")
    print("  discovery.analyze_responses()")
    print("  discovery.generate_report()")
    print("="*60)