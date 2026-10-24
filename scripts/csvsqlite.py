#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
csvsqlite.py — convierte cualquier CSV en una base SQLite lista para tabular.

Flujo temporal:
  1) check : valida tipos, fechas, dinero, nulos y anomalías; genera un reporte.
  2) init  : genera una plantilla YAML editable.
  3) build : construye la base, archiva el CSV, crea _imports/_changes y
             schema.sql.

Uso:
    python3 csvsqlite.py check "datos.csv" -o reports/check_datos.md
    python3 csvsqlite.py init  "datos.csv" --tabla datos -o plantilla.yaml
    python3 csvsqlite.py build "datos.csv" -s plantilla.yaml -o db/datos.sqlite

Delimitador y encoding se detectan automáticamente. Sin dependencias externas.
"""

from __future__ import annotations

import argparse
import codecs
import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

VERSION = "0.4.0-rc1"

# Sin dependencias externas: se incluye un emisor/lector YAML mínimo para el
# subconjunto que usa la especificación (mapeos, secuencias, escalares y flow
# `[a, b]` / `{k: v}`).


# ---------------------------------------------------------------------------
# 1. Núcleo de detección de tipos (reutilizado de analizador_csv.py)
# ---------------------------------------------------------------------------

VACIO = "Vacío"
BOOLEANO = "Booleano"
FECHA = "Fecha"
FECHA_HORA = "Fecha y hora"
HORA = "Hora"
ENTERO = "Entero"
DECIMAL = "Decimal"
MONEDA = "Moneda"
PORCENTAJE = "Porcentaje"
CODIGO = "Código/ID"
TEXTO = "Texto"

FAMILIAS = {
    BOOLEANO: "lógico",
    FECHA: "temporal",
    FECHA_HORA: "temporal",
    HORA: "temporal",
    ENTERO: "numérico",
    DECIMAL: "numérico",
    MONEDA: "numérico",
    PORCENTAJE: "numérico",
    CODIGO: "código",
    TEXTO: "texto",
}

VARIANTES_NOTA = {"notación científica", "separador ambiguo"}

NULOS_POR_DEFECTO = {"null", "none", "n/a", "#n/d", "#n/a", "s/n"}

BOOLEANOS = {
    "true", "false", "verdadero", "falso", "yes", "no", "si", "sí",
}

MAX_CARDINALIDAD = 20_000
MAX_ANOMALIAS = 50_000

PREFERENCIA_DOMINANTE = [
    TEXTO, FECHA, FECHA_HORA, HORA, BOOLEANO,
    CODIGO, ENTERO, DECIMAL, MONEDA, PORCENTAJE,
]

# Tipos de la especificación y su almacenamiento SQLite.
TIPOS_SPEC = {
    "TEXT", "INTEGER", "REAL", "DECIMAL", "MONEY",
    "BOOLEAN", "DATE", "DATETIME", "TIME",
}
SQL_TYPES = {
    "TEXT": "TEXT",
    "INTEGER": "INTEGER",
    "REAL": "REAL",
    "DECIMAL": "NUMERIC",
    "MONEY": "INTEGER",
    "BOOLEAN": "INTEGER",
    "DATE": "TEXT",
    "DATETIME": "TEXT",
    "TIME": "TEXT",
}

MAPEO_BOOLEANO_DEFECTO = {
    "true": True, "false": False,
    "verdadero": True, "falso": False,
    "yes": True, "no": False,
    "si": True, "sí": True,
    "1": True, "0": False,
}


@dataclass
class Deteccion:
    tipo: str
    formato: Optional[str] = None
    evidencia: Optional[str] = None
    padding: Optional[str] = None
    valor_num: Optional[float] = None
    nota: Optional[str] = None
    dos_digitos: bool = False


RE_ENTERO = re.compile(r"^[+-]?[0-9]+$")
RE_NUM_SIMPLE = re.compile(r"^[0-9+\-., ]+$")
RE_CIENTIFICA = re.compile(r"^[+-]?[0-9]+(?:[.,][0-9]+)?[eE][+-]?[0-9]+$")
RE_MILES_ES = re.compile(r"^[+-]?[0-9]{1,3}(?:\.[0-9]{3})+$")
RE_MILES_EN = re.compile(r"^[+-]?[0-9]{1,3}(?:,[0-9]{3})+$")
RE_DEC_ES = re.compile(r"^[+-]?[0-9]{1,3}(?:\.[0-9]{3})*,[0-9]+$")
RE_DEC_EN = re.compile(r"^[+-]?[0-9]{1,3}(?:,[0-9]{3})*\.[0-9]+$")
RE_NUM_ESPACIOS = re.compile(r"^[+-]?[0-9]{1,3}(?: [0-9]{3})+(?:[.,][0-9]+)?$")

RE_FECHA_ISO = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})$")
RE_FECHA_YMD = re.compile(r"^([0-9]{4})([/.-])([0-9]{1,2})\2([0-9]{1,2})$")
RE_FECHA_DMY = re.compile(r"^([0-9]{1,2})([/.-])([0-9]{1,2})\2([0-9]{4})$")
RE_FECHA_DMY2 = re.compile(r"^([0-9]{1,2})([/.-])([0-9]{1,2})\2([0-9]{2})$")
RE_FECHA_INVALIDA = re.compile(r"^[0-9]{1,4}([/.-])[0-9]{1,2}\1[0-9]{1,4}$")

RE_ISO_DT = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})[T ]([0-9]{2}):([0-9]{2})"
    r"(?::([0-9]{2})(?:\.([0-9]+))?)?"
    r"\s*(Z|[+-][0-9]{2}:?[0-9]{2})?$"
)
RE_YMD_DT = re.compile(
    r"^([0-9]{4})([/.-])([0-9]{1,2})\2([0-9]{1,2})"
    r"\s+([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?$"
)
RE_DMY_DT = re.compile(
    r"^([0-9]{1,2})([/.-])([0-9]{1,2})\2([0-9]{4})"
    r"\s+([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?$"
)
RE_DMY2_DT = re.compile(
    r"^([0-9]{1,2})([/.-])([0-9]{1,2})\2([0-9]{2})"
    r"\s+([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?$"
)

RE_HORA = re.compile(r"^([0-9]{1,2}):([0-9]{2})(?::([0-9]{2})(?:\.([0-9]+))?)?$")
RE_PORCENTAJE = re.compile(r"^([+-]?[0-9][0-9., ]*)\s*%\s*$")

SIMBOLOS_MONEDA = "$€£¥"
CODIGOS_MONEDA = (
    "USD", "EUR", "GBP", "JPY", "MXN", "COP", "PEN", "CLP",
    "ARS", "BRL", "CAD", "CHF", "BOB", "PYG", "UYU",
)
_MONEDA_ALT = "|".join(CODIGOS_MONEDA)
RE_MONEDA = re.compile(
    r"^(?P<signo>[+-])?\s*"
    r"(?P<pre>[$€£¥]|" + _MONEDA_ALT + r")?\s*"
    r"(?P<num>[0-9][0-9., ]*)\s*"
    r"(?P<post>[$€£¥]|" + _MONEDA_ALT + r")?\s*$",
    re.IGNORECASE,
)
RE_CODIGO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{3,63}$")


def normalizar(valor) -> str:
    if valor is None:
        return ""
    s = str(valor)
    s = s.replace("\ufeff", "")
    s = s.replace("\xa0", " ").replace("\u202f", " ").replace("\u2009", " ")
    s = s.replace("\u2212", "-")
    return s.strip()


def quitar_acentos(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s)
        if not unicodedata.combining(c)
    )


def es_booleano(s: str) -> bool:
    return s.lower() in BOOLEANOS or quitar_acentos(s).lower() in BOOLEANOS


def fecha_valida(y: int, m: int, d: int) -> bool:
    try:
        datetime(y, m, d)
        return True
    except ValueError:
        return False


def hora_valida(h: int, m: int, s: Optional[int] = None) -> bool:
    return 0 <= h <= 23 and 0 <= m <= 59 and (s is None or 0 <= s <= 59)


def _resolver_fecha(a_str: str, b_str: str, y_str: str, sep: str,
                    sufijo: str = "") -> Optional[Tuple[str, str, str, bool]]:
    a, b = int(a_str), int(b_str)
    dos_digitos = len(y_str) == 2
    if dos_digitos:
        yy = int(y_str)
        y = 2000 + yy if yy <= 68 else 1900 + yy
    else:
        y = int(y_str)

    dmy = fecha_valida(y, b, a)
    mdy = fecha_valida(y, a, b)

    if not dmy and not mdy:
        return None

    if dmy and not mdy:
        evidencia = "dmy"
        fmt = f"%d{sep}%m{sep}{'%y' if dos_digitos else '%Y'}{sufijo}"
    elif mdy and not dmy:
        evidencia = "mdy"
        fmt = f"%m{sep}%d{sep}{'%y' if dos_digitos else '%Y'}{sufijo}"
    else:
        evidencia = "ambigua"
        fmt = (
            f"%d{sep}%m{sep}{'%y' if dos_digitos else '%Y'}{sufijo}"
            f" o %m{sep}%d{sep}{'%y' if dos_digitos else '%Y'}{sufijo}"
        )

    padding = "con cero" if len(a_str) == 2 and len(b_str) == 2 else "sin cero"
    return evidencia, fmt, padding, dos_digitos


def detectar_fecha(s: str) -> Optional[Deteccion]:
    m = RE_FECHA_ISO.fullmatch(s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        if not fecha_valida(y, mo, d):
            return Deteccion(TEXTO, "parece fecha inválida", nota="fecha inválida")
        return Deteccion(FECHA, "%Y-%m-%d", evidencia="iso")

    m = RE_FECHA_YMD.fullmatch(s)
    if m:
        y, sep, mo, d = m.groups()
        if not fecha_valida(int(y), int(mo), int(d)):
            return Deteccion(TEXTO, "parece fecha inválida", nota="fecha inválida")
        return Deteccion(FECHA, f"%Y{sep}%m{sep}%d", evidencia="ymd")

    for regex in (RE_FECHA_DMY, RE_FECHA_DMY2):
        m = regex.fullmatch(s)
        if not m:
            continue
        a, sep, b, y = m.groups()
        res = _resolver_fecha(a, b, y, sep)
        if res is None:
            return Deteccion(TEXTO, "parece fecha inválida", nota="fecha inválida")
        evidencia, fmt, padding, dos = res
        return Deteccion(
            FECHA, fmt, evidencia=evidencia, padding=padding,
            nota="fecha ambigua" if evidencia == "ambigua" else None,
            dos_digitos=dos,
        )
    return None


def detectar_fecha_hora(s: str) -> Optional[Deteccion]:
    m = RE_ISO_DT.fullmatch(s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not fecha_valida(y, mo, d):
            return None
        h, mi = int(m.group(4)), int(m.group(5))
        sec = m.group(6)
        if not hora_valida(h, mi, int(sec) if sec is not None else None):
            return None
        s2 = s.replace("Z", "+00:00")
        s2 = re.sub(r"([+-][0-9]{2})([0-9]{2})$", r"\1:\2", s2)
        s2 = s2.replace(" ", "T", 1)
        try:
            datetime.fromisoformat(s2)
        except ValueError:
            return None
        etiqueta = "ISO 8601"
        if m.group(8):
            etiqueta += " con zona"
        if m.group(7):
            etiqueta += " con fracción de segundo"
        return Deteccion(FECHA_HORA, etiqueta, evidencia="iso")

    m = RE_YMD_DT.fullmatch(s)
    if m:
        y, sep, mo, d, h, mi, sec = m.groups()
        if not fecha_valida(int(y), int(mo), int(d)):
            return None
        if not hora_valida(int(h), int(mi), int(sec) if sec is not None else None):
            return None
        sufijo = " %H:%M:%S" if sec is not None else " %H:%M"
        return Deteccion(FECHA_HORA, f"%Y{sep}%m{sep}%d{sufijo}", evidencia="ymd")

    for regex in (RE_DMY_DT, RE_DMY2_DT):
        m = regex.fullmatch(s)
        if not m:
            continue
        a, sep, b, y, h, mi, sec = m.groups()
        if not hora_valida(int(h), int(mi), int(sec) if sec is not None else None):
            return None
        sufijo = " %H:%M:%S" if sec is not None else " %H:%M"
        res = _resolver_fecha(a, b, y, sep, sufijo=sufijo)
        if res is None:
            return None
        evidencia, fmt, padding, dos = res
        return Deteccion(
            FECHA_HORA, fmt, evidencia=evidencia, padding=padding,
            nota="fecha ambigua" if evidencia == "ambigua" else None,
            dos_digitos=dos,
        )
    return None


def detectar_hora(s: str) -> Optional[Deteccion]:
    m = RE_HORA.fullmatch(s)
    if not m:
        return None
    h, mi, sec, frac = m.groups()
    if not hora_valida(int(h), int(mi), int(sec) if sec is not None else None):
        return None
    if sec is None:
        fmt = "%H:%M"
    elif frac is not None:
        fmt = "%H:%M:%S.%f"
    else:
        fmt = "%H:%M:%S"
    return Deteccion(HORA, fmt)


def clasificar_numero(s: str, ceros_como_codigo: bool = True) -> Optional[Deteccion]:
    s = s.strip()
    if not s:
        return None

    if " " in s:
        if not RE_NUM_ESPACIOS.fullmatch(s):
            return None
        det = clasificar_numero(s.replace(" ", ""), ceros_como_codigo)
        if det is None:
            return None
        det.formato = (det.formato + " · miles con espacio") if det.formato else "miles con espacio"
        return det

    if RE_CIENTIFICA.fullmatch(s):
        try:
            v = float(s.replace(",", "."))
        except ValueError:
            return None
        if not math.isfinite(v):
            v = None
        return Deteccion(DECIMAL, "notación científica", valor_num=v, nota="notación científica")

    if ceros_como_codigo and re.fullmatch(r"[+-]?0[0-9]+", s):
        return Deteccion(CODIGO, "ceros a la izquierda")

    tiene_punto = "." in s
    tiene_coma = "," in s

    if tiene_punto and tiene_coma:
        if s.rfind(",") > s.rfind("."):
            if not RE_DEC_ES.fullmatch(s):
                return None
            v = float(s.replace(".", "").replace(",", "."))
            return Deteccion(DECIMAL, "coma decimal · miles con punto", valor_num=v)
        if not RE_DEC_EN.fullmatch(s):
            return None
        v = float(s.replace(",", ""))
        return Deteccion(DECIMAL, "punto decimal · miles con coma", valor_num=v)

    if tiene_punto:
        if s.count(".") > 1:
            if not RE_MILES_ES.fullmatch(s):
                return None
            return Deteccion(ENTERO, "miles con punto", valor_num=float(s.replace(".", "")))
        m = re.fullmatch(r"([+-]?)([0-9]+)\.([0-9]+)", s)
        if not m:
            return None
        signo, izq, der = m.groups()
        if len(der) == 3 and izq != "0":
            return Deteccion(DECIMAL, "separador ambiguo", nota="separador ambiguo")
        return Deteccion(DECIMAL, "punto decimal", valor_num=float(signo + izq + "." + der))

    if tiene_coma:
        if s.count(",") > 1:
            if not RE_MILES_EN.fullmatch(s):
                return None
            return Deteccion(ENTERO, "miles con coma", valor_num=float(s.replace(",", "")))
        m = re.fullmatch(r"([+-]?)([0-9]+),([0-9]+)", s)
        if not m:
            return None
        signo, izq, der = m.groups()
        if len(der) == 3 and izq != "0":
            return Deteccion(DECIMAL, "separador ambiguo", nota="separador ambiguo")
        return Deteccion(
            DECIMAL, "coma decimal",
            valor_num=float((signo + izq + "." + der) if signo else (izq + "." + der)),
        )

    if RE_ENTERO.fullmatch(s):
        return Deteccion(ENTERO, valor_num=float(s))

    return None


def detectar_porcentaje(s: str) -> Optional[Deteccion]:
    m = RE_PORCENTAJE.fullmatch(s)
    if not m:
        return None
    det = clasificar_numero(m.group(1).strip(), ceros_como_codigo=False)
    if det is None or det.tipo not in (ENTERO, DECIMAL):
        return None
    formato = "porcentaje" + (f" · {det.formato}" if det.formato else "")
    return Deteccion(PORCENTAJE, formato, valor_num=det.valor_num, nota=det.nota)


def detectar_moneda(s: str) -> Optional[Deteccion]:
    m = RE_MONEDA.fullmatch(s)
    if not m:
        return None
    pre, post = m.group("pre"), m.group("post")
    if not pre and not post:
        return None
    det = clasificar_numero(m.group("num").strip(), ceros_como_codigo=False)
    if det is None or det.tipo not in (ENTERO, DECIMAL):
        return None
    simbolo = (pre or post).upper()
    formato = f"moneda {simbolo}"
    if det.formato:
        formato += f" · {det.formato}"
    valor = det.valor_num
    if m.group("signo") == "-" and valor is not None:
        valor = -valor
    return Deteccion(MONEDA, formato, valor_num=valor, nota=det.nota)


def parece_fecha_invalida(s: str) -> bool:
    return RE_FECHA_INVALIDA.fullmatch(s) is not None


def detectar_codigo(s: str) -> Optional[Deteccion]:
    if not RE_CODIGO.fullmatch(s):
        return None
    digitos = sum(1 for c in s if c.isdigit())
    if digitos < 2:
        return None
    if s[0] == "0":
        return Deteccion(CODIGO, "ceros a la izquierda")
    if re.search(r"[._/-]", s) and re.search(r"[0-9][._/-][0-9]", s):
        return Deteccion(CODIGO, "con separadores")
    if re.search(r"[A-Za-z]", s) and re.search(r"[0-9]", s) and len(s) >= 6:
        return Deteccion(CODIGO, "alfanumérico")
    return None


def detectar_tipo(valor, nulos: Set[str]) -> Deteccion:
    s = normalizar(valor)
    if not s or s.lower() in nulos:
        return Deteccion(VACIO)
    if es_booleano(s):
        return Deteccion(BOOLEANO)
    if not any(ch.isdigit() for ch in s):
        return Deteccion(TEXTO)
    if RE_NUM_SIMPLE.fullmatch(s):
        det = clasificar_numero(s)
        if det is not None:
            return det

    det = detectar_fecha_hora(s)
    if det is not None:
        return det
    det = detectar_fecha(s)
    if det is not None:
        return det
    det = detectar_hora(s)
    if det is not None:
        return det
    det = detectar_porcentaje(s)
    if det is not None:
        return det
    det = detectar_moneda(s)
    if det is not None:
        return det
    det = clasificar_numero(s)
    if det is not None:
        return det
    if parece_fecha_invalida(s):
        return Deteccion(TEXTO, "parece fecha inválida", nota="fecha inválida")
    det = detectar_codigo(s)
    if det is not None:
        return det
    return Deteccion(TEXTO)


# ---------------------------------------------------------------------------
# 2. Estadísticas por columna
# ---------------------------------------------------------------------------

@dataclass
class Anomalia:
    fila: int
    tipo: str
    formato: str
    nota: str
    motivo: str
    valor: str


class Columna:
    def __init__(self, indice: int, nombre: str):
        self.indice = indice
        self.nombre = nombre

        self.total = 0
        self.vacios = 0
        self.tipos: Counter = Counter()
        self.formatos: Counter = Counter()
        self.ejemplos_formato: Dict[Tuple[str, str, Optional[str]], str] = {}
        self.evidencias: Counter = Counter()
        self.padding: Counter = Counter()
        self.ejemplos_ambiguos: List[str] = []
        self.dos_digitos = 0

        self.min_num: Optional[float] = None
        self.max_num: Optional[float] = None
        self.min_len: Optional[int] = None
        self.max_len: Optional[int] = None

        self.valores: Counter = Counter()
        self.cardinalidad_excedida = False
        self.ejemplos_tipo: Dict[str, List[str]] = defaultdict(list)

        self.tipo_dominante: Optional[str] = None
        self.familia_dominante: Optional[str] = None
        self.estado_base = ""
        self.estado = ""
        self.nota_variantes = False

        self.anomalias: List[Anomalia] = []
        self.anomalias_truncadas = False
        self.problemas: List[Tuple[str, str]] = []

    def registrar(self, det: Deteccion, valor: str) -> None:
        self.total += 1
        s = normalizar(valor)
        if det.tipo == VACIO:
            self.vacios += 1
            return

        self.tipos[det.tipo] += 1

        if det.formato:
            padding_fmt = det.padding if det.evidencia != "iso" else None
            clave = (det.tipo, det.formato, padding_fmt)
            self.formatos[clave] += 1
            self.ejemplos_formato.setdefault(clave, s)

        if det.evidencia:
            self.evidencias[det.evidencia] += 1
        if det.padding and det.evidencia in ("dmy", "mdy", "ambigua"):
            self.padding[det.padding] += 1
        if det.dos_digitos:
            self.dos_digitos += 1
        if det.evidencia == "ambigua" and len(self.ejemplos_ambiguos) < 5:
            if s not in self.ejemplos_ambiguos:
                self.ejemplos_ambiguos.append(s)

        if det.valor_num is not None and math.isfinite(det.valor_num):
            v = det.valor_num
            self.min_num = v if self.min_num is None else min(self.min_num, v)
            self.max_num = v if self.max_num is None else max(self.max_num, v)

        long = len(s)
        self.min_len = long if self.min_len is None else min(self.min_len, long)
        self.max_len = long if self.max_len is None else max(self.max_len, long)

        if len(self.ejemplos_tipo[det.tipo]) < 3 and s not in self.ejemplos_tipo[det.tipo]:
            self.ejemplos_tipo[det.tipo].append(s)

        if not self.cardinalidad_excedida:
            self.valores[s] += 1
            if len(self.valores) > MAX_CARDINALIDAD:
                self.cardinalidad_excedida = True
                self.valores.clear()

    @property
    def no_vacios(self) -> int:
        return self.total - self.vacios

    @property
    def cardinalidad(self) -> Optional[int]:
        return None if self.cardinalidad_excedida else len(self.valores)

    def calcular_veredicto(self) -> None:
        if self.no_vacios == 0:
            self.tipo_dominante = None
            self.familia_dominante = None
            self.estado_base = "Vacía"
            self.estado = "Vacía"
            return
        self.tipo_dominante = elegir_dominante(self.tipos)
        self.familia_dominante = FAMILIAS.get(self.tipo_dominante, "texto")
        familias = {FAMILIAS[t] for t in self.tipos}
        if len(familias) > 1:
            self.estado_base = "Mixta"
        elif self.cardinalidad == 1:
            self.estado_base = "Constante"
        else:
            self.estado_base = "Homogénea"
        self.estado = self.estado_base + (" con nulos" if self.vacios else "")
        formatos_dom = {
            fmt for (tipo, fmt, _pad) in self.formatos
            if tipo == self.tipo_dominante and fmt
        }
        self.nota_variantes = len(formatos_dom) > 1

    def conteo_familia(self) -> int:
        if self.familia_dominante is None:
            return 0
        return sum(
            c for t, c in self.tipos.items()
            if FAMILIAS.get(t) == self.familia_dominante
        )

    def confianza(self) -> float:
        if self.no_vacios == 0:
            return 0.0
        return 100.0 * self.conteo_familia() / self.no_vacios

    def es_enum_candidata(self) -> bool:
        if self.no_vacios == 0 or self.cardinalidad_excedida:
            return False
        if self.estado_base not in ("Homogénea", "Constante"):
            return False
        # Solo categorías de texto/código: las medidas numéricas y los
        # booleanos (que usan `mapeo`) no son enums.
        if self.familia_dominante not in ("código", "texto"):
            return False
        n = len(self.valores)
        if n < 2 or n > 50:
            return False
        if n >= self.no_vacios:
            return False
        return n <= 30 or (n / self.no_vacios) <= 0.05

    def es_fecha(self) -> bool:
        return FECHA in self.tipos or FECHA_HORA in self.tipos

    def necesita_anomalias(self) -> bool:
        if self.familia_dominante is None:
            return False
        if len({FAMILIAS[t] for t in self.tipos}) > 1:
            return True
        if not self.nota_variantes:
            return False
        formatos_dom = [
            fmt for (tipo, fmt, _pad) in self.formatos
            if tipo == self.tipo_dominante and fmt
        ]
        return any(
            "notación científica" in f or "separador ambiguo" in f
            for f in formatos_dom
        )

    def motivo_anomalia(self, det: Deteccion) -> Optional[str]:
        if det.tipo == VACIO or self.familia_dominante is None:
            return None
        if FAMILIAS.get(det.tipo) != self.familia_dominante:
            return "tipo distinto"
        if (
            det.tipo == self.tipo_dominante
            and det.nota in VARIANTES_NOTA
            and self.nota_variantes
        ):
            return "formato atípico"
        return None


def elegir_dominante(contador: Counter) -> Optional[str]:
    if not contador:
        return None
    maximo = max(contador.values())
    candidatos = [t for t, c in contador.items() if c == maximo]
    if len(candidatos) == 1:
        return candidatos[0]
    for t in PREFERENCIA_DOMINANTE:
        if t in candidatos:
            return t
    return candidatos[0]


# ---------------------------------------------------------------------------
# 3. Análisis del CSV (pass 1 + pass 2)
# ---------------------------------------------------------------------------

@dataclass
class Resultado:
    columnas: List[Columna]
    filas_leidas: int
    filas_descartadas: List[Tuple[int, int, int]]
    total_filas_descartadas: int
    errores_csv: List[Tuple[int, str]]
    encoding: str
    delimitador: str
    nulos: Set[str]
    bom: bool
    nombres_originales: List[str]
    cabeceras_vacias: int
    cabeceras_duplicadas: List[str]
    header_row: int = 1
    footer_rows: int = 0
    # rellenados por evaluar()
    limpio: bool = False
    bloqueantes: int = 0
    advertencias: int = 0


def construir_columnas(nombres_originales: Sequence[str]) -> List[Columna]:
    normalizados = [normalizar(n) for n in nombres_originales]
    conteo = Counter(normalizados)
    usados: Counter = Counter()
    columnas: List[Columna] = []
    for i, n in enumerate(normalizados):
        if not n:
            nombre = f"Columna {i + 1}"
        elif conteo[n] > 1:
            usados[n] += 1
            nombre = f"{n} ({usados[n]})"
        else:
            nombre = n
        columnas.append(Columna(i, nombre))
    return columnas


def tiene_bom(ruta: str) -> bool:
    with open(ruta, "rb") as f:
        return f.read(3) == codecs.BOM_UTF8


def detectar_encoding(ruta: str) -> str:
    with open(ruta, "rb") as f:
        raw = f.read(1024 * 1024)
    if raw.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            raw.decode(enc)
            return enc
        except UnicodeDecodeError as e:
            if e.start >= len(raw) - 4:
                return enc
    return "latin-1"


def detectar_delimitador(ruta: str, encoding: str) -> str:
    with open(ruta, "r", encoding=encoding, newline="") as f:
        sample = f.read(64 * 1024)
    best = None
    for delim in (",", ";", "\t", "|"):
        try:
            rows = list(csv.reader(io.StringIO(sample), delimiter=delim))
        except csv.Error:
            continue
        rows = [r for r in rows[:40] if any(c.strip() for c in r)]
        if len(rows) < 2:
            continue
        counts = [len(r) for r in rows]
        mode, _ = Counter(counts).most_common(1)[0]
        if mode < 2:
            continue
        consistency = sum(1 for c in counts if c == mode) / len(counts)
        score = consistency * 100 + min(mode, 20)
        if best is None or score > best[0]:
            best = (score, delim)
    return best[1] if best else ","


def leer_registros(ruta: str, encoding: str, delimitador: str) -> List[Tuple[int, List[str]]]:
    registros: List[Tuple[int, List[str]]] = []
    with open(ruta, "r", encoding=encoding, newline="") as f:
        lector = csv.reader(f, delimiter=delimitador)
        for fila in lector:
            registros.append((lector.line_num, fila))
    return registros


def preparar_filas(
    registros: List[Tuple[int, List[str]]], header_row: int, footer_rows: int
) -> Tuple[List[str], List[Tuple[int, List[str]]]]:
    if header_row < 1 or header_row > len(registros):
        raise ValueError(f"header-row fuera de rango: {header_row}")
    cabecera = [str(c) for c in registros[header_row - 1][1]]
    datos = registros[header_row:]
    while datos and all(not str(c).strip() for c in datos[-1][1]):
        datos.pop()
    if footer_rows:
        if footer_rows >= len(datos):
            raise ValueError("footer-rows es mayor o igual que las filas de datos.")
        datos = datos[:-footer_rows]
    return cabecera, datos


def analizar(
    ruta: str,
    delimitador: str,
    encoding: str,
    nulos: Set[str],
    header_row: int = 1,
    footer_rows: int = 0,
    forzar_texto: Optional[Set[str]] = None,
) -> Resultado:
    filas_descartadas: List[Tuple[int, int, int]] = []
    total_descartadas = 0
    errores_csv: List[Tuple[int, str]] = []
    filas_leidas = 0

    registros = leer_registros(ruta, encoding, delimitador)
    if not registros:
        raise ValueError("El archivo CSV está vacío.")
    columnas_raw, datos = preparar_filas(registros, header_row, footer_rows)
    if not columnas_raw:
        raise ValueError("La fila de cabecera está vacía.")

    num_columnas = len(columnas_raw)
    nombres_originales = [str(c) for c in columnas_raw]
    columnas = construir_columnas(nombres_originales)
    forzados = {normalizar(n) for n in (forzar_texto or set())}
    cabeceras_vacias = sum(1 for n in nombres_originales if not normalizar(n))
    cabeceras_duplicadas = [
        n for n, c in Counter(normalizar(n) for n in nombres_originales).items()
        if c > 1 and n
    ]

    ancho = len(columnas)
    for linea, fila in datos:
        if len(fila) != num_columnas:
            total_descartadas += 1
            if len(filas_descartadas) < 100:
                filas_descartadas.append((linea, len(fila), num_columnas))
            continue
        filas_leidas += 1
        for i, celda in enumerate(fila[:ancho]):
            if columnas[i].nombre in forzados:
                s = normalizar(celda)
                det = Deteccion(VACIO) if not s or s.lower() in nulos else Deteccion(TEXTO)
            else:
                det = detectar_tipo(celda, nulos)
            columnas[i].registrar(det, celda)

    for col in columnas:
        col.calcular_veredicto()

    columnas_anomalias = {
        i for i, col in enumerate(columnas) if col.necesita_anomalias()
    }
    if columnas_anomalias:
        for linea, fila in datos:
            if len(fila) != num_columnas:
                continue
            for i in columnas_anomalias:
                celda = fila[i]
                col = columnas[i]
                if col.nombre in forzados:
                    s = normalizar(celda)
                    det = Deteccion(VACIO) if not s or s.lower() in nulos else Deteccion(TEXTO)
                else:
                    det = detectar_tipo(celda, nulos)
                motivo = col.motivo_anomalia(det)
                if motivo is None:
                    continue
                if len(col.anomalias) >= MAX_ANOMALIAS:
                    col.anomalias_truncadas = True
                    continue
                col.anomalias.append(Anomalia(
                    fila=linea, tipo=det.tipo,
                    formato=(det.formato or "") + (
                        f" · {det.padding}" if det.padding and det.evidencia != "iso" else ""
                    ),
                    nota=det.nota or "", motivo=motivo,
                    valor=normalizar(celda),
                ))

    return Resultado(
        columnas=columnas,
        filas_leidas=filas_leidas,
        filas_descartadas=filas_descartadas,
        total_filas_descartadas=total_descartadas,
        errores_csv=errores_csv,
        encoding=encoding,
        delimitador=delimitador,
        nulos=nulos,
        bom=tiene_bom(ruta),
        nombres_originales=nombres_originales,
        cabeceras_vacias=cabeceras_vacias,
        cabeceras_duplicadas=cabeceras_duplicadas,
        header_row=header_row,
        footer_rows=footer_rows,
    )


# ---------------------------------------------------------------------------
# 4. Diagnóstico de limpieza
# ---------------------------------------------------------------------------

def _distribucion(col: Columna) -> str:
    items = sorted(col.tipos.items(), key=lambda kv: (-kv[1], kv[0]))
    return " · ".join(
        f"{t} {100.0*c/col.no_vacios:.1f}%".replace(".", ",")
        for t, c in items
    ) if col.no_vacios else "—"


def _problemas_fecha(col: Columna) -> List[Tuple[str, str]]:
    problemas: List[Tuple[str, str]] = []
    formatos = [(k, v) for k, v in col.formatos.items() if k[0] in (FECHA, FECHA_HORA) and k[1]]
    inequivocos = [(k, v) for k, v in formatos if " o " not in (k[1] or "")]
    unicos = {k[1] for k, _v in inequivocos}
    dmy = col.evidencias.get("dmy", 0)
    mdy = col.evidencias.get("mdy", 0)
    amb = col.evidencias.get("ambigua", 0)

    if len(unicos) > 1:
        problemas.append(("bloqueante", "formatos de fecha mezclados: " + ", ".join(sorted(unicos))))
    if amb and not (dmy or mdy):
        problemas.append(("bloqueante", f"{amb} celdas de fecha ambiguas sin orden resoluble"))
    elif amb:
        orden = "d/m" if dmy else "m/d"
        problemas.append(("advertencia", f"{amb} celdas ambiguas (resueltas como {orden})"))
    if col.dos_digitos:
        problemas.append(("advertencia", f"{col.dos_digitos} años de 2 dígitos (interpretados 1969–2068)"))
    return problemas


def diagnosticar(col: Columna) -> List[Tuple[str, str]]:
    problemas: List[Tuple[str, str]] = []
    if col.no_vacios == 0:
        problemas.append(("advertencia", "columna vacía"))
        return problemas

    if col.estado_base == "Mixta":
        problemas.append(("bloqueante", f"columna mixta: {_distribucion(col)}"))

    if col.es_fecha():
        problemas.extend(_problemas_fecha(col))

    bloqueantes = [
        a for a in col.anomalias
        if a.motivo == "tipo distinto" or a.nota == "separador ambiguo"
    ]
    if bloqueantes:
        problemas.append(("bloqueante", f"{len(bloqueantes)} celdas anómalas (tipo distinto o número ambiguo)"))

    cientificas = [a for a in col.anomalias if a.nota == "notación científica"]
    if cientificas:
        problemas.append(("advertencia", f"{len(cientificas)} celdas en notación científica"))

    return problemas


def evaluar(res: Resultado) -> Resultado:
    for col in res.columnas:
        col.problemas = diagnosticar(col)
        res.bloqueantes += sum(1 for s, _m in col.problemas if s == "bloqueante")
        res.advertencias += sum(1 for s, _m in col.problemas if s == "advertencia")

    if res.total_filas_descartadas:
        res.bloqueantes += 1
    if res.cabeceras_vacias or res.cabeceras_duplicadas:
        res.bloqueantes += 1

    res.limpio = res.bloqueantes == 0
    return res


# ---------------------------------------------------------------------------
# 5. Utilidades de formato
# ---------------------------------------------------------------------------

def fmt_num(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def plural(n: int, singular: str, plural_: str) -> str:
    return singular if n == 1 else plural_


def md_texto(valor) -> str:
    s = str(valor).replace("\r", " ").replace("\n", " ")
    return s.replace("|", "\\|")


def md_codigo(valor, max_len: int = 200) -> str:
    s = normalizar(valor)
    if not s:
        return "—"
    s = s.replace("|", "\\|")
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    if "`" in s:
        return f"`` {s} ``"
    return f"`{s}`"


def mostrar_delimitador(d: str) -> str:
    if d == "\t":
        return "TAB (\\t)"
    if d == " ":
        return "espacio"
    return d


def sanitizar_ident(nombre: str) -> str:
    s = normalizar(nombre).lower()
    s = re.sub(r"[^a-z0-9_]+", "_", s).strip("_")
    if not s or s[0].isdigit():
        s = "c_" + s
    return s


def ruta_temporal(suffix: str, prefix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix, prefix=prefix)
    os.close(fd)
    return path


# ---------------------------------------------------------------------------
# 5b. YAML mínimo (sin PyYAML)
# ---------------------------------------------------------------------------

def _yaml_dump(obj) -> str:
    out: List[str] = []

    def scalar(v) -> str:
        if v is None:
            return "null"
        if v is True:
            return "true"
        if v is False:
            return "false"
        if isinstance(v, int):
            return str(v)
        if isinstance(v, float):
            return repr(v)
        s = str(v)
        reservadas = {"true", "false", "null", "yes", "no", "on", "off", "none", "~"}
        if (
            re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_ ./\-]*", s)
            and s.lower() not in reservadas
        ):
            return s
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def key(k) -> str:
        k = str(k)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            return k
        return '"' + k.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def emit_dict(pad: str, d: dict, indent: int) -> None:
        for k, v in d.items():
            kk = key(k)
            if isinstance(v, dict):
                if not v:
                    out.append(pad + kk + ": {}")
                else:
                    out.append(pad + kk + ":")
                    emit_dict(pad + "  ", v, indent + 2)
            elif isinstance(v, list):
                if not v:
                    out.append(pad + kk + ": []")
                else:
                    out.append(pad + kk + ":")
                    emit_list(pad + "  ", v, indent + 2)
            else:
                out.append(pad + kk + ": " + scalar(v))

    def emit_list(pad: str, items: list, indent: int) -> None:
        for item in items:
            if isinstance(item, dict):
                if not item:
                    out.append(pad + "- {}")
                    continue
                it = list(item.items())
                k, v = it[0]
                kk = key(k)
                if isinstance(v, dict):
                    out.append(pad + "- " + kk + ":")
                    emit_dict(pad + "    ", v, indent + 2)
                elif isinstance(v, list):
                    out.append(pad + "- " + kk + ":")
                    emit_list(pad + "    ", v, indent + 2)
                else:
                    out.append(pad + "- " + kk + ": " + scalar(v))
                for k, v in it[1:]:
                    emit_dict(pad + "  ", {k: v}, indent + 2)
            elif isinstance(item, list):
                emit_list(pad, item, indent)
            else:
                out.append(pad + "- " + scalar(item))

    emit_dict("", obj, 0)
    return "\n".join(out) + "\n"


def _strip_comment(linea: str) -> str:
    out: List[str] = []
    in_s = in_d = False
    for i, c in enumerate(linea):
        if c == "'" and not in_d:
            in_s = not in_s
        elif c == '"' and not in_s:
            in_d = not in_d
        elif c == "#" and not in_s and not in_d and (i == 0 or linea[i - 1] in " \t"):
            break
        out.append(c)
    return "".join(out)


def _split_key(s: str) -> Tuple[str, str]:
    s = s.strip()
    in_s = in_d = False
    for i, c in enumerate(s):
        if c == "'" and not in_d:
            in_s = not in_s
        elif c == '"' and not in_s:
            in_d = not in_d
        elif c == ":" and not in_s and not in_d:
            return s[:i].strip(), s[i + 1:].strip()
    return s, ""


def _split_flow(s: str) -> List[str]:
    parts: List[str] = []
    depth = 0
    in_s = in_d = False
    cur: List[str] = []
    for c in s:
        if c == "'" and not in_d:
            in_s = not in_s
        elif c == '"' and not in_s:
            in_d = not in_d
        elif not in_s and not in_d:
            if c in "[{(":
                depth += 1
            elif c in "]})":
                depth -= 1
            elif c == "," and depth == 0:
                parts.append("".join(cur).strip())
                cur = []
                continue
        cur.append(c)
    resto = "".join(cur).strip()
    if resto:
        parts.append(resto)
    return parts


def _yaml_scalar(s: str):
    s = s.strip()
    if s == "" or s.lower() in ("null", "~"):
        return None
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        q = s[0]
        cuerpo = s[1:-1]
        if q == '"':
            cuerpo = cuerpo.replace('\\"', '"').replace("\\\\", "\\")
        else:
            cuerpo = cuerpo.replace("''", "'")
        return cuerpo
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if re.fullmatch(r"[+-]?[0-9]+", s):
        return int(s)
    if re.fullmatch(r"[+-]?[0-9]+\.[0-9]+", s):
        return float(s)
    return s


def _yaml_value(s: str):
    s = s.strip()
    if s.startswith("[") and s.endswith("]"):
        return [_yaml_scalar(x) for x in _split_flow(s[1:-1])]
    if s.startswith("{") and s.endswith("}"):
        d = {}
        for part in _split_flow(s[1:-1]):
            if not part:
                continue
            k, v = _split_key(part)
            d[k] = _yaml_scalar(v)
        return d
    return _yaml_scalar(s)


def _yaml_load(texto: str):
    lineas: List[Tuple[int, str]] = []
    for raw in texto.splitlines():
        sin = _strip_comment(raw)
        if not sin.strip():
            continue
        indent = len(sin) - len(sin.lstrip(" "))
        lineas.append((indent, sin.strip()))

    idx = 0
    n = len(lineas)

    def parse_map(ind: int) -> dict:
        nonlocal idx
        d: dict = {}
        while idx < n and lineas[idx][0] == ind:
            contenido = lineas[idx][1]
            if contenido.startswith("- ") or contenido == "-":
                break
            k, resto = _split_key(contenido)
            idx += 1
            if resto == "":
                if idx < n and lineas[idx][0] > ind:
                    d[k] = parse_block(lineas[idx][0])
                else:
                    d[k] = None
            else:
                d[k] = _yaml_value(resto)
        return d

    def parse_seq(ind: int) -> list:
        nonlocal idx
        items: list = []
        while idx < n and lineas[idx][0] == ind and (lineas[idx][1] == "-" or lineas[idx][1].startswith("- ")):
            contenido = lineas[idx][1]
            resto = contenido[1:].strip()
            idx += 1
            if resto == "":
                if idx < n and lineas[idx][0] > ind:
                    items.append(parse_block(lineas[idx][0]))
                else:
                    items.append(None)
            elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*:", resto):
                d: dict = {}
                k, r = _split_key(resto)
                if r == "":
                    if idx < n and lineas[idx][0] > ind:
                        d[k] = parse_block(lineas[idx][0])
                    else:
                        d[k] = None
                else:
                    d[k] = _yaml_value(r)
                while idx < n and lineas[idx][0] == ind + 2 and not (lineas[idx][1].startswith("- ") or lineas[idx][1] == "-"):
                    k2, r2 = _split_key(lineas[idx][1])
                    idx += 1
                    if r2 == "":
                        if idx < n and lineas[idx][0] > ind + 2:
                            d[k2] = parse_block(lineas[idx][0])
                        else:
                            d[k2] = None
                    else:
                        d[k2] = _yaml_value(r2)
                items.append(d)
            else:
                items.append(_yaml_value(resto))
        return items

    def parse_block(min_indent: int):
        nonlocal idx
        if idx >= n:
            return None
        ind, contenido = lineas[idx]
        if ind < min_indent:
            return None
        if contenido == "-" or contenido.startswith("- "):
            return parse_seq(ind)
        return parse_map(ind)

    return parse_block(0)


# ---------------------------------------------------------------------------
# 6. Comando `check`
# ---------------------------------------------------------------------------

def _tabla_md(encabezados, filas):
    lineas = ["| " + " | ".join(encabezados) + " |",
              "|" + "|".join(["---"] * len(encabezados)) + "|"]
    lineas += ["| " + " | ".join(str(c) for c in fila) + " |" for fila in filas]
    return lineas


def reporte_check(res: Resultado, ruta_csv: str) -> str:
    L = []
    L.append(f"# Reporte de validación — `{os.path.basename(ruta_csv)}`")
    L.append("")
    L += _tabla_md(["Campo", "Valor"], [
        ["Archivo", f"`{ruta_csv}`"],
        ["Delimitador", f"`{mostrar_delimitador(res.delimitador)}`"],
        ["Encoding", f"`{res.encoding}`"],
        ["Fila de encabezado", fmt_num(res.header_row)],
        ["Filas finales excluidas", fmt_num(res.footer_rows)],
        ["Filas de datos", fmt_num(res.filas_leidas)],
        ["Columnas", fmt_num(len(res.columnas))],
        ["Filas descartadas", fmt_num(res.total_filas_descartadas)],
    ])
    L.append("")

    L.append("## Veredicto")
    L.append("")
    if res.limpio:
        L.append("**CSV limpio** ✅ — listo para `init` y `build`.")
    else:
        L.append(f"**CSV NO limpio** ❌ — {fmt_num(res.bloqueantes)} "
                 f"{plural(res.bloqueantes, 'problema bloqueante', 'problemas bloqueantes')}"
                 f", {fmt_num(res.advertencias)} advertencia(s).")
    L.append("")

    L.append("## Problemas por columna")
    L.append("")
    con_problemas = [c for c in res.columnas if c.problemas]
    if not con_problemas:
        L.append("Ninguna columna con problemas.")
    else:
        for col in con_problemas:
            L.append(f"### {col.indice + 1}. {md_texto(col.nombre)}")
            L.append("")
            for severidad, mensaje in col.problemas:
                icono = "🔴" if severidad == "bloqueante" else "🟡"
                L.append(f"- {icono} **{severidad}**: {mensaje}")
            if col.anomalias:
                n = len(col.anomalias)
                L.append("")
                L.append(f"<details><summary>Ver las {fmt_num(n)} "
                         f"{plural(n, 'celda anómala', 'celdas anómalas')}</summary>")
                L.append("")
                L += _tabla_md(["Fila", "Valor", "Tipo", "Motivo"], [
                    [a.fila, md_codigo(a.valor), md_texto(a.tipo), md_texto(a.motivo or a.nota or "—")]
                    for a in col.anomalias[:1000]
                ])
                if n > 1000:
                    L.append("")
                    L.append(f"_Se muestran las primeras 1.000 de {fmt_num(n)}._")
                elif col.anomalias_truncadas:
                    L.append("")
                    L.append(f"_La lista se truncó en {fmt_num(MAX_ANOMALIAS)}._")
                L.append("")
                L.append("</details>")
            L.append("")
    L.append("")

    L.append("## Filas descartadas")
    L.append("")
    if not res.total_filas_descartadas:
        L.append("Ninguna.")
    else:
        L += _tabla_md(["Línea", "Campos", "Esperados"], [
            [a, b, c] for a, b, c in res.filas_descartadas
        ])
    L.append("")

    if res.cabeceras_vacias or res.cabeceras_duplicadas:
        L.append("## Cabeceras")
        L.append("")
        if res.cabeceras_vacias:
            L.append(f"- {fmt_num(res.cabeceras_vacias)} "
                     f"{plural(res.cabeceras_vacias, 'cabecera vacía', 'cabeceras vacías')}.")
        if res.cabeceras_duplicadas:
            L.append("- Cabeceras duplicadas: " + ", ".join(f"`{md_texto(n)}`" for n in res.cabeceras_duplicadas))
        L.append("")

    L.append("## Notas")
    L.append("")
    L.append("- Corrige los problemas bloqueantes sobre el propio CSV y vuelve a ejecutar `check`.")
    L.append("- Las advertencias no bloquean, pero conviene revisarlas.")
    L.append("- Tipos detectados por columna (referencia):")
    L.append("")
    L += _tabla_md(["#", "Columna", "Tipo detectado", "Distribución"], [
        [c.indice + 1, md_texto(c.nombre),
         ("Mixta" if c.estado_base == "Mixta" else (c.tipo_dominante or "—")),
         md_texto(_distribucion(c))]
        for c in res.columnas
    ])
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# 7. Comando `init`
# ---------------------------------------------------------------------------

MAPA_TIPO_SPEC = {
    ENTERO: "INTEGER",
    DECIMAL: "DECIMAL",
    MONEDA: "MONEY",
    PORCENTAJE: "REAL",
    BOOLEANO: "BOOLEAN",
    FECHA: "DATE",
    FECHA_HORA: "DATETIME",
    HORA: "TIME",
    CODIGO: "TEXT",
    TEXTO: "TEXT",
}


def _nombre_sugiere_dinero(nombre: str) -> bool:
    n = quitar_acentos(normalizar(nombre)).lower()
    pistas = (
        "total", "subtotal", "precio", "importe", "monto", "valor",
        "descuento", "impuesto", "iva", "tax", "amount", "price",
        "saldo", "credito", "debito", "cargo", "abono",
    )
    return any(p in n for p in pistas)


def tipo_spec_de_columna(col: Columna) -> str:
    tipos = set(col.tipos)
    if tipos <= {ENTERO}:
        return "INTEGER"
    if FECHA_HORA in tipos:
        return "DATETIME"
    if FECHA in tipos:
        return "DATE"
    if HORA in tipos:
        return "TIME"
    if MONEDA in tipos:
        return "MONEY"
    if _nombre_sugiere_dinero(col.nombre) and tipos <= {ENTERO, DECIMAL, MONEDA}:
        return "MONEY"
    if PORCENTAJE in tipos:
        return "REAL"
    if DECIMAL in tipos or ENTERO in tipos:
        return "DECIMAL"
    if BOOLEANO in tipos:
        return "BOOLEAN"
    if CODIGO in tipos or TEXTO in tipos:
        return "TEXT"
    # último recurso: mapeo por el tipo dominante
    return MAPA_TIPO_SPEC.get(col.tipo_dominante, "TEXT")


def formato_fuente_de_columna(col: Columna) -> Optional[str]:
    if not col.es_fecha():
        return None
    formatos = [
        (k, v) for k, v in col.formatos.items()
        if k[0] in (FECHA, FECHA_HORA) and k[1] and " o " not in (k[1] or "")
    ]
    if not formatos:
        return None
    (tipo, fmt, _pad), _ = max(formatos, key=lambda kv: kv[1])
    if fmt.startswith("ISO 8601"):
        return "iso8601"
    return fmt


def mapeo_booleano_de_columna(col: Columna) -> Optional[Dict[str, bool]]:
    if BOOLEANO not in col.tipos:
        return None
    norm = {
        normalizar(v): v
        for v in col.valores
        if not col.cardinalidad_excedida
    }
    mapeo: Dict[str, bool] = {}
    for valor_original in col.valores:
        clave = quitar_acentos(normalizar(valor_original)).lower()
        if clave in MAPEO_BOOLEANO_DEFECTO:
            mapeo[valor_original] = bool(MAPEO_BOOLEANO_DEFECTO[clave])
    return mapeo if mapeo else None


def generar_spec(res: Resultado, tabla: str) -> Dict:
    columnas = []
    for col in res.columnas:
        nombre_db = sanitizar_ident(col.nombre)
        entry: Dict = {
            "nombre": col.nombre,
            "nombre_db": nombre_db,
            "tipo": tipo_spec_de_columna(col),
            "nullable": col.vacios > 0,
            "clave_primaria": False,
            "unico": False,
            "indice": False,
        }

        if col.es_fecha():
            fmt = formato_fuente_de_columna(col)
            if fmt:
                entry["formato_fuente"] = fmt

        if BOOLEANO in col.tipos:
            mapeo = mapeo_booleano_de_columna(col)
            if mapeo:
                entry["mapeo"] = mapeo

        if entry["tipo"] == "MONEY":
            entry["escala"] = "centavos"

        columnas.append(entry)

    return {
        "tabla": tabla,
        "origen": {
            "delimitador": res.delimitador,
            "encoding": res.encoding,
            "header_row": res.header_row,
            "footer_rows": res.footer_rows,
        },
        "columnas": columnas,
    }


# ---------------------------------------------------------------------------
# 8. Comando `build`
# ---------------------------------------------------------------------------

def _cargar_spec(ruta: str) -> Dict:
    with open(ruta, "r", encoding="utf-8") as f:
        spec = _yaml_load(f.read())
    if not isinstance(spec, dict):
        raise ValueError("La especificación YAML debe ser un objeto/diccionario.")
    return spec


def _validar_spec(spec: Dict) -> Tuple[Dict[str, Dict], Dict[str, Dict]]:
    tabla = spec.get("tabla")
    if not tabla:
        raise ValueError("La especificación no define 'tabla'.")
    if sanitizar_ident(str(tabla)) in {"_imports", "_changes"}:
        raise ValueError(f"Nombre de tabla reservado: {tabla!r}.")
    columnas = spec.get("columnas")
    if not isinstance(columnas, list) or not columnas:
        raise ValueError("La especificación no define 'columnas' (lista).")

    norm: Dict[str, Dict] = {}
    dimensiones: Dict[str, Dict] = {}
    usados_db: Set[str] = set()
    for col in columnas:
        if not isinstance(col, dict):
            raise ValueError("Cada columna de la especificación debe ser un objeto.")
        nombre = str(col.get("nombre") or "").strip()
        if not nombre:
            raise ValueError("Hay una columna sin 'nombre'.")
        if nombre in norm:
            raise ValueError(f"Columna duplicada en la especificación: {nombre!r}.")
        tipo = col.get("tipo")
        if tipo not in TIPOS_SPEC:
            raise ValueError(f"Tipo inválido para la columna {nombre!r}: {tipo!r}.")
        nombre_db = sanitizar_ident(str(col.get("nombre_db") or nombre))
        if not nombre_db:
            raise ValueError(f"Nombre de columna inválido para {nombre!r}.")
        if nombre_db in {"id", "_source_row"}:
            raise ValueError(f"nombre_db reservado: {nombre_db!r}.")
        if nombre_db in usados_db:
            raise ValueError(f"nombre_db duplicado: {nombre_db!r}.")
        usados_db.add(nombre_db)
        col["nombre_db"] = nombre_db

        separar = col.get("separar_en")
        if separar:
            separar = sanitizar_ident(str(separar))
            if not separar:
                raise ValueError(f"separar_en inválido en {nombre!r}.")
            if separar == sanitizar_ident(str(tabla)):
                raise ValueError("La tabla separada no puede llamarse igual que la tabla principal.")
            if separar in {"_imports", "_changes"}:
                raise ValueError(f"Nombre de tabla reservado: {separar!r}.")
            if separar in dimensiones:
                raise ValueError(
                    f"Dos columnas no pueden separarse en la misma tabla: {separar!r}."
                )
            col_sep = sanitizar_ident(str(col.get("columna_tabla_separada") or nombre))
            if not col_sep:
                raise ValueError(f"columna_tabla_separada inválida en {nombre!r}.")
            dimensiones[separar] = {"columna": col_sep}
            col["separar_en"] = separar
            col["columna_tabla_separada"] = col_sep
        else:
            col.pop("separar_en", None)
            col.pop("columna_tabla_separada", None)

        norm[nombre] = col
    return norm, dimensiones


def _parsear_fecha(s: str, fmt: str) -> Optional[datetime]:
    if fmt == "iso8601":
        s2 = s.replace("Z", "+00:00")
        s2 = re.sub(r"([+-][0-9]{2})([0-9]{2})$", r"\1:\2", s2)
        s2 = s2.replace(" ", "T", 1)
        try:
            return datetime.fromisoformat(s2)
        except ValueError:
            return None
    try:
        return datetime.strptime(s, fmt)
    except ValueError:
        return None


def _money_cents(s: str) -> int:
    text = normalizar(s)
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    text = re.sub(
        r"[$€£¥]|(?:USD|EUR|GBP|JPY|MXN|COP|PEN|CLP|ARS|BRL|CAD|CHF|BOB|PYG|UYU)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = text.replace(" ", "")
    det = clasificar_numero(text, ceros_como_codigo=False)
    if det is None or det.tipo not in (ENTERO, DECIMAL) or det.valor_num is None:
        raise ValueError(f"{s!r} no es un importe válido")
    if det.nota == "separador ambiguo":
        raise ValueError(f"{s!r} tiene separador ambiguo")
    value = Decimal(str(det.valor_num))
    if negative:
        value = -value
    cents = (value * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(cents)


def _coaccionar(valor, colspec: Dict, nulos: Set[str]) -> Tuple[bool, object, Optional[str]]:
    s = normalizar(valor)
    tipo = colspec["tipo"]
    nullable = bool(colspec.get("nullable", False))

    if not s or s.lower() in nulos:
        if nullable:
            return True, None, None
        return False, None, "valor vacío en columna no nullable"

    if colspec.get("separar_en"):
        tipo = "TEXT"

    try:
        if tipo == "TEXT":
            v = s
        elif tipo == "INTEGER":
            if re.fullmatch(r"[+-]?0[0-9]+", s):
                return False, None, f"{s!r} tiene ceros a la izquierda; usa TEXT"
            det = clasificar_numero(s, ceros_como_codigo=False)
            if det is None or det.tipo != ENTERO:
                return False, None, f"{s!r} no es un entero válido"
            v = int(det.valor_num)
        elif tipo in ("REAL", "DECIMAL"):
            det = clasificar_numero(s, ceros_como_codigo=False)
            if det is None or det.tipo not in (ENTERO, DECIMAL):
                return False, None, f"{s!r} no es un número válido"
            if det.nota == "separador ambiguo":
                return False, None, f"{s!r} tiene separador ambiguo (¿1234 o 1.234?)"
            v = det.valor_num
        elif tipo == "MONEY":
            if colspec.get("escala", "centavos") != "centavos":
                return False, None, "escala de dinero no soportada; usa 'centavos'"
            v = _money_cents(s)
        elif tipo == "BOOLEAN":
            mapeo = dict(MAPEO_BOOLEANO_DEFECTO)
            extra = colspec.get("mapeo") or {}
            for k, val in extra.items():
                mapeo[quitar_acentos(normalizar(k)).lower()] = bool(val)
            clave = quitar_acentos(s).lower()
            if clave not in mapeo:
                return False, None, f"{s!r} no está en el mapeo booleano"
            v = 1 if mapeo[clave] else 0
        elif tipo in ("DATE", "DATETIME", "TIME"):
            fmt = colspec.get("formato_fuente")
            if not fmt:
                return False, None, f"falta 'formato_fuente' para columna {tipo}"
            dt = _parsear_fecha(s, fmt)
            if dt is None:
                return False, None, f"{s!r} no coincide con el formato {fmt!r}"
            if tipo == "DATE":
                v = dt.strftime("%Y-%m-%d")
            elif tipo == "DATETIME":
                v = dt.strftime("%Y-%m-%d %H:%M:%S")
            else:
                v = dt.strftime("%H:%M:%S")
        else:
            return False, None, f"tipo desconocido {tipo!r}"
    except (ValueError, InvalidOperation) as exc:
        return False, None, str(exc)

    enum = colspec.get("enum")
    if enum is not None and not colspec.get("separar_en") and tipo != "BOOLEAN":
        if v not in enum:
            return False, None, f"{s!r} no está en el enum permitido"

    return True, v, None


def _crear_tablas(cursor, spec: Dict, tabla: str, columnas_norm: Dict,
                  dimensiones: Dict[str, Dict]) -> None:
    for dim, info in dimensiones.items():
        col = info["columna"]
        cursor.execute(
            f'CREATE TABLE "{dim}" ("id" INTEGER PRIMARY KEY, '
            f'"{col}" TEXT NOT NULL UNIQUE)'
        )
        cursor.execute(f'CREATE INDEX "idx_{dim}_{col}" ON "{dim}" ("{col}")')

    defs: List[str] = ['"id" INTEGER PRIMARY KEY', '"_source_row" INTEGER NOT NULL']
    unique_cols: List[str] = []
    indices: List[str] = []
    for colspec in spec["columnas"]:
        nombre_db = colspec["nombre_db"]
        nullable = bool(colspec.get("nullable", False))
        sep = colspec.get("separar_en")
        if sep:
            defs.append(
                f'"{nombre_db}_id" INTEGER{" NOT NULL" if not nullable else ""} '
                f'REFERENCES "{sep}"("id")'
            )
            continue
        tipo_sql = SQL_TYPES[colspec["tipo"]]
        d = f'"{nombre_db}" {tipo_sql}'
        if not nullable:
            d += " NOT NULL"
        if colspec.get("clave_primaria"):
            unique_cols.append(nombre_db)
        if colspec.get("unico"):
            d += " UNIQUE"
        enum = colspec.get("enum")
        if enum:
            if colspec["tipo"] in ("INTEGER", "REAL", "DECIMAL", "MONEY", "BOOLEAN"):
                vals = ", ".join(str(v) for v in enum)
            else:
                vals = ", ".join("'" + str(v).replace("'", "''") + "'" for v in enum)
            d += f' CHECK ("{nombre_db}" IN ({vals}))'
        defs.append(d)
        if colspec.get("indice"):
            indices.append(nombre_db)

    if len(unique_cols) == 1:
        defs.append(f'UNIQUE ("{unique_cols[0]}")')
    elif len(unique_cols) > 1:
        defs.append("UNIQUE (" + ", ".join(f'"{c}"' for c in unique_cols) + ")")

    cursor.execute(f'CREATE TABLE "{tabla}" (' + ", ".join(defs) + ")")
    for c in indices:
        cursor.execute(f'CREATE INDEX "idx_{tabla}_{c}" ON "{tabla}" ("{c}")')


def construir_db(
    spec: Dict,
    csv_path: str,
    delimitador: str,
    encoding: str,
    nulos: Set[str],
    header_row: int = 1,
    footer_rows: int = 0,
) -> Dict:
    tabla = sanitizar_ident(spec["tabla"])
    columnas_norm, dimensiones = _validar_spec(spec)

    registros = leer_registros(csv_path, encoding, delimitador)
    if not registros:
        raise ValueError("El archivo CSV está vacío.")
    cabecera_raw, datos = preparar_filas(registros, header_row, footer_rows)
    cabecera = [normalizar(c) for c in cabecera_raw]

    for nombre in cabecera:
        if nombre not in columnas_norm:
            raise ValueError(f"La columna del CSV {nombre!r} no está en la especificación.")
    for nombre in columnas_norm:
        if nombre not in cabecera:
            raise ValueError(f"La columna de la especificación {nombre!r} no está en el CSV.")
    orden = [cabecera.index(colspec["nombre"]) for colspec in spec["columnas"]]

    errores: List[Tuple[int, str, str, str]] = []
    filas = 0
    for linea, fila in datos:
        if len(fila) != len(cabecera):
            errores.append((linea, "(fila)", "", f"{len(fila)} campos, se esperaban {len(cabecera)}"))
            continue
        filas += 1
        for colspec, idx in zip(spec["columnas"], orden):
            celda = fila[idx]
            ok, valor, error = _coaccionar(celda, colspec, nulos)
            if not ok:
                errores.append((linea, colspec["nombre"], celda, error))
                continue
            sep = colspec.get("separar_en")
            if sep and valor is not None:
                dimensiones[sep].setdefault("valores", set()).add(valor)

    if errores:
        return {"ok": False, "errores": errores, "tabla": tabla, "filas": filas}

    for dim, info in dimensiones.items():
        valores = sorted(info.get("valores", set()))
        info["valor_to_id"] = {v: i + 1 for i, v in enumerate(valores)}

    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    cursor = conn.cursor()
    _crear_tablas(cursor, spec, tabla, columnas_norm, dimensiones)

    for dim, info in dimensiones.items():
        col = info["columna"]
        for valor, did in info["valor_to_id"].items():
            cursor.execute(
                f'INSERT INTO "{dim}" ("id", "{col}") VALUES (?, ?)',
                (did, valor),
            )

    columnas_facto: List[Tuple[str, Dict, Optional[str]]] = [("_source_row", {}, None)]
    for colspec in spec["columnas"]:
        nombre_db = colspec["nombre_db"]
        if colspec.get("separar_en"):
            columnas_facto.append((f"{nombre_db}_id", colspec, colspec["separar_en"]))
        else:
            columnas_facto.append((nombre_db, colspec, None))

    insert_sql = (
        f'INSERT INTO "{tabla}" ('
        + ", ".join(f'"{c}"' for c, _s, _d in columnas_facto)
        + ") VALUES (" + ", ".join("?" for _ in columnas_facto) + ")"
    )

    for linea, fila in datos:
        if len(fila) != len(cabecera):
            continue
        valores: List[object] = [linea]
        for colspec, idx in zip(spec["columnas"], orden):
            celda = fila[idx]
            ok, valor, _error = _coaccionar(celda, colspec, nulos)
            if not ok:
                continue
            sep = colspec.get("separar_en")
            if sep:
                valores.append(
                    dimensiones[sep]["valor_to_id"].get(valor) if valor is not None else None
                )
            else:
                valores.append(valor)
        cursor.execute(insert_sql, valores)

    conn.commit()
    esquema = {}
    for dim in dimensiones:
        esquema[dim] = cursor.execute(f'SELECT COUNT(*) FROM "{dim}"').fetchone()[0]
    esquema[tabla] = cursor.execute(f'SELECT COUNT(*) FROM "{tabla}"').fetchone()[0]
    return {"ok": True, "conn": conn, "tabla": tabla, "filas": filas, "esquema": esquema}


# ---------------------------------------------------------------------------
# 8b. Metadatos, archivo y esquema
# ---------------------------------------------------------------------------


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _relative(path: str, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(root.resolve()))
    except ValueError:
        return str(Path(path).resolve())


def _archivar_csv(csv_path: str, root: Path, archive_dir: str, desactivado: bool) -> Optional[Path]:
    if desactivado:
        return None
    destino_dir = Path(archive_dir)
    if not destino_dir.is_absolute():
        destino_dir = root / destino_dir
    destino_dir.mkdir(parents=True, exist_ok=True)
    source = Path(csv_path).resolve()
    if source.parent == destino_dir.resolve():
        os.chmod(source, stat.S_IREAD)
        return source
    stem = re.sub(r'[<>:"/\\|?*]+', "_", source.stem).strip(" .") or "datos"
    target = destino_dir / f"{date.today().isoformat()}_{stem}.csv"
    counter = 2
    while target.exists():
        target = destino_dir / f"{date.today().isoformat()}_{stem}_{counter}.csv"
        counter += 1
    shutil.copy2(source, target)
    os.chmod(target, stat.S_IREAD)
    return target


def _dump_schema(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
        "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 "
        "WHEN 'view' THEN 2 WHEN 'trigger' THEN 3 ELSE 4 END, name"
    ).fetchall()
    lines = [
        f"-- Esquema generado por csvsqlite {VERSION}",
        "-- No editar a mano; la base es la fuente de verdad.",
        "",
        "PRAGMA foreign_keys = ON;",
        "",
    ]
    for _tipo, _nombre, sql in rows:
        lines.append(sql.rstrip(";") + ";")
        lines.append("")
    return "\n".join(lines)


def _crear_metadatos(
    conn: sqlite3.Connection,
    *,
    tabla: str,
    filas: int,
    esquema: Dict,
    csv_path: str,
    archived_path: Optional[Path],
    spec_path: str,
    root: Path,
) -> None:
    cursor = conn.cursor()
    cursor.execute(
        'CREATE TABLE "_imports" ('
        '"id" INTEGER PRIMARY KEY, '
        '"kind" TEXT NOT NULL, '
        '"source_path" TEXT NOT NULL, '
        '"archived_path" TEXT, '
        '"source_sha256" TEXT NOT NULL, '
        '"template_sha256" TEXT NOT NULL, '
        '"imported_at" TEXT NOT NULL, '
        '"source_rows" INTEGER NOT NULL, '
        '"rejected_rows" INTEGER NOT NULL, '
        '"details_json" TEXT NOT NULL, '
        '"tool_version" TEXT NOT NULL'
        ") STRICT"
    )
    cursor.execute(
        'CREATE TABLE "_changes" ('
        '"id" INTEGER PRIMARY KEY, '
        '"executed_at" TEXT NOT NULL, '
        '"kind" TEXT NOT NULL, '
        '"sql_text" TEXT NOT NULL, '
        '"summary" TEXT NOT NULL, '
        '"affected_rows" INTEGER NOT NULL, '
        '"reversible" INTEGER NOT NULL, '
        '"undo_path" TEXT, '
        '"backup_path" TEXT, '
        '"schema_before" TEXT, '
        '"schema_after" TEXT, '
        '"tool_version" TEXT NOT NULL'
        ") STRICT"
    )
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    archived_text = _relative(str(archived_path), root) if archived_path else None
    cursor.execute(
        'INSERT INTO "_imports" '
        '("kind", "source_path", "archived_path", "source_sha256", '
        '"template_sha256", "imported_at", "source_rows", "rejected_rows", '
        '"details_json", "tool_version") '
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "init",
            _relative(csv_path, root),
            archived_text,
            _sha256_file(csv_path),
            _sha256_file(spec_path),
            now,
            filas,
            0,
            json.dumps(esquema, ensure_ascii=False, sort_keys=True),
            VERSION,
        ),
    )
    cursor.execute(
        'INSERT INTO "_changes" '
        '("executed_at", "kind", "sql_text", "summary", "affected_rows", '
        '"reversible", "undo_path", "backup_path", "schema_before", '
        '"schema_after", "tool_version") '
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            now,
            "init",
            f"init {Path(csv_path).name}",
            f"Importación inicial de {Path(csv_path).name}",
            0,
            0,
            None,
            None,
            None,
            None,
            VERSION,
        ),
    )
    conn.commit()


def _backup_existing(out: Path) -> Optional[Path]:
    if not out.exists():
        return None
    backup_dir = out.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{out.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_pre-build.sqlite"
    shutil.copy2(out, target)
    return target


# ---------------------------------------------------------------------------
# 9. CLI
# ---------------------------------------------------------------------------

def _add_csv_args(p):
    p.add_argument("csv", help="Ruta del archivo CSV")
    p.add_argument("-d", "--delimitador", default="auto",
                   help="Delimitador (',', ';', '|', 'tab') o 'auto'")
    p.add_argument("-e", "--encoding", default="auto",
                   help="Encoding (utf-8-sig, latin-1, cp1252) o 'auto'")
    p.add_argument("--nulos", default="",
                   help="Literales adicionales de nulo separados por coma")
    p.add_argument("--header-row", type=int, default=None,
                   help="Fila lógica del encabezado (1-based; default 1)")
    p.add_argument("--footer-rows", type=int, default=None,
                   help="Filas finales a excluir (default 0)")
    p.add_argument("--forzar-texto", action="append", default=[],
                   help="Columna que debe tratarse como TEXT (repetible)")


def _normalizar_args(args) -> Set[str]:
    if not getattr(args, "encoding", None) or str(args.encoding).lower() == "auto":
        args.encoding = detectar_encoding(args.csv)
    if not getattr(args, "delimitador", None) or str(args.delimitador).lower() == "auto":
        args.delimitador = detectar_delimitador(args.csv, args.encoding)
    if str(args.delimitador).lower() in ("tab", "\\t"):
        args.delimitador = "\t"
    if len(args.delimitador) != 1:
        raise ValueError("El delimitador debe ser un solo carácter (usa 'tab' para tabulador).")
    try:
        codecs.lookup(args.encoding)
    except LookupError:
        raise ValueError(f"Encoding no válido: {args.encoding!r}")
    if args.header_row is not None and args.header_row < 1:
        raise ValueError("header-row empieza en 1.")
    if args.footer_rows is not None and args.footer_rows < 0:
        raise ValueError("footer-rows no puede ser negativo.")
    return {n.strip().lower() for n in args.nulos.split(",") if n.strip()}


def _forzados(args) -> Set[str]:
    return {normalizar(n) for n in (getattr(args, "forzar_texto", None) or []) if normalizar(n)}


def _cmd_check(args) -> int:
    nulos_extra = _normalizar_args(args)
    nulos = set(NULOS_POR_DEFECTO) | nulos_extra
    header_row = args.header_row if args.header_row is not None else 1
    footer_rows = args.footer_rows if args.footer_rows is not None else 0
    res = analizar(
        args.csv, args.delimitador, args.encoding, nulos,
        header_row=header_row, footer_rows=footer_rows,
        forzar_texto=_forzados(args),
    )
    evaluar(res)

    ruta = args.salida if getattr(args, "salida", None) else ruta_temporal(".md", "csvsqlite_check_")
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(reporte_check(res, args.csv))

    if res.limpio:
        print("CSV limpio ✅ — listo para `init` y `build`.")
        print(f"Reporte: {ruta}")
        return 0
    print(f"CSV NO limpio ❌ — {fmt_num(res.bloqueantes)} "
          f"{plural(res.bloqueantes, 'problema bloqueante', 'problemas bloqueantes')}.")
    print(f"Reporte: {ruta}")
    return 1


def _cmd_init(args) -> int:
    nulos_extra = _normalizar_args(args)
    nulos = set(NULOS_POR_DEFECTO) | nulos_extra
    header_row = args.header_row if args.header_row is not None else 1
    footer_rows = args.footer_rows if args.footer_rows is not None else 0
    res = analizar(
        args.csv, args.delimitador, args.encoding, nulos,
        header_row=header_row, footer_rows=footer_rows,
        forzar_texto=_forzados(args),
    )
    evaluar(res)

    if not res.limpio:
        print("El CSV no está limpio. Ejecuta `check` para ver los problemas bloqueantes:")
        for col in res.columnas:
            for severidad, mensaje in col.problemas:
                if severidad == "bloqueante":
                    print(f"  - {col.nombre}: {mensaje}")
        return 1

    tabla = args.tabla or sanitizar_ident(Path(args.csv).stem)
    spec = generar_spec(res, tabla)
    spec["origen"]["archivo"] = os.path.basename(args.csv)
    spec["origen"]["nulos"] = sorted(nulos_extra)
    ruta = args.salida if getattr(args, "salida", None) else "plantilla.yaml"

    encabezado = (
        "# Plantilla de la base SQLite\n"
        "# Revisa y ajusta tipos, MONEY (centavos), clave_primaria, unico,\n"
        "# indice y separar_en antes de ejecutar `build`.\n"
        "# - separar_en: extrae la columna a una tabla relacionada.\n"
        "# - columna_tabla_separada: nombre de la columna en esa tabla.\n"
        "# - MONEY: se guarda como INTEGER de centavos.\n"
        "# - formato_fuente: formato strptime de fechas o 'iso8601'.\n"
        "# - mapeo: traducción de valores booleanos.\n"
    )
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(encabezado)
        f.write(_yaml_dump(spec))

    print(f"Plantilla generada: {ruta}")
    print("Revisa tipos, dinero, tablas separadas y claves antes de `build`.")
    return 0


def _cmd_build(args) -> int:
    nulos_extra = _normalizar_args(args)
    spec = _cargar_spec(args.spec)
    nulos = set(NULOS_POR_DEFECTO) | nulos_extra
    nulos |= {
        str(n).strip().lower()
        for n in (spec.get("origen", {}).get("nulos") or [])
        if str(n).strip()
    }
    out = Path(args.salida)
    if out.exists() and not getattr(args, "force", False):
        print(f"El archivo {str(out)!r} ya existe. Usa --force para sobrescribirlo.")
        return 1

    origen = spec.get("origen", {}) or {}
    header_row = args.header_row if args.header_row is not None else int(origen.get("header_row", 1))
    footer_rows = args.footer_rows if args.footer_rows is not None else int(origen.get("footer_rows", 0))
    res = construir_db(
        spec, args.csv, args.delimitador, args.encoding, nulos,
        header_row=header_row, footer_rows=footer_rows,
    )

    if not res["ok"]:
        ruta = ruta_temporal(".md", "csvsqlite_errores_")
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(f"# Errores de validación — `{os.path.basename(args.csv)}`\n\n")
            f.write(f"Tabla: `{res['tabla']}` · Filas: {fmt_num(res['filas'])}\n\n")
            f.write("| Fila | Columna | Valor | Error |\n|---|---|---|---|\n")
            for linea, col, valor, error in res["errores"][:5000]:
                f.write(f"| {linea} | {md_texto(col)} | {md_codigo(valor)} | {md_texto(error)} |\n")
        print(f"Se encontraron {fmt_num(len(res['errores']))} errores de validación. "
              "No se escribió la base de datos.")
        print(f"Reporte de errores: {ruta}")
        return 1

    root = Path(getattr(args, "raiz", None) or Path.cwd()).expanduser().resolve()
    archived = _archivar_csv(
        args.csv, root,
        getattr(args, "archivar_dir", "data/imports"),
        getattr(args, "no_archivar", False),
    )
    conn: sqlite3.Connection = res["conn"]
    _crear_metadatos(
        conn,
        tabla=res["tabla"],
        filas=res["filas"],
        esquema=res["esquema"],
        csv_path=args.csv,
        archived_path=archived,
        spec_path=args.spec,
        root=root,
    )
    schema_dir = Path(getattr(args, "schema_dir", "schema"))
    if not schema_dir.is_absolute():
        schema_dir = root / schema_dir
    schema_dir.mkdir(parents=True, exist_ok=True)
    schema_path = schema_dir / "schema.sql"
    schema_path.write_text(_dump_schema(conn), encoding="utf-8")

    out.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup_existing(out) if getattr(args, "force", False) else None
    tmp = out.with_name(out.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    disco = sqlite3.connect(tmp)
    conn.backup(disco)
    disco.close()
    conn.close()
    os.replace(tmp, out)

    print(f"Base de datos creada: {out}")
    print(f"Tabla de hechos `{res['tabla']}`: {fmt_num(res['esquema'][res['tabla']])} filas.")
    for dim, n in res["esquema"].items():
        if dim != res["tabla"]:
            print(f"Tabla relacionada `{dim}`: {fmt_num(n)} valores.")
    if archived:
        print(f"CSV archivado: {archived}")
    print(f"Esquema: {schema_path}")
    if backup:
        print(f"Backup previo: {backup}")
    return 0


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_console()
    p = argparse.ArgumentParser(
        prog="csvsqlite.py",
        description="Convierte un CSV en una base SQLite lista para trabajar.",
    )
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="comando", required=True)

    p_check = sub.add_parser("check", help="Valida el CSV y genera un reporte (fase 1).")
    _add_csv_args(p_check)
    p_check.add_argument("-o", "--salida", help="Ruta del reporte .md (por defecto, temporal)")

    p_init = sub.add_parser("init", help="Genera la plantilla YAML desde un CSV limpio.")
    _add_csv_args(p_init)
    p_init.add_argument("-o", "--salida", help="Ruta de la plantilla YAML (default: plantilla.yaml)")
    p_init.add_argument("--tabla", help="Nombre de la tabla principal")

    p_build = sub.add_parser("build", help="Construye la base de datos SQLite (fase 2).")
    _add_csv_args(p_build)
    p_build.add_argument("-s", "--spec", required=True, help="Archivo de plantilla YAML")
    p_build.add_argument("-o", "--salida", required=True, help="Ruta de salida .sqlite")
    p_build.add_argument("--force", action="store_true", help="Sobrescribir la base si existe")
    p_build.add_argument("--raiz", help="Raíz del proyecto (default: directorio actual)")
    p_build.add_argument("--archivar-dir", default="data/imports",
                         help="Directorio para archivar el CSV")
    p_build.add_argument("--no-archivar", action="store_true",
                         help="No conservar una copia del CSV")
    p_build.add_argument("--schema-dir", default="schema",
                         help="Directorio para schema.sql")

    args = p.parse_args(argv)

    try:
        if args.comando == "check":
            return _cmd_check(args)
        if args.comando == "init":
            return _cmd_init(args)
        if args.comando == "build":
            return _cmd_build(args)
    except (ValueError, OSError, sqlite3.Error) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
