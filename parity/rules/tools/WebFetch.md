# WebFetch

Claude Code version: 2.1.312 (baseline session `m1`).

| source | sha256 (normalized) |
|---|---|
| `m1/0060 tools[WebFetch].description` | `e4ecfeb6230606f741bf56a5eb03973f0cfd737b4df34a4b3fbc52042674c97f` |
| `m1/0060 tools[WebFetch].input_schema` | `9c75bdb7d42053d321c6f38515ac7e97267d4fa143694b4914cfa744109a2213` |
| `s1/0022 tools[WebFetch].description` (OAuth variant) | `5ef3e2dcca44992c887221e9e6e1da1077a6f0be6564a2516a0109def8b8b8d5` |

## Purpose and placement

Fetches one URL, turns it into Markdown and answers a prompt about it with a small, fast model
(the side request is in `side-requests.md`). **Deferred**: named in the environment message's
deferred-tool list, absent from `tools` until the model loads it with
`ToolSearch` `select:WebFetch` (`m1/0023`). Once loaded it is sent with `defer_loading: true`,
placed alphabetically among the built-in tools (between `ToolSearch` and `Workflow`), and stays
loaded for the rest of the conversation, across compaction (`m1/0059`) and `--continue`
(`m4/0001`).

noeta equivalent: `noeta/plugins/web/fetch.py`, description `noeta/plugins/web/descriptions/webfetch.md`.
noeta defers it the same way.

## Parameters

JSON Schema 2020-12, `additionalProperties: false`, a description on each property.

| id | name | type | required | enum / default / limits | noeta today |
|---|---|---|---|---|---|
| `tool.WebFetch.p.url` | `url` | string, `format: uri` | yes | the URL to fetch | covered |
| `tool.WebFetch.p.prompt` | `prompt` | string | yes | what to ask about the fetched content | covered |
| `tool.WebFetch.p.offset` | `offset` | integer | no | minimum 0, maximum 2^53-1; character position in the page text to resume from, taken from the previous result, for pages too long for one call | covered |

## Rules

| id | rule | noeta today |
|---|---|---|
| `tool.WebFetch.1` | Fetches the URL, converts it to Markdown and answers `prompt` with a small fast model. | covered |
| `tool.WebFetch.2` | Authenticated or private URLs fail; for those use an authenticated MCP tool or `gh`. | covered |
| `tool.WebFetch.3` | `localhost` and any hostname without a dot fail; for a local server use `curl` through Bash. | covered |
| `tool.WebFetch.4` | `http` is upgraded to `https`. | covered (loopback IP addresses exempt) |
| `tool.WebFetch.5` | A redirect to another host is not followed; it is reported so the model can call again with the new URL. | covered |
| `tool.WebFetch.6` | Responses are cached for 15 minutes per URL. | covered |
| `tool.WebFetch.7` | The description does not mention `WebSearch`. | covered |
| `tool.WebFetch.8` | A URL over 2,000 characters, or carrying a user name or password, is refused as invalid before anything is fetched; a fetch gives up after 60 s and refuses a body over 10 MB (2.1.312 bundle). | covered |

## Result shape

| id | case | shape | noeta today |
|---|---|---|---|
| `tool.WebFetch.r.1` | success | only the small model's answer text (`Example Domain`), no header, no URL/title lines, no source warning | covered |
| `tool.WebFetch.r.3` | error status (4xx/5xx) | not an error result: the status and its reason phrase, a `Retry-After` line when the server sent a numeric one, that the body was not read, and to use a signed-in tool (`gh`, an MCP fetch tool) for a page behind a sign-in (2.1.312 bundle) | covered |
| `tool.WebFetch.r.4` | redirect to another host | not an error result: the original URL, the status and its reason phrase, the target from the `Location` header (marked unverified, cut at 1,000 characters), and the exact call to make next (`url`, `prompt`, and `offset` when one was given); a target that is not http(s), was cut, or has a host name over 255 characters is reported as not fetchable. A redirect that stays on the site (same scheme and port, host equal up to a leading `www.`, no credentials) is followed, at most 10 times (2.1.312 bundle) | covered |
| `tool.WebFetch.r.5` | `offset` at or past the end | not an error result: nothing is left from that offset, and the page text's length (2.1.312 bundle) | covered |
| `tool.WebFetch.r.6` | binary body (PDF, image, ...) | the bytes are saved to a local file and a closing line names the media type, the size and the file's path, so the model can open it with `Read`; the body's text still goes to the digest (2.1.312 bundle) | covered except the digest: the result is only the line naming the file (`deviations.md`); refused when the host gives no spill directory to save into |
| `tool.WebFetch.r.2` | cache marker | in `s1` the result block carried `cache_control` with a 1-hour TTL (OAuth only, not baseline) | n/a (baseline uses no 1-hour TTL here) |

## Variants

- `s1` (OAuth subscriber) appends to `tool.WebFetch.2` that claude.ai artifact links must be read
  with the `Artifact` tool, not WebFetch or curl. Not baseline; n/a for noeta (no claude.ai
  artifacts, spec Non-goals). Schema identical.
