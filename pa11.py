#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pa11.py — accès au lecteur MP3 Napa / A-Max PA11 (SigmaTel STMP3400, USB 066F:004B)
depuis un Mac moderne (ou Linux), sans pilote noyau.

Le PA11 n'est pas une clé USB « mass storage » : son firmware USB (DCC) donne
seulement un accès brut à la mémoire flash NAND (format SmartMedia, pages de
512 + 16 octets). C'est l'ordinateur qui reconstruit la table des blocs et
vérifie les codes correcteurs (ECC). Ce script réimplémente, en espace
utilisateur via libusb, ce que faisait le pilote Mac OS 9 « 066F004BUsb » de
2002 (protocole retrouvé par désassemblage de ce pilote).

Principe d'utilisation :
    python3 pa11.py info            # diagnostic, lecture seule
    python3 pa11.py backup          # sauvegarde brute complète (lecture seule)
    python3 pa11.py open            # copie le contenu dans une image disque et la monte
      ... copier / supprimer des MP3 dans le Finder ...
    python3 pa11.py close           # démonte et réécrit sur le lecteur ce qui a changé

Licence : faites-en ce que vous voulez. Aucune garantie.
"""

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import struct
import subprocess
import sys
import time

VERSION = "0.1"

VID, PID = 0x066F, 0x004B
RECOVERY_PIDS = (0x3400, 0x3410)

PAGE_DATA = 512
PAGE_SPARE = 16
PAGE_RAW = PAGE_DATA + PAGE_SPARE      # 528
MAX_XFER = 0x4200                      # limite du pilote d'origine (32 pages)

# Géométries reconnues par le pilote d'origine (table SM_SIZE).
#   id NAND -> (nom, pages par bloc, blocs physiques, blocs logiques)
# Une « zone » = 1024 blocs physiques / 1000 blocs logiques.
GEOMETRIES = {
    0xE6: ("8 Mo", 16, 1024, 1000),
    0x73: ("16 Mo", 32, 1024, 1000),
    0x75: ("32 Mo", 32, 2048, 2000),
    0x76: ("64 Mo", 32, 4096, 4000),
    0x79: ("128 Mo", 32, 8192, 8000),
}
MAKERS = {0xEC: "Samsung", 0x98: "Toshiba"}

# Le PA11 réserve 1024 Ko de la puce 0 à son propre firmware (fichier Prefs
# d'origine : TotalKiloBytesReserved 1024). Ces blocs n'apparaissent pas dans
# l'espace logique ; on les retire de la capacité comme le pilote d'origine.
RESERVED_KB = 1024

# Statuts de bloc physique (même codage que le pilote d'origine)
BLK_UNUSABLE = 0x00   # défectueux, avant le CIS, ou firmware SigmaTel
BLK_USED = 0x01
BLK_CIS = 0x02
BLK_FREE = 0xFF


class PA11Error(Exception):
    pass


def log(*a):
    print(*a, flush=True)


DEBUG = False


def dbg(*a):
    if DEBUG:
        print("   [debug]", *a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# ECC SmartMedia (3 octets pour 256 octets de données).
# Vérifié bit à bit contre la routine SmartmediaCalculateParity du pilote
# d'origine, exécutée dans un émulateur PowerPC.
# --------------------------------------------------------------------------

def _build_ecc_tables():
    colp = []
    for b in range(256):
        bit = [(b >> i) & 1 for i in range(8)]
        cp0 = bit[0] ^ bit[2] ^ bit[4] ^ bit[6]
        cp1 = bit[1] ^ bit[3] ^ bit[5] ^ bit[7]
        cp2 = bit[0] ^ bit[1] ^ bit[4] ^ bit[5]
        cp3 = bit[2] ^ bit[3] ^ bit[6] ^ bit[7]
        cp4 = bit[0] ^ bit[1] ^ bit[2] ^ bit[3]
        cp5 = bit[4] ^ bit[5] ^ bit[6] ^ bit[7]
        par = sum(bit) & 1
        colp.append((cp0 | cp1 << 1 | cp2 << 2 | cp3 << 3 | cp4 << 4 | cp5 << 5, par))
    return colp


_COLP = _build_ecc_tables()


_PAR = bytes(_COLP[b][1] for b in range(256))          # parité de chaque octet
# masques : positions (octet de poids fort = position 0) dont le bit k d'adresse vaut 1
_LPMASK = [int.from_bytes(bytes(1 if (i >> k) & 1 else 0 for i in range(256)), "big")
           for k in range(8)]


def _popcount(v):
    return bin(v).count("1")


def ecc256(data, off=0):
    """Retourne les 3 octets d'ECC (ordre du pilote : LP7..0, LP15..8, CP)."""
    chunk = bytes(data[off:off + 256])
    v = int.from_bytes(chunk, "big")
    # XOR de tous les octets (repli successif) -> parités de colonnes
    w = 2048
    while w > 8:
        w //= 2
        v = (v >> w) ^ (v & ((1 << w) - 1))
    x = v & 0xFF
    cp, tot = _COLP[x]
    p = int.from_bytes(chunk.translate(_PAR), "big")
    e0 = e1 = 0
    for k in range(8):
        odd = _popcount(p & _LPMASK[k]) & 1      # LP(2k+1)
        even = odd ^ tot                          # LP(2k)
        val = even | (odd << 1)
        if k < 4:
            e0 |= val << (2 * k)
        else:
            e1 |= val << (2 * (k - 4))
    e0 = (~e0) & 0xFF
    e1 = (~e1) & 0xFF
    e2 = ((~(cp << 2)) & 0xFF) | 0x03
    return bytes((e0, e1, e2))


def ecc_correct(data, off, stored):
    """Vérifie/corrige 256 octets. Retourne 'ok', 'corrected' ou 'bad'."""
    calc = ecc256(data, off)
    if calc == bytes(stored):
        return "ok"
    s = int.from_bytes(bytes(stored), "little")
    c = int.from_bytes(calc, "little")
    x = s ^ c
    n = bin(x).count("1")
    if n == 1:
        return "corrected"          # l'erreur est dans l'ECC lui-même
    if n != 11:
        return "bad"
    # 11 bits différents : une seule erreur dans les données, localisable
    e0, e1, e2 = x & 0xFF, (x >> 8) & 0xFF, (x >> 16) & 0xFF
    # bit "impair" de chaque paire = 1 bit d'adresse
    addr = 0
    for k in range(4):
        if (e0 >> (2 * k + 1)) & 1:
            addr |= 1 << k
        if (e1 >> (2 * k + 1)) & 1:
            addr |= 1 << (k + 4)
    bitn = ((e2 >> 3) & 1) | ((e2 >> 5) & 1) << 1 | ((e2 >> 7) & 1) << 2
    data[off + addr] ^= 1 << bitn
    return "corrected" if ecc256(data, off) == bytes(stored) else "bad"


# --------------------------------------------------------------------------
# Adresse de bloc SmartMedia (octets 6-7 et 11-12 du spare, gros-boutiste)
# --------------------------------------------------------------------------

def encode_block_address(logical_in_zone):
    a = logical_in_zone % 1000
    w = 0x1000 | (a << 1)
    if bin(w).count("1") & 1:
        w |= 1
    return bytes(((w >> 8) & 0xFF, w & 0xFF))


def decode_block_address(b2):
    w = (b2[0] << 8) | b2[1]
    return (w >> 1) & 0x3FF


def zero_bits(b):
    return 8 - bin(b & 0xFF).count("1")


def status_ok_block(b):     # SmartmediaVerifyBlock : < 2 bits à zéro
    return zero_bits(b) < 2


def status_ok_data(b):      # SmartmediaVerifyPage : < 4 bits à zéro
    return zero_bits(b) < 4


def is_stmp_block(six):
    """SmartmediaVerifyStmpBlock : statut de bloc 0x00 + étiquette ST/MP/M0..M9."""
    if six[1] != 0:
        return False
    tag = bytes(six[2:4])
    return tag in (b"ST", b"MP", b"M/")


# --------------------------------------------------------------------------
# Transport USB
# --------------------------------------------------------------------------

class UsbTransport:
    def __init__(self):
        try:
            import usb.core
            import usb.util
        except ImportError:
            raise PA11Error(
                "Le module Python 'pyusb' est absent. Lancez d'abord install.command "
                "(ou : pip3 install pyusb libusb-package).")
        self.usb = usb
        backend = None
        try:
            import libusb_package
            backend = libusb_package.get_libusb1_backend()
        except Exception:
            backend = None
        dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
        if dev is None:
            for p in RECOVERY_PIDS:
                if usb.core.find(idVendor=VID, idProduct=p, backend=backend) is not None:
                    raise PA11Error(
                        "Le lecteur est branché mais en mode « récupération » (066F:%04X) : "
                        "son firmware semble absent ou endommagé. Ce mode n'est pas pris en "
                        "charge par cet outil." % p)
            raise PA11Error(
                "Lecteur PA11 introuvable (USB 066F:004B). Vérifiez qu'il est allumé, "
                "branché (adaptateur USB-C/USB-A si besoin) et qu'il affiche le mode USB.")
        self.dev = dev
        try:
            dev.set_configuration()
        except usb.core.USBError as e:
            dbg("set_configuration:", e)
        cfg = dev.get_active_configuration()
        intf = None
        for i in cfg:
            if i.bInterfaceClass == 0xFF or i.bInterfaceProtocol == 0xFF:
                intf = i
                break
        if intf is None:
            intf = cfg[(0, 0)]
        self.intf = intf
        try:
            if dev.is_kernel_driver_active(intf.bInterfaceNumber):
                dev.detach_kernel_driver(intf.bInterfaceNumber)
        except Exception:
            pass
        usb.util.claim_interface(dev, intf.bInterfaceNumber)
        ep_out = ep_in = None
        for ep in intf:
            if usb.util.endpoint_type(ep.bmAttributes) != usb.util.ENDPOINT_TYPE_BULK:
                continue
            if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN:
                ep_in = ep_in or ep
            else:
                ep_out = ep_out or ep
        if ep_in is None or ep_out is None:
            raise PA11Error("Points d'accès USB bulk introuvables sur l'interface %d."
                            % intf.bInterfaceNumber)
        self.ep_in, self.ep_out = ep_in, ep_out
        dbg("interface %d, bulk out 0x%02x, bulk in 0x%02x" % (
            intf.bInterfaceNumber, ep_out.bEndpointAddress, ep_in.bEndpointAddress))
        try:
            self.product = usb.util.get_string(dev, dev.iProduct) if dev.iProduct else ""
            self.manufacturer = (usb.util.get_string(dev, dev.iManufacturer)
                                 if dev.iManufacturer else "")
        except Exception:
            self.product = self.manufacturer = ""

    def write(self, data, timeout=5000):
        dbg("OUT %4d: %s" % (len(data), bytes(data[:32]).hex()))
        n = self.ep_out.write(bytes(data), timeout=timeout)
        if n != len(data):
            raise PA11Error("Écriture USB incomplète (%d/%d)" % (n, len(data)))

    def read(self, size, timeout=5000):
        buf = bytes(self.ep_in.read(size, timeout=timeout))
        dbg("IN  %4d: %s" % (len(buf), buf[:32].hex()))
        return buf

    def close(self):
        try:
            self.usb.util.release_interface(self.dev, self.intf.bInterfaceNumber)
            self.usb.util.dispose_resources(self.dev)
        except Exception:
            pass


# --------------------------------------------------------------------------
# Protocole DCC (commandes reconstituées depuis le pilote Mac OS 9)
# Tous les champs sont petit-boutistes ; les écritures 32 bits se
# chevauchent volontairement exactement comme dans le pilote d'origine.
# --------------------------------------------------------------------------

def _stw(buf, off, val):
    b = (val & 0xFFFFFFFF).to_bytes(4, "little")
    for i in range(4):
        if off + i < len(buf):
            buf[off + i] = b[i]


def _ceil64(n):
    return (n + 63) // 64


class DCC:
    def __init__(self, t):
        self.t = t
        self.dcc_version = None
        self.sm_version = None
        self.chip_id = None
        self.last_power_raw = b""

    def _status(self, cmd):
        self.t.write(cmd)
        r = self.t.read(64)
        if len(r) < 4:
            raise PA11Error("Réponse trop courte du lecteur (%d octets)" % len(r))
        return r

    @property
    def big(self):
        return (self.sm_version or 0) > 0x100

    def get_info(self):
        r = self._status(bytes((3, 0, 0)))
        n = int.from_bytes(r[0:3], "little")
        info = {}
        for k in range(min(n, 9)):
            o = 3 + 6 * k
            typ = int.from_bytes(r[o:o + 3], "little")
            val = int.from_bytes(r[o + 3:o + 6], "little")
            info[typ] = val
        self.dcc_version = info.get(0)
        self.sm_version = info.get(1)
        if (self.dcc_version or 0) >= 0x101:
            r2 = self._status(bytes((0x0C, 0, 0)))
            self.chip_id = (int.from_bytes(r2[0:4], "little") >> 8) & 0xFFFF
        return info

    def get_power(self):
        r = self._status(bytes((0x0A, 0, 0)))
        self.last_power_raw = bytes(r[:8])
        dbg("alimentation : %s" % self.last_power_raw.hex(" "))
        # le pilote lit ce mot SANS conversion d'octets : il teste les octets 1 à 3
        return r[1] == 0 and r[2] == 0 and r[3] == 0

    def detect_media(self, chip):
        cmd = bytearray(6)
        _stw(cmd, 0, 0x106)
        cmd[3] = chip
        r = self._status(cmd)
        st = int.from_bytes(r[0:4], "little") & 0xFFFFFF
        return st == 0, r[3], r[4]

    def read_map(self, chip, first_block, nblocks, ppb):
        """6 octets (spare[4:10] de la 1re page) pour chaque bloc."""
        cmd = bytearray(0x18)
        _stw(cmd, 0, (0x80010C if self.big else 0x10C))
        _stw(cmd, 3, first_block * ppb * PAGE_DATA)
        _stw(cmd, 9, PAGE_RAW)
        _stw(cmd, 0x0C, nblocks)
        _stw(cmd, 0x0F, PAGE_DATA * ppb)
        cnt = _ceil64(nblocks * 6)
        _stw(cmd, 0x12, cnt)
        cmd[0x15] = chip
        self.t.write(cmd)
        r = self.t.read(cnt * 64)
        if len(r) < nblocks * 6:
            raise PA11Error("Lecture de la table des blocs incomplète")
        return r

    def read_pages(self, chip, page, npages):
        nbytes = npages * PAGE_RAW
        if nbytes > MAX_XFER:
            raise ValueError("trop de pages")
        cmd = bytearray(0x15)
        _stw(cmd, 0, (0x800103 if self.big else 0x103))
        _stw(cmd, 3, page * PAGE_DATA)
        _stw(cmd, 9, PAGE_RAW)
        _stw(cmd, 0x0C, npages)
        cnt = _ceil64(nbytes)
        _stw(cmd, 0x0F, cnt)
        cmd[0x12] = chip
        self.t.write(cmd)
        r = self.t.read(cnt * 64, timeout=10000)
        if len(r) < nbytes:
            raise PA11Error("Lecture de pages incomplète (%d/%d octets)" % (len(r), nbytes))
        return bytearray(r[:nbytes])

    def write_pages(self, chip, page, raw):
        npages = len(raw) // PAGE_RAW
        assert npages * PAGE_RAW == len(raw) and len(raw) <= MAX_XFER
        cmd = bytearray(0x15)
        _stw(cmd, 0, (0x400101 if self.big else 0x101))
        _stw(cmd, 3, page * PAGE_DATA)
        _stw(cmd, 9, PAGE_RAW)
        _stw(cmd, 0x0C, npages)
        _stw(cmd, 0x0F, _ceil64(len(raw)))
        cmd[0x12] = chip
        self.t.write(cmd)
        self.t.write(raw, timeout=10000)

    def erase_block(self, chip, block, ppb):
        cmd = bytearray(0x0F)
        _stw(cmd, 0, (0x400104 if self.big else 0x104))
        _stw(cmd, 3, block * ppb * PAGE_DATA)
        cmd[9:11] = struct.pack("<H", ppb)
        cmd[0x0C] = chip
        r = self._status(cmd)
        return (int.from_bytes(r[0:4], "little") & 0xFFFFFF) == 0


# --------------------------------------------------------------------------
# Couche de traduction (FTL) SmartMedia, identique au pilote d'origine
# --------------------------------------------------------------------------

class Chip:
    def __init__(self, dcc, index, maker, devid):
        if devid not in GEOMETRIES:
            raise PA11Error("Puce NAND inconnue (id 0x%02X) sur la puce %d" % (devid, index))
        self.dcc = dcc
        self.index = index
        self.maker = maker
        self.devid = devid
        self.name, self.ppb, self.nphys, self.nlog = GEOMETRIES[devid]
        self.zones = self.nphys // 1024
        self.block_bytes = self.ppb * PAGE_DATA
        self.reserved_blocks = 0
        self.capacity_blocks = self.nlog     # recalculé après scan()
        self.status = None
        self.l2p = None
        self.stmp_blocks = 0
        self.cis_block = None

    def scan(self, progress=None):
        """Reconstitue la table logique -> physique (SmartmediaMapBlocks)."""
        st = bytearray(self.nphys)
        l2p = [None] * self.nlog
        dup = 0
        cis_found = False
        stmp = 0
        for first in range(0, self.nphys, 256):
            n = min(256, self.nphys - first)
            r = self.dcc.read_map(self.index, first, n, self.ppb)
            for i in range(n):
                phys = first + i
                six = r[6 * i:6 * i + 6]
                # six = spare[4..9] : statut données, statut bloc, adresse (2), ECC...
                if not cis_found:
                    if six[1] == 0xFF and status_ok_data(six[0]):
                        cis_found = True
                        self.cis_block = phys
                        st[phys] = BLK_CIS
                    else:
                        st[phys] = BLK_UNUSABLE
                    continue
                if status_ok_block(six[1]):
                    if six[2] == 0xFF and six[3] == 0xFF:
                        st[phys] = BLK_FREE
                    else:
                        zone = phys >> 10
                        a = decode_block_address(six[2:4])
                        st[phys] = BLK_USED
                        if a < 1000:
                            lb = zone * 1000 + a
                            if lb < self.nlog:
                                if l2p[lb] is not None:
                                    dup += 1
                                l2p[lb] = phys
                else:
                    st[phys] = BLK_UNUSABLE
                    if is_stmp_block(six):
                        stmp += 1
            if progress:
                progress(min(first + n, self.nphys), self.nphys)
        if not cis_found:
            raise PA11Error("Bloc CIS introuvable : la mémoire ne semble pas formatée "
                            "au format SmartMedia attendu.")
        self.status, self.l2p, self.stmp_blocks, self.duplicates = st, l2p, stmp, dup
        # Comme le pilote : la réserve n'est retirée que si la puce contient le firmware.
        self.reserved_blocks = (-(-(RESERVED_KB * 1024) // self.block_bytes)
                                if stmp and RESERVED_KB else 0)
        self.capacity_blocks = self.nlog - self.reserved_blocks
        return self

    def counts(self):
        c = {"used": 0, "free": 0, "unusable": 0}
        for s in self.status:
            if s == BLK_USED:
                c["used"] += 1
            elif s == BLK_FREE:
                c["free"] += 1
            elif s == BLK_UNUSABLE:
                c["unusable"] += 1
        c["mapped"] = sum(1 for p in self.l2p if p is not None)
        return c

    # -- lecture -----------------------------------------------------------
    def read_physical_block(self, phys):
        """Retourne (données, liste des spares) d'un bloc physique, ECC appliqué."""
        raw = bytearray()
        per = MAX_XFER // PAGE_RAW
        for p0 in range(0, self.ppb, per):
            n = min(per, self.ppb - p0)
            raw += self.dcc.read_pages(self.index, phys * self.ppb + p0, n)
        data = bytearray(self.block_bytes)
        spares = []
        stats = {"corrected": 0, "bad": 0}
        for p in range(self.ppb):
            pg = raw[p * PAGE_RAW:(p + 1) * PAGE_RAW]
            d = bytearray(pg[:PAGE_DATA])
            sp = bytes(pg[PAGE_DATA:])
            spares.append(sp)
            if not (status_ok_block(sp[5]) and status_ok_data(sp[4])):
                stats["bad"] += 1
            elif d == b"\xff" * PAGE_DATA and sp[8:11] == b"\xff\xff\xff":
                pass   # page effacée
            else:
                for half, eoff in ((0, 13), (256, 8)):
                    res = ecc_correct(d, half, sp[eoff:eoff + 3])
                    if res != "ok":
                        stats[res] += 1
            data[p * PAGE_DATA:(p + 1) * PAGE_DATA] = d
        return data, spares, stats, raw

    def read_logical_block(self, lb):
        phys = self.l2p[lb]
        if phys is None:
            return bytearray(b"\xff" * self.block_bytes), {"corrected": 0, "bad": 0}
        data, _sp, stats, _raw = self.read_physical_block(phys)
        return data, stats

    # -- écriture ----------------------------------------------------------
    def build_raw_block(self, lb, data):
        addr = encode_block_address(lb % 1000)
        raw = bytearray()
        for p in range(self.ppb):
            d = data[p * PAGE_DATA:(p + 1) * PAGE_DATA]
            sp = bytearray(b"\xff" * PAGE_SPARE)
            sp[6:8] = addr
            sp[11:13] = addr
            sp[13:16] = ecc256(d, 0)
            sp[8:11] = ecc256(d, 256)
            raw += d + sp
        return raw

    def pick_free(self, lb):
        zone = lb // 1000
        lo, hi = zone * 1024, min(zone * 1024 + 1024, self.nphys)
        for phys in range(lo, hi):
            if self.status[phys] == BLK_FREE:
                return phys
        raise PA11Error("Plus aucun bloc libre dans la zone %d" % zone)

    def _erase(self, phys):
        # Garde-fou : on n'efface JAMAIS le CIS, le firmware SigmaTel ni un bloc défectueux.
        if self.status[phys] not in (BLK_USED, BLK_FREE):
            raise PA11Error("Refus d'effacer le bloc physique %d (bloc protégé)." % phys)
        return self.dcc.erase_block(self.index, phys, self.ppb)

    def _program_block(self, phys, raw):
        per = MAX_XFER // PAGE_RAW
        for p0 in range(0, self.ppb, per):
            n = min(per, self.ppb - p0)
            self.dcc.write_pages(self.index, phys * self.ppb + p0,
                                 raw[p0 * PAGE_RAW:(p0 + n) * PAGE_RAW])

    def _verify_block(self, phys, lb, data):
        back, sp, stats, _ = self.read_physical_block(phys)
        return (back == data and not stats["bad"]
                and bytes(sp[0][6:8]) == encode_block_address(lb % 1000))

    def mark_bad(self, phys):
        """Marque un bloc défectueux (octet « statut de bloc » à 0x00, norme SmartMedia).
        Une NAND permet de faire passer des bits à 0 sans effacement."""
        page = bytearray(b"\xff" * PAGE_RAW)
        page[PAGE_DATA + 5] = 0x00
        self.status[phys] = BLK_UNUSABLE
        self.dcc.write_pages(self.index, phys * self.ppb, page)
        sp = self.dcc.read_pages(self.index, phys * self.ppb, 1)[PAGE_DATA:]
        if status_ok_block(sp[5]):
            raise PA11Error(
                "Le bloc physique %d n'a pas pu être marqué défectueux : arrêt par sécurité. "
                "Vos données sont dans la sauvegarde ; ne réutilisez pas le lecteur avant "
                "d'avoir signalé ce message." % phys)

    def write_logical_block(self, lb, data, expected_hash=None):
        phys = self.l2p[lb]
        if phys is not None and expected_hash is not None:
            cur, _ = self.read_logical_block(lb)
            if hashlib.sha1(cur).hexdigest() != expected_hash:
                raise PA11Error(
                    "Le bloc logique %d a changé sur le lecteur depuis la copie (le lecteur "
                    "a-t-il été utilisé entre-temps ?). Abandon par sécurité." % lb)
        raw = self.build_raw_block(lb, data)
        if phys is None:
            phys = self.pick_free(lb)
        # Comme le pilote d'origine : effacement puis réécriture sur place.
        if self._erase(phys):
            self._program_block(phys, raw)
            if self._verify_block(phys, lb, data):
                self.l2p[lb] = phys
                self.status[phys] = BLK_USED
                return
        # Bloc usé : on déplace les données vers un bloc libre, puis on marque
        # l'ancien comme défectueux (dans cet ordre, pour ne jamais perdre le bloc).
        log("\n   bloc physique %d défaillant : déplacement du bloc logique %d" % (phys, lb))
        failed = phys
        self.status[failed] = BLK_UNUSABLE
        for _attempt in range(4):
            new = self.pick_free(lb)
            if not self._erase(new):
                self.mark_bad(new)
                continue
            self._program_block(new, raw)
            if not self._verify_block(new, lb, data):
                self.mark_bad(new)
                continue
            self.l2p[lb] = new
            self.status[new] = BLK_USED
            self.mark_bad(failed)
            return
        raise PA11Error("Impossible d'écrire le bloc logique %d (trop de blocs défaillants)." % lb)


# --------------------------------------------------------------------------
# Session : ouverture du lecteur
# --------------------------------------------------------------------------

class Device:
    def __init__(self, transport=None):
        self.t = transport or UsbTransport()
        self.dcc = DCC(self.t)
        self.info = self.dcc.get_info()
        self.power_ok = self.dcc.get_power()
        self.chips = []
        for c in range(4):
            try:
                present, maker, devid = self.dcc.detect_media(c)
            except Exception as e:
                if c == 0:
                    raise
                # pas de réponse propre : vider le tuyau USB pour éviter un décalage
                log("(note : pas de réponse pour la mémoire %d, ignorée)" % c)
                dbg("detect chip %d: %s" % (c, e))
                for _ in range(4):
                    try:
                        self.t.read(MAX_XFER, timeout=300)
                    except Exception:
                        break
                break
            dbg("chip %d present=%s maker=%02x id=%02x" % (c, present, maker, devid))
            if not present or devid not in GEOMETRIES:
                if c == 0:
                    raise PA11Error(
                        "Aucune mémoire détectée (statut=%s, fabricant 0x%02X, id 0x%02X)."
                        % (present, maker, devid))
                break
            self.chips.append(Chip(self.dcc, c, maker, devid))

    def close(self):
        self.t.close()


def progress_bar(label):
    start = time.time()
    last = [0.0]

    def cb(done, total):
        now = time.time()
        if now - last[0] < 0.25 and done < total:
            return
        last[0] = now
        pct = 100.0 * done / total if total else 100
        el = now - start
        eta = (el / done * (total - done)) if done else 0
        sys.stdout.write("\r   %s %5.1f %%  (reste ~%ds)   " % (label, pct, eta))
        if done >= total:
            sys.stdout.write("\n")
        sys.stdout.flush()
    return cb


# --------------------------------------------------------------------------
# Analyse FAT (pour affichage et contrôle de taille)
# --------------------------------------------------------------------------

def describe_volume(sector0, read_sector):
    out = {}
    if sector0[510:512] != b"\x55\xaa":
        out["error"] = "pas de signature 55AA en secteur 0"
        return out
    start = 0
    bpb = sector0
    if not (sector0[0] in (0xEB, 0xE9) and sector0[11:13] in (b"\x00\x02",)):
        # MBR : prendre la 1re partition
        p = sector0[446:462]
        start = struct.unpack("<I", p[8:12])[0]
        size = struct.unpack("<I", p[12:16])[0]
        out["partition"] = (start, size, p[4])
        bpb = read_sector(start)
    tot = struct.unpack("<H", bpb[19:21])[0] or struct.unpack("<I", bpb[32:36])[0]
    out["fs_start"] = start
    out["fs_sectors"] = tot
    out["end_sector"] = start + tot
    lab = bpb[43:54] if bpb[38] == 0x29 else b""
    out["label"] = lab.decode("latin1", "replace").strip()
    out["fstype"] = bpb[54:62].decode("latin1", "replace").strip()
    return out


# --------------------------------------------------------------------------
# Commandes
# --------------------------------------------------------------------------

STATE_DIR = os.path.expanduser(os.environ.get("PA11_HOME", "~/PA11"))


def state_paths(chip):
    sfx = "" if chip == 0 else "-puce%d" % chip
    return (os.path.join(STATE_DIR, "PA11%s.img" % sfx),
            os.path.join(STATE_DIR, "PA11%s.state.json" % sfx))


def cmd_info(dev, args):
    t = dev.t
    log("Lecteur trouvé : %s %s" % (getattr(t, "manufacturer", ""), getattr(t, "product", "")))
    log("  Version DCC : %s   version SmartMedia : %s   puce : %s" % (
        hex(dev.dcc.dcc_version) if dev.dcc.dcc_version is not None else "?",
        hex(dev.dcc.sm_version) if dev.dcc.sm_version is not None else "?",
        ("STMP%04X" % dev.dcc.chip_id) if dev.dcc.chip_id else "?"))
    log("  Alimentation : %s   (réponse brute : %s)" % (
        "OK" if dev.power_ok else "FAIBLE / inconnue", dev.dcc.last_power_raw.hex(" ")))
    for c in dev.chips:
        log("\nMémoire %d : %s %s (id 0x%02X), %d blocs de %d Ko" % (
            c.index, MAKERS.get(c.maker, "fabricant 0x%02X" % c.maker), c.name, c.devid,
            c.nphys, c.block_bytes // 1024))
        c.scan(progress_bar("analyse des blocs"))
        k = c.counts()
        log("  Blocs utilisés %d, libres %d, inutilisables %d (dont firmware SigmaTel %d), "
            "CIS au bloc %s, doublons %d" % (k["used"], k["free"], k["unusable"],
                                             c.stmp_blocks, c.cis_block, c.duplicates))
        log("  Capacité logique : %d blocs = %.1f Mo" % (
            c.capacity_blocks, c.capacity_blocks * c.block_bytes / 1048576))
        blk0, st = c.read_logical_block(0)
        vol = describe_volume(blk0[:512], lambda s: _read_sector(c, s))
        log("  Volume : %s" % vol)
        if st["bad"] or st["corrected"]:
            log("  ECC bloc 0 : %s" % st)


def _read_sector(chip, sector):
    lb, off = divmod(sector * 512, chip.block_bytes)
    data, _ = chip.read_logical_block(lb)
    return data[off:off + 512]


def image_blocks(chip):
    """Nombre de blocs logiques à copier : capacité, étendue au volume FAT si besoin."""
    n = chip.capacity_blocks
    try:
        blk0, _ = chip.read_logical_block(0)
        vol = describe_volume(blk0[:512], lambda s: _read_sector(chip, s))
        if "end_sector" in vol:
            need = -(-vol["end_sector"] * 512 // chip.block_bytes)
            if need > n:
                log("  ATTENTION : le volume FAT annonce %d blocs mais la capacité est de %d ; "
                    "seuls les %d premiers sont copiés et modifiables." % (need, n, n))
    except Exception as e:
        dbg("describe_volume:", e)
    return n


def cmd_backup(dev, args):
    os.makedirs(STATE_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for c in dev.chips:
        path = os.path.join(STATE_DIR, "sauvegarde-brute-puce%d-%s.bin" % (c.index, stamp))
        log("Sauvegarde brute de la mémoire %d (%s) vers %s" % (c.index, c.name, path))
        cb = progress_bar("lecture")
        per = MAX_XFER // PAGE_RAW
        with open(path + ".part", "wb") as f:
            hdr = json.dumps({"pa11": VERSION, "chip": c.index, "maker": c.maker,
                              "devid": c.devid, "ppb": c.ppb, "nphys": c.nphys,
                              "page_raw": PAGE_RAW}).encode()
            f.write(b"PA11RAW\0" + struct.pack("<I", len(hdr)) + hdr)
            total = c.nphys * c.ppb
            for page in range(0, total, per):
                f.write(dev.dcc.read_pages(c.index, page, min(per, total - page)))
                cb(page + per, total)
        os.replace(path + ".part", path)
        log("  OK (%.1f Mo)" % (os.path.getsize(path) / 1048576))


def pull_chip(c, img, state):
    c.scan(progress_bar("analyse des blocs"))
    n = image_blocks(c)
    hashes = []
    errs = {"corrected": 0, "bad": 0}
    cb = progress_bar("copie       ")
    with open(img + ".part", "wb") as f:
        for lb in range(n):
            data, st = c.read_logical_block(lb)
            errs["corrected"] += st["corrected"]
            errs["bad"] += st["bad"]
            f.write(data)
            hashes.append(hashlib.sha1(data).hexdigest())
            cb(lb + 1, n)
    os.replace(img + ".part", img)
    with open(state, "w") as f:
        json.dump({"version": VERSION, "chip": c.index, "devid": c.devid,
                   "block_bytes": c.block_bytes, "blocks": n, "hashes": hashes,
                   "time": time.time()}, f)
    if errs["corrected"]:
        log("  %d erreur(s) de lecture corrigée(s) par l'ECC." % errs["corrected"])
    if errs["bad"]:
        log("  ATTENTION : %d page(s) illisible(s) ou marquée(s) défectueuse(s)." % errs["bad"])
    return n


def cmd_pull(dev, args):
    os.makedirs(STATE_DIR, exist_ok=True)
    for c in dev.chips:
        img, state = state_paths(c.index)
        log("Copie du contenu de la mémoire %d (%s) vers %s" % (c.index, c.name, img))
        n = pull_chip(c, img, state)
        log("  OK : %d blocs, %.1f Mo" % (n, n * c.block_bytes / 1048576))


def changed_blocks(img, st):
    bs = st["block_bytes"]
    changed = []
    with open(img, "rb") as f:
        for lb, h in enumerate(st["hashes"]):
            data = f.read(bs)
            if len(data) < bs:
                data = data + b"\xff" * (bs - len(data))
            if hashlib.sha1(data).hexdigest() != h:
                changed.append(lb)
    return changed


def has_backup(chip):
    if not os.path.isdir(STATE_DIR):
        return False
    pre = "sauvegarde-brute-puce%d-" % chip
    return any(f.startswith(pre) and f.endswith(".bin") for f in os.listdir(STATE_DIR))


def cmd_push(dev, args):
    for c in dev.chips:
        if not has_backup(c.index) and not args.force:
            raise PA11Error("Aucune sauvegarde brute dans %s : lancez d'abord « pa11 backup » "
                            "(une seule fois) avant toute écriture." % STATE_DIR)
    for c in dev.chips:
        img, state = state_paths(c.index)
        if not os.path.exists(img) or not os.path.exists(state):
            log("Mémoire %d : pas d'image locale (%s), rien à écrire." % (c.index, img))
            continue
        with open(state) as f:
            st = json.load(f)
        if st["devid"] != c.devid or st["block_bytes"] != c.block_bytes:
            raise PA11Error("L'image locale ne correspond pas à ce lecteur.")
        if os.path.getsize(img) != st["blocks"] * st["block_bytes"]:
            raise PA11Error("La taille de l'image a changé : refus d'écrire.")
        changed = changed_blocks(img, st)
        log("Mémoire %d : %d bloc(s) modifié(s) sur %d." % (c.index, len(changed), st["blocks"]))
        if not changed:
            continue
        if args.dry_run:
            log("  (simulation : aucune écriture) blocs : %s" % changed[:40])
            continue
        if not dev.power_ok and not (args.force or args.ignorer_alimentation):
            raise PA11Error("Le lecteur signale une alimentation faible (réponse brute : %s). "
                            "Changez la pile, ou relancez avec --ignorer-alimentation."
                            % dev.dcc.last_power_raw.hex(" "))
        c.scan(progress_bar("analyse des blocs"))
        if c.duplicates and not args.force:
            raise PA11Error("La table des blocs contient %d doublon(s) : écriture refusée par "
                            "sécurité (--force pour passer outre)." % c.duplicates)
        # On écrit d'abord les données, et en dernier le début du volume (FAT,
        # répertoire racine) : en cas d'interruption, l'ancien index reste cohérent.
        order = sorted(changed, reverse=True)
        cb = progress_bar("écriture    ")
        bs = st["block_bytes"]
        with open(img, "rb") as f:
            for i, lb in enumerate(order):
                if not (args.force or args.ignorer_alimentation) and not dev.dcc.get_power():
                    raise PA11Error(
                        "Alimentation faible signalée pendant l'écriture (réponse brute : %s) : "
                        "arrêt. Relancez « Fermer PA11 » (le reste sera écrit), ou dans le "
                        "Terminal :  ~/PA11/pa11 close --ignorer-alimentation"
                        % dev.dcc.last_power_raw.hex(" "))
                f.seek(lb * bs)
                data = bytearray(f.read(bs))
                c.write_logical_block(lb, data, None if args.force else st["hashes"][lb])
                st["hashes"][lb] = hashlib.sha1(data).hexdigest()
                with open(state + ".tmp", "w") as g:
                    json.dump(st, g)
                os.replace(state + ".tmp", state)
                cb(i + 1, len(order))
        log("  Écriture terminée et vérifiée.")


# -- montage macOS ---------------------------------------------------------

def _hdiutil(*a):
    return subprocess.run(["hdiutil"] + list(a), capture_output=True)


def mount_image(img):
    r = _hdiutil("attach", "-plist", "-imagekey", "diskimage-class=CRawDiskImage",
                 "-noverify", "-noautofsck", img)
    if r.returncode != 0:
        raise PA11Error("hdiutil attach a échoué : %s" % r.stderr.decode(errors="replace"))
    pl = plistlib.loads(r.stdout)
    mounts, devs = [], []
    for e in pl.get("system-entities", []):
        if e.get("dev-entry"):
            devs.append(e["dev-entry"])
        if e.get("mount-point"):
            mounts.append(e["mount-point"])
    return devs, mounts


def mounted_devices_for(img):
    r = _hdiutil("info", "-plist")
    if r.returncode != 0:
        return [], []
    pl = plistlib.loads(r.stdout)
    real = os.path.realpath(img)
    for im in pl.get("images", []):
        if os.path.realpath(im.get("image-path", "")) == real:
            devs = [e["dev-entry"] for e in im.get("system-entities", []) if e.get("dev-entry")]
            mps = [e["mount-point"] for e in im.get("system-entities", [])
                   if e.get("mount-point")]
            return devs, mps
    return [], []


def prevent_mac_junk(mp):
    """Empêche Spotlight et fseventsd d'écrire leurs index sur le lecteur."""
    try:
        open(os.path.join(mp, ".metadata_never_index"), "w").close()
        os.makedirs(os.path.join(mp, ".fseventsd"), exist_ok=True)
        open(os.path.join(mp, ".fseventsd", "no_log"), "w").close()
    except OSError:
        pass
    subprocess.run(["mdutil", "-i", "off", mp], capture_output=True)


def clean_mac_junk(mp):
    """Supprime ce que macOS ajoute (corbeille, ._fichiers, .DS_Store, index)."""
    for name in (".Spotlight-V100", ".Trashes", ".TemporaryItems"):
        p = os.path.join(mp, name)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    fse = os.path.join(mp, ".fseventsd")
    if os.path.isdir(fse):
        for fn in os.listdir(fse):
            if fn != "no_log":
                try:
                    os.remove(os.path.join(fse, fn))
                except OSError:
                    shutil.rmtree(os.path.join(fse, fn), ignore_errors=True)
    try:
        subprocess.run(["dot_clean", "-m", mp], capture_output=True)
    except Exception:
        pass
    for root, dirs, files in os.walk(mp):
        for fn in files:
            if fn.startswith("._") or fn == ".DS_Store":
                try:
                    os.remove(os.path.join(root, fn))
                except OSError:
                    pass


def cmd_open(dev, args):
    if sys.platform != "darwin":
        log("(montage automatique disponible seulement sur macOS)")
    for c in dev.chips:
        img, state = state_paths(c.index)
        if os.path.exists(img) and mounted_devices_for(img)[0]:
            raise PA11Error("L'image %s est déjà montée : faites d'abord « close »." % img)
    if not all(has_backup(c.index) for c in dev.chips):
        log("Première utilisation : sauvegarde complète de sécurité (une seule fois).")
        cmd_backup(dev, args)
    cmd_pull(dev, args)
    if sys.platform == "darwin":
        for c in dev.chips:
            img, _ = state_paths(c.index)
            devs, mps = mount_image(img)
            if not mps:
                raise PA11Error("L'image a été attachée mais aucun volume n'a été monté "
                                "(système de fichiers non reconnu ?).")
            for mp in mps:
                prevent_mac_junk(mp)
                log("\nLe contenu du lecteur est monté ici : %s" % mp)
                subprocess.run(["open", mp])
        log("\nCopiez ou supprimez vos fichiers dans le Finder, puis lancez « Fermer PA11 »\n"
            "(ou : pa11 close). Laissez le lecteur branché et ne l'utilisez pas entre-temps.\n"
            "Les fichiers mis à la corbeille depuis ce volume seront réellement supprimés.")


def cmd_close(dev, args):
    if sys.platform == "darwin":
        for c in dev.chips:
            img, _ = state_paths(c.index)
            devs, mps = mounted_devices_for(img)
            for mp in mps:
                clean_mac_junk(mp)
            if devs:
                whole = sorted(devs, key=len)[0]
                r = _hdiutil("detach", whole)
                if r.returncode != 0:
                    r = _hdiutil("detach", "-force", whole)
                    if r.returncode != 0:
                        raise PA11Error("Impossible de démonter l'image : fermez les fenêtres "
                                        "ou applications qui l'utilisent.")
                log("Image démontée.")
    cmd_push(dev, args)
    log("\nVous pouvez débrancher le lecteur.")


def main(argv=None):
    global DEBUG
    ap = argparse.ArgumentParser(
        prog="pa11",
        description="Accès au lecteur MP3 Napa/A-Max PA11 (SigmaTel STMP3400) sans pilote.")
    ap.add_argument("--debug", action="store_true", help="affiche les échanges USB")
    ap.add_argument("--simulate", metavar="FICHIER",
                    help="(tests) utilise un lecteur simulé au lieu du vrai")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="diagnostic (lecture seule)")
    sub.add_parser("backup", help="sauvegarde brute de toute la mémoire (lecture seule)")
    sub.add_parser("pull", help="copie le contenu du lecteur dans ~/PA11/PA11.img")
    p = sub.add_parser("push", help="réécrit sur le lecteur les blocs modifiés de l'image")
    p.add_argument("--dry-run", action="store_true", help="liste les blocs sans écrire")
    p.add_argument("--force", action="store_true", help="ignore les contrôles de sécurité")
    p.add_argument("--ignorer-alimentation", action="store_true",
                   help="écrit même si le lecteur signale une pile faible")
    sub.add_parser("open", help="pull + montage de l'image dans le Finder (macOS)")
    p = sub.add_parser("close", help="démontage + push (macOS)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--ignorer-alimentation", action="store_true",
                   help="écrit même si le lecteur signale une pile faible")
    args = ap.parse_args(argv)
    DEBUG = args.debug

    dev = None
    try:
        transport = None
        if args.simulate:
            from pa11sim import SimTransport
            transport = SimTransport.load(args.simulate)
        dev = Device(transport)
        {"info": cmd_info, "backup": cmd_backup, "pull": cmd_pull, "push": cmd_push,
         "open": cmd_open, "close": cmd_close}[args.cmd](dev, args)
        if args.simulate:
            transport.save(args.simulate)
    except PA11Error as e:
        log("\nErreur : %s" % e)
        return 1
    except KeyboardInterrupt:
        log("\nInterrompu.")
        return 130
    finally:
        if dev:
            dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
