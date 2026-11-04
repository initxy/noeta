# Screenshots

Taken by the end-to-end test (`npm run e2e`, `e2e/app.spec.ts`): the built app under `xvfb-run`,
starting its own `noeta serve` on the offline scripted provider, so they are rewritten on every run.

| File | Shows |
| --- | --- |
| `01-new-session.png` | a new session in the project folder chosen for it (click the folder to change it) |
| `02-reply.png` | the reply, the session's directory in the header |
| `03-missing-noeta.png` | the window when `noeta` cannot be found: the install line |
| `04-server-stopped.png` | the window when the local server died: its last output; View > Reload starts it again |

## Claude's desktop app features

| Feature | Here |
| --- | --- |
| Starts its own local server; nothing to connect or paste | done: `noeta serve` on a free loopback port with a fresh token per launch, stopped on quit |
| Everything the web app has (sessions, transcript, approvals, pickers, rewind, …) | done: `web/screenshots/README.md` |
| Project folder for new sessions (File > Open Project Folder…, ⌘O) | done |
| Menu: New Session ⌘N, Settings ⌘,, Reload, DevTools (development builds) | done; Settings opens the app's `settings.json`; Reload restarts a server that stopped |
| A local server that dies is noticed | done: its last output, and Reload to restart; a folder outside the home directory asks before restarting over running sessions |
| Window size and position remembered; one instance (a second launch focuses it) | done |
| Links open in the browser | done: only the app's page stays in the window; the server's other paths are refused; the page may only ask for notifications and clipboard writes |
| System notification when a session needs you or finishes while the window is in the background; clicking it opens the session | done |
| Light and dark following the OS | done |
| Installers: macOS dmg, Linux AppImage and deb (`npm run dist`) | configured, not built by the tests; not signed or notarized |
| Auto-update, tray icon, global quick-entry shortcut | not yet |
| Windows | not yet (untested) |
| Diff review across a session, file tree, terminal pane | not yet (as in `web/`) |

Regenerate with `NOETA_SCREENSHOTS=1 make desktop`.
