# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- `publish` (git method) no longer mistakes its own file for "other staged changes" when the file name has non-ASCII characters on Windows; git's output is read as UTF-8 and names are no longer compared as text.

### Changed
- CI runs the full matrix again (Linux, macOS and Windows, Python 3.10–3.13) now that the repository is public.

## [0.3.0] - 2026-10-02

First public release. Preparation for sharing the project: three rounds of independent security review and the fixes that came out of them.

### Security
- Downloads: 10 MB size cap, one 120 s deadline for connect, headers, redirects and body, at most 5 redirects, http(s) only, no https→http, no redirect to private or local addresses (including unusual spellings such as `127.1` or `%31%32%37.0.0.1`), cap of 5000 events per source.
- A truncated or implausible answer (short body, missing `END:VCALENDAR`, no readable event, dates outside 1990–2100) never replaces the last good copy.
- Server: idle and total connection time limits, a cap on connections and per client, a cap on header size, Host-header check against DNS rebinding (`allowed_hosts`), slow-down after repeated wrong passwords, no `Server:` header, secret path and control characters kept out of logs, password / secret path / allowed hosts reloaded at each refresh without ever being left half-applied.
- `publish`: file name restricted to a plain name, destination symlinks refused, git called with literal pathspecs, timeouts and a clean environment, refuses to publish when other changes are staged.
- All state and output writes go through one safe writer (unpredictable temporary name, no symlink following, fsync, atomic replace); data folder is private.
- Service files for launchd, systemd and Task Scheduler are quoted correctly.
- Text from servers is made terminal-safe before it is shown; URLs with credentials are redacted from errors; warning events in the feed never contain URL query strings or local folders.
- Config values are validated with clear error messages instead of tracebacks.
- CI: lint and security job (ruff with security rules, bandit, pip-audit), pinned tools, Dependabot, least-privilege workflow permissions.

### Changed
- `refresh_minutes` below 10 is refused.
- A `config.toml` in the current folder triggers a warning when it can run commands or publish.
- README: quick start, install with `uv`, plain HTTP over Tailscale as the private default, related projects.

### Added
- `SECURITY.md`, this changelog, issue template.
