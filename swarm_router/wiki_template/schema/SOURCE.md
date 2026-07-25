# Source manifest schema 1.0

Each UTF-8 YAML manifest is an immutable, exact mapping:

| Field | Type and rule |
| --- | --- |
| `source_id` | Globally unique lowercase ASCII slug; filename `<source_id>.yaml` |
| `version` | Positive integer |
| `source_type` | `jira`, `drive`, `document`, `web`, `repository`, `decision`, `runbook`, or `other` |
| `title` | Non-empty Unicode string |
| `locator` | Credential-free HTTPS URL, `external:<slug>`, or safe `sources/originals/...` path |
| `source_updated_at` | UTC ISO 8601 seconds |
| `ingested_at` | UTC ISO 8601 seconds |
| `checksum` | 64 lowercase hexadecimal SHA-256 characters |
| `checksum_algorithm` | Exactly `sha256` |
| `media_type` | Lowercase `type/subtype` |
| `authority` | `authoritative`, `approved-decision`, `supporting`, or `generated` |
| `supersedes` | Prior source IDs; may be empty; the version must increase |
| `notes` | String; may be empty |

URL user information and credential-like query keys are forbidden. Relative
locators cannot be absolute, contain `..`, or leave `sources/originals/`.
Existing manifests cannot be overwritten through the storage API. Changed
source bytes require a new source/version manifest.

Precedence is authority-aware: original authoritative material outranks a
generated summary; an explicit approved decision remains controlling until
explicitly superseded; newer timestamps alone do not choose a winner.
