#!/usr/bin/env python3
"""La interfaz en tu terminal con descargas de mentira, para verla moverse.

    python3 pruebas/demo.py

Monta en pruebas/salida/demo/ una cola, un log, una biblioteca y una tarjeta
inventadas, y un guion que las va cambiando como lo haría el plugin:

    · Nujabes - Modal Soul baja pista a pista hasta terminar (destello y aviso)
    · CASIOPEA - Mint Jams espera turno, busca (el sonar y la cuenta de 45 s
      acortada a 12) y baja
    · en la pestaña 3 hay una «SNOWSKY» con la biblioteca de mentira: ↵ copia

Los archivos que «se bajan» crecen como archivos dispersos: dicen pesar lo que
pesaría un FLAC pero no ocupan disco. Nada toca ~/Música ni tu cola de
verdad. Se sale con q; el guion vuelve a empezar mientras la tengas abierta.
"""
import json
import math
import os
import sys
import threading
import time

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.join(AQUI, "salida", "demo")
BIBLIOTECA = os.path.join(RAIZ, "Música")
# Antes de importar nada de flacpipe: la ruta de la biblioteca se lee al importar.
os.environ["FLACPIPE_BIBLIOTECA"] = BIBLIOTECA
sys.path.insert(0, AQUI)

import conductor  # noqa: E402
import probar     # noqa: E402

tui = conductor.tui
comun = tui.comun

NUJABES = ["Feather", "Ordinary Joe", "Reflection Eternal", "Luv(sic) pt3", "Music Is Mine",
           "Eclipse", "The Sign", "Thank You", "World's End Rhapsody", "Sea of Cloud",
           "Horizon", "Modal Soul", "Flowers", "Light on the Land", "Perfect Circle"]
CASIOPEA = ["Take Me", "Asayake", "Midnight Rendezvous", "Time Limit", "Domino Line",
            "Tears of the Star", "Swear"]


class Guion:
    """Lo que haría el plugin, a mano y en orden, sobre los archivos del escenario."""

    def __init__(self, esc, parar):
        self.esc = esc
        self.parar = parar
        self.cola = esc["_cola"]
        self.log_ruta = esc["comun"]["ARCHIVO_LOG"]
        self.incompletos = esc["tui"]["DIR_INCOMPLETOS"]
        self.descargas = esc["comun"]["DIR_DESCARGAS"]

    def esperar(self, segundos):
        return self.parar.wait(segundos)

    def log(self, texto):
        with open(self.log_ruta, "a", encoding="utf-8") as f:
            f.write(f"{probar.iso(0)}  {texto}\n")

    def cambiar(self, pid, **campos):
        with open(self.cola, encoding="utf-8") as f:
            cola = json.load(f)
        for p in cola["peticiones"]:
            if p["id"] == pid:
                p.update(campos)
        tmp = self.cola + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cola, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.cola)

    def bajar(self, pid, titulos, desde, segundos=2.6):
        """Cada pista crece en la carpeta de incompletos a unos 13 MB/s con
        vaivén y al acabar pasa a .descargas/<id>, como hace Nicotine+."""
        carpeta = os.path.join(self.descargas, pid)
        os.makedirs(carpeta, exist_ok=True)
        total = len(titulos)
        for n in range(desde, total + 1):
            archivo = f"{n:02d} {titulos[n - 1]}.flac"
            ruta = os.path.join(self.incompletos, "INCOMPLETE" + "cd" * 16 + archivo)
            tam, t0 = 0, time.monotonic()
            with open(ruta, "wb") as f:
                f.write(b"fLaC")
            while time.monotonic() - t0 < segundos:
                if self.esperar(0.2):
                    return False
                fase = time.monotonic() - t0
                tam += int(13_000_000 * 0.2 * (1 + 0.45 * math.sin(fase * 2.3)))
                os.truncate(ruta, tam)
            os.replace(ruta, os.path.join(carpeta, archivo))
            self.log(f"[archivo] {archivo}")
            self.cambiar(pid, progreso={"terminados": n, "total": total},
                         actualizado=probar.iso(0))
        return True

    def terminar(self, pid, artista, album, titulos, calidad):
        """Lo bajado pasa a la biblioteca, como al organizar, y la petición acaba."""
        origen = os.path.join(self.descargas, pid)
        destino = os.path.join(BIBLIOTECA, artista, album)
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        if os.path.isdir(destino):
            for nombre in os.listdir(destino):
                os.remove(os.path.join(destino, nombre))
            os.rmdir(destino)
        if os.path.isdir(origen):
            os.replace(origen, destino)
        self.cambiar(pid, estado="listo", actualizado=probar.iso(0),
                     progreso={"terminados": len(titulos), "total": len(titulos)},
                     resultado={"carpeta": f"{artista}/{album}", "artista": artista,
                                "album": album, "pistas": len(titulos), "calidad": calidad,
                                "parcial": False, "faltantes": []},
                     mensaje=None)
        self.log(f"[listo] {pid} → {artista}/{album} ({len(titulos)} pistas, {calidad})")

    def correr(self):
        esc = self.esc
        id_baja, id_espera = esc["_id_baja"], "20260904-090600-casiopea-mint-jams"
        while not self.parar.is_set():
            if not self.bajar(id_baja, NUJABES, desde=7):
                return
            self.terminar(id_baja, "Nujabes", "Modal Soul", NUJABES, "16-bit / 44.1 kHz")
            if self.esperar(5):
                return
            self.cambiar(id_espera, estado="buscando", actualizado=probar.iso(0),
                         mensaje="buscando fuentes")
            self.log(f"[buscar] {id_espera} término='CASIOPEA - Mint Jams flac'")
            if self.esperar(12):
                return
            for puntos, usuario in ((276, "jazzfusion_jp"), (241, "citypop_crate"),
                                    (188, "vinyl_rips")):
                self.log(f"[candidato]   {puntos}  {usuario}  7 pistas, 24-bit / 96 kHz"
                         f"  Mint Jams (1982) [FLAC]")
            self.cambiar(id_espera, estado="descargando", actualizado=probar.iso(0),
                         candidato={"usuario": "jazzfusion_jp", "puntos": 276,
                                    "carpeta": "jazz\\CASIOPEA\\Mint Jams (1982) [FLAC]",
                                    "detalle": "7 pistas, 24-bit / 96 kHz"},
                         progreso={"terminados": 0, "total": len(CASIOPEA)},
                         mensaje="descargando de jazzfusion_jp")
            self.log(f"[descargar] {id_espera} 7 archivos de jazzfusion_jp")
            if not self.bajar(id_espera, CASIOPEA, desde=1, segundos=2.2):
                return
            self.terminar(id_espera, "CASIOPEA", "Mint Jams", CASIOPEA, "24-bit / 96 kHz")
            if self.esperar(10):
                return
            # y otra vez desde el principio, con la cola como al empezar
            self.esc = esc = montar()


def montar():
    esc = probar.escenario_falso(os.path.join(RAIZ, "estado"), "descargando")
    # El escenario deja una pista a medias que nunca crece; aquí las baja el guion.
    os.remove(esc["_actual"])
    # Dos que ya acabaron, para que la lista no empiece vacía: una bien y una mal.
    with open(esc["_cola"], encoding="utf-8") as f:
        cola = json.load(f)
    hechas = [
        {"id": "20260904-083000-tom-misch-geography", "consulta": "Tom Misch - Geography",
         "tipo": "album", "estado": "listo", "creado": probar.iso(3900),
         "actualizado": probar.iso(3600), "intentos": 0, "candidato": None,
         "rechazados": [], "progreso": None, "mensaje": None,
         "resultado": {"carpeta": "Tom Misch/Geography", "artista": "Tom Misch",
                       "album": "Geography", "pistas": 6, "calidad": "24-bit / 48 kHz",
                       "parcial": False, "faltantes": []}},
        {"id": "20260904-084500-joy-again-piano", "consulta": "Joy Again - Piano",
         "tipo": "cancion", "estado": "fallido", "creado": probar.iso(2900),
         "actualizado": probar.iso(2500), "intentos": 3, "candidato": None,
         "rechazados": [], "progreso": None, "resultado": None,
         "mensaje": "sin fuentes en FLAC tras 3 intentos"},
    ]
    cola["peticiones"] = hechas + cola["peticiones"]
    with open(esc["_cola"], "w", encoding="utf-8") as f:
        json.dump(cola, f, ensure_ascii=False, indent=1)
    probar.biblioteca_de_mentira(BIBLIOTECA, probar.ALBUMES_DE_MUESTRA)
    esc["nicotine_vivo"] = True
    esc["fiio_falso"] = True
    # La cuenta de la búsqueda, acortada al ritmo del guion.
    esc["tui"]["VENTANA_BUSQUEDA"] = 12
    return esc


def main():
    esc = montar()
    conductor.preparar(esc)
    if os.environ.get("SALIDA"):                 # lo lanza el banco de pruebas
        conductor.volcar_cuadros(os.environ["SALIDA"], os.environ.get("GRABAR") == "1")
    parar = threading.Event()
    guion = Guion(esc, parar)
    threading.Thread(target=guion.correr, daemon=True).start()
    try:
        return tui.ejecutar()
    finally:
        parar.set()


if __name__ == "__main__":
    sys.exit(main())
