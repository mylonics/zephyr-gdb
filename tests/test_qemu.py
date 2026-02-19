#!/usr/bin/env python3
"""Run the Zephyr GDB extension test against a QEMU instance."""

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import time


def find_gdb():
    """Return a GDB binary that has Python support, or None."""
    gdb_bin = shutil.which('gdb')
    if not gdb_bin:
        return None
    try:
        r = subprocess.run([gdb_bin, '--batch', '--ex', "python print('ok')"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        return gdb_bin if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def find_qemu():
    """Return the first available QEMU x86 binary, or None."""
    for name in ('qemu-system-i386', 'qemu-system-x86_64'):
        if shutil.which(name):
            return name
    return None


def create_gdb_commands(repo_root, output_dir):
    """Write a GDB batch command file and return its path."""
    gdb_commands_file = output_dir / 'gdb_commands.txt'

    repo_root_str = str(repo_root).replace('\\', '/')

    gdb_commands = f'''# Load the Zephyr GDB extension
python
import sys
sys.path.insert(0, '{repo_root_str}')
exec(open('{repo_root_str}/zephyr_gdb.py').read())
end

# Connect to QEMU
target remote :1234

# Set a breakpoint at main
break main

# Continue to main
continue

# Wait a bit for threads to initialize
continue &
# Give it time to run
shell sleep 2

# Stop the program
interrupt

# Display thread information
echo \\n=== THREAD INFORMATION ===\\n
info threads

# Switch to each thread and show backtrace
python
import gdb
try:
    # Get thread count
    output = gdb.execute('info threads', to_string=True)
    print("\\n=== FULL THREAD OUTPUT ===")
    print(output)
    print("\\n=== DETAILED THREAD INFORMATION ===")
    
    # Parse thread IDs and switch to each
    for line in output.split('\\n'):
        if line.strip() and not line.startswith('Id') and not line.startswith('==='):
            parts = line.split()
            if len(parts) > 0:
                # Skip the * marker if present
                tid_str = parts[1] if parts[0] == '*' else parts[0]
                try:
                    tid = int(tid_str)
                    print(f"\\n--- Thread {{tid}} ---")
                    gdb.execute(f'thread {{tid}}')
                    gdb.execute('backtrace 5')
                except (ValueError, gdb.error) as e:
                    pass
except Exception as e:
    print(f"Error during thread inspection: {{e}}")
end

# Quit GDB
quit
'''
    
    gdb_commands_file.write_text(gdb_commands)
    return gdb_commands_file


def run_qemu(elf_file, output_dir):
    """Start QEMU with a GDB server on :1234. Returns the Popen object or None."""
    qemu_bin = find_qemu()
    if not qemu_bin:
        print("Error: QEMU not found (install qemu-system-x86)")
        return None

    qemu_cmd = [
        qemu_bin, '-m', '8', '-cpu', 'qemu32,+nx,+pae',
        '-device', 'isa-debug-exit,iobase=0xf4,iosize=0x04',
        '-no-reboot', '-nographic', '-s', '-S',
        '-kernel', str(elf_file)
    ]
    print(f"Starting QEMU...")
    with open(output_dir / 'qemu.log', 'w') as log_file:
        try:
            proc = subprocess.Popen(qemu_cmd, stdout=log_file, stderr=subprocess.STDOUT)
        except Exception as e:
            print(f"Error starting QEMU: {e}")
            return None

    time.sleep(2)
    if proc.poll() is not None:
        print("Error: QEMU failed to start")
        print((output_dir / 'qemu.log').read_text())
        return None

    print(f"QEMU started (PID: {proc.pid})")
    return proc


def run_gdb(gdb_bin, elf_file, gdb_commands_file, output_dir, repo_root, discovery_mode='auto'):
    """Run GDB in batch mode with the extension. Returns the exit code."""
    env = os.environ.copy()
    env['ZEPHYR_GDB_DISCOVERY_MODE'] = discovery_mode
    for key in ('PYTHONHOME', 'PYTHONPATH', 'Python_ROOT_DIR',
                'Python2_ROOT_DIR', 'Python3_ROOT_DIR'):
        env.pop(key, None)
    ld = env.get('LD_LIBRARY_PATH', '')
    env['LD_LIBRARY_PATH'] = f"/usr/lib/x86_64-linux-gnu:{ld}"

    gdb_cmd = [gdb_bin, '--batch', f'--command={gdb_commands_file}', str(elf_file)]
    print("Running GDB...")
    try:
        with open(output_dir / 'gdb_output.log', 'w') as f:
            result = subprocess.run(gdb_cmd, stdout=f, stderr=subprocess.STDOUT,
                                    cwd=str(repo_root), env=env, timeout=30)
        return result.returncode
    except subprocess.TimeoutExpired:
        print("Warning: GDB timed out")
        return 1
    except Exception as e:
        print(f"Error running GDB: {e}")
        return 1


def analyze_results(output_dir):
    """Print GDB output and return True if thread info was captured."""
    gdb_output = output_dir / 'gdb_output.log'
    if not gdb_output.exists():
        print("GDB output file not found")
        return False

    content = gdb_output.read_text()
    print("\n=== GDB Output ===")
    print(content)

    if 'THREAD INFORMATION' not in content:
        print("FAIL: thread information not captured")
        return False

    thread_count = content.count('Target Id')
    print(f"  threads found : {thread_count}")
    if any(name in content.lower() for name in ('idle', 'main', 'work')):
        print("  thread names  : detected")
    print("PASS")
    return True


def main():
    parser = argparse.ArgumentParser(description='Test Zephyr GDB extension with QEMU')
    parser.add_argument('--elf', required=True, help='Path to zephyr.elf')
    parser.add_argument('--discovery-mode', default='auto',
                        choices=['auto', 'symbols', 'hardcoded'],
                        help='Thread discovery mode (default: auto)')
    args = parser.parse_args()

    elf_file = pathlib.Path(args.elf).resolve()
    if not elf_file.exists():
        sys.exit(f"ELF not found: {elf_file}")

    script_dir = pathlib.Path(__file__).parent.resolve()
    repo_root = script_dir.parent
    output_dir = script_dir / 'output'
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"ELF       : {elf_file}")
    print(f"Mode      : {args.discovery_mode}")
    print(f"Repo root : {repo_root}")

    gdb_bin = find_gdb()
    if not gdb_bin:
        sys.exit("Error: GDB with Python support not found (install gdb)")
    print(f"GDB       : {gdb_bin}")

    gdb_commands_file = create_gdb_commands(repo_root, output_dir)

    qemu_proc = run_qemu(elf_file, output_dir)
    if not qemu_proc:
        return 1

    try:
        run_gdb(gdb_bin, elf_file, gdb_commands_file, output_dir, repo_root, args.discovery_mode)
    finally:
        print("Stopping QEMU...")
        try:
            qemu_proc.terminate()
            qemu_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            qemu_proc.kill()
            qemu_proc.wait()

    return 0 if analyze_results(output_dir) else 1


if __name__ == '__main__':
    sys.exit(main())
