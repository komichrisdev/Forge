# MCP build reproducibility

Verified 2026-07-25 for Swarm Platform V2 Phase 0.3.

## Frozen clean-build procedure

Two newly created `/tmp` copies contained the Node application source,
manifests and lockfile, while excluding `node_modules`, `build` and `dist`.
Each copy independently ran:

```bash
npm ci --ignore-audit --ignore-fund
npm run build
```

Both installs resolved 329 packages from the committed lockfile without
changing `package-lock.json`. Both builds compiled 133 widget modules and
completed in approximately 3.36 seconds; Vite itself reported 0.89–0.92
seconds.

Verified toolchain:

- Debian host Node 20.19.2
- npm 9.2.0
- TypeScript 5.8.3
- Vite 7.3.6
- MCP SDK 1.29.0
- MCP Apps SDK 1.7.4

The pinned optional MCP Inspector declares Node 22.7.5 or newer and therefore
emits an engine warning on Node 20. The install also reports 15 vulnerabilities
already represented by the frozen dependency set (6 moderate and 9 high).
Dependencies were not updated in this phase.

## Results

The two clean output trees were byte-for-byte identical. They were also
byte-for-byte identical to all seven committed generated files.

| Generated file | SHA-256 |
| --- | --- |
| `build/data.js` | `e0f46a03584f3045d63a951c31b14b4f3c081e9555b03198419b2fdc7ddb9e08` |
| `build/data.js.map` | `bf41af56d92328b8b8a9fe0489ed3e2249c18622133bc2e677e38eeea8c4db77` |
| `build/main.js` | `63bf787d72aa64c24268cd376a526b58fd7f9db97dd8b53c69df3d2bcc5400e9` |
| `build/main.js.map` | `67950a1eea1e9be06f9d50b3826d0a447d1049cfc5688f64320d37f5998b4e3d` |
| `build/server.js` | `d7f507016f41ac7a945f6c87c61005a0a7fc90dd343583596263a906f3cc032f` |
| `build/server.js.map` | `9a1bbf8c3825ace4fa01eac57071298fdcbf8a630c9ada498db0c035b9e23a38` |
| `dist/widget.html` | `177970d6685898847c4a38c16297ee62806fee2858be8dbb929aa2b40b06e9c1` |

The combined sorted generated-tree digest used by the independent review was
`91ef6814f49a94e8fc5bd2aed454f4849430f54b3b3e179c7694b9a399bb4b4f`.

## Automated no-drift check

```bash
npm run check:mcp-build
```

The command compiles the server and widget into a new temporary directory,
compares the candidate to committed `build/` and `dist/`, prints differing file
names, and never overwrites committed output. Files are compared byte-for-byte
except for the `sources` path in TypeScript source maps. That one field is
normalized to `../src/<filename>` because an isolated `--outDir` legitimately
changes only the path from the map to the unchanged source. Source-map
`mappings` and every other field remain exact.

Explicit regeneration is separate:

```bash
npm run build:mcp
```

Generated files are committed because the deployed service runs
`build/main.js`. TypeScript/widget sources are authoritative; generated files
must not be edited manually.
