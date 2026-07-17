import { describe, it, expect } from 'vitest'
import {
  ALLOWED_IMAGE_TYPES,
  MAX_IMAGE_BYTES,
  base64FromDataUrl,
  classifyImageFile,
  dataUrlFromAttachment,
  imageFilesFromDataTransfer,
  toAttachmentPayload,
  type PendingImage,
} from './imageAttach'

describe('constants match the backend contract', () => {
  it('whitelist is PNG / JPEG / GIF / WebP', () => {
    expect([...ALLOWED_IMAGE_TYPES].sort()).toEqual([
      'image/gif',
      'image/jpeg',
      'image/png',
      'image/webp',
    ])
  })

  it('single-image cap is 5MB', () => {
    expect(MAX_IMAGE_BYTES).toBe(5 * 1024 * 1024)
  })
})

describe('classifyImageFile', () => {
  it('accepts every whitelisted type and normalizes case', () => {
    for (const type of ALLOWED_IMAGE_TYPES) {
      expect(classifyImageFile({ type, size: 10 })).toEqual({
        ok: true,
        mediaType: type,
      })
    }
    expect(classifyImageFile({ type: 'IMAGE/PNG', size: 10 })).toEqual({
      ok: true,
      mediaType: 'image/png',
    })
  })

  it('rejects non-whitelisted types with a type verdict', () => {
    for (const type of ['image/svg+xml', 'text/plain', 'application/pdf', '']) {
      const verdict = classifyImageFile({ type, size: 10 })
      expect(verdict.ok).toBe(false)
      if (!verdict.ok) {
        expect(verdict.reason).toBe('type')
        expect(verdict.message).toContain('PNG / JPEG / GIF / WebP')
      }
    }
  })

  it('rejects over-5MB files with a size verdict; exactly 5MB passes', () => {
    const over = classifyImageFile({
      type: 'image/png',
      size: MAX_IMAGE_BYTES + 1,
    })
    expect(over).toMatchObject({ ok: false, reason: 'size' })
    expect(
      classifyImageFile({ type: 'image/png', size: MAX_IMAGE_BYTES }),
    ).toEqual({ ok: true, mediaType: 'image/png' })
  })

  it('rejects a missing file', () => {
    expect(classifyImageFile(null)).toMatchObject({
      ok: false,
      reason: 'missing',
    })
    expect(classifyImageFile(undefined)).toMatchObject({
      ok: false,
      reason: 'missing',
    })
  })
})

describe('imageFilesFromDataTransfer', () => {
  const item = (kind: string, type: string, file: unknown) => ({
    kind,
    type,
    getAsFile: () => file as File | null,
  })

  it('picks image files, skipping text items and non-image files', () => {
    const png = { name: 'shot.png' }
    const files = imageFilesFromDataTransfer({
      items: [
        item('string', 'text/plain', null),
        item('file', 'image/png', png),
        item('file', 'application/pdf', { name: 'doc.pdf' }),
      ],
    })
    expect(files).toEqual([png])
  })

  it('tolerates a null DataTransfer and null files', () => {
    expect(imageFilesFromDataTransfer(null)).toEqual([])
    expect(
      imageFilesFromDataTransfer({ items: [item('file', 'image/png', null)] }),
    ).toEqual([])
  })
})

describe('base64 payload helpers', () => {
  it('base64FromDataUrl extracts the payload and rejects non-base64 URLs', () => {
    expect(base64FromDataUrl('data:image/png;base64,AAAA')).toBe('AAAA')
    expect(base64FromDataUrl('data:text/plain,hello')).toBe('')
    expect(base64FromDataUrl('garbage')).toBe('')
  })

  it('dataUrlFromAttachment rebuilds the preview URL', () => {
    expect(
      dataUrlFromAttachment({ media_type: 'image/webp', data_base64: 'AAAA' }),
    ).toBe('data:image/webp;base64,AAAA')
  })

  it('toAttachmentPayload maps pending images to the request shape', () => {
    const pending: PendingImage[] = [
      {
        id: 1,
        mediaType: 'image/png',
        dataBase64: 'AAAA',
        dataUrl: 'data:image/png;base64,AAAA',
        name: 'a.png',
      },
    ]
    expect(toAttachmentPayload(pending)).toEqual([
      { media_type: 'image/png', data_base64: 'AAAA' },
    ])
    expect(toAttachmentPayload([])).toEqual([])
  })
})
