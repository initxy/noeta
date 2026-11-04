// Runs sandboxed before the page: hands it its server and token
// (`window.NOETA_CONFIG`) and the desktop hooks (`window.noetaDesktop`).
// A sandboxed preload can require only "electron": keep this file self-contained.
import { contextBridge, ipcRenderer, type IpcRendererEvent } from "electron";

const config: unknown = ipcRenderer.sendSync("noeta:config");
contextBridge.exposeInMainWorld("NOETA_CONFIG", config);

type Command = { name: string; taskId?: string; path?: string };

contextBridge.exposeInMainWorld("noetaDesktop", {
  platform: process.platform,
  project: (): Promise<string> => ipcRenderer.invoke("noeta:project"),
  chooseProject: (): Promise<string | null> => ipcRenderer.invoke("noeta:choose-project"),
  notify: (n: { title: string; body: string; taskId?: string }): void => ipcRenderer.send("noeta:notify", n),
  onCommand: (cb: (c: Command) => void): (() => void) => {
    const listener = (_e: IpcRendererEvent, c: Command) => cb(c);
    ipcRenderer.on("noeta:command", listener);
    return () => ipcRenderer.removeListener("noeta:command", listener);
  },
});
