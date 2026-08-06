# Unattended mode

One command makes the channel run itself every weekday. It is not installed by
default, because it schedules a process that sends messages under your name.

```bash
cp launchd/com.osg.linkedin-daily.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.osg.linkedin-daily.plist
launchctl list | grep linkedin        # confirm it is registered
```

From then on, at 09:10 Mon-Fri, `scripts/autopilot.sh --live` runs:
Chrome up (idempotent) → top up today's invites from the backlog → send them,
paced → sweep for acceptances → queue those DMs → send them → heartbeat.
Everything it does is logged to `state/daily.log`.

**It is inert until a campaign has been fired.** With an empty backlog it does
nothing, says `no invite supply left — fire a campaign to refill the backlog`,
and exits 0. Firing stays a deliberate act:

```bash
cd .. && bash tools/scripts/fire_campaign.sh gcc-outreach-li
```

## Watching it

```bash
tail -f state/daily.log                       # what it did, per day
cat state/alerts.log                          # breakers and warnings only
node scripts/heartbeat.js                     # exit 1 = a weekday failed silently
```

## Turning it off

```bash
launchctl unload ~/Library/LaunchAgents/com.osg.linkedin-daily.plist
```

To pause for a while without unloading, quit the LinkedIn Chrome: preflight then
exits 2 each morning and the day is skipped cleanly rather than half-sent.

## Why launchd and not the cron.txt next door

`cron.txt` predates this and should not be installed. macOS cron runs outside
your GUI session, so it cannot open the Chrome window the senders attach to; it
needs Full Disk Access to read anything under `~/Desktop`, where this project
lives, and fails silently without it; and its sender lines wrapped everything in
`flock(1)`, which macOS does not ship — that call exited 127 and, because the
script only checked for exit 1, the failure was swallowed and nothing was ever
sent. A LaunchAgent runs as you, in your session, and can open the browser.
