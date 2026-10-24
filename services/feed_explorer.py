"""
FeedExplorer - Threads Feed Discovery

Discovers API endpoints and data structures used by Threads web client.
Logs all found URLs, GraphQL queries, and bootstrap data.
Saves findings to feed_discovery.json.

DO NOT implement scraping - discovery only.
"""

import json
import logging
import os
import re
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

from curl_cffi.requests import Session as CurlSession

logger = logging.getLogger(__name__)


@dataclass
class FeedDiscovery:
    """Results from feed exploration."""
    graphql_endpoints: List[str] = field(default_factory=list)
    graphql_doc_ids: List[str] = field(default_factory=list)
    api_endpoints: List[str] = field(default_factory=list)
    bootstrap_data: List[Dict[str, Any]] = field(default_factory=list)
    relay_queries: List[Dict[str, str]] = field(default_factory=list)
    raw_findings: Dict[str, Any] = field(default_factory=dict)


class FeedExplorer:
    """
    Discovers Threads feed API endpoints.
    
    Usage:
        explorer = FeedExplorer(session)
        discovery = explorer.explore()
        explorer.save_discovery("feed_discovery.json")
    """
    
    THREADS_HOME_URL = "https://www.threads.net/"
    
    def __init__(self, session: CurlSession):
        """
        Initialize FeedExplorer.
        
        Args:
            session: Authenticated CurlSession from SessionManager
        """
        self.session = session
        self.discovery = FeedDiscovery()
    
    def explore(self) -> FeedDiscovery:
        """
        Explore Threads homepage for feed-related endpoints.
        
        Returns:
            FeedDiscovery with all findings
        """
        logger.info("="*60)
        logger.info("FEED EXPLORER - Starting Discovery")
        logger.info("="*60)
        
        # Fetch Threads homepage
        logger.info("[EXPLORE] Fetching https://www.threads.net/")
        try:
            response = self.session.get(
                self.THREADS_HOME_URL,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                }
            )
            html_content = response.text
            logger.info(f"[EXPLORE] Response: {response.status_code} ({len(html_content)} bytes)")
        except Exception as e:
            logger.error(f"[EXPLORE] Failed to fetch homepage: {e}")
            return self.discovery
        
        # Extract all findings
        self._extract_graphql_endpoints(html_content)
        self._extract_graphql_doc_ids(html_content)
        self._extract_api_endpoints(html_content)
        self._extract_bootstrap_data(html_content)
        self._extract_relay_queries(html_content)
        
        # Log summary
        self._log_findings()
        
        return self.discovery
    
    def _extract_graphql_endpoints(self, html: str):
        """Extract GraphQL endpoint URLs."""
        logger.info("\n[EXTRACT] Looking for GraphQL endpoints...")
        
        # Pattern for GraphQL endpoints
        patterns = [
            r'https://www\.threads\.net/api/graphql',
            r'https://graph\.instagram\.com/graphql',
            r'https://[a-z]+\.threads\.net/api/[a-z]+',
        ]
        
        endpoints = set()
        for pattern in patterns:
            matches = re.findall(pattern, html)
            endpoints.update(matches)
        
        self.discovery.graphql_endpoints = sorted(list(endpoints))
        
        if endpoints:
            logger.info(f"[FOUND] GraphQL endpoints: {len(endpoints)}")
            for ep in self.discovery.graphql_endpoints:
                logger.info(f"   - {ep}")
        else:
            logger.info("[NOT FOUND] No GraphQL endpoints")
    
    def _extract_graphql_doc_ids(self, html: str):
        """Extract GraphQL document/operation IDs."""
        logger.info("\n[EXTRACT] Looking for GraphQL doc IDs...")
        
        # Pattern for doc_id or operationName
        patterns = [
            r'"doc_id"\s*:\s*"(\d+)"',
            r'"operationName"\s*:\s*"([^"]+)"',
            r'"queryId"\s*:\s*"([^"]+)"',
            r'"hash"\s*:\s*"([^"]+)"',
        ]
        
        doc_ids = set()
        for pattern in patterns:
            matches = re.findall(pattern, html)
            doc_ids.update(matches)
        
        self.discovery.graphql_doc_ids = sorted(list(doc_ids))
        
        if doc_ids:
            logger.info(f"[FOUND] GraphQL doc IDs: {len(doc_ids)}")
            for doc_id in self.discovery.graphql_doc_ids[:20]:  # Limit to 20
                logger.info(f"   - {doc_id}")
            if len(doc_ids) > 20:
                logger.info(f"   ... and {len(doc_ids) - 20} more")
        else:
            logger.info("[NOT FOUND] No GraphQL doc IDs")
    
    def _extract_api_endpoints(self, html: str):
        """Extract API endpoint URLs."""
        logger.info("\n[EXTRACT] Looking for API endpoints...")
        
        # Pattern for API endpoints
        patterns = [
            r'https://www\.threads\.net/api/[a-zA-Z0-9/_-]+',
            r'https://i\.instagram\.com/api/[a-zA-Z0-9/_-]+',
            r'"url"\s*:\s*"([^"]*(?:feed|timeline|home|posts)[^"]*)"',
            r'"endpoint"\s*:\s*"([^"]+)"',
        ]
        
        endpoints = set()
        for pattern in patterns:
            matches = re.findall(pattern, html, re.IGNORECASE)
            endpoints.update(matches)
        
        self.discovery.api_endpoints = sorted(list(endpoints))
        
        if endpoints:
            logger.info(f"[FOUND] API endpoints: {len(endpoints)}")
            for ep in self.discovery.api_endpoints[:15]:
                logger.info(f"   - {ep}")
            if len(endpoints) > 15:
                logger.info(f"   ... and {len(endpoints) - 15} more")
        else:
            logger.info("[NOT FOUND] No API endpoints")
    
    def _extract_bootstrap_data(self, html: str):
        """Extract JSON bootstrap data blocks."""
        logger.info("\n[EXTRACT] Looking for bootstrap JSON data...")
        
        # Pattern for JSON in script tags
        patterns = [
            r'<script[^>]*>window\._sharedData\s*=\s*({.*?});</script>',
            r'<script[^>]*>window\.__initialData\s*=\s*({.*?});</script>',
            r'<script[^>]*>window\.__bootstrapped\s*=\s*({.*?});</script>',
            r'"config"\s*:\s*\{[^}]*"api[^"]*"\s*:\s*"([^"]+)"',
        ]
        
        bootstrap_blocks = []
        for pattern in patterns:
            matches = re.findall(pattern, html, re.DOTALL)
            for match in matches:
                try:
                    if match.startswith('{'):
                        data = json.loads(match)
                        bootstrap_blocks.append(data)
                    else:
                        bootstrap_blocks.append({"raw": match[:200]})
                except json.JSONDecodeError:
                    bootstrap_blocks.append({"raw": str(match)[:200]})
        
        # Also look for JSON-LD or embedded JSON
        json_patterns = [
            r'<script[^>]*type="application/json"[^>]*>(.*?)</script>',
            r'"feed[^"]*"\s*:\s*\[',
        ]
        
        for pattern in json_patterns:
            matches = re.findall(pattern, html, re.DOTALL)
            for match in matches:
                try:
                    data = json.loads(match)
                    bootstrap_blocks.append(data)
                except:
                    pass
        
        self.discovery.bootstrap_data = bootstrap_blocks
        
        if bootstrap_blocks:
            logger.info(f"[FOUND] Bootstrap data blocks: {len(bootstrap_blocks)}")
            for i, block in enumerate(bootstrap_blocks[:5]):
                logger.info(f"   Block {i+1}: {list(block.keys()) if isinstance(block, dict) else 'array'}")
        else:
            logger.info("[NOT FOUND] No bootstrap data")
    
    def _extract_relay_queries(self, html: str):
        """Extract Relay/GraphQL query patterns."""
        logger.info("\n[EXTRACT] Looking for Relay queries...")
        
        # Pattern for Relay queries
        patterns = [
            r'"id"\s*:\s*"[^"]*(?:Timeline|Feed|Home|UserPosts)[^"]*"',
            r'"cacheID"\s*:\s*"([^"]+)"',
            r'"queryHash"\s*:\s*"([^"]+)"',
            r'"relayStyleMutation"\s*:\s*"(true|false)"',
        ]
        
        relay_queries = []
        for pattern in patterns:
            matches = re.findall(pattern, html, re.IGNORECASE)
            for match in matches:
                relay_queries.append({
                    "pattern": pattern[:50],
                    "match": match[:100]
                })
        
        self.discovery.relay_queries = relay_queries
        
        if relay_queries:
            logger.info(f"[FOUND] Relay query patterns: {len(relay_queries)}")
        else:
            logger.info("[NOT FOUND] No Relay query patterns")
        
        # Look for specific feed-related identifiers
        feed_patterns = [
            (r'timelineFeed', 'TimelineFeed'),
            (r'userFeed', 'UserFeed'),
            (r' reels ', 'Reels'),
            (r'threadsFeed', 'ThreadsFeed'),
            (r'homeFeed', 'HomeFeed'),
        ]
        
        logger.info("\n[EXTRACT] Feed-specific patterns:")
        for pattern, name in feed_patterns:
            matches = re.findall(pattern, html, re.IGNORECASE)
            logger.info(f"   {name}: {len(matches)} occurrences")
    
    def _log_findings(self):
        """Log summary of all findings."""
        logger.info("\n" + "="*60)
        logger.info("FEED DISCOVERY SUMMARY")
        logger.info("="*60)
        logger.info(f"GraphQL endpoints: {len(self.discovery.graphql_endpoints)}")
        logger.info(f"GraphQL doc IDs: {len(self.discovery.graphql_doc_ids)}")
        logger.info(f"API endpoints: {len(self.discovery.api_endpoints)}")
        logger.info(f"Bootstrap blocks: {len(self.discovery.bootstrap_data)}")
        logger.info(f"Relay queries: {len(self.discovery.relay_queries)}")
        logger.info("="*60)
    
    def save_discovery(self, filename: str = "feed_discovery.json"):
        """Save discovery results to JSON file."""
        logger.info(f"\n[SAVE] Saving discovery to {filename}")
        
        output = {
            "graphql_endpoints": self.discovery.graphql_endpoints,
            "graphql_doc_ids": self.discovery.graphql_doc_ids,
            "api_endpoints": self.discovery.api_endpoints,
            "bootstrap_data_count": len(self.discovery.bootstrap_data),
            "relay_queries": self.discovery.relay_queries,
            "discovery_timestamp": self._get_timestamp(),
        }
        
        # Add sample bootstrap data (truncated)
        if self.discovery.bootstrap_data:
            output["bootstrap_sample"] = self.discovery.bootstrap_data[0] if self.discovery.bootstrap_data else {}
        
        try:
            with open(filename, "w") as f:
                json.dump(output, f, indent=2)
            logger.info(f"[SAVE] Discovery saved to {filename}")
        except Exception as e:
            logger.error(f"[SAVE] Failed to save: {e}")
    
    def _get_timestamp(self) -> str:
        """Get current timestamp."""
        from datetime import datetime
        return datetime.now().isoformat()
    
    def get_discovery(self) -> FeedDiscovery:
        """Get the discovery results."""
        return self.discovery


if __name__ == "__main__":
    # Test with mock session
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    
    print("\n" + "="*60)
    print("FEED EXPLORER - Standalone Test")
    print("="*60)
    print("Use FeedExplorer with authenticated session from SessionManager")
    print("="*60)
