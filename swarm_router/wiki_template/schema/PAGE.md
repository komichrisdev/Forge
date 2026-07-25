# Canonical page schema 1.0

Pages are UTF-8 Markdown with one YAML front matter mapping between exact `---`
delimiters. Unknown fields, YAML aliases, duplicate keys and files over
1,000,000 bytes are invalid.

Required fields, in serialization order:

| Field | Type and rule |
| --- | --- |
| `id` | Globally unique lowercase ASCII slug; filename is exactly `<id>.md` |
| `title` | Non-empty Unicode string |
| `project` | Non-empty lowercase ASCII slug |
| `aliases` | List of unique non-empty strings; may be empty |
| `jira_keys` | Unique canonical uppercase keys such as `ORBIT-7`; may be empty |
| `source_refs` | Non-empty unique list of existing source IDs |
| `source_updated_at` | UTC ISO 8601 seconds, `YYYY-MM-DDTHH:MM:SSZ` |
| `ingested_at` | UTC ISO 8601 seconds, `YYYY-MM-DDTHH:MM:SSZ` |
| `verification_status` | `unverified`, `verified`, `conflicted`, or `superseded` |
| `confidence` | Integer from 0 through 100; booleans and text are invalid |
| `tags` | Non-empty unique list of lowercase ASCII slugs |
| `supersedes` | Unique page IDs; may be empty; cycles are invalid |

The body must be non-empty. Material claims cite declared sources using
`[[source:<source-id>]]`; every declared source must appear in the body and
every body source must be declared. Internal links use `[[page:<page-id>]]`.
Relative or absolute filesystem paths are not page identities.

A `conflicted` page requires `## Conflicts` and must preserve all competing
claims with their source citations. A `superseded` page remains stored but must
be named by a replacement page's `supersedes` list. The replacement does not
erase history.
