# Backup and restore

`owui-swarm wiki backup` creates a unique mode-0700 directory beneath
`~/backups/swarm-wiki/` by default. It contains:

- `wiki.bundle` with all Git refs;
- `working-tree.tar.gz` with current tracked working files;
- `metadata.json`, repository status, manifest, and SHA-256 list.

The manifest identifies dirty state. Untracked files, `.locks/`, `.tmp/`,
generated indexes, caches, model files, and secrets are excluded.

`owui-swarm wiki restore-verify BACKUP` checks every hash and the Git bundle,
clones into a newly created temporary directory, overlays the tracked working
tree, runs complete validation, and confirms the expected commit and page/source
counts. It never overwrites `/srv/swarm-wiki` and never starts a service.
