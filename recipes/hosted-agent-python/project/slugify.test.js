const test = require("node:test");
const assert = require("node:assert/strict");
const { slugify } = require("./slugify");

test("joins words with single hyphens", () => {
  assert.equal(slugify("Hello World"), "hello-world");
  assert.equal(slugify("  Many   spaces   here "), "many-spaces-here");
});

test("drops punctuation", () => {
  assert.equal(slugify("Rock & Roll!"), "rock-roll");
  assert.equal(slugify("What's new in 2026?"), "whats-new-in-2026");
});

test("folds accents to plain letters", () => {
  assert.equal(slugify("Crème brûlée"), "creme-brulee");
});

test("returns an empty slug for a title with no letters or digits", () => {
  assert.equal(slugify("!!!"), "");
});
