import assert from "node:assert/strict";
import { test } from "node:test";

import {
  isPrismLanguageSupported,
  prismLanguage,
  canHighlightSize,
  utf8ByteLength,
} from "./prism-languages.js";

// B18 [P1] — the gate must reflect the grammars ACTUALLY bundled in this
// prism-react-renderer build, not an aspirational map.

test("isPrismLanguageSupported: true only for bundled grammars", () => {
  for (const lang of ["javascript", "jsx", "typescript", "tsx", "python", "go", "rust", "json", "yaml", "markup", "sql", "markdown", "css", "kotlin", "swift", "cpp", "c"]) {
    assert.equal(isPrismLanguageSupported(lang), true, `${lang} should be supported`);
  }
  // NOT bundled in this build — must be rejected (was silently degrading to plain).
  for (const lang of ["bash", "ruby", "java", "csharp", "php", "toml", "ini", "scss", "sass", "less", "diff", "mermaid", "nonsense"]) {
    assert.equal(isPrismLanguageSupported(lang), false, `${lang} should NOT be supported`);
  }
  assert.equal(isPrismLanguageSupported(null), false);
  assert.equal(isPrismLanguageSupported(""), false);
});

test("prismLanguage: supported fences resolve, unsupported fall back to plain (null)", () => {
  assert.equal(prismLanguage("js"), "javascript");
  assert.equal(prismLanguage("javascript"), "javascript");
  assert.equal(prismLanguage("ts"), "typescript");
  assert.equal(prismLanguage("tsx"), "tsx");
  assert.equal(prismLanguage("py"), "python");
  assert.equal(prismLanguage("go"), "go");
  assert.equal(prismLanguage("rust"), "rust");
  assert.equal(prismLanguage("json"), "json");
  assert.equal(prismLanguage("yaml"), "yaml");
  assert.equal(prismLanguage("html"), "markup");
  assert.equal(prismLanguage("js copy"), "javascript"); // multi-word info string → first token
  // unsupported / unknown → null → caller renders plain, never hands prism a bad grammar
  for (const fence of ["bash", "sh", "shell", "zsh", "ruby", "rb", "java", "php", "cs", "diff", "mermaid", "toml", "ini", "scss", "less", "", null, "totally-unknown"]) {
    assert.equal(prismLanguage(fence), null, `${fence} must fall back to plain`);
  }
});

test("prismLanguage: case + whitespace tolerant on the bare hint", () => {
  assert.equal(prismLanguage("  Python  "), "python");
  assert.equal(prismLanguage("JS"), "javascript");
});

// B18 [P2] — the 200KB gate must measure UTF-8 bytes, not characters.

test("canHighlightSize: small ASCII passes", () => {
  assert.equal(canHighlightSize("const x = 1;\n".repeat(50)), true);
  assert.equal(canHighlightSize(""), true);
  assert.equal(canHighlightSize(123), false); // non-string
});

test("canHighlightSize: rejects >1500 lines", () => {
  assert.equal(canHighlightSize("a\n".repeat(1501)), false);
});

test("canHighlightSize: non-ASCII over 200KB is rejected on BYTES not length", () => {
  // 70k CJK chars: length 70k (< 200*1024) but UTF-8 ~210KB (> 200*1024).
  const cjk = "中".repeat(70000);
  assert.ok(cjk.length < 200 * 1024, "char length is under the cap");
  assert.ok(utf8ByteLength(cjk) > 200 * 1024, "utf-8 bytes exceed the cap");
  assert.equal(canHighlightSize(cjk), false); // must reject by byte size
  // same character count but ASCII stays under the byte cap → allowed
  assert.equal(canHighlightSize("a".repeat(70000)), true);
});

test("canHighlightSize: exactly 200KB is rejected (>= cap, matches shouldHighlight)", () => {
  const cap = 200 * 1024;
  assert.equal(canHighlightSize("a".repeat(cap)), false); // exactly at the cap → plain
  assert.equal(canHighlightSize("a".repeat(cap - 1)), true); // one under → allowed
});
