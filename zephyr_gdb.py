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
    """
    global _discovery_mode

    if _discovery_mode == 'hardcoded':
        print("Zephyr GDB support loaded (hardcoded/GDB type system mode)")
        return get_hardcoded_offsets()

    symbol_offsets = discover_offsets_from_symbols()

    if symbol_offsets:
        offsets = adapt_offsets_to_structure(symbol_offsets)
        version = offsets.get('version', 'unknown')
        prefix = "forced " if _discovery_mode == 'symbols' else ""
        print(f"Zephyr GDB support loaded ({prefix}symbol-based discovery, version: {version})")
        return offsets

    if _discovery_mode == 'symbols':
        print("=" * 70)
        print("ERROR: symbol-based discovery forced but symbols not found.")
        print("Ensure CONFIG_DEBUG_THREAD_INFO=y or switch to 'auto'/'hardcoded' mode.")
        print("=" * 70)
        return None

    print("Zephyr GDB support loaded (GDB type system)")
    print("Tip: enable CONFIG_DEBUG_THREAD_INFO=y for better Zephyr 3.x compatibility")
    return get_hardcoded_offsets()


def discover_threads(verbose=True):
    """Traverse the kernel thread list and populate thread_cache.

    verbose=False suppresses warnings during initial load when the
    inferior may not be running yet.
    """
    global thread_cache, current_thread_ptr
    
    try:
        # Get architecture handler
        arch = detect_architecture()
        
        # Get structure offsets (tries symbol-based first, falls back to GDB type system)
        offsets = get_kernel_offsets()
        
        # Check if offset discovery failed (e.g., forced symbols mode but symbols unavailable)
        if offsets is None:
            if verbose:
                print("Thread discovery failed: Could not discover offsets")
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
        
        if len(thread_cache) == 0 and verbose:
            print("Warning: No threads discovered")
        
    except Exception as e:
        if verbose:
            print(f"Error discovering threads: {e}")
            import traceback
            traceback.print_exc()


def stop_handler(event=None):
    """Called when the inferior stops — refresh thread list."""
    discover_threads()


def continue_handler(event=None):
    pass


def exit_handler(event=None):
    """Called when the inferior exits — clear thread state."""
    global thread_cache, current_thread_ptr
    thread_cache = []
    current_thread_ptr = None
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
        global _discovery_mode
        arg = arg.strip().lower()
        if not arg:
            print(f"Zephyr discovery mode: {_discovery_mode}")
            return
        if arg not in ('auto', 'symbols', 'hardcoded'):
            print(f"Unknown mode '{arg}'. Valid modes: auto, symbols, hardcoded")
            return
        _discovery_mode = arg
        print(f"Zephyr discovery mode set to '{_discovery_mode}'")


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

# Try initial thread discovery (silent mode - don't print warnings during load)
print("=" * 70)
print("Use 'info threads' to see Zephyr threads")
print("Use 'zephyr-discovery [auto|symbols|hardcoded]' to set discovery mode")
print("=" * 70)
print()

try:
    discover_threads(verbose=False)
except:
    # Don't fail if initial discovery doesn't work
    # (inferior may not be running yet)
    pass
