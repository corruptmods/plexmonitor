# Changelog

## 1.1.2

A sturdiness release: a security review and a bug hunt, with lots of small
fixes. Nothing new to set up.

**Fixed**

- **Download names aren't cut short anymore.** "The Middle" showed as "The
  Mid" and "Godless" as "Go", because a tag like `DL` was matched inside words.
  Tags now only count as whole words after the year or episode number.
- **A client that's down says so.** Downloading shows "Can't reach qBittorrent"
  instead of "Queue is empty", the assistant doesn't claim nothing is
  downloading, and your other clients keep being tracked in the meantime.
- **Pressing Request twice quickly** files one request, not two.
- **"Now on Plex" posts aren't lost** when Discord or Seerr has a bad few
  minutes. The bot keeps trying until the post goes through.
- **Fast downloads show up in Completed Downloads** when it goes by your
  download clients (no Radarr/Sonarr). One that went from half-way to done
  between two refreshes used to count as cancelled.
- **No more frozen leftovers.** Move the dashboard or the stream cards to
  another channel, or turn the separate Completed Downloads message off, and
  the old message is taken down instead of sitting there with its buttons
  still live.
- **Stream cards in their own channel can't hold up the dashboard.** Discord
  limits edits per channel, and the bot now keeps track of each channel
  separately, so a busy cards channel no longer makes the dashboard skip a
  refresh.
- **The dashboard's images stop flickering** after a day of uptime.
- **Stream cards sharing the dashboard's channel** no longer delete the
  dashboard on restart.
- **Popup forms stay open for an hour**, so a slow reply isn't lost.
- **qBittorrent:** a finished torrent being re-checked stays out of
  Downloading. A wrong password is retried once an hour instead of every
  refresh, so qBittorrent won't ban the address Sonarr and Radarr use too.
- **Deluge:** a torrent with an error no longer shows 100%, and the bot
  reconnects by itself when Deluge's daemon restarts.
- **NZBGet:** a job you paused no longer shows 100%.
- **SABnzbd:** Force-priority jobs that keep going in a paused queue show as
  downloading.
- Magnets still fetching their file list no longer show "0.00 MB",
  `DOWNLOAD_VERIFY_SSL` also takes `0`, `no` and `off`, and an address without
  `http://` works.

**Security**

- **The webhook port can't be used to knock the bot offline.** Quiet
  connections are now closed after a few seconds. Before, anyone on your
  network could hold enough open to cut the bot off from Discord.
- **Passwords and API keys never follow a redirect to another server.** If a
  download client, Seerr, Radarr/Sonarr or the AI service points somewhere
  else, the bot stops and logs where.
- **Oversized replies are refused**, so a misbehaving service can't run the
  bot out of memory.
- **Download names show as plain text.** Anyone can name a torrent, so a name
  can't turn into a link or formatting on the dashboard, and the assistant
  treats names as data.
- **Members only, everywhere.** `/recap`, search-as-you-type, the request
  panel and link requests now check that the person is in your server, like
  the assistant already did.
- **Logs:** secrets are blanked in every log line, including from the
  libraries the bot uses, and in more forms (URL-encoded, basic auth).
- **Run the bot as a regular user** by setting `PUID` and `PGID` in `.env`
  (see SETUP). Leave them blank and it runs as root like before.

Rebuild (or pull the new image) and you're done.

## 1.1.1

A small one that makes the "Now on Plex" posts look a lot better. 🍿

- **Posters are tall now.** They used to be cropped to a small square, so you'd
  often see a face or a corner instead of the art. Now the whole poster shows,
  the way it was meant to look.

Everything else in the post is the same: the title, the summary, who asked for
it, and the **Watch on Plex** and **Open in Seerr** buttons.

**Security updates**

- **aiohttp 3.14.5**, which fixes the advisories published for 3.14.3. The one
  that matters most here is the Tautulli webhook listener, which a malicious
  client on your network could tie up or slow down.
- **Pillow 13.0.0**, which fixes its advisories for opening untrusted image
  files. PlexMonitor only draws its own progress bars and never opens outside
  images, so it wasn't really exposed, but there's no reason to stay behind.

Nothing to change on your side: rebuild (or pull the new image) and you're
done.

## 1.1.0

Bring your own download client! 🎉 PlexMonitor now works with every one Seerr
AI supports: **SABnzbd**, **NZBGet**, **qBittorrent**, **Transmission**,
**Deluge** and **rTorrent**. Run one, or mix and match.

**What's new**

- **One Downloading panel for everything.** All your clients show up
  together, with what's actually moving at the top and the total speed.
- **It tells you why something's stuck.** Paused, queued, checking, waiting on
  peers, or unpacking: no more guessing why a download sits at 40%.
- **Completed Downloads covers every client**, not just SABnzbd.
- **Ask the assistant "is Dune still downloading?"** and get a real answer:
  how far along it is and how long is left. (If you use Seerr AI, its own AI
  answers as before.)

**Setting it up**

Add your client's address and login to `.env`. `.env.example` has a short
section for each one. If a client also handles downloads that aren't for
Plex, set `DOWNLOAD_CATEGORIES` to the categories Radarr and Sonarr use.

**Upgrading**

Nothing to change: your SABnzbd setup keeps working as it is. The `sabnzbd`
section of `data/config.json` is now called `downloads`, and the old name
still works. If you update by copying files over the old ones, you can delete
`cogs/sabnzbd.py`. The bot ignores it either way.

## 1.0.0

The first public release. 🎉

PlexMonitor keeps your Discord server up to date on your Plex server: a live
status dashboard, a card for everyone who's watching, and real uptime numbers.
It also lets friends request movies and shows through Seerr and ask an AI
assistant what to watch next, without leaving Discord.

- A live dashboard with streams, bandwidth, library counts, uptime, downloads
  and reliability, refreshed within seconds when someone presses play.
- Stream cards with posters and progress, under the dashboard or in a channel of
  their own.
- A pinned request panel. Type a title, see whether it's already on Plex, and
  request it, or just the seasons you want, in one tap.
- "Now on Plex" posts when requests become available, naming who asked without
  pinging them.
- A private AI assistant. Seerr AI's own AI is used when you have it, otherwise
  Claude, OpenAI or a local model.
- **Report a problem** (or `/issue`) for anything on Plex that won't play or is
  missing episodes, and `/recap` for a spoiler-free "previously on" (with
  Seerr AI).

See the [README](https://github.com/corruptmods/plexmonitor#readme) to get started.
