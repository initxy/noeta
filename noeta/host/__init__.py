"""Running noeta and the ways in from outside, all over the one client API
(``noeta.core.client``): ``LocalApp`` (a store, a worker and a client in one
process), ``http`` (that API over HTTP, what ``noeta serve`` runs) with
``RemoteClient`` its Python client, and ``acp`` (the Agent Client Protocol
editors speak, translated to that API). Imports ``noeta.core`` only."""
