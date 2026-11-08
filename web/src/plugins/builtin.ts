import type { PluginModule } from "@initxy/web-sdk";
import * as compaction from "./compaction";
import * as findings from "./findings";
import * as fs from "./fs";
import * as hooks from "./hooks";
import * as mcp from "./mcp";
import * as memory from "./memory";
import * as permissions from "./permissions";
import * as plan_mode from "./plan_mode";
import * as shell from "./shell";
import * as subtasks from "./subtasks";
import * as title from "./title";
import * as todos from "./todos";
import * as tool_search from "./tool_search";
import * as web from "./web";
import * as workflow from "./workflow";

/** The web parts of plugins noeta ships, by plugin name: each under
 * web/src/plugins/<name>/, loaded through the same ctx and slots a
 * third-party web part gets (importing only react and `@initxy/*`), and
 * only while the task runs that plugin. */
export const BUILTIN_WEB_PARTS: Record<string, PluginModule> = {
  compaction,
  findings,
  fs,
  hooks,
  mcp,
  memory,
  permissions,
  plan_mode,
  shell,
  subtasks,
  title,
  todos,
  tool_search,
  web,
  workflow,
};
