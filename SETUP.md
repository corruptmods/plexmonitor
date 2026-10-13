# PlexMonitor setup

Everything you can set, from first install to the look of the dashboard. For a
quick overview see the [README](README.md).

---

## 1. Install

```bash
git clone https://github.com/corruptmods/plexmonitor.git
cd plexmonitor
cp .env.example .env
nano .env
docker compose up -d --build
```

The image is built from this folder. Your settings and the bot's state live in
`data/`, a bind mount, so rebuilding never loses them.

**Dockge:** clone into your stacks directory (for example
`/opt/stacks/plex-monitor/`), create `.env`, and the stack appears in Dockge.
**Deploy** builds it. After pulling new code, use **Stop → Deploy**.

**Updating:** `git pull`, then `docker compose up -d --build`.

---

## 1b. Creating the Discord bot

Skip this if you already have a working bot token.

**This bot needs no privileged intents.** Leave Message Content, Server Members
and Presence all off. The assistant works without Message Content too: Discord
always delivers the text of DMs and of messages that @mention the bot.

1. [discord.com/developers/applications](https://discord.com/developers/applications)
   → **New Application** → name → **Create**
2. **Bot** tab → **Reset Token** → **Copy** → paste into `.env` as `DISCORD_TOKEN`
3. **OAuth2 → URL Generator**, tick scopes `bot` and `applications.commands`
   (the second is what makes `/load`, `/reload` and `/cogs` appear)
4. Under **Bot Permissions** tick:

   | Permission | Why |
   |---|---|
   | View Channel | See its channels |
   | Send Messages | Post the dashboard, cards, panel and announcements |
   | Embed Links | Render the embeds at all |
   | Attach Files | Posters, progress bars and the width strip on stream cards |
   | Read Message History | Re-find its own messages to edit them |
   | Use External Emojis | Only needed for custom emoji from another server |

   **Read Message History is the one people forget.** Without it the bot cannot
   re-fetch its own message, so it posts a brand new dashboard every minute
   instead of editing the existing one.

   Or use this link directly, substituting your Application ID:

   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot+applications.commands&permissions=379904
   ```

   Two more are worth granting as **channel overrides** rather than server-wide:

   - **Manage Messages** on the request panel channel (to pin the panel) and
     the assistant channel (to take @mention questions out of view).
   - **Manage Channels** on the dashboard channel, for the 🟢/🔴 status dot in
     the channel's name (`channel_dot` in `data/config.json`; set its
     `enabled` to `false` if you'd rather not).

5. Open the URL, pick your server, **Authorize** (needs Manage Server).
   Then, on the **Bot** tab, turn off **Public Bot** so only you can add it
   to servers. The bot only answers AI questions from members of the server
   its dashboard is in, but there's no reason to let others invite it.
6. **Channel ID:** User Settings → Advanced → **Developer Mode** on, then
   right-click the channel → **Copy Channel ID** → `CHANNEL_ID` in `.env`.
7. Check the channel's own permission overrides — a private channel can block
   the bot even when the server role allows it. The symptom in the logs is
   `Channel <id> not found`, which actually means "cannot see it".

---

## 2. What to fill in

Only two blocks are mandatory.

**Discord** — `DISCORD_TOKEN` and `CHANNEL_ID` (the dashboard's channel).

**Data source** — either Tautulli (recommended) or Plex directly:

| | Set these | Notes |
|---|---|---|
| Tautulli | `TAUTULLI_URL`, `TAUTULLI_API_KEY` | Key is under Settings → Web Interface → API |
| Plex | `PLEX_URL`, `PLEX_TOKEN` | Used automatically if Tautulli is blank |

Setting both is fine — Tautulli is preferred, Plex becomes the fallback.

Everything else is optional. Leave a block blank and that feature switches off
cleanly rather than erroring:

| Feature | Settings |
|---|---|
| Downloads in progress | Any of SABnzbd, NZBGet, qBittorrent, Transmission, Deluge and rTorrent (`SABNZBD_*`, `NZBGET_*`, `QBITTORRENT_*`, `TRANSMISSION_*`, `DELUGE_*`, `RTORRENT_*`; see below) |
| Recently downloaded, with proper titles | `RADARR_URL`/`RADARR_API_KEY`, `SONARR_URL`/`SONARR_API_KEY` |
| Uptime figures | `UPTIMEROBOT_API_KEY` (wins if set), or Uptime Kuma 2.x (`UPTIME_*`, below) |
| Requests, `/issue`, "Now on Plex" | `SEERR_URL`, `SEERR_API_KEY` (below) |
| AI assistant | Seerr AI, or `AI_PROVIDER`/`AI_API_KEY` (below) |

Each setting is explained in `.env.example`.

### Seerr: requests, `/issue` and "Now on Plex"

Set `SEERR_URL` and `SEERR_API_KEY` (Seerr → Settings → General → API Key). Add
`SEERR_PUBLIC_URL` if `SEERR_URL` is a LAN address, so links in messages open on
phones.

The bot uses the admin key but **acts as the person asking** (Seerr's
`X-Api-User`), so their permissions, quotas and auto-approval apply exactly as in
the web UI. To be recognised, a person's Discord account has to be linked to their
Seerr account. Anyone who isn't linked and tries to request, report an issue or
use the AI (or just tells the AI "link my account") gets a private **Link your
Seerr account** card with two ways to do it:

- **Themselves:** the card shows their Discord ID and an **Open Seerr** button to
  **profile → Settings → Notifications → Discord**. They paste the ID, save, and
  press **check again**. Only someone signed in to that Seerr account can do this,
  so it can't be used to claim somebody else's.
- **Ask an admin:** they type their Seerr or Plex username. The bot admins get a
  DM with **Approve / Deny**, and the person gets a DM with the result. One
  request per person every 10 minutes.

"Bot admins" are the IDs in `DISCORD_AUTHORIZED_USERS`, or the bot's owner when
that's empty. They can also link people directly with **`/link @member <Seerr
account>`** (the account list autocompletes) and undo it with `/unlink`.
Discord only shows these two to people with Manage Server (change that under
Server Settings → Integrations), and the bot also checks the person is a bot
admin: Manage Server alone isn't enough, because a link lets someone act as
that Seerr account. If two Seerr profiles hold the same Discord ID, the bot
trusts neither until an admin uses `/link`. Approved links and `/link` live in `data/seerr_links.json`. You can also
map people by hand in `data/config.json`:

```json
"requests": {
  "announce_channel_id": 123456789012345678,
  "discord_links": { "222222222222222222": 7 }
}
```

`announce_channel_id` is where "Now on Plex" posts go (0 = off). The bot polls
Seerr every `poll_seconds` (default 120) for requests that finished, posts one
card per title with the poster, a **Watch on Plex** button and who asked for it,
and never posts the same request twice. Requesters are named, never pinged: by
their name in your server when their Discord is linked, otherwise by their Seerr
name. On the very first run it records everything already finished without
posting it. Requests Seerr completed because the media was **deleted** (for
example by Maintainerr) are never announced.

**Watch on Plex** links come from Seerr's Plex **Web App URL** (Seerr →
Settings → Plex). Set it to `https://<your plex>/web/index.html`, or leave it
empty to use app.plex.tv. "Open in Seerr" links use Seerr's **Application URL**
(Settings → General), unless `SEERR_PUBLIC_URL` is set.

If Seerr also sends its own Discord notifications (Seerr → Settings →
Notifications → Discord), turn off "Media Available" there, or people get two
posts.

### Request panel

The way to request in Discord: a permanent message, like the status dashboard.
There's no request command. It's an embed with up to three buttons:

- **Request** opens a popup for a title. A clear title shows its card with
  **Request**, season picks or **Watch on Plex**. Several matches give a picker,
  and what isn't a title (a sentence, a typo) goes to the AI.
- **Ask AI** opens a popup for a question in plain words, answered by the
  assistant. It only appears while the assistant is set up.
- **Report a problem** asks which title (only what's on Plex), then opens the
  same form as `/issue`: won't play, sound, subtitles, missing episodes, wrong
  episode, or something else. It files a Seerr issue as the person.
  - **With plain Seerr**, the admins see it in Seerr like any issue.
  - **With [Seerr AI](https://github.com/corruptmods/seerrai)**, most are fixed
    automatically, and Seerr AI posts what it did on the issue. A file that
    won't play or has bad sound is replaced, subtitles are fetched (or made
    from the audio), a wrong episode is swapped, and missing episodes are
    searched for again. An admin only hears about it if that doesn't work.

  The panel and the confirmation say which of the two applies.

Everything after a press is visible only to the person who pressed. Choose the
channel with `REQUEST_PANEL_CHANNEL_ID` in `.env` (or `requests.panel_channel_id`
in `data/config.json`). Use a channel of its own, not the status or stream
cards channel. The bot posts the panel and pins it (pinning needs Manage
Messages). Its message ID is kept in `data/request_panel.json`. Every 5 minutes
the bot checks it: a deleted panel is posted again, a changed one is edited, and
changing the channel moves it. "Now on Plex" posts go wherever
`ANNOUNCE_CHANNEL_ID` (or `requests.announce_channel_id`) points, and the panel
names that channel.

### Assistant (AI)

People reach the AI through the request panel: **Ask AI** takes a question in
plain words ("something like Severance but shorter?", "is Dune on Plex?"), and
anything typed into **Request** that isn't a title goes there too (a name shared
by a film and a series still gets the picker). @mentions and DMs also work (see
below).

There are two possible sources of answers:

- **Seerr AI's own AI.** If your Seerr is
  [Seerr AI](https://github.com/corruptmods/seerrai), the bot asks it **as the
  person**, just like the AI button in Seerr AI: the same model and settings,
  the same watch-history context, the same daily limit and per-person AI
  switches. It needs nothing in `.env` beyond the Seerr settings above. The bot
  checks for it at start-up and every 10 minutes; plain Seerr doesn't have it.
- **The bot's own model**, set in `.env`:

  | Provider | Settings |
  |---|---|
  | Claude | `AI_PROVIDER=anthropic`, key from console.anthropic.com. Default model `claude-sonnet-5-5` |
  | OpenAI | `AI_PROVIDER=openai`. Default model `gpt-5-mini` |
  | Ollama / LM Studio / other OpenAI-compatible | `AI_PROVIDER=openai`, `AI_BASE_URL=http://ollama:11434/v1`, key may be blank |

Which one answers is `assistant.engine` in `data/config.json`:

| engine | Who answers |
|---|---|
| `auto` (default) | Seerr AI for anyone linked to Seerr who has it switched on; the bot's own model for everyone else, and for everyone when Seerr isn't Seerr AI |
| `seerr` | Only Seerr AI. Unlinked people are told how to link |
| `own` | Only the bot's own model |

When neither is available, the assistant stays off and the request panel drops
its **Ask AI** button. When Seerr AI says someone has used up their AI answers for
the day, the bot shows that message and doesn't work around it.

**`/recap <show>`** is Seerr AI's **Catch Me Up**: a spoiler-free recap of
everything before the next episode the person hasn't watched (from their watch
history), or before a season and episode they name. It needs Seerr AI, with
`engine` set to `auto` or `seerr`.

The assistant only answers members of the server the dashboard is in (the one
`CHANNEL_ID` belongs to): DMs from anyone else, and @mentions in any other
server, are ignored. Its answers can say who is watching what, and the bot's
own model spends your API key.

Conversations are **private**. Only the person asking ever sees the answer:

- **The request panel** answers with a message only the asker can see. Its
  **Reply** button opens a box to keep the conversation going.
- **@mention** the bot in the assistant channel and the question is removed from
  the channel (give the bot **Manage Messages** there) and answered in the
  person's DMs. A short "answered you in DMs" note appears for 20 seconds. If
  their DMs are closed, the note points them to **Ask AI** in the request panel.
- **DMs** just work: every message is the next turn, and **New chat** starts over.

Pick the @mention channel with `AI_CHANNEL_ID` (or `assistant.channel_id` in
`data/config.json`); 0 accepts @mentions in any channel. The bot remembers the
last `assistant.history` exchanges of each person's conversation for 30 minutes
of quiet. `assistant.daily_limit` caps questions per person per day on the bot's
own model (default 25). Seerr AI keeps its own limit. Set `assistant.dms` to
`false` to turn off DM chat, or `assistant.delete_mentions` to `false` to leave
mentions in the channel.

The bot's own model can look things up in Seerr and see what's playing, what
just finished downloading, library sizes, and the asker's own requests and watch
history. Neither AI **files requests**. Suggested titles come back one per page
(◀ ▶ to flip), with a Request button the person presses themselves, or **Watch on
Plex** when it's already there.

### Real-time refresh (Tautulli webhook)

The dashboard refreshes on a timer, and also within seconds of a playback
change if Tautulli tells it. In Tautulli: **Settings → Notification Agents →
Add a new notification agent → Webhook**, with:

- **Webhook URL:** `http://<bot host>:8322/webhook`
- **Webhook Method:** `POST`
- **Triggers:** Playback Start, Playback Stop, Playback Pause, Playback Resume

Port 8322 is published by `compose.yaml` on every interface of the host, and
Docker bypasses most host firewalls. Keep the host off the internet, or bind it
to one address in `compose.yaml` (for example `"192.168.1.10:8322:8322"`). To
require a secret, set `webhook.secret` in `data/config.json` and use
`http://<bot host>:8322/webhook/<secret>` instead. The worst a stranger can do
with the URL is trigger refreshes, which are rate limited anyway.

### Download clients

PlexMonitor reads the download clients Seerr AI supports, as many as you use:

| Client | Settings | Signs in with |
|---|---|---|
| SABnzbd | `SABNZBD_URL`, `SABNZBD_API_KEY` | its API key (Config → General → Security) |
| NZBGet | `NZBGET_URL`, `NZBGET_USERNAME`, `NZBGET_PASSWORD` | its control username and password |
| qBittorrent | `QBITTORRENT_URL`, `QBITTORRENT_USERNAME`, `QBITTORRENT_PASSWORD` | its Web UI login, kept as a session |
| Transmission | `TRANSMISSION_URL`, `TRANSMISSION_USERNAME`, `TRANSMISSION_PASSWORD` | its RPC login, if it has one |
| Deluge | `DELUGE_URL`, `DELUGE_PASSWORD` | its Web UI password |
| rTorrent | `RTORRENT_URL`, `RTORRENT_USERNAME`, `RTORRENT_PASSWORD` | the login of the web server in front of it (usually ruTorrent's) |

Each address is the client's Web UI, e.g. `http://192.168.1.10:8080`.
Transmission's RPC path (`/transmission/rpc`) is added for you. rTorrent's
address is its XML-RPC endpoint, usually `http://<host>/RPC2`; `/RPC2` is
added when the address has no path. Leave a username and password blank only
when the client lets this bot in without one. For qBittorrent that's Options
→ Web UI → *Bypass authentication for clients in whitelisted IP subnets*.

All their queues show as one **Downloading** panel, the ones actually moving
first, with the combined speed. A job that isn't moving says why: *paused*,
*queued*, *checking*, *getting metadata*, *stalled, no peers* for a torrent
nobody is sharing, or *unpacking*, *repairing* and the like while NZBGet
finishes one. A torrent leaves the panel once it finishes downloading,
whether it seeds or not.

If a client also holds downloads that have nothing to do with Plex, set
`DOWNLOAD_CATEGORIES` to the categories Radarr and Sonarr use (for example
`radarr,tv-sonarr`). That means the category in SABnzbd, NZBGet and
qBittorrent, and the label in Transmission, Deluge (its Label plugin) and
ruTorrent. Set `DOWNLOAD_VERIFY_SSL=false` if a client uses a self-signed
HTTPS certificate.

**Completed Downloads** lists what just finished. With Radarr or Sonarr set
up, it lists only what they imported, since a download that finished but
failed to import isn't news. Without them, it uses each client's own record
of finished jobs, and what the bot saw leave the queue at 100%.

The `downloads` section of `data/config.json` (`max_items`, `max_name_length`,
and `keywords`, which trims release tags like `1080p` off names) covers every
client. An older config's `sabnzbd` section is still read when there's no
`downloads` one.

### Stream cards channel

By default the stream cards sit under the dashboard. To give them their own
channel, set `stream_cards.channel_id` in `data/config.json`.

### Uptime Kuma 2.x

Kuma 2.x is read over plain HTTP (Kuma 1.x isn't supported):

1. In Kuma: **Settings → API Keys → Add API Key**. Copy it (shown once).
2. Set `UPTIME_API_KEY` and `UPTIME_MONITOR_ID` in `.env`.
3. Also set `UPTIME_USERNAME` / `UPTIME_PASSWORD` if you want the uptime
   counter seeded with real history — see section 4.

The monitor ID is the number in the URL when you open the monitor
(`/dashboard/7` → `7`).

Two caveats worth knowing up front:

- **Uptime percentages need Kuma ≥ 2.1.0.** The `monitor_uptime_ratio` metric
  did not exist in 2.0.x. Check with
  `docker exec uptime-kuma cat /app/package.json | grep '"version"'`.
- **The 7-day figure needs a published status page.** Kuma's `/metrics` exposes
  only 24h, 30d and 365d windows. The bot fills 7d from the badge endpoint,
  which requires the monitor to be public on a status page. If it isn't, the
  7-day field is simply hidden — nothing breaks.

---

## 2b. Look and feel

All in `data/config.json`. Changes apply on the next refresh (≤ 1 min) — no
rebuild, since `data/` is a bind mount. The exceptions, which need a restart
(`docker compose restart`): the `downloads` section, `webhook.port`, and the
`stream_cards.channel_id` channel. A section that isn't a JSON object is
ignored with a warning in the log, and its defaults are used.

### Section headings

`dashboard.section_titles: true` breaks the embed into Server, Library,
Activity and Reliability headings, styled by `dashboard.section_title_style`.

### `dashboard.field_style`

| Value | Look |
|---|---|
| `boxed` *(default)* | A code block per value. Easy to read at a glance |
| `pill` | Each value in an inline-code capsule — compact, contained |
| `plain` | Bold text, no container |

### `emoji`

Every slot is configurable; set any to `""` to hide it.

```json
"emoji": {
  "online": "🟢", "offline": "🔴",
  "uptime": "🖥️", "streams": "📡",
  "now_playing": "🎬", "downloading": "📥", "uptime_stats": "📊",
  "playing": "▶️", "paused": "⏸️", "transcode": "🔄", "direct": "⚡"
}
```

**Animated emoji.** Discord only *guarantees* custom and animated emoji render
in an embed's **description** and **field values** — titles and field names can
show the raw `<a:name:id>` tag depending on client. So the bot deliberately puts
the status dot in the description, which is where an animated dot is safe:

```json
"online":  "<a:greendot:1234567890123456789>",
"offline": "<a:reddot:1234567890123456789>"
```

Bots can use animated emoji without Nitro. Upload them to any server the bot is
in, then type `\:greendot:` in Discord and send it to get the full tag.

The other slots sit in field names, so keep unicode there unless you've tested
your client. Custom emoji also **never** render inside a code block, so with
`field_style: "boxed"` they can only live on headings and stream titles.

### `panels`

```json
"panels": {
  "total_space": false, "queue_speed": true, "downloads": true,
  "reliability": true, "bandwidth": true, "episodes": true
}
```

`total_space` reports **SABnzbd's** download volume — usually a scratch disk
rather than media storage — so it's off by default. The download panels only
appear when a download client (or, for finished downloads, Radarr or
Sonarr) is set up.

### Stream card percent

`stream_cards.percent_position` places a stream's progress percent:
`"line"` *(default)* on its own line under Bandwidth (`⏳ Progress · 89%`, icon
from `emoji.progress`), `"time"` on the time line, or `"bar"` after the bar.

### Line wrapping

Line breaks are chosen by the bot rather than left to Discord, so long release
names and 4K→1080p transcode reasons wrap at sensible places instead of
mid-word. `dashboard.line_width` (default 40 characters) sets how wide lines
may get. Raise it for denser lines if everyone uses Discord on a desktop.

---

## 3. Progress bars

Everything about the bar lives in `data/config.json`:

```json
"progress_bar": {
  "style": "parallelogram",
  "inline_code": true,
  "segments": 10,
  "show_percent": true
}
```

| Style | Looks like |
|---|---|
| `parallelogram` | ▰▰▰▰▰▰▱▱▱▱ |
| `block` | ██████░░░░ |
| `shade` | ▓▓▓▓▓▓░░░░ |
| `vertical` | ▮▮▮▮▮▮▯▯▯▯ |
| `line` | ━━━━━━──── |
| `square` | ■■■■■■□□□□ |
| `circle` | ⬤⬤⬤⬤⬤⬤◯◯◯◯ |
| `ascii` | `======----` |

`inline_code: true` wraps the bar in backticks, which forces Discord to use a
monospace font. Without it the bar renders in Discord's proportional UI font,
where the filled and empty glyphs may not be the same width — so the bar's
length visibly shifts as progress changes. Leave it on unless you've tested
otherwise on both desktop and mobile.

Changing the style takes effect on the next dashboard refresh (≤ 1 minute).
No rebuild needed — `data/` is a bind mount.

### Moving to custom emoji later

Upload four emoji to your server, then:

```json
"progress_bar": {
  "style": "emoji",
  "styles": {
    "emoji": {
      "filled":    "<:bar_full:123456789012345678>",
      "empty":     "<:bar_empty:123456789012345678>",
      "cap_left":  "<:bar_left:123456789012345678>",
      "cap_right": "<:bar_right:123456789012345678>"
    }
  }
}
```

Get an emoji's ID by typing `\:bar_full:` in Discord and sending it.

`inline_code` is ignored for emoji styles — Discord renders `<:name:id>` as
literal text inside backticks instead of drawing the image. The bot logs a
warning if you leave it on and carries on correctly.

---

## 4. About the uptime figure

Neither Plex nor Tautulli publishes a server uptime, so the bot tracks up and
down transitions itself and saves them to `data/server_state.json`, which
survives container rebuilds. It's Plex's uptime as the bot sees it, not the
bot's own.

It also debounces: one slow response doesn't reset a 60-day figure. Two failed
checks in a row are needed, tunable with `cache.down_confirmations`.

Uptime is shown with no cap: `47m` → `9h 5m` → `6d 3h` → `41d 6h` → `7mo 2d`.

With UptimeRobot (`UPTIMEROBOT_API_KEY`), the figures come from its monitor,
which checks from outside your network and so also covers this container or
the whole host being down.

### Seeding real history from Uptime Kuma

The counter does **not** have to start at zero. Kuma prunes ordinary heartbeats
after 24 hours but keeps every *state transition* for `keepDataPeriodDays` —
365 by default. So your actual last outage is already recorded, and the bot
reads it.

Set `UPTIME_USERNAME` and `UPTIME_PASSWORD` (in addition to `UPTIME_API_KEY`)
and on startup the bot logs into Kuma over socket.io, walks back to the most
recent DOWN transition, and seeds the counter from the recovery that followed
it. It re-syncs every 6 hours, so Kuma stays the source of truth even if the
bot itself was offline during an outage.

Why both credentials: the API key covers `/metrics`, which gives current status
and uptime ratios but **no timestamps**. Kuma exposes timestamps only over the
socket.io API, and 2.x has no way to authenticate that with an API key —
username/password is the only path.

Three things turn this off gracefully, each falling back to local tracking with
a logged reason:

- `UPTIME_HISTORY=false`
- 2FA enabled on the Kuma account (can't be done headlessly)
- `python-socketio` not installed

**One thing to check:** the seeded figure reflects whatever `UPTIME_MONITOR_ID`
points at. If that monitor watches the host machine rather than Plex itself,
the dashboard will show host uptime. Point it at a Plex health check
(`http://<plex>:32400/identity` is a good one) if you want them to agree.

---

## 5. Files you can safely edit

| File | Purpose |
|---|---|
| `data/config.json` | Bar style, library sections, presence text, cache intervals |
| `data/user_mapping.json` | Plex username → display name |
| `.env` | Credentials and URLs |

`data/dashboard_message_id.json` and `data/server_state.json` are written by
the bot. Deleting the first makes it post a fresh dashboard message; deleting
the second resets the uptime counter. The request and assistant features also
write `announced_requests.json` (delete to re-arm announcements — the next run
records the backlog silently again), `ai_usage.json` (today's question
counts) and `seerr_links.json` (links made with `/link`). `requests` and
`assistant` in `config.json` hold their settings.

---

## 6. Sharing the bot token with another program

PlexMonitor can share its Discord bot token with one other program, such as a
watch party bot, so your server sees a single bot. Discord sends every button
press and command to both programs, so each one must answer only its own:

- PlexMonitor's persistent buttons start with `plexmon:` or `plex-monitor:`;
  short-lived menus (picking a title to report) get IDs discord.py generates.
  Its commands are the ones listed in the README. It never deletes messages
  carrying buttons it didn't make.
- It ignores commands it doesn't know, and re-sends its status every 15
  minutes, because the other program may reset it when it connects.
- `WATCH_PARTY_DMS=true` DMs each requester a ▶️ **Start watch party** button
  when their title lands. The button's `custom_id` is
  `reqgo:<discord user id>:<movie|tv>:<tmdb id>:x`, for the other program to
  handle. Leave it off unless you have such a bot.
