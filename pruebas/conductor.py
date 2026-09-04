#!/usr/bin/env python3
"""Corre la TUI dentro de un pty y vuelca la rejilla lógica tras cada repintado.

No se ejecuta a mano: lo lanza probar.py como hijo del pty. Recibe por
variables de entorno dónde volcar (SALIDA) y un escenario en JSON (ESCENARIO)
con lo que hay que sobreescribir de fh_comun/flacpipe_tui para simular una
cola, una descarga en marcha o una tarjeta montada.

Se parchea `escribir` (la única primitiva que pinta texto) para llevar una
copia exacta de lo que va a cada celda, con su color. Nada de emular ANSI.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import flacpipe_tui as tui  # noqa: E402

SALIDA = os.environ["SALIDA"]
escenario = json.loads(os.environ.get("ESCENARIO", "{}"))

comun = tui.comun
for clave, valor in escenario.get("comun", {}).items():
    setattr(comun, clave, valor)
for clave, valor in escenario.get("tui", {}).items():
    setattr(tui, clave, valor)
if escenario.get("nicotine_vivo"):
    comun.pid_nicotine = lambda: 424242

if escenario.get("fiio_falso"):
    import time as _time
    fiio = tui.fiio
    fiio.detectar_destinos = lambda: [{
        "montaje": "/run/media/yahel/SNOWSKY", "dispositivo": "/dev/loop9", "tipo": "vfat",
        "libre": 27_400_000_000, "total": 32_000_000_000, "etiqueta": "SNOWSKY"}]

    def copiar(destino_raiz, albumes, simular=False, rehacer=False, informar=print,
               evento=None, cancelado=None):
        resumen = {"copiados": 0, "archivos": 0, "bytes": 0, "errores": []}
        for a in albumes:
            informar(f"▸ {a['rel']}")
            evento({"t": "album", "rel": a["rel"], "pistas": len(a["pistas"]), "bytes": a["bytes"]})
            por_pista = a["bytes"] // max(1, len(a["pistas"]))
            for i, p in enumerate(a["pistas"], 1):
                nombre = p if isinstance(p, str) else p.get("archivo", str(p))
                evento({"t": "pista", "rel": a["rel"], "archivo": nombre, "indice": i,
                        "total": len(a["pistas"]), "bytes": por_pista})
                hecho = 0
                while hecho < por_pista:
                    if cancelado and cancelado():
                        return resumen
                    n = min(2_000_000, por_pista - hecho)
                    hecho += n
                    evento({"t": "bytes", "n": n})
                    _time.sleep(0.03)
                evento({"t": "pista_ok", "archivo": nombre})
                informar(f"  {tui.OK} {nombre}")
                resumen["archivos"] += 1
                resumen["bytes"] += por_pista
            evento({"t": "orden", "rel": a["rel"], "ok": True})
            evento({"t": "album_ok", "rel": a["rel"]})
            resumen["copiados"] += 1
        return resumen

    fiio.copiar = copiar

LETRAS = "0123456789ABCDEFGHIJ"
REJILLA = {"celdas": None}
_escribir = tui.escribir


def escribir(win, y, x, texto, color=tui.NORMAL, negrita=False, ancho=None):
    r = _escribir(win, y, x, texto, color, negrita, ancho)
    celdas = REJILLA["celdas"]
    if celdas is None or not texto:
        return r
    alto, cols = win.getmaxyx()
    if y < 0 or y >= alto or x < 0 or x >= cols:
        return r
    limite = cols - x
    if ancho is not None:
        limite = min(limite, ancho)
    for i, ch in enumerate(texto[:limite]):
        celdas[y][x + i] = (ch, color)
    return r


tui.escribir = escribir

_dibujar = tui.App.dibujar
CONTADOR = {"n": 0}


def dibujar(self):
    alto, cols = self.pantalla.getmaxyx()
    REJILLA["celdas"] = [[(" ", 0)] * cols for _ in range(alto)]
    _dibujar(self)
    CONTADOR["n"] += 1
    celdas = REJILLA["celdas"]
    texto = "\n".join("".join(c for c, _k in fila).rstrip() for fila in celdas)
    colores = "\n".join("".join(LETRAS[k] if c != " " else " " for c, k in fila).rstrip()
                        for fila in celdas)
    tmp = os.path.join(SALIDA, "pantalla.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(f"# frame {CONTADOR['n']}  {cols}x{alto}\n")
        f.write(texto + "\n\n== colores ==\n" + colores + "\n")
    os.replace(tmp, os.path.join(SALIDA, "pantalla.txt"))


tui.App.dibujar = dibujar

sys.exit(tui.ejecutar())
