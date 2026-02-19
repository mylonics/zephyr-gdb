#!/usr/bin/env python3
"""Build and test the Zephyr GDB extension locally."""

import argparse
import os
import pathlib
import shutil
import subprocess
import sys

SCRIPT_DIR = pathlib.Path(__file__).parent
OUTPUT_DIR = SCRIPT_DIR / 'output'


def run(cmd, *, cwd=None, log=None, env=None):
    if log:
        with open(log, 'w') as f:
            result = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT,
                                    cwd=cwd, env=env)
        if result.returncode != 0:
            print(pathlib.Path(log).read_text())
    else:
        result = subprocess.run(cmd, cwd=cwd, env=env)
    if result.returncode != 0:
        sys.exit(f"Command failed (exit {result.returncode}): {' '.join(str(c) for c in cmd)}")
    return result


def setup_workspace(workspace, version):
    zephyr_base = workspace / 'zephyr'
    if zephyr_base.exists():
        print(f"Using existing workspace: {zephyr_base}")
        return zephyr_base

    workspace.mkdir(parents=True, exist_ok=True)
    print(f"Initializing Zephyr {version} workspace...")
    run(['west', 'init', '-m', 'https://github.com/zephyrproject-rtos/zephyr',
         '--mr', version, '.'],
        cwd=workspace, log=OUTPUT_DIR / 'west_init.log')
    run(['west', 'update', '--narrow', '-o=--depth=1', 'zephyr', 'cmsis', 'hal_intel'],
        cwd=workspace, log=OUTPUT_DIR / 'west_update.log')
    run(['west', 'zephyr-export'], cwd=workspace)
    # Install Zephyr's Python build dependencies (e.g. pyelftools)
    req = zephyr_base / 'scripts' / 'requirements.txt'
    if req.exists():
        run([sys.executable, '-m', 'pip', 'install', '-r', str(req)])
    return zephyr_base


def build(zephyr_base):
    print("Building Zephyr sample...")
    src_conf = SCRIPT_DIR / 'configs' / 'prj.conf'
    dst_conf = zephyr_base / 'samples' / 'synchronization' / 'prj_gdb_test.conf'
    shutil.copy2(src_conf, dst_conf)
    env = os.environ.copy()
    env['ZEPHYR_BASE'] = str(zephyr_base)
    run(['west', 'build', '-p', 'always', '-b', 'qemu_x86',
         str(zephyr_base / 'samples' / 'synchronization'), '--',
         '-DEXTRA_CONF_FILE=prj_gdb_test.conf'],
        cwd=zephyr_base, env=env, log=OUTPUT_DIR / 'build.log')
    elf = zephyr_base / 'build' / 'zephyr' / 'zephyr.elf'
    if not elf.exists():
        sys.exit(f"ELF not found: {elf}")
    # Copy to output dir so it can be uploaded as a CI artifact
    shutil.copy2(elf, OUTPUT_DIR / 'zephyr.elf')
    print(f"Build OK: {elf}")
    return elf


def test(elf, mode):
    print(f"\nTesting discovery mode: {mode}")
    result = subprocess.run([sys.executable, str(SCRIPT_DIR / 'test_qemu.py'),
                             '--elf', str(elf), '--discovery-mode', mode])
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(description='Build and test Zephyr GDB extension')
    parser.add_argument('--zephyr-base', help='Path to existing Zephyr installation')
    parser.add_argument('--zephyr-version', default='v4.2.1',
                        help='Zephyr version to fetch (default: v4.2.1)')
    parser.add_argument('--discovery-mode', default='all',
                        choices=['auto', 'symbols', 'hardcoded', 'all'],
                        help='Discovery mode(s) to test (default: all)')
    parser.add_argument('--build-only', action='store_true',
                        help='Build only, do not run tests')
    parser.add_argument('--skip-build', action='store_true',
                        help='Skip the build step (use existing ELF)')
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.zephyr_base:
        zephyr_base = pathlib.Path(args.zephyr_base).resolve()
    else:
        zephyr_base = setup_workspace(SCRIPT_DIR / 'test_workspace', args.zephyr_version)

    if args.skip_build:
        elf = zephyr_base / 'build' / 'zephyr' / 'zephyr.elf'
        if not elf.exists():
            sys.exit(f"ELF not found: {elf}")
    else:
        elf = build(zephyr_base)

    if args.build_only:
        print(f"ELF: {elf}")
        return 0

    modes = ['auto', 'symbols', 'hardcoded'] if args.discovery_mode == 'all' else [args.discovery_mode]
    results = {m: test(elf, m) for m in modes}

    print()
    ok = all(results.values())
    for m, passed in results.items():
        print(f"  {m}: {'PASS' if passed else 'FAIL'}")
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())


