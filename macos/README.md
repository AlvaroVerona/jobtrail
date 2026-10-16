# jobtrail for macOS

A native Mac app for jobtrail: double-click it, get a window with a Dock icon and menus, no Terminal. It is a thin shell. On launch it starts the jobtrail server from this project and shows its web UI in the window; when you quit, it stops the server.

## Build and install

You need a Mac with Xcode 16+ (Swift 6) and [uv](https://docs.astral.sh/uv/). From the repository root:

```bash
macos/Scripts/build-app.sh                       # builds macos/.build/app/jobtrail.app
open macos/.build/app/jobtrail.app               # run it
cp -R macos/.build/app/jobtrail.app /Applications/   # or install it
```

The script runs `uv sync` first if the project is not installed yet, compiles the app with Swift Package Manager (there is no Xcode project), assembles the `.app`, and ad-hoc signs it.

The app remembers which project folder it was built from, so **it runs that checkout**. If you move the folder, run the script again. It is built from source for the Mac that built it; it is not notarized and not meant to be handed to someone else.

AI features still need [Ollama](https://ollama.com) running, exactly as in the browser version.

## Using it

| | |
|---|---|
| ⌘1 to ⌘7 | Go to Board, Applications, Postings, CV, Follow-ups, Analytics, Settings |
| ⌘R, ⌘[ , ⌘] | Reload, Back, Forward (two-finger swipe also goes back) |
| ⌘+ ⌘- ⌘0 | Zoom |
| Server ▸ Open in Browser (⇧⌘O) | The same page in your default browser |
| Server ▸ Open Data Folder / Show Server Log | Where your data and the log live |
| Server ▸ Check Reminders Now | Look for due reminders right away |
| Server ▸ Restart Server (⇧⌘R) | Start a fresh server |

Closing the window quits the app and stops the server. Links to other sites (a posting's original page) open in your default browser, not in the window.

**Your data** is in the project's `data/` folder, the same database `uv run jobtrail serve` uses, so the app and the command line share it. To keep it elsewhere: `defaults write com.alvaroverona.jobtrail dataDir /path/to/folder`. **The server log** is `~/Library/Logs/jobtrail/server.log`.

**Reminders.** Every five minutes the app asks the server what is due today or overdue, shows the number on the Dock icon, and posts one notification per reminder per day (clicking it opens the person or application). macOS asks for notification permission the first time. It only reads; it never changes your data.

If the app cannot start the server it says why (for example, run `uv sync`) and offers *Try again*, which re-checks everything, so fixing the problem does not need a relaunch.

## Troubleshooting

* **"jobtrail could not start"**: the message says why. The common one is a project that was never installed (`uv sync`); fix it and press *Try again*. *Show server log* opens `~/Library/Logs/jobtrail/server.log`.
* **Slow first start after a rebuild.** Once, right after rebuilding, the server stayed silent for about 25 seconds before answering, and I could not reproduce it in five later launches. I do not know the cause; macOS asking about access to a protected folder such as Documents (ad-hoc signed apps are treated as new after each build) is one guess, so if you see a permission prompt, answer it. If the window gives up after 30 seconds, *Try again* works.
* **No notifications.** Check System Settings ▸ Notifications ▸ jobtrail. Delivery of notifications has not been verified end to end by the author (the Dock badge and the reminder logic have).

## How it works

* A free local port is chosen on every launch, so it never clashes with `uv run jobtrail serve` on 8000.
* The server is started from the project's own virtualenv (`.venv/bin/jobtrail`), because an app opened from Finder does not inherit your shell's `PATH` and so cannot find `uv`.
* The app passes its own process id to the server (`JOBTRAIL_PARENT_PID`); the server exits within a couple of seconds if the app disappears. So a force-quit or a crash cannot leave an orphan server running. This was tested with `kill -9`.
* The window is a `WKWebView`. WKWebView shows no dialogs by itself, so the app implements them (the "Delete?" confirmations would otherwise silently do nothing) and the file picker for the CV upload.
* The web server's own protections still apply: it only answers `127.0.0.1` and refuses cross-site writes.

### Why not a full SwiftUI rewrite?

The web UI already covers every screen and the AI guards live in the Python server. A native rewrite would mean a JSON API plus about nine screens of SwiftUI, for the same behaviour. The shell keeps one codebase and the app can never drift from the web version. Screens can still be made native one at a time later if one is worth it.

## Tests

```bash
cd macos && swift test      # 39 tests
```

They cover the parts that carry risk: resolving the project and its errors, the server environment, which URLs may load in the window, finding a free port, which reminders to announce and when, and the server lifecycle using real throwaway processes (start, health wait, early exit, timeout, later crash, restart, and that shutdown really kills the process). The views themselves are not unit-tested.

`Scripts/generate_icon.swift` regenerates `Resources/AppIcon.icns`; the result is committed.
