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
import json
import locale
import math
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
import fh_listas as listas           # noqa: E402


# ─── Glifos ───────────────────────────────────────────────────────────────────
# Todos ocupan una celda. Nada de emoji: un emoji mide dos columnas y descuadra
# la rejilla entera en cuanto una fuente no coincide con otra.

OCTAVOS = ("", "▏", "▎", "▍", "▌", "▋", "▊", "▉")
BLOQUE = "█"
VACIO = "─"
ALTURAS = "▁▂▃▄▅▆▇█"

# Dos burbujas que suben con vaivén, en una sola celda de braille: algo trabaja.
BURBUJAS = "⡁⠠⠂⢈⠄⠐"
# Gotas que caen por las dos columnas de la celda: algo se está bajando.
GOTAS = "⠡⢂⠌⡐"
# Un sonar: un punto que se abre en onda y se apaga. Algo se está buscando.
SONAR = ("•", "∘", "○", "○", " ")

PATRON_LOG = re.compile(r"^(\S+)\s+\[([^\]]+)\]\s*(.*)$")
PATRON_ID = re.compile(r"(\d{8}-\d{6}-[a-z0-9-]+)")

# Segundos que el plugin acumula resultados antes de elegir fuente
# (settings["ventana_busqueda"] de flac_hunter). Sólo se usa para pintar la cuenta.
VENTANA_BUSQUEDA = 45
# Donde Nicotine+ escribe cada archivo mientras lo baja (config: incompletedir).
DIR_INCOMPLETOS = comun.DIR_INCOMPLETOS
PATRON_INCOMPLETO = re.compile(r"^INCOMPLETE[0-9a-f]{32}(.+)$")
# La lista de transferencias que Nicotine+ guarda cada pocos minutos. Cada fila
# es [usuario, ruta_remota, carpeta, estado, tamaño, ...] (Nicotine+ 3.x,
# pynicotine/transfers.py): de ahí sale cuánto pesa cada pista. "" si no hay.
ARCHIVO_TRANSFERENCIAS = (os.path.join(comun.NICOTINE["datos"], "downloads.json")
                          if comun.NICOTINE.get("datos") else "")
# Segundos que una pista recién terminada sigue contando como «recién bajada».
SEGUNDOS_RECIEN = 3.0
# Lo que Nicotine+ cambia por «_» en el nombre de un archivo (utils.clean_file).
PATRON_NOMBRE_ILEGAL = re.compile(r'[/?:><|*"\x00-\x1f]')
PATRON_CALIDAD = re.compile(r"(\?|\d+)-bit(?:\s*/\s*([\d.]+)\s*kHz)?")

GLIFO_ESTADO = {
    comun.PENDIENTE: "•",
    comun.BUSCANDO: "○",
    comun.DESCARGANDO: "↓",
    comun.LISTO: "✓",
    comun.FALLIDO: "✗",
    comun.CANCELADO: "⊘",
}

OK, MAL, AVISO = "✓", "✗", "!"


# ─── Colores ──────────────────────────────────────────────────────────────────
# Tema acuático: azules de lo hondo a la espuma para casi todo, y tres tonos
# para lo que tiene que saltar a la vista: espuma (bien), coral (mal) y arena
# (ojo). Se pinta con la paleta de 256 colores de xterm; con 8 o 16 colores se
# toma lo más parecido de los básicos, y sin color, negrita e inverso.

(NORMAL, AGUA, ESPUMA, CORAL, CIELO, ARENA, LILA, TENUE, BLANCO, LINEA,
 SEL, INV, VENTANA) = range(13)

# Estilo de la celda, aparte del color
NEGRITA, ATENUADO, INVERSO = 1, 2, 4
# Fondo «al revés» para las terminales sin color: así se marca la selección
REVES = -2

# Degradados de 256 colores, del fondo a la superficie
OCEANO = (24, 25, 31, 32, 38, 39, 45, 81, 117, 159, 195)
CIELOS = (24, 25, 26, 32, 33, 39, 75, 111, 153, 189)
TURQUESA = (23, 30, 37, 44, 80, 116, 152, 195)
ARRECIFE = (23, 29, 36, 43, 79, 115, 158, 195)
CORALES = (52, 88, 131, 167, 174, 210, 217, 224)
DUNAS = (58, 94, 136, 179, 180, 222, 223, 230)
LAVANDA = (60, 61, 104, 105, 147, 189)

FONDO_SELECCION = 236
FONDO_VENTANA = 235
COLOR_LINEA = 23

TEMA_256 = {
    NORMAL: (-1, None, 0),
    AGUA: (81, None, 0),
    ESPUMA: (79, None, 0),
    CORAL: (210, None, 0),
    CIELO: (75, None, 0),
    ARENA: (222, None, 0),
    LILA: (147, None, 0),
    TENUE: (66, None, 0),
    BLANCO: (195, None, NEGRITA),
    LINEA: (COLOR_LINEA, None, 0),
    SEL: (195, FONDO_SELECCION, 0),
    INV: (16, 81, NEGRITA),
    VENTANA: (-1, FONDO_VENTANA, 0),
}

TEMA_BASICO = {
    NORMAL: (-1, None, 0),
    AGUA: (curses.COLOR_CYAN, None, NEGRITA),
    ESPUMA: (curses.COLOR_GREEN, None, 0),
    CORAL: (curses.COLOR_RED, None, 0),
    CIELO: (curses.COLOR_BLUE, None, NEGRITA),
    ARENA: (curses.COLOR_YELLOW, None, 0),
    LILA: (curses.COLOR_MAGENTA, None, 0),
    TENUE: (-1, None, ATENUADO),
    BLANCO: (curses.COLOR_WHITE, None, NEGRITA),
    LINEA: (curses.COLOR_BLUE, None, 0),
    SEL: (curses.COLOR_WHITE, curses.COLOR_BLUE, 0),
    INV: (curses.COLOR_BLACK, curses.COLOR_CYAN, NEGRITA),
    VENTANA: (-1, -1, 0),
}

TEMA_SIN_COLOR = {
    NORMAL: (-1, None, 0),
    AGUA: (-1, None, NEGRITA),
    ESPUMA: (-1, None, 0),
    CORAL: (-1, None, NEGRITA),
    CIELO: (-1, None, 0),
    ARENA: (-1, None, NEGRITA),
    LILA: (-1, None, 0),
    TENUE: (-1, None, ATENUADO),
    BLANCO: (-1, None, NEGRITA),
    LINEA: (-1, None, ATENUADO),
    SEL: (-1, REVES, 0),
    INV: (-1, REVES, NEGRITA),
    VENTANA: (-1, -1, 0),
}

# El degradado que corresponde a cada tono, para barras, olas y brillos
RAMPA = {AGUA: OCEANO, CIELO: CIELOS, ESPUMA: ARRECIFE, CORAL: CORALES,
         ARENA: DUNAS, LILA: LAVANDA, TENUE: TURQUESA}

COLOR_ESTADO = {
    comun.PENDIENTE: LILA,
    comun.BUSCANDO: CIELO,
    comun.DESCARGANDO: AGUA,
    comun.LISTO: ESPUMA,
    comun.FALLIDO: CORAL,
    comun.CANCELADO: TENUE,
}

# Los 16 primeros cambian con cada terminal; éstos (los de xterm) sólo sirven
# para aproximar.
_BASICOS = ((0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0), (0, 0, 238),
            (205, 0, 205), (0, 205, 205), (229, 229, 229), (127, 127, 127),
            (255, 0, 0), (0, 255, 0), (255, 255, 0), (92, 92, 255), (255, 0, 255),
            (0, 255, 255), (255, 255, 255))


def _rgb256(n):
    if n < 16:
        return _BASICOS[n]
    if n < 232:
        n -= 16
        niveles = (0, 95, 135, 175, 215, 255)
        return (niveles[n // 36], niveles[n // 6 % 6], niveles[n % 6])
    gris = 8 + (n - 232) * 10
    return (gris, gris, gris)


RGB = tuple(_rgb256(n) for n in range(256))
# Lo que pone la terminal cuando no se pide color. No hay forma portátil de
# preguntarlo; para mezclar basta con suponer un fondo oscuro y un texto claro.
FONDO_TERMINAL = 234
TINTA_TERMINAL = 252

_PALETA = {"colores": 256, "tema": TEMA_256, "por_defecto": True}
_PARES = {}
_ATRIBUTOS = {}
_CERCANOS = {}
_MEZCLAS = {}


def cercano(rgb, candidatos=range(16, 256)):
    """El color de la paleta más parecido a `rgb` (distancia «redmean»)."""
    clave = (rgb, candidatos.start, candidatos.stop)
    mejor = _CERCANOS.get(clave)
    if mejor is None:
        r, g, b = rgb
        distancia = None
        for n in candidatos:
            r2, g2, b2 = RGB[n]
            media = (r + r2) / 2
            d = ((2 + media / 256) * (r - r2) ** 2 + 4 * (g - g2) ** 2
                 + (2 + (255 - media) / 256) * (b - b2) ** 2)
            if distancia is None or d < distancia:
                mejor, distancia = n, d
        _CERCANOS[clave] = mejor
    return mejor


def mezclar(a, b, t):
    """El color de 256 que queda a `t` (de 0 a 1) del camino entre `a` y `b`."""
    paso = int(max(0.0, min(1.0, t)) * 32 + 0.5)
    clave = (a, b, paso)
    color = _MEZCLAS.get(clave)
    if color is None:
        ra, rb = RGB[a], RGB[b]
        u = paso / 32
        color = cercano(tuple(int(x + (y - x) * u + 0.5) for x, y in zip(ra, rb)))
        _MEZCLAS[clave] = color
    return color


def rampa(colores, t):
    """El punto `t` (de 0 a 1) de un degradado de colores de 256."""
    t = max(0.0, min(1.0, t))
    pos = t * (len(colores) - 1)
    i = int(pos)
    if i >= len(colores) - 1:
        return colores[-1]
    return mezclar(colores[i], colores[i + 1], pos - i)


def _iniciar_colores():
    _PARES.clear()
    _ATRIBUTOS.clear()
    if not curses.has_colors():
        _PALETA.update(colores=0, tema=TEMA_SIN_COLOR)
        return
    curses.start_color()
    try:
        curses.use_default_colors()
        _PALETA["por_defecto"] = True
    except curses.error:
        _PALETA["por_defecto"] = False
    if curses.COLORS >= 256:
        _PALETA.update(colores=256, tema=TEMA_256)
    else:
        _PALETA.update(colores=max(8, min(curses.COLORS, 16)), tema=TEMA_BASICO)


def _ajustar(n):
    """Un color de 256 llevado a lo que tenga la terminal."""
    colores = _PALETA["colores"]
    if n is None or n < 0 or colores >= 256:
        return n
    if colores == 0:
        return -1
    return cercano(RGB[n], range(1, colores))


def _tinta(color, negrita=False):
    """(frente, fondo, estilo) listos para la terminal. `color` es un tono del
    tema (AGUA, TENUE…) o una tupla (frente, fondo) de la paleta de 256; un
    fondo None deja el que ya hubiera debajo."""
    if isinstance(color, tuple):
        frente = _ajustar(color[0])
        fondo = _ajustar(color[1]) if len(color) > 1 else None
        estilo = color[2] if len(color) > 2 else 0
    else:
        frente, fondo, estilo = _PALETA["tema"].get(color, (-1, None, 0))
    if negrita:
        estilo |= NEGRITA
    return frente, fondo, estilo


def _par(frente, fondo):
    """El número de par para (frente, fondo), creándolo la primera vez.

    curses.color_pair() sólo entiende los 256 primeros pares; si se acabaran,
    se reusa el más parecido que ya exista con el mismo fondo (cambiar un par
    en uso repintaría de otro color lo que ya está en pantalla)."""
    if not _PALETA["por_defecto"]:
        frente = curses.COLOR_WHITE if frente < 0 else frente
        fondo = curses.COLOR_BLACK if fondo < 0 else fondo
    elif frente == -1 and fondo == -1:
        return 0
    clave = (frente, fondo)
    numero = _PARES.get(clave)
    if numero is None:
        numero = len(_PARES) + 1
        try:
            if numero >= min(curses.COLOR_PAIRS, 256):
                raise OverflowError
            curses.init_pair(numero, frente, fondo)
        except OverflowError:
            numero = _par_parecido(frente, fondo)
        except (curses.error, ValueError):
            numero = 0                  # la terminal no lo acepta: mejor sin color
        _PARES[clave] = numero
    return numero


def _par_parecido(frente, fondo):
    mejor, distancia = 0, None
    destino = RGB[frente] if 0 <= frente < 256 else RGB[TINTA_TERMINAL]
    for (f, b), numero in _PARES.items():
        if b != fondo or not numero:
            continue
        rgb = RGB[f] if 0 <= f < 256 else RGB[TINTA_TERMINAL]
        d = sum((x - y) ** 2 for x, y in zip(rgb, destino))
        if distancia is None or d < distancia:
            mejor, distancia = numero, d
    return mejor


def _atributo(frente, fondo, estilo):
    clave = (frente, fondo, estilo)
    atributo = _ATRIBUTOS.get(clave)
    if atributo is None:
        atributo = curses.A_NORMAL
        if _PALETA["colores"]:
            atributo |= curses.color_pair(_par(frente, fondo))
        elif fondo == REVES:
            atributo |= curses.A_REVERSE
        if estilo & NEGRITA:
            atributo |= curses.A_BOLD
        if estilo & ATENUADO:
            atributo |= curses.A_DIM
        if estilo & INVERSO:
            atributo |= curses.A_REVERSE
        _ATRIBUTOS[clave] = atributo
    return atributo


# ─── Lienzo ───────────────────────────────────────────────────────────────────

class Lienzo:
    """La pantalla entera en memoria: cada cuadro se compone aquí y se vuelca
    de una vez. Así el fondo es transparente (el texto respeta el que ya había
    debajo, como el de una fila seleccionada) y se pueden aplicar efectos sobre
    lo ya pintado: hundir lo que queda detrás de una ventana o pasar una ola
    por una fila recién terminada.

    Cada celda es (carácter, frente, fondo, estilo), con los colores ya
    ajustados a la terminal."""

    VACIA = (" ", -1, -1, 0)

    def __init__(self):
        self.alto = 0
        self.ancho = 0
        self.filas = []

    def preparar(self, alto, ancho):
        self.alto, self.ancho = alto, ancho
        self.filas = [[self.VACIA] * ancho for _ in range(alto)]

    def poner(self, y, x, texto, frente, fondo, estilo):
        fila = self.filas[y]
        if fondo is None:
            for i, c in enumerate(texto, x):
                fila[i] = (c, frente, fila[i][2], estilo)
        else:
            for i, c in enumerate(texto, x):
                fila[i] = (c, frente, fondo, estilo)

    def fondo(self, y, x0, x1, fondo):
        """Cambia el fondo de un tramo sin tocar lo que hay escrito."""
        if not 0 <= y < self.alto:
            return
        fila = self.filas[y]
        for i in range(max(0, x0), min(self.ancho, x1)):
            c = fila[i]
            fila[i] = (c[0], c[1], fondo, c[3])

    def atenuar(self, cuanto):
        """Todo lo pintado se hunde hacia el fondo (0 nada, 1 del todo)."""
        if cuanto <= 0:
            return
        if _PALETA["colores"] < 256:
            for fila in self.filas:
                for i, (c, f, b, e) in enumerate(fila):
                    fila[i] = (c, f, b, (e | ATENUADO) & ~NEGRITA)
            return
        for fila in self.filas:
            for i, (c, f, b, e) in enumerate(fila):
                debajo = FONDO_TERMINAL if b < 0 else b
                f = mezclar(TINTA_TERMINAL if f < 0 else f, debajo, cuanto)
                if b >= 0:
                    b = mezclar(b, FONDO_TERMINAL, cuanto)
                fila[i] = (c, f, b, e & ~NEGRITA)

    def volcar(self, win):
        """Pasa el cuadro a curses por tramos del mismo color; curses sólo
        manda a la terminal lo que cambió desde el cuadro anterior."""
        alto, ancho = win.getmaxyx()
        for y in range(min(alto, self.alto)):
            fila = self.filas[y]
            n = min(ancho, self.ancho)
            x = 0
            while x < n:
                c, f, b, e = fila[x]
                inicio = x
                trozo = [c]
                # en un espacio el color del texto no se ve: se junta con lo que siga
                frente = None if (c == " " and not e & INVERSO) else f
                x += 1
                while x < n:
                    c, f2, b2, e2 = fila[x]
                    if b2 != b or e2 != e:
                        break
                    if c != " " or e & INVERSO:
                        if frente is None:
                            frente = f2
                        elif f2 != frente:
                            break
                    trozo.append(c)
                    x += 1
                try:
                    win.addstr(y, inicio, "".join(trozo),
                               _atributo(f if frente is None else frente, b, e))
                except curses.error:
                    pass            # la última celda de la pantalla siempre lanza


LIENZO = Lienzo()

# Lo que se mueve avisa al pintarse; mientras algo se mueva, el cuadro
# siguiente llega pronto (30 por segundo) y si no, la pantalla descansa.
RITMO = {"animando": False}


def _anima():
    RITMO["animando"] = True


# ─── Dibujo ───────────────────────────────────────────────────────────────────

def escribir(win, y, x, texto, color=NORMAL, negrita=False, ancho=None):
    """Escribe en el lienzo recortando a la pantalla. Devuelve la x siguiente.

    `color` es un tono del tema o una tupla (frente, fondo) de 256 colores; si
    no trae fondo, el texto conserva el que ya hubiera debajo."""
    if not texto:
        return x
    lienzo = LIENZO
    if y < 0 or y >= lienzo.alto or x < 0 or x >= lienzo.ancho:
        return x
    limite = lienzo.ancho - x
    if ancho is not None:
        limite = min(limite, ancho)
    texto = texto[:limite]
    if not texto:
        return x
    frente, fondo, estilo = _tinta(color, negrita)
    lienzo.poner(y, x, texto, frente, fondo, estilo)
    return x + len(texto)


def escribir_trozos(win, y, x, trozos, ancho=None):
    """Trozos (texto, color) uno detrás de otro. Devuelve la x siguiente."""
    fin = None if ancho is None else x + ancho
    for texto, color in trozos:
        sitio = None if fin is None else fin - x
        if sitio is not None and sitio <= 0:
            break
        x = escribir(win, y, x, texto, NORMAL if color is None else color, ancho=sitio)
    return x


def pintar(win, y, x, ancho, color):
    """Rellena de fondo: una fila seleccionada, el interior de una ventana."""
    lienzo = LIENZO
    if not 0 <= y < lienzo.alto:
        return
    x0, x1 = max(0, x), min(lienzo.ancho, x + ancho)
    if x1 <= x0:
        return
    frente, fondo, estilo = _tinta(color)
    lienzo.poner(y, x0, " " * (x1 - x0), frente, -1 if fondo is None else fondo, estilo)


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


# ─── Movimiento ───────────────────────────────────────────────────────────────
# Todo lo que se mueve mide algo real: las burbujas, que algo trabaja; el sonar,
# que se busca; las gotas, que se baja; la respiración, que se espera turno; el
# brillo de una barra, que alguien la está llenando ahora mismo; el oleaje, la
# velocidad. Nada se anima por adorno cuando no pasa nada.

def suave(u):
    """Arranca y llega despacio (cúbica), u de 0 a 1."""
    u = max(0.0, min(1.0, u))
    return 4 * u ** 3 if u < 0.5 else 1 - (-2 * u + 2) ** 3 / 2


def frenada(u):
    """Sale deprisa y llega frenando (cúbica), u de 0 a 1."""
    u = max(0.0, min(1.0, u))
    return 1 - (1 - u) ** 3


def girador(t=None):
    """Burbujas que suben: algo está trabajando."""
    _anima()
    t = time.monotonic() if t is None else t
    return BURBUJAS[int(t * 9) % len(BURBUJAS)]


def gota(t=None):
    """Gotas que caen: algo se está bajando."""
    _anima()
    t = time.monotonic() if t is None else t
    return GOTAS[int(t * 10) % len(GOTAS)]


def sonar(t=None):
    """(glifo, color): un punto que se abre en onda y se apaga, como un sonar."""
    _anima()
    t = time.monotonic() if t is None else t
    fase = (t / 1.25) % 1.0
    glifo = SONAR[min(len(SONAR) - 1, int(fase * len(SONAR)))]
    return glifo, (rampa(CIELOS, 1.0 - 0.75 * fase),)


def respiro(t=None, colores=LAVANDA, periodo=2.6):
    """Un color que respira despacio, para lo que espera turno."""
    _anima()
    t = time.monotonic() if t is None else t
    u = (math.sin(t * 2 * math.pi / periodo) + 1) / 2
    return (rampa(colores, 0.25 + 0.65 * u),)


def glifo_estado(estado, t=None):
    """(glifo, color) del estado de una petición, con su movimiento si lo tiene."""
    if estado == comun.BUSCANDO:
        return sonar(t)
    if estado == comun.PENDIENTE:
        return GLIFO_ESTADO[estado], respiro(t)
    if estado == comun.DESCARGANDO:
        return gota(t), AGUA
    return GLIFO_ESTADO.get(estado, "·"), COLOR_ESTADO.get(estado, NORMAL)


def _juntar(trozos):
    """Une los trozos seguidos del mismo color: menos llamadas a escribir."""
    salida = []
    for texto, color in trozos:
        if salida and salida[-1][1] == color:
            salida[-1] = (salida[-1][0] + texto, color)
        else:
            salida.append((texto, color))
    return salida


def barra_trozos(ancho, frac, color=AGUA, vacio=LINEA, vivo=False, t=None):
    """Una barra de agua en trozos (texto, color). Se llena con resolución de
    un octavo de celda; el cuerpo es de un azul parejo y el frente se aclara
    como la cresta de una ola. Si `vivo`, un reflejo la recorre mientras algo
    la está llenando de verdad."""
    if ancho <= 0:
        return []
    frac = max(0.0, min(1.0, frac))
    total = frac * ancho
    llenas = min(ancho, int(total))
    resto = int((total - llenas) * 8) if llenas < ancho else 0
    colores = RAMPA.get(color)
    centro = None
    if vivo and total > 0:
        _anima()
        t = time.monotonic() if t is None else t
        centro = ((t / 2.2) % 1.0) * (total + 10) - 5
    celdas = llenas + (1 if resto else 0)
    trozos = []
    for i in range(celdas):
        glifo = BLOQUE if i < llenas else OCTAVOS[resto]
        if colores is None:
            trozos.append((glifo, color))
            continue
        cresta = max(0.0, 1 - (celdas - 1 - i) / 3)     # las tres últimas celdas
        tono = rampa(colores, 0.32 + 0.4 * cresta)
        if centro is not None:
            k = math.exp(-((i - centro) / 2.4) ** 2)
            if k > 0.05:
                tono = mezclar(tono, colores[-2], 0.6 * k)
        trozos.append((glifo, (tono,)))
    hueco = ancho - llenas - (1 if resto else 0)
    if hueco > 0:
        trozos.append((VACIO * hueco, vacio))
    return _juntar(trozos)


def barra(win, y, x, ancho, frac, color=AGUA, vacio=LINEA, vivo=False, t=None):
    """Barra de agua (ver barra_trozos). Devuelve la x siguiente."""
    return escribir_trozos(win, y, x, barra_trozos(ancho, frac, color, vacio, vivo, t))


def corriente(ancho, t=None, color=AGUA):
    """Algo pasa por la tubería sin que se sepa cuánto falta: un pulso de luz
    que la recorre de izquierda a derecha. Trozos (texto, color)."""
    if ancho <= 0:
        return []
    _anima()
    t = time.monotonic() if t is None else t
    colores = RAMPA.get(color, OCEANO)
    cresta = rampa(colores, 0.85)
    centro = ((t / 1.5) % 1.0) * (ancho + 10) - 5
    trozos = []
    for i in range(ancho):
        k = math.exp(-((i - centro) / 2.2) ** 2)
        if k < 0.08:
            trozos.append((VACIO, LINEA))
        else:
            trozos.append(("━" if k > 0.4 else VACIO, (mezclar(COLOR_LINEA, cresta, k),)))
    return _juntar(trozos)


def chispa(muestras, ancho, techo=None):
    """La historia de la velocidad como una línea de agua: cada celda sube y se
    aclara con su muestra. Trozos (texto, color), exactamente `ancho` celdas."""
    if ancho <= 0:
        return []
    datos = list(muestras)[-ancho:]
    if not datos:
        return [(" " * ancho, NORMAL)]
    techo = techo or max(datos) or 1.0
    trozos = []
    for v in datos:
        h = max(0, min(7, int(v / techo * 7 + 0.5)))
        trozos.append((ALTURAS[h], (rampa(TURQUESA, 0.2 + 0.55 * h / 7),)))
    if len(datos) < ancho:
        trozos.append((" " * (ancho - len(datos)), NORMAL))
    return _juntar(trozos)


def medidor(velocimetro, ancho, t=None):
    """Oleaje de ocho alturas montado sobre la velocidad real.

    La altura media es la velocidad actual respecto al máximo que se ha visto;
    encima va un rizo senoidal para que se mueva, y cada celda se aclara al
    subir, como la cresta de una ola. Un peer lento se nota porque el agua se
    queda abajo y oscura, no porque deje de moverse. Trozos (texto, color).
    """
    if ancho <= 0:
        return []
    if velocimetro.actual <= 0:
        return [(ALTURAS[0] * ancho, LINEA)]
    _anima()
    t = time.monotonic() if t is None else t
    techo = max(velocimetro.historial, default=0.0) or velocimetro.actual
    nivel = min(1.0, velocimetro.actual / (techo or 1.0))
    trozos = []
    for i in range(ancho):
        onda = (math.sin(t * 5.1 + i * 0.85) + math.sin(t * 2.7 - i * 0.4) + 2) / 4
        v = nivel * (0.55 + 0.45 * onda)
        h = max(0, min(7, int(v * 7 + 0.5)))
        trozos.append((ALTURAS[h], (rampa(OCEANO, 0.15 + 0.65 * h / 7),)))
    return _juntar(trozos)


def ola(ancho, t=None, altura=1.0, colores=OCEANO):
    """La superficie del agua moviéndose, para las esperas sin medida: leer la
    biblioteca, revisar la instalación. Trozos (texto, color)."""
    if ancho <= 0:
        return []
    _anima()
    t = time.monotonic() if t is None else t
    trozos = []
    for i in range(ancho):
        v = (math.sin(t * 2.4 - i * 0.42) + 0.6 * math.sin(t * 1.5 + i * 0.21) + 1.6) / 3.2
        h = max(0, min(7, int(v * altura * 7 + 0.5)))
        trozos.append((ALTURAS[h], (rampa(colores, 0.1 + 0.85 * h / 7),)))
    return _juntar(trozos)


def brillo(texto, t=None, color=CIELO):
    """Un reflejo que recorre el texto de izquierda a derecha: lo que se está
    cargando. Trozos (texto, color)."""
    if not texto:
        return []
    _anima()
    t = time.monotonic() if t is None else t
    colores = RAMPA.get(color, CIELOS)
    centro = ((t / 1.8) % 1.0) * (len(texto) + 12) - 6
    trozos = []
    for i, c in enumerate(texto):
        k = math.exp(-((i - centro) / 3.2) ** 2)
        trozos.append((c, (rampa(colores, 0.5 + 0.5 * k),)))
    return _juntar(trozos)


def destello(y, x0, x1, u, tonos=ARRECIFE):
    """Un destello del color de `tonos` cruza el texto de la fila de izquierda
    a derecha, como el sol en la cresta de una ola: u va de 0 (entra) a 1 (ya
    pasó). Toca sólo el color de las letras de lo ya pintado; en los tonos
    claros la paleta tiene pasos de sobra para que el brillo corra suave."""
    if _PALETA["colores"] < 256 or u >= 1 or not 0 <= y < LIENZO.alto:
        return
    cresta = tonos[-2]
    centro = x0 - 6 + (x1 - x0 + 12) * suave(u)
    fila = LIENZO.filas[y]
    for cx in range(max(0, x0), min(LIENZO.ancho, x1)):
        k = math.exp(-((cx - centro) / 3.5) ** 2)
        c = fila[cx]
        if k < 0.06 or c[0] == " ":
            continue
        actual = TINTA_TERMINAL if c[1] < 0 else c[1]
        fila[cx] = (c[0], mezclar(actual, cresta, 0.9 * k), c[2], c[3] | NEGRITA)


def cargando(texto, t=None, color=CIELO):
    """Burbujas y el texto con su reflejo: «leyendo la biblioteca…»."""
    return [(girador(t) + " ", color)] + brillo(texto, t, color)


def espera(win, y0, alto, ancho, texto, t=None):
    """Una espera que ocupa la vista: una ola en medio y debajo el texto con
    su reflejo. Para lo que tarda sin poder decir cuánto falta."""
    t = time.monotonic() if t is None else t
    ancho_ola = max(8, min(34, ancho - 8))
    y = y0 + max(0, alto // 2 - 2)
    escribir_trozos(win, y, (ancho - ancho_ola) // 2, ola(ancho_ola, t, 0.9))
    escribir_trozos(win, y + 2, max(0, (ancho - len(texto) - 2) // 2), cargando(texto, t))


def caret(t=None):
    """(glifo, color) del cursor de un campo de texto: late suave en vez de
    encenderse y apagarse a golpes."""
    _anima()
    t = time.monotonic() if t is None else t
    u = (math.sin(t * 2 * math.pi / 1.1) + 1) / 2
    return "▍", (rampa(OCEANO, 0.3 + 0.55 * u),)


def marquesina(texto, ancho, t=None):
    """Desplaza el texto si no cabe; si cabe, lo deja quieto."""
    texto = texto or ""
    if len(texto) <= ancho:
        return texto.ljust(max(0, ancho))
    _anima()
    t = time.monotonic() if t is None else t
    doble = texto + "   ·   "
    salto = int(t * 4) % len(doble)
    return (doble + doble)[salto:salto + ancho]


def pintar_fila(win, y, x, ancho, izq, der=()):
    """Una fila hecha de trozos (texto, color): `izq` de corrido y `der`
    pegado al borde derecho. El último trozo de `izq` se recorta con «…»."""
    if ancho <= 0:
        return
    ancho_der = sum(len(t) for t, _c in der)
    limite = ancho - (ancho_der + 1 if der else 0)
    xx = x
    for texto, color in izq:
        sitio = limite - (xx - x)
        if sitio <= 0:
            break
        xx = escribir(win, y, xx, elipsis(texto, sitio),
                      NORMAL if color is None else color)
    xx = x + max(0, ancho - ancho_der)
    for texto, color in der:
        xx = escribir(win, y, xx, texto, NORMAL if color is None else color)


def pintar_seccion(win, y, x, ancho, alto, titulo, filas, foco=None):
    """Un título con su regla y debajo las filas que quepan en `alto`.

    Si no caben todas, la última línea resume cuántas quedan fuera; con `foco`
    se recorta la ventana para que esa fila siga a la vista.
    """
    if alto < 1 or ancho <= 0:
        return
    nombre, _, detalle = titulo.partition(" · ")
    xx = escribir(win, y, x, elipsis(nombre, ancho), BLANCO)
    if detalle:
        xx = escribir(win, y, xx, elipsis(" · " + detalle, x + ancho - xx), TENUE)
    escribir(win, y, xx + 1, VACIO * max(0, x + ancho - xx - 1), LINEA)
    sitio = alto - 1
    n = len(filas)
    if sitio <= 0:
        return
    if n <= sitio:
        desde, hasta, resumen = 0, n, None
    else:
        cuerpo = sitio - 1
        desde = (0 if foco is None
                 else max(0, min(foco - max(0, cuerpo - 2), n - cuerpo)))
        hasta = min(n, desde + cuerpo)
        partes = ([f"{desde} antes"] if desde else []) + \
                 ([f"{n - hasta} más"] if hasta < n else [])
        resumen = "… " + " · ".join(partes)
    for i, (izq, der) in enumerate(filas[desde:hasta]):
        pintar_fila(win, y + 1 + i, x, ancho, izq, der)
    if resumen:
        escribir(win, y + 1 + (hasta - desde), x, elipsis(resumen, ancho), TENUE)


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


def segundos_desde(iso):
    try:
        return max(0.0, (datetime.now() - datetime.fromisoformat(iso)).total_seconds())
    except (ValueError, TypeError):
        return 0.0


def duracion_entre(iso_a, iso_b):
    """'2:40' o '1 h 05 min' entre dos marcas ISO."""
    try:
        s = (datetime.fromisoformat(iso_b) - datetime.fromisoformat(iso_a)).total_seconds()
    except (ValueError, TypeError):
        return ""
    if s < 0:
        return ""
    if s < 3600:
        return duracion(s)
    return f"{int(s // 3600)} h {int(s % 3600 // 60):02d} min"


# ─── Velocímetro ──────────────────────────────────────────────────────────────

class Velocimetro:
    """Bytes/s a partir de un contador acumulado que se muestrea cada tanto."""

    def __init__(self, ventana=48, intervalo=0.5):
        self.historial = deque(maxlen=ventana)
        self.intervalo = intervalo
        self.actual = 0.0
        self._previo = None
        self._t = 0.0
        self._mostrado = 0.0

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

    def estimado(self):
        """Los bytes de ahora mismo, interpolados entre dos lecturas para que
        el contador suba de corrido y no a saltos. Nunca retrocede."""
        if self._previo is None:
            return 0
        estimacion = self._previo + self.actual * (time.monotonic() - self._t)
        self._mostrado = max(self._mostrado, estimacion)
        return int(self._mostrado)

    def reiniciar(self):
        self.historial.clear()
        self.actual = 0.0
        self._previo = None
        self._t = 0.0
        self._mostrado = 0.0


# ─── Datos ────────────────────────────────────────────────────────────────────

def calidad_corta(detalle):
    """La calidad de la fuente en pocas letras, sacada del `detalle` que el
    plugin guarda en el candidato («15 pistas, 16-bit / 44.1 kHz»):
    «FLAC 16/44.1», «FLAC 24/96»; «FLAC» si no se sabe cuántos bits, y ""
    si no hay detalle. Es de la carpeta entera, no de cada pista."""
    if not detalle:
        return ""
    m = PATRON_CALIDAD.search(detalle)
    if not m or m.group(1) == "?":
        return "FLAC"
    if m.group(2):
        return f"FLAC {m.group(1)}/{m.group(2)}"
    return f"FLAC {m.group(1)}"


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
        self.bajados = []        # (archivo, bytes) ya completos de la petición en curso
        self.incompletos = []    # (archivo, bytes) que Nicotine+ está escribiendo ahora
        self.muestreado_en = 0.0
        self.velocimetro = Velocimetro()
        # Pista a pista, de la petición en curso:
        #   pistas_activas     [{"archivo", "bytes", "total", "velocidad"}] de lo
        #                      que baja ahora; "total" es None si Nicotine+ aún no
        #                      ha dicho cuánto pesa (nunca se estima)
        #   recien_bajadas     {archivo: monotonic} de las que acaban de terminar
        #   bytes_album_total  lo que pesa el álbum entero, o None si falta el
        #                      tamaño de alguna pista
        self.pistas_activas = []
        self.recien_bajadas = {}
        self.bytes_album_total = None
        self.cambios = {}        # id -> (estado nuevo, monotonic): para el parpadeo
        self.eventos = deque()   # peticiones que cambiaron de estado: para los avisos
        self.aviso = ""
        self.log_nuevas = (0, 0.0)  # cuántas líneas acaban de llegar y cuándo

        self._mtime_cola = -1.0
        self._tam_log = -1
        self._ciclos = 0
        self._historia_clave = None
        self._historia = []
        self._candidatos = []
        self._velocimetros = {}          # archivo incompleto -> Velocimetro
        self._id_activa = None
        self._mtime_transferencias = None
        self._transferencias = {}        # id de petición -> {archivo: tamaño}
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
            nueva = comun.leer_cola()
            self._detectar_cambios(nueva)
            self.cola = nueva

        try:
            tam = os.path.getsize(comun.ARCHIVO_LOG)
        except OSError:
            tam = 0
        if tam != self._tam_log:
            self._tam_log = tam
            try:
                with open(comun.ARCHIVO_LOG, encoding="utf-8", errors="replace") as f:
                    nuevo = [l.rstrip("\n") for l in f.readlines()[-800:]]
            except OSError:
                nuevo = []
            # El log sólo crece: lo que haya después de la última línea que ya
            # teníamos es nuevo (la primera lectura no cuenta).
            if self.log and nuevo and nuevo[-1] != self.log[-1]:
                ultima = self.log[-1]
                for i in range(len(nuevo) - 1, -1, -1):
                    if nuevo[i] == ultima:
                        self.log_nuevas = (len(nuevo) - 1 - i, time.monotonic())
                        break
            self.log = nuevo

        if self._ciclos % 4 == 1:                # cada segundo
            self._mirar_descargas()
        if self._ciclos % 8 == 2:                # cada dos segundos
            self.destinos = fiio.detectar_destinos()
        if self._ciclos % 12 == 3:               # cada tres segundos
            self.pid_nicotine = comun.pid_nicotine()

    def _detectar_cambios(self, nueva):
        """Qué petición cambió de estado desde la lectura anterior. La primera
        lectura no cuenta: lo viejo no merece aviso."""
        previos = {p["id"]: p["estado"] for p in self.peticiones()}
        ahora = time.monotonic()
        for p in nueva.get("peticiones", []):
            antes = previos.get(p["id"])
            if antes is not None and antes != p["estado"]:
                self.cambios[p["id"]] = (p["estado"], ahora)
                self.eventos.append(p)

    def _mirar_descargas(self):
        sobras, sueltos = [], []
        activa = self.peticion_en_curso()
        try:
            entradas = sorted(os.listdir(comun.DIR_DESCARGAS))
        except OSError:
            entradas = []
        for nombre in entradas:
            ruta = os.path.join(comun.DIR_DESCARGAS, nombre)
            if activa is not None and nombre == activa["id"]:
                continue           # a medio bajar: no es algo que organizar
            if os.path.isdir(ruta):
                sobras.append(ruta)
            elif (nombre.lower().endswith(biblioteca.EXT_AUDIO)
                    and not nombre.startswith("._")):
                sueltos.append(ruta)
        self.sobras, self.sueltos = sobras, sueltos

        if activa is None or activa["id"] != self._id_activa:
            self._velocimetros = {}
            self.pistas_activas, self.recien_bajadas = [], {}
            self.bytes_album_total = None
        self._id_activa = activa["id"] if activa is not None else None
        if activa is None:
            self.velocimetro.reiniciar()
            self.bajados, self.incompletos = [], []
            return

        # Lo ya completo cae en .descargas/<id>; lo que está bajando ahora
        # mismo lo escribe Nicotine+ en su carpeta de incompletos, con un
        # prefijo INCOMPLETE<md5>. Sumando los dos, los bytes suben de corrido.
        carpeta = os.path.join(comun.DIR_DESCARGAS, activa["id"])
        bajados = []
        for base, _dirs, archivos in os.walk(carpeta):
            for archivo in archivos:
                try:
                    bajados.append((archivo, os.path.getsize(os.path.join(base, archivo))))
                except OSError:
                    pass
        bajados.sort()

        incompletos = []
        ahora = time.time()
        try:
            with os.scandir(DIR_INCOMPLETOS) as entradas:
                for e in entradas:
                    m = PATRON_INCOMPLETO.match(e.name)
                    if not m or not e.is_file():
                        continue
                    st = e.stat()
                    if ahora - st.st_mtime > 120:      # restos de otra ocasión
                        continue
                    incompletos.append((m.group(1), st.st_size))
        except OSError:
            pass
        incompletos.sort()

        # Si Nicotine+ ya apuntó los archivos de esta petición, el incompleto
        # que downloads.json pone en otra carpeta y no en ésta es de otra
        # descarga. Lo que no nombra se queda: puede que aún no lo haya guardado.
        self._leer_transferencias()
        tamanos = self._transferencias.get(activa["id"], {})
        if tamanos:
            ajenos = set()
            for id_pet, nombres in self._transferencias.items():
                if id_pet != activa["id"]:
                    ajenos.update(nombres)
            incompletos = [(n, b) for n, b in incompletos
                           if n in tamanos or n not in ajenos]

        self._mirar_pistas(activa, bajados, incompletos, tamanos)
        self.bajados, self.incompletos = bajados, incompletos
        self.muestreado_en = time.monotonic()
        self.velocimetro.muestra(sum(b for _n, b in bajados)
                                 + sum(b for _n, b in incompletos))

    def _mirar_pistas(self, activa, bajados, incompletos, tamanos):
        """Bytes, tamaño y velocidad de cada pista que baja ahora, y cuáles
        acaban de pasar de incompletas a completas."""
        ahora = time.monotonic()
        nombres = {n for n, _b in incompletos}
        completos = {n for n, _b in bajados}
        recien = {n: t for n, t in self.recien_bajadas.items()
                  if ahora - t < SEGUNDOS_RECIEN}
        for pista in self.pistas_activas:
            n = pista["archivo"]
            if n not in nombres and n in completos and n not in recien:
                recien[n] = ahora

        velocimetros = {n: v for n, v in self._velocimetros.items() if n in nombres}
        activas = []
        for n, b in incompletos:
            v = velocimetros.setdefault(n, Velocimetro())
            v.muestra(b)
            activas.append({"archivo": n, "bytes": b, "total": tamanos.get(n),
                            "velocidad": v.actual})
        self._velocimetros = velocimetros
        self.pistas_activas, self.recien_bajadas = activas, recien

        total = None
        esperadas = (activa.get("progreso") or {}).get("total")
        if (tamanos and None not in tamanos.values()
                and (not esperadas or esperadas == len(tamanos))):
            total = sum(tamanos.values())
        self.bytes_album_total = total

    def _leer_transferencias(self):
        """Los tamaños que Nicotine+ guardó en downloads.json, por petición.
        Sólo se relee si cambió. Nicotine+ lo reescribe en el sitio tras pasar
        el anterior a downloads.json.old: si se pilla a medias, si falta o si
        está roto, vale el .old, como hace el propio Nicotine+; si tampoco
        sirve, no hay tamaños."""
        try:
            mtime = os.path.getmtime(ARCHIVO_TRANSFERENCIAS) if ARCHIVO_TRANSFERENCIAS else None
        except OSError:
            mtime = None
        if mtime == self._mtime_transferencias:
            return
        self._mtime_transferencias = mtime
        por_peticion = {}
        filas = []
        for archivo in ((ARCHIVO_TRANSFERENCIAS, ARCHIVO_TRANSFERENCIAS + ".old")
                        if ARCHIVO_TRANSFERENCIAS else ()):
            try:
                with open(archivo, encoding="utf-8") as f:
                    filas = json.load(f)
            except (OSError, ValueError):
                filas = None
            if isinstance(filas, list):
                break
        if not isinstance(filas, list):
            filas = []
        for fila in filas:
            if not isinstance(fila, list) or len(fila) < 3:
                continue
            ruta, carpeta = fila[1], fila[2]
            if not isinstance(ruta, str) or not isinstance(carpeta, str) or not carpeta:
                continue
            # La carpeta de destino es .descargas/<id>: el id basta, venga la
            # ruta de dentro del snap o de fuera.
            id_pet = os.path.basename(os.path.normpath(carpeta))
            tam = fila[4] if len(fila) > 4 else None
            if isinstance(tam, bool) or not isinstance(tam, (int, float)) or tam <= 0:
                tam = None
            nombre = self._nombre_local(ruta)
            if nombre:
                por_peticion.setdefault(id_pet, {})[nombre] = (
                    int(tam) if tam is not None else None)
        self._transferencias = por_peticion

    @staticmethod
    def _nombre_local(ruta_remota):
        """El nombre con el que Nicotine+ guarda un archivo remoto: lo que va
        tras la última «\\», con lo ilegal cambiado por «_» (utils.clean_file)."""
        nombre = PATRON_NOMBRE_ILEGAL.sub("_", ruta_remota.rpartition("\\")[2])
        limpio = nombre.lstrip(" ").rstrip(". ")
        return limpio or "_" * len(nombre)

    # ── consultas ──
    def peticiones(self):
        return self.cola.get("peticiones", [])

    def activas(self):
        return [p for p in self.peticiones() if p["estado"] in comun.ACTIVOS]

    def peticion_en_curso(self):
        """La que baja ahora; si ninguna baja, la que se está buscando."""
        en_curso = [p for p in self.peticiones() if p["estado"] in comun.EN_CURSO]
        for pet in en_curso:
            if pet["estado"] == comun.DESCARGANDO:
                return pet
        return en_curso[0] if en_curso else None

    def nicotine_vivo(self):
        return self.pid_nicotine is not None

    def historia(self, id_pet):
        """Las líneas del log que hablan de esa petición.

        Las que llevan el id son fáciles. Las que no ([candidato], [archivo],
        [descarte]) se atribuyen a la petición que el plugin tenía entre manos,
        que es la del último [buscar]/[descargar]/[listo] visto. [cola] y
        [cancelada] llevan id pero no cambian de manos: los escribe la CLI.
        """
        clave = (id_pet, self._tam_log)
        if clave == self._historia_clave:
            return self._historia
        actual, salida = None, []
        for linea in self.log:
            m = PATRON_LOG.match(linea)
            etiqueta = m.group(2).split()[0] if m else ""
            con_id = PATRON_ID.search(linea)
            if con_id:
                if etiqueta not in ("cola", "cancelada", "plugin"):
                    actual = con_id.group(1)
                pertenece = con_id.group(1) == id_pet
            else:
                pertenece = actual == id_pet and etiqueta != "plugin"
            if pertenece:
                salida.append(linea)
        self._historia_clave, self._historia = clave, salida
        self._candidatos = self._leer_candidatos(salida)
        return self._historia

    def candidatos(self, id_pet):
        """Las fuentes que el plugin puntuó, sacadas de las líneas [candidato]."""
        self.historia(id_pet)
        return self._candidatos

    @staticmethod
    def _leer_candidatos(lineas):
        vistos = {}
        for linea in lineas:
            m = PATRON_LOG.match(linea)
            if not m or m.group(2) != "candidato":
                continue
            partes = re.split(r"\s{2,}", m.group(3).strip())
            if len(partes) < 3:
                continue
            try:
                puntos = int(partes[0])
            except ValueError:
                continue
            cand = {"puntos": puntos, "usuario": partes[1], "detalle": partes[2],
                    "carpeta": "  ".join(partes[3:])}
            vistos[(cand["usuario"], cand["carpeta"])] = cand   # la última ronda manda
        return sorted(vistos.values(), key=lambda c: -c["puntos"])


# ─── Trabajo en segundo plano ─────────────────────────────────────────────────

class Trabajo:
    """Una tarea larga (copiar al FiiO, organizar) corriendo en un hilo."""

    class Copia:
        """Todo lo que se sabe de una copia a la tarjeta, álbum por álbum y
        pista por pista. Lo llena copiar_fiio con el plan antes de lanzar el
        hilo, lo mueven los eventos de fiio.copiar() y, cuando el hilo acaba,
        se queda en app.ultima_copia (expulsar no lo toca).

        Cada álbum es un dict con rel, artista, album, pistas (nombres),
        bytes, bytes_hechos, estado (ESTADOS), hechas (pistas escritas),
        orden_ok (None si no se pudo mirar) y motivo (por qué se omitió,
        falló o se paró)."""

        ESTADOS = ("espera", "copiando", "copiado", "omitido", "error", "abortado")
        # Segundos de muestras que hacen falta antes de prometer un final
        MUESTRAS_MINIMAS_S = 2.0

        def __init__(self, albumes, simulacion=False, rehacer=False, etiqueta="",
                     montaje=""):
            self.albumes = []
            for a in albumes:
                self.albumes.append({
                    "rel": a["rel"],
                    "artista": a.get("artista", ""),
                    "album": a.get("album", ""),
                    "pistas": [p if isinstance(p, str) else p.get("archivo", str(p))
                               for p in a.get("pistas", [])],
                    "bytes": a.get("bytes", 0),
                    "bytes_hechos": 0,
                    "estado": "espera",
                    "hechas": 0,
                    "orden_ok": None,
                    "motivo": "",
                })
            self._por_rel = {a["rel"]: i for i, a in enumerate(self.albumes)}
            self.simulacion = simulacion
            self.rehacer = rehacer
            self.etiqueta = etiqueta
            self.montaje = montaje
            self.album_indice = -1     # el álbum en curso, índice en albumes
            self.pista = ""
            self.pista_indice = 0      # 1..n dentro del álbum; 0 = ninguna aún
            self.pista_bytes = 0
            self.pista_hechos = 0
            self.hechos_bytes = 0
            self.velocimetro = Velocimetro(intervalo=0.4)
            self.inicio = time.time()  # hora de reloj, para «empezó HH:MM»
            self.fin = None
            self._t0 = time.monotonic()
            self._t1 = None
            self.errores = []          # [{"rel", "error"}]
            self.abortado = False
            self.resumen = None        # lo que devolvió fiio.copiar()
            self.expulsada = None      # True/False tras intentar expulsar

        # ── lo que se lee al pintar ──
        @property
        def terminada(self):
            return self.fin is not None

        def actual(self):
            """El álbum que se está copiando (o el último que se empezó)."""
            if 0 <= self.album_indice < len(self.albumes):
                return self.albumes[self.album_indice]
            return None

        def contar(self, estado):
            return sum(1 for a in self.albumes if a["estado"] == estado)

        @property
        def pistas_total(self):
            return sum(len(a["pistas"]) for a in self.albumes
                       if a["estado"] != "omitido")

        @property
        def pistas_hechas(self):
            return sum(a["hechas"] for a in self.albumes)

        @property
        def bytes_pendientes(self):
            """Lo que queda por escribir: los álbumes en espera y el resto del
            que está en curso. Los omitidos y los que fallaron no cuentan."""
            pendientes = 0
            for a in self.albumes:
                if a["estado"] == "espera":
                    pendientes += a["bytes"]
                elif a["estado"] == "copiando":
                    pendientes += max(0, a["bytes"] - a["bytes_hechos"])
            return pendientes

        @property
        def total_bytes(self):
            """Lo escrito más lo que falta: baja si un álbum se omite o falla."""
            return self.hechos_bytes + self.bytes_pendientes

        def frac(self):
            """De 0 a 1. Por bytes en una copia de verdad; una simulación no
            escribe bytes, así que va por pistas."""
            if not self.simulacion and self.total_bytes:
                return min(1.0, self.hechos_bytes / self.total_bytes)
            total = self.pistas_total
            return min(1.0, self.pistas_hechas / total) if total else 0.0

        def velocidad(self):
            """Bytes/s medios de lo medido hasta ahora (la ventana del
            velocímetro), o None sin un par de segundos de muestras."""
            historial = list(self.velocimetro.historial)
            if (self.simulacion or not historial
                    or len(historial) * self.velocimetro.intervalo < self.MUESTRAS_MINIMAS_S):
                return None
            media = sum(historial) / len(historial)
            return media if media > 0 else None

        def restante(self):
            """Segundos que faltan (bytes pendientes / velocidad media), o None
            si todavía no se sabe o ya terminó."""
            if self.terminada:
                return None
            velocidad = self.velocidad()
            if velocidad is None:
                return None
            return self.bytes_pendientes / velocidad

        def termina(self):
            """Hora de reloj estimada del final, o None."""
            restante = self.restante()
            return None if restante is None else time.time() + restante

        def duracion(self):
            fin = self._t1 if self._t1 is not None else time.monotonic()
            return max(0.0, fin - self._t0)

        def velocidad_media(self):
            """Bytes/s de la copia entera, ya terminada, contando las pausas
            de sync: lo que sirve para estimar la próxima en esa tarjeta."""
            if (self.simulacion or not self.terminada or self.hechos_bytes <= 0
                    or self.duracion() < self.MUESTRAS_MINIMAS_S):
                return None
            return self.hechos_bytes / self.duracion()

        # ── lo que mueve el hilo de la copia ──
        def _album(self, rel):
            i = self._por_rel.get(rel)
            if i is None:
                # fiio.copiar() habló de un álbum que no venía en el plan
                self.albumes.append({"rel": rel, "artista": "", "album": rel,
                                     "pistas": [], "bytes": 0, "bytes_hechos": 0,
                                     "estado": "espera", "hechas": 0,
                                     "orden_ok": None, "motivo": ""})
                i = self._por_rel[rel] = len(self.albumes) - 1
            return i

        def evento(self, ev):
            tipo = ev.get("t")
            if tipo == "album":
                self.album_indice = self._album(ev["rel"])
                a = self.albumes[self.album_indice]
                a["estado"] = "copiando"
                a["hechas"] = 0
                a["bytes_hechos"] = 0
                if ev.get("bytes"):
                    a["bytes"] = ev["bytes"]
                self.pista = ""
                self.pista_indice = self.pista_bytes = self.pista_hechos = 0
            elif tipo == "omitido":
                a = self.albumes[self._album(ev["rel"])]
                a["estado"] = "omitido"
                a["motivo"] = ev.get("motivo", "")
            elif tipo == "pista":
                a = self.albumes[self._album(ev["rel"])]
                if not a["pistas"] and ev.get("total"):
                    a["pistas"] = [""] * ev["total"]
                self.pista = ev["archivo"]
                self.pista_indice = ev["indice"]
                self.pista_bytes = ev.get("bytes", 0)
                self.pista_hechos = 0
            elif tipo == "bytes":
                n = ev["n"]
                self.pista_hechos += n
                self.hechos_bytes += n
                a = self.actual()
                if a is not None:
                    a["bytes_hechos"] += n
                self.velocimetro.muestra(self.hechos_bytes)
            elif tipo == "pista_ok":
                self.pista_hechos = self.pista_bytes
                a = self.actual()
                if a is not None:
                    a["hechas"] += 1
            elif tipo == "orden":
                a = self.albumes[self._album(ev["rel"])]
                a["orden_ok"] = ev["ok"]
                if ev["ok"] is False:
                    a["motivo"] = "el orden físico no quedó como se esperaba"
            elif tipo == "album_ok":
                a = self.albumes[self._album(ev["rel"])]
                a["estado"] = "copiado"
            elif tipo == "error":
                a = self.albumes[self._album(ev["rel"])]
                a["estado"] = "error"
                a["motivo"] = ev.get("error", "")
                self.errores.append({"rel": ev["rel"], "error": ev.get("error", "")})
            elif tipo == "abortado":
                a = self.albumes[self._album(ev["rel"])]
                a["estado"] = "abortado"
                a["motivo"] = f"se paró tras {a['hechas']} de {len(a['pistas'])} pistas"
                self.abortado = True

        def cerrar(self, resumen=None, cancelado=False):
            """El hilo terminó: hora de fin y el álbum que quedó colgado."""
            self.fin = time.time()
            self._t1 = time.monotonic()
            self.resumen = resumen
            if cancelado or (resumen or {}).get("abortado"):
                self.abortado = True
            for a in self.albumes:
                if a["estado"] != "copiando":
                    continue
                if self.abortado:
                    a["estado"] = "abortado"
                    a["motivo"] = (f"se paró tras {a['hechas']} de "
                                   f"{len(a['pistas'])} pistas")
                else:
                    a["estado"] = "error"
                    a["motivo"] = a["motivo"] or "la copia se cortó a medias"

        # ── la velocidad medida de cada tarjeta, para estimar la próxima ──
        @staticmethod
        def archivo_velocidades():
            return os.path.join(comun.DIR_PIPELINE, "fiio-velocidad.json")

        @classmethod
        def leer_velocidades(cls):
            """{etiqueta: bytes/s} de la última copia de verdad a cada tarjeta."""
            import json
            try:
                with open(cls.archivo_velocidades(), encoding="utf-8") as f:
                    datos = json.load(f)
            except (OSError, ValueError):
                return {}
            if not isinstance(datos, dict):
                return {}
            return {k: float(v) for k, v in datos.items()
                    if isinstance(v, (int, float)) and v > 0}

        @classmethod
        def guardar_velocidades(cls, velocidades):
            import json
            ruta = cls.archivo_velocidades()
            try:
                os.makedirs(os.path.dirname(ruta), exist_ok=True)
                with open(ruta + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(velocidades, f, ensure_ascii=False, indent=1)
                os.replace(ruta + ".tmp", ruta)
            except OSError:
                pass

    def __init__(self):
        self.titulo = ""
        self.clase = "copia"     # "copia" se pinta en la vista FiiO; el resto, en Cola
        self.lineas = deque(maxlen=400)
        self.cancelar = threading.Event()
        self.resumen = None
        self.terminado_en = 0.0
        self.hilo = None
        self.velocimetro = Velocimetro(intervalo=0.4)
        self.copia = None        # Trabajo.Copia mientras se copia a la tarjeta

        # progreso de una copia (lo pinta panel_trabajo; el modelo es self.copia)
        self.album = ""
        self.album_pistas = 0
        self.pista = ""
        self.pista_indice = 0
        self.pista_bytes = 0
        self.pista_hechos = 0
        self.total_bytes = 0
        self.hechos_bytes = 0
        self.orden = []          # pistas ya escritas, en orden
        self.orden_t = []        # cuándo quedó escrita cada una (para pintarla)
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

    def lanzar(self, titulo, funcion, clase="copia", copia=None):
        """`copia` es el Trabajo.Copia ya lleno con el plan, si es una copia."""
        if self.activo:
            return False
        self.titulo = titulo
        self.clase = clase
        self.copia = copia
        self.lineas.clear()
        self.cancelar.clear()
        self.resumen = None
        self.terminado_en = 0.0
        self.album = self.pista = ""
        self.album_pistas = self.pista_indice = 0
        self.pista_bytes = self.pista_hechos = 0
        self.total_bytes = self.hechos_bytes = 0
        self.orden = []
        self.orden_t = []
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
        if self.copia is not None and not self.copia.terminada:
            self.copia.cerrar(self.resumen, self.cancelar.is_set())
        self.terminado_en = time.monotonic()

    def evento(self, ev):
        """Recibe los diccionarios que emite fiio.copiar(): todos van al
        modelo de la copia; los de siempre, además, a los campos de arriba."""
        if self.copia is not None:
            self.copia.evento(ev)
        tipo = ev.get("t")
        if tipo == "album":
            self.album = ev["rel"]
            self.album_pistas = ev["pistas"]
            self.orden = []
            self.orden_t = []
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
            self.orden_t.append(time.monotonic())
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
    ("afplay", []),                     # viene con macOS
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
        self.ruta = ""
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
            return "no hay ningún reproductor instalado (mpv, ffplay, gst-play-1.0…)"
        self.parar()
        try:
            self.proceso = subprocess.Popen(
                self.orden + [ruta],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as error:
            return f"no se pudo reproducir: {error}"
        self.titulo = titulo or os.path.basename(ruta)
        self.ruta = ruta
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


def _sin_numero(archivo):
    """'07 The Sign.flac' → 'The Sign': la columna ya lleva el número."""
    nombre = os.path.splitext(archivo)[0]
    limpio = re.sub(r"^\s*\d{1,3}\s*[-._]?\s*", "", nombre)
    return limpio or nombre


def titulo_peticion(pet):
    """«Artista — Álbum» cuando se sabe cada cosa; si no, lo que se pidió."""
    artista, album = pet.get("artista"), pet.get("album")
    if artista and album:
        return f"{artista} — {album}"
    return pet.get("consulta") or ""


def _numero_pista(archivo):
    """'07 The Sign.flac' → '07'; '' si el nombre no empieza por un número."""
    m = re.match(r"^\s*(\d{1,3})", archivo)
    return f"{int(m.group(1)):02d}" if m else ""


def _pares_teclas(win, y, x, pares):
    """«a pide un álbum   r reintentar»: la tecla en agua y negrita, lo que
    hace en tenue. Devuelve la x siguiente."""
    for i, (tecla, texto) in enumerate(pares):
        x = escribir(win, y, x + (2 if i else 0), tecla, AGUA, True)
        x = escribir(win, y, x + 1, texto, TENUE)
    return x


class VistaCola(Vista):
    """Lo que se baja ahora, un bloque por álbum con sólo las pistas que
    están bajando, y debajo de una raya una línea por cada petición que
    espera turno, busca fuente o falló. Lo terminado se va (queda en el Log y
    en la Biblioteca); la ficha entera de una petición se abre encima con ↵."""
    nombre = "Cola"
    # Segundos que un álbum recién terminado se queda, en verde, antes de irse.
    SALIDA = 1.6
    # En una ventana muy ancha, lo de la derecha (velocidad, calidad, r y x)
    # no se va al otro extremo: el bloque se lee de un vistazo.
    ANCHO_MAXIMO = 104

    def __init__(self):
        self.cursor = 0
        self.elegida = None      # id de la petición del cursor: la lista cambia sola
        self.desplazado = 0

    # ── qué se enseña ──
    def bloques(self, app):
        """Las que se bajan ahora, y las que acaban de terminar mientras se van."""
        ahora = time.monotonic()
        salida = []
        for p in app.datos.peticiones():
            if p["estado"] == comun.DESCARGANDO:
                salida.append(p)
            elif p["estado"] == comun.LISTO:
                cambio = app.datos.cambios.get(p["id"])
                if cambio and cambio[0] == comun.LISTO and ahora - cambio[1] < self.SALIDA:
                    salida.append(p)
        return salida

    def lineas(self, app):
        """Una línea cada una: lo que busca fuente, lo que espera turno (en el
        orden de la cola) y lo que falló, que se queda hasta r o x."""
        peticiones = app.datos.peticiones()
        return [p for estado in (comun.BUSCANDO, comun.PENDIENTE, comun.FALLIDO)
                for p in peticiones if p["estado"] == estado]

    def lista(self, app):
        return self.bloques(app) + self.lineas(app)

    def seleccion(self, app):
        lista = self.lista(app)
        if not lista:
            self.elegida = None
            return None
        ids = [p["id"] for p in lista]
        if self.elegida in ids:
            self.cursor = ids.index(self.elegida)
        self.cursor = max(0, min(self.cursor, len(lista) - 1))
        self.elegida = ids[self.cursor]
        return lista[self.cursor]

    def resumen(self, app):
        activas = app.datos.activas()
        if not activas:
            return ""
        bajando = sum(1 for p in activas if p["estado"] == comun.DESCARGANDO)
        return f"{bajando} bajando · {len(activas) - bajando} en espera"

    def teclas(self, app):
        pet = self.seleccion(app)
        fallida = pet is not None and pet["estado"] == comun.FALLIDO
        return [("a", "pedir"), ("i", "importar csv"),
                ("x", "quitar" if fallida else "cancelar"), ("↵", "detalle")]

    def ayuda(self, app):
        """Lo que sale en `?`: las del pie y las que no caben en él."""
        return [("↑↓", "moverse"), ("a", "pedir"), ("i", "importar csv"),
                ("x", "cancelar, o quitar la fallida"), ("r", "reintentar la fallida"),
                ("↵", "la ficha entera"), ("o", "organizar"), ("c", "ir al FiiO"),
                ("L", "limpiar lo terminado")]

    # ── dibujo ──
    def dibujar(self, app, y0, alto, ancho):
        win = app.pantalla
        # Organizar no es cosa del FiiO: su progreso se ve aquí, con la cola.
        if app.trabajo.clase != "copia" and app.trabajo.reciente():
            VistaFiio.panel_trabajo(app, y0, alto, ancho)
            return

        t = time.monotonic()
        ancho_total, ancho = ancho, min(ancho, self.ANCHO_MAXIMO)
        bloques = self.bloques(app)
        lineas = self.lineas(app)
        elegida = self.seleccion(app)
        elegida = elegida["id"] if elegida is not None else None

        # Cada fila: (id de la petición si es su primera fila, qué pinta).
        filas = []
        if not bloques:
            filas.append((None, lambda y: _pares_teclas(
                win, y, escribir(win, y, 2, "Nada bajando ahora.  ", TENUE),
                [("a", "pide un álbum")])))
            filas.append((None, None))
        for pet in bloques:
            filas += self._bloque(app, pet, ancho, t)
            filas.append((None, None))
        if lineas:
            filas.append((None, lambda y: escribir(win, y, 1, VACIO * (ancho - 2), LINEA)))
            for pet in lineas:
                filas.append((pet["id"], lambda y, pet=pet: self._linea(
                    app, pet, y, ancho, t, pet["id"] == elegida)))
        while filas and filas[-1][1] is None:
            filas.pop()

        # Aire arriba si cabe todo; si no cabe, la ventana sigue al cursor.
        n = len(filas)
        arriba = 1 if n < alto else 0
        sitio = alto - arriba
        cabeza = next((i for i, (ident, _p) in enumerate(filas) if ident == elegida), 0)
        if n <= sitio:
            self.desplazado = 0
        else:
            if cabeza < self.desplazado:
                self.desplazado = cabeza
            elif cabeza >= self.desplazado + sitio:
                self.desplazado = cabeza - sitio + 1
            self.desplazado = max(0, min(self.desplazado, n - sitio))

        for i, (ident, pintor) in enumerate(filas[self.desplazado:self.desplazado + sitio]):
            y = y0 + arriba + i
            if ident is not None and ident == elegida:
                pintar(win, y, 0, ancho_total, SEL)
                escribir(win, y, 0, "▎", AGUA)
            if pintor is not None:
                pintor(y)

        ocultas = n - self.desplazado - sitio
        partes = ([f"↑ {self.desplazado}"] if self.desplazado else []) + \
                 ([f"↓ {ocultas}"] if ocultas > 0 else [])
        if partes:
            texto = "  ".join(partes)
            escribir(win, y0 + alto - 1, ancho - len(texto) - 1, texto, TENUE)

    def _bloque(self, app, pet, ancho, t):
        """Las filas de un álbum que se baja: título y velocidad, la barra del
        álbum, las pistas que bajan ahora mismo y cuántas faltan."""
        win = app.pantalla
        datos = app.datos
        terminado = pet["estado"] == comun.LISTO
        en_curso = datos.peticion_en_curso()
        propia = not terminado and en_curso is not None and en_curso["id"] == pet["id"]
        v = datos.velocimetro
        p = pet.get("progreso") or {}
        total = p.get("total") or 0
        activas = datos.pistas_activas if propia else []
        hechas = max(p.get("terminados") or 0, len(datos.bajados) if propia else 0)
        if total:
            hechas = total if terminado else min(hechas, total)
        ahora = time.monotonic()

        def bytes_de(pista):
            """Los bytes de ahora, entre una lectura y la siguiente."""
            b = pista["bytes"] + pista["velocidad"] * max(0.0, ahora - datos.muestreado_en)
            return min(b, pista["total"]) if pista["total"] else b

        # Por bytes si se sabe lo que pesa todo; si no, por pistas, contando
        # la parte bajada de las que tienen tamaño; si no, sólo las acabadas.
        if terminado:
            frac = 1.0
        elif propia and datos.bytes_album_total:
            frac = ((sum(b for _n, b in datos.bajados) + sum(bytes_de(a) for a in activas))
                    / datos.bytes_album_total)
        elif total:
            frac = (hechas + sum(bytes_de(a) / a["total"] for a in activas if a["total"])) / total
        else:
            frac = 0.0
        frac = max(0.0, min(1.0, frac))
        color = ESPUMA if terminado else AGUA
        cambio = datos.cambios.get(pet["id"])
        u = (t - cambio[1]) / self.SALIDA if terminado and cambio else 1.0

        def destellar(y):
            if u < 1:
                _anima()
                destello(y, 0, ancho, u, ARRECIFE)

        def cabeza(y):
            glifo, tono = (OK, ESPUMA) if terminado else glifo_estado(pet["estado"], t)
            escribir(win, y, 1, glifo, tono)
            der = ("terminado" if terminado
                   else f"{comun.formato_bytes(v.actual)}/s" if propia and v.actual > 0 else "")
            escribir(win, y, 3, elipsis(titulo_peticion(pet), ancho - 3 - len(der) - 4),
                     ESPUMA if terminado else BLANCO)
            if der:
                escribir(win, y, ancho - 2 - len(der), der, ESPUMA if terminado else NORMAL,
                         negrita=True)
            destellar(y)

        def barra_album(y):
            cifra = f" {int(frac * 100):>3} %"
            cuantas = f"  {hechas} de {total} pistas" if total else ""
            ancho_barra = max(8, min(56, ancho - 3 - len(cifra) - len(cuantas) - 2))
            x = barra(win, y, 3, ancho_barra, frac, color,
                      vivo=propia and v.actual > 0, t=t)
            x = escribir(win, y, x, cifra, color)
            escribir(win, y, x, cuantas, TENUE)
            destellar(y)

        filas = [(pet["id"], cabeza), (None, barra_album)]
        if not propia:
            return filas

        calidad = calidad_corta((pet.get("candidato") or {}).get("detalle"))
        col = self._columnas(ancho, calidad)
        for archivo, cuando in sorted(datos.recien_bajadas.items(), key=lambda e: e[1]):
            if ahora - cuando < SEGUNDOS_RECIEN:
                filas.append((None, lambda y, a=archivo, c=cuando: self._pista_lista(
                    app, y, a, c, col)))
        for pista in activas:
            filas.append((None, lambda y, pista=pista: self._pista(
                app, y, pista, bytes_de(pista), col, calidad, t)))
        if not activas and not hechas:
            filas.append((None, lambda y: escribir_trozos(
                win, y, col["x"], cargando("esperando al primer archivo…", t, TENUE),
                ancho - col["x"] - 1)))
        faltan = total - hechas - len(activas) if total else 0
        if faltan > 0:
            texto = f"y {faltan} pista{'s' if faltan > 1 else ''} más por bajar"
            filas.append((None, lambda y: escribir(win, y, col["x"],
                                                   elipsis(texto, ancho - col["x"] - 1), TENUE)))
        return filas

    @staticmethod
    def _columnas(ancho, calidad):
        """Dónde va cada cosa en las filas de pista, para que se alineen.
        Con poco sitio se va primero la calidad y luego se acorta la barra."""
        col = {"x": 5, "fin": ancho, "barra": max(8, min(20, (ancho - 40) // 3)),
               "cifra": 9, "velocidad": 10, "calidad": len(calidad) + 2 if calidad else 0}

        def sitio():
            return (ancho - col["x"] - 3 - 1 - col["barra"] - col["cifra"]
                    - col["velocidad"] - col["calidad"] - 2)

        if sitio() < 12 and col["calidad"]:
            col["calidad"] = 0
        if sitio() < 12:
            col["barra"] = max(6, col["barra"] - (12 - sitio()))
        col["titulo"] = max(4, min(32, sitio()))
        return col

    def _pista(self, app, y, pista, bajado, col, calidad, t):
        """Una pista que se baja ahora: número, título, barra, % y velocidad.
        Sin tamaño conocido no hay %: los bytes y una corriente que pasa."""
        win = app.pantalla
        archivo = pista["archivo"]
        x = escribir(win, y, col["x"], rellenar(_numero_pista(archivo), 3), TENUE)
        x = escribir(win, y, x, rellenar(_sin_numero(archivo), col["titulo"]) + " ", NORMAL)
        if pista["total"]:
            frac = bajado / pista["total"]
            barra(win, y, x, col["barra"], frac, AGUA, vivo=pista["velocidad"] > 0, t=t)
            cifra = f" {int(frac * 100):>3} %"
        else:
            escribir_trozos(win, y, x, corriente(col["barra"], t))
            cifra = f" {comun.formato_bytes(bajado)}"
        x += col["barra"]
        x = escribir(win, y, x, rellenar(cifra, col["cifra"]), AGUA)
        if pista["velocidad"] > 0:
            escribir(win, y, x, f"{comun.formato_bytes(pista['velocidad'])}/s"
                     .rjust(col["velocidad"]), NORMAL)
        if col["calidad"]:
            escribir(win, y, col["fin"] - 2 - len(calidad), calidad, TENUE)

    def _pista_lista(self, app, y, archivo, cuando, col):
        """La pista que acaba de terminar se pone verde un momento y se va."""
        win = app.pantalla
        _anima()
        tono = ESPUMA if time.monotonic() - cuando < SEGUNDOS_RECIEN * 0.6 else TENUE
        escribir(win, y, col["x"] - 2, OK, tono)
        x = escribir(win, y, col["x"], rellenar(_numero_pista(archivo), 3), tono)
        x = escribir(win, y, x, rellenar(_sin_numero(archivo), col["titulo"]) + " ", tono)
        escribir(win, y, x, "lista", TENUE)

    def _linea(self, app, pet, y, ancho, t, elegida):
        """Una petición que no se baja: glifo, título y qué le pasa."""
        win = app.pantalla
        estado = pet["estado"]
        glifo, tono_glifo = glifo_estado(estado, t)
        if estado == comun.BUSCANDO:
            texto = f"buscando fuente · {int(segundos_desde(pet.get('actualizado', '')))} s"
            tono = CIELO
        elif estado == comun.PENDIENTE:
            mensaje = pet.get("mensaje") or ""
            if mensaje.startswith("reintentando"):
                texto, tono = mensaje, ARENA
            elif not app.datos.nicotine_vivo():
                texto, tono = "esperando turno · Nicotine+ está cerrado", ARENA
            else:
                texto, tono = "esperando turno", LILA
        else:
            texto, tono = pet.get("mensaje") or "falló", CORAL

        col = max(16, min(38, (ancho - 30) // 2))
        escribir(win, y, 1, glifo, tono_glifo)
        escribir(win, y, 3, rellenar(titulo_peticion(pet), col), BLANCO if elegida else NORMAL)
        x = 3 + col + 2
        der = [("r", "reintentar"), ("x", "quitar")] if estado == comun.FALLIDO else []
        largo_der = sum(len(k) + len(d) + 3 for k, d in der) - 2 if der else 0
        sitio = ancho - x - 2 - (largo_der + 2 if der else 0)
        if der and sitio < 12 and not elegida:
            der, sitio = [], ancho - x - 2           # sin sitio, sólo en la elegida
        escribir(win, y, x, elipsis(texto, sitio), tono)
        if der:
            _pares_teclas(win, y, ancho - 2 - largo_der, der)

    def tecla(self, app, k):
        lista = self.lista(app)
        pet = self.seleccion(app)
        pasos = {curses.KEY_UP: -1, curses.KEY_DOWN: 1,
                 curses.KEY_PPAGE: -5, curses.KEY_NPAGE: 5}
        if k in pasos or k in (curses.KEY_HOME, curses.KEY_END):
            if lista:
                if k == curses.KEY_HOME:
                    self.cursor = 0
                elif k == curses.KEY_END:
                    self.cursor = len(lista) - 1
                else:
                    self.cursor = max(0, min(len(lista) - 1, self.cursor + pasos[k]))
                self.elegida = lista[self.cursor]["id"]
        elif k in ("\n", "\r", curses.KEY_ENTER):
            if pet is not None:
                app.abrir(OverlayFicha(pet["id"]))
        elif k == "a":
            app.abrir(OverlayPedir())
        elif k == "i":
            app.abrir(OverlayImportar())
        elif k == "x":
            if pet is not None and pet["estado"] == comun.FALLIDO:
                app.quitar_peticion(pet)
            else:
                app.cancelar_peticion(pet)
        elif k == "r":
            app.reintentar_peticion(pet)
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
        if not app.biblioteca.heredada:
            pares.append(("h", "pasar a heredada"))
        pares.append(("d", "borrar"))
        return pares

    def dibujar(self, app, y0, alto, ancho):
        win = app.pantalla
        bib = app.biblioteca
        if bib.cargando or not bib.cargada:
            espera(win, y0, alto, ancho, "leyendo la biblioteca…")
            return
        if not bib.artistas:
            casa = os.path.expanduser("~")
            escribir(win, y0 + 1, 2, f"No hay ningún artista en {bib.raiz.replace(casa, '~')}",
                     TENUE)
            if not bib.heredada and os.path.isdir(bib.RAIZ_HEREDADA):
                x = escribir(win, y0 + 3, 2, "b", AGUA, True)
                escribir(win, y0 + 3, x + 1, "muestra la heredada", TENUE)
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
            if col and anchos[col] > 0:
                for y in range(y0, y0 + alto):
                    escribir(win, y, x, "│", LINEA)
                x += 1
            self._columna(app, col, y0, alto, x, anchos[col])
            x += anchos[col]

        if self.filtro:
            escribir(win, y0 + alto - 1, 1, "/", AGUA, True)
            escribir(win, y0 + alto - 1, 3,
                     elipsis(f"«{self.filtro}»", ancho - 4), BLANCO)

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
                    tinta = ARENA
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
            if enfocado:
                pintar(win, y, x, ancho, SEL)
                escribir(win, y, x, "▎", AGUA)
            if marcado:
                escribir(win, y, x + 1, "▸", ESPUMA)
            hueco = ancho - 3 - len(sufijo) - 1
            escribir(win, y, x + 2, elipsis(texto, hueco), BLANCO if elegido else tinta)
            escribir(win, y, x + ancho - len(sufijo) - 1, sufijo,
                     CIELO if enfocado else TENUE)

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
                    app.avisar(f"marcado {item['rel']}", ESPUMA)
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
            app.avisar("releyendo la biblioteca…", CIELO, 2)
        elif k == "b":
            app.biblioteca.cambiar_raiz()
            self.cursor = [0, 0, 0]
            self.desplazado = [0, 0, 0]
            self._artista_previo = self._album_previo = ""
        elif k == "o":
            app.organizar()
        elif k == "h":
            self._pedir_heredar(app)
        elif k in ("d", curses.KEY_DC):
            self._pedir_borrar(app)
        else:
            return False
        return True

    def _pedir_borrar(self, app):
        """d: borra lo marcado sin preguntar. Sin marcas, borra la carpeta entera
        del cursor (el artista en la columna de artistas, el álbum en las otras)
        y antes pregunta."""
        bib = app.biblioteca
        if app.marcados:
            albumes = [item for a in bib.artistas for item in a["albumes"]
                       if item["rel"] in app.marcados]
            if not albumes:
                app.avisar("lo marcado no está en esta biblioteca", ARENA)
                return
            app.borrar_carpetas([a["dir"] for a in albumes], [a["rel"] for a in albumes])
            return
        if self.columna == 0:
            artista = self.artista(app)
            if not artista:
                app.avisar("no hay nada que borrar", ARENA)
                return
            carpeta = os.path.join(bib.raiz, artista["nombre"])
            nombre, albumes = artista["nombre"], artista["albumes"]
        else:
            item = self.album(app)
            if not item:
                app.avisar("no hay nada que borrar", ARENA)
                return
            carpeta, nombre, albumes = item["dir"], item["rel"], [item]
        peso = comun.formato_bytes(sum(a["bytes"] for a in albumes))
        pistas = sum(len(a["pistas"]) for a in albumes)
        app.abrir(OverlayConfirmar(
            f"¿Estás seguro de borrar todo en la carpeta «{nombre}»? "
            f"{len(albumes)} álbum(es), {pistas} pista(s), {peso}. No se puede deshacer.",
            lambda: app.borrar_carpetas([carpeta], [a["rel"] for a in albumes])))

    def a_heredar(self, app):
        """Lo que movería h: los marcados, o el artista entero si el cursor está
        en la columna de artistas, o el álbum donde está el cursor."""
        if app.marcados:
            return [item for a in app.biblioteca.artistas for item in a["albumes"]
                    if item["rel"] in app.marcados]
        if self.columna == 0:
            artista = self.artista(app)
            return list(artista["albumes"]) if artista else []
        item = self.album(app)
        return [item] if item else []

    def _pedir_heredar(self, app):
        if app.biblioteca.heredada:
            app.avisar("ya estás en la heredada; pulsa b para volver a la nueva", ARENA)
            return
        albumes = self.a_heredar(app)
        if not albumes:
            app.avisar("no hay nada que pasar a la heredada", ARENA)
            return
        peso = comun.formato_bytes(sum(a["bytes"] for a in albumes))
        if len(albumes) == 1:
            que = albumes[0]["rel"]
        elif len({a["artista"] for a in albumes}) == 1:
            que = f"{albumes[0]['artista']} ({len(albumes)} álbumes)"
        else:
            que = f"{len(albumes)} álbumes"
        app.abrir(OverlayConfirmar(
            f"¿Mover {que}, {peso}, a la heredada (Artistas) para compartirlo en "
            f"Nicotine+? Deja de salir en la biblioteca nueva.",
            lambda: app.pasar_a_heredada(albumes)))

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
            app.avisar(error, CORAL)
        else:
            app.avisar(f"sonando {titulo}", ESPUMA)


class VistaFiio(Vista):
    """La copia a la tarjeta en tres pantallas: lista para copiar, copiando y
    terminada.

    Qué hay que copiar lo dice fiio.clasificar() contra la tarjeta elegida
    (se vuelve a mirar al cambiar de tarjeta o de marcas y al acabar una
    copia); el progreso sale de Trabajo.Copia y, al acabar, de
    app.ultima_copia. La pantalla de terminada se queda hasta ↵, hasta que
    aparece otra tarjeta o hasta volver a la pestaña con la tarjeta ya fuera."""
    nombre = "FiiO"

    # Fondo de la franja y del botón de cada tono: en 256 colores, letra
    # oscura sobre el color; con 8/16, lo que los básicos dejan leer.
    _FONDOS_256 = {AGUA: 81, ESPUMA: 79, CORAL: 210, ARENA: 222}
    _FONDOS_BASICOS = {ESPUMA: curses.COLOR_GREEN, CORAL: curses.COLOR_RED}
    MOTIVO_AL_DIA = "ya está y en orden"

    def __init__(self):
        self.todos = []          # lo que hay en la biblioteca (o lo marcado)
        self.reparto = None      # fiio.clasificar() contra la tarjeta elegida
        self.en_tarjeta = {}     # rel → bytes que ya ocupan en la tarjeta los de w
        self.calculado_para = None
        self.calculando = False
        self.destino = 0
        self.desplazado = 0
        self.cerrada = None      # la copia terminada que ya no se enseña
        self._fin_visto = None   # (copia, montajes que había al enseñarla)

    @property
    def plan(self):
        """Lo que copiaría ↵: los álbumes que aún no están en la tarjeta."""
        return list((self.reparto or {}).get("nuevos", []))

    def entrar(self, app):
        copia = app.ultima_copia
        montajes = {d["montaje"] for d in app.datos.destinos}
        if copia is not None and copia.montaje not in montajes:
            self.cerrada = copia        # volver con la tarjeta ya fuera
        if self._clave(app) != self.calculado_para:
            self.calcular(app)

    def montaje(self, app):
        destinos = app.datos.destinos
        if not destinos:
            return None
        self.destino = max(0, min(self.destino, len(destinos) - 1))
        return destinos[self.destino]

    @staticmethod
    def etiqueta(destino):
        return (destino.get("etiqueta")
                or os.path.basename(destino["montaje"].rstrip("/\\")) or "tarjeta")

    # ── qué hay que copiar ──
    def _clave(self, app):
        destino = self.montaje(app)
        copia = app.ultima_copia
        return (app.biblioteca.raiz, tuple(sorted(app.marcados)),
                destino["montaje"] if destino else None,
                id(copia) if copia is not None else None)

    def calcular(self, app):
        """listar_biblioteca + clasificar contra la tarjeta elegida, en su hilo."""
        if self.calculando:
            return
        self.calculando = True
        clave = self._clave(app)
        raiz, filtros, montaje = clave[0], list(clave[1]) or None, clave[2]

        def trabajo():
            try:
                todos = fiio.listar_biblioteca(raiz=raiz, filtros=filtros)
            except Exception:
                todos = []
            reparto, en_tarjeta = None, {}
            if montaje:
                destino_raiz = os.path.join(montaje, fiio.DESTINO_REL_POR_DEFECTO)
                try:
                    reparto = fiio.clasificar(destino_raiz, todos)
                except Exception:
                    reparto = None
                for a in (reparto or {}).get("otro_orden", []) + \
                        (reparto or {}).get("distintos", []):
                    en_tarjeta[a["rel"]] = self._peso(
                        os.path.join(destino_raiz, a["artista"], a["album"]))
            self.todos, self.reparto, self.en_tarjeta = todos, reparto, en_tarjeta
            self.calculado_para = clave
            self.calculando = False

        threading.Thread(target=trabajo, daemon=True).start()

    @staticmethod
    def _peso(carpeta):
        try:
            return sum(os.path.getsize(os.path.join(carpeta, n)) for n in os.listdir(carpeta))
        except OSError:
            return 0

    def _reparto(self, app):
        """El reparto si corresponde a la tarjeta y las marcas de ahora."""
        if self.reparto is not None and self.calculado_para == self._clave(app):
            return self.reparto
        return None

    def _arreglar(self, reparto):
        return list((reparto or {}).get("otro_orden", [])) + \
            list((reparto or {}).get("distintos", []))

    @staticmethod
    def _copiando_ahora(app):
        trabajo = app.trabajo
        if trabajo.activo and trabajo.clase == "copia" and trabajo.copia is not None:
            return trabajo.copia
        return None

    def _terminada(self, app):
        """La copia acabada que toca enseñar, o None."""
        copia = app.ultima_copia
        if copia is None or copia is self.cerrada or self._copiando_ahora(app):
            return None
        montajes = {d["montaje"] for d in app.datos.destinos}
        if self._fin_visto is None or self._fin_visto[0] is not copia:
            self._fin_visto = (copia, montajes)
        elif montajes - self._fin_visto[1]:
            self.cerrada = copia        # ha aparecido otra tarjeta
            return None
        return copia

    # ── lo que se ve ──
    def resumen(self, app):
        copia = self._copiando_ahora(app)
        if copia is not None:
            que = "simulando" if copia.simulacion else "copiando"
            return f"{copia.etiqueta} · {que} {int(copia.frac() * 100)} %"
        copia = self._terminada(app)
        if copia is not None and copia.expulsada:
            return f"{copia.etiqueta} expulsada"
        destino = self.montaje(app)
        if destino is None:
            return "sin tarjeta"
        reparto = self._reparto(app)
        if reparto is None:
            return self.etiqueta(destino)
        nuevos, arreglar = len(reparto["nuevos"]), len(self._arreglar(reparto))
        if nuevos:
            return f"{self.etiqueta(destino)} · {nuevos} por copiar"
        if arreglar:
            return f"{self.etiqueta(destino)} · {arreglar} por arreglar"
        return f"{self.etiqueta(destino)} · al día"

    def teclas(self, app):
        if self._copiando_ahora(app) is not None:
            # fiio.copiar() mira la cancelación entre archivos: la pista en
            # curso se termina y el álbum a medias queda para w.
            return [("esc", "cancelar (termina la pista actual)")]
        copia = self._terminada(app)
        if copia is not None:
            teclas = []
            if not copia.simulacion and not copia.expulsada:
                teclas.append(("u", "expulsar"))
            teclas.append(("↵", "volver a la lista" if copia.simulacion else "copiar otra vez"))
            if self._arreglar(self._reparto(app)):
                teclas.append(("w", "arreglar orden"))
            return teclas
        if self.montaje(app) is None:
            return []
        reparto = self._reparto(app)
        teclas = []
        if reparto is not None and reparto["nuevos"]:
            teclas += [("↵", "copiar"), ("n", "simular")]
        if self._arreglar(reparto):
            teclas.append(("w", "arreglar orden"))
        teclas.append(("u", "expulsar"))
        if len(app.datos.destinos) > 1:
            teclas.append(("↑↓", "tarjeta"))
        if app.marcados:
            teclas.append(("m", "quitar marcas"))
        return teclas

    def dibujar(self, app, y0, alto, ancho):
        copia = self._copiando_ahora(app)
        if copia is not None:
            self._pintar_copiando(app, copia, y0, alto, ancho)
            return
        if self._clave(app) != self.calculado_para:
            self.calcular(app)
        copia = self._terminada(app)
        if copia is not None:
            self._pintar_terminada(app, copia, y0, alto, ancho)
            return
        self._pintar_lista(app, y0, alto, ancho)

    # ── piezas comunes ──
    @classmethod
    def _resalte(cls, color):
        """Letra oscura sobre `color`: la franja de arriba y el botón de la caja."""
        if _PALETA["colores"] >= 256:
            return (16, cls._FONDOS_256.get(color, 81), NEGRITA)
        if _PALETA["colores"] and color in cls._FONDOS_BASICOS:
            return (-1, cls._FONDOS_BASICOS[color], NEGRITA)
        return INV

    @staticmethod
    def _nombre(a):
        return f"{a['artista']} — {a['album']}" if a.get("artista") else a["rel"]

    @staticmethod
    def _hora(segundos_epoch):
        return time.strftime("%H:%M", time.localtime(segundos_epoch))

    @staticmethod
    def _tiempo(segundos):
        segundos = max(1, int(round(segundos)))
        if segundos < 90:
            return f"{segundos} s"
        minutos = int(round(segundos / 60))
        if minutos < 90:
            return f"{minutos} min"
        return f"{minutos // 60} h {minutos % 60:02d} min"

    @staticmethod
    def _plural(n, uno, varios):
        return f"{n} {uno if n == 1 else varios}"

    @staticmethod
    def _caja(win, y, x, ancho, filas):
        """Un recuadro con una fila de trozos por línea. Devuelve la y siguiente."""
        escribir(win, y, x, "┌" + VACIO * (ancho - 2) + "┐", LINEA)
        for i, trozos in enumerate(filas, 1):
            escribir(win, y + i, x, "│", LINEA)
            escribir_trozos(win, y + i, x + 3, trozos, ancho=ancho - 5)
            escribir(win, y + i, x + ancho - 1, "│", LINEA)
        escribir(win, y + len(filas) + 1, x, "└" + VACIO * (ancho - 2) + "┘", LINEA)
        return y + len(filas) + 2

    @staticmethod
    def _franja(win, y, ancho, izq, variantes):
        """La fila de la franja: `izq` a la izquierda y, a la derecha, la
        primera de `variantes` (texto) que quepa."""
        largo = sum(len(t) for t, _c in izq)
        der = ()
        for texto in variantes:
            if texto and largo + len(texto) + 4 <= ancho:
                der = ((texto, TENUE), ("  ", NORMAL))
                break
        pintar_fila(win, y, 0, ancho, izq, der)

    @staticmethod
    def _ventana(n, sitio, foco):
        """(desde, hasta, antes, despues): qué filas de n caben en `sitio`
        con `foco` a la vista; antes/despues dicen si hace falta un «…»."""
        if n <= sitio:
            return 0, n, False, False
        desde = max(0, min(foco - sitio // 3, n - sitio))
        hasta = desde + sitio
        antes, despues = desde > 0, hasta < n
        return desde + (1 if antes else 0), hasta - (1 if despues else 0), antes, despues

    # ── lista para copiar ──
    def _pintar_lista(self, app, y0, alto, ancho):
        win = app.pantalla
        destino = self.montaje(app)
        y = y0
        if destino is None:
            escribir(win, y, 2, "No hay ninguna tarjeta montada.", ARENA)
            fuera = app.ultima_expulsion
            if fuera and fuera.get("ok"):
                x = escribir(win, y + 2, 2, f"{OK} {fuera['etiqueta']} expulsada.", ESPUMA, True)
                escribir(win, y + 2, x + 1, "Ya puedes sacar la tarjeta.", TENUE)
                y += 2
            escribir(win, y + 2, 2,
                     "Conecta el FiiO o el lector de la microSD; aparece sola.", TENUE)
            escribir(win, y + 3, 2,
                     f"Se buscan sistemas FAT/exFAT en {fiio.donde_se_buscan()}.", TENUE)
            return

        reparto = self._reparto(app)
        if reparto is None and (self.reparto is None
                                or (self.calculado_para or (None,) * 3)[2] != destino["montaje"]):
            reparto_visto = None          # aún no se sabe nada de esta tarjeta
        else:
            reparto_visto = reparto or self.reparto
        nuevos = list((reparto_visto or {}).get("nuevos", []))
        al_dia = list((reparto_visto or {}).get("al_dia", []))
        otro_orden = list((reparto_visto or {}).get("otro_orden", []))
        distintos = list((reparto_visto or {}).get("distintos", []))
        bytes_copia = sum(a["bytes"] for a in nuevos)
        pistas_copia = sum(len(a["pistas"]) for a in nuevos)
        libre, total = destino["libre"], destino["total"]
        falta = bytes_copia - libre
        no_fat = destino["tipo"] not in ("vfat", "exfat", "msdos")

        # Cuánto ocupa cada parte, para quitar huecos si no cabe todo.
        caja_filas = 4 + (1 if app.marcados else 0)
        abajo = (1 if al_dia else 0) + (1 if otro_orden else 0) + (1 if distintos else 0)
        fijo = (len(app.datos.destinos) + (1 if no_fat else 0) + 2 + (1 if falta > 0 else 0)
                + caja_filas + 2 + 1 + min(len(nuevos), 3) + abajo)
        huecos = 4 if abajo else 3
        holgado = fijo + huecos <= alto
        hueco = 1 if holgado else 0     # sin sitio, tampoco hay blanco en la caja

        # la tarjeta
        for i, d in enumerate(app.datos.destinos):
            elegida = i == self.destino
            if elegida:
                pintar(win, y, 0, ancho, SEL)
            der = [(f"{d['tipo']} · {comun.formato_bytes(d['libre'])} libres de "
                    f"{comun.formato_bytes(d['total'])}", TENUE), ("  ", NORMAL)]
            pintar_fila(win, y, 0, ancho,
                        [("▌" if elegida else " ", AGUA),
                         (self.etiqueta(d), BLANCO if elegida else NORMAL),
                         ("  conectada", ESPUMA)], der)
            y += 1
        if no_fat:
            escribir(win, y, 2, elipsis(f"{AVISO} es {destino['tipo']}: no puedo verificar "
                                        f"el orden físico de las entradas", ancho - 4), ARENA)
            y += 1
        y += hueco

        # el espacio: lo usado, esta copia (rayada) y lo que queda libre
        ancho_barra = max(10, min(60, ancho - 14))
        usado = max(0, total - libre)
        if total:
            celdas_usado = int(round(ancho_barra * usado / total))
            celdas_copia = min(ancho_barra - celdas_usado,
                               int(math.ceil(ancho_barra * bytes_copia / total)) if bytes_copia else 0)
        else:
            celdas_usado = celdas_copia = 0
        x = escribir(win, y, 2, "espacio ", TENUE)
        escribir_trozos(win, y, x, [
            (BLOQUE * celdas_usado, TENUE),
            ("▒" * celdas_copia, CORAL if falta > 0 else ARENA),
            (VACIO * (ancho_barra - celdas_usado - celdas_copia), LINEA)])
        y += 1
        leyenda = [(f"usado {comun.formato_bytes(usado)}  ·  ", TENUE),
                   (f"esta copia {comun.formato_bytes(bytes_copia)}", ARENA)]
        if falta <= 0:
            leyenda.append((f"  ·  quedarán {comun.formato_bytes(libre - bytes_copia)} libres",
                            TENUE))
        escribir_trozos(win, y, x, leyenda, ancho=ancho - x - 1)
        y += 1
        if falta > 0:
            escribir(win, y, x, f"{MAL} no cabe: faltan {comun.formato_bytes(falta)}", CORAL)
            y += 1
        y += hueco

        # la caja: qué falta copiar y la única acción
        ancho_caja = max(30, min(66, ancho - 4))
        filas = []
        if reparto_visto is None:
            filas.append(cargando("mirando qué hay en la tarjeta…"))
            filas.append([])
        elif not self.todos:
            filas.append([("No hay álbumes que copiar en la biblioteca"
                           + (" marcados" if app.marcados else ""), BLANCO)])
            filas.append([])
        elif nuevos:
            filas.append([(self._plural(len(nuevos), "álbum nuevo", "álbumes nuevos")
                           + " para copiar", BLANCO)])
            datos = (f"{self._plural(pistas_copia, 'pista', 'pistas')} · "
                     f"{comun.formato_bytes(bytes_copia)}")
            velocidad = app.velocidades_fiio.get(self.etiqueta(destino))
            if velocidad:
                datos += f" · alrededor de {self._tiempo(bytes_copia / velocidad)}"
            filas.append([(datos, TENUE)])
        else:
            filas.append([(f"{OK} La tarjeta está al día", ESPUMA)])
            filas.append([("no hay nada nuevo que copiar", TENUE)])
        if app.marcados:
            filas.append([("solo lo marcado en Biblioteca · ", ARENA), ("m", AGUA),
                          (" quita las marcas", TENUE)])
        accion = None
        if nuevos and falta > 0:
            accion = [(f"{MAL} No cabe: libera {comun.formato_bytes(falta)} "
                       f"o marca menos álbumes", CORAL)]
        elif nuevos:
            accion = [(" ↵  Empezar a copiar ", self._resalte(AGUA)),
                      ("   o ", TENUE), ("n", AGUA), (" para ver el plan sin copiar", TENUE)]
        if accion:
            filas += ([[]] if holgado else []) + [accion]
        y = self._caja(win, y, 2, ancho_caja, filas)
        y += hueco

        # por copiar, desplazable; lo que ya está y lo que hay que arreglar, abajo
        fondo = y0 + alto - abajo - (hueco if abajo else 0)
        ancho_fila = min(ancho - 4, 72)
        if nuevos and fondo - y >= 2:
            escribir(win, y, 2, "por copiar", TENUE)
            y += 1
            sitio = fondo - y
            self.desplazado = max(0, min(self.desplazado, len(nuevos) - sitio))
            visibles = nuevos[self.desplazado:self.desplazado + sitio]
            sobran = len(nuevos) - self.desplazado - len(visibles)
            if sobran > 0:
                visibles = visibles[:-1]
                sobran += 1
            for a in visibles:
                der = [(f"{len(a['pistas']):>2} pistas  {comun.formato_bytes(a['bytes']):>9}",
                        TENUE)]
                pintar_fila(win, y, 4, ancho_fila - 2,
                            [("+ ", ARENA), (self._nombre(a), NORMAL)], der)
                y += 1
            if sobran > 0:
                texto = f"… y {sobran} más"
                if len(app.datos.destinos) <= 1:
                    texto += "  (↑↓ para verlos)"
                escribir(win, y, 6, texto, TENUE)
                y += 1
            y += hueco
        aviso = [("w", AGUA), (" los reescribe en orden", TENUE)]
        if al_dia:
            pintar_fila(win, y, 2, ancho_fila, [("ya en la tarjeta  ", TENUE),
                        (f"{OK} {self._plural(len(al_dia), 'álbum', 'álbumes')}", ESPUMA)])
            y += 1
        if otro_orden:
            pintar_fila(win, y, 2, ancho_fila, [("en otro orden     ", TENUE),
                        (f"{AVISO} {self._plural(len(otro_orden), 'álbum', 'álbumes')}", ARENA)],
                        aviso)
            y += 1
        if distintos:
            pintar_fila(win, y, 2, ancho_fila, [("incompletos       ", TENUE),
                        (f"{AVISO} {self._plural(len(distintos), 'álbum', 'álbumes')}", ARENA)],
                        aviso)

    # ── copiando ──
    def _pintar_copiando(self, app, copia, y0, alto, ancho):
        win = app.pantalla
        t = time.monotonic()
        holgado = alto >= 16
        y = y0
        en_juego = [a for a in copia.albumes if a["estado"] != "omitido"]
        actual = copia.actual()

        titulo = (f" {girador(t)} SIMULANDO " if copia.simulacion
                  else f" {girador(t)} COPIANDO A {copia.etiqueta} ")
        izq = [(titulo, self._resalte(AGUA)), ("  ", NORMAL)]
        posicion = next((i for i, a in enumerate(en_juego) if a is actual), None)
        if posicion is not None:
            izq.append((f"álbum {posicion + 1} de {len(en_juego)}", BLANCO))
            if copia.pista_indice:
                izq += [(" · ", TENUE), (f"pista {copia.pista_indice} de "
                                         f"{len(actual['pistas'])}", BLANCO)]
        empezo = self._hora(copia.inicio)
        termina = copia.termina()
        if termina is not None:
            fin = self._hora(termina)
            variantes = (f"empezó {empezo} · termina ~{fin}", f"empezó {empezo} → ~{fin}",
                         f"{empezo} → ~{fin}")
        else:
            variantes = (f"empezó {empezo}", empezo)
        self._franja(win, y, ancho, izq, variantes)
        y += 1 + (1 if holgado else 0)

        # la barra del total
        frac = copia.frac()
        ancho_barra = max(10, ancho - 12)
        barra(win, y, 2, ancho_barra, frac, AGUA,
              vivo=copia.velocimetro.actual > 0, t=t)
        escribir(win, y, 2 + ancho_barra + 2, f"{int(frac * 100):>3} %", AGUA, True)
        y += 1
        if copia.simulacion:
            datos = [(f"{copia.pistas_hechas} de {copia.pistas_total} pistas · "
                      "simulación: no se escribe nada", TENUE)]
        else:
            datos = [(f"{comun.formato_bytes(copia.hechos_bytes)} de "
                      f"{comun.formato_bytes(copia.total_bytes)}", TENUE)]
            velocidad = copia.velocidad() or copia.velocimetro.actual
            if velocidad:
                datos += [(" · ", TENUE), (f"{comun.formato_bytes(velocidad)}/s", BLANCO)]
            restante = copia.restante()
            if restante is not None:
                datos.append((f" · quedan {self._tiempo(restante)}", TENUE))
        escribir_trozos(win, y, 2, datos, ancho=ancho - 4)
        y += 1 + (1 if holgado else 0)

        # los álbumes como lista de tareas; el actual, abierto con sus pistas
        filas, foco = [], 0
        ancho_fila = min(ancho - 2, 74)
        ancho_nombre = max(12, ancho_fila - 20)
        ancho_pista = max(8, min(24, ancho_fila - 46))
        for a in copia.albumes:
            n = len(a["pistas"])
            nombre = rellenar(self._nombre(a), ancho_nombre)
            estado = a["estado"]
            if estado == "copiado":
                filas.append(([("  ✓ ", ESPUMA), (nombre, ESPUMA)],
                               [(f"{a['hechas']:>2}/{n}  copiado", TENUE)], False))
            elif estado == "omitido":
                motivo = a["motivo"] or "omitido"
                filas.append(([("  – ", TENUE), (nombre, TENUE)],
                               [(motivo, TENUE if motivo == self.MOTIVO_AL_DIA else ARENA)],
                               False))
            elif estado in ("error", "abortado"):
                filas.append(([(f"  {MAL} ", CORAL), (nombre, CORAL)],
                               [(elipsis(a["motivo"] or estado, 30), CORAL)], False))
            elif a is actual and estado == "copiando":
                foco = len(filas)
                filas.append(([("  ▸ ", AGUA), (nombre, BLANCO)],
                               [(f"{a['hechas']:>2}/{n}", AGUA)], False))
                for j, archivo in enumerate(a["pistas"]):
                    texto = f"{j + 1:02d} {_sin_numero(archivo) if archivo else ''}"
                    if j < a["hechas"]:
                        filas.append(([("      ✓ ", ESPUMA), (texto, ESPUMA)], [], False))
                    elif j == copia.pista_indice - 1:
                        foco = len(filas)
                        izq = [("      ▸ ", AGUA),
                               (rellenar(texto, max(8, ancho_fila - 16 - ancho_pista)), BLANCO)]
                        der = []
                        if copia.pista_bytes:
                            fp = min(1.0, copia.pista_hechos / copia.pista_bytes)
                            der = (barra_trozos(ancho_pista, fp, AGUA, vivo=fp < 1, t=t)
                                   + [(f" {int(fp * 100):>3} %", AGUA)])
                        filas.append((izq, der, True))
                    else:
                        filas.append(([("        ", NORMAL), (texto, TENUE)], [], False))
            else:
                filas.append(([("  · ", TENUE), (nombre, TENUE)],
                               [(f"{a['hechas']:>2}/{n}  en espera", TENUE)], False))

        sitio = y0 + alto - y
        desde, hasta, antes, despues = self._ventana(len(filas), sitio, foco)
        if antes:
            escribir(win, y, 4, f"… {desde} más arriba", TENUE)
            y += 1
        for izq, der, resaltada in filas[desde:hasta]:
            if resaltada:
                pintar(win, y, 0, ancho, SEL)
            pintar_fila(win, y, 0, ancho_fila, izq, der)
            y += 1
        if despues:
            escribir(win, y, 4, f"… {len(filas) - hasta} más", TENUE)

    # ── terminada ──
    def _pintar_terminada(self, app, copia, y0, alto, ancho):
        win = app.pantalla
        copiados = [a for a in copia.albumes if a["estado"] == "copiado"]
        problemas = [a for a in copia.albumes
                     if a["estado"] in ("error", "abortado") or a["orden_ok"] is False
                     or (a["estado"] == "omitido" and a["motivo"] != self.MOTIVO_AL_DIA)]
        sin_empezar = [a for a in copia.albumes if a["estado"] == "espera"]
        fallos = any(a["estado"] == "error" for a in copia.albumes)

        if copia.simulacion:
            titulo, tono = ((f" {MAL} SIMULACIÓN CANCELADA ", ARENA) if copia.abortado
                            else (f" {OK} SIMULACIÓN TERMINADA ", ESPUMA))
        elif copia.abortado:
            titulo, tono = f" {MAL} COPIA CANCELADA ", CORAL
        elif fallos:
            titulo, tono = f" {AVISO} COPIA TERMINADA CON ERRORES ", CORAL
        else:
            titulo, tono = f" {OK} COPIA TERMINADA ", ESPUMA
        pistas = copia.pistas_hechas
        peso = (sum(a["bytes"] for a in copiados) if copia.simulacion
                else copia.hechos_bytes)
        izq = [(titulo, self._resalte(tono)), ("  ", NORMAL),
               (f"{self._plural(len(copiados), 'álbum', 'álbumes')} · "
                f"{self._plural(pistas, 'pista', 'pistas')} · "
                f"{comun.formato_bytes(peso)}", BLANCO),
               (f" en {self._tiempo(copia.duracion())}", TENUE)]
        inicio, fin = self._hora(copia.inicio), self._hora(copia.fin or time.time())
        self._franja(win, y0, ancho, izq, (f"{inicio} → {fin}",))

        # cuánto ocupa cada parte, para quitar huecos si no cabe
        if copia.simulacion:
            caja = [[("Era una simulación: no se escribió nada en la tarjeta.", BLANCO)],
                    [],
                    [(" ↵  Volver a la lista ", self._resalte(AGUA)),
                     ("   para copiar de verdad", TENUE)]]
        elif copia.expulsada:
            caja = [[(f"{OK} {copia.etiqueta} expulsada.", ESPUMA),
                     (" Ya puedes sacar la tarjeta.", TENUE)]]
        elif app.expulsando:
            caja = [cargando("sincronizando y desmontando la tarjeta…")]
        elif copia.expulsada is False:
            caja = [[(f"{MAL} No se pudo expulsar {copia.etiqueta}.", CORAL)],
                    [],
                    [(" u  Intentarlo otra vez ", self._resalte(ESPUMA)),
                     ("   hace sync y la desmonta", TENUE)]]
        else:
            caja = [[("Ya puedes desconectar la FiiO.", BLANCO)],
                    [],
                    [(" u  Expulsar la tarjeta ", self._resalte(ESPUMA)),
                     ("   hace sync y la desmonta", TENUE)]]
        lista = copiados + problemas + sin_empezar
        ayuda = 1 if (problemas or sin_empezar) and not copia.simulacion else 0
        fijo = 1 + 1 + len(caja) + 2 + ayuda
        holgado = fijo + 4 + min(len(lista), 3) <= alto
        if not holgado:
            caja = [fila for fila in caja if fila]
        hueco = 1 if holgado else 0
        y = y0 + 1 + hueco

        bien = not (copia.abortado or fallos)
        ancho_barra = max(10, ancho - 12)
        frac = 1.0 if bien else copia.frac()
        barra(win, y, 2, ancho_barra, frac, ESPUMA if bien else ARENA)
        escribir(win, y, 2 + ancho_barra + 2, f"{int(frac * 100):>3} %",
                 ESPUMA if bien else ARENA, True)
        y += 1 + hueco

        sitio = y0 + alto - y - (len(caja) + 2) - ayuda - (2 * hueco if ayuda else hueco)
        ancho_fila = min(ancho - 2, 74)
        ancho_nombre = max(12, ancho_fila - 20)
        filas = []
        for a in copiados:
            if a["orden_ok"] is False:
                continue
            filas.append(([(f"  {OK} ", ESPUMA), (rellenar(self._nombre(a), ancho_nombre), ESPUMA)],
                          [(self._plural(a["hechas"], "pista", "pistas"), TENUE)]))
        for a in problemas:
            if a["estado"] == "abortado" and copia.simulacion:
                motivo = f"se paró tras {a['hechas']} de {len(a['pistas'])} pistas"
            elif a["estado"] == "abortado":
                motivo = f"a medias: {a['hechas']} de {len(a['pistas'])} pistas"
            elif a["orden_ok"] is False:
                motivo = "quedó en otro orden"
            else:
                motivo = a["motivo"] or a["estado"]
            filas.append(([(f"  {MAL} ", CORAL), (rellenar(self._nombre(a), ancho_nombre), CORAL)],
                          [(motivo, CORAL)]))
        for a in sin_empezar:
            filas.append(([("  · ", TENUE), (rellenar(self._nombre(a), ancho_nombre), TENUE)],
                          [("no se copió", TENUE)]))
        if sitio > 0:
            visibles = filas[:sitio]
            if len(filas) > sitio:
                visibles = filas[:sitio - 1]
            for izq, der in visibles:
                # si el motivo es largo, el nombre cede sitio (hasta 12)
                largo_der = sum(len(t) for t, _c in der)
                nombre = izq[1][0].rstrip()
                cabe = max(12, min(ancho_nombre, ancho - 2 - 4 - 1 - largo_der))
                izq = [izq[0], (rellenar(nombre, cabe), izq[1][1])]
                pintar_fila(win, y, 0, min(ancho - 2, max(ancho_fila, 4 + cabe + 1 + largo_der)),
                            izq, der)
                y += 1
            if len(filas) > len(visibles):
                escribir(win, y, 4, f"… y {len(filas) - len(visibles)} más", TENUE)
                y += 1
        if ayuda:
            y += hueco
            que = []
            if any(a["estado"] in ("abortado", "error") for a in problemas):
                que.append("a medias")
            if any(a["orden_ok"] is False or a["estado"] == "omitido" for a in problemas):
                que.append("en otro orden")
            consejo = [("↵", AGUA), (" vuelve para copiar lo que falte", CORAL)]
            if que:
                consejo += [("  ·  ", TENUE), ("w", AGUA),
                            (" reescribe lo que quedó " + " o ".join(que), CORAL)]
            escribir_trozos(win, y, 2, consejo, ancho=ancho - 4)
            y += 1
        y += hueco
        self._caja(win, y, 2, max(30, min(66, ancho - 4)), caja)

    @staticmethod
    def panel_trabajo(app, y0, alto, ancho):
        """Progreso de una tarea larga. Lo usan la vista FiiO y la de Cola."""
        win = app.pantalla
        trabajo = app.trabajo
        y = y0
        activo = trabajo.activo
        t = time.monotonic()
        if activo:
            escribir(win, y, 2, girador(t), CIELO)
            escribir(win, y, 4, trabajo.titulo, BLANCO)
        else:
            escribir(win, y, 2, OK if not (trabajo.resumen or {}).get("errores") else MAL,
                     ESPUMA if not (trabajo.resumen or {}).get("errores") else CORAL)
            escribir(win, y, 4, trabajo.titulo, BLANCO)
        y += 2

        if trabajo.total_bytes:
            frac = min(1.0, trabajo.hechos_bytes / trabajo.total_bytes)
            escribir(win, y, 2, rellenar("total", 10), TENUE)
            ancho_barra = max(10, ancho - 34)
            barra(win, y, 12, ancho_barra, frac, AGUA,
                  vivo=activo and trabajo.velocimetro.actual > 0, t=t)
            escribir(win, y, 12 + ancho_barra + 1, f"{int(frac * 100):>3} %", AGUA)
            y += 1
            escribir(win, y, 12,
                     f"{comun.formato_bytes(trabajo.hechos_bytes)} de "
                     f"{comun.formato_bytes(trabajo.total_bytes)}", TENUE)
            velocidad = f"{comun.formato_bytes(trabajo.velocimetro.actual)}/s"
            xx = escribir(win, y, max(13, ancho - len(velocidad) - 40), velocidad, BLANCO) + 2
            sitio = ancho - 2 - xx
            if sitio >= 14 and activo:
                xx = escribir_trozos(win, y, xx, medidor(trabajo.velocimetro,
                                                         min(14, sitio - 2), t)) + 2
            sitio = ancho - 2 - xx
            if sitio >= 8:
                escribir_trozos(win, y, xx, chispa(trabajo.velocimetro.historial,
                                                   min(22, sitio)))
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
                barra(win, y, 12, max(10, min(40, ancho - 20)), frac, CIELO,
                      vivo=activo and frac < 1, t=t)
                y += 1
        if trabajo.album_pistas:
            escribir(win, y, 2, rellenar("orden", 10), TENUE)
            x = 12
            for i in range(trabajo.album_pistas):
                if x + 3 > ancho - 1:
                    break
                if i < len(trabajo.orden):
                    # recién escrita salpica espuma y se asienta en su color
                    u = (t - trabajo.orden_t[i]) / 0.9 if i < len(trabajo.orden_t) else 1
                    if u < 1:
                        _anima()
                        tinta = (mezclar(195, TEMA_256[ESPUMA][0], frenada(u)),)
                    else:
                        tinta = ESPUMA
                    x = escribir(win, y, x, f"{i + 1:02d}", tinta, u < 0.5) + 1
                elif i == len(trabajo.orden) and activo:
                    x = escribir(win, y, x, f"{i + 1:02d}", respiro(t, CIELOS, 1.2)) + 1
                else:
                    x = escribir(win, y, x, "··", LINEA) + 1
            if trabajo.orden_ok is False:
                escribir(win, y + 1, 12, f"{AVISO} el orden físico no quedó como se "
                                         f"esperaba; hará falta --rehacer", ARENA)
            y += 2

        y += 1
        alto_log = y0 + alto - y
        if alto_log > 0:
            escribir(win, y - 1, 2, VACIO * max(0, ancho - 4), LINEA)
            lineas = list(trabajo.lineas)[-alto_log:]
            for i, linea in enumerate(lineas):
                tinta = TENUE
                if linea.strip().startswith(MAL):
                    tinta = CORAL
                elif linea.strip().startswith(AVISO):
                    tinta = ARENA
                elif linea.strip().startswith(OK):
                    tinta = ESPUMA
                elif linea.strip().startswith("▸"):
                    tinta = BLANCO
                escribir(win, y + i, 2, elipsis(linea, ancho - 4), tinta)

        if not activo and trabajo.resumen is not None:
            resumen = trabajo.resumen
            texto = (f"{resumen.get('copiados', 0)} álbumes, "
                     f"{resumen.get('archivos', 0)} archivos, "
                     f"{comun.formato_bytes(resumen.get('bytes', 0))}")
            escribir(win, y0 + alto - 1, 2, elipsis(texto, ancho - 4), ESPUMA)

    def tecla(self, app, k):
        if self._copiando_ahora(app) is not None:
            if k in ("\x1b", "x"):
                app.trabajo.cancelar.set()
                app.avisar("cancelando: termina la pista en curso y para…", ARENA)
                return True
            return False

        terminada = self._terminada(app)
        if k == curses.KEY_UP:
            if len(app.datos.destinos) > 1:
                self.destino = max(0, self.destino - 1)
            else:
                self.desplazado = max(0, self.desplazado - 1)
        elif k == curses.KEY_DOWN:
            if len(app.datos.destinos) > 1:
                self.destino = min(len(app.datos.destinos) - 1, self.destino + 1)
            else:
                self.desplazado += 1
        elif k == curses.KEY_PPAGE:
            self.desplazado = max(0, self.desplazado - 10)
        elif k == curses.KEY_NPAGE:
            self.desplazado += 10
        elif k in ("\n", "\r", curses.KEY_ENTER, "c"):
            if terminada is not None:
                # «copiar otra vez»: de vuelta a la lista, mirada de nuevo
                self.cerrada = terminada
                self.desplazado = 0
                self.calcular(app)
            else:
                self._pedir_copia(app, rehacer=False)
        elif k == "w":
            self._pedir_copia(app, rehacer=True)
        elif k == "n" and terminada is None:
            self._simular(app)
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

    def _listos(self, app, destino):
        """El reparto de ahora, o None tras avisar de por qué no lo hay."""
        if destino is None:
            app.avisar("no hay ninguna tarjeta montada", CORAL)
            return None
        reparto = self._reparto(app)
        if reparto is None:
            self.calcular(app)
            app.avisar("un momento: estoy mirando qué hay en la tarjeta", ARENA)
        return reparto

    def _simular(self, app):
        destino = self.montaje(app)
        reparto = self._listos(app, destino)
        if reparto is None:
            return
        if not reparto["nuevos"]:
            app.avisar("no hay nada nuevo que copiar", ESPUMA)
            return
        app.copiar_fiio(destino, list(reparto["nuevos"]), simular=True, rehacer=False)

    def _pedir_copia(self, app, rehacer):
        """↵ (rehacer=False) copia sin preguntar los álbumes que aún no están:
        no pisa nada. w (rehacer=True) reescribe, tras confirmar, los que
        están en otro orden o incompletos, porque antes los borra de la
        tarjeta. La paleta llama aquí con los mismos argumentos."""
        destino = self.montaje(app)
        reparto = self._listos(app, destino)
        if reparto is None:
            return
        libre = destino["libre"]
        if not rehacer:
            albumes = list(reparto["nuevos"])
            if not albumes:
                app.avisar("la tarjeta está al día: no hay nada nuevo que copiar", ESPUMA)
                return
            falta = sum(a["bytes"] for a in albumes) - libre
            if falta > 0:
                app.avisar(f"no cabe: faltan {comun.formato_bytes(falta)}", CORAL)
                return
            app.copiar_fiio(destino, albumes, simular=False, rehacer=False)
            return

        albumes = self._arreglar(reparto)
        if not albumes:
            app.avisar("no hay nada en otro orden ni incompleto", ESPUMA)
            return
        peso = sum(a["bytes"] for a in albumes)
        falta = peso - sum(self.en_tarjeta.get(a["rel"], 0) for a in albumes) - libre
        if falta > 0:
            app.avisar(f"no cabe: faltan {comun.formato_bytes(falta)}", CORAL)
            return
        pregunta = (f"¿Reescribir {self._plural(len(albumes), 'álbum', 'álbumes')} "
                    f"({comun.formato_bytes(peso)}) en {self.etiqueta(destino)}? "
                    f"Primero se BORRAN de la tarjeta y luego se copian otra vez en orden.")
        app.abrir(OverlayConfirmar(
            pregunta,
            lambda: app.copiar_fiio(destino, albumes, simular=False, rehacer=True)))


class VistaLog(Vista):
    nombre = "Log"

    ETIQUETAS = {
        "candidato": LILA, "descargar": AGUA, "archivo": TENUE,
        "listo": ESPUMA, "buscar": CIELO, "cola": CIELO, "plugin": LILA,
        "reintento": ARENA, "error": CORAL, "descartado": TENUE,
    }
    PATRON = PATRON_LOG

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

        # Lo recién llegado entra brillando y se asienta en su color.
        cuantas, cuando = app.datos.log_nuevas
        brillo_ = 0.0
        if not self.filtro and cuantas:
            brillo_ = 1 - suave((time.monotonic() - cuando) / 1.8)
            if brillo_ > 0:
                _anima()
        primera_nueva = len(lineas) - cuantas
        for i in range(alto):
            indice = self.desplazado + i
            if indice >= len(lineas):
                break
            self._linea(win, y0 + i, ancho, lineas[indice], indice == len(lineas) - 1,
                        brillo_ if indice >= primera_nueva else 0.0)

    def _linea(self, win, y, ancho, linea, ultima, brillo_=0.0):
        coincide = self.PATRON.match(linea)
        if not coincide:
            escribir(win, y, 1, elipsis(linea, ancho - 2), TENUE)
            return
        marca, etiqueta, resto = coincide.groups()
        escribir(win, y, 1, marca[11:19] if len(marca) > 18 else marca, TENUE)
        x = 11
        tono = self.ETIQUETAS.get((etiqueta.split() or [""])[0], NORMAL)
        texto = BLANCO if ultima else NORMAL
        if brillo_ > 0 and _PALETA["colores"] >= 256:
            tono = (mezclar(_PALETA["tema"][tono][0] if tono != NORMAL else TINTA_TERMINAL,
                            195, brillo_),)
            texto = (mezclar(TINTA_TERMINAL, 195, brillo_),)
        escribir(win, y, x, rellenar(etiqueta, 11), tono)
        escribir(win, y, x + 12, elipsis(resto, ancho - x - 13), texto)

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

        anotar("titulo", f"Instalación  (Nicotine+ {comun.SABOR_NICOTINE})")
        destino = (os.path.realpath(comun.ENLACE_PLUGIN)
                   if os.path.exists(comun.ENLACE_PLUGIN) else None)
        if destino and os.path.isfile(os.path.join(destino, "__init__.py")):
            anotar(OK, f"el plugin está instalado en {comun.SABOR_NICOTINE}", destino)
        else:
            anotar(MAL, "el plugin NO está instalado",
                   f"ejecuta: {comun.orden_instalador()}")

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
            arranque = comun.arranque_proceso(pid)
            marca = comun.ultimo_arranque_plugin()
            if marca is None:
                anotar(MAL, "el plugin nunca se ha cargado",
                       "no hay '[plugin] activado' en pipeline.log")
            elif arranque is None:
                cuando = datetime.fromtimestamp(marca).strftime("%H:%M:%S")
                anotar(AVISO, f"el plugin se cargó por última vez a las {cuando}",
                       "no pude saber cuándo arrancó Nicotine+ para comparar")
            elif marca >= arranque - 5:
                cuando = datetime.fromtimestamp(marca).strftime("%H:%M:%S")
                anotar(OK, f"el plugin se cargó en esta sesión ({cuando})")
            else:
                anotar(MAL, "el plugin no se cargó en esta sesión de Nicotine+",
                       "mira el log de Nicotine+ por si dio error al cargar")

        if comun.SABOR_NICOTINE == "snap":
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
        if self.revisando and not self.resultados:
            espera(win, y0, alto, ancho, "revisando la instalación…")
            return
        y = y0
        for marca, texto, detalle, _accion in self.resultados:
            if y >= y0 + alto:
                break
            if marca == "titulo":
                if y > y0:
                    y += 1
                if y < y0 + alto:
                    x = escribir(win, y, 2, texto, BLANCO)
                    escribir(win, y, x + 1, VACIO * max(0, ancho - x - 3), LINEA)
                    y += 1
                continue
            tinta = {OK: ESPUMA, MAL: CORAL, AVISO: ARENA}.get(marca, NORMAL)
            escribir(win, y, 3, marca, tinta)
            escribir(win, y, 5, elipsis(texto, ancho - 6),
                     NORMAL if marca == OK else BLANCO)
            y += 1
            if detalle and y < y0 + alto:
                escribir(win, y, 7, elipsis("↳ " + detalle, ancho - 8),
                         TENUE if marca == OK else CIELO)
                y += 1
        if self.revisando and y < y0 + alto:
            escribir_trozos(win, y + 1, 3, cargando("comprobando…"))

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
    """Dibuja una ventana centrada y devuelve (y, x, alto, ancho) de su interior.

    Es una tarjeta con su propio fondo y el borde redondeado; al abrirse, el
    borde sube desde lo hondo hasta su azul mientras lo de detrás se hunde."""
    win = app.pantalla
    pantalla_alto, pantalla_ancho = win.getmaxyx()
    ancho = min(ancho, pantalla_ancho - 2)
    alto = min(alto, pantalla_alto - 2)
    y0 = max(0, (pantalla_alto - alto) // 2)
    x0 = max(0, (pantalla_ancho - ancho) // 2)

    for y in range(y0, y0 + alto):
        pintar(win, y, x0, ancho, VENTANA)

    u = frenada((time.monotonic() - app._overlay_desde) / 0.25)
    if u < 1:
        _anima()
    borde = (rampa(OCEANO, 0.05 + 0.3 * u),)
    sufijo = f" {cola} " if cola else ""
    titulo = f" {elipsis(titulo, max(1, ancho - 8 - len(sufijo)))} "
    relleno = max(0, ancho - 4 - len(titulo) - len(sufijo))
    x = escribir(win, y0, x0, "╭─", borde)
    x = escribir(win, y0, x, titulo, (rampa(OCEANO, 0.3 + 0.4 * u),), True)
    x = escribir(win, y0, x, VACIO * relleno, borde)
    if sufijo:
        x = escribir(win, y0, x, sufijo, TENUE)
    escribir(win, y0, x, "─╮", borde)
    for y in range(y0 + 1, y0 + alto - 1):
        escribir(win, y, x0, "│", borde)
        escribir(win, y, x0 + ancho - 1, "│", borde)
    escribir(win, y0 + alto - 1, x0, "╰" + VACIO * (ancho - 2) + "╯", borde)
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
        escribir(win, y + 1, x, "▸", AGUA)
        campo = elipsis(self.texto, ancho - 3)
        escribir(win, y + 1, x + 2, campo, BLANCO)
        escribir(win, y + 1, x + 2 + len(campo), *caret())
        escribir(win, y + 2, x, "«Artista - Álbum» funciona mejor que un título suelto",
                 TENUE, ancho=ancho)

        escribir(win, y + 4, x, "tipo", TENUE)
        escribir(win, y + 4, x + 8, " álbum ", NORMAL if self.cancion else INV)
        escribir(win, y + 4, x + 17, " canción ", INV if self.cancion else NORMAL)
        escribir(win, y + 4, x + 30, "⇥ cambia", TENUE)

        escribir(win, y + 6, x, "↵ encolar", AGUA)
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


class OverlayImportar(Overlay):
    """Elegir un CSV de playlist (Exportify, TuneMyMusic…) y encolar lo que traiga.

    Dos pasos: primero la lista de .csv a la vista en las carpetas de siempre;
    con ↵ se lee y se enseña el plan (qué se pide, qué ya tienes), y con otro
    ↵ se encola. ⇥ cambia entre pedir canciones o los álbumes completos."""

    def __init__(self):
        self.archivos = listas.csv_candidatos()
        self.cursor = 0
        self.plan = None
        self.error = ""
        self.albumes = False
        self.repetir = False

    def _calcular(self):
        if not self.archivos:
            return
        ruta = self.archivos[self.cursor][0]
        try:
            self.plan = listas.plan(ruta, albumes=self.albumes, repetir=self.repetir)
            self.error = ""
        except (OSError, ValueError) as error:
            self.plan, self.error = None, f"{os.path.basename(ruta)}: {error}"

    def dibujar(self, app):
        if self.plan is None and not self.error:
            self._lista(app)
        else:
            self._resumen(app)

    def _lista(self, app):
        win = app.pantalla
        n = len(self.archivos)
        y, x, alto, ancho = _marco(app, max(min(n, 12) + 5, 8), 78, "importar playlist")
        if not self.archivos:
            escribir(win, y, x, "No encontré ningún .csv en Descargas, Música, Documentos "
                                "ni el escritorio.", ARENA, ancho=ancho)
            escribir(win, y + 2, x, "Exporta la playlist con Exportify o TuneMyMusic y "
                                    "guarda el CSV en una de esas carpetas.", TENUE, ancho=ancho)
            escribir(win, y + 4, x, "esc cerrar", TENUE)
            return
        escribir(win, y, x, "qué archivo", TENUE)
        casa = os.path.expanduser("~")
        sitio = max(1, alto - 3)
        desde_ = max(0, min(self.cursor - sitio + 1, n - sitio))
        for i in range(sitio):
            indice = desde_ + i
            if indice >= n:
                break
            ruta, mtime = self.archivos[indice]
            elegido = indice == self.cursor
            fila = y + 1 + i
            tinta = SEL if elegido else None
            if elegido:
                pintar(win, fila, x - 1, ancho + 2, SEL)
                escribir(win, fila, x - 1, "▎", AGUA)
            carpeta = os.path.dirname(ruta).replace(casa, "~")
            cuando = desde(datetime.fromtimestamp(mtime).isoformat())
            escribir(win, fila, x, elipsis(os.path.basename(ruta), ancho - 34),
                     tinta or BLANCO)
            escribir(win, fila, x + ancho - 32, rellenar(carpeta, 20), tinta or TENUE)
            escribir(win, fila, x + ancho - len(cuando), cuando, tinta or TENUE)
        escribir(win, y + alto - 1, x, "↵ ver qué se pediría", AGUA)
        escribir(win, y + alto - 1, x + 22, "esc cancelar", TENUE)

    def _resumen(self, app):
        win = app.pantalla
        p = self.plan
        if p is None:
            y, x, alto, ancho = _marco(app, 7, 78, "importar playlist")
            escribir(win, y, x, elipsis(self.error, ancho), CORAL)
            escribir(win, y + 2, x, "Hacen falta columnas de artista y título, como las que "
                                    "exportan Exportify o TuneMyMusic.", TENUE, ancho=ancho)
            escribir(win, y + 4, x, "esc volver", TENUE)
            return

        pedir = p["pedir"]
        que = "álbumes" if p["tipo"] == "album" else "canciones"
        avisos = p["avisos"][:2]
        cabecera = 2 + bool(p["tenidas"]) + bool(p["en_cola"]) + len(avisos)
        alto = cabecera + 1 + min(len(pedir) + 1, 14) + 4
        y, x, alto, ancho = _marco(app, alto, 78, f"importar «{p['nombre']}»")
        fondo = y + alto
        casa = os.path.expanduser("~")

        escribir(win, y, x, elipsis(f"{len(p['canciones'])} canciones en "
                                    f"{p['ruta'].replace(casa, '~')}", ancho), TENUE)
        y += 1
        if p["tenidas"]:
            escribir(win, y, x, f"{OK} {len(p['tenidas'])} ya en la biblioteca, no se piden",
                     TENUE)
            y += 1
        if p["en_cola"]:
            escribir(win, y, x, f"{GLIFO_ESTADO[comun.PENDIENTE]} {len(p['en_cola'])} "
                                f"ya en la cola", TENUE)
            y += 1
        for aviso in avisos:
            escribir(win, y, x, elipsis(f"{AVISO} {aviso}", ancho), ARENA)
            y += 1
        y += 1
        if pedir:
            xx = escribir(win, y, x, f"» se piden {len(pedir)} {que}", BLANCO)
            escribir(win, y, xx + 2, elipsis(f"{listas.estimacion(len(pedir), self.albumes)}, "
                                             f"{listas.ritmo(self.albumes)}",
                                             ancho - (xx - x) - 2), TENUE)
            y += 1
            sitio = max(0, fondo - 3 - y)
            for i, e in enumerate(pedir[:sitio]):
                if sitio and i == sitio - 1 and len(pedir) > sitio:
                    escribir(win, y + i, x + 2, f"… y {len(pedir) - i} más", TENUE)
                    break
                escribir(win, y + i, x + 2, elipsis(f"{GLIFO_ESTADO[comun.PENDIENTE]} "
                                                    f"{e['consulta']}", ancho - 2), NORMAL)
        else:
            escribir(win, y, x, "Nada que pedir: todo está ya en la biblioteca o en la cola.",
                     ARENA, ancho=ancho)

        escribir(win, fondo - 2, x, "pedir", TENUE)
        escribir(win, fondo - 2, x + 7, " canciones ", NORMAL if self.albumes else INV)
        escribir(win, fondo - 2, x + 19, " álbumes ", INV if self.albumes else NORMAL)
        escribir(win, fondo - 2, x + 30, "⇥ cambia", TENUE)
        escribir(win, fondo - 2, x + 41, "r", AGUA, True)
        escribir(win, fondo - 2, x + 43, "con lo que ya tienes: "
                                         + ("sí" if self.repetir else "no"), TENUE)
        if pedir:
            escribir(win, fondo - 1, x, f"↵ encolar {len(pedir)} {que}", AGUA)
            escribir(win, fondo - 1, x + 12 + len(str(len(pedir))) + len(que) + 2,
                     "esc volver", TENUE)
        else:
            escribir(win, fondo - 1, x, "esc volver", TENUE)

    def tecla(self, app, k):
        if self.plan is None and not self.error:
            n = len(self.archivos)
            if k == curses.KEY_UP:
                self.cursor = max(0, self.cursor - 1)
            elif k == curses.KEY_DOWN:
                self.cursor = min(max(0, n - 1), self.cursor + 1)
            elif k == curses.KEY_HOME:
                self.cursor = 0
            elif k == curses.KEY_END:
                self.cursor = max(0, n - 1)
            elif k in ("\n", "\r", curses.KEY_ENTER):
                self._calcular()
            elif k in ("\x1b", "q"):
                app.cerrar_overlay()
            return True
        if k == "\x1b":
            self.plan, self.error = None, ""          # volver a la lista
        elif k == "\t" and self.plan is not None:
            self.albumes = not self.albumes
            self._calcular()
        elif k == "r" and self.plan is not None:
            self.repetir = not self.repetir
            self._calcular()
        elif k in ("\n", "\r", curses.KEY_ENTER) and self.plan is not None:
            self._encolar(app)
        return True

    def _encolar(self, app):
        p = self.plan
        app.cerrar_overlay()
        if not p["pedir"]:
            app.avisar("nada que pedir de esa lista", ARENA)
            return
        creadas = listas.encolar(p)
        que = "álbumes" if p["tipo"] == "album" else "canciones"
        app.avisar(f"encoladas {len(creadas)} {que} de «{p['nombre']}» · "
                   f"{listas.estimacion(len(creadas), p['tipo'] == 'album')}", ESPUMA, 6)
        if creadas and not app.datos.nicotine_vivo():
            app.avisar("encoladas, pero Nicotine+ está cerrado: la cola no avanza",
                       ARENA, 6)
        app.ir_a(0)


class OverlayBuscar(Overlay):
    """Filtro incremental para la vista que lo abre."""

    def __init__(self, vista):
        self.vista = vista
        self.texto = vista.filtro

    def dibujar(self, app):
        y, x, _alto, ancho = _marco(app, 5, 56, "buscar")
        win = app.pantalla
        escribir(win, y, x, "/", AGUA)
        campo = elipsis(self.texto, ancho - 3)
        escribir(win, y, x + 2, campo, BLANCO)
        escribir(win, y, x + 2 + len(campo), *caret())
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


class OverlayFicha(Overlay):
    """Todo lo que se sabe de una petición, encima de la Cola: la ficha, lo
    que se mueve en su estado, y debajo pistas, fuentes e historia. Se lee de
    la cola en cada cuadro, así que sigue viva mientras está abierta."""

    def __init__(self, id_pet):
        self.id_pet = id_pet

    def dibujar(self, app):
        win = app.pantalla
        alto_p, ancho_p = win.getmaxyx()
        y, x, alto, ancho = _marco(app, alto_p - 2, min(ancho_p - 4, 124), "ficha",
                                   "esc cierra")
        pet = comun.buscar_peticion(app.datos.cola, self.id_pet)
        if pet is None:
            escribir(win, y, x, "Esa petición ya no está en la cola.", TENUE)
            return
        self._detalle(app, pet, y, alto, x, ancho)

    def tecla(self, app, k):
        if k in ("\x1b", "\n", "\r", curses.KEY_ENTER, "q"):
            app.cerrar_overlay()
        return True

    def _detalle(self, app, pet, y0, alto, x, ancho):
        """Todo lo que se sabe de la petición, repartido según el sitio que haya:
        ficha (a dos columnas si cabe), el bloque vivo del estado, y debajo
        pistas, fuentes e historia; con mucho ancho, pistas a un lado y el
        resto al otro."""
        if pet is None or ancho < 20:
            return
        win = app.pantalla
        t = time.monotonic()
        estado = pet["estado"]
        tope = y0 + alto

        glifo, color_glifo = glifo_estado(estado, t)
        derecha = f"{estado} · {desde(pet.get('actualizado', ''))}"
        escribir(win, y0, x, elipsis(pet["consulta"], max(8, ancho - len(derecha) - 4)),
                 BLANCO)
        escribir(win, y0, x + ancho - len(derecha) - 2, glifo, color_glifo)
        escribir(win, y0, x + ancho - len(derecha), derecha,
                 COLOR_ESTADO.get(estado, NORMAL))
        escribir(win, y0 + 1, x, VACIO * ancho, LINEA)
        y = y0 + 2

        campos = self._campos(app, pet)
        if alto < 18:
            # Poca altura: se quedan los campos de más peso, en su orden.
            limite = max(2, (alto - 2) // 3)
            elegidos = set(id(c) for c in sorted(campos, key=lambda c: c[3])[:limite])
            campos = [c for c in campos if id(c) in elegidos]
        y = self._ficha(win, y, x, ancho, campos, tope)
        y = self._vivo(app, pet, y, x, ancho, tope, t, holgado=alto >= 18)

        secciones = [s for s in (self._pistas(app, pet, ancho, t),
                                 self._fuentes(app, pet, ancho),
                                 self._historia(app, pet, ancho))
                     if s is not None]
        resto = tope - y
        if not secciones or resto < 3:
            return
        lado_a_lado = (ancho >= 110 and resto >= 12 and len(secciones) > 1
                       and secciones[0][0].startswith("pistas"))
        if lado_a_lado:
            col = (ancho - 3) // 2
            self._apilar(win, y, x, col, resto, secciones[:1])
            for yy in range(y + 1, tope):
                escribir(win, yy, x + col + 1, "│", LINEA)
            self._apilar(win, y, x + col + 3, ancho - col - 3, resto, secciones[1:])
        else:
            self._apilar(win, y, x, ancho, resto, secciones)

    @staticmethod
    def _ficha(win, y, x, ancho, campos, tope):
        if not campos or y >= tope:
            return y
        if ancho >= 84:
            col = (ancho - 3) // 2
            mitad = (len(campos) + 1) // 2
            columnas = [campos[:mitad], campos[mitad:]]
            xs = [x, x + col + 3]
        else:
            col, columnas, xs = ancho, [campos], [x]
        filas = max(len(c) for c in columnas)
        for i in range(filas):
            if y + i >= tope:
                break
            for cx, c in zip(xs, columnas):
                if i < len(c):
                    etiqueta, valor, color, _peso = c[i]
                    escribir(win, y + i, cx, rellenar(etiqueta, 10), TENUE)
                    escribir(win, y + i, cx + 10, elipsis(str(valor), col - 10), color)
        return y + min(filas, tope - y)

    def _campos(self, app, pet):
        """(etiqueta, valor, color, peso). El peso decide qué sobrevive cuando
        hay poca altura: 1 lo esencial, 2 lo útil, 3 lo prescindible."""
        estado = pet["estado"]
        creado = pet.get("creado", "")
        campos = [("pedido", f"{hora_de(creado)} · {desde(creado)}", NORMAL, 2),
                  ("tipo", "canción suelta" if pet.get("tipo") == "cancion" else "álbum",
                   NORMAL, 3)]
        if pet.get("lista"):
            campos.append(("lista", pet["lista"], LILA, 2))
        if estado == comun.BUSCANDO:
            consulta = pet["consulta"]
            termino = consulta if "flac" in consulta.casefold() else f"{consulta} flac"
            campos.append(("término", termino, CIELO, 1))
        if estado == comun.PENDIENTE:
            activas = [p["id"] for p in app.datos.activas()]
            delante = activas.index(pet["id"]) if pet["id"] in activas else 0
            campos.append(("turno", "es la siguiente" if delante == 0
                           else f"{delante} por delante", ARENA, 1))
        cand = pet.get("candidato") or {}
        if cand:
            campos.append(("fuente", f"{cand.get('usuario', '?')} · "
                                     f"{cand.get('puntos', '?')} pts", AGUA, 1))
            campos.append(("carpeta", os.path.basename(
                (cand.get("carpeta") or "").replace("\\", "/")), NORMAL, 3))
            campos.append(("calidad", cand.get("detalle", ""), NORMAL,
                           1 if estado == comun.DESCARGANDO else 2))
        resultado = pet.get("resultado") or {}
        if resultado:
            campos.append(("destino", resultado.get("carpeta", ""), ESPUMA, 1))
            if resultado.get("faltantes"):
                campos.append(("faltan", ", ".join(
                    f"{n:02d}" for n in resultado["faltantes"]), ARENA, 1))
        if estado in comun.TERMINADOS:
            tardo = duracion_entre(creado, pet.get("actualizado", ""))
            if tardo:
                campos.append(("tardó", tardo, TENUE, 2))
        if pet.get("intentos"):
            campos.append(("intentos", f"{pet['intentos']} agotado(s)", ARENA, 3))
        if pet.get("rechazados"):
            campos.append(("descartes", f"{len(pet['rechazados'])} fuente(s)", TENUE, 3))
        mensaje = pet.get("mensaje") or ""
        if mensaje and estado in (comun.FALLIDO, comun.CANCELADO):
            campos.append(("motivo" if estado == comun.FALLIDO else "nota", mensaje,
                           CORAL if estado == comun.FALLIDO else TENUE, 1))
        return campos

    def _vivo(self, app, pet, y, x, ancho, tope, t, holgado=True):
        """Lo que se mueve: la barra de descarga, la cuenta de la búsqueda o el
        pulso de la espera. Cada estado en marcha tiene el suyo."""
        win = app.pantalla
        estado = pet["estado"]
        if tope - y < 2:
            return y
        hueco = 1 if holgado else 0

        if estado == comun.DESCARGANDO:
            y += hueco
            p = pet.get("progreso") or {}
            hechos, total = p.get("terminados", 0), p.get("total", 0) or 1
            frac = hechos / total
            cola = f"  {int(frac * 100):>3} %  {hechos} de {total} pistas"
            ancho_barra = max(8, min(60, ancho - 10 - len(cola)))
            escribir(win, y, x, rellenar("progreso", 10), TENUE)
            barra(win, y, x + 10, ancho_barra, frac, AGUA,
                  vivo=app.datos.velocimetro.actual > 0, t=t)
            escribir(win, y, x + 10 + ancho_barra, cola, AGUA)
            # una fila de aire: la barra y el oleaje de debajo son bloques
            # macizos y, pegados, se leen como una sola mancha
            y += 1 + hueco
            if y < tope:
                v = app.datos.velocimetro
                escribir(win, y, x, rellenar("velocidad", 10), TENUE)
                xx = escribir(win, y, x + 10,
                              rellenar(f"{comun.formato_bytes(v.actual)}/s", 11), BLANCO)
                # Por orden de prioridad: el contador (a la derecha), el
                # medidor y, si aún sobra sitio, la chispa; ésta ya va en la
                # barra de estado, así que es la primera que se cae.
                bajado = f"{comun.formato_bytes(v.estimado())} bajados"
                fin = x + ancho
                if fin - xx >= len(bajado) + 10:
                    escribir(win, y, fin - len(bajado), bajado, TENUE)
                    fin -= len(bajado) + 2
                sitio = fin - xx
                if sitio >= 8:
                    xx = escribir_trozos(win, y, xx, medidor(v, min(16, sitio), t)) + 2
                sitio = fin - xx
                if sitio >= 8:
                    escribir_trozos(win, y, xx, chispa(v.historial, min(30, sitio)))
                y += 1

        elif estado == comun.BUSCANDO:
            y += hueco
            transcurrido = segundos_desde(pet.get("actualizado", ""))
            frac = min(1.0, transcurrido / VENTANA_BUSQUEDA)
            if transcurrido < VENTANA_BUSQUEDA:
                cola = f"  {int(transcurrido):>2} s de {VENTANA_BUSQUEDA} · acumulando resultados"
                color = CIELO
            elif transcurrido < VENTANA_BUSQUEDA + 6:
                cola, color = "  puntuando lo que llegó", CIELO
            else:
                cola = (f"  sin resultados aún · el plugin espera hasta "
                        f"{VENTANA_BUSQUEDA * 3} s")
                color = ARENA
            ancho_barra = max(8, min(40, ancho - 12 - len(cola)))
            escribir(win, y, x, rellenar("búsqueda", 10), TENUE)
            glifo, color_glifo = sonar(t)
            escribir(win, y, x + 10, glifo, color_glifo)
            barra(win, y, x + 12, ancho_barra, frac, CIELO, t=t)
            escribir(win, y, x + 12 + ancho_barra, elipsis(cola, ancho - 12 - ancho_barra),
                     color)
            y += 1

        elif estado == comun.PENDIENTE:
            y += hueco
            escribir(win, y, x, rellenar("turno", 10), TENUE)
            if app.datos.nicotine_vivo():
                escribir(win, y, x + 10, GLIFO_ESTADO[comun.PENDIENTE], respiro(t))
                escribir(win, y, x + 12, elipsis("esperando a que el plugin la tome",
                                                 ancho - 12), TENUE)
            else:
                escribir(win, y, x + 10, elipsis(
                    f"{AVISO} Nicotine+ está cerrado: la cola no avanza", ancho - 10), ARENA)
            y += 1
        return y

    # ── las tres secciones: (título, filas, foco) o None ──
    def _pistas(self, app, pet, ancho, t):
        estado = pet["estado"]
        if estado == comun.DESCARGANDO:
            return self._pistas_bajando(app, pet, ancho, t)
        if estado != comun.LISTO:
            return None
        carpeta = (pet.get("resultado") or {}).get("carpeta")
        if not carpeta:
            return None
        ruta = os.path.join(comun.RAIZ_BIBLIOTECA, carpeta)
        if not os.path.isdir(ruta):
            # quizá se pasó a la heredada con h, para compartirla
            ruta = os.path.join(Biblioteca.RAIZ_HEREDADA, carpeta)
        if not os.path.isdir(ruta):
            return ("pistas", [([(f"{AVISO} la carpeta ya no está en {carpeta}", ARENA)], [])],
                    None)
        app.biblioteca.pedir_analisis({"dir": ruta})
        det = app.biblioteca.detalle.get(ruta)
        if det is None:
            return ("pistas", [(cargando("leyendo las pistas…", t), [])], None)

        pistas = sorted(det["pistas"], key=lambda p: (p.get("disco") or 1, p.get("pista") or 0))
        faltan = set(det.get("huecos") or [])
        filas, total = [], 0
        for p in pistas:
            n = p.get("pista")
            while faltan and min(faltan) < (n or 0):
                h = min(faltan)
                faltan.discard(h)
                filas.append(([(f"{h:02d}  ", ARENA), ("— falta —", ARENA)], []))
            total += p.get("tamaño", 0)
            filas.append(([(f"{n:02d}  " if n else "··  ", TENUE),
                           (p.get("titulo") or p["archivo"], NORMAL)],
                          [(duracion(p.get("duracion")), TENUE), ("  ", None),
                           (f"{comun.formato_bytes(p.get('tamaño', 0)):>9}", TENUE),
                           ("  ", None), (OK, ESPUMA)]))
        for h in sorted(faltan):
            filas.append(([(f"{h:02d}  ", ARENA), ("— falta —", ARENA)], []))
        if not filas:
            filas.append(([(det.get("calidad") or "sin pistas legibles", ARENA)], []))
        titulo = f"pistas · {len(pistas)} · {det.get('calidad', '')} · {comun.formato_bytes(total)}"
        return (titulo, filas, None)

    def _pistas_bajando(self, app, pet, ancho, t):
        datos = app.datos
        p = pet.get("progreso") or {}
        total = p.get("total", 0) or 0
        hechos = max(p.get("terminados", 0), len(datos.bajados))
        filas = []
        for i, (nombre, b) in enumerate(datos.bajados, 1):
            filas.append(([(f"{i:02d}  ", TENUE), (_sin_numero(nombre), NORMAL)],
                          [(f"{comun.formato_bytes(b):>9}", TENUE), ("  ", None), (OK, ESPUMA)]))
        foco = None
        v = datos.velocimetro
        for nombre, b in datos.incompletos:
            estimado = b + v.actual * max(0.0, time.monotonic() - datos.muestreado_en)
            foco = len(filas)
            filas.append(([(f"{len(filas) + 1:02d}  ", BLANCO),
                           (_sin_numero(nombre), BLANCO)],
                          corriente(10 if ancho >= 50 else 6, t)
                          + [(f" {comun.formato_bytes(estimado):>9}", AGUA)]))
        pendientes = total - len(filas)
        for _i in range(max(0, pendientes)):
            filas.append(([(f"{len(filas) + 1:02d}  ", LINEA), ("pendiente", TENUE)],
                          [("·", LINEA)]))
        if not filas:
            filas.append((cargando("esperando al primer archivo…", t, TENUE), []))
        titulo = f"pistas · {hechos} de {total} bajadas"
        return (titulo, filas, foco)

    def _fuentes(self, app, pet, ancho):
        candidatos = app.datos.candidatos(pet["id"])
        if not candidatos:
            return None
        cand = pet.get("candidato") or {}
        elegida = (cand.get("usuario"),
                   os.path.basename((cand.get("carpeta") or "").replace("\\", "/")))
        descartadas = {(u, os.path.basename((c or "").replace("\\", "/")))
                       for u, c in (pet.get("rechazados") or [])}
        filas = []
        for c in candidatos:
            clave = (c["usuario"], c["carpeta"])
            es_elegida = clave == elegida
            es_descartada = clave in descartadas
            izq = [(f"{c['puntos']:>4}  ", AGUA if es_elegida else TENUE),
                   (rellenar(c["usuario"], 16) + "  ", BLANCO if es_elegida else NORMAL)]
            if ancho >= 56:
                izq.append((rellenar(c["detalle"], 28) + "  ", TENUE))
            if ancho >= 84:
                izq.append((c["carpeta"], TENUE))
            corto = ancho < 70
            der = ([("◂" if corto else "◂ elegida", AGUA)] if es_elegida
                   else [(MAL if corto else f"{MAL} descartada", CORAL)] if es_descartada
                   else [])
            filas.append((izq, der))
        return (f"fuentes · {len(filas)} candidatas", filas, None)

    def _historia(self, app, pet, ancho):
        lineas = app.datos.historia(pet["id"])
        filas = []
        for linea in lineas:
            m = PATRON_LOG.match(linea)
            if not m:
                continue
            marca, etiqueta, resto = m.groups()
            if etiqueta == "candidato":
                continue                       # eso ya está en «fuentes»
            resto = (resto.replace("/" + pet["id"], "").replace(pet["id"], "")
                          .replace(comun.DIR_DESCARGAS, ".descargas")
                          .replace(comun.RAIZ_BIBLIOTECA + os.sep, ""))
            resto = re.sub(r"\s{2,}", " ", resto).strip(" :")
            color = VistaLog.ETIQUETAS.get(etiqueta.split()[0], NORMAL)
            hora = marca[11:19] if len(marca) > 18 else marca
            filas.append(([(hora + "  ", TENUE), (rellenar(etiqueta, 10) + " ", color),
                           (resto, NORMAL)], []))
        if not filas:
            return None
        if pet["estado"] in comun.EN_CURSO:
            izq, der = filas[-1]
            filas[-1] = ([izq[0], izq[1], (izq[2][0], BLANCO)], der)
        return (f"historia · {len(filas)} líneas", filas, len(filas) - 1)

    @staticmethod
    def _apilar(win, y, x, ancho, alto, secciones):
        """Reparte `alto` filas entre las secciones: cada una recibe hasta 4 de
        entrada y lo que sobre se reparte por orden. Cada sección empieza con
        una fila en blanco."""
        necesidades = [len(filas) + 2 for _t, filas, _f in secciones]
        alturas = [0] * len(secciones)
        libre = alto
        for i, n in enumerate(necesidades):
            alturas[i] = min(n, 4, libre)
            libre -= alturas[i]
        for i, n in enumerate(necesidades):
            extra = min(n - alturas[i], libre)
            alturas[i] += extra
            libre -= extra
        for (titulo, filas, foco), h in zip(secciones, alturas):
            if h < 3:
                continue
            pintar_seccion(win, y + 1, x, ancho, h - 1, titulo, filas, foco)
            y += h


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
        escribir(win, y + 4, x, "s", ESPUMA, True)
        escribir(win, y + 4, x + 2, "sí", NORMAL)
        escribir(win, y + 4, x + 8, "n", CORAL, True)
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
        # `ayuda` si la vista tiene teclas que no caben en el pie
        filas += [(t, d) for t, d in getattr(vista, "ayuda", vista.teclas)(app)]
        y, x, alto, ancho = _marco(app, len(filas) + 4, 62, "ayuda", "? cierra")
        win = app.pantalla
        for i, (tecla, texto) in enumerate(filas):
            if i >= alto:
                break
            if not tecla:
                escribir(win, y + i, x, texto, AGUA, True)
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
        escribir(win, y, x, ":", AGUA)
        campo = elipsis(self.texto, ancho - 12)
        escribir(win, y, x + 2, campo, BLANCO)
        escribir(win, y, x + 2 + len(campo), *caret())
        cola = f"{len(opciones)} de {len(app.ordenes())}"
        escribir(win, y, x + ancho - len(cola), cola, TENUE)
        escribir(win, y + 1, x, VACIO * ancho, LINEA)

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
                escribir(win, fila, x - 1, "▎", AGUA)
            escribir(win, fila, x, elipsis(nombre, ancho - 8),
                     SEL if elegido else NORMAL)
            if atajo:
                escribir(win, fila, x + ancho - len(atajo), atajo,
                         SEL if elegido else AGUA)

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
        # La última copia a la tarjeta (Trabajo.Copia), ya terminada: sigue
        # aquí aunque luego se expulse o se lance otra tarea.
        self.ultima_copia = None
        # {"montaje", "etiqueta", "ok"} del último intento de expulsar
        self.ultima_expulsion = None
        self.expulsando = False
        # {etiqueta: bytes/s} medidos en la última copia de verdad a cada tarjeta
        self.velocidades_fiio = Trabajo.Copia.leer_velocidades()
        self.vistas = [VistaCola(), VistaBiblioteca(), VistaFiio(),
                       VistaLog(), VistaSalud()]
        self.actual = 0
        self.overlay = None
        self._aviso = ""
        self._aviso_color = NORMAL
        self._aviso_desde = 0.0
        self._aviso_hasta = 0.0
        self._overlay_desde = 0.0
        self._animando = True
        self._sub_objetivo = None
        self._sub_desde = self._sub_actual = (0.0, 0.0)
        self._sub_t0 = 0.0
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
        if self.overlay is None:
            self._overlay_desde = time.monotonic()
        self.overlay = overlay

    def cerrar_overlay(self):
        self.overlay = None

    def avisar(self, texto, color=NORMAL, segundos=4.0):
        self._aviso = texto
        self._aviso_color = color
        self._aviso_desde = time.monotonic()
        self._aviso_hasta = self._aviso_desde + segundos

    # ── acciones ──
    def pedir(self, consultas, cancion=False):
        creadas = comun.añadir_peticiones(consultas, tipo="cancion" if cancion else "album")
        if not creadas:
            self.avisar("ya estaba en la cola", ARENA)
            return
        self.avisar(f"encolado {creadas[0]['consulta']}", ESPUMA)
        if not self.datos.nicotine_vivo():
            self.avisar("encolado, pero Nicotine+ está cerrado: la cola no avanza",
                        ARENA, 6)
        self.ir_a(0)

    def cancelar_peticion(self, pet):
        if pet is None:
            return
        if pet["estado"] not in comun.ACTIVOS:
            self.avisar("esa petición ya terminó", ARENA)
            return

        def _op(cola):
            objetivo = comun.buscar_peticion(cola, pet["id"])
            if objetivo is not None:
                objetivo["estado"] = comun.CANCELADO
                objetivo["actualizado"] = comun.ahora()
                objetivo["mensaje"] = "cancelada desde la interfaz"

        comun.modificar_cola(_op)
        comun.registrar(f"[cancelada] {pet['id']}")
        self.avisar(f"cancelada {pet['consulta']}", ARENA)

    def reintentar_peticion(self, pet):
        """Una fallida vuelve a esperar turno con los intentos a cero. Los
        descartes se quedan: el plugin no vuelve a elegir esas fuentes."""
        if pet is None or pet["estado"] != comun.FALLIDO:
            self.avisar("sólo se reintenta una petición fallida", ARENA)
            return

        def _op(cola):
            objetivo = comun.buscar_peticion(cola, pet["id"])
            if objetivo is None or objetivo["estado"] != comun.FALLIDO:
                return False
            objetivo.update(estado=comun.PENDIENTE, actualizado=comun.ahora(), intentos=0,
                            candidato=None, progreso=None, resultado=None, mensaje="")
            return True

        if not comun.modificar_cola(_op):
            self.avisar("esa petición ya no está fallida", ARENA)
            return
        # [cola] y no otra etiqueta: así Datos.historia no le pasa a esta
        # petición las líneas sin id de la que se esté bajando.
        comun.registrar(f"[cola] ↻ {pet['id']}  ({pet['consulta']}) otra vez, "
                        f"sin {len(pet.get('rechazados') or [])} fuente(s) descartada(s)")
        self.avisar(f"↻ {titulo_peticion(pet)} vuelve a la cola", ESPUMA)

    def quitar_peticion(self, pet):
        """Saca de cola.json una petición fallida. Lo que pasó sigue en el log."""
        if pet is None or pet["estado"] != comun.FALLIDO:
            self.avisar("sólo se quita una petición fallida; x cancela las demás", ARENA)
            return

        def _op(cola):
            antes = len(cola["peticiones"])
            cola["peticiones"] = [p for p in cola["peticiones"]
                                  if not (p["id"] == pet["id"] and p["estado"] == comun.FALLIDO)]
            return antes - len(cola["peticiones"])

        if comun.modificar_cola(_op):
            comun.registrar(f"[cola] - {pet['id']}  ({pet['consulta']}) quitada")
            self.avisar(f"quitada {titulo_peticion(pet)}", TENUE)

    def limpiar_cola(self):
        def _op(cola):
            antes = len(cola["peticiones"])
            cola["peticiones"] = [p for p in cola["peticiones"]
                                  if p["estado"] in comun.ACTIVOS]
            return antes - len(cola["peticiones"])

        quitadas = comun.modificar_cola(_op)
        self.avisar(f"{quitadas} petición(es) fuera de la cola", ESPUMA)

    def organizar(self):
        if self.trabajo.activo:
            self.avisar("ya hay una tarea en curso", ARENA)
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

    def pasar_a_heredada(self, albumes):
        if self.trabajo.activo:
            self.avisar("ya hay una tarea en curso", ARENA)
            return

        def tarea(trabajo):
            trabajo.registrar(f"▸ {len(albumes)} álbum(es) a la heredada")
            resumen = biblioteca.mover_a_heredada(albumes, informar=trabajo.registrar)
            for item in albumes:
                self.marcados.discard(item["rel"])
            self.biblioteca.cargar()
            if resumen["errores"]:
                self.avisar(f"{len(resumen['errores'])} error(es) al mover; "
                            f"mira la Cola", CORAL, 6)
            elif resumen["omitidos"]:
                self.avisar(f"{resumen['copiados']} movido(s), {resumen['omitidos']} "
                            f"ya estaban en la heredada", ARENA, 6)
            else:
                self.avisar(f"{resumen['copiados']} álbum(es) en la heredada", ESPUMA)
            return resumen

        self.trabajo.lanzar("pasando a la heredada", tarea, clase="organizar")

    def borrar_carpetas(self, carpetas, rels):
        if self.trabajo.activo:
            self.avisar("ya hay una tarea en curso", ARENA)
            return
        # si suena algo de lo que se va a borrar, se para antes
        sonando = self.reproductor.ruta if self.reproductor.sonando else ""
        if sonando and any(sonando.startswith(c.rstrip(os.sep) + os.sep) for c in carpetas):
            self.reproductor.parar()
        raiz = self.biblioteca.raiz

        def tarea(trabajo):
            trabajo.registrar(f"▸ borrando {len(carpetas)} carpeta(s)")
            resumen = biblioteca.borrar_carpetas(carpetas, raiz, informar=trabajo.registrar)
            for rel in rels:
                self.marcados.discard(rel)
            self.biblioteca.cargar()
            if resumen["errores"]:
                self.avisar(f"{len(resumen['errores'])} error(es) al borrar; "
                            f"mira la Cola", CORAL, 6)
            else:
                self.avisar(f"{resumen['borrados']} carpeta(s) borrada(s)", ESPUMA)
            return resumen

        self.trabajo.lanzar("borrando", tarea, clase="organizar")

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
            self.avisar("no hay tarjeta o no hay nada que copiar", ARENA)
            return
        if self.trabajo.activo:
            self.avisar("ya hay una tarea en curso", ARENA)
            return
        if self.expulsando:
            self.avisar("la tarjeta se está expulsando", ARENA)
            return
        raiz_destino = os.path.join(destino["montaje"], fiio.DESTINO_REL_POR_DEFECTO)
        albumes = list(plan)
        total = sum(a["bytes"] for a in albumes)
        etiqueta = (destino.get("etiqueta")
                    or os.path.basename(destino["montaje"].rstrip("/\\")))
        copia = Trabajo.Copia(albumes, simulacion=simular, rehacer=rehacer,
                              etiqueta=etiqueta, montaje=destino["montaje"])

        def tarea(trabajo):
            trabajo.total_bytes = 0 if simular else total
            resumen = None
            try:
                resumen = fiio.copiar(
                    raiz_destino, albumes, simular=simular, rehacer=rehacer,
                    informar=trabajo.registrar,
                    evento=trabajo.evento,
                    cancelado=trabajo.cancelar.is_set)
            except Exception as error:
                copia.errores.append({"rel": "", "error": str(error)})
                raise
            finally:
                copia.cerrar(resumen, trabajo.cancelar.is_set())
                self.ultima_copia = copia
                media = copia.velocidad_media()
                if media:
                    self.velocidades_fiio[etiqueta] = media
                    Trabajo.Copia.guardar_velocidades(self.velocidades_fiio)
            return resumen

        titulo = ("simulando la copia" if simular
                  else ("reescribiendo en la tarjeta" if rehacer else "copiando al FiiO"))
        self.trabajo.lanzar(f"{titulo} · {len(albumes)} álbumes", tarea, copia=copia)
        self.ir_a(2)

    def expulsar(self, destino):
        if destino is None:
            self.avisar("no hay ninguna tarjeta montada", ARENA)
            return
        if self.trabajo.activo:
            self.avisar("espera a que termine la copia", ARENA)
            return
        if self.expulsando:
            return
        self.avisar("sincronizando y expulsando…", CIELO, 8)
        etiqueta = destino.get("etiqueta") or destino["montaje"]
        self.expulsando = True

        # En su propio hilo y no como Trabajo: así no borra el resultado de la
        # copia (ni su panel en FiiO) ni ocupa la Cola con un panel de tarea.
        def tarea():
            salida = []
            try:
                ok = bool(fiio.desmontar(destino["montaje"], informar=salida.append))
            except Exception as error:
                ok = False
                salida.append(str(error))
            self.ultima_expulsion = {"montaje": destino["montaje"],
                                     "etiqueta": etiqueta, "ok": ok}
            copia = self.ultima_copia
            if copia is not None and copia.montaje == destino["montaje"]:
                copia.expulsada = ok
            if ok:
                self.avisar(f"{OK} {etiqueta} expulsada", ESPUMA, 6)
            else:
                motivo = salida[-1] if salida else "no se pudo"
                self.avisar(f"{MAL} {etiqueta} sin expulsar · {motivo}", CORAL, 8)
            self.expulsando = False

        threading.Thread(target=tarea, daemon=True).start()

    def arrancar_nicotine(self):
        if self.datos.nicotine_vivo():
            self.avisar("Nicotine+ ya está corriendo")
            return
        ok, detalle = comun.lanzar_nicotine()
        if ok:
            self.avisar("abriendo Nicotine+…", ESPUMA)
        else:
            self.avisar(f"no pude abrirlo: {detalle}", CORAL)

    def abrir_log(self):
        if comun.ES_WINDOWS:
            # No hay editor de terminal que dar por hecho: que lo abra Windows.
            try:
                os.startfile(comun.ARCHIVO_LOG)
            except OSError as error:
                self.avisar(f"no pude abrir el log: {error}", CORAL)
            return
        editor = os.environ.get("EDITOR") or "nano"
        try:
            curses.def_prog_mode()
            curses.endwin()
            subprocess.call([editor, comun.ARCHIVO_LOG])
        except OSError as error:
            self.avisar(f"no pude abrir {editor}: {error}", CORAL)
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
            ("importar una playlist en csv (Spotify)", "i",
             lambda: self.abrir(OverlayImportar())),
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
            ("pasar a la heredada (compartir en Nicotine+)", "h",
             lambda: (self.ir_a(1), self.vistas[1]._pedir_heredar(self))),
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
        self.pantalla.timeout(15)
        self.pantalla.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        ultimo = 0.0
        while not self.salir:
            k = self._tecla()
            pulsada = k is not None
            if pulsada:
                self.procesar(k)
                if self.salir:
                    return
            # 30 cuadros por segundo mientras algo se mueve; si no, 4 bastan
            # para el reloj y lo que llegue del sondeo. Una tecla repinta ya.
            ahora = time.monotonic()
            intervalo = 1 / 30 if self._animando else 1 / 4
            if pulsada or ahora - ultimo >= intervalo:
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
        LIENZO.preparar(alto, ancho)
        RITMO["animando"] = False
        if alto < 12 or ancho < 46:
            escribir(win, 0, 0, "La ventana es demasiado pequeña.", ARENA)
            escribir(win, 1, 0, f"Hacen falta 46x12; hay {ancho}x{alto}.", TENUE)
        else:
            self._eventos()
            self._cabecera(ancho)
            self._pestanas(ancho)
            escribir(win, alto - 3, 0, VACIO * ancho, LINEA)

            cuerpo_y = 3
            cuerpo_alto = alto - 6
            self.vista().dibujar(self, cuerpo_y, cuerpo_alto, ancho)

            self._estado(alto - 2, ancho)
            self._teclas(alto - 1, ancho)

            if self.overlay is not None:
                # lo de detrás se hunde mientras la ventana sube
                u = frenada((time.monotonic() - self._overlay_desde) / 0.2)
                if u < 1:
                    _anima()
                # en tres pasos: cada paso intermedio gasta pares de color
                LIENZO.atenuar(0.2 * math.ceil(3 * u))
                self.overlay.dibujar(self)
        LIENZO.volcar(win)
        win.refresh()
        self._animando = RITMO["animando"]

    def _activo(self):
        """Algo está pasando de verdad: una descarga, una copia, una tarea."""
        pet = self.datos.peticion_en_curso()
        return self.trabajo.activo or (pet is not None
                                       and pet["estado"] == comun.DESCARGANDO)

    def _cabecera(self, ancho):
        win = self.pantalla
        t = time.monotonic()
        # La marca es agua: quieta en reposo, y fluye mientras algo se mueve.
        fluye = self._activo()
        if fluye:
            _anima()
        x = 1
        for i, letra in enumerate("flacpipe"):
            if fluye:
                ola_ = 0.5 + 0.5 * math.sin(2 * math.pi * (i / 9 - t / 2.2))
                tono = rampa(OCEANO, 0.42 + 0.5 * ola_)
            else:
                tono = rampa(OCEANO, 0.48 + 0.42 * i / 7)
            x = escribir(win, 0, x, letra, (tono,), True)
        marca = x

        partes = []
        vivo = self.datos.nicotine_vivo()
        partes.append(("nicotine+", "●" if vivo else "○", ESPUMA if vivo else TENUE))
        destinos = self.datos.destinos
        partes.append((destinos[0]["etiqueta"] if destinos else "tarjeta",
                       "●" if destinos else "○", ESPUMA if destinos else TENUE))
        reloj = time.strftime("%H:%M")
        largo = sum(len(n) + 2 for n, _p, _c in partes) + 3 * len(partes) + len(reloj)

        # Mientras se copia de verdad a la tarjeta, se ve desde cualquier
        # otra pestaña: «FiiO ▮▮▮▮▯▯▯▯ 40 %», a la izquierda de los
        # indicadores. En la del FiiO ya lo dice la franja.
        progreso = []
        copia = self.trabajo.copia if self.trabajo.activo else None
        if (copia is not None and not copia.simulacion
                and not isinstance(self.vista(), VistaFiio)):
            frac = copia.frac()
            porcentaje = (f" {int(frac * 100)} %", AGUA)
            completo = ([("FiiO ", TENUE)]
                        + barra_trozos(8, frac, AGUA,
                                       vivo=copia.velocimetro.actual > 0, t=t)
                        + [porcentaje])
            # sin sitio, primero se va la barra; en una ventana mínima, todo
            for opcion in (completo, [("FiiO", TENUE), porcentaje]):
                if ancho - largo - 4 - sum(len(c) for c, _ in opcion) > marca:
                    progreso = opcion
                    break
        largo_progreso = sum(len(c) for c, _ in progreso) + (3 if progreso else 0)
        x_derecha = ancho - largo - largo_progreso - 1

        # La petición en curso se queda con lo que sobre; es lo primero que se va.
        pet = self.datos.peticion_en_curso()
        if pet is not None and x_derecha - marca >= 4:
            glifo, color = glifo_estado(pet["estado"], t)
            escribir(win, 0, marca + 2, glifo, color)
            sitio = x_derecha - (marca + 4) - 2
            if sitio >= 6:
                escribir(win, 0, marca + 4, elipsis(pet["consulta"], sitio), TENUE)

        x = x_derecha
        if progreso:
            x = escribir_trozos(win, 0, x, progreso) + 3
        for nombre, punto, color in partes:
            x = escribir(win, 0, x, nombre, TENUE)
            x = escribir(win, 0, x + 1, punto, color) + 3
        escribir(win, 0, x, reloj, TENUE)

    def _pestanas(self, ancho):
        win = self.pantalla
        x = 1
        posiciones = []
        for i, vista in enumerate(self.vistas):
            elegida = i == self.actual
            inicio = x
            x = escribir(win, 1, x, f"{i + 1}", AGUA if elegida else (25,), elegida)
            x = escribir(win, 1, x + 1, vista.nombre, BLANCO if elegida else TENUE)
            posiciones.append((inicio, x - inicio))
            x += 3
        cola = []
        resumen = self.vista().resumen(self)
        if resumen:
            cola.append(resumen)
        activas = len(self.datos.activas())
        if activas and not isinstance(self.vista(), VistaCola):   # la Cola ya lo dice
            cola.append(f"{activas} en cola")
        if self.marcados:
            cola.append(f"{len(self.marcados)} marcados")
        if self.datos.sobras or self.datos.sueltos:
            cola.append(f"{len(self.datos.sobras) + len(self.datos.sueltos)} sin organizar")
        texto = "  ·  ".join(cola)
        if texto:
            escribir(win, 1, max(x + 1, ancho - len(texto) - 1),
                     elipsis(texto, max(0, ancho - x - 2)), TENUE)
        self._subrayado(2, ancho, *posiciones[self.actual])

    def _subrayado(self, y, ancho, x_obj, w_obj):
        """La raya bajo la pestaña activa. Al cambiar de pestaña se desliza
        hasta la nueva y frena al llegar; a medio camino usa medias celdas
        (╺ ╸) para que el movimiento no vaya a saltos."""
        win = self.pantalla
        ahora = time.monotonic()
        objetivo = (float(x_obj - 1), float(w_obj + 2))
        if self._sub_objetivo is None:
            self._sub_objetivo = self._sub_desde = objetivo
            self._sub_t0 = ahora - 1
        elif objetivo != self._sub_objetivo:
            self._sub_desde = self._sub_actual
            self._sub_objetivo = objetivo
            self._sub_t0 = ahora
        u = (ahora - self._sub_t0) / 0.26
        if u < 1:
            _anima()
        k = frenada(u)
        x0 = self._sub_desde[0] + (objetivo[0] - self._sub_desde[0]) * k
        w = self._sub_desde[1] + (objetivo[1] - self._sub_desde[1]) * k
        self._sub_actual = (x0, w)

        escribir(win, y, 0, VACIO * ancho, LINEA)
        izq, der = x0, x0 + w
        for cx in range(max(0, int(izq)), min(ancho, int(math.ceil(der)))):
            cubre_izq = max(izq, cx) - cx          # de 0 a 1 dentro de la celda
            cubre_der = min(der, cx + 1) - cx
            if cubre_der - cubre_izq < 0.25:
                continue
            if cubre_izq >= 0.5:
                glifo = "╺"
            elif cubre_der <= 0.5:
                glifo = "╸"
            else:
                glifo = "━"
            tono = rampa(OCEANO, 0.55 + 0.4 * (cx - izq) / max(1.0, w))
            escribir(win, y, cx, glifo, (tono,))

    def _eventos(self):
        """Avisos automáticos cuando una petición cambia de estado."""
        while self.datos.eventos:
            p = self.datos.eventos.popleft()
            estado = p["estado"]
            if estado == comun.LISTO:
                faltan = (p.get("resultado") or {}).get("faltantes") or []
                falta = (f" · faltan {len(faltan)} pista{'s' if len(faltan) > 1 else ''}"
                         if faltan else "")
                self.avisar(f"{OK} {titulo_peticion(p)} terminado{falta} · ya está en la "
                            f"Biblioteca", ARENA if faltan else ESPUMA, 6)
            elif estado == comun.FALLIDO:
                self.avisar(f"{MAL} {p['consulta']} · {p.get('mensaje') or 'falló'}", CORAL, 8)
            elif estado == comun.DESCARGANDO:
                c = p.get("candidato") or {}
                self.avisar(f"↓ {p['consulta']} · de {c.get('usuario', '?')} · "
                            f"{c.get('detalle', '')}", AGUA, 5)
            elif estado == comun.PENDIENTE and (p.get("mensaje") or "").startswith("reintentando"):
                self.avisar(f"{AVISO} {p['consulta']} · {p['mensaje']}", ARENA, 6)

    def _estado(self, y, ancho):
        self._estado_base(y, ancho)
        self._aviso_animado(y, ancho)

    def _aviso_animado(self, y, ancho):
        """El aviso es una tarjeta con el filo de su color: entra deslizándose
        por la derecha, frena al llegar, se queda y se va por donde vino."""
        t = time.monotonic()
        if not self._aviso or t >= self._aviso_hasta:
            return
        _anima()
        texto = elipsis(f" {self._aviso}  ", max(4, ancho - 4))
        largo = len(texto) + 1
        entrada = salida = 0.35
        desde_inicio = t - self._aviso_desde
        hasta_fin = self._aviso_hasta - t
        if desde_inicio < entrada:
            fuera = (1 - frenada(desde_inicio / entrada)) * largo
        elif hasta_fin < salida:
            fuera = (1 - frenada(hasta_fin / salida)) * largo
        else:
            fuera = 0
        x = ancho - 1 - largo + int(fuera + 0.5)
        color = self._aviso_color if self._aviso_color != NORMAL else AGUA
        frente = _PALETA["tema"].get(color, (-1, None, 0))[0]
        if _PALETA["colores"] >= 256:
            pintar(self.pantalla, y, x, largo, VENTANA)
            escribir(self.pantalla, y, x, "▌", color)
            escribir(self.pantalla, y, x + 1, texto, (frente, FONDO_VENTANA))
        else:
            escribir(self.pantalla, y, x, "▌", color)
            escribir(self.pantalla, y, x + 1, texto, color, True)

    def _estado_base(self, y, ancho):
        win = self.pantalla
        t = time.monotonic()
        if self.reproductor.sonando:
            transcurrido = self.reproductor.transcurrido()
            total = self.reproductor.duracion
            escribir(win, y, 1, "▶", AGUA)
            escribir(win, y, 3, elipsis(self.reproductor.titulo, max(4, ancho - 34)),
                     BLANCO)
            x = max(5, ancho - 30)
            if total:
                barra(win, y, x, 16, min(1.0, transcurrido / total), AGUA, vivo=True, t=t)
                escribir(win, y, x + 17,
                         f"{duracion(transcurrido)}/{duracion(total)}", TENUE)
            else:
                escribir(win, y, x, duracion(transcurrido), TENUE)
            return
        pet = self.datos.peticion_en_curso()
        bajando = pet is not None and pet["estado"] == comun.DESCARGANDO
        if self.trabajo.activo:
            copia = self.trabajo.copia
            if copia is not None and isinstance(self.vista(), VistaFiio):
                album = copia.actual()
                texto = self.trabajo.titulo
                if album is not None:
                    nombre = (f"{album['artista']} — {album['album']}"
                              if album["artista"] else album["rel"])
                    texto = f"{'simulando' if copia.simulacion else 'copiando'} {nombre}"
                escribir(win, y, 1, girador(t), CIELO)
                escribir(win, y, 3, elipsis(texto, ancho - 4), AGUA)
                return
            # Fuera del FiiO, si algo se está bajando, manda la Cola: el
            # progreso de la copia ya va en la cabecera.
            if copia is None or not bajando:
                escribir(win, y, 1, girador(t), CIELO)
                escribir(win, y, 3, elipsis(self.trabajo.titulo, ancho - 4), TENUE)
                return
        if self.datos.aviso:
            escribir(win, y, 1, elipsis(self.datos.aviso, ancho - 2), CORAL)
            return
        if bajando:
            velocidad = self.datos.velocimetro
            texto = f"{comun.formato_bytes(velocidad.actual)}/s"
            escribir(win, y, 1, texto, BLANCO)
            escribir_trozos(win, y, 3 + len(texto),
                            chispa(velocidad.historial, min(30, ancho - 20)))
            return
        escribir(win, y, 1, "todo en calma", TENUE)

    def _teclas(self, y, ancho):
        """La fila de teclas. `?` y `q` van reservadas a la derecha: son las dos
        que nunca pueden faltar, así que se dibujan antes que las de la vista."""
        win = self.pantalla

        def par(x, tecla, texto):
            x = escribir(win, y, x, tecla, AGUA, True)
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
