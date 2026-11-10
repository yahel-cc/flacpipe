#!/usr/bin/env python3
"""Comprobación sin pantalla de fh_fiio.clasificar y de que copiar() sigue igual.

Biblioteca y tarjeta de verdad, con archivos diminutos en una carpeta temporal:
un álbum nuevo, uno al día y uno con una pista de menos. Sale con 0 si todo cuadra.

    python3 pruebas/fiio_clasificar.py
"""
import os
import shutil
import sys
import tempfile

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RAIZ, "flac_hunter"))
import fh_fiio as fiio  # noqa: E402

fallos = []


def comprobar(que, ok):
    print(("  ok  " if ok else "  MAL ") + que)
    if not ok:
        fallos.append(que)


def escribir(carpeta, pistas):
    os.makedirs(carpeta, exist_ok=True)
    for p in pistas:
        with open(os.path.join(carpeta, p), "wb") as f:
            f.write(b"fLaC" + b"\0" * 100)


def main():
    base = tempfile.mkdtemp(prefix="fiio-clasificar-")
    try:
        lib = os.path.join(base, "lib")
        tarjeta = os.path.join(base, "tarjeta")
        os.makedirs(tarjeta)
        pistas = ["01 A.flac", "02 B.flac", "03 C.flac"]
        for artista, album in (("Uno", "Nuevo"), ("Dos", "AlDia"), ("Tres", "Cojo")):
            escribir(os.path.join(lib, artista, album), pistas)
        escribir(os.path.join(tarjeta, "Dos", "AlDia"), pistas)
        escribir(os.path.join(tarjeta, "Tres", "Cojo"), pistas[:2])

        albumes = fiio.listar_biblioteca(lib)
        rels = lambda xs: [a["rel"] for a in xs]  # noqa: E731
        r = fiio.clasificar(tarjeta, albumes)
        print("clasificar:")
        comprobar("claves", sorted(r) == ["al_dia", "distintos", "nuevos", "otro_orden"])
        comprobar("nuevos", rels(r["nuevos"]) == ["Uno/Nuevo"])
        comprobar("al_dia", rels(r["al_dia"]) == ["Dos/AlDia"])
        comprobar("distintos", rels(r["distintos"]) == ["Tres/Cojo"])
        comprobar("otro_orden vacío fuera de FAT", rels(r["otro_orden"]) == [])
        comprobar("no escribe nada", not os.path.exists(os.path.join(tarjeta, "Uno")))

        # Orden físico distinto: se simula forzando que el orden sea observable
        # y que listdir devuelva otro orden.
        original = (fiio._orden_es_observable, fiio._orden_fisico)
        fiio._orden_es_observable = lambda ruta: True
        fiio._orden_fisico = lambda d: list(reversed(pistas)) if d.endswith("AlDia") \
            else original[1](d)
        try:
            r2 = fiio.clasificar(tarjeta, albumes)
        finally:
            fiio._orden_es_observable, fiio._orden_fisico = original
        comprobar("otro_orden en FAT", rels(r2["otro_orden"]) == ["Dos/AlDia"])

        print("copiar:")
        eventos, lineas = [], []
        res = fiio.copiar(tarjeta, albumes, informar=lineas.append, evento=eventos.append)
        comprobar("copia sólo el nuevo", res["copiados"] == 1 and res["archivos"] == 3)
        comprobar("omite el que está al día", res["omitidos"] == 1)
        comprobar("avisa del incompleto", res["desordenados"] == ["Tres/Cojo"])
        comprobar("sin errores", not res["errores"] and not res["abortado"])
        omitidos = {e["rel"]: e["motivo"] for e in eventos if e["t"] == "omitido"}
        comprobar("eventos omitido", omitidos == {
            "Dos/AlDia": "ya está y en orden", "Tres/Cojo": "copia incompleta o distinta"})
        comprobar("nuevo copiado entero", sorted(os.listdir(
            os.path.join(tarjeta, "Uno", "Nuevo"))) == pistas)
        comprobar("incompleto intacto", len(os.listdir(
            os.path.join(tarjeta, "Tres", "Cojo"))) == 2)
        r3 = fiio.clasificar(tarjeta, albumes)
        comprobar("tras copiar, el nuevo está al día",
                  sorted(rels(r3["al_dia"])) == ["Dos/AlDia", "Uno/Nuevo"])
    finally:
        shutil.rmtree(base, ignore_errors=True)
    if fallos:
        print("FALLARON:", ", ".join(fallos))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
