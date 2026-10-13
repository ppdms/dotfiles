# Codex sandbox setting (Mac only)

One small preference, stated in full.

The settings may contain:

```json
{ "profiles": { "darwin": { "applications": { "codex": { "sandboxMode": "workspace-write" } } } } }
```

With that field present, each rebuild sets the top-level
`sandbox_mode` in Codex's `config.toml` to the given value —
`read-only`, `workspace-write`, or `danger-full-access` — and
preserves everything else in the file. The helper is
`programs/codex/set-sandbox.py`.

With the field `null` or absent, the file remains completely
untouched, including a hand-set `danger-full-access`. No default
exists.

`danger-full-access` disables Codex's sandbox: commands it runs carry
full user permissions. The setting manages no credentials.
