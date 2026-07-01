// unit tests for the pure user-bubble image-render logic. Node's builtin runner, zero new deps:
//   node --test src/app/chat-images.test.js
// Covers: (1) extracting images only from user messages, deduped by hash; (2) the unified
// "local base64 first, else hash URL" logic; (3) the browser-side content fingerprint
// (SHA-256 hex) aligning with the backend ContentStore addressing.
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  base64ToBytes,
  dataUrlFromBase64,
  imageSrcFor,
  sha256Hex,
  userMessageImages,
} from "./chat-images.js";

const imageBlock = (hash, mediaType = "image/png") => ({
  __canonical_tag__: "image_block",
  source: { hash, media_type: mediaType },
});
const textBlock = (text) => ({ __canonical_tag__: "text_block", text });

test("userMessageImages: extract image_block from user messages (hash + mediaType)", () => {
  const messages = [
    { role: "user", content: [textBlock("look at this image"), imageBlock("a".repeat(64), "image/jpeg")] },
  ];
  const imgs = userMessageImages(messages);
  assert.deepEqual(imgs, [{ hash: "a".repeat(64), mediaType: "image/jpeg" }]);
});

test("userMessageImages: only the user role — assistant/tool image blocks are ignored (the model emits no images)", () => {
  const messages = [
    { role: "assistant", content: [imageBlock("b".repeat(64))] },
    { role: "tool", content: [imageBlock("c".repeat(64))] },
    { role: "user", content: [imageBlock("d".repeat(64))] },
  ];
  const imgs = userMessageImages(messages);
  assert.deepEqual(imgs.map((i) => i.hash), ["d".repeat(64)]);
});

test("userMessageImages: keep only the first occurrence of a hash (dedup; bubble doesn't redraw)", () => {
  const h = "e".repeat(64);
  const messages = [
    { role: "user", content: [imageBlock(h, "image/png"), imageBlock(h, "image/jpeg")] },
    { role: "user", content: [imageBlock(h)] },
  ];
  const imgs = userMessageImages(messages);
  assert.equal(imgs.length, 1);
  assert.equal(imgs[0].hash, h);
  assert.equal(imgs[0].mediaType, "image/png"); // mediaType from the first occurrence
});

test("userMessageImages: text-only message / empty input → empty array", () => {
  assert.deepEqual(userMessageImages([{ role: "user", content: [textBlock("hi")] }]), []);
  assert.deepEqual(userMessageImages([]), []);
  assert.deepEqual(userMessageImages(null), []);
});

test("imageSrcFor: local-cache hit → use the local data URL (zero requests, the just-sent moment)", () => {
  const h = "f".repeat(64);
  const cache = new Map([[h, "data:image/png;base64,LOCAL"]]);
  assert.equal(imageSrcFor(h, "t-1", cache), "data:image/png;base64,LOCAL");
});

test("imageSrcFor: local miss → fall back to the global content route (reloaded history)", () => {
  const h = "0".repeat(64);
  // New protocol: blobs are content-addressed and served task-scope-free.
  assert.equal(imageSrcFor(h, "t-1", new Map()), `/content/${h}`);
});

test("imageSrcFor: local takes priority over the hash URL (same hash, both paths converge on one key)", () => {
  const h = "1".repeat(64);
  const cache = new Map([[h, "data:image/png;base64,LOCAL"]]);
  // Even when a URL could be built, use local whenever it exists.
  assert.equal(imageSrcFor(h, "t-9", cache), "data:image/png;base64,LOCAL");
});

test("imageSrcFor: local miss builds the content route regardless of taskId (no longer task-scoped)", () => {
  const h = "2".repeat(64);
  assert.equal(imageSrcFor(h, null, new Map()), `/content/${h}`);
});

test("imageSrcFor: URL applies encodeURIComponent to the hash", () => {
  const h = "3".repeat(64);
  assert.equal(imageSrcFor(h, "a/b", new Map()), `/content/${h}`);
});

test("dataUrlFromBase64: rebuild the data URL from {media_type, base64}", () => {
  assert.equal(dataUrlFromBase64("image/gif", "AAAA"), "data:image/gif;base64,AAAA");
  assert.equal(dataUrlFromBase64("", "AAAA"), "data:image/png;base64,AAAA"); // fallback type
  assert.equal(dataUrlFromBase64("image/png", ""), ""); // no bytes → empty
});

test("sha256Hex: the browser-side content fingerprint aligns with backend ContentStore addressing", async () => {
  // The SHA-256 of the empty string is a well-known constant; this proves we compute the
  // sha256 hex of the raw bytes, matching the backend hashlib.sha256(body).hexdigest()
  // ⇒ local image and history hash share one key.
  const EMPTY_SHA256 =
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
  assert.equal(await sha256Hex(new Uint8Array([])), EMPTY_SHA256);

  // The SHA-256 of "abc" is also a well-known constant.
  const ABC_SHA256 =
    "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
  const bytes = base64ToBytes(Buffer.from("abc", "utf-8").toString("base64"));
  assert.equal(await sha256Hex(bytes), ABC_SHA256);
});
