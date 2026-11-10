#!/usr/bin/env python3
"""La interfaz en tu terminal con descargas de mentira, para verla moverse.

    python3 pruebas/demo.py

Monta en pruebas/salida/demo/ una cola, un log, una biblioteca y una tarjeta
inventadas, y un guion que las va cambiando como lo haría el plugin:

    · Nujabes - Modal Soul baja de dos en dos pistas, cada una a su velocidad,
      hasta terminar (destello y aviso); su downloads.json ya trae los tamaños
    · Tom Misch - Geography busca mientras tanto y al final falla; Hiatus
      Kaiyote - Mood Valiant ya había fallado
    · CASIOPEA - Mint Jams espera turno, busca (el sonar y la cuenta de 45 s
      acortada a 12) y baja; Nicotine+ tarda unos segundos en apuntar sus
      tamaños, así que al principio se ven bytes y velocidad sin porcentaje
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
# Bytes por segundo de cada una de las dos pistas que bajan a la vez.
VELOCIDADES = (16_000_000, 7_000_000)


class Guion:
    """Lo que haría el plugin, a mano y en orden, sobre los archivos del escenario."""

    def __init__(self, esc, parar):
        self.esc = esc
        self.parar = parar
        self.cola = esc["_cola"]
        self.log_ruta = esc["comun"]["ARCHIVO_LOG"]
        self.incompletos = esc["tui"]["DIR_INCOMPLETOS"]
        self.descargas = esc["comun"]["DIR_DESCARGAS"]
        self.transferencias = esc["tui"]["ARCHIVO_TRANSFERENCIAS"]

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

    def apuntar(self, pid, usuario, remota, tamanos):
        """Lo que Nicotine+ guarda en downloads.json: una fila por archivo,
        [usuario, ruta remota, carpeta, estado, tamaño, bytes, atributos]."""
        carpeta = os.path.join(self.descargas, pid)
        filas = [[usuario, remota + archivo, carpeta, "Queued", tam, None, {}]
                 for archivo, tam in tamanos.items()]
        tmp = self.transferencias + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(filas, f, ensure_ascii=False)
        os.replace(tmp, self.transferencias)

    def bajar(self, pid, tamanos, desde, apuntar_tras=None):
        """Dos pistas a la vez, cada una a su velocidad y con vaivén, crecen en
        la carpeta de incompletos; la que acaba pasa a .descargas/<id> y su
        carril toma la siguiente, como hace Nicotine+. Con apuntar_tras=(s, f),
        f() se llama a los s segundos: Nicotine+ guardando downloads.json."""
        carpeta = os.path.join(self.descargas, pid)
        os.makedirs(carpeta, exist_ok=True)
        archivos = list(tamanos)
        total, siguiente, terminados = len(archivos), desde, desde - 1
        carriles = [None] * len(VELOCIDADES)       # [archivo, ruta, bytes] o None
        t0 = time.monotonic()
        while terminados < total:
            for i, carril in enumerate(carriles):
                if carril is None and siguiente <= total:
                    archivo = archivos[siguiente - 1]
                    ruta = os.path.join(self.incompletos,
                                        "INCOMPLETE" + f"{siguiente:02d}" * 16 + archivo)
                    with open(ruta, "wb") as f:
                        f.write(b"fLaC")
                    carriles[i] = [archivo, ruta, 4]
                    siguiente += 1
            if self.esperar(0.2):
                return False
            fase = time.monotonic() - t0
            if apuntar_tras and fase >= apuntar_tras[0]:
                apuntar_tras[1]()
                apuntar_tras = None
            for i, carril in enumerate(carriles):
                if carril is None:
                    continue
                archivo, ruta, tam = carril
                paso = VELOCIDADES[i] * 0.2 * (1 + 0.45 * math.sin(fase * 2.3 + i * 1.9))
                carril[2] = tam = min(tamanos[archivo], tam + int(paso))
                os.truncate(ruta, tam)
                if tam >= tamanos[archivo]:
                    os.replace(ruta, os.path.join(carpeta, archivo))
                    carriles[i] = None
                    terminados += 1
                    self.log(f"[archivo] {archivo}")
                    self.cambiar(pid, progreso={"terminados": terminados, "total": total},
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
        id_busca = "20260904-090500-tom-misch-geography"
        while not self.parar.is_set():
            self.apuntar(id_baja, "shibuya_kei", esc["_remota"], esc["_tamanos"])
            if not self.bajar(id_baja, esc["_tamanos"], desde=7):
                return
            self.terminar(id_baja, "Nujabes", "Modal Soul", NUJABES, "16-bit / 44.1 kHz")
            if self.esperar(3):
                return
            self.cambiar(id_busca, estado="fallido", actualizado=probar.iso(0), intentos=3,
                         mensaje="ninguna fuente con todas las pistas")
            self.log(f"[fallido] {id_busca} ninguna fuente con todas las pistas")
            if self.esperar(3):
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
            # 24/96 pesa más. Nicotine+ apunta los tamaños al rato, no al empezar:
            # hasta entonces la Cola sólo puede enseñar bytes y velocidad.
            tamanos = {f"{n:02d} {t}.flac": 70_000_000 + n * 9_000_000
                       for n, t in enumerate(CASIOPEA, 1)}
            remota = "jazz\\CASIOPEA\\Mint Jams (1982) [FLAC]\\"
            apuntar = (5.0, lambda: self.apuntar(id_espera, "jazzfusion_jp", remota, tamanos))
            if not self.bajar(id_espera, tamanos, desde=1, apuntar_tras=apuntar):
                return
            self.terminar(id_espera, "CASIOPEA", "Mint Jams", CASIOPEA, "24-bit / 96 kHz")
            if self.esperar(10):
                return
            # y otra vez desde el principio, con la cola como al empezar
            self.esc = esc = montar()


def montar():
    esc = probar.escenario_falso(os.path.join(RAIZ, "estado"), "descargando")
    # El escenario deja pistas a medias que nunca crecen; aquí las baja el guion.
    for ruta in esc["_actual"]:
        os.remove(ruta)
    # Una que ya acabó, para el historial (la fallida ya la trae el escenario).
    with open(esc["_cola"], encoding="utf-8") as f:
        cola = json.load(f)
    hechas = [
        {"id": "20260904-083000-khruangbin-mordechai", "consulta": "Khruangbin - Mordechai",
         "tipo": "album", "estado": "listo", "creado": probar.iso(3900),
         "actualizado": probar.iso(3600), "intentos": 0, "candidato": None,
         "rechazados": [], "progreso": None, "mensaje": None,
         "resultado": {"carpeta": "Khruangbin/Mordechai", "artista": "Khruangbin",
                       "album": "Mordechai", "pistas": 10, "calidad": "24-bit / 48 kHz",
                       "parcial": False, "faltantes": []}},
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
