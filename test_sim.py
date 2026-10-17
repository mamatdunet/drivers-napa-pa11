# -*- coding: utf-8 -*-
"""Tests de bout en bout de pa11.py sur un lecteur simulé (Linux : nécessite mtools)."""
import importlib
import os
import random
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pa11      # noqa
import pa11sim   # noqa

W = tempfile.mkdtemp(prefix="pa11test-")
os.chdir(W)
FAILS = []


def check(name, cond):
    print(("  OK   " if cond else "  ÉCHEC ") + name, flush=True)
    if not cond:
        FAILS.append(name)


def run(home, *a):
    os.environ["PA11_HOME"] = os.path.join(W, home)
    importlib.reload(pa11)
    devnull = open(os.devnull, "w")
    old = sys.stdout
    sys.stdout = devnull
    try:
        return pa11.main(["--simulate", "p.sim"] + list(a))
    finally:
        sys.stdout = old


def mkfat(path, blocks, bs, fill=b"\x00"):
    with open(path, "wb") as f:
        f.write(fill * (blocks * bs))
    subprocess.run(["mkfs.fat", "-n", "NAPA", path], capture_output=True, check=True)


def mt(*a):
    subprocess.run(list(a), capture_output=True, check=True)


def scenario(devid, nphys, ppb, label):
    print("\n== %s ==" % label)
    bs = ppb * 512
    reserved = -(-(1024 * 1024) // bs)
    nlog = {1024: 1000}.get(nphys, nphys // 1024 * 1000)
    cap = nlog - reserved
    mkfat("orig.img", cap, bs, fill=b"\xff")
    r = random.Random(devid)
    open("a.mp3", "wb").write(r.randbytes(min(3_000_000, cap * bs // 4)))
    mt("mcopy", "-i", "orig.img", "a.mp3", "::/A.MP3")
    t = pa11sim.build_player(open("orig.img", "rb").read(), devid=devid, nphys=nphys, ppb=ppb,
                             seed=devid, bad=(7,))
    t.save("p.sim")
    for d in ("h", "h2"):
        shutil.rmtree(d, ignore_errors=True)
    check("info", run("h", "info") == 0)
    check("pull", run("h", "pull") == 0)
    check("image identique", open("h/PA11.img", "rb").read() == open("orig.img", "rb").read())
    open("b.mp3", "wb").write(r.randbytes(min(2_000_000, cap * bs // 4)))
    mt("mdel", "-i", "h/PA11.img", "::/A.MP3")
    mt("mcopy", "-i", "h/PA11.img", "b.mp3", "::/B.MP3")
    check("push refusé sans sauvegarde", run("h", "push") == 1)
    check("backup", run("h", "backup") == 0)
    bk = [f for f in os.listdir("h") if f.startswith("sauvegarde-brute")]
    check("fichier de sauvegarde", len(bk) == 1 and
          os.path.getsize(os.path.join("h", bk[0])) > nphys * ppb * 528)
    check("push", run("h", "push") == 0)
    check("relecture", run("h2", "pull") == 0)
    check("contenu après écriture identique",
          open("h2/PA11.img", "rb").read() == open("h/PA11.img", "rb").read())
    mt("mcopy", "-n", "-i", "h2/PA11.img", "::/B.MP3", "b2.mp3")
    check("fichier relu intact", open("b2.mp3", "rb").read() == open("b.mp3", "rb").read())
    fsck = subprocess.run(["fsck.fat", "-n", "h2/PA11.img"], capture_output=True)
    check("système de fichiers sain", fsck.returncode == 0)
    t = pa11sim.SimTransport.load("p.sim")
    c = t.chips[0]
    erased = {e[1] for e in t.log if e[0] == "erase"}
    fw = set(range(nphys - 64, nphys)) | {0, 1, 7}
    check("aucun effacement de bloc protégé", not (erased & fw))
    check("2e push : rien à écrire", run("h", "push") == 0 and
          sum(1 for e in pa11sim.SimTransport.load("p.sim").log if e[0] == "erase") == len(
              [e for e in t.log if e[0] == "erase"]))


scenario(0x76, 4096, 32, "64 Mo (Samsung K9F1208)")
scenario(0xE6, 1024, 16, "8 Mo (16 pages/bloc)")
scenario(0x79, 8192, 32, "128 Mo")
print("\n%d échec(s)" % len(FAILS))
shutil.rmtree(W, ignore_errors=True)
sys.exit(1 if FAILS else 0)
