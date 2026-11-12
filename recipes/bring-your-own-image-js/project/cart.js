// Total price of a cart in cents, after a percentage discount.
function total(items, discountPercent = 0) {
  const subtotal = items.reduce((sum, item) => sum + item.price * item.qty, 0);
  return Math.round(subtotal - discountPercent);
}

module.exports = { total };
