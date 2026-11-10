// The config a session the page makes starts with.

/** A new session's: the mode and model picked for it, and the project it
 * runs in ("": the server's directory). */
export function newSessionConfig(pick: { mode: string; model: string; project: string }, timezone: string): Record<string, unknown> {
  const config: Record<string, unknown> = { timezone, permission_mode: pick.mode };
  if (pick.model) config.model = pick.model;
  if (pick.project) config.cwd = pick.project;
  return config;
}

/** A session continuing an ended one's conversation (`history_from`). The
 * server carries over what the ended one ran with that the page may not
 * know as it is now (its permission mode, which a plugin may have switched,
 * and the plugins it ran without), so the page names neither. */
export function continuationConfig(source: { model: string; cwd: string }, timezone: string): Record<string, unknown> {
  const config: Record<string, unknown> = { timezone, continued_as: "resume" };
  if (source.model) config.model = source.model;
  if (source.cwd) config.cwd = source.cwd;
  return config;
}
