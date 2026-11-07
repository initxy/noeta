import type { PluginModule } from "@noeta-ai/web-sdk";

/** The web parts of plugins noeta ships, by plugin name: each under
 * web/src/plugins/<name>/, loaded through the same ctx and slots a
 * third-party web part gets, and only while the task runs that plugin. */
export const BUILTIN_WEB_PARTS: Record<string, PluginModule> = {};
