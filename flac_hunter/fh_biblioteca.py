"""Lectura de metadatos FLAC y organización a Artista/Álbum/NN Título.flac.

Sin dependencias externas: los tags Vorbis y el STREAMINFO se leen a mano del
contenedor FLAC. Lo usa tanto el plugin (dentro del snap, donde no hay pip)
como la CLI.
"""

import os
import re
import shutil
import struct

import fh_comun as comun

EXT_AUDIO = ".flac"
NOMBRES_CARATULA = ("cover", "folder", "front", "albumart", "caratula")
EXT_IMAGEN = (".jpg", ".jpeg", ".png")

# FAT32/exFAT prohíbe \ / : * ? " < > |. Se sustituyen por homoglifos Unicode
# para no perder legibilidad en la pantalla del FiiO.
SUSTITUCIONES = {
    "/": "⁄",   # ⁄
    "\\": "＼",  # ＼
    ":": "꞉",   # ꞉
    "*": "✱",   # ✱
    "?": "？",   # ？
    '"': "＂",   # ＂
    "<": "＜",   # ＜
    ">": "＞",   # ＞
    "|": "｜",   # ｜
}
RESERVADOS_FAT = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


# ─── Lectura del contenedor FLAC ──────────────────────────────────────────────

def leer_tags(ruta):
    """Devuelve el bloque VORBIS_COMMENT como dict con claves en mayúsculas."""
    tags = {}
    try:
        with open(ruta, "rb") as f:
            if f.read(4) != b"fLaC":
                return {"ERROR": "no es un FLAC válido"}
            while True:
                cabecera = f.read(4)
                if len(cabecera) < 4:
                    break
                tipo = cabecera[0] & 0x7F
                ultimo = (cabecera[0] & 0x80) != 0
                largo = struct.unpack(">I", b"\x00" + cabecera[1:4])[0]
                datos = f.read(largo)
                if tipo == 4:  # VORBIS_COMMENT
                    pos = 0
                    largo_vendor = struct.unpack("<I", datos[pos:pos + 4])[0]
                    pos += 4 + largo_vendor
                    cuantos = struct.unpack("<I", datos[pos:pos + 4])[0]
                    pos += 4
                    for _ in range(cuantos):
                        largo_c = struct.unpack("<I", datos[pos:pos + 4])[0]
                        pos += 4
                        comentario = datos[pos:pos + largo_c].decode("utf-8", "replace")
                        pos += largo_c
                        if "=" in comentario:
                            clave, valor = comentario.split("=", 1)
                            clave = clave.upper()
                            # Un tag repetido (p. ej. varios ARTIST) se queda con el primero
                            tags.setdefault(clave, valor)
                    break
                if ultimo:
                    break
    except Exception as error:
        tags["ERROR"] = str(error)
    return tags


def leer_streaminfo(ruta):
    """Devuelve {sample_rate, channels, bit_depth, duracion} del bloque STREAMINFO."""
    info = {}
    try:
        with open(ruta, "rb") as f:
            if f.read(4) != b"fLaC":
                return info
            cabecera = f.read(4)
            if len(cabecera) < 4:
                return info
            tipo = cabecera[0] & 0x7F
            largo = struct.unpack(">I", b"\x00" + cabecera[1:4])[0]
            datos = f.read(largo)
            if tipo == 0 and len(datos) >= 18:
                val = struct.unpack(">I", datos[10:14])[0]
                info["sample_rate"] = (val >> 12) & 0xFFFFF
                info["channels"] = ((val >> 9) & 0x7) + 1
                info["bit_depth"] = ((val >> 4) & 0x1F) + 1
                muestras = ((val & 0xF) << 32) | struct.unpack(">I", datos[14:18])[0]
                if info["sample_rate"]:
                    info["duracion"] = muestras / info["sample_rate"]
    except Exception:
        pass
    return info


# ─── Nombres seguros ──────────────────────────────────────────────────────────

def sanear(nombre, maximo=110):
    """Nombre válido en FAT32/exFAT y legible en el FiiO."""
    nombre = "".join(SUSTITUCIONES.get(c, c) for c in (nombre or ""))
    nombre = "".join(c for c in nombre if ord(c) >= 32)
    nombre = " ".join(nombre.split())
    nombre = nombre.rstrip(". ")          # FAT no admite punto ni espacio final
    if len(nombre) > maximo:
        nombre = nombre[:maximo].rstrip(". ")
    if nombre.split(".")[0].upper() in RESERVADOS_FAT:
        nombre = "_" + nombre
    return nombre or "Sin nombre"


def numero(bruto):
    """'4/9', '04', '4', 'A2' -> 4. None si no hay número."""
    if bruto is None:
        return None
    m = re.search(r"\d+", str(bruto).split("/")[0])
    return int(m.group()) if m else None


_PREFIJO_PISTA = re.compile(r"^\s*(?:\d{1,3})\s*[-._)\]]?\s+")


def _titulo_desde_nombre(nombre_archivo):
    base = os.path.splitext(nombre_archivo)[0]
    return _PREFIJO_PISTA.sub("", base).strip() or base


def _mayoria(valores):
    valores = [v for v in valores if v]
    if not valores:
        return None
    return max(set(valores), key=valores.count)


# ─── Análisis de una carpeta descargada ───────────────────────────────────────

def analizar(origen):
    """Lee todos los FLAC bajo `origen` (recursivo). Devuelve (pistas, avisos).

    Cada pista: {ruta, archivo, disco, pista, titulo, artista, albumartist,
                 album, bit_depth, sample_rate, tamaño}
    """
    avisos = []
    pistas = []

    rutas = []
    for base, _dirs, archivos in os.walk(origen):
        for archivo in sorted(archivos):
            if archivo.startswith("._") or not archivo.lower().endswith(EXT_AUDIO):
                continue
            rutas.append(os.path.join(base, archivo))
    rutas.sort()

    for ruta in rutas:
        archivo = os.path.basename(ruta)
        tags = leer_tags(ruta)
        if "ERROR" in tags:
            avisos.append(f"{archivo}: {tags['ERROR']} — se omite")
            continue
        stream = leer_streaminfo(ruta)

        titulo = (tags.get("TITLE") or "").strip() or _titulo_desde_nombre(archivo)
        num = numero(tags.get("TRACKNUMBER") or tags.get("TRACK"))
        if num is None:
            num = numero(re.match(r"\s*(\d{1,3})", archivo).group(1)) if re.match(r"\s*(\d{1,3})", archivo) else None
            if num is not None:
                avisos.append(f"{archivo}: sin TRACKNUMBER, se usó el número del nombre ({num})")

        bruto_pista = tags.get("TRACKNUMBER") or tags.get("TRACK") or ""
        total_declarado = (numero(tags.get("TRACKTOTAL"))
                           or numero(tags.get("TOTALTRACKS"))
                           or (numero(str(bruto_pista).split("/")[1])
                               if "/" in str(bruto_pista) else None))

        pistas.append({
            "ruta": ruta,
            "archivo": archivo,
            "total_declarado": total_declarado,
            "disco": numero(tags.get("DISCNUMBER")) or 1,
            "pista": num,
            "titulo": titulo,
            "artista": (tags.get("ARTIST") or "").strip(),
            "albumartist": (tags.get("ALBUMARTIST") or tags.get("ALBUM ARTIST") or "").strip(),
            "album": (tags.get("ALBUM") or "").strip(),
            "bit_depth": stream.get("bit_depth", 0),
            "sample_rate": stream.get("sample_rate", 0),
            "tamaño": os.path.getsize(ruta),
        })

    # Pistas sin número: van al final en orden de nombre de archivo
    sin_numero = [p for p in pistas if p["pista"] is None]
    if sin_numero:
        avisos.append(f"{len(sin_numero)} pista(s) sin número; se numeran al final por nombre de archivo")
        siguiente = max((p["pista"] for p in pistas if p["pista"]), default=0) + 1
        for p in sorted(sin_numero, key=lambda p: p["archivo"]):
            p["pista"] = siguiente
            siguiente += 1

    pistas.sort(key=lambda p: (p["disco"], p["pista"], p["archivo"]))
    return pistas, avisos


def huecos(pistas):
    """{disco: [números que faltan]}.

    Un álbum al que le falta la pista 04 se ve idéntico a uno completo si sólo
    cuentas archivos, por eso se comprueba la secuencia y no el total.

    Si los tags declaran TRACKTOTAL, se compara contra 1..total, lo que además
    detecta que falte la ÚLTIMA pista. Sin ese dato sólo se pueden ver los
    huecos interiores, entre la primera y la última que sí están: así un archivo
    suelto (sólo la 04) no se reporta como si le faltaran la 01, 02 y 03.
    """
    por_disco = {}
    for p in pistas:
        d = por_disco.setdefault(p["disco"], {"numeros": set(), "totales": []})
        d["numeros"].add(p["pista"])
        if p.get("total_declarado"):
            d["totales"].append(p["total_declarado"])

    resultado = {}
    for disco, datos in por_disco.items():
        numeros = {n for n in datos["numeros"] if n}
        if not numeros:
            continue
        totales = datos["totales"]
        if totales:
            esperados = set(range(1, max(set(totales), key=totales.count) + 1))
        else:
            esperados = set(range(min(numeros), max(numeros) + 1))
        faltan = sorted(esperados - numeros)
        if faltan:
            resultado[disco] = faltan
    return resultado


def calidad(pistas):
    """'24-bit / 96 kHz' a partir de los STREAMINFO leídos."""
    bits = {p["bit_depth"] for p in pistas if p.get("bit_depth")}
    tasas = {p["sample_rate"] for p in pistas if p.get("sample_rate")}
    partes = []
    if bits:
        partes.append(f"{max(bits)}-bit")
    if tasas:
        sr = max(tasas)
        partes.append(f"{sr / 1000:.6g} kHz")
    return " / ".join(partes) if partes else "calidad desconocida"


def deducir_artista_album(pistas, artista_sug=None, album_sug=None, nombre_origen=""):
    """Decide bajo qué Artista/Álbum va la carpeta."""
    # ALBUMARTIST manda: es lo correcto para recopilatorios y discos con invitados.
    artista = _mayoria([p["albumartist"] for p in pistas])
    if not artista:
        artistas = [p["artista"] for p in pistas if p["artista"]]
        dominante = _mayoria(artistas)
        # Si más del 60% comparte ARTIST, es un disco de ese artista.
        if dominante and artistas.count(dominante) / max(len(pistas), 1) >= 0.6:
            artista = dominante
        elif artistas:
            artista = artista_sug or "Varios Artistas"
    if not artista:
        artista = artista_sug or "Desconocido"

    album = _mayoria([p["album"] for p in pistas]) or album_sug
    if not album:
        # Último recurso: el nombre de la carpeta de origen, sin adornos de release
        album = re.sub(r"[\[(][^\])]*[\])]", " ", nombre_origen)
        album = re.sub(r"\b(flac|24 ?bit|16 ?bit|web|vinyl|cd|lossless|\d{3,6}khz)\b", " ",
                       album, flags=re.IGNORECASE)
        album = " ".join(album.split()).strip(" -_") or "Sin álbum"
    return artista, album


def nombres_finales(pistas):
    """Asigna a cada pista su nombre `NN Título.flac`, renumerando si hay varios discos.

    Con más de un disco la numeración es continua (disco 1: 01-12, disco 2: 13-24)
    para que el FiiO reproduzca el álbum completo en orden dentro de una sola carpeta.
    """
    discos = sorted({p["disco"] for p in pistas})
    ancho = 3 if len(pistas) > 99 else 2

    if len(discos) > 1:
        for indice, p in enumerate(pistas, start=1):
            p["numero_final"] = indice
    else:
        for p in pistas:
            p["numero_final"] = p["pista"]
        # Si los números originales chocan o se salen de rango, renumeramos
        finales = [p["numero_final"] for p in pistas]
        if len(set(finales)) != len(finales) or min(finales) < 1:
            for indice, p in enumerate(pistas, start=1):
                p["numero_final"] = indice

    for p in pistas:
        p["nombre_final"] = f"{p['numero_final']:0{ancho}d} {sanear(p['titulo'])}{EXT_AUDIO}"
    return pistas


def _caratula(origen):
    """Ruta de la mejor imagen de portada dentro de `origen`, o None."""
    mejor = None
    for base, _dirs, archivos in os.walk(origen):
        for archivo in archivos:
            nombre, ext = os.path.splitext(archivo.lower())
            if ext not in EXT_IMAGEN or archivo.startswith("._"):
                continue
            ruta = os.path.join(base, archivo)
            prioridad = 0 if any(nombre.startswith(n) for n in NOMBRES_CARATULA) else 1
            tamaño = os.path.getsize(ruta)
            clave = (prioridad, -tamaño)
            if mejor is None or clave < mejor[0]:
                mejor = (clave, ruta)
    return mejor[1] if mejor else None


def _mover(origen, destino):
    """Mueve preservando datos; cae a copiar+borrar si cruza sistemas de archivos."""
    shutil.move(origen, destino)


def organizar(origen, artista_sug=None, album_sug=None, raiz=None,
              simular=False, borrar_origen=True):
    """Mueve los FLAC de `origen` a raiz/Artista/Álbum/NN Título.flac.

    Devuelve un dict con lo que hizo (o haría, si simular=True).
    """
    raiz = raiz or comun.RAIZ_BIBLIOTECA
    pistas, avisos = analizar(origen)
    if not pistas:
        return {"ok": False, "motivo": "no se encontró ningún FLAC legible",
                "avisos": avisos, "pistas": 0}

    artista, album = deducir_artista_album(
        pistas, artista_sug, album_sug, os.path.basename(origen.rstrip("/")))
    nombres_finales(pistas)

    carpeta_rel = os.path.join(sanear(artista, 80), sanear(album, 90))
    carpeta = os.path.join(raiz, carpeta_rel)

    movidos, omitidos = [], []
    if not simular:
        os.makedirs(carpeta, exist_ok=True)

    for p in pistas:
        destino = os.path.join(carpeta, p["nombre_final"])
        p["destino"] = destino
        if os.path.exists(destino):
            # Nos quedamos con el de mayor profundidad de bits; a igualdad, el más grande.
            existente = leer_streaminfo(destino)
            mejor_nuevo = (p["bit_depth"], p["tamaño"]) > (
                existente.get("bit_depth", 0), os.path.getsize(destino))
            if not mejor_nuevo:
                omitidos.append(p["nombre_final"])
                if not simular and borrar_origen:
                    os.remove(p["ruta"])
                continue
            avisos.append(f"{p['nombre_final']}: se reemplaza por una versión mejor")
        if not simular:
            _mover(p["ruta"], destino)
        movidos.append(p["nombre_final"])

    portada = _caratula(origen)
    if portada and not simular:
        destino_portada = os.path.join(carpeta, "cover" + os.path.splitext(portada)[1].lower())
        if not os.path.exists(destino_portada):
            shutil.copy2(portada, destino_portada)

    if not simular and borrar_origen:
        shutil.rmtree(origen, ignore_errors=True)

    # Lo que importa no es si al lote movido le faltan pistas, sino si al álbum
    # ya completo le falta alguna: mover una pista suelta a un álbum que ya
    # tiene las otras doce no debe avisar de nada.
    if simular:
        faltantes = sorted(n for lista in huecos(pistas).values() for n in lista)
    else:
        pistas_destino, _ = analizar(carpeta)
        faltantes = sorted(n for lista in huecos(pistas_destino).values() for n in lista)

    if faltantes:
        avisos.append("el álbum queda INCOMPLETO, faltan las pistas "
                      + ", ".join(f"{n:02d}" for n in faltantes))

    return {
        "ok": True,
        "faltantes": faltantes,
        "artista": artista,
        "album": album,
        "carpeta": carpeta_rel,
        "pistas": len(movidos),
        "omitidas": len(omitidos),
        "calidad": calidad(pistas),
        "nombres": movidos,
        "avisos": avisos,
    }
