#!/usr/bin/env python3
"""Busca nombres usados que no existen: el NameError que sólo salta al pintar.

Sin dependencias (pyflakes no viene con Python): con `symtable` se sacan los
nombres que cada función lee como globales y se comprueba que el módulo los
define o que son de Python. No sustituye a un linter, pero caza lo que más
duele en una interfaz: una función borrada o mal escrita en una rama que sólo
se pinta en cierto estado.

    python3 pruebas/nombres.py flacpipe_tui.py flac_hunter/*.py
"""
import ast
import builtins
import sys
import symtable


def definidos(arbol):
    nombres = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__"}
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nombres.add(nodo.name)
        elif isinstance(nodo, (ast.Import, ast.ImportFrom)):
            for alias in nodo.names:
                nombres.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(nodo, ast.Name) and isinstance(nodo.ctx, ast.Store):
            nombres.add(nodo.id)
        elif isinstance(nodo, ast.Global):
            nombres.update(nodo.names)
    return nombres


def revisar(ruta):
    codigo = open(ruta, encoding="utf-8").read()
    arbol = ast.parse(codigo, ruta)
    validos = definidos(arbol)
    problemas = []

    def recorrer(tabla):
        for simbolo in tabla.get_symbols():
            if (simbolo.is_referenced() and simbolo.is_global()
                    and not simbolo.is_assigned() and simbolo.get_name() not in validos):
                problemas.append((tabla.get_lineno(), tabla.get_name(), simbolo.get_name()))
        for hija in tabla.get_children():
            recorrer(hija)

    recorrer(symtable.symtable(codigo, ruta, "exec"))
    return problemas


def main():
    total = 0
    for ruta in sys.argv[1:]:
        for linea, ambito, nombre in revisar(ruta):
            print(f"{ruta}:{linea}: «{nombre}» no está definido (en {ambito})")
            total += 1
    sys.exit(1 if total else 0)


if __name__ == "__main__":
    main()
