# Sources

Manifests are immutable provenance records. A changed external source receives
a new source ID/version and may point to the prior manifest through
`supersedes`; existing manifests and original files are never silently edited.

Locators may be credential-free HTTPS URLs, `external:<slug>` identifiers, or
safe relative paths beneath `sources/originals/`. Checksums cover the referenced
bytes or the representation documented in the manifest.

Only synthetic fixtures are stored in this Phase 1.0 foundation.
