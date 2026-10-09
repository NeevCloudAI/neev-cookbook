// A shopping cart's totals, in whole cents.

export function subtotal(items) {
  return items.reduce((sum, item) => sum + item.priceCents * item.qty, 0);
}
