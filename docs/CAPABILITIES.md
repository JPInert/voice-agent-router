# What the voice reaches

The production system behind this router pipes one voice to the lights, the TV, the car, the phone,
the cameras, mail, calendar, music and the desktop. This page lists the devices, accounts and
services it reaches: how a spoken request gets there, whether it can change anything, and what
stands in the way when it can. It is not exhaustive: some personal read-outs (a few
phrases that speak private data) are left out on purpose.

How to read it. Each row was checked against the production code and its phrase file on
2026-10-16 by reading them. I did not fire every row that day, and the last column says exactly
what of the row is in this repo. A row I could not find in code or config is not listed.

**How it is reached**

| Word | Meaning |
|---|---|
| Phrase | Tier 0. A phrase in the YAML list or a hand-coded intent. No model is called. |
| Haiku | Rung 1. One tool-less Claude Haiku call that classifies and sometimes answers. |
| Agent | Rung 2 or 3. A Claude Agent SDK session in the room, with tools. |
| Phone worker | The Sonnet session that answers a phone request no phrase matched. See [PHONE_WORKER.md](PHONE_WORKER.md). |
| Connector | A hosted MCP connector (mail, calendar, drive, music) attached to the model's login. |
| MCP | The stdio MCP server that gives any Claude Code session voice tools. |
| Push | A watcher that speaks on its own through the voice daemon. Nothing asks it to. |

**Permission gate**

| Word | Meaning |
|---|---|
| auto | Runs with no question. |
| asks | Needs a spoken yes first. From the phone there is no yes/no channel, so it is refused, not assumed. |
| blocked | Never runs, in any session. |
| read-only | Cannot change anything. |

## Devices and accounts you can speak to

| Connector | Reached by | Changes state? | Gate | In this repo |
|---|---|---|---|---|
| **Smart-home lights, plugs, scripts and scenes** (Home Assistant) | Phrase: YAML `ha` actions (12 in the live file), hand-coded intents (light percentages, per-room lights, chained commands), then Home Assistant's own Assist grammar. Phone worker: `house` | Yes | auto. A scene run at an unusual hour asks first in the room. A trick marked `confirm: true` asks in the room and is refused from the phone | Adapter shape: `router/connectors/home.py` (`HttpHome`, `Reflex`, `house`). The entity map is not included |
| **Home state reads** (locks, doors, temperatures, thermostat) | Phone worker: `house_status` | No | read-only | `house_status` in `router/connectors/home.py` |
| **TV** (webOS media player through Home Assistant, infrared scripts as fallback) | Phrase: volume and mute | Yes | auto | No (hand-coded intents) |
| **Thermostat / AC** | Phrase: "set the AC to 72", "it's hot" nudges the setpoint and says what changed | Yes | auto | No |
| **Kodi playback** (HTPC) | Phrase: "play X", "next episode", pause and stop, matched against the library with TMDB and MDBList; YAML `kodi` actions | Yes | auto. Away from home, "play" is refused so the home TV is never started from the road | The `kodi` trick action in `router/tricks.py`. The library matcher is not included |
| **Kodi now playing** | Phone worker: `now_playing` | No | read-only | `router/connectors/kodi.py` (`HttpKodi`) |
| **YouTube on the TV** | Phrase: "play funny videos on youtube", clip-shaped requests that miss the library (yt-dlp search into Kodi) | Yes | auto | No |
| **Spotify playback** | Phrase: "play X", "queue X", "on my phone" or "in the car", through the Spotify Web API. The identified speaker is passed along so each person's own account is used | Yes | auto | No |
| **Spotify reads and library** (hosted connector) | Phone worker: currently playing and search | Reads only | auto for reads. Save to library, remove from library, generate playlist: blocked | Names classified in `router/connectors/hosted.py` |
| **The car** (Tesla Fleet API): send a destination, add a stop | Phrase: navigate intent, car-command actions in the trick file. Phone worker: `car` | Yes | auto. Destination and stop wake a sleeping car without asking | `router/connectors/car.py` (`CliCarBackend`). The Fleet API service itself is [tesla-fleet-voice](https://github.com/JPInert/tesla-fleet-voice) |
| **Car climate, media, volume** | Phrase: car-command actions in the trick file (5 in the live file), volume intent. Phone worker: via `house` | Yes | Runs at once on an awake car. A sleeping car asks "wake it?" in the room. From the phone it wakes without asking, because the phone cannot answer | Not in the `car` tool, by design. `router/tricks.py` has no car action kind; a `shell` trick can call your car client |
| **Car reads** (battery, temperature, state) | Phrase: YAML `shell` (read-only). Phone worker: `car` | No | read-only; never wakes the car | `router/connectors/car.py` |
| **Car lock, unlock, trunk, windows, honk, flash, sentry** | Not reachable by voice | n/a | blocked by omission: no phrase and no tool action exists | The action list in `car.py` is the allow-list, and a test checks it |
| **Phone intents** (timer, alarm, battery, open an app) | Phone worker: `phone_do` | Yes | auto, inputs validated (timer 1 to 86400 s, alarm HH:MM, app name letters and digits only) | `router/connectors/phone.py` (`AdbPhone`). Production used root over ssh, which is not included |
| **Phone screen** (open a page, picture, map or camera still) | Phone worker: `phone_open`, and a camera-still tool | Yes (puts something on the screen) | auto, plain http(s) URLs only | `phone_open` in `router/connectors/phone.py` |
| **Phone's stock assistant** | Phrase: YAML `shell` (read-only) wakes it by key event | Yes | auto | No |
| **Cameras: health spot-check** | Phrase: YAML `shell` (read-only) checks tiles, announcer, NVR stats, per-camera fps, detector speed, disk | No | read-only | No |
| **Gmail** (hosted connector) | Phone worker | Drafts only | auto: search, read, list, create draft. Send, reply, forward, trash, delete, spam: blocked | `mail_search`, `mail_get`, `mail_draft` in `router/connectors/mail.py`; hosted names in `hosted.py` |
| **Google Calendar** (hosted connector) | Phone worker | Add to the owner's own calendar | auto: list, get, search, suggest time. Add: only with no attendees and no notifications (a rule). Respond, update, delete: blocked | `calendar_list`, `calendar_add` in `router/connectors/calendar.py`; the rule is `gate.no_attendees` |
| **Google Drive** (hosted connector) | Phone worker | Reads only | auto: search, read, metadata, recent. Share, trash, update, create, copy: blocked | Names in `hosted.py` |
| **The web** | Phone worker and rung 3: WebSearch, WebFetch | No | auto | Built-in tools named in `phone_worker.py` |
| **Food diary** (FatSecret) | Phone worker: `food` | Yes: log and delete | auto. Search, log, day, delete only: no create, no weight. An amount outside 0 to 10 units is refused | `router/connectors/food.py` (backend is a CLI you supply) |
| **Remote-support platforms** (managed Windows PCs: connected or not, last seen, system info, processes, disk, network, event log) | Phone worker | No | auto for reads. Click, type, scroll, shell, PowerShell, reboot, open or close a session: blocked | No |
| **The desk computer** (shell, files, services, printers, Bluetooth, audio) | Agent: rung 2 has Bash and read tools, rung 3 adds write and web tools. Haiku answers from a machine snapshot when it can | Yes | A command proven read-only runs silently. Anything else is read aloud and needs a spoken yes. One question per task, and a no ends the task | `router/ladder.py`, `router/permissions.py`, `router/snapshot.py` |
| **Dictation into a terminal** | Phrase: "type ..." and "tell claude ...". Only that path types into the focused terminal | Yes | auto, and only on those words | No |
| **Monitors** | Phrase: "good night" also turns this computer's displays off | Yes | the scene asks at an unusual hour | No |
| **Claude Code sessions already open on the desk** | MCP: `sessions`, `send_to_session`. Hands-free coding mode is [talk2code](https://github.com/JPInert/talk2code) | Yes: types into another session | `submit` defaults to off, so the text waits in that session's prompt for a human to read and press Enter | `mcp_server/voice_mcp.py` |
| **Voice tools for any Claude Code session** | MCP: `say`, `ask`, `listen`, `notify`, `status` and others (11 here) | Speaks; `ask` takes a spoken answer | auto | `mcp_server/voice_mcp.py` |
| **New phrases, by voice** | MCP: `add_trick` writes a YAML entry | Yes | Written disabled. Enabled only after the entry is read back and answered with a spoken yes | Validation in `router/tricks.py`. The `add_trick` tool in `mcp_server/voice_mcp.py` posts to the production curator, which does the read-back and the yes; `server.py` has no such endpoint |
| **Bigger jobs: hand-off** | Phone worker: `delegate`, then "go ahead", then "computer, confirm" | Yes, after two yeses | A look-only report first. "Go ahead" only arms the plan and reads it back. "Confirm" within a minute runs it, once. Sends, pushes, restarts, remote control, ssh and sudo are denied in the run | `gate.PlanSlot` (the two-yes slot). The runner is not included |
| **New tools, by voice** | Phone worker: `build_wish`, then "go ahead", then "install it" | Yes (code) | Builds only in a staging copy; install is the second yes, with a health check and automatic rollback | No |
| **Memory and reminders** | Phone worker: `memory`, `reminder` into a local memory layer | Local facts only | auto | No (separate work) |
| **Wishlist** | Phone worker: `wish`, and an automatic note when a reply says "I can't ..." | Appends to a local file | auto | `router/connectors/wish.py` |

## Things that speak without being asked

All of these speak through the daemon's own `/say` door, so ducking, earcons and "do not talk
over a question" apply to them. If the daemon is down they log and drop the alert.

| Source | Trigger | Gate |
|---|---|---|
| Camera person events (NVR over MQTT) | A person. On one camera, arrival or departure is worked out from the order of two zones | Speak only. Per-camera cooldown and a global gap |
| Machine alarms and battery | CPU temperature or disk usage over a threshold, with hysteresis and an all-clear; device battery warnings at 20, 10, 5, 3 and 1 percent | Speak only. Battery warnings are held at night, except 3 and 1 percent, which speak on the desk only |
| Pet water fountain (through Home Assistant) | Low, empty, refilled, plus one daily summary | Speak only |
| Calendar reminders | 5 to 10 minutes before a timed event on the primary calendar; a new unanswered invite once | Read-only on the calendar. Silent from 22:00 to 07:00 |
| Car alerts (charge complete, low battery, left unlocked) | A watchdog timer that never wakes the car to poll it | Speak only. Quiet at night |

## Where the model comes from

| Piece | What it does |
|---|---|
| Subscription first, metered fallback | Sessions use the Claude subscription. On a rate or usage limit they retry once on a metered API key under a daily spend cap, with a cooldown. A turn is never retried after it exposed output or a tool call |
| Free chain for question-shaped misses | In the room, a question nobody else claimed goes to a chain of free OpenAI-compatible models and is spoken |
| Phone's own assistant | Only when the desk cannot be reached from the phone or the phone's listener is not reading |

## Blocked or absent everywhere

None of these has a phrase or a tool: send, reply to or forward mail; trash or delete mail; answer or
change a calendar invite; delete an event; share or delete a Drive file; change a Spotify library;
click, type, run a shell or reboot on a remote PC; lock, unlock or open the car. Where a hosted
connector offers the tool anyway, the phone worker has it in `disallowed_tools`, which takes
precedence over allow rules and over the permission callback. See
[PHONE_WORKER.md](PHONE_WORKER.md#what-it-can-do-and-what-it-cannot). The room's rung 2 and 3
sessions are different: they can run any shell command or write any file, and the only thing
between them and it is the spoken yes.

### The exact blocked connector tools

The names the phone worker passes as `disallowed_tools` (`router/connectors/hosted.py`):

| Connector | Blocked tools |
|---|---|
| Gmail | `send_message`, `reply`, `forward`, `trash_message`, `trash_thread`, `delete_draft`, `update_draft`, `delete_label`, `mark_message_spam`, `mark_thread_spam` |
| Google Calendar | `respond_to_event`, `delete_event`, `update_event` |
| Google Drive | `share_file`, `trash_file`, `update_file`, `create_file`, `copy_file` |
| Spotify | `save_to_library`, `remove_from_library`, `generate_playlist` |

## Not driven by voice

Android home-screen widgets (built with Tasker) show the car's battery and food totals. They are
fed from the desk and have no voice path. They are published separately as
[tasker-remote-widgets](https://github.com/JPInert/tasker-remote-widgets).
