-- Give every customer an email address, taken from their orders.
-- SQLite cannot add a NOT NULL column without a default, so the table is rebuilt.
ALTER TABLE customers RENAME TO customers_old;
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT NOT NULL,
    email TEXT NOT NULL
);
INSERT INTO customers (id, name, city, email)
SELECT c.id, c.name, c.city, o.contact_email
FROM customers_old c
JOIN (SELECT customer_id, MAX(contact_email) AS contact_email FROM orders GROUP BY customer_id) o
    ON o.customer_id = c.id;
DROP TABLE customers_old;
