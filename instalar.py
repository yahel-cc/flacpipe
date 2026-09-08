#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Instalador de flacpipe para Linux, macOS y Windows.

    python3 instalar.py                      # instala (con Nicotine+ CERRADO)
    python3 instalar.py --biblioteca RUTA    # si la música no está en la carpeta habitual
    python3 instalar.py --simular            # enseña qué haría, sin tocar nada
    python3 instalar.py --quitar             # desinstala (no toca la música ni la cola)

En Linux y macOS también vale  bash instalar.sh;  en Windows,  instalar.bat
(doble clic o desde una terminal). Los dos acaban llamando a este archivo.

Sólo usa la biblioteca estándar. Qué hace, en orden:

  1. comprueba Python (3.8+) y localiza Nicotine+: snap, flatpak o nativo en
     Linux, la app en macOS, %APPDATA%\\nicotine en Windows;
  2. decide dónde está la biblioteca de música y lo escribe en
     flac_hunter/flacpipe.json, que es lo único que leen el plugin, la CLI y
     la interfaz para saber dónde trabajar;
  3. enlaza flac_hunter/ en la carpeta de plugins de Nicotine+ (enlace
     simbólico; junction en Windows; copia si nada de eso se puede);
  4. habilita el plugin en la config de Nicotine+ y apunta downloaddir a
     <biblioteca>/.descargas, dejando una copia de la config al lado;
  5. deja el comando `flacpipe` a mano: ~/.local/bin en Linux y macOS,
     %LOCALAPPDATA%\\flacpipe\\bin (y el PATH del usuario) en Windows;
  6. en Windows instala windows-curses, que la interfaz necesita;
  7. crea <biblioteca>/.pipeline y .descargas y comprueba que todo importa.

Se puede volver a ejecutar las veces que haga falta: es idempotente.
"""
from __future__ import print_function

import sys

if sys.version_info < (3, 8):
    sys.stderr.write(
        "flacpipe necesita Python 3.8 o más nuevo; este es %s.\n"
        "  Linux:   sudo apt install python3   (o el gestor de tu distribución)\n"
        "  macOS:   brew install python        (o https://www.python.org/downloads/)\n"
        "  Windows: https://www.python.org/downloads/windows/  y marca \"Add python.exe to PATH\"\n"
        % sys.version.split()[0])
    sys.exit(1)

import argparse
import ast
import io
import json
import os
import re
import shutil
import subprocess

AQUI = os.path.dirname(os.path.abspath(__file__))
DIR_PLUGIN = os.path.join(AQUI, "flac_hunter")
ARCHIVO_CLI = os.path.join(AQUI, "flacpipe")
ARCHIVO_CONFIG = os.path.join(DIR_PLUGIN, "flacpipe.json")
ES_WINDOWS = sys.platform == "win32"
ES_MAC = sys.platform == "darwin"

for _flujo in (sys.stdout, sys.stderr):
    try:
        _flujo.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, DIR_PLUGIN)
import fh_comun as comun  # noqa: E402


# ─── Utilidades ───────────────────────────────────────────────────────────────

def es_enlace_o_junction(ruta):
    """True si `ruta` es un enlace simbólico o, en Windows, una junction."""
    if os.path.islink(ruta):
        return True
    if ES_WINDOWS:
        try:
            return bool(os.lstat(ruta).st_reparse_tag)
        except (OSError, AttributeError):
            return False
    return False


def crear_enlace_directorio(origen, destino):
    """Enlaza `destino` -> `origen`. Devuelve cómo lo hizo.

    En Windows un enlace simbólico exige modo desarrollador o administrador,
    pero una junction no, y a Nicotine+ le da igual cuál de las dos sea.
    """
    if ES_WINDOWS:
        try:
            import _winapi
            _winapi.CreateJunction(origen, destino)
            return "junction"
        except (ImportError, AttributeError, OSError):
            pass
        os.symlink(origen, destino, target_is_directory=True)
        return "enlace simbólico"
    os.symlink(origen, destino)
    return "enlace simbólico"


def quitar_ruta_plugin(destino):
    """Quita lo que haya en la carpeta de plugins con nuestro nombre, sea
    enlace, junction o copia. Si es otra cosa, se niega."""
    if not os.path.lexists(destino):
        return
    if es_enlace_o_junction(destino):
        try:
            os.unlink(destino)
        except OSError:
            os.rmdir(destino)
        return
    info = os.path.join(destino, "PLUGININFO")
    try:
        es_nuestro = "FLAC Hunter" in io.open(info, encoding="utf-8", errors="replace").read()
    except OSError:
        es_nuestro = False
    if not es_nuestro:
        raise SystemExit("En %s hay una carpeta que no es este plugin; quítala a mano." % destino)
    shutil.rmtree(destino)


def _span_seccion(texto, nombre):
    """(inicio, fin) del cuerpo de la sección [nombre] en un config de
    Nicotine+, o None si no existe."""
    m = re.search(r"^\[%s\][ \t]*\r?\n" % re.escape(nombre), texto, re.MULTILINE)
    if not m:
        return None
    siguiente = re.search(r"^\[[^\]]+\][ \t]*\r?\n", texto[m.end():], re.MULTILINE)
    fin = m.end() + siguiente.start() if siguiente else len(texto)
    return m.end(), fin


def _poner_clave(texto, seccion, clave, valor):
    """Deja `clave = valor` en [seccion], creando lo que falte."""
    span = _span_seccion(texto, seccion)
    linea = "%s = %s\n" % (clave, valor)
    if span is None:
        if texto and not texto.endswith("\n"):
            texto += "\n"
        return texto + "\n[%s]\n%s" % (seccion, linea)
    inicio, fin = span
    cuerpo = texto[inicio:fin]
    patron = re.compile(r"^%s[ \t]*=.*(?:\r?\n|$)" % re.escape(clave), re.MULTILINE)
    if patron.search(cuerpo):
        cuerpo = patron.sub(lambda _m: linea, cuerpo, count=1)
    else:
        cuerpo = linea + cuerpo
    return texto[:inicio] + cuerpo + texto[fin:]


def _plugins_habilitados(texto):
    span = _span_seccion(texto, "plugins")
    if span is None:
        return []
    m = re.search(r"^enabled[ \t]*=[ \t]*(\[.*\])[ \t]*$", texto[span[0]:span[1]], re.MULTILINE)
    if not m:
        return []
    try:
        lista = ast.literal_eval(m.group(1))
    except (ValueError, SyntaxError):
        return []
    return list(lista) if isinstance(lista, (list, tuple)) else []


def editar_config_nicotine(texto, descargas, quitar=False):
    """Texto nuevo de la config de Nicotine+ con el plugin (des)habilitado y,
    al instalar, downloaddir apuntando a .descargas.

    Nicotine+ escribe la config en modo texto, así que en Windows lleva CRLF;
    se trabaja con LF y se devuelve con los saltos que traía.
    """
    salto = "\r\n" if "\r\n" in texto else "\n"
    texto = texto.replace("\r\n", "\n")
    habilitados = _plugins_habilitados(texto)
    if quitar:
        habilitados = [p for p in habilitados if p != "flac_hunter"]
    elif "flac_hunter" not in habilitados:
        habilitados.append("flac_hunter")
    texto = _poner_clave(texto, "plugins", "enabled", repr(habilitados))
    if not quitar and descargas:
        texto = _poner_clave(texto, "transfers", "downloaddir", descargas)
    return texto.replace("\n", salto)


def _añadir_al_path_windows(dir_bin):
    """Mete dir_bin en el PATH del usuario (registro) y avisa a las ventanas
    nuevas. Devuelve True si hubo que añadirlo."""
    import ctypes
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_READ | winreg.KEY_WRITE) as clave:
        try:
            actual, tipo = winreg.QueryValueEx(clave, "Path")
        except FileNotFoundError:
            actual, tipo = "", winreg.REG_EXPAND_SZ
        partes = [p for p in actual.split(";") if p]
        objetivo = dir_bin.rstrip("\\").lower()
        if any(p.rstrip("\\").lower() == objetivo for p in partes):
            return False
        partes.append(dir_bin)
        winreg.SetValueEx(clave, "Path", 0, tipo or winreg.REG_EXPAND_SZ, ";".join(partes))
    resultado = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 5000,
                                             ctypes.byref(resultado))
    return True


def _quitar_del_path_windows(dir_bin):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                        winreg.KEY_READ | winreg.KEY_WRITE) as clave:
        try:
            actual, tipo = winreg.QueryValueEx(clave, "Path")
        except FileNotFoundError:
            return False
        objetivo = dir_bin.rstrip("\\").lower()
        partes = [p for p in actual.split(";") if p and p.rstrip("\\").lower() != objetivo]
        if len(partes) == len([p for p in actual.split(";") if p]):
            return False
        winreg.SetValueEx(clave, "Path", 0, tipo or winreg.REG_EXPAND_SZ, ";".join(partes))
    return True


def _ruta_bajo_home_visible(ruta):
    """El snap sólo lee @{HOME}/[^.]*/**: dentro de HOME y sin carpetas
    ocultas en el camino."""
    casa = os.path.realpath(comun.casa())
    ruta = os.path.realpath(ruta)
    if not ruta.startswith(casa.rstrip(os.sep) + os.sep):
        return False
    relativa = ruta[len(casa):].strip(os.sep)
    return not any(parte.startswith(".") for parte in relativa.split(os.sep))


# ─── El instalador ────────────────────────────────────────────────────────────

class Instalador(object):

    def __init__(self, args):
        self.args = args
        self.simular = args.simular
        self.avisos = []
        self.nicotine = None
        self.biblioteca = None
        self.dir_bin = None
        self.modo_plugin = None

    # salida
    def paso(self, texto):
        print("\n▸ " + texto)

    def ok(self, texto, detalle=""):
        print("  ✓ " + texto + ("\n      " + detalle if detalle else ""))

    def aviso(self, texto, detalle=""):
        self.avisos.append(texto)
        print("  ! " + texto + ("\n      " + detalle if detalle else ""))

    def haria(self, texto):
        print("  · " + texto + ("  (simulado)" if self.simular else ""))

    def falla(self, texto, detalle=""):
        print("  ✗ " + texto + ("\n      " + detalle if detalle else ""))
        sys.exit(1)

    def preguntar(self, pregunta, por_defecto=True):
        if self.args.si:
            return por_defecto
        if not sys.stdin.isatty():
            return por_defecto
        try:
            respuesta = input("  ? %s [%s] " % (pregunta, "S/n" if por_defecto else "s/N"))
        except EOFError:
            return por_defecto
        respuesta = respuesta.strip().lower()
        if not respuesta:
            return por_defecto
        return respuesta in ("s", "si", "sí", "y", "yes")

    # pasos
    def detectar(self):
        self.paso("Sistema")
        sistema = "Windows" if ES_WINDOWS else "macOS" if ES_MAC else "Linux"
        self.ok("%s, Python %s" % (sistema, sys.version.split()[0]), sys.executable)

        if self.args.nicotine_datos:
            datos = os.path.abspath(os.path.expanduser(self.args.nicotine_datos))
            config = os.path.abspath(os.path.expanduser(self.args.nicotine_config)) \
                if self.args.nicotine_config else os.path.join(datos, "config")
            sabor = ("windows" if ES_WINDOWS else "mac" if ES_MAC
                     else "snap" if os.sep + "snap" + os.sep in datos
                     else "flatpak" if ".var" + os.sep + "app" in datos else "nativo")
            self.nicotine = comun._rutas_nicotine(sabor, config, datos)
        else:
            self.nicotine = comun.detectar_nicotine()
        n = self.nicotine
        if os.path.isfile(n["config"]):
            self.ok("Nicotine+ (%s) con config en" % n["sabor"], n["config"])
        elif comun.programa_nicotine_instalado(n["sabor"]):
            self.aviso("Nicotine+ (%s) está instalado pero nunca se ha abierto" % n["sabor"],
                       "se creará una config mínima en " + n["config"])
        else:
            self.aviso("no encuentro Nicotine+ instalado; se deja todo preparado",
                       "instálalo desde https://nicotine-plus.org y ábrelo: el plugin "
                       "cargará solo.\n      Plugins en: " + n["plugins"])

        pid = comun.pid_nicotine()
        if pid is not None and not self.simular and not self.args.quitar:
            self.falla("Nicotine+ está abierto (pid %d). Ciérralo y vuelve a ejecutar esto." % pid,
                       "al salir reescribe su config y pisaría los cambios")
        elif pid is not None:
            self.aviso("Nicotine+ está abierto (pid %d)" % pid)

    def decidir_biblioteca(self):
        self.paso("Biblioteca")
        previa = comun.leer_config().get("biblioteca")
        if self.args.biblioteca:
            ruta = self.args.biblioteca
            origen = "por --biblioteca"
        elif previa:
            ruta = previa
            origen = "de una instalación anterior (flacpipe.json)"
        else:
            ruta = comun.musica_por_defecto()
            origen = "la carpeta de música del sistema"
        ruta = os.path.normpath(os.path.abspath(os.path.expanduser(ruta)))
        self.biblioteca = ruta
        if os.path.isdir(ruta):
            self.ok("%s  (%s)" % (ruta, origen))
        else:
            self.aviso("%s no existe (%s); se creará" % (ruta, origen),
                       "si tu música está en otro sitio: --biblioteca RUTA")
        if not self.simular:
            for carpeta in (ruta, os.path.join(ruta, ".pipeline"), os.path.join(ruta, ".descargas")):
                os.makedirs(carpeta, exist_ok=True)
            if not os.access(ruta, os.W_OK):
                self.falla("no puedo escribir en " + ruta)
        self.haria("crear %s y %s" % (os.path.join(ruta, ".pipeline"),
                                       os.path.join(ruta, ".descargas")))

    def escribir_config(self):
        self.paso("Configuración de flacpipe")
        datos = comun.leer_config()
        datos["biblioteca"] = self.biblioteca
        datos["nicotine"] = {"sabor": self.nicotine["sabor"],
                             "config": self.nicotine["config"],
                             "datos": self.nicotine["datos"]}
        datos.setdefault("ignorar", [])
        self.haria("escribir " + ARCHIVO_CONFIG)
        if not self.simular:
            with io.open(ARCHIVO_CONFIG, "w", encoding="utf-8") as f:
                json.dump(datos, f, ensure_ascii=False, indent=2)
                f.write("\n")
        self.ok("biblioteca = " + self.biblioteca)

    def enlazar_plugin(self):
        self.paso("Plugin en Nicotine+")
        dir_plugins = self.nicotine["plugins"]
        destino = os.path.join(dir_plugins, "flac_hunter")
        copiar = self.args.copiar
        if self.nicotine["sabor"] == "snap" and not _ruta_bajo_home_visible(DIR_PLUGIN):
            self.aviso("el snap no puede leer %s (AppArmor sólo deja carpetas no ocultas "
                       "dentro de tu HOME)" % DIR_PLUGIN,
                       "se copia el plugin en vez de enlazarlo: reinstala tras cambiar el código")
            copiar = True
        self.haria("quitar lo que haya en " + destino)
        self.haria(("copiar" if copiar else "enlazar") + " %s -> %s" % (destino, DIR_PLUGIN))
        if self.simular:
            self.modo_plugin = "copia" if copiar else "enlace"
            return
        os.makedirs(dir_plugins, exist_ok=True)
        quitar_ruta_plugin(destino)
        for raiz, carpetas, _archivos in os.walk(DIR_PLUGIN):
            for c in carpetas:
                if c == "__pycache__":
                    shutil.rmtree(os.path.join(raiz, c), ignore_errors=True)
        if not copiar:
            try:
                self.modo_plugin = crear_enlace_directorio(DIR_PLUGIN, destino)
            except OSError as error:
                self.aviso("no se pudo enlazar (%s); se copia" % error,
                           "reinstala tras cada cambio de código")
                copiar = True
        if copiar:
            shutil.copytree(DIR_PLUGIN, destino,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            self.modo_plugin = "copia"
        if not os.path.isfile(os.path.join(destino, "__init__.py")):
            self.falla("después de instalar no veo %s" % os.path.join(destino, "__init__.py"))
        self.ok("%s en %s" % (self.modo_plugin, destino))
        if self.modo_plugin == "copia":
            self.aviso("el plugin es una COPIA: tras cambiar el código, vuelve a ejecutar el instalador")

    def configurar_nicotine(self):
        self.paso("Config de Nicotine+")
        ruta = self.nicotine["config"]
        descargas = os.path.join(self.biblioteca, ".descargas")
        try:
            texto = io.open(ruta, encoding="utf-8").read()
        except OSError:
            texto = ""
        quitar = self.args.quitar
        nuevo = editar_config_nicotine(texto, descargas, quitar=quitar)
        if nuevo == texto:
            self.ok("ya estaba como hace falta", ruta)
            return
        if quitar:
            self.haria("quitar flac_hunter de los plugins habilitados en " + ruta)
        else:
            self.haria("habilitar flac_hunter y poner downloaddir = %s en %s" % (descargas, ruta))
        if self.simular:
            return
        os.makedirs(os.path.dirname(ruta), exist_ok=True)
        if texto:
            respaldo = ruta + ".antes-de-flacpipe"
            if not os.path.exists(respaldo):
                shutil.copy2(ruta, respaldo)
                self.ok("copia de seguridad de la config", respaldo)
        with io.open(ruta, "w", encoding="utf-8") as f:
            f.write(nuevo)
        self.ok("plugins habilitados: %s" % _plugins_habilitados(nuevo))
        if not quitar:
            self.ok("downloaddir = " + descargas,
                    "así una descarga manual no aparece como un artista en la biblioteca")

    def instalar_cli(self):
        self.paso("Comando flacpipe")
        if ES_WINDOWS:
            base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
            self.dir_bin = os.path.join(base, "flacpipe", "bin")
            archivo = os.path.join(self.dir_bin, "flacpipe.cmd")
            contenido = '@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, ARCHIVO_CLI)
            self.haria("escribir " + archivo)
            self.haria("añadir %s al PATH del usuario" % self.dir_bin)
            if self.simular:
                return
            os.makedirs(self.dir_bin, exist_ok=True)
            with io.open(archivo, "w", encoding="utf-8", newline="") as f:
                f.write(contenido)
            self.ok(archivo)
            try:
                if _añadir_al_path_windows(self.dir_bin):
                    self.ok("añadido al PATH del usuario",
                            "abre una terminal NUEVA para que la encuentre")
                else:
                    self.ok("ya estaba en el PATH")
            except OSError as error:
                self.aviso("no pude tocar el PATH (%s)" % error,
                           "añade a mano %s a la variable Path del usuario" % self.dir_bin)
            return

        self.dir_bin = os.path.join(os.path.expanduser("~"), ".local", "bin")
        enlace = os.path.join(self.dir_bin, "flacpipe")
        self.haria("enlazar %s -> %s" % (enlace, ARCHIVO_CLI))
        if not self.simular:
            os.makedirs(self.dir_bin, exist_ok=True)
            os.chmod(ARCHIVO_CLI, 0o755)
            if os.path.lexists(enlace):
                os.unlink(enlace)
            os.symlink(ARCHIVO_CLI, enlace)
            self.ok(enlace)
        self._asegurar_path_unix()

    def _asegurar_path_unix(self):
        en_path = any(os.path.realpath(p) == os.path.realpath(self.dir_bin)
                      for p in os.environ.get("PATH", "").split(os.pathsep) if p)
        if en_path:
            self.ok("%s ya está en el PATH" % self.dir_bin)
            return
        shell = os.path.basename(os.environ.get("SHELL", ""))
        if shell == "fish":
            self.aviso("%s no está en tu PATH" % self.dir_bin,
                       "en fish:  fish_add_path ~/.local/bin")
            return
        if shell == "zsh":
            rc = "~/.zshrc"
        elif shell == "bash":
            rc = "~/.bash_profile" if ES_MAC else "~/.bashrc"
        else:
            rc = "~/.profile"
        linea = 'export PATH="$HOME/.local/bin:$PATH"'
        ruta_rc = os.path.expanduser(rc)
        try:
            ya = os.path.isfile(ruta_rc) and linea in io.open(ruta_rc, encoding="utf-8",
                                                               errors="replace").read()
        except OSError:
            ya = False
        if ya:
            self.aviso("%s no está en el PATH de esta terminal, pero sí en %s" % (self.dir_bin, rc),
                       "abre una terminal nueva")
            return
        self.haria("añadir  %s  a %s" % (linea, rc))
        if self.simular:
            return
        if self.preguntar("¿Añadir ~/.local/bin al PATH en %s?" % rc):
            with io.open(ruta_rc, "a", encoding="utf-8") as f:
                f.write("\n# flacpipe\n%s\n" % linea)
            self.ok("añadido a %s" % rc, "abre una terminal NUEVA para que encuentre flacpipe")
        else:
            self.aviso("%s no está en tu PATH" % self.dir_bin,
                       "añade a %s:  %s" % (rc, linea))

    def instalar_curses_windows(self):
        if not ES_WINDOWS:
            return
        self.paso("Interfaz de terminal (windows-curses)")
        try:
            import curses  # noqa: F401
            self.ok("curses ya está disponible")
            return
        except ImportError:
            pass
        self.haria("%s -m pip install windows-curses" % sys.executable)
        if self.simular:
            return
        for extra in ([], ["--user"]):
            orden = [sys.executable, "-m", "pip", "install", "--quiet"] + extra + ["windows-curses"]
            try:
                r = subprocess.run(orden, capture_output=True, text=True, timeout=600)
            except (OSError, subprocess.SubprocessError) as error:
                r = None
                detalle = str(error)
            if r is not None and r.returncode == 0:
                self.ok("windows-curses instalado")
                return
            detalle = (r.stderr or r.stdout).strip().splitlines()[-1:] if r is not None else [detalle]
        self.aviso("no pude instalar windows-curses; la CLI funciona igual, la interfaz no",
                   "inténtalo a mano:  %s -m pip install windows-curses\n      %s"
                   % (sys.executable, " ".join(detalle)))

    def comprobar(self):
        self.paso("Comprobación")
        if self.simular:
            self.haria("importar los módulos con un intérprete limpio y lanzar flacpipe --help")
            return
        codigo = ("import os, sys; sys.path.insert(0, %r); "
                  "import fh_comun, fh_biblioteca, fh_fiio, fh_listas; "
                  "print(fh_comun.RAIZ_BIBLIOTECA); print(fh_comun.SABOR_NICOTINE); "
                  "print(os.path.isfile(os.path.join(fh_comun.ENLACE_PLUGIN, '__init__.py')))"
                  % DIR_PLUGIN)
        env = dict(os.environ)
        env.pop("FLACPIPE_BIBLIOTECA", None)
        try:
            r = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True,
                               timeout=60, env=env)
        except (OSError, subprocess.SubprocessError) as error:
            self.falla("no pude ejecutar la comprobación: %s" % error)
        if r.returncode != 0:
            self.falla("los módulos no importan", r.stderr.strip().splitlines()[-1:] and
                       r.stderr.strip().splitlines()[-1] or "")
        raiz, sabor, plugin_ok = (r.stdout.strip().splitlines() + ["", "", ""])[:3]
        if os.path.normcase(raiz) != os.path.normcase(self.biblioteca):
            self.falla("el código resuelve la biblioteca en %s, no en %s" % (raiz, self.biblioteca))
        self.ok("el código ve la biblioteca en %s (Nicotine+ %s)" % (raiz, sabor))
        if plugin_ok != "True":
            self.falla("el código no encuentra el plugin instalado en " + comun.ENLACE_PLUGIN)
        self.ok("el plugin instalado responde")
        try:
            r = subprocess.run([sys.executable, ARCHIVO_CLI, "--help"], capture_output=True,
                               text=True, timeout=60, env=env)
            if r.returncode == 0:
                self.ok("flacpipe --help funciona")
            else:
                self.aviso("flacpipe --help falló", r.stderr.strip().splitlines()[-1:][0]
                           if r.stderr.strip() else "")
        except (OSError, subprocess.SubprocessError) as error:
            self.aviso("no pude lanzar flacpipe: %s" % error)

    def resumen(self):
        print()
        if self.simular:
            print("Simulación terminada: no se ha tocado nada.")
            return
        print("✓ Instalado.")
        print("   1) Abre Nicotine+ (o:  flacpipe arrancar)")
        print("   2) flacpipe revisar          ← comprueba que el plugin cargó")
        print('   3) flacpipe pedir "Radiohead - OK Computer"')
        print("   4) flacpipe                  ← la interfaz")
        if self.avisos:
            print("\nCosas a tener en cuenta:")
            for a in self.avisos:
                print("   ! " + a)

    # desinstalar
    def quitar(self):
        self.paso("Desinstalar")
        destino = os.path.join(self.nicotine["plugins"], "flac_hunter")
        self.haria("quitar " + destino)
        if not self.simular and os.path.lexists(destino):
            quitar_ruta_plugin(destino)
            self.ok("plugin quitado de Nicotine+")
        self.configurar_nicotine()
        if ES_WINDOWS:
            base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
            dir_bin = os.path.join(base, "flacpipe", "bin")
            self.haria("borrar %s y quitarlo del PATH" % dir_bin)
            if not self.simular:
                shutil.rmtree(os.path.dirname(dir_bin), ignore_errors=True)
                try:
                    _quitar_del_path_windows(dir_bin)
                except OSError:
                    pass
                self.ok("comando flacpipe quitado")
        else:
            enlace = os.path.join(os.path.expanduser("~"), ".local", "bin", "flacpipe")
            self.haria("borrar " + enlace)
            if not self.simular and os.path.lexists(enlace):
                os.unlink(enlace)
                self.ok("comando flacpipe quitado")
        self.haria("borrar " + ARCHIVO_CONFIG)
        if not self.simular and os.path.exists(ARCHIVO_CONFIG):
            os.unlink(ARCHIVO_CONFIG)
        print("\n✓ Desinstalado. No se ha tocado la música, ni %s, ni la cola en .pipeline."
              % comun.RAIZ_BIBLIOTECA)
        print("  El código sigue en %s; bórralo tú si ya no lo quieres." % AQUI)

    def ejecutar(self):
        self.detectar()
        if self.args.quitar:
            self.biblioteca = comun.RAIZ_BIBLIOTECA
            self.quitar()
            return 0
        self.decidir_biblioteca()
        self.escribir_config()
        self.enlazar_plugin()
        self.configurar_nicotine()
        self.instalar_cli()
        self.instalar_curses_windows()
        self.comprobar()
        self.resumen()
        return 0


def main():
    p = argparse.ArgumentParser(
        description="Instala flacpipe: el plugin en Nicotine+ y el comando flacpipe.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Con Nicotine+ CERRADO. Se puede repetir las veces que haga falta.")
    p.add_argument("--biblioteca", metavar="RUTA",
                   help="Carpeta de la biblioteca de música (por defecto, la del sistema)")
    p.add_argument("--copiar", action="store_true",
                   help="Copiar el plugin en vez de enlazarlo")
    p.add_argument("--nicotine-datos", metavar="DIR",
                   help="Carpeta de datos de Nicotine+ si la detección no acierta "
                        "(la que contiene plugins/)")
    p.add_argument("--nicotine-config", metavar="ARCHIVO",
                   help="Archivo de config de Nicotine+ si no está junto a los datos")
    p.add_argument("--simular", "-n", action="store_true", help="Enseñar qué haría sin tocar nada")
    p.add_argument("--si", "-y", action="store_true", help="No preguntar nada")
    p.add_argument("--quitar", action="store_true", help="Desinstalar")
    args = p.parse_args()
    try:
        return Instalador(args).ejecutar()
    except KeyboardInterrupt:
        print("\nCancelado.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
