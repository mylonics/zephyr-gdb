# Zephyr GDB Extension Tests

This directory contains automated tests for the Zephyr GDB extension.

## Test Structure

### Local Test Script (`../test_local.py`)

A comprehensive all-in-one test script that can be run locally to build and test the GDB extension. This is the recommended way to test the extension before submitting changes.

**Features:**
- Checks for all required dependencies
- Initializes a minimal Zephyr workspace automatically
- Builds a Zephyr sample application
- Runs the GDB extension test
- Provides clear success/failure reporting
- Cross-platform support (Linux, macOS, Windows)

**Usage:**
```bash
# From repository root
python3 test_local.py

# Or with options
python3 test_local.py --zephyr-base ~/zephyrproject/zephyr
python3 test_local.py --zephyr-version v3.7.1
python3 test_local.py --help
```

### Automated CI Testing (`test.yml`)

The GitHub Actions workflow automatically tests the extension with:
- **Zephyr v4.2.1** (latest 4.x release)

The workflow:
1. Sets up Ubuntu with required dependencies
2. Installs Zephyr SDK
3. Calls the `test_local.py` script to build and test
4. Captures and validates thread output

### Test Script (`test_qemu.py`)

The test script:
1. Starts QEMU with GDB server on port 1234
2. Connects GDB with the Zephyr extension loaded
3. Breaks at `main()` and continues execution
4. Captures thread information using `info threads`
5. Switches to each thread and displays backtrace
6. Validates that thread discovery worked correctly

**Usage:**
```bash
python3 tests/test_qemu.py ~/zephyrproject/zephyr
```

**Note:** This script expects a Zephyr build to already exist. For automated building and testing, use the `test_local.py` script from the repository root.

### Test Configuration (`configs/prj.conf`)

The test uses a Kconfig overlay file that enables:
- `CONFIG_THREAD_MONITOR=y` - Required for thread list discovery
- `CONFIG_THREAD_NAME=y` - Enables thread naming
- `CONFIG_DEBUG=y` - Debug build mode
- `CONFIG_DEBUG_OPTIMIZATIONS=y` - Debug-friendly optimizations
- `CONFIG_NO_OPTIMIZATIONS=y` - Disable aggressive optimizations for better debugging

## Running Tests Locally

### Prerequisites

- Zephyr development environment set up
- West installed
- QEMU installed (`qemu-system-x86` or `qemu-system-i386`)
- Zephyr SDK installed (for build toolchain)
- GDB with Python support: `gdb` (standard GDB package)

**Important:** Use the standard `gdb` package, not `gdb-multiarch`. On Ubuntu, `gdb-multiarch` has broken Python support (missing C extension modules like `_struct` and `_posixsubprocess`). The standard `gdb` package includes proper Python integration and works correctly with the extension.

### Quick Test (Recommended)

Use the all-in-one test script from the repository root:

```bash
cd /path/to/zephyr-gdb
python3 test_local.py
```

This handles everything automatically including building Zephyr.

### Manual Test with Existing Build

If you already have a Zephyr workspace and want to test manually:

```bash
# 1. Build Zephyr sample
cd ~/zephyrproject/zephyr
# Copy test configuration
cp /path/to/zephyr-gdb/tests/configs/prj.conf samples/synchronization/prj_gdb_test.conf
west build -p -b qemu_x86 samples/synchronization -- \
  -DEXTRA_CONF_FILE=prj_gdb_test.conf

# 2. Run the test
cd /path/to/zephyr-gdb
python3 tests/test_qemu.py ~/zephyrproject/zephyr
```

### Expected Output

A successful test will show:

```
========================================
Test Results
========================================

=== GDB Output ===
Zephyr RTOS GDB support loaded
...
=== THREAD INFORMATION ===
  Id   Target Id            Prio State Frame
* 1    idle_00              15    0    ...
  2    main                 0     0    ...
  3    thread_a             7     0    ...
  4    thread_b             7     0    ...

✓ Thread information captured
✓ Found 4 thread(s)
✓ Thread names detected
✓ Thread switching successful

=== TEST PASSED ===
```

## Test Output Files

Test results are saved in `tests/output/`:
- `gdb_output.log` - Full GDB session output including thread information
- `qemu.log` - QEMU console output
- `gdb_commands.txt` - GDB commands executed during the test

## Continuous Integration

The test workflow runs automatically on:
- Push to `main` or `develop` branches
- Pull requests to `main`
- Manual trigger via GitHub Actions UI

## Troubleshooting

### "QEMU failed to start"
- Ensure QEMU is installed: `sudo apt-get install qemu-system-x86`
- Check that the build directory contains `zephyr.elf`

### "Thread information not captured"
- Verify CONFIG_THREAD_MONITOR=y in build configuration
- Check GDB has Python support: `gdb --version` and `gdb -batch -ex "python print('ok')"`
- Review `tests/output/gdb_output.log` for error messages

### "Thread names not detected"
- This may be expected if CONFIG_THREAD_NAME=n
- Check that thread discovery still worked (thread count > 0)

## Zephyr Version Compatibility

The test suite validates compatibility with:
- **Zephyr 4.2.1** (current stable, automatically tested)

Older versions (3.7.x, 3.5.0, 3.0.0, 2.7.x) should also work but are not automatically tested.

## Adding New Tests

To add tests for different Zephyr samples or boards:

1. Create a new test script in `tests/`
2. Add a new job to `.github/workflows/test.yml`
3. Document the test in this README

For example, testing with ARM architecture:

```yaml
test-qemu-arm:
  runs-on: ubuntu-22.04
  steps:
    # ... similar to test-qemu but with:
    # -b qemu_cortex_m3
    # -t arm-zephyr-eabi
    # qemu-system-arm
```

## Manual Testing with Different Zephyr Versions

To test with a specific Zephyr version:

```bash
# Clone Zephyr at specific version
west init -m https://github.com/zephyrproject-rtos/zephyr --mr v4.2.1 ~/zephyr-test
cd ~/zephyr-test
west update

# Build and test
cd zephyr
west build -p -b qemu_x86 samples/synchronization -- \
  -DCONFIG_THREAD_MONITOR=y \
  -DCONFIG_THREAD_NAME=y \
  -DCONFIG_DEBUG_INFO=y

# Run test
cd /path/to/zephyr-gdb
python3 tests/test_qemu.py ~/zephyr-test/zephyr
```

## Contributing

When adding new features to the GDB extension:
1. Add corresponding tests
2. Ensure tests pass for both Zephyr 4.2 and 3.7
3. Update this README if test procedures change
