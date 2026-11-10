# Embedded Systems Workbench — Architecture

Status: initial scaffold (section 14 deliverables). No project C code has been
ported yet; no protocol has been defined. Both wait for the real C sources.

## 1. Layers

```
┌─────────────────────────────────────────────────────────────────────┐
│ Presentation  app/ui/                                               │
│   MainWindow · 9 pages · docks · dialogs · Qt models · theme        │
│   (no business logic; talks only to services via AppContext)        │
├─────────────────────────────────────────────────────────────────────┤
│ Application services  app/core/services/                            │
│   DeviceService · ConnectionManager · CaptureService ·              │
│   ProtocolService · PortingService · TestRunner                     │
├──────────────────────────────┬──────────────────────────────────────┤
│ Infrastructure               │ Protocol layer  app/core/protocols/  │
│  networking/  TCP/UDP        │   ProtocolDecoder interface          │
│  database/    SQLite repos   │   DecoderRegistry                    │
│  config.py    JSON config    │   DeclarativeDecoder (user-defined)  │
│  logging_setup.py            │                                      │
├──────────────────────────────┴──────────────────────────────────────┤
│ Embedded compatibility layer  app/core/compatibility/               │
│   fixed_int · endian · bits · crc · buffer · serialization ·        │
│   framing · hal                                                     │
│   (pure Python, no Qt; the home of C-semantics helpers)             │
└─────────────────────────────────────────────────────────────────────┘
```

Dependency rule: arrows only point down. `compatibility/` imports nothing from
the rest of the app. `networking/` knows framers but not protocols. The UI
never imports `networking/`, `database/` or `compatibility/` directly except
for small display helpers (hex formatting, framing validation in the editor).

`app/context.py` is the composition root. It builds the `Database`, runs
migrations, configures logging and constructs every service once. Services
receive their dependencies through constructors, so tests build them directly
against a temporary database.

## 2. Runtime model

* **One GUI thread, asynchronous I/O.** All sockets are Qt objects
  (`QTcpSocket`, `QTcpServer`, `QUdpSocket`) driven by the Qt event loop. No
  call blocks; results arrive as signals.
* **Batching keeps the GUI responsive.** `CaptureService` coalesces packets:
  the monitor is refreshed every `monitor_refresh_interval_ms` (default 100)
  and recorded packets are written in one transaction every
  `capture_flush_interval_ms` (default 250).
* **Test suites run out of process.** `TestRunner.run_pytest` uses `QProcess`,
  streams output to the Test Bench and parses JUnit XML when it finishes.
* **Threads and SQLite.** `Database` hands each thread its own connection
  (thread-local). WAL mode lets readers proceed while one writer commits.
  Today all writes happen on the GUI thread; the per-thread design is there so
  a capture or analysis worker can be added without changing repositories.

## 3. Data flow: one received TCP frame

```
QTcpSocket.readyRead
  → TcpClient._on_ready_read      bytes counted (rx_bytes)
  → Framer.feed(data)             0..n complete frames, partial data buffered
  → Endpoint.frame_received       per frame (rx_frames)
  → ConnectionManager.packet      wrapped as Packet(timestamp, RX, src, dst, …)
  → CaptureService.add_packet     live ring (deque) + pending DB batch
  → packets_added (batched)       PacketMonitorPage → PacketTableModel.append
  → user selects row              ProtocolService.decode → DecoderRegistry
```

TX is symmetric: `ConnectionManager.send` → `Framer.encode` → socket write →
`frame_sent` → `Packet(TX)`. The monitor always shows bytes exactly as they
went over the wire (headers and delimiters included).

## 4. Networking

| Class | Qt class | Notes |
|---|---|---|
| `TcpClient` | `QTcpSocket` | connect timeout, optional auto-reconnect, one framer |
| `TcpServer` | `QTcpServer` | many clients, **one framer per client**, send to one or all |
| `UdpClient` | `QUdpSocket` | fixed remote; "Connected" means bound and ready |
| `UdpServer` | `QUdpSocket` | replies to an explicit peer or the last sender |

All endpoints share the `Endpoint` base: state (`Disconnected / Connecting /
Connected / Listening / Error`), RX/TX byte and message counters, error count
and last error, and the same signals.

**Framing** (`compatibility/framing.py`) is configured per connection:

| Type | Use |
|---|---|
| `raw` | pass chunks through as received (no message boundaries) |
| `delimiter` | terminator byte sequence, e.g. `0d0a` |
| `fixed` | constant frame size |
| `length_prefixed` | 1/2/4-byte length, either byte order, field offset, adjust for headers/trailers |

All framers enforce a maximum frame length and reset their buffer on error or
disconnect. UDP needs no framing: one datagram is one message.

UDP endpoints accept literal IP addresses (and `localhost`) only, so opening a
connection never blocks on DNS. TCP clients use Qt's asynchronous lookup.

## 5. Persistence

SQLite file (default `data/workbench.db`), opened with `foreign_keys=ON`,
`journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`.

Schema is versioned by append-only migrations in `database/schema/NNNN_*.sql` (loaded by `database/migrations.py`)
(table `schema_migrations`). An application refuses to open a database whose
schema is newer than it knows.

| Table | Purpose |
|---|---|
| `devices` | device inventory |
| `connections` | connection configurations (framing stored as JSON) |
| `sessions` | recording sessions |
| `packets` | recorded frames; payload is a **BLOB**; cascade-deleted with session |
| `protocols` | user-defined decoder definitions (JSON) |
| `test_cases` | device request/expected-response cases (BLOBs) |
| `test_results` | device and pytest results |
| `application_logs` | structured log records (pruned to a configured size) |
| `porting_projects` | one per C code base |
| `porting_modules` | one per C source file; status; Python target |
| `porting_module_dependencies` | includes, system includes, external calls |
| `porting_function_mappings` | C symbol → Python symbol |
| `porting_notes` | notes, compatibility issues, behavioral differences |
| `porting_verification_results` | each executed verification run |

Indexes cover the hot paths: packets by session/time, logs by time, modules by
project/status, notes and verification by module.

`Database.transaction()` uses `BEGIN IMMEDIATE`; nested blocks become
savepoints. `Database.backup()` uses SQLite's online backup API, so a backup
of a live database is consistent.

## 6. Protocol decoding

`ProtocolDecoder.decode(frame) -> DecodedMessage` never raises for malformed
input; problems are reported as errors and invalid fields. The registry also
contains decoder crashes, so a decoder bug cannot take down the monitor.

Two ways to add a protocol:

1. **Declarative** — a JSON field list stored in `protocols` (integers of any
   fixed width and per-field byte order, floats, bytes, ASCII, enums, bit
   sub-fields, expected constants, length-from-field). Good for inspecting
   simple flat frames.
2. **Code** — a `ProtocolDecoder` subclass. This is the route for a protocol
   ported from C: the decoder lives next to the ported module, uses the
   compatibility layer, and is verified against the C implementation like any
   other ported code.

No protocol ships with the application. None will be added until the C
sources defining it have been inspected.

## 7. Embedded compatibility layer

| Module | Replaces in C | Key behaviour |
|---|---|---|
| `fixed_int` | `uint8_t`…`int64_t` arithmetic and casts | wrap on conversion; signed overflow, bad shifts, `/0`, `INT_MIN/-1` raise `CUndefinedBehaviorError` unless wrapping is explicitly requested; C division/modulo truncate toward zero |
| `endian` | `htons`, byte swaps, `memcpy` of integers | byte order always explicit, never host-dependent |
| `bits` | `BIT()`, masks, field get/set, rotates | field writes reject values that do not fit |
| `crc` | CRC tables / bit loops | Rocksoft model engine; 11 catalogue algorithms; sum8, xor8, Fletcher-16, RFC 1071 |
| `buffer` | pointer walking, fixed arrays, ring buffers | bounds-checked reader/writer; explicit ring overflow policy |
| `serialization` | packed/natural structs on the wire | explicit byte order, padding computed, every field range-checked; bit fields deliberately unsupported (implementation-defined) |
| `framing` | stream reassembly | see §4 |
| `hal` | tick counters, register access, blocking writes | interfaces + test doubles (`ManualClock`, `MemoryRegisterBus`) |

These helpers are checked against code compiled by the host C compiler
(`tests/compatibility/test_against_c_reference.py`), not just against Python
expectations.

## 8. Porting workflow support

`PortingService` + the Porting Workspace page implement brief sections 3, 8
and 9:

* **Import** scans a C tree (`porting/c_scanner.py`) and creates one module
  per `.c` file with its header, include and external-call dependencies and
  every top-level symbol as an unmapped row in the function mapping. The
  scanner is an inventory aid without a preprocessor; it never sets a status.
* **Statuses**: Not Analyzed → Analyzed → Ready to Port → In Progress →
  Ported → Verification Required → Verified, plus Blocked.
* **Rules enforced in code**:
  * Ported / Verification Required need a Python target module.
  * Verified needs the latest recorded run to compare against the C
    implementation (C harness or golden vectors), have ≥1 pass, 0 failures and
    no reported binary incompatibility.
  * A later failing run moves a Verified module back to Verification Required.
* **Report**: Markdown status report per project (status counts, mapping
  progress, latest verification, recorded issues and risks).

See `docs/PORTING_GUIDE.md` for the per-module procedure.

## 9. Configuration and logging

* `data/config.json` (or `--config` / `EWB_CONFIG`) overrides
  `resources/default_config.json`. Unknown keys are logged and ignored;
  invalid values stop startup with a clear message.
* Logs are structured: `log.info("msg", extra={"context": {...}})`. Sinks:
  console, rotating JSON-lines file (`data/logs/workbench.jsonl`),
  `application_logs` table, and the GUI (Application Logs page and
  Diagnostics dock).

## 10. Cross-platform notes

* Pure Python + PySide6; no platform-specific code in the application.
* Paths go through `pathlib`; the C scanner normalizes `\` to `/` in stored
  paths so a project imported on Windows matches one imported on Linux.
* The C reference harness builds with `cc`/`gcc`/`clang` (`$CC` honoured)
  and exports symbols with `__declspec(dllexport)` on Windows. With MSVC only,
  the build command in `tests/conftest.py` needs an MSVC variant (not yet
  written); without any compiler those tests are **skipped, never passed**.

## 11. Session workflow

`WorkflowService` (`core/services/workflow_service.py`) orchestrates the
five-step page (`ui/pages/workflow.py`); it adds no transport or storage of
its own.

* **Server** — a *runtime* connection in `ConnectionManager.open_runtime()`
  (negative id, never written to `connections`), so it survives switching
  the database. Its packets are stored with `connection_id = NULL` and the
  peer addresses. Settings persist in `AppConfig.workflow_server_*`.
* **Database switch** — `Database.reopen(path)` swaps the file under the
  same object, so every repository follows; stale per-thread connections are
  replaced via a generation counter. Migrations run on the new file. Refused
  while a session records.
* **Devices** — tracked from the server's `peer_changed` signal; one
  `session_devices` row per connection (reconnects make new rows). Names come
  from the user or from a unique Device Manager entry with the same IP.
* **Operations** — `OperationRunner` keeps a FIFO per (connection, peer):
  send → wait for the first frame *from that peer* → strip framing
  (`Framer.unwrap`) → judge by the rule → store request, response, status
  and latency in `operation_results`. Devices run in parallel, each device's
  operations in order. Disconnect cancels that device's queue. Any frame
  from that device arriving while an operation waits counts as its reply,
  so unsolicited device messages can be mistaken for replies (noted risk).
* **Report** — `core/reporting/session_report.py` builds the report only
  from the database (session, devices, results, packet totals per peer,
  warnings logged during the session), so past sessions can be reported
  again. HTML uses concrete colours so it renders both in browsers and in the
  Qt preview.

Schema migration 3 adds `sessions.config_json`, `device_operations`,
`session_devices` and `operation_results`.

## 12. Template mechanisms

* **Project profile** (`app/project.py`, `project.json`): name, pages and
  order, start page, plugins, SQL folders, theme. Validated at start-up;
  unknown keys are errors.
* **Page registry** (`app/ui/registry.py`): built-in pages are `"module:Class"`
  strings imported lazily, so a deleted page module is skipped with a warning
  instead of crashing. Plugins call `add_page(...)` (optionally
  `replace=True`) and `on_startup(hook)`.
* **Named SQL** (`app/core/database/sql_library.py`): `-- name:` blocks in
  `.sql` files, built-ins first, then `sql_dirs`; same name overrides (logged).
  `Database.run/run_one/run_scalar/run_write`; `run_read_only` uses a
  `mode=ro` connection for the Database Explorer.
* **Migrations** (`app/core/database/schema/NNNN_*.sql`): split with
  `sqlite3.complete_statement`, applied in one transaction per file. The
  conversion from the earlier Python-defined migrations produced the
  identical schema (checked table by table, whitespace-normalised).
* **Theme** (`theme.configure`): colour overrides rebuild the stylesheet and
  colour maps before any window exists.

## 13. Known gaps and risks

* No real C sources or protocol definitions have been provided, so nothing
  project-specific is ported, mapped or verified.
* `PacketTableModel` keeps up to `max_live_packets` in memory; very long
  captures should be recorded and reopened from the database.
* Replay re-sends captured frames through the connection's framer; on a
  framed connection that wraps them twice. Use a raw-framed connection for
  replay (the UI says so).
* The C scanner misses symbols created by macros and does not evaluate `#if`.
  Use the compiler (`gcc -E`, libclang) for authoritative analysis.
* Workflow replies are matched as "next frame from that device". Devices
  that send unsolicited data need a protocol-aware decoder/correlation
  (request ids) once their protocol is known.
* DB writes run on the GUI thread in small batches. If profiling with a real
  device shows stalls, move `CaptureService.flush` to a worker thread (the
  per-thread connection design already supports it).
