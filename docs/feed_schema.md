# ⚠️ DEPRECATED: Threads Feed Schema

> **STATUS: INFERRED - NOT OBSERVED**
> 
> This document contains field mappings that have NOT been validated against actual captured data.
> It is based on external research and may not reflect the actual Threads API structure.

---

## Original Purpose

This document was intended to describe the data structure of Threads feed responses from network capture.

## Current Status

| Status | Value |
|--------|-------|
| Data Source | INFERRED |
| JSON Paths | NOT VERIFIED |
| Field Names | NOT OBSERVED |
| Structure | EXTERNAL RESEARCH ONLY |

## Evidence Report

See `EVIDENCE_REPORT.md` for details on why this data is INFERRED.

**Key Finding:** Without a valid authenticated session, no feed data can be captured from Threads.net.

---

## Why This Document Is Inferred

1. **No session cookies** - THREADS_SESSION_ID is empty
2. **No API responses captured** - Only static CDN assets observed
3. **No feed data** - No user, thread, text, or media data observed
4. **Schema based on** - External Meta GraphQL research

## What Would Make This OBSERVED

1. Valid authenticated session cookies
2. Captured BZ/GraphQL responses from Threads.net
3. Actual JSON data with verified field paths
4. Real example values from captured responses

---

## Placeholder for Future Observed Schema

When valid capture is achieved, this document should be replaced with:

```
### User Object (OBSERVED)

| Field | JSON Path | Example Value |
|-------|-----------|---------------|
| username | data.user.username | "example_user" |
| user_id | data.user.pk | 123456789 |
```

---

## Next Steps

1. Obtain valid session cookies
2. Run DeepFeedDiscovery with valid session
3. Replace this document with OBSERVED schema

---

*Last updated: 2026-06-11*
*Status: INFERRED - Awaiting valid session to validate*
