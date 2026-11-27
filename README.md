# SMTC Discord RPC

A tiny Windows tray app that shows whatever you're playing as a **Spotify-style "Listening to" card on Discord**, with album art and a live progress bar. It works with anything that reports to the Windows media overlay (Spotify, YouTube in the browser, local players, etc.).

It reads your playback from **[SMTC Bridge](https://github.com/nuttylmao/smtc-bridge) by nutty**, so that needs to be running too.

## Quick start

1. Run **SMTC Bridge** (see its [releases](https://github.com/nuttylmao/smtc-bridge/releases)).
2. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and click **New Application**.
   The application's **name** is what shows after "Listening to", so name it whatever you like.
3. Copy the **Application ID** from the app's General Information page.
4. Open **SMTC-Discord-RPC.exe**, paste the ID, and hit **Start**.

That's it. Closing the window sends it to the system tray; right-click the tray icon to quit.

## Settings

| Option | What it does |
| --- | --- |
| Music only | Ignores video sessions (YouTube, Twitch, etc.) |
| Fetch album art | Looks up cover art through the free iTunes Search API. Turn off to skip the lookup. |
| Start presence automatically | Starts as soon as the app opens (needs a saved Application ID) |
| Start with Windows | Launches minimized to the tray on login |
| Ignore apps | Comma-separated names, e.g. `spotify, chrome`. Handy if you don't want to double up with Discord's native Spotify presence. |
| SMTC Bridge URL | Only change this if you moved the bridge off port 5000 |

Settings are saved to `%APPDATA%\SMTC Discord RPC\config.json`.

## Run from source

```
pip install -r requirements.txt
python smtc_discord_rpc.py
```

## Build the exe

Double-click `build.bat` (or run it in a terminal). The exe lands in `dist\SMTC-Discord-RPC.exe`.

To use your own icon, replace `icon.ico` with a multi-size `.ico` (16, 32, 48, 64, 128, 256 px) and rebuild.

## Troubleshooting

- **"Can't reach SMTC Bridge"**: start the bridge, or check the URL in settings.
- **"Waiting for Discord"**: the Discord *desktop* app must be open (the web version doesn't support Rich Presence).
- **Nothing shows up on your profile**: Discord Settings > Activity Privacy > turn on "Share your detected activities with others".
- **Moved the exe?** Toggle "Start with Windows" off and on again so it points at the new location.
- Discord only accepts a presence update about every 15 seconds, so skips and seeks can take a moment to show.

## Credits

Built on [SMTC Bridge](https://github.com/nuttylmao/smtc-bridge) by nutty. This project isn't affiliated with Discord or SMTC Bridge.
