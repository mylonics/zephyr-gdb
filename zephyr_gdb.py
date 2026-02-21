#!/usr/bin/env python3
"""
Zephyr RTOS GDB Thread Awareness Extension

This GDB Python extension provides RTOS thread awareness for debugging
Zephyr applications. It automatically discovers structure offsets from
symbols exported by Zephyr (for Zephyr 3.x+) and falls back to hardcoded
offsets for older versions.

This is the Phase 2b unified implementation combining symbol-based discovery
with hardcoded offset fallback for maximum compatibility.

Usage:
    (gdb) source /path/to/zephyr_gdb.py
    (gdb) info threads
    (gdb) thread 2

Requirements:
    - GDB 8.0+ with Python 3.6+ support
    - Zephyr application built with CONFIG_THREAD_MONITOR=y
    - Debug symbols included (CONFIG_DEBUG_INFO=y)

Discovery Method:
    - Default: Tries symbol-based discovery first (Zephyr 3.x+), falls back to GDB type system (Zephyr 2.7+)
    - Can be forced using ZEPHYR_GDB_DISCOVERY_MODE environment variable:
      * 'auto' (default): Try symbols first, fall back to hardcoded
      * 'symbols': Force symbol-based discovery only (fails if symbols unavailable)
      * 'hardcoded': Force hardcoded/GDB type system mode
    - Can also be set at runtime with the 'zephyr-discovery' GDB command:
      (gdb) zephyr-discovery hardcoded
      (gdb) zephyr-discovery symbols
      (gdb) zephyr-discovery auto

For more information, see README.md
"""

import gdb
import struct
import sys
import os

# Check Python version
if sys.version_info < (3, 6):
    print("="*70)
    print("ERROR: Zephyr GDB extension requires Python 3.6 or later")
    print(f"Current version: {sys.version}")
    print()
    print("Note: If using Zephyr SDK, Python 3.10 is required.")
    print("See: https://github.com/mylonics/zephyr-gdb#python-version-requirements")
    print("="*70)
    sys.exit(1)

# Global state
thread_cache = []  # List of ZephyrThread objects
current_thread_ptr = None  # Pointer to currently executing thread
_cached_offsets = None  # Cached kernel offsets (set at load and by zephyr-discovery)
_hw_active_lwp = None  # LWP of the thread actually executing on hardware

# Discovery mode: 'auto' (default), 'symbols', 'hardcoded'
_discovery_mode = os.environ.get('ZEPHYR_GDB_DISCOVERY_MODE', 'auto').lower()


class ZephyrThread:
    """Represents a single Zephyr thread"""
    
    next_lwp = 1  # Light-weight process ID counter
    
    def __init__(self, thread_ptr, offsets, arch):
        """
        Initialize a Zephyr thread object
        
        Args:
            thread_ptr: gdb.Value pointer to k_thread structure
            offsets: Dictionary of structure offsets
            arch: Architecture-specific handler
        """
        self.thread_ptr = thread_ptr
        self.offsets = offsets
        self.arch = arch
        self.lwp = ZephyrThread.next_lwp
        ZephyrThread.next_lwp += 1
        self.active = False
        self._update()
    
    def _update(self):
        """Update thread information from target memory"""
        try:
            thread = self.thread_ptr.dereference()
            
            # Extract thread name if available
            try:
                if 'name' in thread.type.fields():
                    self.name = thread['name'].string()
                else:
                    self.name = f"thread_{self.thread_ptr}"
            except:
                self.name = f"thread_{self.thread_ptr}"
            
            # Extract thread state
            try:
                self.state = int(thread['base']['thread_state'])
            except:
                self.state = 0
            
            # Extract priority
            try:
                self.prio = int(thread['base']['prio'])
            except:
                self.prio = 0
            
            # Extract callee-saved registers
            try:
                self.callee_saved = thread['callee_saved']
            except:
                self.callee_saved = None
            
            # Update frame information
            self._update_frame()
            
        except Exception as e:
            print(f"Warning: Failed to update thread {self.thread_ptr}: {e}")
            self.name = f"thread_{self.thread_ptr}"
            self.state = 0
            self.prio = 0
            self.frame_str = "??"
    
    def _update_frame(self):
        """Update stack frame information for this thread"""
        global current_thread_ptr, reg_cache
        
        if self.thread_ptr == current_thread_ptr:
            # This is the current thread - use actual CPU state
            self.active = True
            try:
                frame = gdb.newest_frame()
                pc = frame.pc()
                self.frame_str = f"0x{pc:x} in {frame.name() or '??'}()"
            except:
                self.frame_str = "??"
        else:
            # Non-current thread - reconstruct from saved context
            self.active = False
            try:
                if self.callee_saved is not None and self.arch:
                    pc = self.arch.get_thread_pc(self.callee_saved)
                    try:
                        block = gdb.block_for_pc(pc)
                        while block and block.function is None:
                            block = block.superblock
                        func_name = str(block.function) if block and block.function else "??"
                    except:
                        func_name = "??"
                    self.frame_str = f"0x{pc:x} in {func_name}()"
                else:
                    self.frame_str = "??"
            except Exception as e:
                self.frame_str = "??"


class ArchitectureHandler:
    """Base class for architecture-specific handling"""

    def get_thread_pc(self, callee_saved):
        raise NotImplementedError()

    def get_thread_sp(self, callee_saved):
        """Return the saved stack pointer for a suspended thread, or 0."""
        return 0

    def _probe_field(self, callee_saved, *fields):
        """Return int value of the first matching field name found in callee_saved."""
        try:
            field_names = {f.name for f in callee_saved.type.fields()}
            for name in fields:
                if name in field_names:
                    return int(callee_saved[name])
        except Exception:
            pass
        return 0


class ARMCortexMHandler(ArchitectureHandler):
    """ARM Cortex-M specific handling"""

    def get_thread_pc(self, callee_saved):
        try:
            if 'psp' in [f.name for f in callee_saved.type.fields()]:
                psp = int(callee_saved['psp'])
                pc_bytes = gdb.selected_inferior().read_memory(psp + 24, 4)
                return struct.unpack('<I', pc_bytes)[0]
        except Exception:
            pass
        return 0

    def get_thread_sp(self, callee_saved):
        try:
            return self._probe_field(callee_saved, 'psp', 'sp')
        except Exception:
            return 0


class x86Handler(ArchitectureHandler):
    """x86/x86_64 specific handling"""

    def get_thread_pc(self, callee_saved):
        return 0


class ARCHandler(ArchitectureHandler):
    """ARC processor family specific handling"""

    def get_thread_pc(self, callee_saved):
        return self._probe_field(callee_saved, 'blink', 'pc', 'ilink')


class RISCVHandler(ArchitectureHandler):
    """RISC-V architecture specific handling"""

    def get_thread_pc(self, callee_saved):
        return self._probe_field(callee_saved, 'ra', 'mepc', 'pc')

    def get_thread_sp(self, callee_saved):
        return self._probe_field(callee_saved, 'sp')


def detect_architecture():
    """
    Detect target architecture from GDB
    
    Returns:
        ArchitectureHandler instance for the detected architecture
        
    Note: Falls back to ARMCortexMHandler for unknown architectures
    """
    try:
        arch_str = gdb.execute('show architecture', to_string=True).lower()
        
        if 'arm' in arch_str or 'cortex' in arch_str:
            return ARMCortexMHandler()
        elif 'i386' in arch_str or 'x86-64' in arch_str:
            return x86Handler()
        elif 'arc' in arch_str:
            return ARCHandler()
        elif 'riscv' in arch_str:
            return RISCVHandler()
        else:
            print(f"Warning: Unknown architecture: {arch_str}")
            print("Thread awareness may not work correctly")
            print("Supported: ARM Cortex-M, x86/x86-64, ARC, RISC-V")
            return ARMCortexMHandler()  # Default fallback
    except:
        return ARMCortexMHandler()  # Default fallback


def discover_offsets_from_symbols():
    """Discover structure offsets from Zephyr exported symbols.

    Returns a dictionary of offsets, or None if symbols are not available.
    """
    try:
        offsets_symbol = gdb.lookup_symbol('_kernel_thread_info_offsets')
        if not offsets_symbol or not offsets_symbol[0]:
            return None

        num_symbol = gdb.lookup_symbol('_kernel_thread_info_num_offsets')
        num_offsets = 13
        if num_symbol and num_symbol[0]:
            try:
                num_offsets = int(num_symbol[0].value())
            except Exception:
                pass

        offsets_value = offsets_symbol[0].value()

        # Field names ordered to match the offset array indices
        OFFSET_FIELDS = [
            'version', 'k_curr_thread', 'k_threads', 't_entry',
            't_next_thread', 't_state', 't_user_options', 't_prio',
            't_stack_pointer', 't_name', 't_arch', 't_preempt_float',
            't_coop_float',
        ]

        try:
            offsets = {name: int(offsets_value[i])
                       for i, name in enumerate(OFFSET_FIELDS)
                       if num_offsets > i}
        except (IndexError, TypeError, gdb.error) as e:
            print(f"Warning: Failed to read some offsets: {e}")
            return None

        return offsets

    except (gdb.error, AttributeError, TypeError):
        return None


def adapt_offsets_to_structure(symbol_offsets):
    """Convert flat symbol offset dict to the nested structure used by discover_threads."""
    offsets = {
        'kernel': {
            'threads': symbol_offsets.get('k_threads', 0),
            'current': symbol_offsets.get('k_curr_thread', 0),
        },
        'thread': {
            'next_thread': symbol_offsets.get('t_next_thread', 0),
            'stack_pointer': symbol_offsets.get('t_stack_pointer', 0),
            'state': symbol_offsets.get('t_state', 0),
            'prio': symbol_offsets.get('t_prio', 0),
            'name': symbol_offsets.get('t_name', 0),
            'entry': symbol_offsets.get('t_entry', 0),
        }
    }
    if 'version' in symbol_offsets:
        offsets['version'] = symbol_offsets['version']
    return offsets


def get_hardcoded_offsets():
    """Return offsets that defer to GDB's type system (None = use field access)."""
    return {
        'kernel': {'threads': None},
        'thread': {'next_thread': None},
    }


def get_kernel_offsets():
    """Return structure offsets, using symbol-based discovery or hardcoded fallback.

    Controlled by _discovery_mode: 'auto', 'symbols', or 'hardcoded'.
    Returns None if forced symbol mode fails.

    This function is only called at script load time and when the user
    explicitly invokes ``zephyr-discovery``.  The result is stored in
    ``_cached_offsets`` for use by ``discover_threads()``.
    """
    global _discovery_mode

    if _discovery_mode == 'hardcoded':
        print("Zephyr GDB: using hardcoded/GDB type system offsets")
        return get_hardcoded_offsets()

    symbol_offsets = discover_offsets_from_symbols()

    if symbol_offsets:
        offsets = adapt_offsets_to_structure(symbol_offsets)
        version = offsets.get('version', 'unknown')
        prefix = "forced " if _discovery_mode == 'symbols' else ""
        print(f"Zephyr GDB: {prefix}symbol-based discovery (version: {version})")
        return offsets

    if _discovery_mode == 'symbols':
        print("=" * 70)
        print("ERROR: symbol-based discovery forced but symbols not found.")
        print("Ensure CONFIG_DEBUG_THREAD_INFO=y or switch to 'auto'/'hardcoded' mode.")
        print("=" * 70)
        return None

    print("Zephyr GDB: using GDB type system offsets")
    print("Tip: enable CONFIG_DEBUG_THREAD_INFO=y for better Zephyr 3.x compatibility")
    return get_hardcoded_offsets()


def discover_threads(verbose=True):
    """Traverse the kernel thread list and populate thread_cache.

    verbose=False suppresses warnings during initial load when the
    inferior may not be running yet.

    Uses ``_cached_offsets`` which must have been set previously by
    ``get_kernel_offsets()`` (called at script load / ``zephyr-discovery``).
    """
    global thread_cache, current_thread_ptr, _hw_active_lwp
    
    # Reset LWP counter so IDs always start at 1
    ZephyrThread.next_lwp = 1
    
    try:
        # Get architecture handler
        arch = detect_architecture()
        
        # Use cached offsets — discovery is done elsewhere
        offsets = _cached_offsets
        
        # Check if offsets are available
        if offsets is None:
            if verbose:
                print("Thread discovery failed: No offsets available")
                print("Run 'zephyr-discovery' to configure offset discovery")
            return
        
        # Try to read the kernel structure
        try:
            kernel = gdb.parse_and_eval('_kernel')
        except:
            if verbose:
                print("Warning: Could not find '_kernel' symbol")
                print("Make sure you are debugging a Zephyr application")
                print("and that CONFIG_DEBUG_INFO=y is set")
            return
        # Try to get current thread
        try:
            # Look for current thread in per-CPU structure
            current_thread_ptr = kernel['cpus'][0]['current']
        except:
            try:
                # Alternative: try direct current field
                current_thread_ptr = kernel['current']
            except:
                if verbose:
                    print("Warning: Could not determine current thread")
                current_thread_ptr = None
        
        # Try to get thread list
        try:
            thread_list_head = kernel['threads']
        except:
            if verbose:
                print("Warning: Could not find thread list in kernel structure")
                print("Make sure CONFIG_THREAD_MONITOR=y is set in your Zephyr configuration")
            return
        
        if not thread_list_head or int(thread_list_head) == 0:
            if verbose:
                print("Warning: Thread list is empty")
            return
        
        # Traverse the thread list
        new_thread_list = []
        current_ptr = thread_list_head
        max_threads = 100  # Safety limit to prevent infinite loops
        
        while current_ptr and int(current_ptr) != 0 and len(new_thread_list) < max_threads:
            # Create or update thread object
            zt = ZephyrThread(current_ptr, offsets, arch)
            new_thread_list.append(zt)
            
            # Get next thread pointer
            try:
                thread_struct = current_ptr.dereference()
                current_ptr = thread_struct['next_thread']
                
                # Check if we've looped back to the start
                if current_ptr == thread_list_head:
                    break
            except:
                # End of list or error
                break
        
        # Update global thread cache
        old_thread_set = set(str(t.thread_ptr) for t in thread_cache)
        
        # Announce new threads
        for t in new_thread_list:
            if str(t.thread_ptr) not in old_thread_set:
                print(f"[New thread '{t.name}' (LWP {t.lwp})]")
        
        thread_cache = new_thread_list

        # Record which thread is hardware-active (actually running on CPU)
        _hw_active_lwp = None
        for t in thread_cache:
            if t.active:
                _hw_active_lwp = t.lwp
                break

        if len(thread_cache) == 0 and verbose:
            print("Warning: No threads discovered")
        
    except Exception as e:
        if verbose:
            print(f"Error discovering threads: {e}")
            import traceback
            traceback.print_exc()


def stop_handler(event=None):
    """Called when the inferior stops — restore real CPU regs and refresh threads."""
    global _real_cpu_regs
    # Restore real CPU registers if we previously swapped to a suspended thread
    if _real_cpu_regs is not None:
        try:
            gdb.execute(f'set $sp = 0x{_real_cpu_regs["sp"]:x}', to_string=True)
            gdb.execute(f'set $pc = 0x{_real_cpu_regs["pc"]:x}', to_string=True)
        except Exception:
            pass
        _real_cpu_regs = None
    discover_threads(verbose=False)


def continue_handler(event=None):
    pass


def exit_handler(event=None):
    """Called when the inferior exits — clear thread state."""
    global thread_cache, current_thread_ptr, _cached_offsets, _real_cpu_regs, _hw_active_lwp
    thread_cache = []
    current_thread_ptr = None
    _cached_offsets = None
    _real_cpu_regs = None
    _hw_active_lwp = None
    ZephyrThread.next_lwp = 1


class CommandInfoThreads(gdb.Command):
    """
    Override for GDB's 'info threads' command
    
    Displays all Zephyr threads with their state and current frame.
    """
    
    def __init__(self):
        super(CommandInfoThreads, self).__init__('info threads', gdb.COMMAND_USER)
    
    def invoke(self, arg, from_tty=False):
        """Execute the command"""
        if len(thread_cache) == 0:
            print("No threads.")
            return
        
        print("  Id   Target Id            Prio State Frame")
        for t in thread_cache:
            # Format state
            state_str = f"{t.state:3d}"
            
            # Format line
            active_marker = '*' if t.active else ' '
            print(f"{active_marker} {t.lwp:<4d} {t.name:<20s} {t.prio:4d} {state_str:5s} {t.frame_str}")


class CommandThread(gdb.Command):
    """
    Override for GDB's 'thread' command
    
    Allows switching between Zephyr threads for inspection.
    """
    
    def __init__(self):
        super(CommandThread, self).__init__('thread', gdb.COMMAND_USER)
    
    def invoke(self, arg, from_tty=False):
        """Execute the command"""
        if not arg:
            # Display current thread
            for t in thread_cache:
                if t.active:
                    print(f"[Current thread is {t.lwp} ({t.name})]")
                    return
            print("No current thread")
            return
        
        # Switch to specified thread
        try:
            target_lwp = int(arg)
        except ValueError:
            print(f"Invalid thread ID: {arg}")
            return
        
        found = False
        for t in thread_cache:
            if t.lwp == target_lwp:
                # Mark this thread as active
                t.active = True
                found = True
                print(f"[Switching to thread {t.lwp} ({t.name})]")
                # Note: Full register switching would be implemented here
            elif t.active:
                # Deactivate previously active thread
                t.active = False
        
        if not found:
            print(f"Thread ID {target_lwp} not known.")


class CommandZephyrDiscovery(gdb.Command):
    """Set the Zephyr thread discovery mode at runtime.

    Usage: zephyr-discovery [auto|symbols|hardcoded]

      auto      - Try symbol-based discovery first, fall back to hardcoded (default)
      symbols   - Force symbol-based discovery only (Zephyr 3.x+)
      hardcoded - Force hardcoded/GDB type system offsets (fastest; Zephyr 2.7+)

    With no argument, prints the current mode.
    """

    def __init__(self):
        super(CommandZephyrDiscovery, self).__init__('zephyr-discovery', gdb.COMMAND_USER)

    def invoke(self, arg, from_tty=False):
        global _discovery_mode, _cached_offsets
        arg = arg.strip().lower()
        if not arg:
            print(f"Zephyr discovery mode: {_discovery_mode}")
            if _cached_offsets is not None:
                print("Offsets: cached (use a mode argument to re-discover)")
            else:
                print("Offsets: not yet discovered")
            return
        if arg not in ('auto', 'symbols', 'hardcoded'):
            print(f"Unknown mode '{arg}'. Valid modes: auto, symbols, hardcoded")
            return
        _discovery_mode = arg
        # Re-discover offsets immediately with the new mode
        _cached_offsets = get_kernel_offsets()
        if _cached_offsets is not None:
            print(f"Zephyr discovery mode set to '{_discovery_mode}' (offsets updated)")
        else:
            print(f"Zephyr discovery mode set to '{_discovery_mode}' (offset discovery failed)")


# ---------------------------------------------------------------------------
# MI (Machine Interface) Commands — Centralized Registry
#
# These require GDB 12+ which introduced gdb.MICommand.  The commands use
# the "-override-" prefix so they can coexist alongside the built-in MI
# commands while providing Zephyr-aware thread information.
#
# In cortex-debug, set "overrideMICommands": true in launch.json to
# automatically route the standard MI commands to these overrides.
#
# ┌──────────────────────────────────┬──────────────────────────────────────┐
# │ Standard MI Command              │ Override MI Command                  │
# ├──────────────────────────────────┼──────────────────────────────────────┤
# │ -thread-info                     │ -override-thread-info                │
# │ -thread-list-ids                 │ -override-thread-list-ids            │
# │ -thread-select                   │ -override-thread-select              │
# │ -stack-list-frames               │ -override-stack-list-frames          │
# └──────────────────────────────────┴──────────────────────────────────────┘
#
# To add a new overridden MI command:
#   1. Create a class inheriting from gdb.MICommand (see examples below)
#   2. Add an entry to MI_COMMAND_REGISTRY at the bottom of this section
#   3. The command will be registered automatically at script load time
# ---------------------------------------------------------------------------

def _resolve_sal(pc):
    """Return (file, fullname, line) for a PC, or (None, None, None)."""
    try:
        sal = gdb.find_pc_line(pc)
        if sal.symtab:
            return sal.symtab.filename, sal.symtab.fullname(), str(sal.line)
    except Exception:
        pass
    return None, None, None


def _build_frame_dict(thread, include_args=True):
    """Build an MI-compatible frame dictionary for a Zephyr thread.

    Returns a dict with keys matching the standard MI frame tuple:
    level, addr, func, and optionally file, fullname, line, arch.

    When include_args=True (default), includes args=[] for use in
    -thread-info.  Set to False for -stack-list-frames which does not
    include args in the standard format.
    """
    frame = {"level": "0", "addr": "0x0", "func": "??"}
    if include_args:
        frame["args"] = []

    if thread.active:
        try:
            gdb_frame = gdb.newest_frame()
            pc = gdb_frame.pc()
            frame["addr"] = f"0x{pc:x}"
            frame["func"] = gdb_frame.name() or "??"
            sal = gdb_frame.find_sal()
            if sal.symtab:
                frame["file"] = sal.symtab.filename
                frame["fullname"] = sal.symtab.fullname()
                frame["line"] = str(sal.line)
            try:
                frame["arch"] = gdb_frame.architecture().name()
            except Exception:
                pass
        except Exception:
            pass
    else:
        try:
            if thread.callee_saved is not None and thread.arch:
                pc = thread.arch.get_thread_pc(thread.callee_saved)
                frame["addr"] = f"0x{pc:x}"
                try:
                    block = gdb.block_for_pc(pc)
                    while block and block.function is None:
                        block = block.superblock
                    frame["func"] = str(block.function) if block and block.function else "??"
                except Exception:
                    pass
                # Resolve source file and line
                f, fn, ln = _resolve_sal(pc)
                if f:
                    frame["file"] = f
                    frame["fullname"] = fn
                    frame["line"] = ln
        except Exception:
            pass

    return frame


# Saved real CPU registers — stored the first time we switch away from the
# hardware-active thread so we can restore them when switching back.
_real_cpu_regs = None  # dict {"sp": int, "pc": int} or None


def _switch_thread_context(target_thread):
    """Set SP/PC registers to *target_thread*'s saved context.

    Uses ``_hw_active_lwp`` (set by ``discover_threads`` on each stop) to
    decide whether the target is the hardware-running thread.  This is
    independent of the ``active`` flag which tracks the *selected* thread
    for the UI.

    When switching to the hardware-active thread, restores the real CPU
    registers saved earlier.  When switching to any other (suspended)
    thread, saves the current CPU registers (if not saved already) and
    sets SP/PC to the thread's callee-saved values so native GDB commands
    operate in the correct context.
    """
    global _real_cpu_regs

    if target_thread.lwp == _hw_active_lwp:
        # Switching back to the hardware-running thread — restore original regs
        if _real_cpu_regs is not None:
            try:
                gdb.execute(f'set $sp = 0x{_real_cpu_regs["sp"]:x}', to_string=True)
                gdb.execute(f'set $pc = 0x{_real_cpu_regs["pc"]:x}', to_string=True)
            except Exception:
                pass
            _real_cpu_regs = None
    else:
        # Switching to a suspended thread — save real regs, set to thread context
        if target_thread.callee_saved is not None and target_thread.arch:
            saved_pc = target_thread.arch.get_thread_pc(target_thread.callee_saved)
            saved_sp = target_thread.arch.get_thread_sp(target_thread.callee_saved)
            if saved_pc and saved_sp:
                try:
                    if _real_cpu_regs is None:
                        _real_cpu_regs = {
                            "sp": int(gdb.parse_and_eval('$sp')),
                            "pc": int(gdb.parse_and_eval('$pc')),
                        }
                    gdb.execute(f'set $sp = 0x{saved_sp:x}', to_string=True)
                    gdb.execute(f'set $pc = 0x{saved_pc:x}', to_string=True)
                except Exception:
                    pass


def _ensure_thread_cache():
    """Refresh the thread cache if it is empty (quiet — no console output)."""
    if len(thread_cache) == 0:
        discover_threads(verbose=False)


def _find_thread(lwp):
    """Return the ZephyrThread with the given LWP id, or None."""
    for t in thread_cache:
        if t.lwp == lwp:
            return t
    return None


def _get_current_thread_id():
    """Return the LWP id (as str) of the hardware-active thread, or None.

    This returns the thread that is actually executing on the CPU, not
    the thread that was most recently selected for viewing.
    """
    if _hw_active_lwp is not None:
        return str(_hw_active_lwp)
    # Fallback: look for the active flag
    for t in thread_cache:
        if t.active:
            return str(t.lwp)
    return None


def _is_valid_code_addr(pc):
    """Return True if *pc* looks like a plausible code address.

    On ARM Cortex-M the region 0xE000_0000–0xFFFF_FFFF is system/PPB
    space and never contains user code.  Address 0 is also invalid.
    """
    if pc == 0:
        return False
    if pc >= 0xE0000000:
        return False
    return True


def _build_frame_list(thread, low=None, high=None):
    """Return a list of MI-compatible frame dicts for *thread*.

    For the active (currently executing) thread the real GDB frame chain
    is walked.  For suspended threads we attempt to unwind the stack by
    temporarily setting SP/PC to the saved context and walking GDB's
    frame chain.  If that fails, a single synthetic frame is returned.

    *low* / *high* optionally limit the returned frame range (inclusive,
    0-based).
    """
    frames = []

    if thread.active:
        # Walk the real GDB frame chain
        try:
            frame = gdb.newest_frame()
            level = 0
            while frame is not None:
                pc = frame.pc()
                if not _is_valid_code_addr(pc):
                    break
                if (low is None or level >= low) and (high is None or level <= high):
                    fd = {"level": str(level), "addr": f"0x{pc:x}",
                          "func": frame.name() or "??"}
                    sal = frame.find_sal()
                    if sal.symtab:
                        fd["file"] = sal.symtab.filename
                        fd["fullname"] = sal.symtab.fullname()
                        fd["line"] = str(sal.line)
                    try:
                        fd["arch"] = frame.architecture().name()
                    except Exception:
                        pass
                    frames.append(fd)
                level += 1
                try:
                    frame = frame.older()
                except gdb.error:
                    break
        except Exception:
            frames.append(_build_frame_dict(thread, include_args=False))
    else:
        # Suspended thread — try to unwind via GDB by temporarily
        # setting SP and PC to the thread's saved context.
        unwound = False
        if thread.callee_saved is not None and thread.arch:
            saved_pc = thread.arch.get_thread_pc(thread.callee_saved)
            saved_sp = thread.arch.get_thread_sp(thread.callee_saved)
            if saved_pc and saved_sp:
                try:
                    # Save current register values
                    orig_sp = int(gdb.parse_and_eval('$sp'))
                    orig_pc = int(gdb.parse_and_eval('$pc'))

                    # Temporarily set SP/PC to the suspended thread's context
                    gdb.execute(f'set $sp = 0x{saved_sp:x}', to_string=True)
                    gdb.execute(f'set $pc = 0x{saved_pc:x}', to_string=True)

                    try:
                        frame = gdb.newest_frame()
                        level = 0
                        while frame is not None:
                            pc = frame.pc()
                            if not _is_valid_code_addr(pc):
                                break
                            if (low is None or level >= low) and (high is None or level <= high):
                                fd = {"level": str(level),
                                      "addr": f"0x{pc:x}",
                                      "func": frame.name() or "??"}
                                sal = frame.find_sal()
                                if sal.symtab:
                                    fd["file"] = sal.symtab.filename
                                    fd["fullname"] = sal.symtab.fullname()
                                    fd["line"] = str(sal.line)
                                try:
                                    fd["arch"] = frame.architecture().name()
                                except Exception:
                                    pass
                                frames.append(fd)
                            level += 1
                            try:
                                frame = frame.older()
                            except gdb.error:
                                break
                        if frames:
                            unwound = True
                    finally:
                        # Always restore original registers
                        gdb.execute(f'set $sp = 0x{orig_sp:x}', to_string=True)
                        gdb.execute(f'set $pc = 0x{orig_pc:x}', to_string=True)
                except Exception:
                    pass

        # Fallback: single synthetic frame from callee-saved context
        if not unwound:
            if low is None or low == 0:
                frames.append(_build_frame_dict(thread, include_args=False))

    return frames


# Guard: gdb.MICommand is only available in GDB 12+
if hasattr(gdb, 'MICommand'):

    def _parse_override_thread_argv(argv):
        """Parse ``--override-thread <id>`` from an MI argument list.

        Returns ``(thread_id_or_none, remaining_args)`` where *remaining_args*
        is the argv list with ``--override-thread <id>`` removed.

        This is the canonical way override MI commands accept a thread
        identifier when cortex-debug has ``overrideMICommands`` enabled.
        The option mirrors the standard ``--thread`` option but is prefixed
        so that GDB's MI layer does not intercept it.
        """
        thread_id = None
        remaining = []
        i = 0
        while i < len(argv):
            if argv[i] == '--override-thread' and i + 1 < len(argv):
                try:
                    thread_id = int(argv[i + 1])
                except ValueError:
                    raise gdb.GdbError(f"Invalid thread id: {argv[i + 1]}")
                i += 2
            else:
                remaining.append(argv[i])
                i += 1
        return thread_id, remaining

    class MIOverrideThreadInfo(gdb.MICommand):
        """MI command: -override-thread-info [id]

        Mirrors the built-in -thread-info command using Zephyr thread data.

        With no arguments all known threads are returned.  When an optional
        thread *id* is given, only that thread's information is returned.

        Result keys:
            threads  - list of thread description dicts
            current-thread-id - id of the currently active thread (if any)
        """

        def __init__(self):
            super().__init__('-override-thread-info')

        def invoke(self, argv):
            _ensure_thread_cache()

            # Accept --override-thread <id> or a positional id argument
            override_id, remaining = _parse_override_thread_argv(argv)
            filter_id = override_id
            if filter_id is None and remaining:
                try:
                    filter_id = int(remaining[0])
                except ValueError:
                    raise gdb.GdbError(f"Invalid thread id: {remaining[0]}")

            threads = []
            current_id = _get_current_thread_id()

            for t in thread_cache:
                if filter_id is not None and t.lwp != filter_id:
                    continue

                thread_dict = {
                    "id": str(t.lwp),
                    "target-id": f"Zephyr thread {t.lwp} ({t.name})",
                    "name": t.name or f"thread_{t.lwp}",
                    "state": "stopped",
                    "frame": _build_frame_dict(t),
                }
                threads.append(thread_dict)

            result = {"threads": threads}
            if current_id is not None:
                result["current-thread-id"] = current_id

            return result

    class MIOverrideThreadListIds(gdb.MICommand):
        """MI command: -override-thread-list-ids

        Mirrors the built-in -thread-list-ids command using Zephyr thread
        data.

        Result keys:
            thread-ids       - tuple containing thread-id list
            current-thread-id - id of the currently active thread (if any)
            number-of-threads - total number of known threads

        Note: The standard MI wire format uses repeated ``thread-id``
        keys inside a tuple (``{thread-id="1",thread-id="2"}``).
        Python dicts cannot have duplicate keys, so we emit
        ``thread-ids={thread-id=["1","2"]}`` instead.  MI clients
        that access ``result['thread-ids']['thread-id']`` will
        receive the id list either way.
        """

        def __init__(self):
            super().__init__('-override-thread-list-ids')

        def invoke(self, argv):
            _ensure_thread_cache()

            thread_ids = [str(t.lwp) for t in thread_cache]
            current_id = _get_current_thread_id()

            # Each ["thread-id", id] pair serializes as ["thread-id","N"]
            # which the MI parser treats identically to the standard
            # duplicate-key format thread-id="1",thread-id="2",...
            result = {
                "thread-ids": [["thread-id", tid] for tid in thread_ids],
                "number-of-threads": str(len(thread_cache)),
            }
            if current_id is not None:
                result["current-thread-id"] = current_id

            return result

    class MIOverrideThreadSelect(gdb.MICommand):
        """MI command: -override-thread-select <id>

        Mirrors the built-in -thread-select command using Zephyr thread
        data.  Switches the "current" thread to the one identified by *id*.

        For non-active threads, this also sets the CPU's SP and PC
        registers to the thread's saved context so that subsequent
        native GDB commands (e.g. -stack-list-variables, -var-create)
        work correctly without needing ``--thread``.

        For the active (currently executing) thread, registers are
        restored to the real CPU state.

        Result keys:
            new-thread-id - the id of the newly selected thread
            frame         - the top frame of the selected thread
        """

        def __init__(self):
            super().__init__('-override-thread-select')

        def invoke(self, argv):
            # Accept --override-thread <id> or a positional id argument
            override_id, remaining = _parse_override_thread_argv(argv)
            target_lwp = override_id
            if target_lwp is None:
                if not remaining:
                    raise gdb.GdbError("Thread id required")
                try:
                    target_lwp = int(remaining[0])
                except ValueError:
                    raise gdb.GdbError(f"Invalid thread id: {remaining[0]}")

            _ensure_thread_cache()

            target_thread = None
            prev_active = None
            for t in thread_cache:
                if t.lwp == target_lwp:
                    target_thread = t
                elif t.active:
                    prev_active = t

            if target_thread is None:
                raise gdb.GdbError(f"Thread ID {target_lwp} not known.")

            # Update active flags
            if prev_active:
                prev_active.active = False
            target_thread.active = True

            # Switch CPU register context
            _switch_thread_context(target_thread)

            return {
                "new-thread-id": str(target_thread.lwp),
                "frame": _build_frame_dict(target_thread),
            }

    class MIOverrideStackListFrames(gdb.MICommand):
        """MI command: -override-stack-list-frames --override-thread <id> [<low> <high>]

        Mirrors the built-in -stack-list-frames command using Zephyr thread
        data.

        The ``--override-thread <id>`` argument identifies the Zephyr
        thread.  (The standard ``--thread`` option cannot be used because
        GDB's MI layer intercepts it and attempts to switch to the thread
        natively, which fails for Zephyr threads.)

        Example:
            -override-stack-list-frames --override-thread 3 0 19

        Result keys:
            stack - list of frame dicts (level, addr, func, …)
        """

        def __init__(self):
            super().__init__('-override-stack-list-frames')

        def invoke(self, argv):
            _ensure_thread_cache()

            # Parse --override-thread <id> and optional <low> <high>
            thread_id, positional = _parse_override_thread_argv(argv)

            low = None
            high = None
            if len(positional) >= 2:
                try:
                    low = int(positional[0])
                    high = int(positional[1])
                except ValueError:
                    raise gdb.GdbError("Usage: -override-stack-list-frames "
                                       "--override-thread <id> [<low> <high>]")
            elif len(positional) == 1:
                raise gdb.GdbError("Usage: -override-stack-list-frames "
                                   "--override-thread <id> [<low> <high>]")

            # Resolve target thread
            if thread_id is not None:
                target = _find_thread(thread_id)
                if target is None:
                    raise gdb.GdbError(f"Thread ID {thread_id} not known.")
            else:
                # Default to the active thread
                target = None
                for t in thread_cache:
                    if t.active:
                        target = t
                        break
                if target is None:
                    raise gdb.GdbError("No active thread")

            frames = _build_frame_list(target, low=low, high=high)
            # Wrap each frame dict as ["frame", dict] so that GDB
            # serializes it as ["frame",{level="0",...}] which the MI
            # parser treats identically to frame={level="0",...} — the
            # format cortex-debug expects (accessed via @frame.level).
            return {"stack": [["frame", f] for f in frames]}

    # -------------------------------------------------------------------
    # MI Command Registry
    #
    # All override MI commands are listed here.  To add a new one, just
    # append a (class, description) tuple.  Registration happens once
    # at script load time (see below).
    # -------------------------------------------------------------------
    MI_COMMAND_REGISTRY = [
        (MIOverrideThreadInfo,      'thread-info       -> override-thread-info'),
        (MIOverrideThreadListIds,   'thread-list-ids   -> override-thread-list-ids'),
        (MIOverrideThreadSelect,    'thread-select     -> override-thread-select'),
        (MIOverrideStackListFrames, 'stack-list-frames -> override-stack-list-frames'),
    ]


# Register GDB event handlers
try:
    gdb.events.stop.connect(stop_handler)
    gdb.events.cont.connect(continue_handler)
    gdb.events.exited.connect(exit_handler)
except AttributeError:
    print("Warning: GDB event handlers not available")
    print("Thread awareness may not work correctly")

# Register custom commands
try:
    cmd_info_threads = CommandInfoThreads()
    cmd_thread = CommandThread()
    cmd_zephyr_discovery = CommandZephyrDiscovery()
except:
    print("Warning: Failed to register custom commands")

# Register MI commands from the centralized registry (requires GDB 12+)
if hasattr(gdb, 'MICommand'):
    _mi_instances = []
    try:
        for cmd_class, desc in MI_COMMAND_REGISTRY:
            _mi_instances.append(cmd_class())
        print(f"Registered {len(_mi_instances)} override MI command(s):")
        for _, desc in MI_COMMAND_REGISTRY:
            print(f"  {desc}")
    except Exception as e:
        print(f"Warning: Failed to register MI commands: {e}")
else:
    print("Note: gdb.MICommand not available (requires GDB 12+); "
          "-override-* MI commands disabled")

# Try initial thread discovery (silent mode - don't print warnings during load)
print("=" * 70)
print("Use 'info threads' to see Zephyr threads")
print("Use 'zephyr-discovery [auto|symbols|hardcoded]' to set discovery mode")
print("=" * 70)
print()

# Discover offsets at load time (silent if inferior not yet running)
try:
    _cached_offsets = get_kernel_offsets()
except:
    pass

try:
    discover_threads(verbose=False)
except:
    # Don't fail if initial discovery doesn't work
    # (inferior may not be running yet)
    pass
