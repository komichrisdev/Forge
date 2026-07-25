# Ingestion boundary

Phase 1.0 provides storage only. It does not fetch Jira, Drive, web pages,
documents, email, uploads, OCR, or model output.

A future ingestion operation must:

1. preserve or checksum the authoritative representation;
2. append a new immutable source manifest;
3. create a proposal by default;
4. cite sources for material claims;
5. keep contradictions visible;
6. require explicit approval before changing canonical pages.

Agents must not place credentials, confidential exports, or unreviewed private
content in this repository.
