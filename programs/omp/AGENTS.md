# Cloudflare

When interacting with Cloudflare, use the `cf` CLI unless the project has a
Wrangler configuration file (`wrangler.jsonc` or `wrangler.toml`). `cf` is
installed globally; inside a project that depends on `cf`, the global command
delegates to the project's copy.

## Finding and inspecting commands

- Never discover commands by chaining nested `--help`. Describe the task:
  `cf cli search "<describe the task>"` (one quoted argument; runs locally,
  needs no credentials) returns up to five matching commands as JSON.
- Inspect the chosen generated command's API request with
  `cf schema <command>` (e.g. `cf schema d1 create`). For a command's own
  arguments and options, run that command with `--help`.
- Preview every mutation with `--dry-run` (prints the request as JSON, needs
  no credentials) before running it for real.

## Output

- stdout is the JSON result only when piped; parse it or filter with `jq`.
  Messages and errors go to stderr.
- Lists return one page; paging options differ per command — check `--help`.

## Safety

- Never pass `--force` to a destructive command without explicit user
  approval; on some commands it is also an API parameter with wider effect
  (e.g. `cf workers delete --force`).
- In a non-interactive session, a destructive command without `--force`
  prints `Aborted.` to stderr and exits 0. A zero exit does not mean the
  resource was deleted — check stderr.
- `--local` only works for KV keys, D1 (`cf d1 raw`, `cf d1 migrations
  list`/`apply`) and R2 objects. Commands with no local equivalent error
  instead of falling through to production.
- Authentication is the user's step: `cf auth login` opens a browser. Never
  run it for the user. Unattended contexts use `CLOUDFLARE_API_TOKEN`, which
  takes priority over any stored login.

## Wrangler projects

- Never run `cf dev`, `cf build` or `cf deploy` in a project that has a
  Wrangler configuration file but no `cloudflare.config.ts`. Migrate first:
  `cf migrate --dry-run`, then `cf migrate`, then resolve every
  `TODO(@cloudflare)` comment in the generated `cloudflare.config.ts`,
  asking the user about any choice that cannot be made from the code.
