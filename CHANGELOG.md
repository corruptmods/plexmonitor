# Changelog

## 1.1.0

PlexMonitor now works with every download client Seerr AI supports:
**SABnzbd**, **NZBGet**, **qBittorrent**, **Transmission**, **Deluge** and
**rTorrent**. Use one, or as many as you run.

- The **Downloading** panel shows all of them together, the ones actually
  moving first, with the combined speed.
- Anything that isn't moving says why: paused, queued, checking, getting
  metadata, stalled with no peers, or unpacking and repairing while NZBGet
  finishes up.
- **Completed Downloads** picks up finished jobs from every client.
- New settings for each client (see `.env.example`), plus
  `DOWNLOAD_CATEGORIES` to show only the categories or labels Radarr and
  Sonarr use, and `DOWNLOAD_VERIFY_SSL` for self-signed certificates.
- The `sabnzbd` section of `data/config.json` is now called `downloads`. Your
  old one keeps working.

Updating by copying files over the old ones? You can delete `cogs/sabnzbd.py`;
the bot skips it either way.

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
