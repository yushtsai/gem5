# Copyright (c) 2012-2013 ARM Limited
# All rights reserved.
#
# Simple test script for MSP432-like architecture
#
# "m5 test.py"

import argparse
import os
import sys

import m5
from m5.defines import buildEnv
from m5.objects import *
from m5.params import NULL
from m5.util import (
    addToPath,
    fatal,
    warn,
)

from gem5.isas import ISA

addToPath("../")

from common import (
    CacheConfig,
    CpuConfig,
    MemConfig,
    ObjectList,
    Options,
    Simulation,
)
from common.Caches import *
from common.cpu2000 import *
from common.FileSystemConfig import config_filesystem
from ruby import Ruby

# --- MSP432 Specific Configurations ---

class InstructionBuffer(NoncoherentCache):
    size = '512B'
    assoc = 2
    tag_latency = 1
    data_latency = 1
    response_latency = 1
    mshrs = 4
    tgts_per_mshr = 8
    # MSP432 prefetcher: 4-8 instructions
    # If using 32 bytes line size, we can prefetch 4-8 (if mix of 2/4 bytes)
    prefetcher = TaggedPrefetcher(degree=4)

class DataSRAM(NoncoherentCache):
    size = '64KiB'
    assoc = 1 # Direct mapped or single bank simulation
    tag_latency = 1
    data_latency = 1
    response_latency = 1
    mshrs = 4
    tgts_per_mshr = 8

def get_processes(args):
    """Interprets provided args and returns a list of processes"""

    multiprocesses = []
    inputs = []
    outputs = []
    errouts = []
    pargs = []

    workloads = args.cmd.split(";")
    if args.input != "":
        inputs = args.input.split(";")
    if args.output != "":
        outputs = args.output.split(";")
    if args.errout != "":
        errouts = args.errout.split(";")
    if args.options != "":
        pargs = args.options.split(";")

    idx = 0
    for wrkld in workloads:
        process = Process(pid=100 + idx)
        process.executable = wrkld
        process.cwd = os.getcwd()
        process.gid = os.getgid()

        if args.env:
            with open(args.env) as f:
                process.env = [line.rstrip() for line in f]

        if len(pargs) > idx:
            process.cmd = [wrkld] + pargs[idx].split()
        else:
            process.cmd = [wrkld]

        if len(inputs) > idx:
            process.input = inputs[idx]
        if len(outputs) > idx:
            process.output = outputs[idx]
        if len(errouts) > idx:
            process.errout = errouts[idx]

        multiprocesses.append(process)
        idx += 1

    if args.smt:
        cpu_type = ObjectList.cpu_list.get(args.cpu_type)
        assert ObjectList.is_o3_cpu(cpu_type), "SMT requires an O3CPU"
        return multiprocesses, idx
    else:
        return multiprocesses, 1


parser = argparse.ArgumentParser()
Options.addCommonOptions(parser)
Options.addSEOptions(parser)

# MSP432 Settings: 48MHz
parser.set_defaults(cpu_clock='48MHz', sys_clock='48MHz', mem_size='512MiB')

args = parser.parse_args()

multiprocesses = []
numThreads = 1

if args.bench:
    apps = args.bench.split("-")
    if len(apps) != args.num_cpus:
        print("number of benchmarks not equal to set num_cpus!")
        sys.exit(1)

    for app in apps:
        try:
            if ObjectList.cpu_list.get_isa(args.cpu_type) == ISA.ARM:
                # ARM workload setup
                pass
            multiprocesses.append(workload.makeProcess())
        except:
            print(f"Unable to find workload for: {app}", file=sys.stderr)
            sys.exit(1)
elif args.cmd:
    multiprocesses, numThreads = get_processes(args)
else:
    print("No workload specified. Exiting!\n", file=sys.stderr)
    sys.exit(1)


(CPUClass, test_mem_mode, FutureClass) = Simulation.setCPUClass(args)
CPUClass.numThreads = numThreads

# Check -- do not allow SMT with multiple CPUs
if args.smt and args.num_cpus > 1:
    fatal("You cannot use SMT with multiple CPUs!")

np = args.num_cpus
mp0_path = multiprocesses[0].executable
system = System(
    cpu=[CPUClass(cpu_id=i) for i in range(np)],
    mem_mode=test_mem_mode,
    mem_ranges=[AddrRange(args.mem_size)],
    cache_line_size=32 # 256 bits Instruction Buffer width
)

if numThreads > 1:
    system.multi_thread = True

system.voltage_domain = VoltageDomain(voltage=args.sys_voltage)
system.clk_domain = SrcClockDomain(
    clock=args.sys_clock, voltage_domain=system.voltage_domain
)
system.cpu_voltage_domain = VoltageDomain()
system.cpu_clk_domain = SrcClockDomain(
    clock=args.cpu_clock, voltage_domain=system.cpu_voltage_domain
)

for cpu in system.cpu:
    cpu.clk_domain = system.cpu_clk_domain

for i in range(np):
    if args.smt:
        system.cpu[i].workload = multiprocesses
    elif len(multiprocesses) == 1:
        system.cpu[i].workload = multiprocesses[0]
    else:
        system.cpu[i].workload = multiprocesses[i]

    system.cpu[i].createThreads()

# Noncoherent setup for MCU
system.membus = NoncoherentXBar()
system.membus.forward_latency = 3
system.membus.response_latency = 3
system.membus.frontend_latency = 1
system.membus.width = 8

# Flash Memory Simulation (Main Memory)
# Setting latency to mimic Slow Flash (e.g., 50ns)
# Note: SimpleMemory doesn't have latency params easily set here through MemConfig
# We'll use MemConfig with a specific type if possible or just rely on default.
# But for MSP432, 48MHz means 1 cycle is ~21ns. Flash access might be 150ns (7 cycles).
MemConfig.config_mem(args, system)

# Custom Cache setup for MSP432
for cpu in system.cpu:
    cpu.createInterruptController()
    if buildEnv["USE_X86_ISA"]:
        cpu.interrupts[0].pio = system.membus.mem_side_ports
        cpu.interrupts[0].int_master = system.membus.cpu_side_ports
        cpu.interrupts[0].int_slave = system.membus.mem_side_ports

    # Instruction Buffer
    cpu.icache = InstructionBuffer()
    cpu.icache.cpu_side = cpu.icache_port
    cpu.icache.mem_side = system.membus.cpu_side_ports

    # Data SRAM
    cpu.dcache = DataSRAM()
    cpu.dcache.cpu_side = cpu.dcache_port
    cpu.dcache.mem_side = system.membus.cpu_side_ports

system.system_port = system.membus.cpu_side_ports
config_filesystem(system, args)

system.workload = SEWorkload.init_compatible(mp0_path)

if args.wait_gdb:
    system.workload.wait_for_remote_gdb = True

root = Root(full_system=False, system=system)
Simulation.run(args, root, system, FutureClass)
