# Changelog

## 1.1.1

A small one that makes the "Now on Plex" posts look a lot better. 🍿

- **Posters are tall now.** They used to be cropped to a small square, so you'd
  often see a face or a corner instead of the art. Now the whole poster shows,
  the way it was meant to look.

Everything else in the post is the same: the title, the summary, who asked for
it, and the **Watch on Plex** and **Open in Seerr** buttons. Nothing to change
on your side.

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
