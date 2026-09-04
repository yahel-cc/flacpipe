#!/usr/bin/env python3
"""Banco de pruebas de la interfaz: lanza la TUI en un pty del tamaño que se
pida, manda teclas y guarda capturas de la rejilla lógica que vuelca
conductor.py (texto más un mapa de colores por celda).

    python3 pruebas/probar.py            # todo
    python3 pruebas/probar.py real       # tu cola de verdad, sólo lectura, 3 tamaños
    python3 pruebas/probar.py falso      # descarga simulada, búsqueda, espera, aviso
    python3 pruebas/probar.py fiio       # copia a una tarjeta de mentira

Las capturas quedan en pruebas/salida/capturas/<sesion>-<momento>.txt. Nada
de esto escribe en ~/Música: lo real se lee y lo simulado vive en
pruebas/salida/. Las flechas van en modo aplicación (ESC O A…), porque curses
activa `keypad` y la terminal manda eso, no ESC [ A.
"""
import fcntl
import json
import os
import pty
import select
import shutil
import signal
import struct
import sys
import termios
import threading
import time
from datetime import datetime, timedelta

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)
sys.path.insert(0, os.path.join(RAIZ, "flac_hunter"))
import fh_comun as comun  # noqa: E402

CONDUCTOR = os.path.join(AQUI, "conductor.py")
SALIDA = os.path.join(AQUI, "salida")          # ignorado por git
CAPTURAS = os.path.join(SALIDA, "capturas")
os.makedirs(CAPTURAS, exist_ok=True)

ARRIBA, ABAJO, DER, IZQ = "\x1bOA", "\x1bOB", "\x1bOC", "\x1bOD"


class Sesion:
    def __init__(self, nombre, cols, filas, escenario=None):
        self.nombre = nombre
        self.salida = os.path.join(SALIDA, "sesiones", nombre)
        shutil.rmtree(self.salida, ignore_errors=True)
        os.makedirs(self.salida)
        env = dict(os.environ, TERM="xterm-256color", SALIDA=self.salida,
                   ESCENARIO=json.dumps(escenario or {}))
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe(sys.executable, [sys.executable, CONDUCTOR], env)
        self.pid, self.fd = pid, fd
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", filas, cols, 0, 0))
        self._vivo = True
        self._basura = b""
        threading.Thread(target=self._drenar, daemon=True).start()

    def _drenar(self):
        while self._vivo:
            r, _w, _e = select.select([self.fd], [], [], 0.1)
            if r:
                try:
                    trozo = os.read(self.fd, 65536)
                except OSError:
                    break
                if not trozo:
                    break
                self._basura += trozo[-4000:]

    def teclas(self, *secuencias, pausa=0.15):
        for s in secuencias:
            os.write(self.fd, s.encode())
            time.sleep(pausa)

    def redimensionar(self, cols, filas):
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", filas, cols, 0, 0))
        os.kill(self.pid, signal.SIGWINCH)

    def capturar(self, etiqueta, espera=0.6):
        time.sleep(espera)
        origen = os.path.join(self.salida, "pantalla.txt")
        destino = os.path.join(CAPTURAS, f"{self.nombre}-{etiqueta}.txt")
        for _ in range(20):
            if os.path.exists(origen):
                break
            time.sleep(0.2)
        shutil.copy(origen, destino)
        return destino

    def cerrar(self):
        try:
            os.write(self.fd, b"q")
            time.sleep(0.4)
            os.write(self.fd, b"s")   # por si preguntó
        except OSError:
            pass
        self._vivo = False
        try:
            os.waitpid(self.pid, os.WNOHANG)
            time.sleep(0.3)
            os.kill(self.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass


# ─── escenarios ──────────────────────────────────────────────────────────────

def iso(delta_s):
    return (datetime.now() - timedelta(seconds=delta_s)).strftime("%Y-%m-%dT%H:%M:%S")


def escenario_falso(raiz, en_curso="descargando"):
    """Cola con una descarga en marcha, una búsqueda, una en espera y lo real."""
    shutil.rmtree(raiz, ignore_errors=True)
    pipeline = os.path.join(raiz, ".pipeline")
    descargas = os.path.join(raiz, ".descargas")
    incompletos = os.path.join(raiz, "incomplete")
    for d in (pipeline, descargas, incompletos):
        os.makedirs(d)

    real = comun.leer_cola()
    listas = [p for p in real["peticiones"] if p["estado"] in ("listo", "fallido")][-4:]

    id_baja = "20260904-090000-nujabes-modal-soul"
    id_busca = "20260904-090500-tom-misch-geography"
    id_espera = "20260904-090600-casiopea-mint-jams"
    baja = {
        "id": id_baja, "consulta": "Nujabes - Modal Soul", "tipo": "album",
        "artista": "Nujabes", "album": "Modal Soul", "estado": "descargando",
        "creado": iso(400), "actualizado": iso(35), "intentos": 0,
        "candidato": {"usuario": "shibuya_kei", "carpeta": "music\\Nujabes\\Modal Soul (2005) [FLAC]",
                      "puntos": 281, "detalle": "15 pistas, 16-bit / 44.1 kHz"},
        "rechazados": [["QuietCulchie", "shared\\Nujabes - Modal Soul"]],
        "progreso": {"terminados": 6, "total": 15, "bytes": 412_000_000},
        "resultado": None, "mensaje": "descargando de shibuya_kei",
    }
    busca = {
        "id": id_busca, "consulta": "Tom Misch - Geography", "tipo": "album",
        "artista": "Tom Misch", "album": "Geography", "estado": "buscando",
        "creado": iso(60), "actualizado": iso(23), "intentos": 0, "candidato": None,
        "rechazados": [], "progreso": None, "resultado": None, "mensaje": "buscando fuentes",
    }
    espera = {
        "id": id_espera, "consulta": "CASIOPEA - Mint Jams", "tipo": "album",
        "artista": "CASIOPEA", "album": "Mint Jams", "estado": "pendiente",
        "creado": iso(20), "actualizado": iso(20), "intentos": 0, "candidato": None,
        "rechazados": [], "progreso": None, "resultado": None, "mensaje": None,
    }
    if en_curso == "descargando":
        activas = [baja, espera]
    else:
        activas = [busca, espera]
    cola = {"version": 1, "peticiones": listas + activas}
    json.dump(cola, open(os.path.join(pipeline, "cola.json"), "w"), ensure_ascii=False, indent=1)

    try:
        log_real = open(comun.ARCHIVO_LOG, encoding="utf-8").read().splitlines()
    except OSError:
        log_real = []
    pistas = ["Feather", "Ordinary Joe", "Reflection Eternal", "Luv(sic) pt3", "Music Is Mine",
              "Eclipse", "The Sign", "Thank You", "World's End Rhapsody", "Sea of Cloud",
              "Horizon", "Modal Soul", "Flowers", "Light on the Land", "Perfect Circle"]
    lineas = list(log_real)
    hoy = iso(0)[:10]
    lineas += [
        f"{hoy}T09:00:00  [cola] + {id_baja}  (Nujabes - Modal Soul)",
        f"{hoy}T09:00:02  [buscar] {id_baja} token=1938700 término='Nujabes - Modal Soul flac'",
        f"{hoy}T09:00:47  [candidato]   281  shibuya_kei  15 pistas, 16-bit / 44.1 kHz  Modal Soul (2005) [FLAC]",
        f"{hoy}T09:00:47  [candidato]   274  ConFamServer  15 pistas, 16-bit / 44.1 kHz  Nujabes - Modal Soul",
        f"{hoy}T09:00:47  [candidato]   198  QuietCulchie  14 pistas, desconocida  Nujabes - Modal Soul",
        f"{hoy}T09:00:47  [candidato]   151  lofi_hermit  15 pistas, 16-bit / 44.1 kHz  2005 - Modal Soul",
        f"{hoy}T09:00:47  [descarte] QuietCulchie: le falta 1 pista",
        f"{hoy}T09:00:48  [descargar] {id_baja} 15 archivos de shibuya_kei → {descargas}/{id_baja}",
    ]
    carpeta = os.path.join(descargas, id_baja)
    os.makedirs(carpeta)
    for i, nombre in enumerate(pistas[:6], 1):
        archivo = f"{i:02d} {nombre}.flac"
        with open(os.path.join(carpeta, archivo), "wb") as f:
            f.write(b"\0" * (30_000_000 + i * 2_500_000))
        lineas.append(f"{hoy}T09:0{min(9, i)}:10  [archivo] {archivo}")
    lineas += [
        f"{hoy}T09:05:00  [cola] + {id_busca}  (Tom Misch - Geography)",
        f"{hoy}T09:06:00  [cola] + {id_espera}  (CASIOPEA - Mint Jams)",
    ]
    if en_curso != "descargando":
        lineas.append(f"{hoy}T09:06:05  [buscar] {id_busca} token=1938701 término='Tom Misch - Geography flac'")
    open(os.path.join(pipeline, "pipeline.log"), "w").write("\n".join(lineas) + "\n")

    actual = os.path.join(incompletos, "INCOMPLETE" + "ab" * 16 + "07 The Sign.flac")
    with open(actual, "wb") as f:
        f.write(b"\0" * 5_000_000)

    return {
        "comun": {"ARCHIVO_COLA": os.path.join(pipeline, "cola.json"),
                  "ARCHIVO_LOCK": os.path.join(pipeline, "cola.lock"),
                  "ARCHIVO_LOG": os.path.join(pipeline, "pipeline.log"),
                  "DIR_DESCARGAS": descargas},
        "tui": {"DIR_INCOMPLETOS": incompletos},
        "nicotine_vivo": True,
        "_actual": actual, "_cola": os.path.join(pipeline, "cola.json"),
        "_id_baja": id_baja, "_carpeta": carpeta,
    }


def crecer(archivo, parar, bytes_s=4_200_000):
    """Simula la descarga: el archivo incompleto engorda a ~4 MB/s con vaivén."""
    import math
    t0 = time.monotonic()
    while not parar.is_set():
        time.sleep(0.25)
        v = bytes_s * (1 + 0.35 * math.sin((time.monotonic() - t0) * 1.7))
        with open(archivo, "ab") as f:
            f.write(b"\0" * int(v * 0.25))


# ─── recorridos ──────────────────────────────────────────────────────────────

def real(cols, filas, etiqueta):
    s = Sesion(f"real-{etiqueta}", cols, filas)
    s.capturar("cola", espera=2.0)
    s.teclas(ABAJO, ABAJO, ABAJO)
    s.capturar("cola-4a", espera=0.8)
    s.teclas("\x1bOF")           # fin
    s.capturar("cola-ultima", espera=0.8)
    s.teclas("2")
    s.capturar("biblioteca", espera=2.5)
    s.teclas("3")
    s.capturar("fiio", espera=1.0)
    s.teclas("5")
    s.capturar("salud", espera=3.0)
    s.teclas("1", "?")
    s.capturar("ayuda", espera=0.6)
    s.cerrar()


def falso(cols, filas, etiqueta):
    raiz = os.path.join(SALIDA, "falso")
    esc = escenario_falso(raiz, "descargando")
    parar = threading.Event()
    threading.Thread(target=crecer, args=(esc["_actual"], parar), daemon=True).start()
    s = Sesion(f"falso-{etiqueta}", cols, filas, esc)
    s.capturar("bajando-1", espera=2.5)
    s.capturar("bajando-2", espera=2.0)
    s.teclas(ABAJO)
    s.capturar("pendiente", espera=0.8)
    s.teclas(ABAJO)
    s.capturar("lista", espera=0.8)
    # ahora la descarga termina: la petición pasa a listo → aviso + parpadeo
    cola = json.load(open(esc["_cola"]))
    for p in cola["peticiones"]:
        if p["id"] == esc["_id_baja"]:
            p["estado"] = "listo"
            p["actualizado"] = iso(0)
            p["progreso"] = {"terminados": 15, "total": 15}
            p["resultado"] = {"carpeta": "Nujabes/Modal Soul", "artista": "Nujabes",
                              "album": "Modal Soul", "pistas": 15,
                              "calidad": "16-bit / 44.1 kHz", "parcial": False, "faltantes": []}
    parar.set()
    json.dump(cola, open(esc["_cola"], "w"), ensure_ascii=False)
    with open(esc["comun"]["ARCHIVO_LOG"], "a") as f:
        f.write(f"{iso(0)}  [listo] {esc['_id_baja']} → Nujabes/Modal Soul (15 pistas, 16-bit / 44.1 kHz)\n")
    s.teclas("\x1bOH")           # inicio
    s.capturar("terminada-aviso", espera=0.9)
    s.capturar("terminada-parpadeo", espera=0.35)
    s.capturar("terminada-quieta", espera=3.0)
    s.cerrar()

    esc = escenario_falso(raiz, "buscando")
    s = Sesion(f"falso-{etiqueta}-busca", cols, filas, esc)
    s.capturar("buscando", espera=2.0)
    s.cerrar()


def fiio_prueba(cols, filas, etiqueta):
    s = Sesion(f"fiio-{etiqueta}", cols, filas, {"fiio_falso": True})
    s.teclas("3")
    s.capturar("plan", espera=1.5)
    s.teclas("\r")
    s.capturar("confirmar", espera=0.5)
    s.teclas("s")
    s.capturar("copiando-1", espera=1.5)
    s.capturar("copiando-2", espera=3.0)
    s.capturar("copiando-3", espera=4.0)
    s.cerrar()


if __name__ == "__main__":
    que = sys.argv[1] if len(sys.argv) > 1 else "todo"
    if que in ("real", "todo"):
        real(200, 50, "grande")
        real(105, 48, "media")
        real(78, 18, "chica")
    if que in ("falso", "todo"):
        falso(200, 50, "grande")
        falso(105, 48, "media")
        falso(78, 18, "chica")
    if que in ("fiio", "todo"):
        fiio_prueba(120, 40, "media")
    print("capturas en", CAPTURAS)
