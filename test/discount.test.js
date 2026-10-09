import { test } from "node:test";
import assert from "node:assert/strict";
import { applyDiscount } from "../cart.js";

test("SAVE10 takes 10% off", () => {
  assert.equal(applyDiscount(1000, "SAVE10"), 900);
});
