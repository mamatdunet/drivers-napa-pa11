# -*- coding: utf-8 -*-
"""Lecteur PA11 simulé (pour tester pa11.py sans le matériel).

Reproduit le protocole DCC tel que le pilote d'origine l'utilise, sur une
NAND SmartMedia simulée (pages 512+16, effacement par bloc, programmation
qui ne peut que passer des bits de 1 à 0, comme une vraie NAND).
"""
import pickle
import random
import struct

PAGE_RAW = 528


def _ecc(d, off):
    from pa11 import ecc256
    return ecc256(d, off)


def _addr(lb):
    a = lb % 1000
    w = 0x1000 | (a << 1)
    if bin(w).count("1") % 2:
        w |= 1
    return bytes((w >> 8, w & 0xFF))


class SimNand:
    def __init__(self, devid=0x76, nphys=4096, ppb=32, maker=0xEC):
        self.devid, self.nphys, self.ppb, self.maker = devid, nphys, ppb, maker
        self.mem = bytearray(b"\xff" * (nphys * ppb * PAGE_RAW))
        self.erase_count = [0] * nphys
        self.fail_erase = set()

    def page(self, p):
        return self.mem[p * PAGE_RAW:(p + 1) * PAGE_RAW]

    def program(self, p, raw):
        o = p * PAGE_RAW
        cur = int.from_bytes(self.mem[o:o + PAGE_RAW], "big")
        new = cur & int.from_bytes(raw, "big")   # NAND : les bits ne peuvent que passer à 0
        self.mem[o:o + PAGE_RAW] = new.to_bytes(PAGE_RAW, "big")

    def erase(self, blk):
        if blk in self.fail_erase:
            return False
        o = blk * self.ppb * PAGE_RAW
        self.mem[o:o + self.ppb * PAGE_RAW] = b"\xff" * (self.ppb * PAGE_RAW)
        self.erase_count[blk] += 1
        return True

    def write_block_raw(self, blk, spare_fn, data=None):
        for p in range(self.ppb):
            d = bytearray(b"\xff" * 512) if data is None else bytearray(data[p * 512:(p + 1) * 512])
            sp = spare_fn(p, d)
            self.program(blk * self.ppb + p, bytes(d) + bytes(sp))


class SimTransport:
    def __init__(self, chips):
        self.chips = chips
        self.pending = None
        self.out = b""
        self.manufacturer, self.product = "SigmaTel (simulé)", "PA11"
        self.log = []

    # --- persistance ---
    @classmethod
    def load(cls, path):
        with open(path, "rb") as f:
            return pickle.load(f)

    def save(self, path):
        self.pending, self.out = None, b""
        with open(path, "wb") as f:
            pickle.dump(self, f)

    def close(self):
        pass

    # --- USB ---
    def write(self, data, timeout=0):
        data = bytes(data)
        if self.pending is not None:
            chip, page, npages = self.pending
            self.pending = None
            assert len(data) == npages * PAGE_RAW, "taille de données inattendue"
            for i in range(npages):
                chip.program(page + i, data[i * PAGE_RAW:(i + 1) * PAGE_RAW])
            self.log.append(("write", page, npages))
            return
        cmd = int.from_bytes(data[0:3], "little")
        base = cmd & 0x3FFF
        flags = cmd & ~0x3FFF
        resp = bytearray(64)
        if len(data) == 3 and cmd == 3:
            resp[0:3] = (2).to_bytes(3, "little")
            resp[3:9] = (0).to_bytes(3, "little") + (0x0102).to_bytes(3, "little")
            resp[9:15] = (1).to_bytes(3, "little") + (0x0105).to_bytes(3, "little")
        elif len(data) == 3 and cmd == 0x0C:
            struct.pack_into("<I", resp, 0, 0x3410 << 8)
        elif len(data) == 3 and cmd == 0x0A:
            pass
        elif base == 0x106 and len(data) == 6:
            # attention : l'octet 3 (numéro de puce) a écrasé l'octet haut du mot
            c = data[3]
            if c < len(self.chips):
                resp[3], resp[4] = self.chips[c].maker, self.chips[c].devid
            else:
                resp[0] = 1
        elif base == 0x10C and len(data) == 24:
            assert flags == 0x800000
            addr = struct.unpack_from("<I", data, 3)[0]
            pr = int.from_bytes(data[9:12], "little")
            nb = int.from_bytes(data[12:15], "little")
            bsz = int.from_bytes(data[15:18], "little")
            cnt = int.from_bytes(data[18:21], "little")
            chip = self.chips[data[21]]
            assert pr == 528 and bsz == chip.ppb * 512 and cnt == (nb * 6 + 63) // 64
            first = addr // bsz
            out = bytearray()
            for b in range(first, first + nb):
                out += chip.page(b * chip.ppb)[516:522]
            out += bytes(cnt * 64 - len(out))
            self.out = bytes(out)
            return
        elif base == 0x103 and len(data) == 21:
            assert flags == 0x800000
            addr = struct.unpack_from("<I", data, 3)[0]
            pr = int.from_bytes(data[9:12], "little")
            n = int.from_bytes(data[12:15], "little")
            cnt = int.from_bytes(data[15:18], "little")
            chip = self.chips[data[18]]
            assert pr == 528 and addr % 512 == 0 and cnt == (n * 528 + 63) // 64
            page = addr // 512
            out = bytearray()
            for i in range(n):
                out += chip.page(page + i)
            self.out = bytes(out) + bytes(cnt * 64 - len(out))
            return
        elif base == 0x101 and len(data) == 21:
            assert flags == 0x400000
            addr = struct.unpack_from("<I", data, 3)[0]
            n = int.from_bytes(data[12:15], "little")
            chip = self.chips[data[18]]
            self.pending = (chip, addr // 512, n)
            return
        elif base == 0x104 and len(data) == 15:
            assert flags == 0x400000
            addr = struct.unpack_from("<I", data, 3)[0]
            ppb = struct.unpack_from("<H", data, 9)[0]
            chip = self.chips[data[12]]
            assert ppb == chip.ppb
            blk = addr // (ppb * 512)
            ok = chip.erase(blk)
            self.log.append(("erase", blk))
            if not ok:
                resp[0] = 1
        else:
            raise AssertionError("commande inconnue %s" % data.hex())
        self.out = bytes(resp)

    def read(self, size, timeout=0):
        r, self.out = self.out[:size], self.out[size:]
        return r


def build_player(image_bytes, devid=0x76, nphys=4096, ppb=32, seed=1, bad=(7, 1500)):
    """Crée une NAND « d'usine » : CIS, firmware SigmaTel, blocs défectueux, données FAT."""
    rnd = random.Random(seed)
    chip = SimNand(devid, nphys, ppb)
    # bloc 0 : défectueux d'usine ; bloc 1 : CIS
    chip.write_block_raw(0, lambda p, d: b"\xff" * 5 + b"\x00" + b"\xff" * 10)
    chip.write_block_raw(1, lambda p, d: b"\xff" * 16,
                         data=b"\x01\x03\xd9\x01\xff\x18" + b"\x00" * (ppb * 512 - 6))
    # 64 blocs firmware SigmaTel (1 Mo) marqués « défectueux » avec étiquette
    tags = [b"ST", b"MP"] + [b"M" + bytes([c]) for c in b"/0123456789"]
    fw = list(range(nphys - 64, nphys))   # firmware en fin de puce (dernière zone)
    for i, b in enumerate(fw):
        tag = tags[i % len(tags)]
        chip.write_block_raw(b, lambda p, d, tag=tag: b"\xff\xff\xff\xff\xff\x00" + tag + b"\xff" * 8,
                             data=rnd.randbytes(64) * (ppb * 8))
    for b in bad:
        chip.write_block_raw(b, lambda p, d: b"\xff" * 5 + b"\xf0" + b"\xff" * 10)
    # données : blocs logiques répartis aléatoirement dans leur zone
    bs = ppb * 512
    nlog = len(image_bytes) // bs
    used = set(fw) | set(bad) | {0, 1}
    for lb in range(nlog):
        blk = image_bytes[lb * bs:(lb + 1) * bs]
        if blk == b"\xff" * bs and rnd.random() < 0.5:
            continue       # bloc jamais écrit
        zone = lb // 1000
        while True:
            phys = rnd.randrange(zone * 1024, zone * 1024 + 1024)
            if phys not in used:
                break
        used.add(phys)

        def sp(p, d, lb=lb):
            s = bytearray(b"\xff" * 16)
            s[6:8] = _addr(lb)
            s[11:13] = _addr(lb)
            s[13:16] = _ecc(d, 0)
            s[8:11] = _ecc(d, 256)
            return s
        chip.write_block_raw(phys, sp, data=blk)
    return SimTransport([chip])
