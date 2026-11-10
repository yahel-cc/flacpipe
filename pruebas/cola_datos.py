#!/usr/bin/env python3
"""Comprueba, sin pantalla, lo que Datos sabe de cada pista que baja.

    python3 pruebas/cola_datos.py

Monta el escenario `descargando` de probar.py en pruebas/salida/cola_datos/,
hace crecer las dos pistas a medias y deja correr el sondeo (Datos._ciclo)
unos segundos. Luego mira pistas_activas, recien_bajadas, bytes_album_total
y calidad_corta, y qué pasa si downloads.json falta, está vacío o roto.
Sale con 0 si todo cuadra.
"""
import json
import os
import shutil
import sys
import threading
import time

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)

import conductor  # noqa: E402
import probar     # noqa: E402

tui = conductor.tui
RAIZ = os.path.join(probar.SALIDA, "cola_datos")
FALLOS = []
TOQUES = [0]


def comprobar(cierto, que):
    print(("  bien  " if cierto else "  MAL   ") + que)
    if not cierto:
        FALLOS.append(que)


def sondear(datos, segundos):
    """El hilo de Datos, a mano: un ciclo cada 0,25 s (las descargas, cada 1 s)."""
    fin = time.monotonic() + segundos
    while time.monotonic() < fin:
        datos._ciclo()
        time.sleep(0.25)


def tocar(ruta, contenido):
    """Reescribe downloads.json y le cambia la fecha, para que se relea."""
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(contenido)
    TOQUES[0] += 1
    t = time.time() + TOQUES[0]
    os.utime(ruta, (t, t))


def main():
    print("calidad_corta")
    for detalle, esperado in (("15 pistas, 16-bit / 44.1 kHz", "FLAC 16/44.1"),
                              ("7 pistas, 24-bit / 96 kHz", "FLAC 24/96"),
                              ("9 pistas, 24-bit", "FLAC 24"),
                              ("15 pistas, ?-bit / 44.1 kHz", "FLAC"),
                              ("14 pistas, desconocida", "FLAC"),
                              ("", ""), (None, "")):
        comprobar(tui.calidad_corta(detalle) == esperado,
                  f"calidad_corta({detalle!r}) == {esperado!r}")

    esc = probar.escenario_falso(RAIZ, "descargando")
    conductor.preparar(esc)
    siete, ocho = (os.path.basename(r)[42:] for r in esc["_actual"])
    # Un incompleto de otra descarga: downloads.json lo pone en otra carpeta.
    ajeno = os.path.join(esc["tui"]["DIR_INCOMPLETOS"], "INCOMPLETE" + "99" * 16 + "Otra cosa.flac")
    with open(ajeno, "wb") as f:
        f.write(b"\0" * 1000)

    parar = threading.Event()
    hilo = threading.Thread(target=probar.crecer, args=(esc["_actual"], parar), daemon=True)
    hilo.start()
    datos = tui.Datos()
    try:
        print("dos pistas bajando")
        sondear(datos, 2.3)
        pet = datos.peticion_en_curso()
        comprobar(pet is not None and pet["id"] == esc["_id_baja"],
                  "la petición en curso es la que baja, no la que busca")
        activas = {p["archivo"]: p for p in datos.pistas_activas}
        comprobar(sorted(activas) == [siete, ocho], f"pistas_activas = {sorted(activas)}")
        comprobar(activas.get(siete, {}).get("total") == esc["_tamanos"][siete],
                  f"{siete}: total sale de downloads.json")
        comprobar(ocho in activas and activas[ocho]["total"] is None,
                  f"{ocho}: sin fila en downloads.json, total None")
        comprobar(all(p["velocidad"] > 0 for p in activas.values()),
                  "velocidad > 0 en las dos: " + ", ".join(
                      f"{p['velocidad'] / 1e6:.1f} MB/s" for p in activas.values()))
        comprobar(all(0 < activas[os.path.basename(r)[42:]]["bytes"] <= os.path.getsize(r)
                      for r in esc["_actual"] if os.path.basename(r)[42:] in activas),
                  "bytes: lo que medía el incompleto al mirarlo")
        comprobar("Otra cosa.flac" not in [n for n, _b in datos.incompletos],
                  "el incompleto de otra descarga no cuenta")
        comprobar(datos.bytes_album_total is None,
                  "bytes_album_total None: a la 08 le falta el tamaño")
        comprobar(len(datos.bajados) == 6, "bajados sigue como estaba (6)")

        print("downloads.json con todas las pistas")
        filas = json.load(open(esc["_transferencias"], encoding="utf-8"))
        filas.append(["shibuya_kei", esc["_remota"] + ocho, esc["_carpeta"], "Transferring",
                      esc["_tamanos"][ocho], None, {}])
        tocar(esc["_transferencias"], json.dumps(filas))
        sondear(datos, 1.1)
        activas = {p["archivo"]: p for p in datos.pistas_activas}
        comprobar(activas.get(ocho, {}).get("total") == esc["_tamanos"][ocho],
                  f"{ocho}: total ahora sí")
        comprobar(datos.bytes_album_total == sum(esc["_tamanos"].values()),
                  f"bytes_album_total = {datos.bytes_album_total}")

        print("una pista termina")
        shutil.move(esc["_actual"][0], os.path.join(esc["_carpeta"], siete))
        sondear(datos, 1.1)
        comprobar(siete in datos.recien_bajadas, f"{siete} en recien_bajadas")
        comprobar([p["archivo"] for p in datos.pistas_activas] == [ocho],
                  "sólo queda la 08 bajando")
        sondear(datos, tui.SEGUNDOS_RECIEN + 0.3)
        comprobar(siete not in datos.recien_bajadas, "y al rato deja de ser reciente")

        print("downloads.json a medio escribir: vale el .old")
        with open(esc["_transferencias"] + ".old", "w", encoding="utf-8") as f:
            json.dump(filas, f)
        tocar(esc["_transferencias"], json.dumps(filas)[:200])
        sondear(datos, 1.1)
        activas = {p["archivo"]: p for p in datos.pistas_activas}
        comprobar(activas.get(ocho, {}).get("total") == esc["_tamanos"][ocho],
                  f"{ocho}: el tamaño sale del .old")
        os.remove(esc["_transferencias"] + ".old")

        print("downloads.json roto, vacío o ausente (y sin .old)")
        for nombre, contenido in (("roto", "[[\"a\", "), ("vacío", ""),
                                  ("otra forma", "{\"a\": 1}"), ("ausente", None)):
            if contenido is None:
                os.remove(esc["_transferencias"])
            else:
                tocar(esc["_transferencias"], contenido)
            sondear(datos, 1.1)
            comprobar(ocho in [p["archivo"] for p in datos.pistas_activas]
                      and all(p["total"] is None for p in datos.pistas_activas)
                      and datos.bytes_album_total is None, f"{nombre}: sin tamaños y sin error")
    finally:
        parar.set()
        hilo.join(1)
        datos.cerrar()

    if FALLOS:
        print(f"FALLARON {len(FALLOS)}")
        return 1
    print("todo bien")
    return 0


if __name__ == "__main__":
    sys.exit(main())
