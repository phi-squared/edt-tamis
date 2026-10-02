# Security policy

## Reporting a vulnerability

Please report security problems **privately**, not as a public issue: on the repository page open **Security → Report a vulnerability** (GitHub private vulnerability reporting). If that button is not there, open a normal issue that says only "security contact please" and no details, and I will reply with a private channel.

I am a student maintaining this in my spare time. I read reports as soon as I can, usually within a week or two, and will tell you what I decided. There is no bug bounty.

## What is in scope

`tamis` downloads calendar files from servers you configure, merges them, and either writes a file, serves it over HTTP (`tamis serve`) or pushes it to a git repo (`tamis publish`). Interesting reports include:

- a hostile or broken calendar server making `tamis` hang, crash, use excessive memory, read or write files it should not, or contact internal addresses
- the HTTP server leaking the calendar to someone who should not have it, or being taken down by a single client
- `publish` writing outside its target folder, or publishing something other than the calendar
- secrets (server password, URL tokens) ending up in logs, the generated calendar, child processes or a repository
- generated service files (launchd, systemd, Task Scheduler) that can be abused through unusual paths or user names

## Out of scope

- Things that require an attacker who can already edit your `config.toml` (it can run commands by design: `alert_command`)
- Exposing `tamis serve` to the internet without a password or secret path and then finding the calendar is readable
- The privacy of your timetable *selection* when you choose `publish` to a public repo: that is what public means
- Findings in the university's own systems (ADE); report those to the university

## Supported versions

Only the latest release. Fixes are released as a new version and noted in [CHANGELOG.md](CHANGELOG.md).
