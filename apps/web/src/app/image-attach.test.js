// issue 02 — unit tests for the shared client-side image gate.
// Acceptance: cover both reject paths (whitelist and size), proving paste and picker share one verdict.
// Node built-in runner, no new deps: `node --test src/app/image-attach.test.js`.
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  ALLOWED_IMAGE_TYPES,
  MAX_IMAGE_BYTES,
  classifyImageFile,
} from "./image-attach.js";

// A minimal File stand-in (exposes only the type/size fields the check reads).
const fakeFile = (type, size) => ({ type, size });

test("whitelist: PNG / JPEG / GIF / WebP all pass", () => {
  for (const type of ALLOWED_IMAGE_TYPES) {
    const v = classifyImageFile(fakeFile(type, 1024));
    assert.equal(v.ok, true, `${type} should pass`);
    assert.equal(v.mediaType, type);
  }
});

test("whitelist: type is case-insensitive (normalized to lowercase)", () => {
  const v = classifyImageFile(fakeFile("IMAGE/PNG", 10));
  assert.equal(v.ok, true);
  assert.equal(v.mediaType, "image/png");
});

test("reject path 1: type not in whitelist (.bmp) rejected, reason=type, clear notice", () => {
  const v = classifyImageFile(fakeFile("image/bmp", 1024));
  assert.equal(v.ok, false);
  assert.equal(v.reason, "type");
  assert.match(v.message, /Unsupported image type/);
  assert.match(v.message, /PNG \/ JPEG \/ GIF \/ WebP/);
});

test("reject path 1: .heic also not in whitelist, rejected", () => {
  const v = classifyImageFile(fakeFile("image/heic", 1024));
  assert.equal(v.ok, false);
  assert.equal(v.reason, "type");
});

test("reject path 1: empty type (unknown) rejected, notice contains (unknown)", () => {
  const v = classifyImageFile(fakeFile("", 1024));
  assert.equal(v.ok, false);
  assert.equal(v.reason, "type");
  assert.match(v.message, /\(unknown\)/);
});

test("reject path 2: image > 5MB rejected, reason=size, notice asks to compress", () => {
  const v = classifyImageFile(fakeFile("image/png", MAX_IMAGE_BYTES + 1));
  assert.equal(v.ok, false);
  assert.equal(v.reason, "size");
  assert.match(v.message, /5MB/);
  assert.match(v.message, /compress/);
});

test("boundary: exactly 5MB still passes (only strictly over is rejected)", () => {
  const v = classifyImageFile(fakeFile("image/jpeg", MAX_IMAGE_BYTES));
  assert.equal(v.ok, true);
});

test("check order: type before size — an oversize non-whitelist image is rejected by type (no size detail leaked)", () => {
  const v = classifyImageFile(fakeFile("image/bmp", MAX_IMAGE_BYTES + 999));
  assert.equal(v.ok, false);
  assert.equal(v.reason, "type");
});

test("empty / invalid file: reason=missing (caller silently skips, shows no notice)", () => {
  assert.equal(classifyImageFile(null).reason, "missing");
  assert.equal(classifyImageFile(undefined).reason, "missing");
});

test("constants: whitelist and cap (PNG/JPEG/GIF/WebP + 5MB)", () => {
  assert.deepEqual(ALLOWED_IMAGE_TYPES, [
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
  ]);
  assert.equal(MAX_IMAGE_BYTES, 5 * 1024 * 1024);
});
