// pure logic for rendering images in user bubbles (React-free, so node --test can cover it directly).
//
// A user-sent image has two byte-fetch paths in the frontend; this module unifies them
// under **one key: the content fingerprint hash**:
//   1. Just sent: the pasted/picked base64 is in hand locally — display it directly, zero extra requests.
//   2. Reloaded history: the local copy is gone — fetch by hash via the backend route ``/tasks/{id}/images/{hash}``.
// The key fact: the backend ContentStore is content-addressed (``hash = sha256(raw bytes)`` in hex),
// so computing SHA-256 over the same bytes in the browser before sending yields a hash
// that necessarily equals the ledger's ``ImageBlock(ContentRef).hash``. The "just-sent local
// image" and the "history hash" thus land on the same key: at render time, ``use the local
// cache if present, otherwise build the hash URL`` — both paths converge in one bubble logic.

// The content block array of a canonical message (tolerant: a bare array also works).
function blocksOf(message) {
  if (!message) return [];
  if (Array.isArray(message.content)) return message.content;
  if (Array.isArray(message)) return message;
  return [];
}

// Extract image blocks from **user messages** in a set of canonical messages; returns [{hash, mediaType}].
// Only role==="user" (the model emits no images, D1; even if assistant/tool image-shaped blocks
// appear, they are out of scope for this fetch path — matching the backend
// ``collect_task_image_refs`` "user-only" rule).
// Deduplicate in order of appearance (keep first per hash; don't draw the same image twice in a bubble).
export function userMessageImages(messages) {
  const out = [];
  const seen = new Set();
  for (const message of Array.isArray(messages) ? messages : []) {
    if (!message || message.role !== "user") continue;
    for (const block of blocksOf(message)) {
      if (!block || block.__canonical_tag__ !== "image_block") continue;
      const source = block.source || block;
      const hash = source && typeof source.hash === "string" ? source.hash : null;
      if (!hash || seen.has(hash)) continue;
      seen.add(hash);
      out.push({ hash, mediaType: source.media_type || source.mediaType || "" });
    }
  }
  return out;
}

// data:<media_type>;base64,<payload> — rebuild the local-preview data URL from
// {media_type, data_base64} (on submit we drop the dataUrl to save bytes, keeping only
// base64 + type; reconstruct on demand here, without writing back to the ledger).
export function dataUrlFromBase64(mediaType, dataBase64) {
  if (!dataBase64) return "";
  return `data:${mediaType || "image/png"};base64,${dataBase64}`;
}

// base64 → raw bytes (Uint8Array). Used to compute the content fingerprint in the browser
// (matching the backend's bytes). Uses atob (browser builtin); in non-browser environments
// (node --test) the caller passes Buffer-decoded bytes or skips.
export function base64ToBytes(dataBase64) {
  const binary = atob(dataBase64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

// hex(sha256(bytes)), aligned with the backend's content addressing ``hashlib.sha256(body).hexdigest()``.
// Uses Web Crypto (``crypto.subtle.digest``); returns lowercase 64-char hex.
export async function sha256Hex(bytes) {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

// Unified image fetch: on local-cache hit use the local data URL (zero
// requests), otherwise build the backend hash route. ``localCache`` is
// Map<hash, dataUrl>. The new protocol serves blobs from the global,
// content-addressed GET /content/{hash} (T6) — no task scope — and the route
// sniffs the media type from the magic bytes. ``taskId`` is accepted for call-
// site compatibility but no longer part of the URL.
export function imageSrcFor(hash, taskId, localCache) {
  if (!hash) return "";
  const local = localCache && typeof localCache.get === "function"
    ? localCache.get(hash)
    : localCache && localCache[hash];
  if (local) return local;
  return `/content/${encodeURIComponent(hash)}`;
}
