"""Rutas, cola persistente y utilidades compartidas por el plugin y la CLI.

Este módulo lo importan dos procesos distintos:
  - el plugin, dentro del snap de Nicotine+ (confinado)
  - la CLI `flacpipe`, fuera del snap

Por eso todas las rutas son absolutas y toda escritura de la cola pasa por un
flock sobre `cola.lock`.
"""

import errno
import fcntl
import json
import os
import re
import time
import unicodedata
from datetime import datetime

# ─── Rutas ────────────────────────────────────────────────────────────────────
# El snap ve /home/yahel/Música porque la interfaz `home` permite
# @{HOME}/[^.]*/** — o sea, carpetas ocultas DENTRO de Música sí, pero no
# carpetas ocultas colgando directamente de $HOME.
RAIZ_BIBLIOTECA = "/home/yahel/Música"
DIR_PIPELINE = os.path.join(RAIZ_BIBLIOTECA, ".pipeline")
DIR_DESCARGAS = os.path.join(RAIZ_BIBLIOTECA, ".descargas")
ARCHIVO_COLA = os.path.join(DIR_PIPELINE, "cola.json")
ARCHIVO_LOCK = os.path.join(DIR_PIPELINE, "cola.lock")
ARCHIVO_LOG = os.path.join(DIR_PIPELINE, "pipeline.log")

# Carpetas de primer nivel de ~/Música que NO son artistas.
# "Artistas" es la biblioteca vieja de Yahel; se deja intacta a propósito.
NO_SON_ARTISTAS = {".pipeline", ".descargas", "Artistas", "lost+found"}

# ─── Estados de una petición ──────────────────────────────────────────────────
PENDIENTE = "pendiente"
BUSCANDO = "buscando"
DESCARGANDO = "descargando"
LISTO = "listo"
FALLIDO = "fallido"
CANCELADO = "cancelado"

EN_CURSO = {BUSCANDO, DESCARGANDO}
ACTIVOS = {PENDIENTE} | EN_CURSO
TERMINADOS = {LISTO, FALLIDO, CANCELADO}

MAX_LOG_BYTES = 1_000_000


def ahora():
    return datetime.now().isoformat(timespec="seconds")


def asegurar_dirs():
    for d in (DIR_PIPELINE, DIR_DESCARGAS):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as error:
            if error.errno != errno.EEXIST:
                raise


def registrar(mensaje):
    """Escribe una línea en el log del pipeline. Nunca lanza."""
    try:
        asegurar_dirs()
        if os.path.exists(ARCHIVO_LOG) and os.path.getsize(ARCHIVO_LOG) > MAX_LOG_BYTES:
            with open(ARCHIVO_LOG, "r", encoding="utf-8", errors="replace") as f:
                cola_texto = f.readlines()[-2000:]
            with open(ARCHIVO_LOG, "w", encoding="utf-8") as f:
                f.writelines(cola_texto)
        with open(ARCHIVO_LOG, "a", encoding="utf-8") as f:
            f.write(f"{ahora()}  {mensaje}\n")
    except Exception:
        pass


# ─── Cola con bloqueo ─────────────────────────────────────────────────────────

class bloqueo:
    """Context manager: flock exclusivo sobre cola.lock."""

    def __init__(self, timeout=20):
        self.timeout = timeout
        self._f = None

    def __enter__(self):
        asegurar_dirs()
        self._f = open(ARCHIVO_LOCK, "a+")
        limite = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self._f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() > limite:
                    raise TimeoutError(f"No se pudo bloquear {ARCHIVO_LOCK}")
                time.sleep(0.05)

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._f.fileno(), fcntl.LOCK_UN)
        finally:
            self._f.close()
            self._f = None
        return False


def _cola_vacia():
    return {"version": 1, "peticiones": []}


def leer_cola():
    """Lee la cola sin bloquear (para lecturas informativas)."""
    try:
        with open(ARCHIVO_COLA, "r", encoding="utf-8") as f:
            datos = json.load(f)
    except (OSError, ValueError):
        return _cola_vacia()
    if not isinstance(datos, dict) or "peticiones" not in datos:
        return _cola_vacia()
    return datos


def escribir_cola(cola):
    """Escritura atómica: temporal en el mismo directorio + os.replace."""
    asegurar_dirs()
    tmp = ARCHIVO_COLA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cola, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, ARCHIVO_COLA)


def modificar_cola(funcion):
    """Lee-modifica-escribe la cola bajo flock. `funcion(cola)` devuelve el resultado."""
    with bloqueo():
        cola = leer_cola()
        resultado = funcion(cola)
        escribir_cola(cola)
    return resultado


# ─── Peticiones ───────────────────────────────────────────────────────────────

def _slug(texto, maximo=40):
    texto = unicodedata.normalize("NFKD", texto)
    texto = texto.encode("ascii", "ignore").decode("ascii").lower()
    texto = re.sub(r"[^a-z0-9]+", "-", texto).strip("-")
    return texto[:maximo] or "peticion"


def separar_consulta(texto):
    """'Radiohead - OK Computer' -> ('Radiohead', 'OK Computer').

    Acepta ' - ', ' – ' y ' — ' como separador. Si no hay separador, todo el
    texto es la consulta y no sabemos artista ni álbum por adelantado.
    """
    for sep in (" — ", " – ", " - "):
        if sep in texto:
            artista, _, album = texto.partition(sep)
            return artista.strip(), album.strip()
    return None, None


def crear_peticion(consulta, tipo="album", artista=None, album=None, lista=None):
    consulta = " ".join(consulta.split())
    if artista is None and album is None:
        artista, album = separar_consulta(consulta)
    marca = datetime.now().strftime("%Y%m%d-%H%M%S")
    return {
        "id": f"{marca}-{_slug(consulta)}",
        "consulta": consulta,
        "tipo": tipo,                 # "album" | "cancion"
        "lista": lista,               # de qué playlist vino, si vino de una
        "artista": artista,
        "album": album,
        "estado": PENDIENTE,
        "creado": ahora(),
        "actualizado": ahora(),
        "intentos": 0,
        "candidato": None,
        "rechazados": [],             # [[usuario, carpeta], ...]
        "progreso": None,
        "resultado": None,
        "mensaje": "",
    }


def añadir_peticiones(consultas, tipo="album", lista=None):
    """Añade consultas a la cola, ignorando las que ya estén activas. Devuelve las creadas."""
    creadas = []

    def _op(cola):
        activas = {
            p["consulta"].casefold()
            for p in cola["peticiones"] if p["estado"] in ACTIVOS
        }
        for consulta in consultas:
            consulta = " ".join(consulta.split())
            if not consulta or consulta.casefold() in activas:
                continue
            pet = crear_peticion(consulta, tipo=tipo, lista=lista)
            cola["peticiones"].append(pet)
            activas.add(consulta.casefold())
            creadas.append(pet)

    modificar_cola(_op)
    for pet in creadas:
        registrar(f"[cola] + {pet['id']}  ({pet['consulta']})")
    return creadas


def buscar_peticion(cola, id_pet):
    for pet in cola["peticiones"]:
        if pet["id"] == id_pet:
            return pet
    return None


def pid_nicotine():
    """PID del Nicotine+ real, o None.

    No basta con buscar "nicotine" en la línea de comandos: `snap run --shell
    nicotine-plus -c ...` también la contiene y daba falsos positivos. El
    proceso de verdad ejecuta el script `bin/nicotine`, así que exigimos un
    argumento cuyo nombre base sea exactamente `nicotine` o `nicotine-plus`.
    """
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read().decode("utf-8", "replace").split("\0")
        except OSError:
            continue
        if any(a in ("--shell", "-c") for a in argv):
            continue
        for arg in argv:
            if "/" in arg and os.path.basename(arg) in ("nicotine", "nicotine-plus"):
                return int(pid)
    return None


def formato_bytes(n):
    for unidad in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unidad == "TB":
            return f"{n:.1f} {unidad}" if unidad != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


# ─── Instalación dentro del snap ──────────────────────────────────────────────
# Lo usan tanto `flacpipe revisar` como la vista de salud de la interfaz.

DIR_SNAP = "/home/yahel/snap/nicotine-plus/current"
ENLACE_PLUGIN = os.path.join(DIR_SNAP, ".local/share/nicotine/plugins/flac_hunter")
CONFIG_NICOTINE = os.path.join(DIR_SNAP, ".config/nicotine/config")


def ultimo_arranque_plugin():
    """Marca de tiempo del último '[plugin] activado' en pipeline.log."""
    try:
        with open(ARCHIVO_LOG, encoding="utf-8", errors="replace") as f:
            lineas = [l for l in f if "[plugin] activado" in l]
    except OSError:
        return None
    if not lineas:
        return None
    try:
        return datetime.fromisoformat(lineas[-1].split("  ")[0]).timestamp()
    except ValueError:
        return None


def snap_lee_plugin():
    """Comprueba de verdad que el proceso confinado alcanza el código del plugin."""
    import subprocess
    orden = ("python3 -c \"import os,sys;"
             "sys.exit(0 if os.path.isfile('%s/__init__.py') else 1)\"" % ENLACE_PLUGIN)
    try:
        r = subprocess.run(["snap", "run", "--shell", "nicotine-plus", "-c", orden],
                           capture_output=True, timeout=60)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None
