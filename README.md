# Zephyr GDB Thread Awareness

A GDB extension for Zephyr RTOS thread awareness.

## Zephyr Configuration

The following Kconfig options must be enabled in your `prj.conf`:

```kconfig
# Required: exposes the thread list to the debugger
CONFIG_THREAD_MONITOR=y

# Required: includes function and variable debug symbols
CONFIG_DEBUG_INFO=y

# Recommended (Zephyr 3.x+): exports thread info offset symbols used by
# symbol-based discovery; without this, the extension falls back to
# hardcoded offsets
CONFIG_DEBUG_THREAD_INFO=y
```

## Prerequisites

GDB must be built with Python support. The Zephyr SDK ships this as `arm-zephyr-eabi-gdb-py` (or the equivalent for your architecture). The Zephyr SDK 0.17.3 bundles against Python 3.10. Install the matching Python version and ensure it is on your `PATH`.

## Usage

Source the script in GDB:

```gdb
(gdb) source /path/to/zephyr-gdb/zephyr_gdb.py
```

## Features

- Threads list (`info threads`)
- Thread context switching
- Automatic thread discovery (symbol or type based)

## Discovery Mode

By default, the extension tries symbol-based discovery first (Zephyr 3.x+) and falls back to hardcoded offsets. You can override this at runtime:

```gdb
(gdb) zephyr-discovery auto       # default: try symbols, fall back to hardcoded
(gdb) zephyr-discovery symbols    # symbol-based only (Zephyr 3.x+)
(gdb) zephyr-discovery hardcoded  # hardcoded offsets only (faster, Zephyr 2.7+)
(gdb) zephyr-discovery            # print current mode
```

Or set it before launching GDB via the environment variable:

```bash
ZEPHYR_GDB_DISCOVERY_MODE=hardcoded arm-zephyr-eabi-gdb-py build/zephyr/zephyr.elf
```

## Example: QEMU

1. **Build a Zephyr sample** (e.g., synchronization):
   ```bash
   west build -b qemu_x86 samples/synchronization
   ```

2. **Start QEMU with a GDB server** (in a separate terminal):
   ```bash
   qemu-system-i386 -m 8 -cpu qemu32,+nx,+pae \
       -nographic -no-reboot \
       -device isa-debug-exit,iobase=0xf4,iosize=0x04 \
       -s -S -kernel build/zephyr/zephyr.elf
   ```

3. **Connect GDB**:
   ```gdb
   $ arm-zephyr-eabi-gdb-py build/zephyr/zephyr.elf
   (gdb) target remote :1234
   (gdb) source /path/to/zephyr-gdb/zephyr_gdb.py
   (gdb) continue
   ^C
   (gdb) info threads
     Id   Target Id         Frame
   * 1    Thread 0x... (main) 0x... in main ()
     2    Thread 0x... (idle) 0x... in arch_cpu_idle ()
     3    Thread 0x... (workq) 0x... in z_work_q_main ()
   ```
