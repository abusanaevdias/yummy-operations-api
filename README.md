# Yummy Operations API

A persistent local backend demonstrator for food-box reservations, branch records, templates, and employee state. Python 3.11+, SQLite, and the standard library; no dependencies to install.

The project explores the operations concepts in the [Yummy React interface](https://github.com/abusanaevdias/yummy). It is a **separate implementation**. That frontend still uses its own mock data and is not connected to this API. All included names and addresses are explicitly synthetic.

## Try it

Run a complete temporary HTTP workflow:

```bash
python3.11 app.py demo
```

The demo seeds a temporary SQLite file, creates one reservation, repeats the request with the same idempotency key, and transitions it through `ready` to `picked_up`. It prints a JSON report and removes the temporary data after shutdown. It makes requests only to its own loopback server.

To explore a persistent database:

```bash
python3.11 app.py --db demo.sqlite3 init
python3.11 app.py --db demo.sqlite3 seed
python3.11 app.py --db demo.sqlite3 server
```

The default address is `http://127.0.0.1:8765`. Use `server --port 0` to choose a free local port. Stop with Ctrl+C; using the same `--db` file preserves records across restarts. A parent directory for a custom database path must already exist.

`seed` installs two branches, two templates, and two employees in one transaction. Repeating it leaves edited fixtures untouched. It refuses a nonempty database that it did not seed. It creates no orders automatically.

From a second terminal:

```bash
curl http://127.0.0.1:8765/api/health
curl 'http://127.0.0.1:8765/api/templates?status=active'
curl -i -X POST http://127.0.0.1:8765/api/orders \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: example-reservation-1' \
  --data-binary @sample-order.json
curl 'http://127.0.0.1:8765/api/orders?branchId=b_demo_north&status=reserved'
```

Repeat the POST exactly: the first response is `201`, the replay is `200`, and both contain the same original order. Use the returned order ID for a transition:

```bash
curl -X POST http://127.0.0.1:8765/api/orders/ORDER_ID/transitions \
  -H 'Content-Type: application/json' \
  --data '{"status":"ready"}'
```

## API contract

All request and response fields use camelCase. Creation returns the resource plus a `Location` header. Record deletion returns `204` with no body. PATCH updates only supplied fields and rejects unknown fields and identifiers.

| Resource | Collection | Individual record | Filters |
| --- | --- | --- | --- |
| Templates | GET, POST `/api/templates` | GET, PATCH, DELETE `/api/templates/{id}` | `status=active` or `archived` |
| Branches | GET, POST `/api/branches` | GET, PATCH, DELETE `/api/branches/{id}` | None |
| Employees | GET, POST `/api/employees` | GET, PATCH, DELETE `/api/employees/{id}` | `branchId`, `active=true` or `false` |
| Orders | GET, POST `/api/orders` | GET `/api/orders/{id}` | `branchId`, `status` |
| Order transitions | POST `/api/orders/{id}/transitions` | Body: `{"status":"ready"}` | None |
| Health | GET `/api/health` | Local demo mode and schema version | None |

All collection GETs return `{items, total, limit, offset}`. Defaults are `limit=50`, `offset=0`; maximums are 100 and 10000 respectively. Orders sort newest first; other collections sort by name, then ID. Duplicate and unknown query parameters return `422`.

### Input fields

POST requires all listed fields, except order `quantity`, which defaults to 1:

| Resource | Fields and rules |
| --- | --- |
| Template | `name` (1–100 chars), `description` (0–500 chars), `costPrice`, `salePrice`, `pickupFrom`, `pickupTo`, `status` |
| Branch | `name` (1–100 chars), `address` (1–200 chars), `status` (`open` / `closed`) |
| Employee | `name` (1–100 chars), existing `branchId`, `role` (`cashier` / `manager`), boolean `active` |
| Order | existing `branchId`, existing `templateId`, `customerName` (1–100 chars), integer `quantity` (1–100) |

Names and descriptions are trimmed; control characters are rejected. IDs accept up to 100 characters in payloads. Template prices must be integers from 1 to 1000000, with `salePrice <= costPrice`. They are illustrative whole KZT values; the API performs no money movement. `amount` is a snapshot of `salePrice × quantity`.

Pickup times use ASCII 24-hour `HH:MM`, with the end strictly after the start on the same day. Overnight windows are rejected. Only open branches and active templates accept new reservations. Existing orders snapshot branch and box names, unit price, and pickup window; later catalog edits preserve those snapshots.

### Reservation state

```text
reserved -> ready -> picked_up
    |         |
    +---------+----> cancelled
    +---------+----> expired
```

`picked_up`, `cancelled`, and `expired` are terminal. Skipping `ready`, repeating a transition, or reopening a terminal order returns `409`. Every successful transition adds a timestamped `statusHistory` entry atomically. Expiry is an explicit operator action, not a background timer.

The original frontend has a `paid` status. This API deliberately uses `reserved`: it has no payment integration or evidence of payment. A frontend adapter must map its screens and labels explicitly. Employee `boxesCreated` is omitted because this project has no box-inventory creation workflow.

Branch `boxesSold` retains the UI's field name but counts **synthetic picked-up units**, not financial sales. `redemptionRate` is picked-up units divided by picked-up plus expired units, as a percentage rounded to one decimal. It is `null` with no resolved units; cancelled, ready, and reserved orders are excluded.

### Idempotency and errors

Order creation requires `Idempotency-Key`: 1–128 ASCII letters, digits, `.`, `_`, `:`, or `-`. Keys are global to the database. Equal normalized requests replay the original creation response, even after a later transition, with `Idempotency-Replayed: true`. GET the order to see its current state. Reusing a key with different data returns `409`. Failed requests do not consume keys.

Errors have one shape:

```json
{"error":{"code":"validation_error","message":"Expected true or false.","field":"active"}}
```

| Status | Meaning |
| --- | --- |
| 400 / 411 / 413 / 415 | Invalid JSON or framing, missing length, body over 16 KiB, or wrong content type |
| 403 / 421 | Browser origin or Host does not match this loopback server |
| 404 / 405 | Missing record/route or unsupported route method |
| 409 | Record in use, unavailable reservation, invalid transition, or conflicting idempotency key |
| 422 | Invalid fields or query parameters |
| 408 / 503 | Body read timeout or busy/unavailable storage |

POST/PATCH require `Content-Length` and `Content-Type: application/json`; chunked bodies are rejected. JSON arrays, duplicate keys, lone Unicode surrogates in keys or nested values, and nonfinite number literals are rejected. Valid Unicode text and paired surrogate escapes are supported. Foreign-key references prevent deletion of templates or branches used by orders, including terminal orders. Branches referenced by employees are also protected.

## Verify locally

```bash
python3.11 -m unittest -v
```

The suite uses temporary database files and real loopback HTTP servers. It checks restart persistence, atomic validation, snapshots, terminal states, filtering, concurrent duplicate creation, request framing, strict JSON, origin checks, repeatable seeding, and CLI behavior. No external network or credentials are required.

## Scope and next work

This is an AI-assisted portfolio exercise, not a deployed operations service. The Python implementation and synthetic fixtures are original; the existing Yummy UI supplied domain concepts and field naming.

The server has no authentication, authorization, TLS, inventory limits, reservation dates, automatic expiry, payment processing, metrics pipeline, or production deployment. It binds only to IPv4 loopback/localhost and rejects cross-origin browser requests. `http.server` is a learning-oriented server; do not expose it through a public proxy. The original React development server would need an explicit same-origin proxy or a considered authentication/CORS design before integration.

Idempotency records and history are retained indefinitely. Catalog filtering/pagination is performed in memory after a database read, appropriate for these small fixtures. Larger installations would need SQL pagination, migrations, retention, backups, access controls, and bounded concurrency.

See [ARCHITECTURE.md](ARCHITECTURE.md) for transaction boundaries and design tradeoffs. Licensed under [MIT](LICENSE).
