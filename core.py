# core.py: Core code for the 6800 CPU
# Copyright (C) 2020 Robert Baruch <robert.c.baruch@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

# Generate and verify code:
# python3 core.py --insn jmp generate -t il > core.il
# sby -f core.sby

# Simulate code:
# python3 core.py
# gtkwave test.gtkw

from enum import IntEnum
import importlib
from typing import List, Dict, Tuple, Optional

from formal.verification import FormalData, Verification
from alu8 import ALU8Func, ALU8
from consts.consts import ModeBits, Flags

from nmigen import Signal, Value, Elaboratable, Module, Cat, Const, Mux, signed
from nmigen import ClockDomain, ClockSignal
from nmigen.hdl.ast import Statement
from nmigen.asserts import Assert, Past, Cover, Assume
from nmigen.build import Platform
from nmigen.cli import main_parser, main_runner
from nmigen.back.pysim import Simulator, Delay


class Reg8(IntEnum):
    """Values for specifying an 8-bit register for things
    like sources and destinations. Can also specify the
    (H)igh or (L)ow 8 bits of a 16-bit signal."""
    NONE = 0
    A = 1
    B = 2
    XH = 3
    XL = 4
    SPH = 5
    SPL = 6
    PCH = 7
    PCL = 8
    TMP8 = 9
    TMP16H = 10
    TMP16L = 11
    DIN = 12
    DOUT = 13


class Reg16(IntEnum):
    """Values for specifying a 16-bit register for things
    like sources and destinations."""
    NONE = 0
    X = 1
    SP = 2
    PC = 3
    TMP16 = 4
    ADDR = 5


class Core(Elaboratable):
    """The core of the CPU. There is another layer which
    handles I/O for the actual pins."""

    reg8_map: Dict[IntEnum, Tuple[Signal, bool]]
    reg16_map: Dict[IntEnum, Tuple[Signal, bool]]

    def __init__(self, verification: Verification = None):
        self.Addr = Signal(16)
        self.Din = Signal(8)
        self.Dout = Signal(8)
        self.RW = Signal(reset=1)  # 1 = read, 0 = write
        self.VMA = Signal()  # 1 = address is valid

        # registers
        self.a = Signal(8, reset_less=True)
        self.b = Signal(8, reset_less=True)
        self.x = Signal(16, reset_less=True)
        self.sp = Signal(16, reset_less=True)
        self.pc = Signal(16, reset_less=True)
        self.instr = Signal(8, reset_less=True)
        self.tmp8 = Signal(8, reset_less=True)
        self.tmp16 = Signal(16, reset_less=True)

        # busses
        self.src8_1 = Signal(8)  # Input 1 of the ALU
        self.src8_2 = Signal(8)  # Input 2 of the ALU
        self.alu8 = Signal(8)   # Output from the ALU
        self.ccs = Signal(8)    # Flags from the ALU

        # selectors for busses
        self.src8_1_select = Signal(Reg8)
        self.src8_2_select = Signal(Reg8)

        # function control
        self.alu8_func = Signal(ALU8Func)

        # mappings of selectors to signals. The second tuple element is
        # whether the register is read/write.
        self.reg8_map = {
            Reg8.A: (self.a, True),
            Reg8.B: (self.b, True),
            Reg8.XH: (self.x[8:], True),
            Reg8.XL: (self.x[:8], True),
            Reg8.SPH: (self.sp[8:], True),
            Reg8.SPL: (self.sp[:8], True),
            Reg8.PCH: (self.pc[8:], True),
            Reg8.PCL: (self.pc[:8], True),
            Reg8.TMP8: (self.tmp8, True),
            Reg8.TMP16H: (self.tmp16[8:], True),
            Reg8.TMP16L: (self.tmp16[:8], True),
            Reg8.DIN: (self.Din, False),  # read-only register
            Reg8.DOUT: (self.Dout, True),
        }
        self.reg16_map = {
            Reg16.X: (self.x, True),
            Reg16.SP: (self.sp, True),
            Reg16.PC: (self.pc, True),
            Reg16.TMP16: (self.tmp16, True),
            Reg16.ADDR: (self.Addr, True),
        }

        # internal state
        self.reset_state = Signal(2)  # where we are during reset
        self.cycle = Signal(4)        # where we are during instr processing
        self.mode = Signal(2)         # mode bits, decoded by ModeBits

        self.end_instr_flag = Signal()    # performs end-of-instruction actions
        self.end_instr_addr = Signal(16)  # where the next instruction is

        # Formal verification
        self.verification = verification
        self.formalData = FormalData(verification)

    def ports(self) -> List[Signal]:
        return [self.Addr, self.Din, self.Dout, self.RW]

    def elaborate(self, platform: Platform) -> Module:
        m = Module()
        m.submodules.alu = alu = ALU8()

        # defaults
        m.d.comb += self.end_instr_flag.eq(0)
        m.d.comb += self.src8_1_select.eq(Reg8.NONE)
        m.d.comb += self.src8_2_select.eq(Reg8.NONE)
        m.d.comb += self.alu8_func.eq(ALU8Func.NONE)
        m.d.ph1 += self.VMA.eq(1)
        m.d.ph1 += self.cycle.eq(self.cycle + 1)

        # some common instruction decoding
        m.d.comb += self.mode.eq(self.instr[4:6])

        self.src_bus_setup(m, self.reg8_map, self.src8_1, self.src8_1_select)
        self.src_bus_setup(m, self.reg8_map, self.src8_2, self.src8_2_select)

        m.d.comb += alu.input1.eq(self.src8_1)
        m.d.comb += alu.input2.eq(self.src8_2)
        m.d.comb += self.alu8.eq(alu.output)
        m.d.comb += alu.func.eq(self.alu8_func)
        m.d.comb += self.ccs.eq(alu.ccs)

        self.reset_handler(m)
        with m.If(self.reset_state == 3):
            with m.If(self.cycle == 0):
                self.fetch(m)
            with m.Else():
                self.execute(m)
        self.maybe_do_formal_verification(m)
        self.end_instr_flag_handler(m)

        return m

    def src_bus_setup(self, m: Module, reg_map: Dict[IntEnum, Tuple[Signal, bool]], bus: Signal, selector: Signal):
        with m.Switch(selector):
            for e, reg in reg_map.items():
                with m.Case(e):
                    m.d.comb += bus.eq(reg[0])
            with m.Default():
                m.d.comb += bus.eq(0)

    def dest_bus_setup(self, m: Module, reg_map: Dict[IntEnum, Tuple[Signal, bool]], bus: Signal, bitmap: Signal):
        for e, reg in reg_map.items():
            if reg[1]:
                with m.If(bitmap[e.value]):
                    m.d.ph1 += reg[0].eq(bus)

    def reset_handler(self, m: Module):
        """Generates logic for reading the reset vector at 0xFFFE
        and jumping there."""
        with m.Switch(self.reset_state):
            with m.Case(0):
                m.d.ph1 += self.Addr.eq(0xFFFE)
                m.d.ph1 += self.RW.eq(1)
                m.d.ph1 += self.reset_state.eq(1)
            with m.Case(1):
                m.d.ph1 += self.Addr.eq(0xFFFF)
                m.d.ph1 += self.RW.eq(1)
                m.d.ph1 += self.tmp8.eq(self.Din)
                m.d.ph1 += self.reset_state.eq(2)
            with m.Case(2):
                m.d.ph1 += self.reset_state.eq(3)
                reset_vec = Cat(self.Din, self.tmp8)
                self.end_instr(m, reset_vec)

    def end_instr_flag_handler(self, m: Module):
        """Generates logic for handling the end of an instruction."""
        with m.If(self.end_instr_flag):
            m.d.ph1 += self.pc.eq(self.end_instr_addr)
            m.d.ph1 += self.Addr.eq(self.end_instr_addr)
            m.d.ph1 += self.RW.eq(1)
            m.d.ph1 += self.cycle.eq(0)

    def fetch(self, m: Module):
        """Fetch the opcode at PC, which should already be on the address lines.
        The opcode is on the data lines by the end of the cycle.
        We always increment PC and Addr and go to instruction cycle 1."""
        m.d.ph1 += self.instr.eq(self.Din)
        m.d.ph1 += self.RW.eq(1)
        m.d.ph1 += self.pc.eq(self.pc + 1)
        m.d.ph1 += self.Addr.eq(self.pc + 1)

    def maybe_do_formal_verification(self, m: Module):
        """If formal verification is enabled, take pre- and post-snapshots, and do asserts.

        A pre-snapshot is taken of the registers when self.Din is the instruction we're
        looking for, and we're on cycle 0. We use Din because Din -> instr only at the
        *end* of cycle 0.

        A post-snapshot is taken of the registers during cycle 0 of the *next* instruction.
        It's not really a "snapshot", in that the CPU state aren't stored. All verification
        takes place using combinatorial statements.
        """
        if self.verification is not None:
            with m.If((self.cycle == 0) & (self.reset_state == 3)):
                with m.If(self.verification.valid(self.Din)):
                    self.formalData.preSnapshot(
                        m, self.Din, self.ccs, self.a, self.b, self.x, self.sp, self.pc)
                with m.Else():
                    self.formalData.noSnapshot(m)

                with m.If(self.formalData.snapshot_taken):
                    self.formalData.postSnapshot(
                        m, self.ccs, self.a, self.b, self.x, self.sp, self.pc)
                    self.verification.check(m, self.instr, self.formalData)

    def execute(self, m: Module):
        """Execute the instruction in the instr register."""
        with m.Switch(self.instr):
            with m.Case("00000001"):  # NOP
                self.NOP(m)
            with m.Case("00000110"):  # TAP
                self.TAP(m)
            with m.Case("00000111"):  # TPA
                self.TPA(m)
            with m.Case("0000100-"):  # INX/DEX
                self.IN_DE_X(m)
            with m.Case("0000101-"):  # CLV, SEV
                self.CL_SE_V(m)
            with m.Case("0000110-"):  # CLC, SEC
                self.CL_SE_C(m)
            with m.Case("0000111-"):  # CLI, SEI
                self.CL_SE_I(m)
            with m.Case("0010----"):  # Branch instructions
                self.BR(m)
            with m.Case("01--0000"):  # NEG
                self.ALU2(m, ALU8Func.SUB, 0, 1)
            with m.Case("01--0011"):  # COM
                self.ALU2(m, ALU8Func.COM, 0, 1)
            with m.Case("01--0100"):  # LSR
                self.ALU2(m, ALU8Func.LSR, 0, 1)
            with m.Case("01--0110"):  # ROR
                self.ALU2(m, ALU8Func.ROR, 0, 1)
            with m.Case("01--0111"):  # ASR
                self.ALU2(m, ALU8Func.ASR, 0, 1)
            with m.Case("01--1000"):  # ASL
                self.ALU2(m, ALU8Func.ASL, 0, 1)
            with m.Case("01--1001"):  # ROL
                self.ALU2(m, ALU8Func.ROL, 0, 1)
            with m.Case("01--1010"):  # DEC
                self.ALU2(m, ALU8Func.DEC, 0, 1)
            with m.Case("01--1100"):  # INC
                self.ALU2(m, ALU8Func.INC, 0, 1)
            with m.Case("01--1101"):  # TST
                self.ALU2(m, ALU8Func.SUB, 1, 0, store=False)
            with m.Case("011-1110"):  # JMP
                self.JMP(m)
            with m.Case("01--1111"):  # CLR
                self.ALU2(m, ALU8Func.SUB, 1, 1)
            with m.Case("1---0110"):  # LDA
                self.ALU(m, ALU8Func.LD)
            with m.Case("1---0000"):  # SUB
                self.ALU(m, ALU8Func.SUB)
            with m.Case("1---0001"):  # CMP
                self.ALU(m, ALU8Func.SUB, store=False)
            with m.Case("1---0010"):  # SBC
                self.ALU(m, ALU8Func.SBC)
            with m.Case("1---0100"):  # AND
                self.ALU(m, ALU8Func.AND)
            with m.Case("1---0101"):  # BIT
                self.ALU(m, ALU8Func.AND, store=False)
            with m.Case("1--10111", "1-100111"):  # STA
                self.STA(m)
            with m.Case("1---1000"):  # EOR
                self.ALU(m, ALU8Func.EOR)
            with m.Case("1---1001"):  # ADC
                self.ALU(m, ALU8Func.ADC)
            with m.Case("1---1010"):  # ORA
                self.ALU(m, ALU8Func.ORA)
            with m.Case("1---1011"):  # ADD
                self.ALU(m, ALU8Func.ADD)
            with m.Default():  # Illegal
                self.end_instr(m, self.pc)

    def read_byte(self, m: Module, cycle: int, addr: Statement, comb_dest: Signal):
        """Reads a byte starting from the given cycle.

        The byte read is combinatorically placed in comb_dest. If, however, comb_dest
        is None, then the byte read is in Din. In either case, that value is only
        valid for the cycle after the given cycle. If you want it after that, you will
        have to store it yourself.
        """
        with m.If(self.cycle == cycle):
            m.d.ph1 += self.Addr.eq(addr)
            m.d.ph1 += self.RW.eq(1)

        with m.If(self.cycle == cycle + 1):
            if comb_dest is not None:
                m.d.comb += comb_dest.eq(self.Din)
            if self.verification is not None:
                self.formalData.read(m, self.Addr, self.Din)

    def ALU(self, m: Module, func: ALU8Func, store: bool = True):
        b = self.instr[6]

        with m.If(self.mode == ModeBits.DIRECT.value):
            operand = self.mode_direct(m)
            self.read_byte(m, cycle=1, addr=operand, comb_dest=self.src8_2)

            with m.If(self.cycle == 2):
                m.d.comb += self.src8_1.eq(Mux(b, self.b, self.a))
                m.d.comb += self.alu8_func.eq(func)
                if store:
                    with m.If(b):
                        m.d.ph1 += self.b.eq(self.alu8)
                    with m.Else():
                        m.d.ph1 += self.a.eq(self.alu8)
                self.end_instr(m, self.pc)

        with m.Elif(self.mode == ModeBits.EXTENDED.value):
            operand = self.mode_ext(m)
            self.read_byte(m, cycle=2, addr=operand, comb_dest=self.src8_2)

            with m.If(self.cycle == 3):
                m.d.comb += self.src8_1.eq(Mux(b, self.b, self.a))
                m.d.comb += self.alu8_func.eq(func)
                if store:
                    with m.If(b):
                        m.d.ph1 += self.b.eq(self.alu8)
                    with m.Else():
                        m.d.ph1 += self.a.eq(self.alu8)
                self.end_instr(m, self.pc)

        with m.Elif(self.mode == ModeBits.IMMEDIATE.value):
            operand = self.mode_immediate8(m)

            with m.If(self.cycle == 2):
                m.d.comb += self.src8_1.eq(Mux(b, self.b, self.a))
                m.d.comb += self.src8_2.eq(operand)
                m.d.comb += self.alu8_func.eq(func)
                if store:
                    with m.If(b):
                        m.d.ph1 += self.b.eq(self.alu8)
                    with m.Else():
                        m.d.ph1 += self.a.eq(self.alu8)
                self.end_instr(m, self.pc)

        with m.Elif(self.mode == ModeBits.INDEXED.value):
            operand = self.mode_indexed(m)
            self.read_byte(m, cycle=3, addr=operand, comb_dest=self.src8_2)

            with m.If(self.cycle == 4):
                m.d.comb += self.src8_1.eq(Mux(b, self.b, self.a))
                m.d.comb += self.alu8_func.eq(func)
                if store:
                    with m.If(b):
                        m.d.ph1 += self.b.eq(self.alu8)
                    with m.Else():
                        m.d.ph1 += self.a.eq(self.alu8)
                self.end_instr(m, self.pc)

    def ALU2(self, m: Module, func: ALU8Func, operand1: int, operand2: int, store: bool = True):
        with m.If(self.mode == ModeBits.A.value):
            m.d.comb += self.src8_1.eq(Mux(operand1, self.a, 0))
            m.d.comb += self.src8_2.eq(Mux(operand2, self.a, 0))
            m.d.comb += self.alu8_func.eq(func)
            if store:
                m.d.ph1 += self.a.eq(self.alu8)
            self.end_instr(m, self.pc)

        with m.Elif(self.mode == ModeBits.B.value):
            m.d.comb += self.src8_1.eq(Mux(operand1, self.b, 0))
            m.d.comb += self.src8_2.eq(Mux(operand2, self.b, 0))
            m.d.comb += self.alu8_func.eq(func)
            if store:
                m.d.ph1 += self.b.eq(self.alu8)
            self.end_instr(m, self.pc)

        with m.Elif(self.mode == ModeBits.EXTENDED.value):
            operand = self.mode_ext(m)
            self.read_byte(m, cycle=2, addr=operand, comb_dest=None)

            with m.If(self.cycle == 3):
                m.d.comb += self.src8_1.eq(Mux(operand1, self.Din, 0))
                m.d.comb += self.src8_2.eq(Mux(operand2, self.Din, 0))
                m.d.comb += self.alu8_func.eq(func)
                # Output during cycle 4:
                m.d.ph1 += self.tmp8.eq(self.alu8)
                m.d.ph1 += self.VMA.eq(0)
                m.d.ph1 += self.Addr.eq(operand)
                m.d.ph1 += self.RW.eq(1)

            with m.Elif(self.cycle == 4):
                # Output during cycle 5:
                m.d.ph1 += self.Addr.eq(operand)
                m.d.ph1 += self.Dout.eq(self.tmp8)
                m.d.ph1 += self.RW.eq(0)
                if not store:
                    m.d.ph1 += self.VMA.eq(0)

            with m.Elif(self.cycle == 5):
                if store:
                    if self.verification is not None:
                        self.formalData.write(m, self.Addr, self.Dout)
                self.end_instr(m, self.pc)

        with m.Elif(self.mode == ModeBits.INDEXED.value):
            operand = self.mode_indexed(m)
            self.read_byte(m, cycle=3, addr=operand, comb_dest=None)

            with m.If(self.cycle == 4):
                m.d.comb += self.src8_1.eq(Mux(operand1, self.Din, 0))
                m.d.comb += self.src8_2.eq(Mux(operand2, self.Din, 0))
                m.d.comb += self.alu8_func.eq(func)
                # Output during cycle 5:
                m.d.ph1 += self.tmp8.eq(self.alu8)
                m.d.ph1 += self.VMA.eq(0)
                m.d.ph1 += self.Addr.eq(operand)
                m.d.ph1 += self.RW.eq(1)

            with m.If(self.cycle == 5):
                # Output during cycle 6:
                m.d.ph1 += self.Addr.eq(operand)
                m.d.ph1 += self.Dout.eq(self.tmp8)
                m.d.ph1 += self.RW.eq(0)
                if not store:
                    m.d.ph1 += self.VMA.eq(0)

            with m.If(self.cycle == 6):
                if store:
                    if self.verification is not None:
                        self.formalData.write(m, self.Addr, self.Dout)
                self.end_instr(m, self.pc)

    def BR(self, m: Module):
        operand = self.mode_immediate8(m)

        relative = Signal(signed(8))
        m.d.comb += relative.eq(operand)

        # At this point, pc is the instruction start + 2, so we just
        # add the signed relative offset to get the target.
        with m.If(self.cycle == 2):
            m.d.ph1 += self.tmp16.eq(self.pc + relative)

        with m.If(self.cycle == 3):
            take_branch = self.branch_check(m)
            self.end_instr(m, Mux(take_branch, self.tmp16, self.pc))

    def CL_SE_C(self, m: Module):
        """Clears or sets Carry."""
        with m.If(self.cycle == 1):
            m.d.comb += self.alu8_func.eq(
                Mux(self.instr[0], ALU8Func.SEC, ALU8Func.CLC))
            self.end_instr(m, self.pc)

    def CL_SE_V(self, m: Module):
        """Clears or sets Overflow."""
        with m.If(self.cycle == 1):
            m.d.comb += self.alu8_func.eq(
                Mux(self.instr[0], ALU8Func.SEV, ALU8Func.CLV))
            self.end_instr(m, self.pc)

    def CL_SE_I(self, m: Module):
        """Clears or sets Interrupt."""
        with m.If(self.cycle == 1):
            m.d.comb += self.alu8_func.eq(
                Mux(self.instr[0], ALU8Func.SEI, ALU8Func.CLI))
            self.end_instr(m, self.pc)

    def IN_DE_X(self, m: Module):
        """Increments or decrements X."""
        dec = self.instr[0]

        with m.If(self.cycle == 1):
            m.d.ph1 += self.VMA.eq(0)
            m.d.ph1 += self.Addr.eq(self.x)
            m.d.ph1 += self.x.eq(Mux(dec, self.x - 1, self.x + 1))

        with m.If(self.cycle == 2):
            m.d.ph1 += self.VMA.eq(0)
            m.d.ph1 += self.Addr.eq(self.x)

        with m.If(self.cycle == 3):
            m.d.comb += self.alu8_func.eq(
                Mux(self.x == 0, ALU8Func.SEZ, ALU8Func.CLZ))
            self.end_instr(m, self.pc)

    def JMP(self, m: Module):
        with m.If(self.mode == ModeBits.EXTENDED.value):
            operand = self.mode_ext(m)

            with m.If(self.cycle == 2):
                self.end_instr(m, operand)

        with m.Elif(self.mode == ModeBits.INDEXED.value):
            operand = self.mode_indexed(m)

            with m.If(self.cycle == 3):
                self.end_instr(m, operand)

    def NOP(self, m: Module):
        self.end_instr(m, self.pc)

    def STA(self, m: Module):
        b = self.instr[6]

        with m.If(self.mode == ModeBits.DIRECT.value):
            operand = self.mode_direct(m)

            with m.If(self.cycle == 1):
                # Output during cycle 2:
                m.d.ph1 += self.VMA.eq(0)
                m.d.ph1 += self.Addr.eq(operand)
                m.d.ph1 += self.RW.eq(1)

            with m.If(self.cycle == 2):
                # Output during cycle 3:
                m.d.ph1 += self.Addr.eq(operand)
                m.d.ph1 += self.Dout.eq(Mux(b, self.b, self.a))
                m.d.ph1 += self.RW.eq(0)

            with m.If(self.cycle == 3):
                if self.verification is not None:
                    self.formalData.write(m, self.Addr, self.Dout)
                m.d.comb += self.src8_2.eq(Mux(b, self.b, self.a))
                m.d.comb += self.alu8_func.eq(ALU8Func.LD)
                self.end_instr(m, self.pc)

        with m.Elif(self.mode == ModeBits.EXTENDED.value):
            operand = self.mode_ext(m)

            with m.If(self.cycle == 2):
                # Output during cycle 3:
                m.d.ph1 += self.VMA.eq(0)
                m.d.ph1 += self.Addr.eq(operand)
                m.d.ph1 += self.RW.eq(1)

            with m.If(self.cycle == 3):
                # Output during cycle 4:
                m.d.ph1 += self.Addr.eq(operand)
                m.d.ph1 += self.Dout.eq(Mux(b, self.b, self.a))
                m.d.ph1 += self.RW.eq(0)

            with m.If(self.cycle == 4):
                if self.verification is not None:
                    self.formalData.write(m, self.Addr, self.Dout)
                m.d.comb += self.src8_2.eq(Mux(b, self.b, self.a))
                m.d.comb += self.alu8_func.eq(ALU8Func.LD)
                self.end_instr(m, self.pc)

        with m.Elif(self.mode == ModeBits.INDEXED.value):
            operand = self.mode_indexed(m)

            with m.If(self.cycle == 3):
                # Output during cycle 4:
                m.d.ph1 += self.VMA.eq(0)
                m.d.ph1 += self.Addr.eq(operand)
                m.d.ph1 += self.RW.eq(1)

            with m.If(self.cycle == 4):
                # Output during cycle 5:
                m.d.ph1 += self.Addr.eq(operand)
                m.d.ph1 += self.Dout.eq(Mux(b, self.b, self.a))
                m.d.ph1 += self.RW.eq(0)

            with m.If(self.cycle == 5):
                if self.verification is not None:
                    self.formalData.write(m, self.Addr, self.Dout)
                m.d.comb += self.src8_2.eq(Mux(b, self.b, self.a))
                m.d.comb += self.alu8_func.eq(ALU8Func.LD)
                self.end_instr(m, self.pc)

    def TAP(self, m: Module):
        """Transfer A to CCS."""
        with m.If(self.cycle == 1):
            m.d.comb += self.alu8_func.eq(ALU8Func.TAP)
            m.d.comb += self.src8_1.eq(self.a)
            self.end_instr(m, self.pc)

    def TPA(self, m: Module):
        """Transfer CCS to A."""
        with m.If(self.cycle == 1):
            m.d.comb += self.alu8_func.eq(ALU8Func.TPA)
            m.d.ph1 += self.a.eq(self.alu8)
            self.end_instr(m, self.pc)

    def branch_check(self, m: Module) -> Signal:
        """Generates logic for a 1-bit value for branching.

        Returns a 1-bit Signal which is set if the branch should be
        taken. The branch logic is determined by the instruction.
        """
        invert = self.instr[0]
        cond = Signal()
        take_branch = Signal()

        with m.Switch(self.instr[1:4]):
            with m.Case("000"):  # BRA, BRN
                m.d.comb += cond.eq(1)
            with m.Case("001"):  # BHI, BLS
                m.d.comb += cond.eq(~(self.ccs[Flags.C] | self.ccs[Flags.Z]))
            with m.Case("010"):  # BCC, BCS
                m.d.comb += cond.eq(~self.ccs[Flags.C])
            with m.Case("011"):  # BNE, BEQ
                m.d.comb += cond.eq(~self.ccs[Flags.Z])
            with m.Case("100"):  # BVC, BVS
                m.d.comb += cond.eq(~self.ccs[Flags.V])
            with m.Case("101"):  # BPL, BMI
                m.d.comb += cond.eq(~self.ccs[Flags.N])
            with m.Case("110"):  # BGE, BLT
                m.d.comb += cond.eq(~(self.ccs[Flags.N] ^ self.ccs[Flags.V]))
            with m.Case("111"):  # BGT, BLE
                m.d.comb += cond.eq(~(self.ccs[Flags.Z] |
                                      (self.ccs[Flags.N] ^ self.ccs[Flags.V])))

        m.d.comb += take_branch.eq(cond ^ invert)
        return take_branch

    def mode_immediate8(self, m: Module) -> Statement:
        """Generates logic to get the 8-bit operand for immediate mode instructions.

        Returns a Statement containing an 8-bit operand.
        After cycle 1, tmp8 contains the operand.
        """
        operand = Mux(self.cycle == 1, self.Din, self.tmp8)

        with m.If(self.cycle == 1):
            m.d.ph1 += self.tmp8.eq(self.Din)
            m.d.ph1 += self.pc.eq(self.pc + 1)
            m.d.ph1 += self.Addr.eq(self.pc + 1)
            m.d.ph1 += self.RW.eq(1)
            if self.verification is not None:
                self.formalData.read(m, self.Addr, self.Din)

        return operand

    def mode_direct(self, m: Module) -> Statement:
        """Generates logic to get the 8-bit zero-page address for direct mode instructions.

        Returns a Statement containing a 16-bit address where the upper byte is zero.
        After cycle 1, tmp16 contains the address.
        """
        operand = Mux(self.cycle == 1, self.Din, self.tmp16)

        with m.If(self.cycle == 1):
            m.d.ph1 += self.tmp16[8:].eq(0)
            m.d.ph1 += self.tmp16[:8].eq(self.Din)
            m.d.ph1 += self.pc.eq(self.pc + 1)
            m.d.ph1 += self.Addr.eq(self.pc + 1)
            m.d.ph1 += self.RW.eq(1)
            if self.verification is not None:
                self.formalData.read(m, self.Addr, self.Din)

        return operand

    def mode_indexed(self, m: Module) -> Statement:
        """Generates logic to get the 16-bit address for indexed mode instructions.

        Returns a Statement containing a 16-bit address.
        After cycle 2, tmp16 contains the address. The address is not valid until after
        cycle 2.
        """
        operand = self.tmp16

        with m.If(self.cycle == 1):
            # Output during cycle 2:
            m.d.ph1 += self.tmp16[8:].eq(0)
            m.d.ph1 += self.tmp16[:8].eq(self.Din)
            m.d.ph1 += self.pc.eq(self.pc + 1)
            m.d.ph1 += self.Addr.eq(self.pc + 1)
            m.d.ph1 += self.RW.eq(1)
            m.d.ph1 += self.VMA.eq(0)
            if self.verification is not None:
                self.formalData.read(m, self.Addr, self.Din)

        with m.If(self.cycle == 2):
            # Output during cycle 3:
            m.d.ph1 += self.tmp16.eq(self.tmp16 + self.x)
            m.d.ph1 += self.VMA.eq(0)

        return operand

    def mode_ext(self, m: Module) -> Statement:
        """Generates logic to get the 16-bit address for extended mode instructions.

        Returns a Statement containing the 16-bit address. After cycle 2, tmp16 
        contains the address.
        """
        operand = Mux(self.cycle == 2, Cat(
            self.Din, self.tmp16[8:]), self.tmp16)

        with m.If(self.cycle == 1):
            m.d.ph1 += self.tmp16[8:].eq(self.Din)
            m.d.ph1 += self.pc.eq(self.pc + 1)
            m.d.ph1 += self.Addr.eq(self.pc + 1)
            m.d.ph1 += self.RW.eq(1)
            if self.verification is not None:
                self.formalData.read(m, self.Addr, self.Din)

        with m.If(self.cycle == 2):
            m.d.ph1 += self.tmp16[:8].eq(self.Din)
            m.d.ph1 += self.pc.eq(self.pc + 1)
            if self.verification is not None:
                self.formalData.read(m, self.Addr, self.Din)

        return operand

    def end_instr(self, m: Module, addr: Statement):
        """Ends the instruction.

        Loads the PC and Addr register with the given addr, sets R/W mode
        to read, and sets the cycle to 0 at the end of the current cycle.
        """
        m.d.comb += self.end_instr_addr.eq(addr)
        m.d.comb += self.end_instr_flag.eq(1)


if __name__ == "__main__":
    parser = main_parser()
    parser.add_argument("--insn")
    args = parser.parse_args()

    verification: Optional[Verification] = None
    if args.insn is not None:
        module = importlib.import_module(f"formal.formal_{args.insn}")
        formal_class = getattr(module, "Formal")
        verification = formal_class()

    m = Module()
    m.submodules.core = core = Core(verification)
    m.domains.ph1 = ph1 = ClockDomain("ph1")

    rst = Signal()
    ph1clk = ClockSignal("ph1")
    ph1.rst = rst

    if verification is not None:
        # Cycle counter
        cycle2 = Signal(6, reset_less=True)
        m.d.ph1 += cycle2.eq(cycle2 + 1)

        # Force a reset
        # m.d.comb += Assume(rst == (cycle2 < 8))

        with m.If(cycle2 == 20):
            m.d.ph1 += Cover(core.formalData.snapshot_taken &
                             core.end_instr_flag)
            m.d.ph1 += Assume(core.formalData.snapshot_taken &
                              core.end_instr_flag)

        # Verify reset does what it's supposed to
        with m.If(Past(rst, 4) & ~Past(rst, 3) & ~Past(rst, 2) & ~Past(rst)):
            m.d.ph1 += Assert(Past(core.Addr, 2) == 0xFFFE)
            m.d.ph1 += Assert(Past(core.Addr) == 0xFFFF)
            m.d.ph1 += Assert(core.Addr[8:] == Past(core.Din, 2))
            m.d.ph1 += Assert(core.Addr[:8] == Past(core.Din))
            m.d.ph1 += Assert(core.Addr == core.pc)

        main_runner(parser, args, m, ports=core.ports() + [ph1clk, rst])

    else:
        # Fake memory
        mem = {
            0xFFFE: 0x12,
            0xFFFF: 0x34,
            0x1234: 0x20,  # BRA 0x1234
            0x1235: 0xFE,
            0x1236: 0x01,  # NOP
            0xA010: 0x01,  # NOP
        }
        with m.Switch(core.Addr):
            for addr, data in mem.items():
                with m.Case(addr):
                    m.d.comb += core.Din.eq(data)
            with m.Default():
                m.d.comb += core.Din.eq(0xFF)

        sim = Simulator(m)
        sim.add_clock(1e-6, domain="ph1")

        def process():
            yield
            yield
            yield
            yield
            yield
            yield
            yield
            yield
            yield
            yield
            yield
            yield
            yield
            yield
            yield
            yield

        sim.add_sync_process(process, domain="ph1")
        with sim.write_vcd("test.vcd", "test.gtkw", traces=core.ports()):
            sim.run()
