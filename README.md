# edt-tamis — one calendar with only your courses

[![tests](https://github.com/phi-squared/edt-tamis/actions/workflows/tests.yml/badge.svg)](https://github.com/phi-squared/edt-tamis/actions/workflows/tests.yml)
[![licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)

ADE (the *emploi du temps* system at Unistra and many other French universities) exports timetables per **group**, not per course. 
If you take courses from several programmes, you end up with several noisy calendars. 
`tamis` downloads those exports, keeps only the courses you list, greys out clashes, never hides an exam, and gives you **one** iCalendar feed that updates itself.

- command line only, no GUI, no account, no telemetry, no third-party code
- everything personal lives in one TOML config file, outside the repository
- one global **observation period** for every source; the `nbWeeks=4` in ADE export links is replaced
- two ways to reach your devices: **private** (`serve`, e.g. behind Tailscale) or **public** (`publish` to a git repo, gist or folder)

*An independent student project. It is not affiliated with, endorsed by or supported by the University of Strasbourg or the vendor of ADE.* It only reads the anonymous calendar exports that a university chooses to publish, the same way a browser or a calendar app would.

## Quick start

1. Install it (no admin rights needed): `uv tool install git+https://github.com/phi-squared/edt-tamis@v0.3.1` — or see [1. Install and configure](#1-install-and-configure) for pipx.
2. `tamis init` creates your config and prints where it is. Open it and fill in your ADE resource ids and your courses ([Finding your ADE ids](#finding-your-ade-ids), [Writing course rules](#writing-course-rules)).
3. `tamis status` downloads every source once and tells you whether it works; `tamis report` shows what was kept, greyed out and what clashes.
4. Get the calendar onto your devices: `tamis build -o timetable.ics` for a one-off file, or `tamis serve` for a feed that updates itself ([3. Private: `serve`](#3-private-serve-recommended)).

If something looks wrong, `tamis titles` shows every event title and which course it matched.

## Related projects

Simpler tools exist that merge several iCalendar feeds (for example *ics-fusion* and *uniCal*). `tamis` is built around ADE's per-group exports: it keeps only the courses you list, greys out clashes, never hides an exam, serves the last good copy when ADE is down, and puts a warning in the calendar when updates stop.

---

## Requirements

| | |
|---|---|
| Python | **3.10 or newer** (3.11+ needs nothing else; 3.10 needs `tomli`, installed automatically by `pipx install .`) |
| macOS | 12 or newer. Apple's `/usr/bin/python3` is too old: `brew install python` |
| Linux | any distribution with Python ≥ 3.10; `systemd` for the background service |
| Windows | 10 or 11, Python from python.org or the Microsoft Store. **Needs `tzdata`** (Windows has no time-zone database): `pip install .` installs it, or `python -m pip install tzdata` |
| optional | `git` for `publish` with method `git`; Tailscale for the private multi-device setup |

The test suite runs on Linux, macOS and Windows with Python 3.10–3.13 on every pull request and push to main (`.github/workflows/tests.yml`). To check a machine yourself: `python -m unittest discover -s tests`.

---

## 1. Install and configure

```sh
git clone <this repo> ~/edt-tamis
cd ~/edt-tamis

# either: install the `tamis` command in its own environment
uv tool install .         # no admin rights needed; get uv: https://docs.astral.sh/uv/ (or `brew install uv`)
# or, from a fresh machine without cloning:  uv tool install git+https://github.com/phi-squared/edt-tamis@v0.3.1
#   upgrade later: the same command with the newer tag and `--force`
pipx install .            # get pipx: `brew install pipx` / `sudo apt install pipx` / `py -m pip install --user pipx`

# or: run it in place without installing (Python 3.11+; Windows first needs `py -m pip install tzdata`)
python3 -m tamis --version  # then write `python3 -m tamis` wherever this README says `tamis`

tamis init                  # creates your config (the path is printed)
```

On Windows use `py` instead of `python3`, and `%USERPROFILE%\edt-tamis` instead of
`~/edt-tamis`. 
(Plain `pip install` into Homebrew's or Debian's Python is refused by design — that's what pipx is for.) 
After pulling a new version: `uv tool install --force .` (or `pipx install --force .`), then `tamis service --install` again.

A `config.toml` in the folder you run `tamis` from takes precedence over the per-user one, and a config can run commands (`alert_command`) and publish files: never run `tamis` in a folder with a config you have not read. The config goes to `~/.config/edt-tamis/config.toml` (`%APPDATA%\edt-tamis\config.toml` on Windows). 
Every option is explained inside. Then:

```sh
tamis urls       # the exact ADE URLs, with your period filled in
tamis status     # downloads every source now: event count, last good download, errors
tamis titles     # every event title in your sources, and which course it maps to
tamis report     # per course: sessions kept / greyed out, exams, clashes
tamis build -o timetable.ics   # a one-off file, if that's all you want
```

`tamis status` is also the test of whether your ADE export is **really public**: it sends no browser cookies, so if it works from a terminal, it works without your university login. 
If a source needs a signed-in session you'll see `got a login page instead of a calendar`. 
Run it on the machine that will do the fetching (some universities also restrict ADE to their own network).

Check once that the events reach the end of your period (`report` lists January exams, for instance). 
If they stop after four weeks, your university's ADE ignores `firstDate` / `lastDate` — please open an issue.

### Finding your ADE ids

- **Easiest:** do the export you normally do on ADE, copy the link, and paste it as `url = "..."` in a `[[sources]]` block. The time window in it is replaced by `[period]`.
- **Tidier:** the link contains `resources=<id>` and `projectId=<n>`. Put `project_id` in `[settings]` once and list ids per source: `resources = [12345, 12346]`. In the ADE tree, hovering a group shows `javascript:check(<id>, …)` — the same id. TD subgroups usually have their own id, which is how you get "only my TD group".
- `project_id` changes every academic year. If everything returns 0 events in September, that's why.

### Writing course rules

`match` is a regular expression tested against the event title, ignoring case and accents.
`titles --unmatched` shows what you have not covered yet. After a timetable revision run `report`: 
a course showing `<-- matches nothing` means ADE renamed it.

| option | effect |
|---|---|
| `priority` | when two kept events overlap, the lower one is greyed out (`TRANSPARENT`, `TENTATIVE`, `· ` prefix, "clashes with …" in the notes) |
| `attend = false` | grey out every session, keep the exams solid |
| `skip_slots = ["Fri 13:30-15:30"]` | grey out one weekly slot (English day names, local time) |
| `skip_match = ["\\btd\\b"]` | remove matching sessions of that course (never an exam) |
| `on_conflict = "drop"` | remove clash losers instead of greying them |
| `[[drop]]` | remove matching events everywhere (information meetings, seminars) |

Exams (`exam_pattern`) are never greyed out. 
Two exams at the same time are printed as `*** EXAM CLASHES ***` by `report` and logged by `serve` / `publish`.

Events keep ADE's UIDs and the output is byte-stable, so calendar apps update events in place (a room change just shows up) and an unchanged timetable costs clients nothing.

---

## 2. Private or public?

| | **private: `serve`** | **public: `publish`** |
|---|---|---|
| where the feed lives | on your own always-on machine | a GitHub repo/gist, or any folder a web server or sync service exposes |
| who can read it | only devices on your Tailscale network (or your own laptop) | anyone with the URL |
| subscribe once via iCloud / Google Calendar | no (their servers can't reach your tailnet) | yes |
| needs | an always-on machine + Tailscale on every device | `git` and a repo, or a folder |

The timetable data itself is public (ADE's anonymous export). 
What is personal is your *selection*: it says where you are, every week. 
Choose accordingly. 
Switching later is just running the other command — the config is the same.

---

## 3. Private: `serve` (recommended)

`tamis serve` re-downloads from ADE every `refresh_minutes` (default 120) and answers requests from memory at `http://127.0.0.1:8765/calendar.ics`, with `ETag` / `304 Not Modified` support. 
If ADE is down, the previous calendar keeps being served. 
Config edits are picked up at the next refresh, including a changed password (`auth_file`), `url_path` and `allowed_hosts`; `bind` and `port` need a restart.

### Run it in the background

```sh
tamis service              # prints the job for this OS: launchd (macOS), systemd (Linux), Task Scheduler (Windows)
tamis service --install    # writes it, then prints the one or two commands to activate it
```

The job file is generated from *this* machine's interpreter, package location and config path; nothing is hard-coded. 
After a major Python upgrade, run `tamis service --install` again.

- **macOS:** keep the toolkit and config outside `~/Documents`, `~/Desktop`, `~/Downloads` and iCloud Drive — macOS blocks background jobs from reading those. Logs: `~/Library/Logs/edt-tamis/`.
- **Linux:** `loginctl enable-linger $USER` keeps it running while you are logged out.
- **Windows:** the job starts at logon and restarts on failure; it runs windowless (`pythonw`). Logs: `%LOCALAPPDATA%\edt-tamis\Logs\`. Remove with `Unregister-ScheduledTask -TaskName "edt-tamis serve"`.

### Always-on machine + Tailscale

```
ADE ──https, every 2 h──> always-on machine: tamis serve (127.0.0.1 only)
                              │ tailscale serve (tailnet only; WireGuard-encrypted)
         ┌────────────────────┼─────────────────────┐
     MacBook              iPad / iPhone          Android
   Calendar.app           Calendar app       ICSx⁵ → any calendar app
```

On the always-on machine (Mac mini, Raspberry Pi, a Windows PC that stays on, …):

```sh
tamis status && tamis report                      # works without a browser login? config ok?
tamis service --install                         # + the printed commands
curl -s localhost:8765/calendar.ics | head    # BEGIN:VCALENDAR
tailscale serve --bg --http=80 8765           # plain HTTP on your tailnet only; persists across reboots
tailscale serve status                        # shows http://<machine>.<tailnet>.ts.net/ (flags differ slightly between Tailscale versions: see `tailscale serve --help`)
```

Plain HTTP is the default recommendation here: traffic between your devices is already encrypted by Tailscale (WireGuard), and the HTTPS variant would put your machine and tailnet name into the public Certificate Transparency logs. Without a password, add the machine name to `allowed_hosts` (see below) or `tamis serve` answers 421.

`tailscale serve` needs *MagicDNS* enabled in the Tailscale admin console (DNS page). 
If you prefer HTTPS (`tailscale serve --bg 8765` with *HTTPS certificates* enabled), know that certificate names are public (Certificate Transparency logs), so the **machine and tailnet names** become visible — pick a neutral machine name. 
The feed content stays private either way.

A Mac used as the server should stay awake and restart after power loss:
`sudo pmset -a sleep 0 womp 1 autorestart 1`. 
The job and the Tailscale app run in the user session, so the machine must stay **logged in** (a locked screen is fine). 
With FileVault on, a cold boot waits at the login screen — log in once after a power cut; `sudo fdesetup authrestart` for planned restarts.

### Subscribing

Feed URL: `https://<machine>.<tailnet>.ts.net/calendar.ics`. 
Tailscale must be connected when a device refreshes; if it isn't, that refresh fails quietly and the next one catches up.

- **Mac:** Calendar → *File → New Calendar Subscription…* → **Location: On My Mac** (not iCloud), auto-refresh every hour.
- **iPad / iPhone:** *Settings → Apps → Calendar → Calendar Accounts → Add Account → Other → Add Subscribed Calendar*, on each device.
- **Android:** **ICSx⁵** (free on F-Droid, paid on Google Play, open source). The calendar is stored on the phone and appears in any calendar app (Fossify Calendar, Etar, or Google Calendar, which displays it without uploading it).
- **Windows:** Thunderbird (fetches locally, supports passwords). The *new* Outlook fetches subscriptions through Microsoft's servers, which can't reach your tailnet.

### Battery

A device refresh is one small HTTPS request, usually answered `304 Not Modified`.

- **iPad:** *Calendar Accounts → Fetch New Data* → the subscription on **Hourly**, or *Manually* (refreshes when you open Calendar). Avoid 15-minute fetching.
- **Android:** ICSx⁵ every 4–6 hours or daily; leave battery optimisation on.
- **Mac:** every hour or every day.

### Optional password

Useful if other people's devices are on your tailnet. 
Put `user:password` in a file, `chmod 600` it (on Windows keep it inside your user profile), set `auth_file = "auth.txt"` in `[settings]` (relative to the config), and enter the same credentials in each subscription. 
`serve` refuses to start if the file is readable by other users. Repeated wrong passwords from one address are answered more slowly (nobody is locked out). HTTP Basic sends the password unencrypted, so use it on a tailnet or behind a TLS proxy, not across the open internet.

### Requests from other host names (`allowed_hosts`)

Without a password, a server on `127.0.0.1` only answers requests addressed to `127.0.0.1`, `localhost` or `::1`. This blocks DNS-rebinding attacks, where a web page you visit tricks your browser into reading your local feed. If you reach the server through a proxy that forwards another host name, such as `tailscale serve`, list that name in `[settings]`, for example `allowed_hosts = ["mymac.tailnet-name.ts.net"]`. With a password set, the check is off.

Never use `tailscale funnel` for the private setup — that publishes the server to the internet.

---

## Knowing when it breaks

Nothing fails silently, but nothing breaks your calendar either:

| what goes wrong | what happens |
|---|---|
| ADE unreachable, returns an error, a login page, or suddenly an **empty** calendar | the last good copy is kept (no events disappear); the log and `tamis status` say why |
| …and that lasts longer than `stale_after_hours` (default 12) | an all-day event **"⚠ timetable NOT updated since …"** appears on *today* in every subscribed calendar, listing the failing sources; it disappears by itself once a download works again |
| same moment, if `alert_command` is set | the command runs once (and once more on recovery): a macOS notification on the server, or a push to your phone via ntfy — examples in the config |
| your **device** stops receiving the feed (server off, Tailscale disconnected) | the server can't tell you that, so set `watchdog_days = 3`: the feed carries an event that is always three days ahead. While updates arrive it keeps moving; if they stop, it stays put and reaches *today* — on the device that is out of date |

Logs: `~/Library/Logs/edt-tamis/serve.log` (macOS), `journalctl --user -u edt-tamis-serve` (Linux), `%LOCALAPPDATA%\edt-tamis\Logs\` (Windows). 
Every refresh logs one line, e.g. `142 events  !! using old copy for: M2 Statistique`. 
`tamis status` exits with code 1 on any failure, so it also works with any external monitor.

---

## 4. Public: `publish`

```sh
tamis publish           # build and publish once
tamis publish --watch   # keep running, republish every refresh_minutes
tamis service --mode publish --install    # the same as a background job
```

Add a `[publish]` section to your config (template in `tamis/config.example.toml`):

**Secret GitHub gist (unguessable URL):**

1. Create a *secret* gist on gist.github.com with one file `calendar.ics` (any content).
2. `git clone https://gist.github.com/<gist-id>.git ~/edt-feed` (authenticate once: `gh auth login`, or an SSH key).
3. Config:
   ```toml
   [publish]
   method     = "git"
   target     = "~/edt-feed"
   file_name  = "calendar.ics"
   public_url = "https://gist.githubusercontent.com/<user>/<gist-id>/raw/calendar.ics"
   ```
4. `tamis publish`, then subscribe everywhere to `public_url` — on a Mac with **Location: iCloud**, so iCloud refreshes it for all your Apple devices; on Android via ICSx⁵ or Google Calendar (*Other calendars → From URL*; Google refreshes on its own schedule, often 8–24 h).

`keep_history = false` (the default) amends a single commit and force-pushes, so the repo holds only the current timetable, not an archive of where you were each week. 
Feed commits are authored as `edt-tamis`, not with your git identity. 
A *secret* gist is unlisted, not access-controlled: anyone with the link can read it.

`file_name` must be a plain file name (letters, digits, `_ - .`, spaces; no folders), `target` must be set, and `publish` refuses to write through a symbolic link or to publish a git clone that has other changes staged.

**Any folder** (`method = "dir"`, `target = "/path/to/folder"`): writes the file there — a web server's document root, a Nextcloud public share, etc. 
Use `tamis secret` for an unguessable file name.

**Switching a running Tailscale setup to public** without git: `tailscale funnel --bg 8765` publishes the same `serve` endpoint to the internet. Set `url_path` to the output of `tamis secret` first, since the machine name is public. Funnel forwards your public host name, which the host check refuses (HTTP 421) until you list exactly that name in `allowed_hosts` — do not use `"*"`, which switches the check off for a server that is now on the internet behind nothing but the secret path.

---

## Project layout

```
tamis/
  cli.py        command line (argparse)            config.py   TOML config, observation period
  ade.py        ADE URLs, download, last good copy  ics.py      iCalendar parse / write
  rules.py      course matching, dedup, clashes     report.py   `report` and `titles`
  pipeline.py   fetch -> filter -> write, loops     server.py   `serve`
  publish.py    `publish` (git / dir)               service.py  launchd / systemd / Task Scheduler
  health.py     warning events, watchdog, alerts    paths.py    per-OS config/data/log folders
  config.example.toml
tests/          unittest suite + fixture feeds (no network needed)
```

Development: `python -m unittest discover -s tests -v`. 
No dependencies beyond the standard library (plus `tomli` on 3.10 and `tzdata` on Windows).

The last good downloads and the built calendar are kept in `~/Library/Application Support/edt-tamis` (macOS), `~/.local/state/edt-tamis/data` (Linux) or `%LOCALAPPDATA%\edt-tamis\Data` (Windows) — deliberately not a cache folder, which the system may empty.

The tool never logs in anywhere and never writes to ADE. 
Please keep `refresh_minutes` at an hour or more (values below 10 are refused) — the timetable does not change faster than that, and the university's server is shared.
If a university closes the anonymous export, point sources at downloaded files with
`file = "..."`; everything else keeps working.
