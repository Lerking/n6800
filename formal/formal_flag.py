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

from nmigen import Signal, Value, Cat, Module, Mux
from nmigen.hdl.ast import Statement
from nmigen.asserts import Assert
from .verification import FormalData, Verification
from consts.consts import Flags

CLV = "00001010"
SEV = "00001011"
CLC = "00001100"
SEC = "00001101"
CLI = "00001110"
SEI = "00001111"


class Formal(Verification):
    def __init__(self):
        pass

    def valid(self, instr: Value) -> Value:
        return instr.matches(CLV, SEV, CLC, SEC, CLI, SEI)

    def check(self, m: Module, instr: Value, data: FormalData):
        m.d.comb += [
            Assert(data.post_a == data.pre_a),
            Assert(data.post_b == data.pre_b),
            Assert(data.post_x == data.pre_x),
            Assert(data.post_sp == data.pre_sp),
            Assert(data.addresses_written == 0),
            Assert(data.addresses_read == 0),
        ]
        m.d.comb += Assert(data.post_pc == data.plus16(data.pre_pc, 1))

        c = Signal()
        v = Signal()
        i = Signal()

        m.d.comb += c.eq(data.pre_ccs[Flags.C])
        m.d.comb += v.eq(data.pre_ccs[Flags.V])
        m.d.comb += i.eq(data.pre_ccs[Flags.I])

        with m.Switch(instr):
            with m.Case(CLV):
                m.d.comb += v.eq(0)
            with m.Case(SEV):
                m.d.comb += v.eq(1)
            with m.Case(CLC):
                m.d.comb += c.eq(0)
            with m.Case(SEC):
                m.d.comb += c.eq(1)
            with m.Case(CLI):
                m.d.comb += i.eq(0)
            with m.Case(SEI):
                m.d.comb += i.eq(1)

        self.assertFlags(m, data.post_ccs, data.pre_ccs, V=v, C=c, I=i)
