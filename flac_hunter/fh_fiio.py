"""Parte 2 del pipeline: copia estrictamente secuencial y ordenada al FiiO.

El reproductor FiiO Snowsky no ordena por metadatos: muestra los archivos en el
orden en que aparecen en la tabla de directorios de la tarjeta (FAT32/exFAT).
Ese orden es el orden de creación de las entradas. Por eso aquí:

  - se copia un archivo a la vez, nunca en paralelo;
  - se recorre artista → álbum → pista 01, 02, 03…;
  - se hace fsync de cada archivo y sync tras cada álbum, para que la entrada
    quede escrita antes de crear la siguiente;
  - al terminar cada álbum se relee el directorio y se comprueba que el orden
    físico coincide con el orden de pistas.
"""

import os
import re
import shutil
import subprocess
import unicodedata

import fh_biblioteca as biblioteca
import fh_comun as comun

TAMAÑO_BLOQUE = 4 * 1024 * 1024
SISTEMAS_EXTRAIBLES = {"vfat", "exfat", "fuseblk", "ntfs", "ntfs3", "msdos"}
BASES_MONTAJE = ("/run/media/", "/media/", "/mnt/")

# Subcarpeta dentro de la tarjeta donde vive la música.
DESTINO_REL_POR_DEFECTO = "Artistas"


def _clave_orden(texto):
    """Orden alfabético ignorando acentos y mayúsculas."""
    plano = unicodedata.normalize("NFKD", texto)
    plano = "".join(c for c in plano if not unicodedata.combining(c))
    return (plano.casefold(), texto)


# ─── Detección de la tarjeta ──────────────────────────────────────────────────

def _desescapar(ruta):
    """/proc/mounts escapa espacio, tab, salto de línea y \\ en octal."""
    return re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1), 8)), ruta)


def detectar_destinos():
    """Puntos de montaje que parecen una tarjeta/DAP conectado."""
    encontrados = []
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            lineas = f.readlines()
    except OSError:
        return encontrados

    for linea in lineas:
        partes = linea.split()
        if len(partes) < 3:
            continue
        dispositivo, montaje, tipo = partes[0], _desescapar(partes[1]), partes[2]
        if tipo not in SISTEMAS_EXTRAIBLES:
            continue
        if not montaje.startswith(BASES_MONTAJE):
            continue
        try:
            st = os.statvfs(montaje)
            libre = st.f_bavail * st.f_frsize
            total = st.f_blocks * st.f_frsize
        except OSError:
            libre = total = 0
        encontrados.append({
            "montaje": montaje,
            "dispositivo": dispositivo,
            "tipo": tipo,
            "libre": libre,
            "total": total,
            "etiqueta": os.path.basename(montaje),
        })
    return encontrados


# ─── Inventario de la biblioteca ──────────────────────────────────────────────

def listar_biblioteca(raiz=None, filtros=None):
    """Devuelve [(artista, album, [rutas de pista ordenadas], portada)] ya ordenado.

    `filtros` acepta 'Artista' o 'Artista/Álbum'; sin filtros, toda la
    biblioteca nueva (se excluye la carpeta heredada 'Artistas').
    """
    raiz = raiz or comun.RAIZ_BIBLIOTECA
    filtros = [f.strip("/") for f in (filtros or []) if f.strip("/")]
    salida = []

    try:
        artistas = [d for d in os.listdir(raiz)
                    if os.path.isdir(os.path.join(raiz, d))
                    and d not in comun.NO_SON_ARTISTAS
                    and not d.startswith(".")]
    except OSError:
        return salida

    for artista in sorted(artistas, key=_clave_orden):
        dir_artista = os.path.join(raiz, artista)
        try:
            albumes = [d for d in os.listdir(dir_artista)
                       if os.path.isdir(os.path.join(dir_artista, d))]
        except OSError:
            continue

        for album in sorted(albumes, key=_clave_orden):
            rel = f"{artista}/{album}"
            if filtros and not any(
                    rel == f or artista == f or rel.startswith(f + "/") for f in filtros):
                continue

            dir_album = os.path.join(dir_artista, album)
            try:
                archivos = os.listdir(dir_album)
            except OSError:
                continue

            pistas = sorted(
                (a for a in archivos
                 if a.lower().endswith(biblioteca.EXT_AUDIO) and not a.startswith("._")),
            )
            if not pistas:
                continue

            portada = next(
                (a for a in sorted(archivos)
                 if os.path.splitext(a.lower())[0].startswith(biblioteca.NOMBRES_CARATULA)
                 and os.path.splitext(a.lower())[1] in biblioteca.EXT_IMAGEN),
                None)

            salida.append({
                "artista": artista,
                "album": album,
                "rel": rel,
                "dir": dir_album,
                "pistas": pistas,
                "portada": portada,
                "bytes": sum(os.path.getsize(os.path.join(dir_album, p)) for p in pistas),
            })

    return salida


# ─── Copia ────────────────────────────────────────────────────────────────────

def _copiar_archivo(origen, destino):
    """Copia un archivo y lo sincroniza a disco antes de devolver el control."""
    with open(origen, "rb") as f_in, open(destino, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out, TAMAÑO_BLOQUE)
        f_out.flush()
        os.fsync(f_out.fileno())
    try:
        shutil.copystat(origen, destino)
    except OSError:
        pass  # FAT no soporta todos los atributos; no es un error


def _sincronizar_directorio(ruta):
    try:
        fd = os.open(ruta, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _orden_fisico(directorio):
    """Nombres de los FLAC en el orden en que están en la tabla de directorios."""
    try:
        return [e for e in os.listdir(directorio)
                if e.lower().endswith(biblioteca.EXT_AUDIO)]
    except OSError:
        return []


def _tipo_sistema(ruta):
    """Sistema de archivos del punto de montaje que contiene `ruta`."""
    ruta = os.path.abspath(ruta)
    mejor = ("", "")
    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            for linea in f:
                partes = linea.split()
                if len(partes) < 3:
                    continue
                montaje = _desescapar(partes[1])
                if (ruta == montaje or ruta.startswith(montaje.rstrip("/") + "/")) \
                        and len(montaje) > len(mejor[0]):
                    mejor = (montaje, partes[2])
    except OSError:
        pass
    return mejor[1]


def _orden_es_observable(ruta):
    """En ext4/btrfs el orden de listdir no dice nada; en FAT/exFAT sí."""
    return _tipo_sistema(ruta) in {"vfat", "exfat", "msdos"}


def copiar(destino_raiz, albumes, simular=False, rehacer=False, informar=print):
    """Copia secuencialmente los álbumes indicados. Devuelve un resumen."""
    resumen = {"copiados": 0, "omitidos": 0, "archivos": 0, "bytes": 0,
               "desordenados": [], "errores": []}

    for item in albumes:
        dir_destino = os.path.join(destino_raiz, item["artista"], item["album"])
        existe = os.path.isdir(dir_destino)
        esperado = list(item["pistas"])

        if existe and not rehacer:
            actual = _orden_fisico(dir_destino)
            if actual == esperado or (not _orden_es_observable(dir_destino)
                                      and sorted(actual) == sorted(esperado)):
                informar(f"  ⏩ {item['rel']} — ya está y en orden")
                resumen["omitidos"] += 1
                continue
            if sorted(actual) == sorted(esperado):
                informar(f"  ⚠️  {item['rel']} — están todas pero el orden físico no coincide")
                resumen["desordenados"].append(item["rel"])
                continue
            informar(f"  ⚠️  {item['rel']} — copia incompleta o distinta; "
                     f"usa --rehacer para reescribirla en orden")
            resumen["desordenados"].append(item["rel"])
            continue

        informar(f"  📁 {item['rel']} — {len(esperado)} pistas "
                 f"({comun.formato_bytes(item['bytes'])})")

        if simular:
            for i, pista in enumerate(esperado, 1):
                informar(f"       {i:>3}. {pista}")
            resumen["copiados"] += 1
            resumen["archivos"] += len(esperado)
            resumen["bytes"] += item["bytes"]
            continue

        try:
            if existe and rehacer:
                # Borrar y recrear es lo único que devuelve la tabla de
                # directorios a un orden limpio en FAT/exFAT.
                shutil.rmtree(dir_destino)
            # Crear artista y álbum en este orden, nunca al revés
            os.makedirs(os.path.join(destino_raiz, item["artista"]), exist_ok=True)
            os.makedirs(dir_destino, exist_ok=False)

            for pista in esperado:
                origen = os.path.join(item["dir"], pista)
                _copiar_archivo(origen, os.path.join(dir_destino, pista))
                resumen["archivos"] += 1
                resumen["bytes"] += os.path.getsize(origen)
                informar(f"       ✓ {pista}")

            # La portada va al final para no ocupar una entrada antes de las pistas
            if item["portada"]:
                _copiar_archivo(os.path.join(item["dir"], item["portada"]),
                                os.path.join(dir_destino, item["portada"]))

            _sincronizar_directorio(dir_destino)
            os.sync()

            if _orden_es_observable(dir_destino):
                fisico = _orden_fisico(dir_destino)
                if fisico != esperado:
                    informar("       ⚠️  el orden físico no quedó como se esperaba")
                    resumen["desordenados"].append(item["rel"])
            resumen["copiados"] += 1

        except Exception as error:
            informar(f"       ❌ error: {error}")
            resumen["errores"].append(f"{item['rel']}: {error}")

    if not simular:
        os.sync()

    return resumen


def desmontar(montaje, informar=print):
    """sync + udisksctl unmount del punto de montaje."""
    os.sync()
    try:
        salida = subprocess.run(
            ["udisksctl", "unmount", "-p",
             f"block_devices/{os.path.basename(_dispositivo_de(montaje) or '')}"],
            capture_output=True, text=True, timeout=60)
        if salida.returncode != 0:
            salida = subprocess.run(["udisksctl", "unmount", "-b", _dispositivo_de(montaje)],
                                    capture_output=True, text=True, timeout=60)
        informar((salida.stdout or salida.stderr).strip())
        return salida.returncode == 0
    except (OSError, subprocess.SubprocessError) as error:
        informar(f"No se pudo desmontar automáticamente: {error}")
        return False


def _dispositivo_de(montaje):
    for d in detectar_destinos():
        if d["montaje"] == montaje:
            return d["dispositivo"]
    return None
