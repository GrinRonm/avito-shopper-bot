"""Простой доступ к config.json (перечитывается при каждом обращении)."""
import json
import os

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def cfg(key: str, default=None):
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get(key, default)
    except Exception:
        return default


def set_cfg(key: str, value):
    """Записать один ключ в config.json (используется админкой)."""
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data[key] = value
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
