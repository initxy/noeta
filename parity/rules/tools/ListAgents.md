# ListAgents

Claude Code 2.1.312 · baseline `m1` · rules in our words (see `../README.md`).

| source | sha256 |
|---|---|
| `m1/0001 tools[ListAgents].description` | `398d4802fc2327352ff22bdb927adf533cb667a3a553b152c5efd19b661f7c20` |
| `m1/0001 tools[ListAgents].input_schema` | `8d54e176ce6cfedd5389c3eef8d7f3cc62a510286a513e72613f0483e43c3b0e` |

## Purpose

Lists the agents the model can address with `SendMessage`. Always loaded (no `defer_loading`);
position 5 of 13 in the baseline list. Never called in the recordings. noeta's is
`ListAgentsTool` (`noeta/plugins/subtasks/tool.py`).

## Parameters

`additionalProperties: false`; nothing required.

| id | name | type | required | enum / default / limits | noeta today |
|---|---|---|---|---|---|
| `tool.ListAgents.p.channel` | `channel` | string | no | maxLength 256; documented as unavailable in this build — leave unset | covered |
| `tool.ListAgents.p.q` | `q` | string | no | maxLength 256; documented as unavailable in this build — leave unset | covered |

## Rules

| id | rule | noeta today |
|---|---|---|
| `tool.ListAgents.1` | Lists every agent reachable by `SendMessage`: sub-agents this session spawned, teammates, other local Claude Code sessions on the machine, the user's cloud sessions (when the session has cloud access) and, with Remote Control connected, the account's sessions on other machines; each row is labelled with its kind. | covered (sub-agents and the store's open root tasks; cloud/remote rows n/a) |
| `tool.ListAgents.2` | A cloud session receives messages but cannot reply yet; do not ask it to answer — read its answer in its own transcript. | n/a (cloud feature) |
| `tool.ListAgents.3` | The row's name is the address: send with `SendMessage({to: "<name>", message: "..."})`, using the listed name character for character. | covered |
| `tool.ListAgents.4` | Append a row's ` [ref]` suffix to the name only when the bare name is ambiguous (two rows share it) or an error asks for disambiguation. | n/a (addresses are task ids, never ambiguous) |

## Result shape

| id | shape (observed) |
|---|---|
| `tool.ListAgents.r.1` | Not observed (never called in m1–m4, s1). |

## Variants

Same hashes in every session.
