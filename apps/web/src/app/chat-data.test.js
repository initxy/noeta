import { test } from "node:test";
import assert from "node:assert/strict";

import { projectCacheableMessages, messagesContainUser } from "./chat-data.js";

const userBlob = JSON.stringify([
  {
    __canonical_tag__: "message",
    role: "user",
    content: [{ __canonical_tag__: "text_block", text: "hello" }],
  },
]);

const assistantBlob = JSON.stringify([
  {
    __canonical_tag__: "message",
    role: "assistant",
    content: [{ __canonical_tag__: "text_block", text: "hi" }],
  },
]);

test("message content projection is cacheable only after a valid canonical message array", () => {
  assert.equal(projectCacheableMessages(null), null);
  assert.equal(projectCacheableMessages(""), null);
  assert.equal(projectCacheableMessages("not json"), null);
  assert.equal(projectCacheableMessages(JSON.stringify({ role: "user" })), null);

  const projected = projectCacheableMessages(userBlob);
  assert.equal(Array.isArray(projected.full), true);
  assert.deepEqual(projected.text, [{ role: "user", text: "hello" }]);
});

test("messagesContainUser identifies when the optimistic opening bubble can clear", () => {
  assert.equal(messagesContainUser(projectCacheableMessages(userBlob).full), true);
  assert.equal(messagesContainUser(projectCacheableMessages(assistantBlob).full), false);
  assert.equal(messagesContainUser(null), false);
});
