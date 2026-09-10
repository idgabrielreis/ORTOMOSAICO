"""API HTTP: criação de projeto, varredura e resumo do dataset."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(storage_root):
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


def _esperar_job(client: TestClient, job_id: str, timeout: float = 120.0) -> dict:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = client.get(f"/api/jobs/{job_id}").json()
        if payload["status"] in ("succeeded", "failed", "canceled"):
            return payload
        time.sleep(0.5)
    raise AssertionError("job não terminou no tempo esperado")


def test_fluxo_projeto_scan_e_processamento(client: TestClient, flight_dir: Path):
    created = client.post("/api/projects", json={"name": "Fazenda Santa Rita"})
    assert created.status_code == 201
    project_id = created.json()["id"]

    scan = client.post(f"/api/projects/{project_id}/scan", json={"path": str(flight_dir)})
    assert scan.status_code == 202
    _esperar_job(client, scan.json()["job_id"])

    summary = client.get(f"/api/projects/{project_id}/summary").json()
    assert summary["valid_images"] == 6
    assert summary["folders"] >= 3
    assert summary["images_with_gps"] == 6

    folders = client.get(f"/api/projects/{project_id}/folders").json()
    assert len(folders) >= 3
    assert sum(f["images"] for f in folders) == summary["total_files"]

    geojson = client.get(f"/api/projects/{project_id}/images.geojson").json()
    tipos = {f["properties"]["kind"] for f in geojson["features"]}
    assert tipos == {"camera", "footprint"}

    job = client.post(f"/api/projects/{project_id}/jobs", json={"engine": "direct"})
    assert job.status_code == 202
    finished = _esperar_job(client, job.json()["id"])
    assert finished["status"] == "succeeded", finished["error"]

    info = client.get(f"/api/projects/{project_id}/exports/info").json()
    assert info["epsg"] and info["bounds_wgs84"]

    tile_json = client.get(f"/api/projects/{project_id}/tilejson.json")
    assert tile_json.status_code == 200
    assert tile_json.json()["bounds"]


def test_scan_com_caminho_inexistente_retorna_400(client: TestClient):
    project_id = client.post("/api/projects", json={"name": "x"}).json()["id"]

    response = client.post(f"/api/projects/{project_id}/scan", json={"path": "/nao/existe"})

    assert response.status_code == 400


def test_job_sem_imagens_retorna_400(client: TestClient):
    project_id = client.post("/api/projects", json={"name": "vazio"}).json()["id"]

    response = client.post(f"/api/projects/{project_id}/jobs", json={"engine": "direct"})

    assert response.status_code == 400


def test_system_info_lista_motores(client: TestClient):
    payload = client.get("/api/system/info").json()

    nomes = {engine["name"] for engine in payload["engines"]}
    assert {"odm", "direct"} <= nomes


def test_qualidade_do_projeto_vale_para_o_job(client: TestClient, flight_dir: Path):
    """A qualidade é escolhida na criação do projeto e o job herda essa escolha."""
    project_id = client.post(
        "/api/projects", json={"name": "Voo qualidade baixa", "quality": "baixa"}
    ).json()["id"]
    scan = client.post(f"/api/projects/{project_id}/scan", json={"paths": [str(flight_dir)]})
    _esperar_job(client, scan.json()["job_id"])

    job = client.post(f"/api/projects/{project_id}/jobs", json={"engine": "direct"})

    assert job.status_code == 202
    assert client.get(f"/api/projects/{project_id}").json()["quality"] == "baixa"


def test_system_info_lista_qualidades(client: TestClient):
    payload = client.get("/api/system/info").json()

    assert {q["value"] for q in payload["qualities"]} == {"alta", "media", "baixa"}
