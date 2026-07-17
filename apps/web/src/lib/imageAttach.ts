/**
 * The single client-side gate for composer image attachments.
 *
 * All three entry points (picker button, paste, drag-drop) share this module,
 * so every path runs the same verdict — no duplicate validation. Pure
 * (React-free) so vitest can cover the reject paths directly.
 *
 * The type whitelist and 5MB cap match the backend
 * `noeta.agent.api.image_input` (`ALLOWED_IMAGE_TYPES` / `MAX_IMAGE_BYTES`):
 * the client rejects early with a toast, the backend re-validates with a 400.
 */

import type { ImageAttachment } from '../api/types'

/** Allowed image MIME types (compared lowercase), matching the backend whitelist. */
export const ALLOWED_IMAGE_TYPES: readonly string[] = Object.freeze([
  'image/png',
  'image/jpeg',
  'image/gif',
  'image/webp',
])

/** Single-image size cap (bytes). 5MB. */
export const MAX_IMAGE_BYTES = 5 * 1024 * 1024

const ALLOWED = new Set(ALLOWED_IMAGE_TYPES)

export type ImageVerdict =
  | { ok: true; mediaType: string }
  | { ok: false; reason: 'type' | 'size' | 'missing'; message: string }

/**
 * Client-side check for a File (or a {type, size}-shaped object). Returns a
 * verdict; the caller decides how to surface a rejection (toast). `mediaType`
 * is the normalized (lowercase) type, reused on enqueue.
 */
export function classifyImageFile(
  file: { type?: string; size?: number } | null | undefined,
): ImageVerdict {
  if (!file) {
    return { ok: false, reason: 'missing', message: 'Invalid file.' }
  }
  const mediaType = String(file.type || '').toLowerCase()
  if (!ALLOWED.has(mediaType)) {
    return {
      ok: false,
      reason: 'type',
      message: `Unsupported image type ${file.type || '(unknown)'}; only PNG / JPEG / GIF / WebP are supported.`,
    }
  }
  if (Number(file.size) > MAX_IMAGE_BYTES) {
    return {
      ok: false,
      reason: 'size',
      message: 'This image is over 5MB; please compress it before attaching.',
    }
  }
  return { ok: true, mediaType }
}

/**
 * The image Files carried by a DataTransfer (paste / drop). Non-file items
 * and non-image files are skipped — validation proper is classifyImageFile's
 * job; this only picks candidates so paste keeps plain text pasting intact.
 */
export function imageFilesFromDataTransfer(
  data: {
    items?: ArrayLike<{ kind: string; type: string; getAsFile(): File | null }>
  } | null,
): File[] {
  const out: File[] = []
  const items = data?.items
  if (!items) return out
  for (let i = 0; i < items.length; i += 1) {
    const item = items[i]
    if (item.kind !== 'file' || !item.type.startsWith('image/')) continue
    const file = item.getAsFile()
    if (file) out.push(file)
  }
  return out
}

/** `data:<type>;base64,<payload>` → the bare base64 payload ('' when malformed). */
export function base64FromDataUrl(dataUrl: string): string {
  const comma = dataUrl.indexOf(',')
  if (comma < 0 || !dataUrl.slice(0, comma).endsWith(';base64')) return ''
  return dataUrl.slice(comma + 1)
}

/** Rebuild a local-preview data URL from an attachment payload. */
export function dataUrlFromAttachment(image: ImageAttachment): string {
  return `data:${image.media_type || 'image/png'};base64,${image.data_base64}`
}

/** A validated attachment queued in the composer, with its local preview. */
export interface PendingImage {
  /** Render key (local, monotonically assigned by the composer). */
  id: number
  mediaType: string
  dataBase64: string
  /** Local preview data URL. */
  dataUrl: string
  name: string
}

/** Pending images → the request payload shape ({media_type, data_base64}[]). */
export function toAttachmentPayload(images: PendingImage[]): ImageAttachment[] {
  return images.map((img) => ({
    media_type: img.mediaType,
    data_base64: img.dataBase64,
  }))
}
