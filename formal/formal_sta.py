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
from consts.consts import ModeBits


class Formal(Verification):
    def __init__(self):
        pass

    def valid(self, instr: Value) -> Value:
        return instr.matches("1--10111", "1-100111")

    def check(self, m: Module, instr: Value, data: FormalData):
        mode = instr[4:6]
        b = instr[6]
        input = Mux(b, data.pre_b, data.pre_a)

        m.d.comb += [
            Assert(data.post_a == data.pre_a),
            Assert(data.post_b == data.pre_b),
            Assert(data.post_x == data.pre_x),
            Assert(data.post_sp == data.pre_sp),
        ]

        with m.If(mode == ModeBits.DIRECT.value):
            m.d.comb += [
                Assert(data.post_pc == data.plus16(data.pre_pc, 2)),

                Assert(data.addresses_read == 1),
                Assert(data.read_addr[0] == data.plus16(data.pre_pc, 1)),

                Assert(data.addresses_written == 1),
                Assert(data.write_addr[0] == data.read_data[0]),

                Assert(data.write_data[0] == input),
            ]
        with m.Elif(mode == ModeBits.EXTENDED.value):
            m.d.comb += [
                Assert(data.post_pc == data.plus16(data.pre_pc, 3)),

                Assert(data.addresses_read == 2),
                Assert(data.read_addr[0] == data.plus16(data.pre_pc, 1)),
                Assert(data.read_addr[1] == data.plus16(data.pre_pc, 2)),

                Assert(data.addresses_written == 1),
                Assert(
                    data.write_addr[0] == Cat(data.read_data[1], data.read_data[0])),

                Assert(data.write_data[0] == input),
            ]

        with m.Elif(mode == ModeBits.INDEXED.value):
            m.d.comb += [
                Assert(data.post_pc == data.plus16(data.pre_pc, 2)),

                Assert(data.addresses_read == 1),
                Assert(data.read_addr[0] == data.plus16(data.pre_pc, 1)),

                Assert(data.addresses_written == 1),
                Assert(
                    data.write_addr[0] == (data.pre_x + data.read_data[0])[:16]),

                Assert(data.write_data[0] == input),
            ]

        self.assertFlags(m, data.post_ccs, data.pre_ccs,
                         Z=(input == 0), N=input[7], V=0)
