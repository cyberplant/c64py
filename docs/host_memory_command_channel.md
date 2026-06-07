# Host command channel via C64 RAM (draft)

This document sketches a **machine-local control plane**: a BASIC or ML program inside the emulated C64 sets up a small protocol in RAM; the **host** Python process watches fixed addresses and performs actions (exit, snapshot, HTTP bridge, etc.), then writes a reply the guest can poll.

It is **not** a security boundary. Anyone who can run the emulator with the feature enabled can define arbitrary handlers. Treat it like `DEBUG` ports: **off by default**, for scripted harnesses and developer workflows only.

## Motivation

- Drive the emulator from **BASIC** without adding custom KERNAL ROMs or IEC gadgets.
- Clean shutdown after a batch job (`PRINT` progress, then request `exit`).
- Future: return structured JSON to the guest for tests (golden-master comparisons).

## Proposed CLI shape (illustrative)

```text
python C64.py --host-command-ctrl 0xC000,0xC002 ...
```

- **Argument 1** (e.g. `C000`): **guest → host** “mailbox” — two bytes (or one pointer) meaning “command block address low/high” or a **ZP-style 16-bit pointer** to a descriptor in guest RAM.
- **Argument 2** (e.g. `C002`): **host → guest** “response pointer” — where the host writes the address of a reply block after handling a command.

Exact semantics (flat 16-bit vs zero-page only, endianness) must be fixed before implementation. Little-endian 6510 order is the natural default.

## Descriptor layout (sketch)

Guest allocates a contiguous block, e.g. starting at `$1000`:

| Offset | Size | Meaning |
|--------|------|---------|
| +0 | 1 | **Magic / version** (e.g. `$01`) |
| +1 | 1 | **Command id** (enum: `1` = exit, `2` = snapshot, …) |
| +2 | 1 | **Flags** (reserved) |
| +3 | 1 | **Payload length** `N` (0–255 for v1) |
| +4 | N | **Payload** (PETSCII or UTF-8 subset; JSON if both sides agree) |

The **control word** at `$C000` could mean: “pointer = `$1000`” using two bytes low/high, **or** a sentinel: non-zero `$C000` means “poll `$C000/$C001` as 16-bit LE pointer to descriptor”.

After the host consumes the command it should **clear** the mailbox (write `$0000` to `$C000`) or set a **status byte** so the guest does not double-fire.

## Host → guest reply (sketch)

Symmetric block at an address the host chooses, **written into RAM** by the emulator:

| Field | Meaning |
|-------|---------|
| Status | `0` pending, `1` ok, `$FF` error |
| Length + payload | Same as guest → host |

The second CLI address (`$C002`) could hold the **reply block address** (16-bit), updated by the host when a reply is ready; the guest polls until non-zero, reads the block, then clears `$C002`/`C003`.

## When to poll on the host

- **Per instruction** (expensive): simplest semantics.
- **Every N virtual cycles** or **once per emulated frame**: cheaper; adds latency.
- **Cooperative**: guest hits a `NOP` sled that maps to a “trap” PC (heavy-handed).

The right default is probably **once per CPU quantum** in the Python loop, or a hook next to existing **snapshot / trace** checks, with a **rate limit** so a buggy guest cannot busy-spin the host.

## JSON in guest RAM

Putting ASCII/JSON in C64 RAM is fine for small messages. Constraints:

- Keep payloads **short** (≤ 256 bytes v1) to avoid spanning I/O shadows and to simplify parsing.
- **PETSCII vs ASCII**: document whether the host normalizes or expects PETSCII only.

## Security and abuse

- **Never enable by default** in distro packages.
- **Path / network commands** must validate against an allow-list (e.g. snapshot path under `TMPDIR` only).
- **“Run shell command”** should not exist in v1; if added later, require an explicit second flag and a config file allow-list.

## Alternatives

| Approach | Pros | Cons |
|----------|------|------|
| This RAM mailbox | Zero extra hardware in the guest | Easy to foot-gun; needs clear ABI |
| TCP control port (`C64.py --tcp-port`) from ML | Already exists | harder from pure BASIC |
| `USR()` hook to Python | Typed arguments | needs stub ML or modified BASIC |

## Open questions

1. **ZP-only restriction?** Some users want `$00–$FF` mailboxes; others want `$C000` cartridge RAM. Support both with validation.
2. **Re-entrancy**: guest issues command while host is still writing reply.
3. **Rust fast core**: mailbox must be observed from the same place **KERNAL hooks** already stop the batch, or the guest may never be seen.
4. **Unit tests**: small PRG that pokes mailbox; host asserts `exit` within N cycles.

## Status

**Not implemented** — design only. When you pick address semantics and command IDs, add a short `docs/` section here and wire the CLI in `C64.py` behind an explicit flag name (bikeshed: `--host-command-ctrl`, `--guest-mailbox`, …).
