"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { formatDecimal, formatNumber } from "@/lib/format";

export function ExportPanel({ projectId }: { projectId: string }) {
  const [info, setInfo] = useState<Awaited<ReturnType<typeof api.rasterInfo>> | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [maxSize, setMaxSize] = useState(4096);

  useEffect(() => {
    api.rasterInfo(projectId).then(setInfo).catch((e) => setError(e.message));
  }, [projectId]);

  if (error) return <div className="alert warn">{error}</div>;

  const downloads: [string, string, string][] = [
    ["geotiff", "Ortomosaico RGB (GeoTIFF)", "Orto_<projeto>.rgb.tif, georreferenciado, na resolução do processamento"],
    ["png", "PNG", "Imagem de visualização, reamostrada"],
    ["report", "Relatório", "JSON com parâmetros, avisos e estatísticas do processamento"],
  ];

  return (
    <div className="grid cols-2" style={{ alignItems: "start" }}>
      <div className="card">
        <h2>Exportar</h2>
        <div className="field">
          <label>Tamanho máximo do PNG (px)</label>
          <select value={maxSize} onChange={(event) => setMaxSize(Number(event.target.value))}>
            {[1024, 2048, 4096, 8192, 16384].map((size) => (
              <option key={size} value={size}>{size}</option>
            ))}
          </select>
        </div>
        {downloads.map(([format, label, description]) => (
          <div className="kv" key={format}>
            <span>
              <strong style={{ color: "var(--text)" }}>{label}</strong>
              <br />
              <span style={{ fontSize: 12 }}>{description}</span>
            </span>
            <a
              className="btn"
              href={`/api/projects/${projectId}/exports/${format}${
                format === "png" ? `?max_size=${maxSize}` : ""
              }`}
            >
              Baixar
            </a>
          </div>
        ))}
      </div>

      <div className="card">
        <h2>Ortomosaico</h2>
        {info ? (
          <>
            <div className="kv"><span>Dimensões</span>
              <span>{formatNumber(info.width)} × {formatNumber(info.height)} px</span></div>
            <div className="kv"><span>GSD</span>
              <span>{formatDecimal(info.gsd_m * 100, " cm/px")}</span></div>
            <div className="kv"><span>Sistema de coordenadas</span>
              <span>EPSG:{info.epsg}</span></div>
            <div className="kv"><span>Limites (WGS84)</span>
              <span style={{ fontSize: 12 }}>
                {info.bounds_wgs84.map((v) => v.toFixed(5)).join(", ")}
              </span></div>
            <img
              src={`/api/projects/${projectId}/preview.jpg`}
              alt="pré-visualização do ortomosaico"
              style={{ width: "100%", marginTop: 14, borderRadius: 8, border: "1px solid var(--border)" }}
            />
          </>
        ) : (
          <p className="muted">Carregando informações do raster…</p>
        )}
      </div>
    </div>
  );
}
