// A shopping cart's totals, in whole cents.

export function subtotal(items) {
  return items.reduce((sum, item) => sum + item.priceCents * item.qty, 0);
}

// Applies a discount code such as "SAVE10" (10% off) to a total in cents.
export function applyDiscount(totalCents, code) {
  const percent = Number(code.replace("SAVE", ""));
  return totalCents - (totalCents * percent) / 100;
}
