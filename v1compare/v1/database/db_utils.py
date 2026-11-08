from __future__ import annotations

from contextlib import contextmanager
import os
import sqlite3
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .models import ExperimentBase, MaterialBase, ProcessBase


MATERIAL_DB_URL = os.getenv("MATERIAL_DB_URL", "sqlite:///database/material_base.db")
PROCESS_DB_URL = os.getenv("PROCESS_DB_URL", "sqlite:///database/process_base.db")
EXPERIMENT_DB_URL = os.getenv("EXPERIMENT_DB_URL", "sqlite:///database/experiment_base.db")

material_engine = create_engine(MATERIAL_DB_URL, future=True)
process_engine = create_engine(PROCESS_DB_URL, future=True)
experiment_engine = create_engine(EXPERIMENT_DB_URL, future=True)

material_session_factory = sessionmaker(bind=material_engine, expire_on_commit=False, future=True)
process_session_factory = sessionmaker(bind=process_engine, expire_on_commit=False, future=True)
experiment_session_factory = sessionmaker(bind=experiment_engine, expire_on_commit=False, future=True)


def init_material_db() -> None:
    MaterialBase.metadata.create_all(material_engine)
    _patch_material_db_schema()


def init_process_db() -> None:
    ProcessBase.metadata.create_all(process_engine)


def init_experiment_db() -> None:
    ExperimentBase.metadata.create_all(experiment_engine)


def init_db() -> None:
    """Initialize all database files (material/process/experiment)."""

    init_material_db()
    init_process_db()
    init_experiment_db()
    _seed_chip_slots_if_empty()


def _patch_material_db_schema() -> None:
    if not MATERIAL_DB_URL.startswith("sqlite:///"):
        return
    db_path = MATERIAL_DB_URL.removeprefix("sqlite:///")
    if not db_path:
        return
    if not os.path.exists(db_path):
        return

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        flake_columns = {row[1] for row in cur.execute("PRAGMA table_info(flake)")}
        if "image_20x_id" not in flake_columns:
            cur.execute("ALTER TABLE flake ADD COLUMN image_20x_id INTEGER")
        if "mask_path" not in flake_columns:
            cur.execute("ALTER TABLE flake ADD COLUMN mask_path TEXT")
        if "mask_20x_path" not in flake_columns:
            cur.execute("ALTER TABLE flake ADD COLUMN mask_20x_path TEXT")
        device_columns = {row[1] for row in cur.execute("PRAGMA table_info(device)")}
        if "image_5x_id" not in device_columns:
            cur.execute("ALTER TABLE device ADD COLUMN image_5x_id INTEGER")
            if "image_id" in device_columns:
                cur.execute("UPDATE device SET image_5x_id = image_id WHERE image_5x_id IS NULL")
        if "image_10x_id" not in device_columns:
            cur.execute("ALTER TABLE device ADD COLUMN image_10x_id INTEGER")
        if "image_20x_id" not in device_columns:
            cur.execute("ALTER TABLE device ADD COLUMN image_20x_id INTEGER")
        if "image_100x_id" not in device_columns:
            cur.execute("ALTER TABLE device ADD COLUMN image_100x_id INTEGER")
        conn.commit()
    finally:
        conn.close()


def _seed_chip_slots_if_empty() -> None:
    from database import models
    from database.seed_chip_slots import seed_default_chip_slots

    with material_session_scope() as session:
        has_slots = session.query(models.ChipSlot.id).first() is not None
    if not has_slots:
        seed_default_chip_slots(init=False)


@contextmanager
def material_session_scope():
    session = material_session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def process_session_scope():
    session = process_session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def experiment_session_scope():
    session = experiment_session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# Backwards-compatible alias for existing imports that expect a single scope.
session_scope = material_session_scope
