import { describe, expect, it } from 'vitest'
import { isValidAlias, parseEnvLines, parseHeaderLines, splitArgs } from './mcp'

describe('parseHeaderLines', () => {
  it('empty text → empty record', () => {
    expect(parseHeaderLines('')).toEqual({ ok: true, value: {} })
    expect(parseHeaderLines('  \n\n  ')).toEqual({ ok: true, value: {} })
  })

  it('parses "Name: value" lines, trimming both sides', () => {
    const r = parseHeaderLines('Authorization: Bearer abc\n  X-Team :  data ')
    expect(r.ok).toBe(true)
    expect(r.value).toEqual({ Authorization: 'Bearer abc', 'X-Team': 'data' })
  })

  it('keeps colons inside the value (only the first separates)', () => {
    const r = parseHeaderLines('X-Url: https://example.com:8080/path')
    expect(r.value).toEqual({ 'X-Url': 'https://example.com:8080/path' })
  })

  it('reports the first malformed line', () => {
    const r = parseHeaderLines('Authorization: ok\nnot-a-header')
    expect(r.ok).toBe(false)
    expect(r.badLine).toBe('not-a-header')
  })

  it('rejects a line with an empty name', () => {
    expect(parseHeaderLines(': value').ok).toBe(false)
  })
})

describe('parseEnvLines', () => {
  it('parses "NAME=value" lines; the value may contain "="', () => {
    const r = parseEnvLines('API_TOKEN=abc\nQUERY=a=b')
    expect(r.ok).toBe(true)
    expect(r.value).toEqual({ API_TOKEN: 'abc', QUERY: 'a=b' })
  })

  it('reports a line without "="', () => {
    const r = parseEnvLines('JUST_A_NAME')
    expect(r.ok).toBe(false)
    expect(r.badLine).toBe('JUST_A_NAME')
  })
})

describe('splitArgs', () => {
  it('splits on any whitespace and drops blanks', () => {
    expect(splitArgs('')).toEqual([])
    expect(splitArgs('  -y   --port 8080 ')).toEqual(['-y', '--port', '8080'])
  })
})

describe('isValidAlias', () => {
  it('accepts the SDK alias charset within 32 chars', () => {
    expect(isValidAlias('github')).toBe(true)
    expect(isValidAlias('my-server_2')).toBe(true)
  })

  it('rejects uppercase, spaces, empty, and overlong aliases', () => {
    expect(isValidAlias('GitHub')).toBe(false)
    expect(isValidAlias('my server')).toBe(false)
    expect(isValidAlias('')).toBe(false)
    expect(isValidAlias('a'.repeat(33))).toBe(false)
  })
})
