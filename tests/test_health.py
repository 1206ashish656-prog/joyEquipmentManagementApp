"""FastAPI test for /healthz -- the public, unauthenticated health check
used by the hosting platform. Same TestClient + StaticPool SQLite
pattern as the other backend tests, though this route doesn't even
touch the DB."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import db.base as db_base
from backend.main import app
from db.models import Base


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(db_base, "_engine", engine)
    monkeypatch.setattr(db_base, "_SessionLocal", SessionLocal)
    yield TestClient(app)


def test_healthz_returns_ok_without_login(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
