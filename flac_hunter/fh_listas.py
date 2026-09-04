#!/usr/bin/env python3
"""Playlists exportadas en CSV, convertidas en peticiones para la cola.

Spotify no exporta nada por sí mismo, pero sitios como Exportify, TuneMyMusic
o Soundiiz sacan una playlist como CSV. Cada uno pone columnas distintas, así
que aquí se adivinan por la cabecera. De cada fila sólo hace falta el artista,
el título y, si viene, el álbum: con eso el plugin busca «Artista - Canción
flac» en Soulseek y archiva lo que baja por sus tags, en su álbum de verdad.

Lo usan la CLI (`flacpipe pedir --csv`) y la interfaz (tecla `i` en la Cola).
El plugin no lo importa.
"""

import csv
import os
import re
import unicodedata

import fh_biblioteca as biblioteca
import fh_comun as comun
import fh_fiio as fiio

# Cabeceras que se reconocen, ya normalizadas: minúsculas y sin acentos.
# Se busca coincidencia exacta, para que «name» no se confunda con «album name».
CABECERAS = {
    "titulo": ("track name", "track title", "song name", "song title", "title",
               "track", "song", "titulo", "cancion", "tema", "nombre de la pista"),
    "artista": ("artist name(s)", "artist names", "artist name", "artists", "artist",
                "artista", "artistas", "interprete", "nombre del artista"),
    "album": ("album name", "album title", "album", "release name", "disco"),
    "lista": ("playlist name", "playlist", "lista", "lista de reproduccion"),
}

# Dónde buscar CSV cuando se importa desde la interfaz (sin recorrer subcarpetas)
DIRS_CSV = [os.path.expanduser(d) for d in
            ("~/Descargas", "~/Downloads", "~/Música", "~/Music",
             "~/Documentos", "~/Documents", "~/Escritorio", "~/Desktop", "~")]

# Un cálculo grueso para decir cuánto tardará: 45 s de búsqueda más la descarga
SEGUNDOS_POR_CANCION = 120
SEGUNDOS_POR_ALBUM = 600

_FEAT = re.compile(r"\s*[(\[]\s*(?:feat|ft|featuring|with|con)\.?\s[^)\]]*[)\]]", re.I)
_FEAT_GUION = re.compile(r"\s+[-–]\s+(?:feat|ft|featuring)\.?\s.*$", re.I)
_REMASTER = re.compile(
    r"\s*(?:[-–]\s*|[(\[]\s*)(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?(?:\s+version)?\s*[)\]]?\s*$",
    re.I)


# ─── Nombres ──────────────────────────────────────────────────────────────────

def _normalizar_cabecera(texto):
    texto = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return " ".join(texto.replace("﻿", "").strip().strip('"').lower().split())


def clave(texto):
    """Forma comparable de un nombre: sin acentos, minúsculas, sólo letras y números."""
    texto = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", texto.lower())


def artista_principal(texto):
    """'Malcolm Todd, Omar Apollo' → 'Malcolm Todd'.

    Soulseek exige que todas las palabras de la búsqueda estén en la ruta, así
    que un segundo artista sólo estorba. La coma es ambigua: «Tyler, The
    Creator» y «Earth, Wind & Fire» la llevan en el nombre. Se corta por la
    coma sólo cuando lo de antes tiene más de una palabra, que es como se ven
    dos artistas de verdad.
    """
    texto = " ".join((texto or "").split())
    for sep in (";", " / ", " | "):
        if sep in texto:
            texto = texto.split(sep)[0].strip()
    if "," in texto:
        antes = texto.partition(",")[0].strip()
        if len(antes.split()) >= 2:
            texto = antes
    return texto


def limpiar_titulo(texto):
    """Quita lo que sobra para buscar: «(feat. X)», «- 2009 Remaster»…"""
    original = " ".join((texto or "").split())
    limpio = _FEAT.sub("", original)
    limpio = _FEAT_GUION.sub("", limpio)
    limpio = _REMASTER.sub("", limpio)
    limpio = limpio.strip(" -–")
    return limpio or original


# ─── Leer el CSV ──────────────────────────────────────────────────────────────

def _filas(ruta):
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        texto = f.read()
    lineas = texto.splitlines()
    if not lineas:
        return []
    # El separador: el que más veces aparezca en la cabecera
    separador = max(",;\t", key=lineas[0].count)
    return [fila for fila in csv.reader(lineas, delimiter=separador)
            if any(celda.strip() for celda in fila)]


def leer(ruta):
    """Devuelve (nombre de la lista, canciones, avisos).

    Cada canción: {artista, titulo, album, consulta, fila}. Las repetidas
    dentro del CSV se quedan una sola vez.
    """
    filas = _filas(ruta)
    if not filas:
        raise ValueError("el archivo está vacío")
    cabecera = [_normalizar_cabecera(c) for c in filas[0]]
    columnas = {}
    for campo, nombres in CABECERAS.items():
        for nombre in nombres:
            if nombre in cabecera:
                columnas[campo] = cabecera.index(nombre)
                break
    if "titulo" not in columnas or "artista" not in columnas:
        raise ValueError("no encuentro las columnas de artista y título; "
                         "las que hay son: " + ", ".join(c.strip() for c in filas[0]))

    def celda(fila, campo):
        i = columnas.get(campo)
        return fila[i].strip() if i is not None and i < len(fila) else ""

    canciones, avisos, vistas = [], [], set()
    nombre_lista = ""
    for numero, fila in enumerate(filas[1:], 2):
        artista = artista_principal(celda(fila, "artista"))
        titulo = limpiar_titulo(celda(fila, "titulo"))
        album = " ".join(celda(fila, "album").split())
        if not nombre_lista and celda(fila, "lista"):
            nombre_lista = celda(fila, "lista")
        if not artista or not titulo:
            avisos.append(f"fila {numero}: sin artista o sin título, se salta")
            continue
        consulta = f"{artista} - {titulo}"
        if consulta.casefold() in vistas:
            continue
        vistas.add(consulta.casefold())
        canciones.append({"artista": artista, "titulo": titulo, "album": album,
                          "consulta": consulta, "fila": numero})
    if not nombre_lista:
        nombre_lista = os.path.splitext(os.path.basename(ruta))[0]
    return nombre_lista, canciones, avisos


# ─── Qué hay ya ───────────────────────────────────────────────────────────────

def indice_biblioteca():
    """Lo que ya está en disco, por nombre de carpeta y de archivo, sin leer tags.

    Devuelve dos conjuntos de claves: (artista, título) por cada pista y
    (artista, álbum) por cada carpeta. Mira la biblioteca nueva y la heredada.
    """
    pistas, albumes = set(), set()
    raices = (comun.RAIZ_BIBLIOTECA, os.path.join(comun.RAIZ_BIBLIOTECA, "Artistas"))
    for raiz in raices:
        for item in fiio.listar_biblioteca(raiz=raiz):
            ka = clave(item["artista"])
            albumes.add((ka, clave(item["album"])))
            for archivo in item["pistas"]:
                titulo = limpiar_titulo(biblioteca._titulo_desde_nombre(archivo))
                pistas.add((ka, clave(titulo)))
    return pistas, albumes


def plan(ruta, albumes=False, repetir=False):
    """Qué se pediría de ese CSV y qué no, y por qué.

    Con `albumes`, en vez de cada canción se piden los discos completos a los
    que pertenecen, una vez cada uno. Con `repetir` se piden también las que
    ya están en la biblioteca o ya se pidieron.
    """
    nombre, canciones, avisos = leer(ruta)
    tenidas_pistas, tenidos_albumes = indice_biblioteca()
    en_cola = {p["consulta"].casefold(): p["estado"] for p in comun.leer_cola()["peticiones"]
               if p["estado"] in comun.ACTIVOS or p["estado"] == comun.LISTO}

    pedir, tenidas, encoladas, vistos = [], [], [], set()
    for c in canciones:
        if albumes:
            if not c["album"]:
                avisos.append(f"{c['consulta']}: el CSV no dice el álbum, se salta")
                continue
            entrada = {"consulta": f"{c['artista']} - {c['album']}",
                       "artista": c["artista"], "album": c["album"]}
            tenida = (clave(c["artista"]), clave(c["album"])) in tenidos_albumes
        else:
            entrada = dict(c)
            tenida = (clave(c["artista"]), clave(c["titulo"])) in tenidas_pistas
        k = entrada["consulta"].casefold()
        if k in vistos:
            continue
        vistos.add(k)
        if tenida and not repetir:
            tenidas.append(entrada)
        elif k in en_cola and not repetir:
            entrada["estado"] = en_cola[k]
            encoladas.append(entrada)
        else:
            pedir.append(entrada)

    return {"nombre": nombre, "ruta": ruta, "tipo": "album" if albumes else "cancion",
            "canciones": canciones, "pedir": pedir, "tenidas": tenidas,
            "en_cola": encoladas, "avisos": avisos}


def encolar(plan_):
    """Mete en la cola lo que el plan dice que hay que pedir. Devuelve las creadas."""
    return comun.añadir_peticiones([e["consulta"] for e in plan_["pedir"]],
                                   tipo=plan_["tipo"], lista=plan_["nombre"])


def estimacion(n, albumes=False):
    """'≈ 1 h 20 min' para n canciones (o álbumes), a ojo."""
    segundos = n * (SEGUNDOS_POR_ALBUM if albumes else SEGUNDOS_POR_CANCION)
    if segundos < 3600:
        return f"≈ {max(1, segundos // 60)} min"
    horas, resto = divmod(segundos, 3600)
    return f"≈ {horas} h {resto // 60:02d} min"


def ritmo(albumes=False):
    """'a unos 2 min por canción' / 'a unos 10 min por álbum'."""
    if albumes:
        return f"a unos {SEGUNDOS_POR_ALBUM // 60} min por álbum"
    return f"a unos {SEGUNDOS_POR_CANCION // 60} min por canción"


def csv_candidatos(limite=15):
    """Los .csv a la vista en las carpetas de siempre, del más reciente al más viejo."""
    vistos, salida = set(), []
    for carpeta in DIRS_CSV:
        try:
            entradas = list(os.scandir(carpeta))
        except OSError:
            continue
        for e in entradas:
            if not e.name.lower().endswith(".csv") or e.name.startswith("."):
                continue
            try:
                real = os.path.realpath(e.path)
                if real in vistos or not e.is_file():
                    continue
                vistos.add(real)
                salida.append((e.path, e.stat().st_mtime))
            except OSError:
                continue
    salida.sort(key=lambda par: -par[1])
    return salida[:limite]
