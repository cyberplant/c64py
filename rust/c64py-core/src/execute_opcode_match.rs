match opcode {
            0xa9 => {
            return lda_imm(cpu, mem);
            },
            0xa5 => {
            return lda_zp(cpu, mem);
            },
            0xb5 => {
            return lda_zpx(cpu, mem);
            },
            0xad => {
            return lda_abs(cpu, mem);
            },
            0xbd => {
            let base = read_word_at(mem, cpu.pc.wrapping_add(1));
            let addr = base.wrapping_add(cpu.x as u16);
            cpu.a = mr(mem, cpu, addr);
            update_nz(cpu, cpu.a);
            cpu.pc = (cpu.pc.wrapping_add(3)) & 0xFFFF;
            return if page_crossed(base, cpu.x) { 5 } else { 4 };
            },
            0xb9 => {
            return lda_absy(cpu, mem);
            },
            0xa1 => {
            return lda_indx(cpu, mem);
            },
            0xb1 => {
            return lda_indy(cpu, mem);
            },
            0xa2 => {
            return ldx_imm(cpu, mem);
            },
            0xa6 => {
            return ldx_zp(cpu, mem);
            },
            0xae => {
            return ldx_abs(cpu, mem);
            },
            0xb6 => {
            let zp_addr = mr(mem, cpu, cpu.pc.wrapping_add(1)).wrapping_add(cpu.y);
            cpu.x = mr(mem, cpu, u16::from(zp_addr));
            update_nz(cpu, cpu.x);
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 4;
            },
            0xbe => {
            let base = read_word_at(mem, cpu.pc.wrapping_add(1));
            let addr = base.wrapping_add(cpu.y as u16);
            cpu.x = mr(mem, cpu, addr);
            update_nz(cpu, cpu.x);
            cpu.pc = (cpu.pc.wrapping_add(3)) & 0xFFFF;
            return if page_crossed(base, cpu.y) { 5 } else { 4 };
            },
            0xa0 => {
            return ldy_imm(cpu, mem);
            },
            0xa4 => {
            return ldy_zp(cpu, mem);
            },
            0xac => {
            return ldy_abs(cpu, mem);
            },
            0xbc => {
            return ldy_absx(cpu, mem);
            },
            0xb4 => {
            return ldy_zpx(cpu, mem);
            },
            0x85 => {
            return sta_zp(cpu, mem);
            },
            0x95 => {
            return sta_zpx(cpu, mem);
            },
            0x8d => {
            return sta_abs(cpu, mem);
            },
            0x9d => {
            return sta_absx(cpu, mem);
            },
            0x99 => {
            return sta_absy(cpu, mem);
            },
            0x81 => {
            return sta_indx(cpu, mem);
            },
            0x91 => {
            return sta_indy(cpu, mem);
            },
            0x86 => {
            return stx_zp(cpu, mem);
            },
            0x8e => {
            return stx_abs(cpu, mem);
            },
            0x96 => {
            return stx_zpy(cpu, mem);
            },
            0x84 => {
            return sty_zp(cpu, mem);
            },
            0x8c => {
            return sty_abs(cpu, mem);
            },
            0x94 => {
            return sty_zpx(cpu, mem);
            },
            0x87 => {
            let zp_addr = mr(mem, cpu, cpu.pc.wrapping_add(1));
            mw(mem, cpu, zp_addr as u16, cpu.a & cpu.x);
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xa3 => {
            let zp_addr = mr(mem, cpu, cpu.pc.wrapping_add(1)).wrapping_add(cpu.x);
            let addr: u16 = u16::from(mr(mem, cpu, u16::from(zp_addr))) | (u16::from(mr(mem, cpu, zp_addr.wrapping_add(1) as u16)) << 8);
            cpu.a = mr(mem, cpu, addr);
            cpu.x = cpu.a;
            update_nz(cpu, cpu.a);
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 6;
            },
            0xc7 => {
            let zp_addr = mr(mem, cpu, cpu.pc.wrapping_add(1));
            let value = (mr(mem, cpu, u16::from(zp_addr)) - 1) & 0xFF;
            mw(mem, cpu, zp_addr as u16, value);
            let result = cpu.a - value;
            set_flag(cpu, 0x01, result >= 0);
            set_flag(cpu, 0x02, result == 0);
            set_flag(cpu, 0x80, (result & 0x80) != 0);
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 5;
            },
            0x69 => {
            return adc_imm(cpu, mem);
            },
            0x61 => {
            return adc_indx(cpu, mem);
            },
            0x65 => {
            return adc_zp(cpu, mem);
            },
            0x75 => {
            return adc_zpx(cpu, mem);
            },
            0x6d => {
            return adc_abs(cpu, mem);
            },
            0x79 => {
            return adc_absy(cpu, mem);
            },
            0x7d => {
            return adc_absx(cpu, mem);
            },
            0x71 => {
            return adc_indy(cpu, mem);
            },
            0xe9 => {
            return sbc_imm(cpu, mem);
            },
            0xe5 => {
            return sbc_zp(cpu, mem);
            },
            0xf5 => {
            let zp_addr = mr(mem, cpu, cpu.pc.wrapping_add(1)).wrapping_add(cpu.x);
            let value = mr(mem, cpu, u16::from(zp_addr));
            let carry: u8 = if (cpu.p & 0x01) != 0 { 1 } else { 0 };
            let result: i32 = i32::from(cpu.a) - i32::from(value) - (1 - i32::from(carry));
            set_flag(cpu, 0x01, result >= 0);
            set_flag(cpu, 0x40, ((i32::from(cpu.a) ^ i32::from(value)) & 0x80) != 0 && ((i32::from(cpu.a) ^ result) & 0x80) != 0);
            cpu.a = (result & 0xFF) as u8;
            update_nz(cpu, cpu.a);
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 4;
            },
            0xe1 => {
            let zp_addr = mr(mem, cpu, cpu.pc.wrapping_add(1)).wrapping_add(cpu.x);
            let addr_low = mr(mem, cpu, u16::from(zp_addr));
            let addr_high = mr(mem, cpu, zp_addr.wrapping_add(1) as u16);
            let addr: u16 = u16::from(addr_low) | (u16::from(addr_high) << 8);
            let value = mr(mem, cpu, addr);
            let carry: u8 = if (cpu.p & 0x01) != 0 { 1 } else { 0 };
            let result: i32 = i32::from(cpu.a) - i32::from(value) - (1 - i32::from(carry));
            set_flag(cpu, 0x01, result >= 0);
            set_flag(cpu, 0x40, ((i32::from(cpu.a) ^ i32::from(value)) & 0x80) != 0 && ((i32::from(cpu.a) ^ result) & 0x80) != 0);
            cpu.a = (result & 0xFF) as u8;
            update_nz(cpu, cpu.a);
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 6;
            },
            0xf1 => {
            let zp_ptr = mr(mem, cpu, cpu.pc.wrapping_add(1));
            let addr_low = mr(mem, cpu, u16::from(zp_ptr));
            let addr_high = mr(mem, cpu, zp_ptr.wrapping_add(1) as u16);
            let base: u16 = u16::from(addr_low) | (u16::from(addr_high) << 8);
            let addr = base.wrapping_add(cpu.y as u16);
            let value = mr(mem, cpu, addr);
            let carry: u8 = if (cpu.p & 0x01) != 0 { 1 } else { 0 };
            let result: i32 = i32::from(cpu.a) - i32::from(value) - (1 - i32::from(carry));
            set_flag(cpu, 0x01, result >= 0);
            set_flag(cpu, 0x40, ((i32::from(cpu.a) ^ i32::from(value)) & 0x80) != 0 && ((i32::from(cpu.a) ^ result) & 0x80) != 0);
            cpu.a = (result & 0xFF) as u8;
            update_nz(cpu, cpu.a);
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return if page_crossed(base, cpu.y) { 6 } else { 5 };
            },
            0xed => {
            return sbc_abs(cpu, mem);
            },
            0xfd => {
            let base = read_word_at(mem, cpu.pc.wrapping_add(1));
            let addr = base.wrapping_add(cpu.x as u16);
            let value = mr(mem, cpu, addr);
            let carry: u8 = if (cpu.p & 0x01) != 0 { 1 } else { 0 };
            let result: i32 = i32::from(cpu.a) - i32::from(value) - (1 - i32::from(carry));
            set_flag(cpu, 0x01, result >= 0);
            set_flag(cpu, 0x40, ((i32::from(cpu.a) ^ i32::from(value)) & 0x80) != 0 && ((i32::from(cpu.a) ^ result) & 0x80) != 0);
            cpu.a = (result & 0xFF) as u8;
            update_nz(cpu, cpu.a);
            cpu.pc = (cpu.pc.wrapping_add(3)) & 0xFFFF;
            return if page_crossed(base, cpu.x) { 5 } else { 4 };
            },
            0xf9 => {
            let base = read_word_at(mem, cpu.pc.wrapping_add(1));
            let addr = base.wrapping_add(cpu.y as u16);
            let value = mr(mem, cpu, addr);
            let carry: u8 = if (cpu.p & 0x01) != 0 { 1 } else { 0 };
            let result: i32 = i32::from(cpu.a) - i32::from(value) - (1 - i32::from(carry));
            set_flag(cpu, 0x01, result >= 0);
            set_flag(cpu, 0x40, ((i32::from(cpu.a) ^ i32::from(value)) & 0x80) != 0 && ((i32::from(cpu.a) ^ result) & 0x80) != 0);
            cpu.a = (result & 0xFF) as u8;
            update_nz(cpu, cpu.a);
            cpu.pc = (cpu.pc.wrapping_add(3)) & 0xFFFF;
            return if page_crossed(base, cpu.y) { 5 } else { 4 };
            },
            0x29 => {
            return and_imm(cpu, mem);
            },
            0x25 => {
            return and_zp(cpu, mem);
            },
            0x35 => {
            return and_zpx(cpu, mem);
            },
            0x2d => {
            return and_abs(cpu, mem);
            },
            0x3d => {
            return and_absx(cpu, mem);
            },
            0x39 => {
            return and_absy(cpu, mem);
            },
            0x21 => {
            return and_indx(cpu, mem);
            },
            0x31 => {
            return and_indy(cpu, mem);
            },
            0x09 => {
            return ora_imm(cpu, mem);
            },
            0x05 => {
            return ora_zp(cpu, mem);
            },
            0x15 => {
            return ora_zpx(cpu, mem);
            },
            0x0d => {
            return ora_abs(cpu, mem);
            },
            0x1d => {
            return ora_absx(cpu, mem);
            },
            0x19 => {
            return ora_absy(cpu, mem);
            },
            0x01 => {
            return ora_indx(cpu, mem);
            },
            0x11 => {
            return ora_indy(cpu, mem);
            },
            0x49 => {
            return eor_imm(cpu, mem);
            },
            0x45 => {
            return eor_zp(cpu, mem);
            },
            0x55 => {
            return eor_zpx(cpu, mem);
            },
            0x4d => {
            return eor_abs(cpu, mem);
            },
            0x5d => {
            return eor_absx(cpu, mem);
            },
            0x59 => {
            return eor_absy(cpu, mem);
            },
            0x41 => {
            return eor_indx(cpu, mem);
            },
            0x51 => {
            return eor_indy(cpu, mem);
            },
            0xc9 => {
            return cmp_imm(cpu, mem);
            },
            0xc5 => {
            return cmp_zp(cpu, mem);
            },
            0xcd => {
            return cmp_abs(cpu, mem);
            },
            0xdd => {
            let base = read_word_at(mem, cpu.pc.wrapping_add(1));
            let addr = base.wrapping_add(cpu.x as u16);
            let value = mr(mem, cpu, addr);
            let result = cpu.a.wrapping_sub(value);
            set_flag(cpu, 0x01, cpu.a >= value);
            update_nz(cpu, result);
            cpu.pc = (cpu.pc.wrapping_add(3)) & 0xFFFF;
            return if page_crossed(base, cpu.x) { 5 } else { 4 };
            },
            0xd9 => {
            let base = read_word_at(mem, cpu.pc.wrapping_add(1));
            let addr = base.wrapping_add(cpu.y as u16);
            let value = mr(mem, cpu, addr);
            let result = cpu.a.wrapping_sub(value);
            set_flag(cpu, 0x01, cpu.a >= value);
            update_nz(cpu, result);
            cpu.pc = (cpu.pc.wrapping_add(3)) & 0xFFFF;
            return if (base & 0xFF00) != (addr & 0xFF00) { 5 } else { 4 };
            },
            0xe0 => {
            return cpx_imm(cpu, mem);
            },
            0xe4 => {
            return cpx_zp(cpu, mem);
            },
            0xec => {
            return cpx_abs(cpu, mem);
            },
            0xc0 => {
            return cpy_imm(cpu, mem);
            },
            0xc4 => {
            return cpy_zp(cpu, mem);
            },
            0xcc => {
            return cpy_abs(cpu, mem);
            },
            0xc1 => {
            let zp_addr = mr(mem, cpu, cpu.pc.wrapping_add(1)).wrapping_add(cpu.x);
            let addr: u16 = u16::from(mr(mem, cpu, u16::from(zp_addr))) | (u16::from(mr(mem, cpu, zp_addr.wrapping_add(1) as u16)) << 8);
            let value = mr(mem, cpu, addr);
            let result = cpu.a.wrapping_sub(value);
            set_flag(cpu, 0x01, cpu.a >= value);
            update_nz(cpu, result);
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 6;
            },
            0xd1 => {
            let zp_addr = mr(mem, cpu, cpu.pc.wrapping_add(1));
            let base: u16 = u16::from(mr(mem, cpu, u16::from(zp_addr))) | (u16::from(mr(mem, cpu, zp_addr.wrapping_add(1) as u16)) << 8);
            let addr = base.wrapping_add(cpu.y as u16);
            let value = mr(mem, cpu, addr);
            let result = cpu.a.wrapping_sub(value);
            set_flag(cpu, 0x01, cpu.a >= value);
            update_nz(cpu, result);
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return if page_crossed(base, cpu.y) { 6 } else { 5 };
            },
            0xd5 => {
            let zp_addr = mr(mem, cpu, cpu.pc.wrapping_add(1)).wrapping_add(cpu.x);
            let value = mr(mem, cpu, u16::from(zp_addr));
            let result = cpu.a.wrapping_sub(value);
            set_flag(cpu, 0x01, cpu.a >= value);
            update_nz(cpu, result);
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 4;
            },
            0xd6 => {
            let zp_addr = mr(mem, cpu, cpu.pc.wrapping_add(1)).wrapping_add(cpu.x);
            let old = mr(mem, cpu, u16::from(zp_addr));
            rmw_dummy_6510(mem, cpu, u16::from(zp_addr), old);
            let value = old.wrapping_sub(1);
            mw(mem, cpu, zp_addr as u16, value);
            update_nz(cpu, value);
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 6;
            },
            0xe6 => {
            return inc_zp(cpu, mem);
            },
            0xf6 => {
            let zp_addr = mr(mem, cpu, cpu.pc.wrapping_add(1)).wrapping_add(cpu.x);
            let old = mr(mem, cpu, u16::from(zp_addr));
            rmw_dummy_6510(mem, cpu, u16::from(zp_addr), old);
            let value = old.wrapping_add(1);
            mw(mem, cpu, zp_addr as u16, value);
            update_nz(cpu, value);
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 6;
            },
            0xee => {
            return inc_abs(cpu, mem);
            },
            0xc6 => {
            return dec_zp(cpu, mem);
            },
            0xce => {
            return dec_abs(cpu, mem);
            },
            0xde => {
            return dec_absx(cpu, mem);
            },
            0xe8 => {
            return inx(cpu, mem);
            },
            0xc8 => {
            return iny(cpu, mem);
            },
            0xca => {
            return dex(cpu, mem);
            },
            0x88 => {
            return dey(cpu, mem);
            },
            0x0a => {
            return asl_acc(cpu, mem);
            },
            0x06 => {
            return asl_zp(cpu, mem);
            },
            0x16 => {
            return asl_zpx(cpu, mem);
            },
            0x0e => {
            return asl_abs(cpu, mem);
            },
            0x1e => {
            return asl_absx(cpu, mem);
            },
            0x4a => {
            return lsr_acc(cpu, mem);
            },
            0x46 => {
            return lsr_zp(cpu, mem);
            },
            0x56 => {
            return lsr_zpx(cpu, mem);
            },
            0x4e => {
            return lsr_abs(cpu, mem);
            },
            0x5e => {
            return lsr_absx(cpu, mem);
            },
            0x2a => {
            return rol_acc(cpu, mem);
            },
            0x26 => {
            return rol_zp(cpu, mem);
            },
            0x36 => {
            return rol_zpx(cpu, mem);
            },
            0x2e => {
            return rol_abs(cpu, mem);
            },
            0x3e => {
            return rol_absx(cpu, mem);
            },
            0x6a => {
            return ror_acc(cpu, mem);
            },
            0x66 => {
            return ror_zp(cpu, mem);
            },
            0x76 => {
            return ror_zpx(cpu, mem);
            },
            0x6e => {
            return ror_abs(cpu, mem);
            },
            0x7e => {
            return ror_absx(cpu, mem);
            },
            0xfe => {
            let base = read_word_at(mem, cpu.pc.wrapping_add(1));
            let addr = base.wrapping_add(cpu.x as u16);
            let old = mr(mem, cpu, addr);
            rmw_dummy_6510(mem, cpu, addr, old);
            let value = old.wrapping_add(1);
            mw(mem, cpu, addr, value);
            update_nz(cpu, value);
            cpu.pc = (cpu.pc.wrapping_add(3)) & 0xFFFF;
            return 7;
            },
            0x90 => {
            return bcc(cpu, mem);
            },
            0xb0 => {
            return bcs(cpu, mem);
            },
            0xf0 => {
            return beq(cpu, mem);
            },
            0xd0 => {
            return bne(cpu, mem);
            },
            0x10 => {
            return bpl(cpu, mem);
            },
            0x30 => {
            return bmi(cpu, mem);
            },
            0x50 => {
            return bvc(cpu, mem);
            },
            0x70 => {
            return bvs(cpu, mem);
            },
            0x4c => {
            return jmp_abs(cpu, mem);
            },
            0x6c => {
            return jmp_ind(cpu, mem);
            },
            0x20 => {
            return jsr_abs(cpu, mem);
            },
            0x60 => {
            return rts(cpu, mem);
            },
            0x40 => {
            return rti(cpu, mem);
            },
            0x48 => {
            return pha(cpu, mem);
            },
            0x68 => {
            return pla(cpu, mem);
            },
            0x08 => {
            return php(cpu, mem);
            },
            0x28 => {
            return plp(cpu, mem);
            },
            0x7a => {
            cpu.sp = (cpu.sp + 1) & 0xFF;
            cpu.y = mr(mem, cpu, 0x0100u16.wrapping_add(cpu.sp as u16));
            update_nz(cpu, cpu.y);
            cpu.pc = (cpu.pc.wrapping_add(1)) & 0xFFFF;
            return 4;
            },
            0x7f => {
            let base = read_word_at(mem, cpu.pc.wrapping_add(1));
            let addr = base.wrapping_add(cpu.x as u16);
            let value = mr(mem, cpu, addr);
            let carry: u8 = if (cpu.p & 0x01) != 0 { 1 } else { 0 };
            let new_carry = (value & 0x01) != 0;
            let value = ((value >> 1) | (carry << 7)) & 0xFF;
            mw(mem, cpu, addr, value);
            set_flag(cpu, 0x01, new_carry);
            let carry: u8 = if (cpu.p & 0x01) != 0 { 1 } else { 0 };
            let result = cpu.a + value + carry;
            set_flag(cpu, 0x01, result > 0xFF);
            cpu.a = (result & 0xFF) as u8;
            update_nz(cpu, cpu.a);
            cpu.pc = (cpu.pc.wrapping_add(3)) & 0xFFFF;
            return 7;
            },
            0xa7 => {
            let zp_addr = mr(mem, cpu, cpu.pc.wrapping_add(1));
            cpu.a = mr(mem, cpu, u16::from(zp_addr));
            cpu.x = cpu.a;
            update_nz(cpu, cpu.a);
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xaf => {
            let addr = read_word_at(mem, cpu.pc.wrapping_add(1));
            cpu.a = mr(mem, cpu, addr);
            cpu.x = cpu.a;
            update_nz(cpu, cpu.a);
            cpu.pc = (cpu.pc.wrapping_add(3)) & 0xFFFF;
            return 4;
            },
            0xbf => {
            let base = read_word_at(mem, cpu.pc.wrapping_add(1));
            let addr = base.wrapping_add(cpu.y as u16);
            cpu.a = mr(mem, cpu, addr);
            cpu.x = cpu.a;
            update_nz(cpu, cpu.a);
            cpu.pc = (cpu.pc.wrapping_add(3)) & 0xFFFF;
            return if page_crossed(base, cpu.y) { 5 } else { 4 };
            },
            0xff => {
            let base = read_word_at(mem, cpu.pc.wrapping_add(1));
            let addr = base.wrapping_add(cpu.x as u16);
            let value = mr(mem, cpu, addr).wrapping_add(1);
            mw(mem, cpu, addr, value);
            let carry: u8 = if (cpu.p & 0x01) != 0 { 1 } else { 0 };
            let result: i32 = i32::from(cpu.a) - i32::from(value) - (1 - i32::from(carry));
            set_flag(cpu, 0x01, result >= 0);
            set_flag(cpu, 0x40, ((i32::from(cpu.a) ^ i32::from(value)) & 0x80) != 0 && ((i32::from(cpu.a) ^ result) & 0x80) != 0);
            cpu.a = (result & 0xFF) as u8;
            update_nz(cpu, cpu.a);
            cpu.pc = (cpu.pc.wrapping_add(3)) & 0xFFFF;
            return 7;
            },
            0xaa => {
            return tax(cpu, mem);
            },
            0xa8 => {
            return tay(cpu, mem);
            },
            0x8a => {
            return txa(cpu, mem);
            },
            0x98 => {
            return tya(cpu, mem);
            },
            0xba => {
            return tsx(cpu, mem);
            },
            0x9a => {
            cpu.sp = cpu.x;
            cpu.pc = (cpu.pc.wrapping_add(1)) & 0xFFFF;
            return 2;
            },
            0x18 => {
            set_flag(cpu, 0x01, false);
            cpu.pc = (cpu.pc.wrapping_add(1)) & 0xFFFF;
            return 2;
            },
            0x38 => {
            set_flag(cpu, 0x01, true);
            cpu.pc = (cpu.pc.wrapping_add(1)) & 0xFFFF;
            return 2;
            },
            0x58 => {
            cpu.pre_i_flag = cpu.p & 0x04;
            set_flag(cpu, 0x04, false);
            cpu.cli_sei_delay = true;
            cpu.pc = (cpu.pc.wrapping_add(1)) & 0xFFFF;
            return 2;
            },
            0x78 => {
            cpu.pre_i_flag = cpu.p & 0x04;
            set_flag(cpu, 0x04, true);
            cpu.cli_sei_delay = true;
            cpu.pc = (cpu.pc.wrapping_add(1)) & 0xFFFF;
            return 2;
            },
            0xd8 => {
            set_flag(cpu, 0x08, false);
            cpu.pc = (cpu.pc.wrapping_add(1)) & 0xFFFF;
            return 2;
            },
            0xf8 => {
            set_flag(cpu, 0x08, true);
            cpu.pc = (cpu.pc.wrapping_add(1)) & 0xFFFF;
            return 2;
            },
            0xb8 => {
            set_flag(cpu, 0x40, false);
            cpu.pc = (cpu.pc.wrapping_add(1)) & 0xFFFF;
            return 2;
            },
            0x00 => {
            return brk(cpu, mem);
            },
            0x02 => {
            cpu.stopped = true;
            cpu.pc = (cpu.pc.wrapping_add(1)) & 0xFFFF;
            return 0;
            },
            0xea => {
            cpu.pc = (cpu.pc.wrapping_add(1)) & 0xFFFF;
            return 2;
            },
            0x80 => {
            mr(mem, cpu, cpu.pc.wrapping_add(1));
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 2;
            },
            0x82 => {
            mr(mem, cpu, cpu.pc.wrapping_add(1));
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 2;
            },
            0x89 => {
            mr(mem, cpu, cpu.pc.wrapping_add(1));
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 2;
            },
            0xc2 => {
            mr(mem, cpu, cpu.pc.wrapping_add(1));
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 2;
            },
            0xe2 => {
            mr(mem, cpu, cpu.pc.wrapping_add(1));
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 2;
            },
            0x04 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x44 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x64 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x14 => {
            cpu.pc = (cpu.pc.wrapping_add(3)) & 0xFFFF;
            return 4;
            },
            0x1c => {
            cpu.pc = (cpu.pc.wrapping_add(3)) & 0xFFFF;
            return 4;
            },
            0x3c => {
            cpu.pc = (cpu.pc.wrapping_add(3)) & 0xFFFF;
            return 4;
            },
            0x5c => {
            cpu.pc = (cpu.pc.wrapping_add(3)) & 0xFFFF;
            return 4;
            },
            0x7c => {
            cpu.pc = (cpu.pc.wrapping_add(3)) & 0xFFFF;
            return 4;
            },
            0xdc => {
            cpu.pc = (cpu.pc.wrapping_add(3)) & 0xFFFF;
            return 4;
            },
            0xfc => {
            cpu.pc = (cpu.pc.wrapping_add(3)) & 0xFFFF;
            return 4;
            },
            0x24 => {
            return bit_zp(cpu, mem);
            },
            0x2c => {
            return bit_abs(cpu, mem);
            },
            0x03 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x07 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x0b => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x0f => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x12 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x13 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x17 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x1a => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x1b => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x1f => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x22 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x27 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x2f => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x32 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x33 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x34 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x37 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x3a => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x3b => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x3f => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x42 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x43 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x47 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x4b => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x4f => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x52 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x53 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x54 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x57 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x5a => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x5b => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x5f => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x62 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x63 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x67 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x6b => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x6f => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x72 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x73 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x74 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x77 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x7b => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x83 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x8b => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x8f => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x92 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x93 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x97 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x9b => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x9c => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x9e => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0x9f => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xab => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xb2 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xb3 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xb7 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xbb => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xc3 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xcb => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xcf => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xd2 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xd3 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xd4 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xd7 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xda => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xdb => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xdf => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xe3 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xe7 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xeb => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xef => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xf2 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xf3 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xf4 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xf7 => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xfa => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            0xfb => {
            cpu.pc = (cpu.pc.wrapping_add(2)) & 0xFFFF;
            return 3;
            },
            _ => {
            cpu.stopped = true;
            return 0;
            },
}
