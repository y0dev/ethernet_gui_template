# Example: a C console menu as a Session Workflow

**Dummy example.** The device, protocol and C program are invented, to show
how a typical C test console maps onto the workbench. Use it as a pattern
for your own device.

## The starting point

`console_menu.c` is the kind of program the workflow replaces. It waits for
one device to connect, prints a menu, sends the chosen command and prints
the raw reply for a person to check:

```text
1) Ping
2) Send device info
3) LED on
4) LED off
5) Blink LED 3 times
6) Read temperature
7) Reset
```

Every command is one binary frame:

```text
LEN | CMD | DATA... | CRC8
```

* `LEN` counts the CMD and DATA bytes.
* `CRC8` is CRC-8/SMBUS over LEN, CMD and DATA.
* The device answers with `CMD | 0x80`, or `FF <cmd>` for an unknown command.

## What changes in the workflow

| C console | Session Workflow |
|---|---|
| `listen()` / `accept()` one device | Step 1 server. Any number of devices can connect. |
| `printf` menu + `switch` | Step 4 operations menu, one operation per `case` |
| `send_cmd()` builds LEN and CRC | The operation holds `CMD DATA CRC`. The server's **framing** adds LEN. |
| `print_reply()`, checked by eye | The reply rule marks each result PASSED, FAILED or TIMEOUT |
| Nothing kept | Every frame and result is stored per session, plus a report |

`make_command_set.py` turns each `case` into an operation and writes
`led_device.json`:

| Operation | Sends (hex) | Reply rule |
|---|---|---|
| Ping | `01 12` | exactly `81` |
| Send device info | `02 1B` | starts with `82 53 4E 3D` (`0x82` "SN=") |
| LED on / LED off | `10 01 86` / `10 00 81` | exactly `90 01` / `90 00` |
| Blink LED x3 | `11 03 19 83` | exactly `91 00` |
| Blink LED x10 | `11 0A 0A 47` | exactly `91 00`. Not in the C menu: same command, other values. |
| Read temperature | `20 F5` | starts with `A0` |
| Reset | `30 85` | no reply expected |
| Unknown command (expect error) | `7F 6F` | exactly `FF 7F` |

The last byte of each "Sends" value is the CRC. The generator computes it
once with the workbench's CRC engine, so the operations are plain hex. Replies
are compared after the framing is removed, so a rule never includes LEN or
the reply CRC.

## Try it

1. Start the workbench (`python main.py`) and open **Session Workflow**.
2. **Server:** TCP server, `127.0.0.1` (or `0.0.0.0`), port `5000`. Under
   *Message framing*, choose **Length prefixed** with:
   * field size **1 byte**
   * **big endian**
   * offset **0**
   * length adjust **1** (the uncounted CRC byte after the data)

   Then click **Start server**.
3. **Database:** **Use this database**, or **Create new…** for a separate test
   campaign.
4. Start three dummy devices in a terminal:

   ```bash
   python examples/led_device/led_device_sim.py --port 5000 --count 3
   ```

   They appear in **Devices** as `127.0.0.1:<port>`. Name them if you like,
   then click **Start session**.
5. **Operations:** **Import…** `examples/led_device/led_device.json`. Tick
   the target devices, then either:
   * select operations and click **Run selected** (several run in order on
     each device), or
   * click **Run all in order**, or
   * right-click a device in step 3 for the same menu.
6. **Report:** **Finish session**. The HTML, Markdown and CSV reports go to
   `data/reports/`.

`tests/integration/test_examples.py` runs exactly this flow automatically.

## Adapting it to your device

1. In `make_command_set.py`, replace the command constants and the
   `operation(...)` list with your menu entries.
2. Replace `request_payload()` with your encoding. The compatibility layer
   reproduces C behaviour:
   * `app/core/compatibility/crc.py` for any CRC by its parameters
   * `serialization.py` for C struct layouts
   * `endian.py` for byte order
3. Set `FRAMING` to match your frames. If there is no length field, use
   **Delimiter** for text lines or **Raw** for one frame per TCP packet.
4. Run `python examples/led_device/make_command_set.py` and import the JSON.

Some cases need code rather than a fixed operation:

* **Frames that change per send,** such as a sequence number or timestamp.
  Build them in a page (`python tools/new_page.py <key> "<Title>" --template
  device_console`) with the same encoder.
* **Values typed by the user,** such as "blink N times". Either add one
  operation per common value (like *Blink LED x10*) or add an input to a
  `device_console` page.
