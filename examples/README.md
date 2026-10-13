# Examples

Example settings for a Mac and a server. These profiles contain
fictitious hostnames, identities and paths:

- `macbook/` — `example-laptop`, marked `exampleOnly = true`. It
  contains settings only. Secret declarations are excluded, and both system
  and Home Manager activation refuse this profile.
- `server/` — `example-server`, with example domains (`example.net`),
  a documentation-range LAN address (`192.0.2.10`), public image pins
  and a public CA certificate.

The profiles support evaluation and build checks without private state.
The commands that exercise them are in
[Daily use](../docs/daily-use.md#checking-the-examples).

The profiles conform to `settings/profile-schema.json`. Evaluation and
build commands do not activate either example configuration.
Production Mac profiles are not marked `exampleOnly` and still require
the two encrypted files in a private, rendered input outside Git.
