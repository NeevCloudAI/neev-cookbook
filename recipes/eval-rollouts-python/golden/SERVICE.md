# Inventory service

Runs on http://127.0.0.1:8000 and keeps stock in memory. `data/inventory.json` is only what it loaded at start-up; changing that file does not change the service.

- `GET /items` returns `{"items": [{"sku": ..., "name": ..., "stock": ...}, ...]}`
- `GET /items/<sku>` returns one item
- `PUT /items/<sku>` with the JSON body `{"stock": 20}` sets that item's stock
- `GET /health` returns the service's boot id and how many writes it has taken
