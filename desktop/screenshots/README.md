# Screenshots

Taken by the end-to-end test (`npm run e2e`, `e2e/app.spec.ts`): the built app under `xvfb-run`,
starting its own `noeta serve` on the offline scripted provider, so they are rewritten on every run.

| File | Shows |
| --- | --- |
| `01-new-session.png` | a new session in the project folder chosen for it (click the folder to change it) |
| `02-reply.png` | the reply, the session's directory in the header |
| `03-missing-noeta.png` | the window when `noeta` cannot be found: the install line |

## Claude's desktop app features

| Feature | Here |
| --- | --- |
| Starts its own local server; nothing to connect or paste | done: `noeta serve` on a free loopback port with a fresh token per launch, stopped on quit |
| Everything the web app has (sessions, transcript, approvals, pickers, rewind, …) | done: `web/screenshots/README.md` |
| Project folder for new sessions (File > Open Project Folder…, ⌘O) | done |
| Menu: New Session ⌘N, Settings ⌘,, Reload, DevTools (development builds) | done; Settings opens the app's `settings.json` |
| Window size and position remembered; one instance (a second launch focuses it) | done |
| Links open in the browser | done |
| System notification when a session needs you or finishes while the window is in the background; clicking it opens the session | done |
| Light and dark following the OS | done |
| Installers: macOS dmg, Linux AppImage and deb (`npm run dist`) | configured, not built by the tests; not signed or notarized |
| Auto-update, tray icon, global quick-entry shortcut | not yet |
| Windows | not yet (untested) |
| Diff review across a session, file tree, terminal pane | not yet (as in `web/`) |
