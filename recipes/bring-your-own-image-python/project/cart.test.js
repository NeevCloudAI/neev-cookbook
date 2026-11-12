const test = require("node:test");
const assert = require("node:assert/strict");
const { total } = require("./cart.js");

test("adds up price times quantity", () => {
  assert.equal(total([{ price: 250, qty: 2 }, { price: 100, qty: 1 }]), 600);
});

test("an empty cart costs nothing", () => {
  assert.equal(total([]), 0);
});

test("takes 10% off", () => {
  assert.equal(total([{ price: 1000, qty: 2 }], 10), 1800);
});
