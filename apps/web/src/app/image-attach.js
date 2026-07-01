// the single client-side gate for composer image attachments.
// Both image entry points (paste D3 and the picker button, issue 02) share
// ChatComposer's ``ingestImageFiles``. This check is a pure function so ``node --test``
// can cover both reject paths (whitelist + size) directly, and so paste and picker
// run the same verdict (no duplicate implementation).
//
// The type whitelist matches the backend ``ImageInput`` (image/png|jpeg|gif|webp);
// single-image cap is 5MB to stop base64 bloat from blowing up the request body.

// Allowed image MIME types (compared lowercase), matching the backend ImageInput whitelist.
export const ALLOWED_IMAGE_TYPES = Object.freeze([
  "image/png",
  "image/jpeg",
  "image/gif",
  "image/webp",
]);

// Single-image size cap (bytes). 5MB.
export const MAX_IMAGE_BYTES = 5 * 1024 * 1024;

const ALLOWED = new Set(ALLOWED_IMAGE_TYPES);

// Client-side check for a File (or a {type, size}-shaped object). Returns a verdict;
// the caller decides how to surface it:
//   { ok: true,  mediaType }                     → enqueue
//   { ok: false, reason: "type", message, ... }  → type not in whitelist, reject + notice
//   { ok: false, reason: "size", message, ... }  → over 5MB, reject + ask to compress
//   { ok: false, reason: "missing", message }    → empty/invalid file
// mediaType is the normalized (lowercase) type, reused on enqueue so the caller need not recompute.
export function classifyImageFile(file) {
  if (!file) {
    return { ok: false, reason: "missing", message: "Invalid file." };
  }
  const mediaType = String(file.type || "").toLowerCase();
  if (!ALLOWED.has(mediaType)) {
    return {
      ok: false,
      reason: "type",
      mediaType,
      message: `Unsupported image type ${file.type || "(unknown)"}; only PNG / JPEG / GIF / WebP are supported.`,
    };
  }
  if (Number(file.size) > MAX_IMAGE_BYTES) {
    return {
      ok: false,
      reason: "size",
      mediaType,
      message: "This image is over 5MB; please compress it before pasting.",
    };
  }
  return { ok: true, mediaType };
}
