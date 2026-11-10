#!/usr/bin/env python3
"""Banco de pruebas de la interfaz: lanza la TUI en un pty del tamaño que se
pida, manda teclas y guarda capturas de la rejilla lógica que vuelca
conductor.py (texto más un mapa de colores por celda).

    python3 pruebas/probar.py            # todo
    python3 pruebas/probar.py real       # tu cola de verdad, sólo lectura, 3 tamaños
    python3 pruebas/probar.py falso      # descarga simulada, búsqueda, espera, aviso
    python3 pruebas/probar.py fiio       # copia a una tarjeta de mentira
    python3 pruebas/probar.py importar   # importar playlists en csv desde la interfaz
    python3 pruebas/probar.py heredar    # pasar a la heredada en una biblioteca de mentira
    python3 pruebas/probar.py animar     # graba cada cuadro: descarga, ola, pestañas, ventana

Con `animar`, los cuadros quedan en pruebas/salida/sesiones/animar/cuadros/ y
pruebas/render.py los convierte en hojas o en un GIF.

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
FALLOS = []      # sesiones en las que la interfaz murió con una excepción


class Sesion:
    def __init__(self, nombre, cols, filas, escenario=None, grabar=False,
                 term="xterm-256color", programa=None):
        self.nombre = nombre
        self.salida = os.path.join(SALIDA, "sesiones", nombre)
        shutil.rmtree(self.salida, ignore_errors=True)
        os.makedirs(self.salida)
        env = dict(os.environ, TERM=term, SALIDA=self.salida,
                   ESCENARIO=json.dumps(escenario or {}), GRABAR="1" if grabar else "0")
        self.cuadros = os.path.join(self.salida, "cuadros")
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe(sys.executable, [sys.executable, programa or CONDUCTOR], env)
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
        origen_json = os.path.join(self.salida, "pantalla.json")
        if os.path.exists(origen_json):
            shutil.copy(origen_json, destino[:-4] + ".json")
        return destino

    def cerrar(self):
        if b"Traceback" in self._basura:
            # la interfaz murió dentro del pty: que no pase como una captura más
            texto = self._basura.decode("utf-8", "replace")
            FALLOS.append(self.nombre)
            print(f"\n!! {self.nombre}: la interfaz lanzó una excepción\n"
                  + texto[texto.rfind("Traceback"):][-1500:])
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
    """Cola con una descarga en marcha, una búsqueda, una en espera y lo real.

    Con `descargando`, además una búsqueda y una fallida, dos pistas a medio
    bajar a la vez (esc["_actual"] es la lista, para crecer()) y un
    downloads.json de mentira con el tamaño de cada pista de Nujabes salvo el
    de la 08, para que una pista tenga porcentaje y la otra no."""
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
    id_falla = "20260904-085500-hiatus-kaiyote-mood-valiant"
    falla = {
        "id": id_falla, "consulta": "Hiatus Kaiyote - Mood Valiant", "tipo": "album",
        "artista": "Hiatus Kaiyote", "album": "Mood Valiant", "estado": "fallido",
        "creado": iso(900), "actualizado": iso(500), "intentos": 3, "candidato": None,
        "rechazados": [["vinyl_rips", "music\\Hiatus Kaiyote\\Mood Valiant [MP3 320]"],
                       ["neosoul_crate", "share\\Hiatus Kaiyote - Mood Valiant"]],
        "progreso": None, "resultado": None, "mensaje": "nadie lo tiene en FLAC",
    }
    if en_curso == "descargando":
        activas = [falla, baja, busca, espera]
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

    # Dos pistas a medio bajar a la vez, como hace Nicotine+ con una carpeta.
    a_medias = [("ab" * 16, "07 The Sign.flac", 5_000_000)]
    if en_curso == "descargando":
        a_medias.append(("ef" * 16, "08 Thank You.flac", 2_000_000))
    actuales = []
    for md5, archivo, ya in a_medias:
        ruta = os.path.join(incompletos, "INCOMPLETE" + md5 + archivo)
        with open(ruta, "wb") as f:
            f.write(b"\0" * ya)
        actuales.append(ruta)

    # Lo que Nicotine+ guarda en downloads.json: [usuario, ruta remota, carpeta,
    # estado, tamaño, bytes, atributos]. Falta la 08 a propósito, y hay una
    # fila de otra descarga que no es de esta petición.
    remota = "music\\Nujabes\\Modal Soul (2005) [FLAC]\\"
    tamanos = {f"{i:02d} {nombre}.flac": 30_000_000 + i * 2_500_000
               for i, nombre in enumerate(pistas, 1)}
    filas = [["shibuya_kei", remota + archivo, carpeta,
              "Finished" if i <= 6 else "Transferring" if i <= 8 else "Queued",
              tam, tam if i <= 6 else None, {}]
             for i, (archivo, tam) in enumerate(tamanos.items(), 1)
             if not archivo.startswith("08 ")]
    filas.append(["otro", "x\\y\\Otra cosa.flac", os.path.join(raiz, "Downloads"),
                  "Finished", 12_345_678, 12_345_678, {}])
    transferencias = os.path.join(raiz, "downloads.json")
    with open(transferencias, "w", encoding="utf-8") as f:
        json.dump(filas, f, ensure_ascii=False)

    return {
        "comun": {"ARCHIVO_COLA": os.path.join(pipeline, "cola.json"),
                  "ARCHIVO_LOCK": os.path.join(pipeline, "cola.lock"),
                  "ARCHIVO_LOG": os.path.join(pipeline, "pipeline.log"),
                  "DIR_DESCARGAS": descargas},
        "tui": {"DIR_INCOMPLETOS": incompletos, "ARCHIVO_TRANSFERENCIAS": transferencias},
        "nicotine_vivo": True,
        "_actual": actuales, "_cola": os.path.join(pipeline, "cola.json"),
        "_id_baja": id_baja, "_carpeta": carpeta, "_tamanos": tamanos,
        "_transferencias": transferencias, "_remota": remota, "_id_falla": id_falla,
    }


def crecer(archivos, parar, bytes_s=4_200_000):
    """Simula la descarga: cada archivo incompleto (uno o una lista) engorda con
    vaivén, el primero a ~4 MB/s y cada siguiente a poco más de la mitad."""
    import math
    if isinstance(archivos, str):
        archivos = [archivos]
    t0 = time.monotonic()
    while not parar.is_set():
        time.sleep(0.25)
        fase = (time.monotonic() - t0) * 1.7
        for i, archivo in enumerate(archivos):
            v = bytes_s * 0.55 ** i * (1 + 0.35 * math.sin(fase + i))
            try:                 # sin crearlo: si ya no está, terminó
                fd = os.open(archivo, os.O_WRONLY | os.O_APPEND)
            except OSError:
                continue
            try:
                os.write(fd, b"\0" * int(v * 0.25))
            finally:
                os.close(fd)


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


def csv_de_muestra(carpeta):
    """Tres CSV como los que exportan los sitios, y uno que no sirve."""
    shutil.rmtree(carpeta, ignore_errors=True)
    os.makedirs(carpeta)
    rutas = {}

    rutas["raro"] = os.path.join(carpeta, "raro.csv")
    open(rutas["raro"], "w", encoding="utf-8").write("foo,bar\n1,2\n")
    time.sleep(0.05)

    rutas["mia"] = os.path.join(carpeta, "mia.csv")
    open(rutas["mia"], "w", encoding="utf-8").write(
        "Artista;Canción;Álbum\n"
        "CASIOPEA;Asayake;Mint Jams\n"
        "Masayoshi Takanaka;Blue Lagoon;An Insatiable High\n")
    time.sleep(0.05)

    rutas["tune"] = os.path.join(carpeta, "tunemymusic.csv")
    open(rutas["tune"], "w", encoding="utf-8").write(
        "Track name,Artist name,Album,Playlist name,Type,ISRC,Spotify - id\n"
        "Sum of My Cares,The Long Faces,Sum of My Cares,Domingo lento,Song,USX,1\n"
        "Modal Soul,Nujabes,Modal Soul,Domingo lento,Song,JPX,2\n"
        "Reflection Eternal,Nujabes,Modal Soul,Domingo lento,Song,JPX,3\n"
        "Lovers Rock,TV Girl,French Exit,Domingo lento,Song,USX,4\n")
    time.sleep(0.05)

    rutas["exportify"] = os.path.join(carpeta, "Mi playlist.csv")
    open(rutas["exportify"], "w", encoding="utf-8-sig").write(
        "Track URI,Track Name,Album Name,Artist Name(s),Release Date,Duration (ms),"
        "Popularity,Explicit,Added By,Added At,Genres,Record Label\n"
        "spotify:track:1,Cadence,Cadence,The Long Faces,2017-05-05,299000,40,false,u,t,indie,I\n"
        "spotify:track:2,Jean Skirt,Do That Again,Malcolm Todd,2026-04-04,59000,70,false,u,t,pop,C\n"
        "spotify:track:3,Feather (feat. Cise Starr & Akin),Modal Soul,Nujabes,2005-11-11,178000,60,false,u,t,jazz,H\n"
        "spotify:track:4,Karma Police - Remastered 2009,OK Computer,Radiohead,1997-05-21,264000,80,false,u,t,rock,X\n"
        "spotify:track:5,EARFQUAKE,IGOR,\"Tyler, The Creator\",2019-05-17,190000,85,true,u,t,hip hop,C\n"
        "spotify:track:6,South of the River,Geography,\"Tom Misch, Loyle Carner\",2018-04-13,240000,65,false,u,t,soul,B\n"
        "spotify:track:7,Cadence,Cadence,The Long Faces,2017-05-05,299000,40,false,u,t,indie,I\n"
        "spotify:track:8,,Un Álbum,Nadie,2020-01-01,1000,0,false,u,t,,\n")
    return rutas


def importar(cols, filas, etiqueta):
    raiz = os.path.join(SALIDA, "falso")
    esc = escenario_falso(raiz, "descargando")
    carpeta_csv = os.path.join(SALIDA, "csv")
    rutas = csv_de_muestra(carpeta_csv)
    esc["listas"] = {"DIRS_CSV": [carpeta_csv]}
    s = Sesion(f"importar-{etiqueta}", cols, filas, esc)
    s.teclas("i")
    s.capturar("selector", espera=1.0)
    s.teclas("\r")
    s.capturar("plan", espera=0.8)
    s.teclas("\t")
    s.capturar("plan-albumes", espera=0.8)
    s.teclas("\t", "r")
    s.capturar("plan-repetir", espera=0.8)
    s.teclas("r", "\r")
    s.capturar("encolado", espera=0.7)
    s.capturar("encolado-lista", espera=2.5)
    cola = json.load(open(esc["_cola"]))
    nuevas = [p for p in cola["peticiones"] if p.get("lista")]
    print(f"  encoladas de verdad: {len(nuevas)}  lista={nuevas[0]['lista'] if nuevas else None}")
    s.teclas("\x1bOF")                   # la última encolada: su ficha dice de qué lista vino
    s.capturar("ficha-lista", espera=0.6)
    s.teclas("i", "\x1bOF", "\r")        # el último de la lista: raro.csv
    s.capturar("error", espera=0.8)
    s.teclas("\x1b", "\x1bOA", "\r")    # tunemymusic.csv
    s.capturar("plan-tune", espera=0.8)
    s.cerrar()


def biblioteca_de_mentira(raiz, albumes, tam=40_000_000):
    """Crea raiz/Artista/Álbum/NN Título.flac. Son archivos dispersos: dicen
    pesar `tam` bytes (lo que suma el plan de copia) pero no ocupan disco."""
    shutil.rmtree(raiz, ignore_errors=True)
    for artista, album, titulos in albumes:
        carpeta = os.path.join(raiz, artista, album)
        os.makedirs(carpeta, exist_ok=True)
        for i, titulo in enumerate(titulos, 1):
            with open(os.path.join(carpeta, f"{i:02d} {titulo}.flac"), "wb") as f:
                f.write(b"fLaC")
                f.truncate(tam + i * 1_300_000)
    return raiz


def con_biblioteca(raiz, crear_sesion):
    """Lanza la sesión con FLACPIPE_BIBLIOTECA apuntando a `raiz`."""
    anterior = os.environ.get("FLACPIPE_BIBLIOTECA")
    os.environ["FLACPIPE_BIBLIOTECA"] = raiz
    try:
        return crear_sesion()
    finally:
        if anterior is None:
            os.environ.pop("FLACPIPE_BIBLIOTECA")
        else:
            os.environ["FLACPIPE_BIBLIOTECA"] = anterior


ALBUMES_DE_MUESTRA = [
    ("CASIOPEA", "Mint Jams", ["Take Me", "Asayake", "Midnight Rendezvous", "Time Limit",
                               "Domino Line", "Tears of the Star", "Swear"]),
    ("Nujabes", "Modal Soul", ["Feather", "Ordinary Joe", "Reflection Eternal",
                               "Luv(sic) pt3", "Music Is Mine", "Eclipse", "The Sign"]),
    ("Tom Misch", "Geography", ["Before Paris", "Lost in Paris", "South of the River",
                                "Movie", "Tick Tock", "It Runs Through Me"]),
]


def fiio_prueba(cols, filas, etiqueta):
    """Cuatro álbumes y una tarjeta con uno completo y otro con una pista menos:
    2 nuevos, 1 al día y 1 por arreglar."""
    albumes = [
        ("CASIOPEA", "Mint Jams", ["Take Me", "Asayake", "Midnight Rendezvous", "Time Limit"]),
        ("Nujabes", "Modal Soul", ["Feather", "Ordinary Joe", "Reflection Eternal",
                                   "Luv(sic) pt3", "Music Is Mine"]),
        ("Tom Misch", "Geography", ["Before Paris", "Lost in Paris", "South of the River",
                                    "Movie", "Tick Tock"]),
        ("Khruangbin", "Con Todo El Mundo", ["Cómo Me Quieres", "Evan Finds the Third Room",
                                             "Shades of Man", "A Hymn"]),
    ]
    raiz = biblioteca_de_mentira(os.path.join(SALIDA, "biblioteca-fiio"), albumes)
    tarjeta = os.path.join(SALIDA, "fiio_tarjeta")
    shutil.rmtree(tarjeta, ignore_errors=True)
    for (artista, album, _t), quitar in ((albumes[0], 0), (albumes[1], 1)):
        origen = os.path.join(raiz, artista, album)
        destino = os.path.join(tarjeta, "Artistas", artista, album)
        os.makedirs(destino)
        pistas = sorted(os.listdir(origen))
        for pista in pistas[:len(pistas) - quitar]:
            with open(os.path.join(destino, pista), "wb") as f:
                f.write(b"fLaC")
    s = con_biblioteca(raiz, lambda: Sesion(f"fiio-{etiqueta}", cols, filas,
                                            {"fiio_falso": True, "fiio_tarjeta": tarjeta}))
    s.teclas("3")
    s.capturar("plan", espera=1.5)
    s.teclas("\r")
    s.capturar("confirmar", espera=0.5)
    s.teclas("s")
    s.capturar("copiando-1", espera=1.5)
    s.capturar("copiando-2", espera=3.0)
    s.capturar("copiando-3", espera=4.0)
    s.cerrar()


def heredar(cols, filas, etiqueta):
    """Biblioteca de mentira en pruebas/salida: h sobre un artista y sobre un álbum."""
    raiz = os.path.join(SALIDA, "biblioteca-heredar")
    shutil.rmtree(raiz, ignore_errors=True)
    for rel in ("Art A/Disco 1/01 Uno.flac", "Art A/Disco 2/01 Dos.flac",
                "Art B/Solo/01 Tres.flac", "Art B/Otro/01 Cuatro.flac"):
        ruta = os.path.join(raiz, rel)
        os.makedirs(os.path.dirname(ruta), exist_ok=True)
        with open(ruta, "wb") as f:
            f.write(b"fLaC" + b"\0" * 4000)
    os.makedirs(os.path.join(raiz, "Artistas", "Viejo", "Disco"), exist_ok=True)
    anterior = os.environ.get("FLACPIPE_BIBLIOTECA")
    os.environ["FLACPIPE_BIBLIOTECA"] = raiz
    try:
        s = Sesion(f"heredar-{etiqueta}", cols, filas)
    finally:
        if anterior is None:
            os.environ.pop("FLACPIPE_BIBLIOTECA")
        else:
            os.environ["FLACPIPE_BIBLIOTECA"] = anterior
    s.teclas("2")
    s.capturar("biblioteca", espera=1.0)
    s.teclas("h")                      # cursor en artistas: Art A entero
    s.capturar("confirmar-artista", espera=0.4)
    s.teclas("s")
    s.capturar("movido-artista", espera=1.5)
    s.teclas(DER, "h")                 # Art B, sólo el álbum del cursor
    s.capturar("confirmar-album", espera=0.4)
    s.teclas("s")
    s.capturar("movido-album", espera=1.5)
    s.teclas("b")
    s.capturar("heredada", espera=1.0)
    s.cerrar()
    for dp, _ds, fs in sorted(os.walk(raiz)):
        for f in fs:
            print("   ", os.path.relpath(os.path.join(dp, f), raiz))


def terminar_descarga(esc):
    """La descarga simulada acaba: la petición pasa a lista con su resultado."""
    cola = json.load(open(esc["_cola"]))
    for p in cola["peticiones"]:
        if p["id"] == esc["_id_baja"]:
            p["estado"] = "listo"
            p["actualizado"] = iso(0)
            p["progreso"] = {"terminados": 15, "total": 15}
            p["resultado"] = {"carpeta": "Nujabes/Modal Soul", "artista": "Nujabes",
                              "album": "Modal Soul", "pistas": 15,
                              "calidad": "16-bit / 44.1 kHz", "parcial": False, "faltantes": []}
    json.dump(cola, open(esc["_cola"], "w"), ensure_ascii=False)
    with open(esc["comun"]["ARCHIVO_LOG"], "a") as f:
        f.write(f"{iso(0)}  [listo] {esc['_id_baja']} → Nujabes/Modal Soul "
                f"(15 pistas, 16-bit / 44.1 kHz)\n")


def animar(cols=120, filas=36):
    """Graba cada cuadro de un recorrido con todo lo que se mueve. Las marcas
    de tiempo quedan en marcas.json para elegir cuadros con render.py --entre."""
    raiz = os.path.join(SALIDA, "falso")
    esc = escenario_falso(raiz, "descargando")
    parar = threading.Event()
    threading.Thread(target=crecer, args=(esc["_actual"], parar), daemon=True).start()
    s = Sesion("animar", cols, filas, esc, grabar=True)
    marcas = {}
    t0 = time.monotonic()

    def marca(nombre):
        marcas[nombre] = round(time.monotonic() - t0, 2)

    time.sleep(3.0)
    marca("terminar")
    parar.set()
    terminar_descarga(esc)
    s.teclas("\x1bOH")
    time.sleep(3.0)
    marca("pestana-2")
    s.teclas("2", pausa=0)
    time.sleep(0.8)
    marca("pestana-5")
    s.teclas("5", pausa=0)
    time.sleep(1.2)
    marca("pestana-1")
    s.teclas("1", pausa=0)
    time.sleep(0.8)
    marca("ventana")
    s.teclas("a", pausa=0)
    time.sleep(0.9)
    s.teclas("Nujabes", pausa=0.05)
    time.sleep(0.6)
    s.teclas("\x1b", pausa=0.3)
    marca("fin")
    s.cerrar()
    with open(os.path.join(s.salida, "marcas.json"), "w") as f:
        json.dump(marcas, f, indent=1)
    print("marcas:", marcas)
    print("cuadros en", s.cuadros)


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
    if que in ("importar", "todo"):
        importar(120, 40, "media")
        importar(78, 20, "chica")
    if que in ("heredar", "todo"):
        heredar(120, 30, "media")
    if que == "animar":
        animar()
    print("capturas en", CAPTURAS)
    if FALLOS:
        print("FALLARON:", ", ".join(FALLOS))
        sys.exit(1)
