#!/usr/bin/env python3
"""Interfaz de terminal a pantalla completa para flacpipe.

Se dibuja a mano con `curses`, de la biblioteca estándar: flacpipe no tiene
ninguna dependencia externa y esto no la añade.

Cinco vistas (teclas 1-5) y una última fila que dice SIEMPRE qué hace cada
tecla en la vista en la que estás, para no tener que memorizar nada. La
navegación es con flechas; no hay atajos estilo vim a propósito.

Estructura:
    Datos          sondeo de cola.json, pipeline.log, montajes y descargas
    Trabajo        una tarea larga (copiar al FiiO, organizar) en un hilo
    Biblioteca     inventario de ~/Música + análisis perezoso por álbum
    Reproductor    escuchar una pista con el reproductor que haya instalado
    Vista*         las cinco pantallas
    Overlay*       las ventanas que se abren encima
    App            bucle, distribución y reparto de teclas
"""

import curses
import locale
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime

_DIR_BASE = os.path.dirname(os.path.realpath(__file__))
_DIR_PLUGIN = os.path.join(_DIR_BASE, "flac_hunter")
if _DIR_PLUGIN not in sys.path:
    sys.path.insert(0, _DIR_PLUGIN)

import fh_biblioteca as biblioteca   # noqa: E402
import fh_comun as comun             # noqa: E402
import fh_fiio as fiio               # noqa: E402


# ─── Glifos ───────────────────────────────────────────────────────────────────
# Todos ocupan una celda. Nada de emoji: un emoji mide dos columnas y descuadra
# la rejilla entera en cuanto una fuente no coincide con otra.

OCTAVOS = ("", "▏", "▎", "▍", "▌", "▋", "▊", "▉")
BLOQUE = "█"
VACIO = "─"
BRAILLE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
ALTURAS = "▁▂▃▄▅▆▇█"

GLIFO_ESTADO = {
    comun.PENDIENTE: "⧗",
    comun.BUSCANDO: "⟳",
    comun.DESCARGANDO: "↓",
    comun.LISTO: "✓",
    comun.FALLIDO: "✗",
    comun.CANCELADO: "⊘",
}

OK, MAL, AVISO = "✓", "✗", "!"


# ─── Colores ──────────────────────────────────────────────────────────────────

(NORMAL, AMBAR, VERDE, ROJO, AZUL, AMARILLO, VIOLETA, TENUE, BLANCO,
 SEL, INV, MARCA) = range(12)

_ATRIB = {}

COLOR_ESTADO = {
    comun.PENDIENTE: AMARILLO,
    comun.BUSCANDO: AZUL,
    comun.DESCARGANDO: AMBAR,
    comun.LISTO: VERDE,
    comun.FALLIDO: ROJO,
    comun.CANCELADO: TENUE,
}


def _iniciar_colores():
    for indice in range(12):
        _ATRIB[indice] = curses.A_NORMAL
    if not curses.has_colors():
        _ATRIB[BLANCO] = curses.A_BOLD
        _ATRIB[SEL] = curses.A_REVERSE
        _ATRIB[INV] = curses.A_REVERSE | curses.A_BOLD
        _ATRIB[MARCA] = curses.A_BOLD
        _ATRIB[TENUE] = curses.A_DIM
        return

    curses.start_color()
    try:
        curses.use_default_colors()
        fondo = -1
    except curses.error:
        fondo = curses.COLOR_BLACK

    muchos = curses.COLORS >= 256
    tinta = {
        AMBAR: 208 if muchos else curses.COLOR_YELLOW,
        VERDE: 114 if muchos else curses.COLOR_GREEN,
        ROJO: 203 if muchos else curses.COLOR_RED,
        AZUL: 75 if muchos else curses.COLOR_CYAN,
        AMARILLO: 179 if muchos else curses.COLOR_YELLOW,
        VIOLETA: 141 if muchos else curses.COLOR_MAGENTA,
        TENUE: 243 if muchos else curses.COLOR_BLUE,
        BLANCO: 255 if muchos else curses.COLOR_WHITE,
    }
    for indice, color in tinta.items():
        try:
            curses.init_pair(indice, color, fondo)
            _ATRIB[indice] = curses.color_pair(indice)
        except curses.error:
            pass
    _ATRIB[BLANCO] |= curses.A_BOLD
    if not muchos:
        _ATRIB[TENUE] |= curses.A_DIM

    parejas = {
        SEL: (255 if muchos else curses.COLOR_WHITE, 238 if muchos else curses.COLOR_BLUE),
        INV: (16 if muchos else curses.COLOR_BLACK, 208 if muchos else curses.COLOR_YELLOW),
        MARCA: (157 if muchos else curses.COLOR_GREEN, 22 if muchos else curses.COLOR_BLACK),
    }
    for indice, (frente, atras) in parejas.items():
        try:
            curses.init_pair(indice, frente, atras)
            _ATRIB[indice] = curses.color_pair(indice)
        except curses.error:
            _ATRIB[indice] = curses.A_REVERSE
    _ATRIB[INV] |= curses.A_BOLD


def at(color=NORMAL, negrita=False):
    return _ATRIB.get(color, curses.A_NORMAL) | (curses.A_BOLD if negrita else 0)


# ─── Dibujo ───────────────────────────────────────────────────────────────────

def escribir(win, y, x, texto, color=NORMAL, negrita=False, ancho=None):
    """Escribe recortando a la ventana. Devuelve la x siguiente."""
    if not texto:
        return x
    alto, cols = win.getmaxyx()
    if y < 0 or y >= alto or x < 0 or x >= cols:
        return x
    limite = cols - x
    if ancho is not None:
        limite = min(limite, ancho)
    texto = texto[:limite]
    if not texto:
        return x
    try:
        win.addstr(y, x, texto, at(color, negrita))
    except curses.error:
        pass  # la última celda de la pantalla siempre lanza; no es un error
    return x + len(texto)


def pintar(win, y, x, ancho, color):
    """Rellena de fondo, para una fila seleccionada."""
    escribir(win, y, x, " " * max(0, ancho), color)


def elipsis(texto, ancho):
    texto = texto or ""
    if ancho <= 0:
        return ""
    if len(texto) <= ancho:
        return texto
    if ancho == 1:
        return "…"
    return texto[:ancho - 1] + "…"


def rellenar(texto, ancho):
    return elipsis(texto, ancho).ljust(max(0, ancho))


def barra(win, y, x, ancho, frac, color=AMBAR, vacio=TENUE):
    """Barra con resolución de un octavo de celda."""
    if ancho <= 0:
        return x
    frac = max(0.0, min(1.0, frac))
    total = frac * ancho
    llenas = int(total)
    resto = int((total - llenas) * 8)
    lleno = BLOQUE * min(llenas, ancho)
    if resto and len(lleno) < ancho:
        lleno += OCTAVOS[resto]
    x = escribir(win, y, x, lleno, color)
    return escribir(win, y, x, VACIO * max(0, ancho - len(lleno)), vacio)


def girador(t=None):
    t = time.monotonic() if t is None else t
    return BRAILLE[int(t * 10) % len(BRAILLE)]


def chispa(muestras, ancho, techo=None):
    """Sparkline de las últimas `ancho` muestras. Devuelve exactamente `ancho`."""
    if ancho <= 0:
        return ""
    datos = list(muestras)[-ancho:]
    if not datos:
        return " " * ancho
    techo = techo or max(datos) or 1.0
    salida = "".join(
        ALTURAS[max(0, min(7, int(v / techo * 7 + 0.5)))] for v in datos)
    return salida.rjust(ancho)


def marquesina(texto, ancho, t=None):
    """Desplaza el texto si no cabe; si cabe, lo deja quieto."""
    texto = texto or ""
    if len(texto) <= ancho:
        return texto.ljust(max(0, ancho))
    t = time.monotonic() if t is None else t
    doble = texto + "   ·   "
    salto = int(t * 4) % len(doble)
    return (doble + doble)[salto:salto + ancho]


def duracion(segundos):
    segundos = int(segundos or 0)
    return f"{segundos // 60}:{segundos % 60:02d}"


def hora_de(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except (ValueError, TypeError):
        return ""


def desde(iso):
    """'hace 3 min' a partir de una marca ISO."""
    try:
        d = (datetime.now() - datetime.fromisoformat(iso)).total_seconds()
    except (ValueError, TypeError):
        return ""
    if d < 90:
        return f"hace {int(d)} s"
    if d < 5400:
        return f"hace {int(d // 60)} min"
    if d < 172800:
        return f"hace {int(d // 3600)} h"
    return f"hace {int(d // 86400)} días"


# ─── Velocímetro ──────────────────────────────────────────────────────────────

class Velocimetro:
    """Bytes/s a partir de un contador acumulado que se muestrea cada tanto."""

    def __init__(self, ventana=48, intervalo=0.5):
        self.historial = deque(maxlen=ventana)
        self.intervalo = intervalo
        self.actual = 0.0
        self._previo = None
        self._t = 0.0

    def muestra(self, acumulado):
        ahora = time.monotonic()
        if self._previo is None:
            self._previo, self._t = acumulado, ahora
            return
        transcurrido = ahora - self._t
        if transcurrido < self.intervalo:
            return
        self.actual = max(0, acumulado - self._previo) / transcurrido
        self.historial.append(self.actual)
        self._previo, self._t = acumulado, ahora

    def reiniciar(self):
        self.historial.clear()
        self.actual = 0.0
        self._previo = None
        self._t = 0.0


# ─── Datos ────────────────────────────────────────────────────────────────────

class Datos:
    """Sondeo del estado del pipeline en un hilo aparte.

    Todo lo caro (recorrer /proc, listar .descargas) vive aquí para que el
    bucle de dibujado no se detenga nunca.
    """

    def __init__(self):
        self.cola = {"version": 1, "peticiones": []}
        self.log = []
        self.pid_nicotine = None
        self.destinos = []
        self.sobras = []
        self.sueltos = []
        self.velocimetro = Velocimetro()
        self.aviso = ""

        self._mtime_cola = -1.0
        self._tam_log = -1
        self._ciclos = 0
        self._parar = threading.Event()
        self._hilo = None

    def arrancar(self):
        self._ciclo()
        self._hilo = threading.Thread(target=self._bucle, daemon=True)
        self._hilo.start()

    def cerrar(self):
        self._parar.set()

    def _bucle(self):
        while not self._parar.is_set():
            try:
                self._ciclo()
            except Exception as error:            # nunca tumbar la interfaz
                self.aviso = f"sondeo: {error}"
            self._parar.wait(0.25)

    def _ciclo(self):
        self._ciclos += 1

        try:
            mtime = os.path.getmtime(comun.ARCHIVO_COLA)
        except OSError:
            mtime = 0.0
        if mtime != self._mtime_cola:
            self._mtime_cola = mtime
            self.cola = comun.leer_cola()

        try:
            tam = os.path.getsize(comun.ARCHIVO_LOG)
        except OSError:
            tam = 0
        if tam != self._tam_log:
            self._tam_log = tam
            try:
                with open(comun.ARCHIVO_LOG, encoding="utf-8", errors="replace") as f:
                    self.log = [l.rstrip("\n") for l in f.readlines()[-800:]]
            except OSError:
                self.log = []

        if self._ciclos % 4 == 1:                # cada segundo
            self._mirar_descargas()
        if self._ciclos % 8 == 2:                # cada dos segundos
            self.destinos = fiio.detectar_destinos()
        if self._ciclos % 12 == 3:               # cada tres segundos
            self.pid_nicotine = comun.pid_nicotine()

    def _mirar_descargas(self):
        sobras, sueltos = [], []
        try:
            entradas = sorted(os.listdir(comun.DIR_DESCARGAS))
        except OSError:
            entradas = []
        for nombre in entradas:
            ruta = os.path.join(comun.DIR_DESCARGAS, nombre)
            if os.path.isdir(ruta):
                sobras.append(ruta)
            elif (nombre.lower().endswith(biblioteca.EXT_AUDIO)
                    and not nombre.startswith("._")):
                sueltos.append(ruta)
        self.sobras, self.sueltos = sobras, sueltos

        activa = self.peticion_en_curso()
        if activa is None:
            self.velocimetro.reiniciar()
            return
        carpeta = os.path.join(comun.DIR_DESCARGAS, activa["id"])
        total = 0
        for base, _dirs, archivos in os.walk(carpeta):
            for archivo in archivos:
                try:
                    total += os.path.getsize(os.path.join(base, archivo))
                except OSError:
                    pass
        self.velocimetro.muestra(total)

    # ── consultas ──
    def peticiones(self):
        return self.cola.get("peticiones", [])

    def activas(self):
        return [p for p in self.peticiones() if p["estado"] in comun.ACTIVOS]

    def peticion_en_curso(self):
        for pet in self.peticiones():
            if pet["estado"] in comun.EN_CURSO:
                return pet
        return None

    def nicotine_vivo(self):
        return self.pid_nicotine is not None


# ─── Trabajo en segundo plano ─────────────────────────────────────────────────

class Trabajo:
    """Una tarea larga (copiar al FiiO, organizar) corriendo en un hilo."""

    def __init__(self):
        self.titulo = ""
        self.clase = "copia"     # "copia" se pinta en la vista FiiO; el resto, en Cola
        self.lineas = deque(maxlen=400)
        self.cancelar = threading.Event()
        self.resumen = None
        self.terminado_en = 0.0
        self.hilo = None
        self.velocimetro = Velocimetro(intervalo=0.4)

        # progreso de una copia
        self.album = ""
        self.album_pistas = 0
        self.pista = ""
        self.pista_indice = 0
        self.pista_bytes = 0
        self.pista_hechos = 0
        self.total_bytes = 0
        self.hechos_bytes = 0
        self.orden = []          # pistas ya escritas, en orden
        self.orden_ok = None

    @property
    def activo(self):
        return self.hilo is not None and self.hilo.is_alive()

    def reciente(self, segundos=30):
        """Corriendo, o acabada hace poco: el resumen se queda un rato a la vista."""
        if self.activo:
            return True
        return (self.resumen is not None
                and time.monotonic() - self.terminado_en < segundos)

    def registrar(self, texto):
        self.lineas.append(texto)

    def lanzar(self, titulo, funcion, clase="copia"):
        if self.activo:
            return False
        self.titulo = titulo
        self.clase = clase
        self.lineas.clear()
        self.cancelar.clear()
        self.resumen = None
        self.terminado_en = 0.0
        self.album = self.pista = ""
        self.album_pistas = self.pista_indice = 0
        self.pista_bytes = self.pista_hechos = 0
        self.total_bytes = self.hechos_bytes = 0
        self.orden = []
        self.orden_ok = None
        self.velocimetro.reiniciar()
        self.hilo = threading.Thread(target=self._correr, args=(funcion,), daemon=True)
        self.hilo.start()
        return True

    def _correr(self, funcion):
        try:
            self.resumen = funcion(self)
        except Exception as error:
            self.registrar(f"{MAL} {error}")
            self.resumen = {"errores": [str(error)]}
        self.terminado_en = time.monotonic()

    def evento(self, ev):
        """Recibe los diccionarios que emite fiio.copiar()."""
        tipo = ev.get("t")
        if tipo == "album":
            self.album = ev["rel"]
            self.album_pistas = ev["pistas"]
            self.orden = []
            self.orden_ok = None
        elif tipo == "pista":
            self.pista = ev["archivo"]
            self.pista_indice = ev["indice"]
            self.pista_bytes = ev["bytes"]
            self.pista_hechos = 0
        elif tipo == "bytes":
            self.pista_hechos += ev["n"]
            self.hechos_bytes += ev["n"]
            self.velocimetro.muestra(self.hechos_bytes)
        elif tipo == "pista_ok":
            self.orden.append(ev["archivo"])
            self.pista_hechos = self.pista_bytes
        elif tipo == "orden":
            self.orden_ok = ev["ok"]


# ─── Biblioteca ───────────────────────────────────────────────────────────────

class Biblioteca:
    """Inventario de ~/Música, con análisis perezoso del álbum que estás mirando."""

    RAIZ_HEREDADA = os.path.join(comun.RAIZ_BIBLIOTECA, "Artistas")

    def __init__(self):
        self.raiz = comun.RAIZ_BIBLIOTECA
        self.artistas = []       # [{"nombre", "albumes": [item, ...]}]
        self.cargando = False
        self.cargada = False
        self.version = 0         # sube en cada recarga; las vistas se resitúan
        self.detalle = {}        # dir -> {"pistas", "huecos", "calidad", "mtime"}
        self._firma = None
        self._pendientes = deque()
        self._despierta = threading.Event()
        self._parar = threading.Event()
        self._candado = threading.Lock()
        threading.Thread(target=self._analizador, daemon=True).start()

    def cerrar(self):
        self._parar.set()
        self._despierta.set()

    @property
    def heredada(self):
        return self.raiz == self.RAIZ_HEREDADA

    def cambiar_raiz(self):
        self.raiz = comun.RAIZ_BIBLIOTECA if self.heredada else self.RAIZ_HEREDADA
        self.cargar()

    def cargar(self):
        if self.cargando:
            return
        self.cargando = True
        self.cargada = False
        threading.Thread(target=self._cargar, daemon=True).start()

    def _cargar(self):
        firma = self.firma()
        try:
            items = fiio.listar_biblioteca(raiz=self.raiz)
        except Exception:
            items = []
        artistas = []
        for item in items:
            if not artistas or artistas[-1]["nombre"] != item["artista"]:
                artistas.append({"nombre": item["artista"], "albumes": []})
            artistas[-1]["albumes"].append(item)
        self.artistas = artistas
        self._firma = firma
        self.version += 1
        self.cargada = True
        self.cargando = False

    def firma(self):
        """Huella barata de la biblioteca: la raíz y el mtime de cada artista.

        Con esto se detecta que el plugin acaba de organizar un álbum, tanto si
        crea una carpeta de artista nueva (cambia el mtime de la raíz) como si
        mete el álbum en una que ya existía (cambia el del artista).
        """
        try:
            partes = [self.raiz, os.path.getmtime(self.raiz)]
            with os.scandir(self.raiz) as entradas:
                for e in sorted(entradas, key=lambda e: e.name):
                    if (e.is_dir() and not e.name.startswith(".")
                            and e.name not in comun.NO_SON_ARTISTAS):
                        partes.append((e.name, e.stat().st_mtime))
        except OSError:
            return None
        return tuple(partes)

    def pedir_analisis(self, item):
        if item is None:
            return
        try:
            mtime = os.path.getmtime(item["dir"])
        except OSError:
            return
        guardado = self.detalle.get(item["dir"])
        if guardado is not None and guardado.get("mtime") == mtime:
            return
        with self._candado:
            if (item["dir"], mtime) not in self._pendientes:
                self._pendientes.append((item["dir"], mtime))
        self._despierta.set()

    def _analizador(self):
        vueltas = 0
        while not self._parar.is_set():
            self._despierta.wait(0.5)
            self._despierta.clear()
            while True:
                with self._candado:
                    if not self._pendientes:
                        break
                    carpeta, mtime = self._pendientes.popleft()
                guardado = self.detalle.get(carpeta)
                if guardado is not None and guardado.get("mtime") == mtime:
                    continue
                try:
                    self.detalle[carpeta] = self._analizar(carpeta, mtime)
                except Exception as error:
                    self.detalle[carpeta] = {"pistas": [], "huecos": [], "mtime": mtime,
                                             "calidad": f"no se pudo leer: {error}"}
            vueltas += 1
            if vueltas % 6 == 0:          # cada ~3 s
                self._vigilar()

    def _vigilar(self):
        """Recarga sola si algo cambió en el disco desde la última lectura."""
        if self.cargando or not self.cargada:
            return
        firma = self.firma()
        if firma is not None and firma != self._firma:
            self.cargar()

    @staticmethod
    def _analizar(carpeta, mtime):
        pistas, _avisos = biblioteca.analizar(carpeta)
        for p in pistas:
            p["duracion"] = biblioteca.leer_streaminfo(p["ruta"]).get("duracion", 0)
        faltan = sorted(n for lista in biblioteca.huecos(pistas).values() for n in lista)
        return {"pistas": pistas, "huecos": faltan, "mtime": mtime,
                "calidad": biblioteca.calidad(pistas)}


# ─── Reproductor ──────────────────────────────────────────────────────────────

REPRODUCTORES = (
    ("mpv", ["--no-video", "--really-quiet"]),
    ("gst-play-1.0", []),
    ("pw-play", []),
    ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "quiet"]),
    ("mplayer", ["-really-quiet"]),
)


class Reproductor:
    """Escuchar una pista sin salir de la interfaz, con lo que haya instalado."""

    def __init__(self):
        self.orden = None
        for nombre, extras in REPRODUCTORES:
            ruta = shutil.which(nombre)
            if ruta:
                self.orden = [ruta] + extras
                self.nombre = nombre
                break
        self.proceso = None
        self.titulo = ""
        self.duracion = 0.0
        self.inicio = 0.0

    @property
    def disponible(self):
        return self.orden is not None

    @property
    def sonando(self):
        if self.proceso is None:
            return False
        if self.proceso.poll() is not None:
            self.proceso = None
            return False
        return True

    def transcurrido(self):
        return time.monotonic() - self.inicio if self.sonando else 0.0

    def tocar(self, ruta, titulo=""):
        if not self.disponible:
            return "no hay ningún reproductor instalado (mpv, gst-play-1.0, pw-play…)"
        self.parar()
        try:
            self.proceso = subprocess.Popen(
                self.orden + [ruta],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as error:
            return f"no se pudo reproducir: {error}"
        self.titulo = titulo or os.path.basename(ruta)
        self.duracion = biblioteca.leer_streaminfo(ruta).get("duracion", 0) or 0
        self.inicio = time.monotonic()
        return None

    def parar(self):
        if self.proceso is None:
            return
        try:
            self.proceso.terminate()
            try:
                self.proceso.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.proceso.kill()
        except OSError:
            pass
        self.proceso = None
        self.titulo = ""


# ─── Vistas ───────────────────────────────────────────────────────────────────

class Vista:
    nombre = ""

    def entrar(self, app):
        pass

    def resumen(self, app):
        """Texto corto para la esquina de las pestañas: dónde estás mirando."""
        return ""

    def teclas(self, app):
        return []

    def dibujar(self, app, y0, alto, ancho):
        pass

    def tecla(self, app, k):
        return False


class VistaCola(Vista):
    nombre = "Cola"

    def __init__(self):
        self.cursor = 0
        self.desplazado = 0
        self.todo = False

    def lista(self, app):
        peticiones = app.datos.peticiones()
        if self.todo:
            return list(reversed(peticiones))
        activas = [p for p in peticiones if p["estado"] in comun.ACTIVOS]
        recientes = [p for p in peticiones if p["estado"] in comun.TERMINADOS][-8:]
        return activas + list(reversed(recientes))

    def seleccion(self, app):
        lista = self.lista(app)
        if not lista:
            return None
        self.cursor = max(0, min(self.cursor, len(lista) - 1))
        return lista[self.cursor]

    def teclas(self, app):
        return [("↑↓", "moverse"), ("a", "pedir"), ("x", "cancelar"),
                ("o", "organizar"), ("c", "copiar al FiiO"),
                ("t", "todo" if not self.todo else "sólo activas"),
                ("L", "limpiar")]

    def dibujar(self, app, y0, alto, ancho):
        win = app.pantalla
        # Organizar no es cosa del FiiO: su progreso se ve aquí, con la cola.
        if app.trabajo.clase != "copia" and app.trabajo.reciente():
            VistaFiio.panel_trabajo(app, y0, alto, ancho)
            return

        lista = self.lista(app)
        doble = ancho >= 78
        izq = max(30, min(46, int(ancho * 0.42))) if doble else ancho

        if not lista:
            escribir(win, y0 + 1, 2, "La cola está vacía.", TENUE)
            escribir(win, y0 + 3, 2, "Pulsa ", TENUE)
            escribir(win, y0 + 3, 8, "a", AMBAR, True)
            escribir(win, y0 + 3, 10, " para pedir un álbum: «Artista - Álbum».", TENUE)
            return

        self.cursor = max(0, min(self.cursor, len(lista) - 1))
        visibles = max(1, alto // 2)
        if self.cursor < self.desplazado:
            self.desplazado = self.cursor
        if self.cursor >= self.desplazado + visibles:
            self.desplazado = self.cursor - visibles + 1
        self.desplazado = max(0, min(self.desplazado, max(0, len(lista) - visibles)))

        for fila in range(visibles):
            indice = self.desplazado + fila
            if indice >= len(lista):
                break
            self._fila(app, lista[indice], y0 + fila * 2, izq,
                       indice == self.cursor)

        if doble:
            for y in range(y0, y0 + alto):
                escribir(win, y, izq, "│", TENUE)
            self._detalle(app, self.seleccion(app), y0, alto, izq + 2, ancho - izq - 3)

    def _fila(self, app, pet, y, ancho, activa):
        win = app.pantalla
        estado = pet["estado"]
        if activa:
            pintar(win, y, 0, ancho, SEL)
            pintar(win, y + 1, 0, ancho, SEL)
        glifo = (girador() if estado == comun.BUSCANDO
                 else GLIFO_ESTADO.get(estado, "·"))
        color = SEL if activa else COLOR_ESTADO.get(estado, NORMAL)
        escribir(win, y, 1, glifo, color)
        titulo = pet["consulta"]
        if activa:
            titulo = marquesina(titulo, ancho - 4)
        escribir(win, y, 3, elipsis(titulo, ancho - 4),
                 SEL if activa else (BLANCO if estado in comun.EN_CURSO else NORMAL))

        segunda, tinta = self._segunda_linea(app, pet)
        escribir(win, y + 1, 3, elipsis(segunda, ancho - 4), SEL if activa else tinta)

    def _segunda_linea(self, app, pet):
        estado = pet["estado"]
        if estado == comun.DESCARGANDO and pet.get("progreso"):
            p = pet["progreso"]
            hechos, total = p.get("terminados", 0), p.get("total", 0) or 1
            velocidad = app.datos.velocimetro.actual
            return (f"{hechos}/{total} pistas · "
                    f"{comun.formato_bytes(velocidad)}/s", AMBAR)
        if estado == comun.LISTO and pet.get("resultado"):
            r = pet["resultado"]
            faltan = r.get("faltantes") or []
            if faltan:
                return (f"{AVISO} falta " + ", ".join(f"{n:02d}" for n in faltan), AMARILLO)
            return (f"{r['pistas']} pistas · {r['calidad']}", TENUE)
        if pet.get("mensaje"):
            return (pet["mensaje"], ROJO if estado == comun.FALLIDO else TENUE)
        return (desde(pet.get("actualizado", "")), TENUE)

    def _detalle(self, app, pet, y0, alto, x, ancho):
        win = app.pantalla
        if pet is None or ancho < 20:
            return
        y = y0
        escribir(win, y, x, elipsis(pet["consulta"], ancho), BLANCO)
        y += 1
        escribir(win, y, x, "─" * ancho, TENUE)
        y += 1

        def campo(etiqueta, valor, color=NORMAL):
            nonlocal y
            if y >= y0 + alto or not valor:
                return
            escribir(win, y, x, rellenar(etiqueta, 10), TENUE)
            escribir(win, y, x + 10, elipsis(str(valor), ancho - 10), color)
            y += 1

        campo("estado", pet["estado"], COLOR_ESTADO.get(pet["estado"], NORMAL))
        campo("tipo", "canción suelta" if pet.get("tipo") == "cancion" else "álbum")
        campo("pedido", f"{hora_de(pet.get('creado',''))}  ({desde(pet.get('creado',''))})")

        candidato = pet.get("candidato") or {}
        if candidato:
            campo("fuente", f"{candidato.get('usuario','?')}"
                            f"   {candidato.get('puntos','?')} pts", AMBAR)
            campo("carpeta", os.path.basename(
                (candidato.get("carpeta") or "").replace("\\", "/")))
            campo("calidad", candidato.get("detalle", ""))
        if pet.get("rechazados"):
            campo("descartes", f"{len(pet['rechazados'])} fuente(s)", TENUE)

        progreso = pet.get("progreso") or {}
        if pet["estado"] == comun.DESCARGANDO and progreso:
            hechos = progreso.get("terminados", 0)
            total = progreso.get("total", 0) or 1
            y += 1
            if y < y0 + alto:
                escribir(win, y, x, rellenar("progreso", 10), TENUE)
                ancho_barra = max(8, ancho - 10 - 8)
                barra(win, y, x + 10, ancho_barra, hechos / total)
                escribir(win, y, x + 10 + ancho_barra + 1,
                         f"{int(hechos / total * 100):>3} %", AMBAR)
                y += 1
            velocidad = app.datos.velocimetro
            if y < y0 + alto:
                escribir(win, y, x, rellenar("velocidad", 10), TENUE)
                texto = f"{comun.formato_bytes(velocidad.actual)}/s"
                escribir(win, y, x + 10, rellenar(texto, 12), BLANCO)
                escribir(win, y, x + 22, chispa(velocidad.historial, ancho - 24), AZUL)
                y += 1

        resultado = pet.get("resultado") or {}
        if resultado:
            y += 1
            campo("destino", resultado.get("carpeta", ""), VERDE)
            campo("pistas", f"{resultado.get('pistas','?')} · "
                            f"{resultado.get('calidad','')}")
            if resultado.get("faltantes"):
                campo("faltan", ", ".join(f"{n:02d}" for n in resultado["faltantes"]),
                      AMARILLO)

        if pet.get("mensaje") and y < y0 + alto:
            y += 1
            escribir(win, y, x, elipsis(pet["mensaje"], ancho),
                     ROJO if pet["estado"] == comun.FALLIDO else TENUE)

    def tecla(self, app, k):
        lista = self.lista(app)
        if k == curses.KEY_UP:
            self.cursor = max(0, self.cursor - 1)
        elif k == curses.KEY_DOWN:
            self.cursor = min(max(0, len(lista) - 1), self.cursor + 1)
        elif k == curses.KEY_HOME:
            self.cursor = 0
        elif k == curses.KEY_END:
            self.cursor = max(0, len(lista) - 1)
        elif k == curses.KEY_NPAGE:
            self.cursor = min(max(0, len(lista) - 1), self.cursor + 5)
        elif k == curses.KEY_PPAGE:
            self.cursor = max(0, self.cursor - 5)
        elif k == "a":
            app.abrir(OverlayPedir())
        elif k == "x":
            app.cancelar_peticion(self.seleccion(app))
        elif k == "t":
            self.todo = not self.todo
            self.cursor = 0
        elif k == "L":
            app.limpiar_cola()
        elif k == "o":
            app.organizar()
        elif k == "c":
            app.ir_a(2)
        else:
            return False
        return True


class VistaBiblioteca(Vista):
    nombre = "Biblioteca"

    def __init__(self):
        self.columna = 0          # 0 artistas · 1 álbumes · 2 pistas
        self.cursor = [0, 0, 0]
        self.desplazado = [0, 0, 0]
        self.filtro = ""
        self._version = -1
        self._artista_previo = ""
        self._album_previo = ""

    def entrar(self, app):
        if not app.biblioteca.cargada and not app.biblioteca.cargando:
            app.biblioteca.cargar()

    def resumen(self, app):
        raiz = app.biblioteca.raiz
        corta = raiz.replace(os.path.expanduser("~"), "~")
        return f"heredada · {corta}" if app.biblioteca.heredada else corta

    def _resituar(self, app):
        """Tras una recarga automática, volver a donde estabas: por nombre, no por índice."""
        if app.biblioteca.version == self._version:
            return
        self._version = app.biblioteca.version
        if self._artista_previo:
            for i, a in enumerate(self.artistas(app)):
                if a["nombre"] == self._artista_previo:
                    self.cursor[0] = i
                    break
        if self._album_previo:
            artista = self.artista(app)
            if artista:
                for j, item in enumerate(artista["albumes"]):
                    if item["rel"] == self._album_previo:
                        self.cursor[1] = j
                        break

    # ── datos de cada columna ──
    def artistas(self, app):
        artistas = app.biblioteca.artistas
        if self.filtro:
            aguja = self.filtro.casefold()
            artistas = [a for a in artistas if aguja in a["nombre"].casefold()]
        return artistas

    def artista(self, app):
        artistas = self.artistas(app)
        if not artistas:
            return None
        self.cursor[0] = max(0, min(self.cursor[0], len(artistas) - 1))
        return artistas[self.cursor[0]]

    def album(self, app):
        artista = self.artista(app)
        if artista is None or not artista["albumes"]:
            return None
        self.cursor[1] = max(0, min(self.cursor[1], len(artista["albumes"]) - 1))
        return artista["albumes"][self.cursor[1]]

    def pistas(self, app):
        item = self.album(app)
        if item is None:
            return []
        detalle = app.biblioteca.detalle.get(item["dir"])
        if detalle is None:
            return [{"archivo": p, "titulo": os.path.splitext(p)[0],
                     "duracion": 0, "ruta": os.path.join(item["dir"], p)}
                    for p in item["pistas"]]
        return detalle["pistas"]

    def pista(self, app):
        pistas = self.pistas(app)
        if not pistas:
            return None
        self.cursor[2] = max(0, min(self.cursor[2], len(pistas) - 1))
        return pistas[self.cursor[2]]

    def teclas(self, app):
        pares = [("←→", "columna"), ("↑↓", "moverse"), ("␣", "marcar"),
                 ("c", "copiar"), ("p", "escuchar")]
        if app.reproductor.sonando:
            pares.append(("s", "parar"))
        pares += [("/", "buscar"), ("r", "recargar"),
                  ("b", "nueva" if app.biblioteca.heredada else "heredada")]
        return pares

    def dibujar(self, app, y0, alto, ancho):
        win = app.pantalla
        bib = app.biblioteca
        if bib.cargando or not bib.cargada:
            escribir(win, y0 + 1, 2, f"{girador()} leyendo la biblioteca…", AZUL)
            return
        if not bib.artistas:
            escribir(win, y0 + 1, 2, f"No hay ningún artista en {bib.raiz}", TENUE)
            return

        self._resituar(app)
        # el análisis del álbum seleccionado se pide en cuanto lo miras
        bib.pedir_analisis(self.album(app))
        artista_actual, album_actual = self.artista(app), self.album(app)
        self._artista_previo = artista_actual["nombre"] if artista_actual else ""
        self._album_previo = album_actual["rel"] if album_actual else ""

        anchos = self._anchos(ancho)
        x = 0
        for col in range(3):
            if col:
                for y in range(y0, y0 + alto):
                    escribir(win, y, x, "│", TENUE)
                x += 1
            self._columna(app, col, y0, alto, x, anchos[col])
            x += anchos[col]

        if self.filtro:
            escribir(win, y0 + alto - 1, 1,
                     elipsis(f"buscando «{self.filtro}»", ancho - 2), AMBAR)

    @staticmethod
    def _anchos(ancho):
        util = ancho - 2                       # dos separadores
        if ancho < 62:
            return [util, 0, 0]
        if ancho < 92:
            uno = max(16, int(util * 0.30))
            return [uno, util - uno, 0]
        uno = max(18, int(util * 0.24))
        dos = max(24, int(util * 0.36))
        return [uno, dos, util - uno - dos]

    def _columna(self, app, col, y0, alto, x, ancho):
        if ancho <= 0:
            return
        win = app.pantalla
        if col == 0:
            filas = [(a["nombre"], f"{len(a['albumes'])}", NORMAL, False)
                     for a in self.artistas(app)]
        elif col == 1:
            artista = self.artista(app)
            filas = []
            for item in (artista["albumes"] if artista else []):
                detalle = app.biblioteca.detalle.get(item["dir"])
                sufijo = f"{len(item['pistas'])}"
                tinta = NORMAL
                if detalle and detalle["huecos"]:
                    sufijo = AVISO + sufijo
                    tinta = AMARILLO
                filas.append((item["album"], sufijo, tinta, item["rel"] in app.marcados))
        else:
            filas = []
            for i, p in enumerate(self.pistas(app), 1):
                filas.append((f"{i:02d} {p.get('titulo') or p['archivo']}",
                              duracion(p.get("duracion")), NORMAL, False))

        visibles = alto
        if not filas:
            escribir(win, y0, x + 1, "(vacío)", TENUE, ancho=ancho - 1)
            return
        self.cursor[col] = max(0, min(self.cursor[col], len(filas) - 1))
        if self.cursor[col] < self.desplazado[col]:
            self.desplazado[col] = self.cursor[col]
        if self.cursor[col] >= self.desplazado[col] + visibles:
            self.desplazado[col] = self.cursor[col] - visibles + 1
        self.desplazado[col] = max(0, min(self.desplazado[col],
                                          max(0, len(filas) - visibles)))

        for fila in range(visibles):
            indice = self.desplazado[col] + fila
            if indice >= len(filas):
                break
            texto, sufijo, tinta, marcado = filas[indice]
            y = y0 + fila
            elegido = indice == self.cursor[col]
            enfocado = elegido and col == self.columna
            if elegido:
                pintar(win, y, x, ancho, SEL if enfocado else NORMAL)
            if marcado:
                escribir(win, y, x, "▸", SEL if enfocado else VERDE)
            hueco = ancho - 2 - len(sufijo) - 1
            escribir(win, y, x + 1, elipsis(texto, hueco),
                     SEL if enfocado else (BLANCO if elegido else tinta))
            escribir(win, y, x + ancho - len(sufijo) - 1, sufijo,
                     SEL if enfocado else TENUE)

    def tecla(self, app, k):
        col = self.columna
        if k == curses.KEY_LEFT:
            self.columna = max(0, col - 1)
        elif k == curses.KEY_RIGHT:
            self.columna = min(2, col + 1)
        elif k == curses.KEY_UP:
            self.cursor[col] = max(0, self.cursor[col] - 1)
            self._reiniciar_hijas(col)
        elif k == curses.KEY_DOWN:
            self.cursor[col] += 1
            self._reiniciar_hijas(col)
        elif k == curses.KEY_PPAGE:
            self.cursor[col] = max(0, self.cursor[col] - 10)
            self._reiniciar_hijas(col)
        elif k == curses.KEY_NPAGE:
            self.cursor[col] += 10
            self._reiniciar_hijas(col)
        elif k == curses.KEY_HOME:
            self.cursor[col] = 0
            self._reiniciar_hijas(col)
        elif k == " ":
            item = self.album(app)
            if item:
                if item["rel"] in app.marcados:
                    app.marcados.discard(item["rel"])
                    app.avisar(f"desmarcado {item['rel']}")
                else:
                    app.marcados.add(item["rel"])
                    app.avisar(f"marcado {item['rel']}", VERDE)
        elif k == "c":
            item = self.album(app)
            if not app.marcados and item:
                app.marcados.add(item["rel"])
            app.ir_a(2)
        elif k == "p":
            self._escuchar(app)
        elif k == "s":
            app.reproductor.parar()
        elif k == "/":
            app.abrir(OverlayBuscar(self))
        elif k == "r":
            app.biblioteca.cargar()
            app.avisar("releyendo la biblioteca…", AZUL, 2)
        elif k == "b":
            app.biblioteca.cambiar_raiz()
            self.cursor = [0, 0, 0]
            self.desplazado = [0, 0, 0]
            self._artista_previo = self._album_previo = ""
        elif k == "o":
            app.organizar()
        else:
            return False
        return True

    def _reiniciar_hijas(self, col):
        for hija in range(col + 1, 3):
            self.cursor[hija] = 0
            self.desplazado[hija] = 0

    def _escuchar(self, app):
        pista = self.pista(app)
        if pista is None:
            return
        titulo = pista.get("titulo") or pista["archivo"]
        error = app.reproductor.tocar(pista["ruta"], titulo)
        if error:
            app.avisar(error, ROJO)
        else:
            app.avisar(f"sonando {titulo}", VERDE)


class VistaFiio(Vista):
    nombre = "FiiO"

    def __init__(self):
        self.plan = []
        self.plan_para = None
        self.calculando = False
        self.destino = 0
        self.desplazado = 0

    def entrar(self, app):
        self.calcular(app)

    def montaje(self, app):
        destinos = app.datos.destinos
        if not destinos:
            return None
        self.destino = max(0, min(self.destino, len(destinos) - 1))
        return destinos[self.destino]

    def calcular(self, app):
        if self.calculando:
            return
        self.calculando = True
        filtros = sorted(app.marcados) or None
        raiz = app.biblioteca.raiz

        def trabajo():
            try:
                self.plan = fiio.listar_biblioteca(raiz=raiz, filtros=filtros)
            except Exception:
                self.plan = []
            self.plan_para = (raiz, tuple(filtros or ()))
            self.calculando = False

        threading.Thread(target=trabajo, daemon=True).start()

    def teclas(self, app):
        if app.trabajo.activo:
            return [("x", "abortar")]
        return [("↵", "copiar"), ("n", "simular"), ("w", "rehacer"),
                ("m", "quitar marcas"), ("u", "expulsar"), ("↑↓", "tarjeta")]

    def dibujar(self, app, y0, alto, ancho):
        win = app.pantalla
        if app.trabajo.clase == "copia" and app.trabajo.reciente():
            self.panel_trabajo(app, y0, alto, ancho)
            return

        destino = self.montaje(app)
        y = y0
        if destino is None:
            escribir(win, y, 2, "No hay ninguna tarjeta montada.", AMARILLO)
            escribir(win, y + 2, 2,
                     "Conecta el FiiO o el lector de la microSD; aparece sola.", TENUE)
            escribir(win, y + 3, 2,
                     "Se buscan sistemas FAT/exFAT en /run/media, /media y /mnt.", TENUE)
            y += 5
        else:
            for i, d in enumerate(app.datos.destinos):
                elegido = i == self.destino
                if elegido:
                    pintar(win, y, 0, ancho, SEL)
                escribir(win, y, 1, "▸" if elegido else " ", SEL if elegido else TENUE)
                escribir(win, y, 3, elipsis(d["montaje"], ancho - 34),
                         SEL if elegido else NORMAL)
                cola = (f"{d['tipo']}  {comun.formato_bytes(d['libre'])} libres "
                        f"de {comun.formato_bytes(d['total'])}")
                escribir(win, y, max(4, ancho - len(cola) - 2), cola,
                         SEL if elegido else TENUE)
                y += 1
            y += 1
            if destino["tipo"] not in ("vfat", "exfat", "msdos"):
                escribir(win, y, 2, f"{AVISO} es {destino['tipo']}: no puedo verificar "
                                    f"el orden físico de las entradas", AMARILLO)
                y += 2

        bytes_plan = sum(a["bytes"] for a in self.plan)
        pistas = sum(len(a["pistas"]) for a in self.plan)
        origen = "marcados" if app.marcados else "toda la biblioteca"
        escribir(win, y, 2, rellenar("origen", 10), TENUE)
        escribir(win, y, 12, elipsis(app.biblioteca.raiz, ancho - 14))
        y += 1
        escribir(win, y, 2, rellenar("plan", 10), TENUE)
        if self.calculando:
            escribir(win, y, 12, f"{girador()} calculando…", AZUL)
        else:
            escribir(win, y, 12, f"{len(self.plan)} álbumes · {pistas} pistas · "
                                 f"{comun.formato_bytes(bytes_plan)}  ({origen})", BLANCO)
        y += 1
        if destino:
            escribir(win, y, 2, rellenar("destino", 10), TENUE)
            escribir(win, y, 12, elipsis(os.path.join(
                destino["montaje"], fiio.DESTINO_REL_POR_DEFECTO), ancho - 14))
            y += 1
            libre = destino["libre"]
            escribir(win, y, 2, rellenar("espacio", 10), TENUE)
            ancho_barra = max(10, min(40, ancho - 34))
            usado = 1 - (libre / destino["total"]) if destino["total"] else 0
            barra(win, y, 12, ancho_barra, usado, VERDE if libre > bytes_plan else ROJO)
            escribir(win, y, 12 + ancho_barra + 1,
                     f"{comun.formato_bytes(libre)} libres", TENUE)
            y += 1
            if bytes_plan > libre:
                escribir(win, y, 2, f"{MAL} no cabe: faltan "
                                    f"{comun.formato_bytes(bytes_plan - libre)}", ROJO)
                y += 1
        y += 1

        alto_lista = y0 + alto - y - 1
        if alto_lista > 1 and self.plan:
            escribir(win, y, 2, "se copiará en este orden", TENUE)
            y += 1
            for i, item in enumerate(self.plan[self.desplazado:
                                               self.desplazado + alto_lista - 1]):
                escribir(win, y + i, 3, f"{self.desplazado + i + 1:>3}. ", TENUE)
                escribir(win, y + i, 8, elipsis(item["rel"], ancho - 30))
                cola = f"{len(item['pistas'])} pistas  {comun.formato_bytes(item['bytes'])}"
                escribir(win, y + i, max(9, ancho - len(cola) - 2), cola, TENUE)
            sobran = len(self.plan) - self.desplazado - (alto_lista - 1)
            if sobran > 0:
                escribir(win, y0 + alto - 1, 3, f"… y {sobran} más", TENUE)

    @staticmethod
    def panel_trabajo(app, y0, alto, ancho):
        """Progreso de una tarea larga. Lo usan la vista FiiO y la de Cola."""
        win = app.pantalla
        trabajo = app.trabajo
        y = y0
        activo = trabajo.activo
        escribir(win, y, 2, trabajo.titulo, BLANCO)
        if activo:
            escribir(win, y, 2 + len(trabajo.titulo) + 1, girador(), AZUL)
        y += 2

        if trabajo.total_bytes:
            frac = min(1.0, trabajo.hechos_bytes / trabajo.total_bytes)
            escribir(win, y, 2, rellenar("total", 10), TENUE)
            ancho_barra = max(10, ancho - 34)
            barra(win, y, 12, ancho_barra, frac)
            escribir(win, y, 12 + ancho_barra + 1, f"{int(frac * 100):>3} %", AMBAR)
            y += 1
            escribir(win, y, 12,
                     f"{comun.formato_bytes(trabajo.hechos_bytes)} de "
                     f"{comun.formato_bytes(trabajo.total_bytes)}", TENUE)
            velocidad = f"{comun.formato_bytes(trabajo.velocimetro.actual)}/s"
            escribir(win, y, max(13, ancho - len(velocidad) - 22), velocidad, BLANCO)
            escribir(win, y, max(14, ancho - 20), chispa(trabajo.velocimetro.historial, 18),
                     AZUL)
            y += 2

        if trabajo.album:
            escribir(win, y, 2, rellenar("álbum", 10), TENUE)
            escribir(win, y, 12, elipsis(trabajo.album, ancho - 14), BLANCO)
            y += 1
        if trabajo.pista:
            escribir(win, y, 2, rellenar("pista", 10), TENUE)
            escribir(win, y, 12, elipsis(
                f"{trabajo.pista_indice}/{trabajo.album_pistas}  {trabajo.pista}",
                ancho - 14))
            y += 1
            if trabajo.pista_bytes:
                frac = min(1.0, trabajo.pista_hechos / trabajo.pista_bytes)
                barra(win, y, 12, max(10, min(40, ancho - 20)), frac, AMBAR)
                y += 1
        if trabajo.album_pistas:
            escribir(win, y, 2, rellenar("orden", 10), TENUE)
            x = 12
            for i in range(trabajo.album_pistas):
                if x + 3 > ancho - 1:
                    break
                hecho = i < len(trabajo.orden)
                x = escribir(win, y, x, f"{i + 1:02d} " if hecho else "·· ",
                             VERDE if hecho else TENUE)
            if trabajo.orden_ok is False:
                escribir(win, y + 1, 12, f"{AVISO} el orden físico no quedó como se "
                                         f"esperaba; hará falta --rehacer", AMARILLO)
            y += 2

        y += 1
        alto_log = y0 + alto - y
        if alto_log > 0:
            escribir(win, y - 1, 2, "─" * max(0, ancho - 4), TENUE)
            lineas = list(trabajo.lineas)[-alto_log:]
            for i, linea in enumerate(lineas):
                tinta = TENUE
                if linea.strip().startswith(MAL):
                    tinta = ROJO
                elif linea.strip().startswith(AVISO):
                    tinta = AMARILLO
                elif linea.strip().startswith(OK):
                    tinta = VERDE
                elif linea.strip().startswith("▸"):
                    tinta = BLANCO
                escribir(win, y + i, 2, elipsis(linea, ancho - 4), tinta)

        if not activo and trabajo.resumen is not None:
            resumen = trabajo.resumen
            texto = (f"{resumen.get('copiados', 0)} álbumes, "
                     f"{resumen.get('archivos', 0)} archivos, "
                     f"{comun.formato_bytes(resumen.get('bytes', 0))}")
            escribir(win, y0 + alto - 1, 2, elipsis(texto, ancho - 4), VERDE)

    def tecla(self, app, k):
        if app.trabajo.activo:
            if k == "x":
                app.trabajo.cancelar.set()
                app.avisar("abortando tras la pista en curso…", AMARILLO)
                return True
            return False

        if k == curses.KEY_UP:
            self.destino = max(0, self.destino - 1)
        elif k == curses.KEY_DOWN:
            self.destino = min(max(0, len(app.datos.destinos) - 1), self.destino + 1)
        elif k in ("\n", "\r", curses.KEY_ENTER, "c"):
            self._pedir_copia(app, rehacer=False)
        elif k == "w":
            self._pedir_copia(app, rehacer=True)
        elif k == "n":
            app.copiar_fiio(self.montaje(app), self.plan, simular=True, rehacer=False)
        elif k == "m":
            app.marcados.clear()
            self.calcular(app)
            app.avisar("marcas quitadas")
        elif k == "u":
            app.expulsar(self.montaje(app))
        elif k == "r":
            self.calcular(app)
        else:
            return False
        return True

    def _pedir_copia(self, app, rehacer):
        destino = self.montaje(app)
        if destino is None:
            app.avisar("no hay ninguna tarjeta montada", ROJO)
            return
        if not self.plan:
            app.avisar("no hay nada que copiar", AMARILLO)
            return
        bytes_plan = sum(a["bytes"] for a in self.plan)
        if bytes_plan > destino["libre"]:
            app.avisar(f"no cabe: faltan "
                       f"{comun.formato_bytes(bytes_plan - destino['libre'])}", ROJO)
            return
        pregunta = (f"¿Copiar {len(self.plan)} álbumes "
                    f"({comun.formato_bytes(bytes_plan)}) a {destino['montaje']}?")
        if rehacer:
            pregunta += "  Se BORRAN y reescriben los que ya estén."
        app.abrir(OverlayConfirmar(
            pregunta,
            lambda: app.copiar_fiio(destino, self.plan, simular=False, rehacer=rehacer)))


class VistaLog(Vista):
    nombre = "Log"

    ETIQUETAS = {
        "candidato": VIOLETA, "descargar": AMBAR, "archivo": TENUE,
        "listo": VERDE, "buscar": AZUL, "cola": AZUL, "plugin": VIOLETA,
        "reintento": AMARILLO, "error": ROJO, "descartado": TENUE,
    }
    PATRON = re.compile(r"^(\S+)\s+\[([^\]]+)\]\s*(.*)$")

    def __init__(self):
        self.desplazado = 0
        self.seguir = True
        self.filtro = ""

    def lineas(self, app):
        lineas = app.datos.log
        if self.filtro:
            aguja = self.filtro.casefold()
            lineas = [l for l in lineas if aguja in l.casefold()]
        return lineas

    def teclas(self, app):
        return [("↑↓", "mover"), ("f", "seguir" if not self.seguir else "fijar"),
                ("/", "filtrar"), ("inicio/fin", "extremos")]

    def dibujar(self, app, y0, alto, ancho):
        win = app.pantalla
        lineas = self.lineas(app)
        if not lineas:
            escribir(win, y0 + 1, 2,
                     "El log está vacío." if not self.filtro
                     else f"Nada coincide con «{self.filtro}».", TENUE)
            return
        if self.seguir:
            self.desplazado = max(0, len(lineas) - alto)
        self.desplazado = max(0, min(self.desplazado, max(0, len(lineas) - alto)))

        for i in range(alto):
            indice = self.desplazado + i
            if indice >= len(lineas):
                break
            self._linea(win, y0 + i, ancho, lineas[indice],
                        indice == len(lineas) - 1)

    def _linea(self, win, y, ancho, linea, ultima):
        coincide = self.PATRON.match(linea)
        if not coincide:
            escribir(win, y, 1, elipsis(linea, ancho - 2), TENUE)
            return
        marca, etiqueta, resto = coincide.groups()
        escribir(win, y, 1, marca[11:19] if len(marca) > 18 else marca, TENUE)
        x = 11
        escribir(win, y, x, rellenar(f"[{etiqueta}]", 12),
                 self.ETIQUETAS.get(etiqueta, NORMAL))
        escribir(win, y, x + 13, elipsis(resto, ancho - x - 14),
                 BLANCO if ultima else NORMAL)

    def tecla(self, app, k):
        if k == curses.KEY_UP:
            self.desplazado = max(0, self.desplazado - 1)
            self.seguir = False
        elif k == curses.KEY_DOWN:
            self.desplazado += 1
            self.seguir = False
        elif k == curses.KEY_PPAGE:
            self.desplazado = max(0, self.desplazado - 10)
            self.seguir = False
        elif k == curses.KEY_NPAGE:
            self.desplazado += 10
            self.seguir = False
        elif k == curses.KEY_HOME:
            self.desplazado = 0
            self.seguir = False
        elif k == curses.KEY_END:
            self.seguir = True
        elif k == "f":
            self.seguir = not self.seguir
        elif k == "/":
            app.abrir(OverlayBuscar(self))
        else:
            return False
        return True


class VistaSalud(Vista):
    nombre = "Salud"

    def __init__(self):
        self.resultados = []
        self.revisando = False
        self.hecho_en = 0.0

    def entrar(self, app):
        if not self.resultados and not self.revisando:
            self.revisar(app)

    def teclas(self, app):
        return [("R", "revisar"), ("o", "organizar"), ("p", "volver a pedir"),
                ("n", "abrir Nicotine+"), ("3", "ir al FiiO")]

    def revisar(self, app):
        if self.revisando:
            return
        self.revisando = True
        self.resultados = []
        threading.Thread(target=self._revisar, args=(app,), daemon=True).start()

    def _revisar(self, app):
        salida = []

        def anotar(marca, texto, detalle="", accion=""):
            salida.append((marca, texto, detalle, accion))
            self.resultados = list(salida)

        anotar("titulo", "Instalación")
        destino = (os.path.realpath(comun.ENLACE_PLUGIN)
                   if os.path.exists(comun.ENLACE_PLUGIN) else None)
        if destino and os.path.isfile(os.path.join(destino, "__init__.py")):
            anotar(OK, "el plugin está enlazado en el snap", destino)
        else:
            anotar(MAL, "el plugin NO está instalado", "ejecuta: bash instalar.sh")

        try:
            texto = open(comun.CONFIG_NICOTINE, encoding="utf-8").read()
            habilitado = "'flac_hunter'" in texto
            descargas_ok = f"downloaddir = {comun.DIR_DESCARGAS}" in texto
        except OSError:
            habilitado = descargas_ok = False
        anotar(OK if habilitado else MAL,
               "está habilitado en la config de Nicotine+" if habilitado
               else "NO está habilitado en la config de Nicotine+")
        anotar(OK if descargas_ok else AVISO,
               "downloaddir apunta a .descargas" if descargas_ok
               else "downloaddir NO apunta a .descargas")

        for carpeta in (comun.DIR_PIPELINE, comun.DIR_DESCARGAS, comun.RAIZ_BIBLIOTECA):
            anotar(OK if os.access(carpeta, os.W_OK) else MAL, f"{carpeta} escribible")

        anotar("titulo", "Nicotine+")
        pid = app.datos.pid_nicotine
        if pid is None:
            anotar(MAL, "Nicotine+ NO está corriendo", "pulsa n para abrirlo", "n")
        else:
            anotar(OK, f"Nicotine+ corriendo (pid {pid})")
            try:
                arranque = os.path.getmtime(f"/proc/{pid}")
            except OSError:
                arranque = 0
            marca = comun.ultimo_arranque_plugin()
            if marca is None:
                anotar(MAL, "el plugin nunca se ha cargado",
                       "no hay '[plugin] activado' en pipeline.log")
            elif marca >= arranque - 5:
                cuando = datetime.fromtimestamp(marca).strftime("%H:%M:%S")
                anotar(OK, f"el plugin se cargó en esta sesión ({cuando})")
            else:
                anotar(MAL, "el plugin no se cargó en esta sesión de Nicotine+",
                       "mira el log de Nicotine+ por si dio error al cargar")

        alcanza = comun.snap_lee_plugin()
        if alcanza is True:
            anotar(OK, "el snap confinado alcanza el código del plugin")
        elif alcanza is False:
            anotar(MAL, "el snap NO puede leer el plugin (AppArmor)",
                   "mueve el código a una ruta sin punto inicial dentro de $HOME")
        else:
            anotar(AVISO, "no pude comprobar el acceso del snap")

        anotar("titulo", "Cola y descargas")
        activas = len(app.datos.activas())
        anotar(OK, f"{activas} petición(es) activas, "
                   f"{len(app.datos.peticiones())} en total")
        pendientes = len(app.datos.sobras) + len(app.datos.sueltos)
        if pendientes:
            anotar(AVISO, f"{pendientes} elemento(s) sin organizar en .descargas",
                   "pulsa o para organizarlos", "o")

        anotar("titulo", "Álbumes incompletos")
        incompletos = []
        for item in fiio.listar_biblioteca():
            try:
                pistas, _ = biblioteca.analizar(item["dir"])
            except Exception:
                continue
            faltan = sorted(n for l in biblioteca.huecos(pistas).values() for n in l)
            if faltan:
                incompletos.append((item, faltan))
                anotar(AVISO, f"{item['rel']} — faltan "
                              + ", ".join(f"{n:02d}" for n in faltan),
                       "pulsa p para volver a pedirlo", "p:" + item["rel"])
        if not incompletos:
            anotar(OK, "ningún álbum tiene huecos en la numeración")

        anotar("titulo", "FiiO")
        if not app.datos.destinos:
            anotar(AVISO, "ninguna tarjeta montada",
                   "normal si no la tienes conectada")
        for d in app.datos.destinos:
            anotar(OK, f"{d['montaje']}  ({d['tipo']}, "
                       f"{comun.formato_bytes(d['libre'])} libres)")
            if d["tipo"] not in ("vfat", "exfat", "msdos"):
                anotar(AVISO, f"es {d['tipo']}: no puedo verificar el orden físico")

        self.hecho_en = time.monotonic()
        self.revisando = False

    def dibujar(self, app, y0, alto, ancho):
        win = app.pantalla
        y = y0
        for marca, texto, detalle, _accion in self.resultados:
            if y >= y0 + alto:
                break
            if marca == "titulo":
                if y > y0:
                    y += 1
                if y < y0 + alto:
                    escribir(win, y, 1, "▸ " + texto, AMBAR, True)
                    y += 1
                continue
            tinta = {OK: VERDE, MAL: ROJO, AVISO: AMARILLO}.get(marca, NORMAL)
            escribir(win, y, 3, marca, tinta)
            escribir(win, y, 5, elipsis(texto, ancho - 6),
                     NORMAL if marca == OK else BLANCO)
            y += 1
            if detalle and y < y0 + alto:
                escribir(win, y, 7, elipsis("↳ " + detalle, ancho - 8),
                         TENUE if marca == OK else AMBAR)
                y += 1
        if self.revisando and y < y0 + alto:
            escribir(win, y + 1, 3, f"{girador()} comprobando…", AZUL)

    def tecla(self, app, k):
        if k == "R":
            self.revisar(app)
        elif k == "o":
            app.organizar()
        elif k == "n":
            app.arrancar_nicotine()
        elif k == "p":
            for marca, _t, _d, accion in self.resultados:
                if accion.startswith("p:"):
                    app.pedir([accion[2:].replace("/", " - ")])
                    return True
            app.avisar("no hay ningún álbum incompleto que volver a pedir")
        else:
            return False
        return True


# ─── Overlays ─────────────────────────────────────────────────────────────────

class Overlay:
    def dibujar(self, app):
        pass

    def tecla(self, app, k):
        return True


def _marco(app, alto, ancho, titulo, cola=""):
    """Dibuja una ventana centrada y devuelve (y, x) de su interior."""
    win = app.pantalla
    pantalla_alto, pantalla_ancho = win.getmaxyx()
    ancho = min(ancho, pantalla_ancho - 2)
    alto = min(alto, pantalla_alto - 2)
    y0 = max(0, (pantalla_alto - alto) // 2)
    x0 = max(0, (pantalla_ancho - ancho) // 2)

    for y in range(y0, y0 + alto):
        pintar(win, y, x0, ancho, NORMAL)

    prefijo = "╭─ " + titulo + " "
    sufijo = (" " + cola + " ─╮") if cola else "╮"
    relleno = max(0, ancho - len(prefijo) - len(sufijo))
    escribir(win, y0, x0, (prefijo + "─" * relleno + sufijo)[:ancho], AMBAR)
    for y in range(y0 + 1, y0 + alto - 1):
        escribir(win, y, x0, "│", AMBAR)
        escribir(win, y, x0 + ancho - 1, "│", AMBAR)
    escribir(win, y0 + alto - 1, x0, "╰" + "─" * (ancho - 2) + "╯", AMBAR)
    return y0 + 1, x0 + 2, alto - 2, ancho - 4


class OverlayPedir(Overlay):
    """Encolar un álbum o una canción."""

    def __init__(self, texto="", cancion=False):
        self.texto = texto
        self.cancion = cancion

    def dibujar(self, app):
        y, x, _alto, ancho = _marco(app, 9, 66, "pedir")
        win = app.pantalla
        escribir(win, y, x, "qué buscar", TENUE)
        escribir(win, y + 1, x, "▸", AMBAR)
        campo = elipsis(self.texto, ancho - 3)
        escribir(win, y + 1, x + 2, campo, BLANCO)
        if int(time.monotonic() * 2) % 2:
            escribir(win, y + 1, x + 2 + len(campo), BLOQUE, AMBAR)
        escribir(win, y + 2, x, "«Artista - Álbum» funciona mejor que un título suelto",
                 TENUE, ancho=ancho)

        escribir(win, y + 4, x, "tipo", TENUE)
        escribir(win, y + 4, x + 8, " álbum ", NORMAL if self.cancion else INV)
        escribir(win, y + 4, x + 17, " canción ", INV if self.cancion else NORMAL)
        escribir(win, y + 4, x + 30, "⇥ cambia", TENUE)

        escribir(win, y + 6, x, "↵ encolar", AMBAR)
        escribir(win, y + 6, x + 12, "esc cancelar", TENUE)

    def tecla(self, app, k):
        if k in ("\n", "\r", curses.KEY_ENTER):
            if self.texto.strip():
                app.pedir([self.texto], cancion=self.cancion)
            app.cerrar_overlay()
        elif k == "\t":
            self.cancion = not self.cancion
        elif k in ("\x7f", "\b", curses.KEY_BACKSPACE):
            self.texto = self.texto[:-1]
        elif k == "\x1b":
            app.cerrar_overlay()
        elif isinstance(k, str) and k.isprintable():
            self.texto += k
        return True


class OverlayBuscar(Overlay):
    """Filtro incremental para la vista que lo abre."""

    def __init__(self, vista):
        self.vista = vista
        self.texto = vista.filtro

    def dibujar(self, app):
        y, x, _alto, ancho = _marco(app, 5, 56, "buscar")
        win = app.pantalla
        escribir(win, y, x, "/", AMBAR)
        campo = elipsis(self.texto, ancho - 3)
        escribir(win, y, x + 2, campo, BLANCO)
        if int(time.monotonic() * 2) % 2:
            escribir(win, y, x + 2 + len(campo), BLOQUE, AMBAR)
        escribir(win, y + 2, x, "↵ aplicar    esc quitar el filtro", TENUE)

    def tecla(self, app, k):
        if k in ("\n", "\r", curses.KEY_ENTER):
            self.vista.filtro = self.texto
            app.cerrar_overlay()
        elif k == "\x1b":
            self.vista.filtro = ""
            app.cerrar_overlay()
        elif k in ("\x7f", "\b", curses.KEY_BACKSPACE):
            self.texto = self.texto[:-1]
            self.vista.filtro = self.texto
        elif isinstance(k, str) and k.isprintable():
            self.texto += k
            self.vista.filtro = self.texto
        return True


class OverlayConfirmar(Overlay):
    def __init__(self, pregunta, accion):
        self.pregunta = pregunta
        self.accion = accion

    def dibujar(self, app):
        y, x, _alto, ancho = _marco(app, 7, 74, "confirmar")
        win = app.pantalla
        palabras, linea, lineas = self.pregunta.split(), "", []
        for palabra in palabras:
            if len(linea) + len(palabra) + 1 > ancho:
                lineas.append(linea)
                linea = palabra
            else:
                linea = (linea + " " + palabra).strip()
        lineas.append(linea)
        for i, texto in enumerate(lineas[:3]):
            escribir(win, y + i, x, texto, BLANCO)
        escribir(win, y + 4, x, "s", VERDE, True)
        escribir(win, y + 4, x + 2, "sí", NORMAL)
        escribir(win, y + 4, x + 8, "n", ROJO, True)
        escribir(win, y + 4, x + 10, "no  ·  esc cancela", TENUE)

    def tecla(self, app, k):
        if k in ("s", "S", "y", "Y", "\n", "\r", curses.KEY_ENTER):
            app.cerrar_overlay()
            self.accion()
        elif k in ("n", "N", "\x1b", "q"):
            app.cerrar_overlay()
        return True


class OverlayAyuda(Overlay):
    def dibujar(self, app):
        vista = app.vista()
        filas = [("", "en todas partes")]
        filas += [("1 … 5", "ir a una vista"),
                  ("←  →  ↑  ↓", "moverse"),
                  ("tab", "vista siguiente"),
                  (":", "paleta de órdenes"),
                  ("?", "esta ayuda"),
                  ("q", "salir")]
        filas.append(("", f"en «{vista.nombre}»"))
        filas += [(t, d) for t, d in vista.teclas(app)]
        y, x, alto, ancho = _marco(app, len(filas) + 4, 62, "ayuda", "? cierra")
        win = app.pantalla
        for i, (tecla, texto) in enumerate(filas):
            if i >= alto:
                break
            if not tecla:
                escribir(win, y + i, x, texto, AMBAR, True)
            else:
                escribir(win, y + i, x + 2, rellenar(tecla, 12), BLANCO)
                escribir(win, y + i, x + 14, elipsis(texto, ancho - 15), TENUE)

    def tecla(self, app, k):
        app.cerrar_overlay()
        return True


class OverlayPaleta(Overlay):
    def __init__(self):
        self.texto = ""
        self.cursor = 0

    def opciones(self, app):
        todas = app.ordenes()
        if not self.texto:
            return todas
        aguja = self.texto.casefold()
        return [o for o in todas if aguja in o[0].casefold()]

    def dibujar(self, app):
        opciones = self.opciones(app)
        y, x, alto, ancho = _marco(app, min(18, max(6, len(opciones) + 4)), 66, "orden")
        win = app.pantalla
        escribir(win, y, x, ":", AMBAR)
        campo = elipsis(self.texto, ancho - 12)
        escribir(win, y, x + 2, campo, BLANCO)
        if int(time.monotonic() * 2) % 2:
            escribir(win, y, x + 2 + len(campo), BLOQUE, AMBAR)
        cola = f"{len(opciones)} de {len(app.ordenes())}"
        escribir(win, y, x + ancho - len(cola), cola, TENUE)
        escribir(win, y + 1, x, "─" * ancho, TENUE)

        self.cursor = max(0, min(self.cursor, max(0, len(opciones) - 1)))
        sitio = alto - 2
        desde = max(0, min(self.cursor - sitio + 1, len(opciones) - sitio))
        desde = max(0, desde)
        for i in range(sitio):
            indice = desde + i
            if indice >= len(opciones):
                break
            nombre, atajo, _accion = opciones[indice]
            elegido = indice == self.cursor
            fila = y + 2 + i
            if elegido:
                pintar(win, fila, x - 1, ancho + 2, SEL)
            escribir(win, fila, x, elipsis(nombre, ancho - 8),
                     SEL if elegido else NORMAL)
            if atajo:
                escribir(win, fila, x + ancho - len(atajo), atajo,
                         SEL if elegido else AMBAR)

    def tecla(self, app, k):
        opciones = self.opciones(app)
        if k in ("\n", "\r", curses.KEY_ENTER):
            app.cerrar_overlay()
            if opciones:
                opciones[max(0, min(self.cursor, len(opciones) - 1))][2]()
        elif k == "\x1b":
            app.cerrar_overlay()
        elif k == curses.KEY_UP:
            self.cursor = max(0, self.cursor - 1)
        elif k == curses.KEY_DOWN:
            self.cursor = min(max(0, len(opciones) - 1), self.cursor + 1)
        elif k in ("\x7f", "\b", curses.KEY_BACKSPACE):
            self.texto = self.texto[:-1]
            self.cursor = 0
        elif isinstance(k, str) and k.isprintable():
            self.texto += k
            self.cursor = 0
        return True


# ─── Aplicación ───────────────────────────────────────────────────────────────

class App:
    def __init__(self, pantalla):
        self.pantalla = pantalla
        self.datos = Datos()
        self.trabajo = Trabajo()
        self.biblioteca = Biblioteca()
        self.reproductor = Reproductor()
        self.marcados = set()
        self.vistas = [VistaCola(), VistaBiblioteca(), VistaFiio(),
                       VistaLog(), VistaSalud()]
        self.actual = 0
        self.overlay = None
        self._aviso = ""
        self._aviso_color = NORMAL
        self._aviso_hasta = 0.0
        self.salir = False

    # ── ciclo de vida ──
    def arrancar(self):
        self.datos.arrancar()
        self.biblioteca.cargar()
        self.vista().entrar(self)

    def cerrar(self):
        self.reproductor.parar()
        self.datos.cerrar()
        self.biblioteca.cerrar()

    def vista(self):
        return self.vistas[self.actual]

    def ir_a(self, indice):
        self.actual = max(0, min(len(self.vistas) - 1, indice))
        self.vista().entrar(self)

    def abrir(self, overlay):
        self.overlay = overlay

    def cerrar_overlay(self):
        self.overlay = None

    def avisar(self, texto, color=NORMAL, segundos=4.0):
        self._aviso = texto
        self._aviso_color = color
        self._aviso_hasta = time.monotonic() + segundos

    # ── acciones ──
    def pedir(self, consultas, cancion=False):
        creadas = comun.añadir_peticiones(consultas, tipo="cancion" if cancion else "album")
        if not creadas:
            self.avisar("ya estaba en la cola", AMARILLO)
            return
        self.avisar(f"encolado {creadas[0]['consulta']}", VERDE)
        if not self.datos.nicotine_vivo():
            self.avisar("encolado, pero Nicotine+ está cerrado: la cola no avanza",
                        AMARILLO, 6)
        self.ir_a(0)

    def cancelar_peticion(self, pet):
        if pet is None:
            return
        if pet["estado"] not in comun.ACTIVOS:
            self.avisar("esa petición ya terminó", AMARILLO)
            return

        def _op(cola):
            objetivo = comun.buscar_peticion(cola, pet["id"])
            if objetivo is not None:
                objetivo["estado"] = comun.CANCELADO
                objetivo["actualizado"] = comun.ahora()
                objetivo["mensaje"] = "cancelada desde la interfaz"

        comun.modificar_cola(_op)
        comun.registrar(f"[cancelada] {pet['id']}")
        self.avisar(f"cancelada {pet['consulta']}", AMARILLO)

    def limpiar_cola(self):
        def _op(cola):
            antes = len(cola["peticiones"])
            cola["peticiones"] = [p for p in cola["peticiones"]
                                  if p["estado"] in comun.ACTIVOS]
            return antes - len(cola["peticiones"])

        quitadas = comun.modificar_cola(_op)
        self.avisar(f"{quitadas} petición(es) fuera de la cola", VERDE)

    def organizar(self):
        if self.trabajo.activo:
            self.avisar("ya hay una tarea en curso", AMARILLO)
            return
        carpetas = list(self.datos.sobras)
        sueltos = list(self.datos.sueltos)
        if not carpetas and not sueltos:
            self.avisar("no hay nada que organizar en .descargas")
            return

        def tarea(trabajo):
            resumen = {"copiados": 0, "archivos": 0, "bytes": 0, "errores": []}
            pendientes = list(carpetas)
            if sueltos:
                trabajo.registrar(f"▸ {len(sueltos)} archivo(s) suelto(s)")
                pendientes = self._agrupar_sueltos(trabajo, sueltos) + pendientes
            for carpeta in pendientes:
                trabajo.registrar(f"▸ {os.path.basename(carpeta)}")
                resultado = biblioteca.organizar(carpeta)
                if not resultado.get("ok"):
                    trabajo.registrar(f"  {MAL} {resultado.get('motivo')}")
                    resumen["errores"].append(resultado.get("motivo", ""))
                    continue
                trabajo.registrar(f"  {OK} {resultado['carpeta']} · "
                                  f"{resultado['calidad']}")
                resumen["copiados"] += 1
                resumen["archivos"] += len(resultado.get("nombres", []))
                for aviso in resultado.get("avisos", []):
                    trabajo.registrar(f"  {AVISO} {aviso}")
            return resumen

        self.trabajo.lanzar("organizando .descargas", tarea, clase="organizar")
        self.ir_a(0)

    @staticmethod
    def _agrupar_sueltos(trabajo, sueltos):
        """Agrupa FLAC sueltos por ALBUMARTIST+ALBUM en carpetas temporales."""
        grupos = {}
        for ruta in sueltos:
            tags = biblioteca.leer_tags(ruta)
            if "ERROR" in tags:
                trabajo.registrar(f"  {AVISO} {os.path.basename(ruta)}: {tags['ERROR']}")
                continue
            artista = (tags.get("ALBUMARTIST") or tags.get("ARTIST") or "").strip()
            album = (tags.get("ALBUM") or "").strip()
            if not artista or not album:
                trabajo.registrar(f"  {AVISO} {os.path.basename(ruta)}: sin ARTIST/ALBUM")
                continue
            grupos.setdefault((artista, album), []).append(ruta)

        carpetas = []
        for (artista, album), rutas in grupos.items():
            destino = os.path.join(comun.DIR_DESCARGAS,
                                   "_sueltos-" + comun._slug(f"{artista} {album}"))
            os.makedirs(destino, exist_ok=True)
            for ruta in rutas:
                shutil.move(ruta, os.path.join(destino, os.path.basename(ruta)))
            carpetas.append(destino)
        return carpetas

    def copiar_fiio(self, destino, plan, simular=False, rehacer=False):
        if destino is None or not plan:
            self.avisar("no hay tarjeta o no hay nada que copiar", AMARILLO)
            return
        if self.trabajo.activo:
            self.avisar("ya hay una tarea en curso", AMARILLO)
            return
        raiz_destino = os.path.join(destino["montaje"], fiio.DESTINO_REL_POR_DEFECTO)
        albumes = list(plan)
        total = sum(a["bytes"] for a in albumes)

        def tarea(trabajo):
            trabajo.total_bytes = 0 if simular else total
            return fiio.copiar(
                raiz_destino, albumes, simular=simular, rehacer=rehacer,
                informar=trabajo.registrar,
                evento=trabajo.evento,
                cancelado=trabajo.cancelar.is_set)

        titulo = ("simulando la copia" if simular
                  else ("reescribiendo en la tarjeta" if rehacer else "copiando al FiiO"))
        self.trabajo.lanzar(f"{titulo} · {len(albumes)} álbumes", tarea)
        self.ir_a(2)

    def expulsar(self, destino):
        if destino is None:
            self.avisar("no hay ninguna tarjeta montada", AMARILLO)
            return
        if self.trabajo.activo:
            self.avisar("espera a que termine la copia", AMARILLO)
            return
        self.avisar("sincronizando y expulsando…", AZUL, 8)

        def tarea(trabajo):
            fiio.desmontar(destino["montaje"], informar=trabajo.registrar)
            return {"copiados": 0, "archivos": 0, "bytes": 0}

        self.trabajo.lanzar("expulsando la tarjeta", tarea)

    def arrancar_nicotine(self):
        if self.datos.nicotine_vivo():
            self.avisar("Nicotine+ ya está corriendo")
            return
        try:
            subprocess.Popen(["nicotine"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            self.avisar("abriendo Nicotine+…", VERDE)
        except OSError as error:
            self.avisar(f"no pude abrirlo: {error}", ROJO)

    def abrir_log(self):
        editor = os.environ.get("EDITOR") or "nano"
        try:
            curses.def_prog_mode()
            curses.endwin()
            subprocess.call([editor, comun.ARCHIVO_LOG])
        except OSError as error:
            self.avisar(f"no pude abrir {editor}: {error}", ROJO)
        finally:
            curses.reset_prog_mode()
            self.pantalla.clear()
            self.pantalla.refresh()

    def ordenes(self):
        """(nombre, atajo, acción) para la paleta."""
        vista_fiio = self.vistas[2]
        return [
            ("pedir un álbum", "a", lambda: self.abrir(OverlayPedir())),
            ("pedir una canción suelta", "", lambda: self.abrir(OverlayPedir(cancion=True))),
            ("cancelar la petición seleccionada", "x",
             lambda: self.cancelar_peticion(self.vistas[0].seleccion(self))),
            ("limpiar lo terminado de la cola", "L", self.limpiar_cola),
            ("organizar .descargas", "o", self.organizar),
            ("copiar al FiiO", "c", lambda: (self.ir_a(2),
                                             vista_fiio._pedir_copia(self, False))),
            ("copiar al FiiO en simulación", "n",
             lambda: (self.ir_a(2), self.copiar_fiio(vista_fiio.montaje(self),
                                                     vista_fiio.plan, simular=True))),
            ("reescribir en la tarjeta (--rehacer)", "w",
             lambda: (self.ir_a(2), vista_fiio._pedir_copia(self, True))),
            ("quitar todas las marcas", "m", lambda: (self.marcados.clear(),
                                                      vista_fiio.calcular(self))),
            ("expulsar la tarjeta", "u",
             lambda: self.expulsar(vista_fiio.montaje(self))),
            ("recargar la biblioteca desde el disco", "r",
             lambda: (self.biblioteca.cargar(), self.ir_a(1))),
            ("cambiar entre la biblioteca nueva y la heredada", "b",
             lambda: (self.biblioteca.cambiar_raiz(), self.ir_a(1))),
            ("parar la reproducción", "s", self.reproductor.parar),
            ("abrir Nicotine+", "", self.arrancar_nicotine),
            ("revisar la instalación", "R",
             lambda: (self.ir_a(4), self.vistas[4].revisar(self))),
            ("abrir pipeline.log en $EDITOR", "", self.abrir_log),
            ("salir", "q", lambda: setattr(self, "salir", True)),
        ]

    # ── bucle ──
    def bucle(self):
        # `timeout` y no `nodelay`: sin espera, ncurses devuelve las flechas
        # troceadas (ESC, '[', 'B') en vez de KEY_DOWN y no se reconocen.
        self.pantalla.timeout(25)
        self.pantalla.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        ultimo = 0.0
        while not self.salir:
            k = self._tecla()
            if k is not None:
                self.procesar(k)
                if self.salir:
                    return
            ahora = time.monotonic()
            if ahora - ultimo >= 1 / 15:
                ultimo = ahora
                try:
                    self.dibujar()
                except curses.error:
                    pass

    def _tecla(self):
        try:
            return self.pantalla.get_wch()
        except curses.error:
            return None
        except KeyboardInterrupt:
            self.salir = True
            return None

    def procesar(self, k):
        if k == curses.KEY_RESIZE:
            self.pantalla.clear()
            return
        if self.overlay is not None:
            self.overlay.tecla(self, k)
            return
        if isinstance(k, str) and k in "12345":
            self.ir_a(int(k) - 1)
            return
        if k == "\t":
            self.ir_a((self.actual + 1) % len(self.vistas))
            return
        if k == curses.KEY_BTAB:
            self.ir_a((self.actual - 1) % len(self.vistas))
            return
        if k == "?":
            self.abrir(OverlayAyuda())
            return
        if k == ":":
            self.abrir(OverlayPaleta())
            return
        if k == "q":
            if self.trabajo.activo:
                self.abrir(OverlayConfirmar(
                    "Hay una tarea en curso. ¿Salir de todas formas?",
                    lambda: setattr(self, "salir", True)))
            else:
                self.salir = True
            return
        if self.vista().tecla(self, k):
            return
        if k == "s":
            self.reproductor.parar()

    # ── dibujado ──
    def dibujar(self):
        win = self.pantalla
        alto, ancho = win.getmaxyx()
        win.erase()
        if alto < 12 or ancho < 46:
            escribir(win, 0, 0, "La ventana es demasiado pequeña.", AMARILLO)
            escribir(win, 1, 0, f"Hacen falta 46x12; hay {ancho}x{alto}.", TENUE)
            win.refresh()
            return

        self._cabecera(ancho)
        self._pestanas(ancho)
        escribir(win, 2, 0, "─" * ancho, TENUE)

        cuerpo_y = 3
        cuerpo_alto = alto - 6
        self.vista().dibujar(self, cuerpo_y, cuerpo_alto, ancho)

        escribir(win, alto - 3, 0, "─" * ancho, TENUE)
        self._estado(alto - 2, ancho)
        self._teclas(alto - 1, ancho)

        if self.overlay is not None:
            self.overlay.dibujar(self)
        win.refresh()

    def _cabecera(self, ancho):
        win = self.pantalla
        escribir(win, 0, 1, "flacpipe", AMBAR, True)
        pet = self.datos.peticion_en_curso()
        if pet is not None:
            escribir(win, 0, 10, girador(), AZUL)
            escribir(win, 0, 12, elipsis(pet["consulta"], max(0, ancho - 46)), TENUE)

        derecha = []
        derecha.append(("nicotine+ ●" if self.datos.nicotine_vivo() else "nicotine+ ○",
                        VERDE if self.datos.nicotine_vivo() else TENUE))
        destinos = self.datos.destinos
        derecha.append((f"{destinos[0]['etiqueta']} ●" if destinos else "tarjeta ○",
                        VERDE if destinos else TENUE))
        derecha.append((time.strftime("%H:%M"), TENUE))

        x = ancho - sum(len(t) + 3 for t, _c in derecha) - 1
        for texto, color in derecha:
            x = escribir(win, 0, x, texto, color) + 3

    def _pestanas(self, ancho):
        win = self.pantalla
        x = 1
        for i, vista in enumerate(self.vistas):
            etiqueta = f" {i + 1} {vista.nombre} "
            x = escribir(win, 1, x, etiqueta, INV if i == self.actual else TENUE)
            x += 1
        cola = []
        resumen = self.vista().resumen(self)
        if resumen:
            cola.append(resumen)
        activas = len(self.datos.activas())
        if activas:
            cola.append(f"{activas} en cola")
        if self.marcados:
            cola.append(f"{len(self.marcados)} marcados")
        if self.datos.sobras or self.datos.sueltos:
            cola.append(f"{len(self.datos.sobras) + len(self.datos.sueltos)} sin organizar")
        texto = "  ·  ".join(cola)
        if texto:
            escribir(win, 1, max(x + 2, ancho - len(texto) - 1), texto, TENUE)

    def _estado(self, y, ancho):
        win = self.pantalla
        if self._aviso and time.monotonic() < self._aviso_hasta:
            escribir(win, y, 1, elipsis(self._aviso, ancho - 2), self._aviso_color, True)
            return
        if self.reproductor.sonando:
            transcurrido = self.reproductor.transcurrido()
            total = self.reproductor.duracion
            escribir(win, y, 1, "▶", VERDE)
            escribir(win, y, 3, elipsis(self.reproductor.titulo, max(4, ancho - 34)),
                     BLANCO)
            x = max(5, ancho - 30)
            if total:
                barra(win, y, x, 16, min(1.0, transcurrido / total), VERDE)
                escribir(win, y, x + 17,
                         f"{duracion(transcurrido)}/{duracion(total)}", TENUE)
            else:
                escribir(win, y, x, duracion(transcurrido), TENUE)
            return
        if self.trabajo.activo:
            escribir(win, y, 1, girador(), AZUL)
            escribir(win, y, 3, elipsis(self.trabajo.titulo, ancho - 4), TENUE)
            return
        if self.datos.aviso:
            escribir(win, y, 1, elipsis(self.datos.aviso, ancho - 2), ROJO)
            return
        pet = self.datos.peticion_en_curso()
        if pet is not None and pet["estado"] == comun.DESCARGANDO:
            velocidad = self.datos.velocimetro
            escribir(win, y, 1, f"{comun.formato_bytes(velocidad.actual)}/s", BLANCO)
            escribir(win, y, 14, chispa(velocidad.historial, min(30, ancho - 18)), AZUL)
            return
        escribir(win, y, 1, "todo en calma", TENUE)

    def _teclas(self, y, ancho):
        """La fila de teclas. `?` y `q` van reservadas a la derecha: son las dos
        que nunca pueden faltar, así que se dibujan antes que las de la vista."""
        win = self.pantalla

        def par(x, tecla, texto):
            x = escribir(win, y, x, tecla, AMBAR, True)
            return escribir(win, y, x + 1, texto, TENUE) + 2

        fijas = [("?", "ayuda"), ("q", "salir")]
        reserva = sum(len(t) + len(d) + 3 for t, d in fijas)
        borde = max(2, ancho - reserva)
        x = borde
        for tecla, texto in fijas:
            x = par(x, tecla, texto)

        x = 1
        for tecla, texto in self.vista().teclas(self):
            if x + len(tecla) + len(texto) + 3 > borde:
                escribir(win, y, x, "…", TENUE)
                break
            x = par(x, tecla, texto)


# ─── Entrada ──────────────────────────────────────────────────────────────────

def _arrancar(pantalla):
    _iniciar_colores()
    app = App(pantalla)
    app.arrancar()
    try:
        app.bucle()
    finally:
        app.cerrar()
    return 0


def ejecutar():
    """Abre la interfaz. Devuelve el código de salida."""
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    comun.asegurar_dirs()
    if not sys.stdout.isatty():
        print("flacpipe: la interfaz necesita una terminal de verdad.", file=sys.stderr)
        return 1
    try:
        return curses.wrapper(_arrancar) or 0
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(ejecutar())
