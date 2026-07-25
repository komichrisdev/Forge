# Retrieval boundary

Phase 1.0 supports exact page retrieval by validated page ID and deterministic
metadata listing. It performs no keyword search, FTS, embedding, ranking, fuzzy
matching, or model retrieval.

Consumers use `[[page:<page-id>]]` as stable internal links and must inspect
source references and verification status before relying on a claim.
