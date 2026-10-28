"""Print the normalized sha256 of a source text in a Claude Code recording.

usage: srchash.py <recordings-dir> <session>/<nnnn> <selector> [<selector> ...]
  selector:  system[i] | tools[Name].description | tools[Name].input_schema
             | messages[i].content[j]   (content[0] for string content)
             | file:<relative path>      (a plain text file in the recordings dir)
Normalization: the recording's cwd, config dir and home are replaced by $CWD, $CFG, $HOME
(longest first), then every run of digits becomes N. Hash = sha256 of the UTF-8 result.
"""
import hashlib, json, re, sys

def norm(text, b):
    ctx = {}
    for sg in b.get("safeguards", []) or []:
        c = sg.get("classifier_context") or {}
        ctx = {"$CWD": c.get("live_cwd"), "$HOME": c.get("home_dir"),
               "$CFG": (c.get("rule_roots") or {}).get("userSettings")}
    for k, v in sorted(ctx.items(), key=lambda kv: -len(kv[1] or "")):
        if v:
            text = text.replace(v, k)
    return re.sub(r"\d+", "N", text)

def pick(b, sel):
    m = re.fullmatch(r"system\[(\d+)\]", sel)
    if m:
        return b["system"][int(m[1])]["text"]
    m = re.fullmatch(r"tools\[(.+)\]\.(description|input_schema)", sel)
    if m:
        t = next(t for t in b["tools"] if t["name"] == m[1])
        v = t[m[2]]
        return v if isinstance(v, str) else json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    m = re.fullmatch(r"messages\[(\d+)\]\.content\[(\d+)\]", sel)
    if m:
        c = b["messages"][int(m[1])]["content"]
        if isinstance(c, str):
            return c
        c = c[int(m[2])]
        if c["type"] == "text":
            return c["text"]
        cc = c.get("content")
        return cc if isinstance(cc, str) else json.dumps(cc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    raise SystemExit("bad selector " + sel)

root, req, *sels = sys.argv[1:]
b = json.load(open(f"{root}/{req}-v1_messages.json"))["body"] if not req.startswith("file") else {}
if not b:
    b = json.load(open(f"{root}/m1/0001-v1_messages.json"))["body"]
for s in sels:
    text = open(f"{root}/{s[5:]}").read() if s.startswith("file:") else pick(b, s)
    print(hashlib.sha256(norm(text, b).encode()).hexdigest(), req, s, len(text))
