from __future__ import annotations
from app.db.repository import PurchasingRepository
import sqlite3
from app.db.database import get_connection

repo = PurchasingRepository()


def list_material_choices() -> list[dict]:
    return repo.list_material_choices()


def list_suppliers_for_material(goods_name: str) -> list[dict]:
    return repo.list_suppliers_for_material(goods_name)


def material_catalog_is_available() -> bool:
    return repo.count_material_choices() > 0


def material_exists(goods_name: str) -> bool:
    return repo.material_exists(goods_name)

