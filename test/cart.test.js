import { test } from "node:test";
import assert from "node:assert/strict";
import { subtotal } from "../cart.js";

test("subtotal adds price times quantity", () => {
  assert.equal(subtotal([{ priceCents: 250, qty: 2 }, { priceCents: 100, qty: 1 }]), 600);
});

test("subtotal of an empty cart is zero", () => {
  assert.equal(subtotal([]), 0);
});
