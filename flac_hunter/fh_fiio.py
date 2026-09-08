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
# Sistemas en los que listdir devuelve el orden físico de la tabla de directorios.
SISTEMAS_FAT = {"vfat", "exfat", "msdos"}
# Cómo llama cada sistema operativo a lo mismo.
NOMBRES_TIPO = {"fat": "vfat", "fat12": "vfat", "fat16": "vfat", "fat32": "vfat"}
# Linux: dónde montan las tarjetas udisks y compañía. macOS usa /Volumes y
# Windows letras de unidad; eso lo resuelve _montajes().
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


def _montajes():
    """Todo lo montado: [(dispositivo, montaje, tipo, etiqueta, parece_tarjeta)].

    `parece_tarjeta` es lo que en cada sistema distingue un medio extraíble:
    en Linux, colgar de /run/media, /media o /mnt; en macOS, estar en /Volumes;
    en Windows, ser una unidad extraíble (o fija pero FAT/exFAT, que es como
    aparecen algunos lectores de tarjetas) y no la del sistema.
    """
    salida = []
    if comun.ES_WINDOWS:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        k32.GetDriveTypeW.restype = ctypes.c_uint
        # Sin esto, un lector de tarjetas vacío abre un diálogo "no hay disco".
        previo = k32.SetErrorMode(1)
        try:
            mascara = k32.GetLogicalDrives()
            sistema = os.environ.get("SystemDrive", "C:").upper()
            for i in range(26):
                if not mascara & (1 << i):
                    continue
                raiz = f"{chr(65 + i)}:\\"
                clase = k32.GetDriveTypeW(raiz)   # 2 extraíble, 3 fijo, 4 red, 5 óptico
                if clase not in (2, 3):
                    continue
                etiqueta = ctypes.create_unicode_buffer(261)
                nombre_fs = ctypes.create_unicode_buffer(261)
                if not k32.GetVolumeInformationW(raiz, etiqueta, 261, None, None, None,
                                                 nombre_fs, 261):
                    continue
                tipo = NOMBRES_TIPO.get(nombre_fs.value.lower(), nombre_fs.value.lower())
                parece = raiz[:2].upper() != sistema and (clase == 2 or tipo in SISTEMAS_FAT)
                salida.append((raiz, raiz, tipo, etiqueta.value or raiz[:2], parece))
        finally:
            k32.SetErrorMode(previo)
        return salida

    if comun.ES_MAC:
        try:
            texto = subprocess.run(["mount"], capture_output=True, text=True,
                                   timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return salida
        for linea in texto.splitlines():
            m = re.match(r"^(\S+) on (.+) \((\w+)", linea)
            if not m:
                continue
            montaje = m.group(2)
            tipo = NOMBRES_TIPO.get(m.group(3).lower(), m.group(3).lower())
            salida.append((m.group(1), montaje, tipo, os.path.basename(montaje),
                           montaje.startswith("/Volumes/")))
        return salida

    try:
        with open("/proc/mounts", "r", encoding="utf-8") as f:
            for linea in f:
                partes = linea.split()
                if len(partes) < 3:
                    continue
                montaje = _desescapar(partes[1])
                salida.append((partes[0], montaje, partes[2], os.path.basename(montaje),
                               montaje.startswith(BASES_MONTAJE)))
    except OSError:
        pass
    return salida


def detectar_destinos():
    """Puntos de montaje que parecen una tarjeta/DAP conectado."""
    encontrados = []
    for dispositivo, montaje, tipo, etiqueta, parece in _montajes():
        if tipo not in SISTEMAS_EXTRAIBLES or not parece:
            continue
        try:
            uso = shutil.disk_usage(montaje)
            libre, total = uso.free, uso.total
        except OSError:
            libre = total = 0
        encontrados.append({
            "montaje": montaje,
            "dispositivo": dispositivo,
            "tipo": tipo,
            "libre": libre,
            "total": total,
            "etiqueta": etiqueta,
        })
    return encontrados


def donde_se_buscan():
    """Texto para los mensajes: dónde se esperan las tarjetas en este sistema."""
    if comun.ES_WINDOWS:
        return "unidades extraíbles (letras de unidad)"
    if comun.ES_MAC:
        return "/Volumes"
    return "/run/media, /media y /mnt"


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

def _copiar_archivo(origen, destino, avance=None):
    """Copia un archivo y lo sincroniza a disco antes de devolver el control.

    Si se pasa `avance`, se le llama con los bytes de cada bloque escrito, para
    que una interfaz pueda pintar el progreso dentro de un archivo grande.
    """
    with open(origen, "rb") as f_in, open(destino, "wb") as f_out:
        if avance is None:
            shutil.copyfileobj(f_in, f_out, TAMAÑO_BLOQUE)
        else:
            while True:
                bloque = f_in.read(TAMAÑO_BLOQUE)
                if not bloque:
                    break
                f_out.write(bloque)
                avance(len(bloque))
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
    if comun.ES_WINDOWS:
        ruta = ruta.lower()
    mejor = ("", "")
    for _dispositivo, montaje, tipo, _etiqueta, _parece in _montajes():
        base = montaje.lower() if comun.ES_WINDOWS else montaje
        if (ruta == base or ruta.startswith(base.rstrip(os.sep) + os.sep)) \
                and len(base) > len(mejor[0]):
            mejor = (base, tipo)
    return mejor[1]


def _sync():
    """Vaciar cachés a disco. Windows no tiene sync global: ahí basta con el
    fsync por archivo que ya se hace."""
    if hasattr(os, "sync"):
        os.sync()


def _orden_es_observable(ruta):
    """En ext4/btrfs el orden de listdir no dice nada; en FAT/exFAT sí."""
    return _tipo_sistema(ruta) in SISTEMAS_FAT


def copiar(destino_raiz, albumes, simular=False, rehacer=False, informar=print,
           evento=None, cancelado=None):
    """Copia secuencialmente los álbumes indicados. Devuelve un resumen.

    `informar` recibe líneas de texto (la CLI las imprime tal cual).
    `evento` recibe el mismo avance como diccionarios, para pintar barras:
        {"t": "album",   "rel", "pistas", "bytes"}      empieza un álbum
        {"t": "omitido", "rel", "motivo"}               no hacía falta copiarlo
        {"t": "pista",   "rel", "archivo", "indice", "total", "bytes"}
        {"t": "bytes",   "n"}                           bloque escrito
        {"t": "pista_ok","archivo"}
        {"t": "orden",   "rel", "ok"}                   verificación del orden
        {"t": "album_ok","rel"}
        {"t": "error",   "rel", "error"}
        {"t": "abortado","rel"}
    `cancelado()` se consulta entre archivos: si devuelve True, se para en
    limpio. El álbum a medias queda marcado como desordenado, porque en FAT la
    única forma de arreglarlo es reescribirlo con --rehacer.
    """
    resumen = {"copiados": 0, "omitidos": 0, "archivos": 0, "bytes": 0,
               "desordenados": [], "errores": [], "abortado": False}

    def _ev(**kw):
        if evento:
            evento(kw)

    def _parar():
        return cancelado is not None and cancelado()

    for item in albumes:
        if _parar():
            resumen["abortado"] = True
            break

        dir_destino = os.path.join(destino_raiz, item["artista"], item["album"])
        existe = os.path.isdir(dir_destino)
        esperado = list(item["pistas"])

        if existe and not rehacer:
            actual = _orden_fisico(dir_destino)
            if actual == esperado or (not _orden_es_observable(dir_destino)
                                      and sorted(actual) == sorted(esperado)):
                informar(f"  » {item['rel']} — ya está y en orden")
                _ev(t="omitido", rel=item["rel"], motivo="ya está y en orden")
                resumen["omitidos"] += 1
                continue
            if sorted(actual) == sorted(esperado):
                informar(f"  ! {item['rel']} — están todas pero el orden físico no coincide")
                _ev(t="omitido", rel=item["rel"], motivo="el orden físico no coincide")
                resumen["desordenados"].append(item["rel"])
                continue
            informar(f"  ! {item['rel']} — copia incompleta o distinta; "
                     f"usa --rehacer para reescribirla en orden")
            _ev(t="omitido", rel=item["rel"], motivo="copia incompleta o distinta")
            resumen["desordenados"].append(item["rel"])
            continue

        informar(f"  ▸ {item['rel']} — {len(esperado)} pistas "
                 f"({comun.formato_bytes(item['bytes'])})")
        _ev(t="album", rel=item["rel"], pistas=len(esperado), bytes=item["bytes"])

        if simular:
            for i, pista in enumerate(esperado, 1):
                informar(f"       {i:>3}. {pista}")
                _ev(t="pista", rel=item["rel"], archivo=pista, indice=i,
                    total=len(esperado), bytes=0)
                _ev(t="pista_ok", archivo=pista)
            resumen["copiados"] += 1
            resumen["archivos"] += len(esperado)
            resumen["bytes"] += item["bytes"]
            _ev(t="album_ok", rel=item["rel"])
            continue

        try:
            if existe and rehacer:
                # Borrar y recrear es lo único que devuelve la tabla de
                # directorios a un orden limpio en FAT/exFAT.
                shutil.rmtree(dir_destino)
            # Crear artista y álbum en este orden, nunca al revés
            os.makedirs(os.path.join(destino_raiz, item["artista"]), exist_ok=True)
            os.makedirs(dir_destino, exist_ok=False)

            a_medias = False
            for i, pista in enumerate(esperado, 1):
                if _parar():
                    resumen["abortado"] = True
                    a_medias = True
                    informar(f"       · abortado tras {i - 1} de {len(esperado)} pistas")
                    _ev(t="abortado", rel=item["rel"])
                    break
                origen = os.path.join(item["dir"], pista)
                _ev(t="pista", rel=item["rel"], archivo=pista, indice=i,
                    total=len(esperado), bytes=os.path.getsize(origen))
                _copiar_archivo(origen, os.path.join(dir_destino, pista),
                                avance=(lambda n: _ev(t="bytes", n=n)) if evento else None)
                resumen["archivos"] += 1
                resumen["bytes"] += os.path.getsize(origen)
                informar(f"       ✓ {pista}")
                _ev(t="pista_ok", archivo=pista)

            if a_medias:
                # Queda medio álbum en la tarjeta: sólo --rehacer lo arregla.
                resumen["desordenados"].append(item["rel"])
                _sincronizar_directorio(dir_destino)
                _sync()
                break

            # La portada va al final para no ocupar una entrada antes de las pistas
            if item["portada"]:
                _copiar_archivo(os.path.join(item["dir"], item["portada"]),
                                os.path.join(dir_destino, item["portada"]))

            _sincronizar_directorio(dir_destino)
            _sync()

            if _orden_es_observable(dir_destino):
                fisico = _orden_fisico(dir_destino)
                if fisico != esperado:
                    informar("       ! el orden físico no quedó como se esperaba")
                    resumen["desordenados"].append(item["rel"])
                    _ev(t="orden", rel=item["rel"], ok=False)
                else:
                    _ev(t="orden", rel=item["rel"], ok=True)
            resumen["copiados"] += 1
            _ev(t="album_ok", rel=item["rel"])

        except Exception as error:
            informar(f"       ✗ error: {error}")
            _ev(t="error", rel=item["rel"], error=str(error))
            resumen["errores"].append(f"{item['rel']}: {error}")

    if not simular:
        _sync()

    return resumen


def desmontar(montaje, informar=print):
    """sync + desmontar/expulsar el punto de montaje, con lo que tenga cada sistema."""
    _sync()
    try:
        if comun.ES_WINDOWS:
            letra = os.path.splitdrive(montaje)[0]
            orden = ["powershell", "-NoProfile", "-Command",
                     "(New-Object -ComObject Shell.Application).NameSpace(17)"
                     f".ParseName('{letra}').InvokeVerb('Eject')"]
            salida = subprocess.run(orden, capture_output=True, text=True, timeout=60,
                                    creationflags=0x08000000)
        elif comun.ES_MAC:
            salida = subprocess.run(["diskutil", "unmount", montaje],
                                    capture_output=True, text=True, timeout=60)
        else:
            salida = subprocess.run(
                ["udisksctl", "unmount", "-p",
                 f"block_devices/{os.path.basename(_dispositivo_de(montaje) or '')}"],
                capture_output=True, text=True, timeout=60)
            if salida.returncode != 0:
                salida = subprocess.run(["udisksctl", "unmount", "-b", _dispositivo_de(montaje)],
                                        capture_output=True, text=True, timeout=60)
        informar((salida.stdout or salida.stderr).strip() or "hecho")
        return salida.returncode == 0
    except (OSError, subprocess.SubprocessError) as error:
        informar(f"No se pudo desmontar automáticamente: {error}")
        return False


def _dispositivo_de(montaje):
    for d in detectar_destinos():
        if d["montaje"] == montaje:
            return d["dispositivo"]
    return None
