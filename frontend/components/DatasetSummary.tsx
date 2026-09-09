"use client";

import type { DatasetSummary } from "@/lib/api";
import { formatDecimal, formatNumber } from "@/lib/format";

/** Tela "Dataset encontrado": o que o sistema achou antes de processar. */
export function DatasetSummaryCard({ summary }: { summary: DatasetSummary }) {
  const camera = summary.cameras?.[0]?.model ?? "não identificada";
  const resolution = summary.resolutions?.[0]?.size ?? "—";

  return (
    <div className="card">
      <h2>Dataset encontrado</h2>
      <p className="sub" style={{ marginBottom: 14 }}>
        Todas as imagens abaixo pertencem ao mesmo voo e serão processadas como uma
        única missão.
      </p>

      <div className="grid cols-3" style={{ marginBottom: 14 }}>
        <div className="card stat" style={{ background: "var(--panel-2)" }}>
          <div className="value">{formatNumber(summary.valid_images)}</div>
          <div className="label">Imagens válidas</div>
        </div>
        <div className="card stat" style={{ background: "var(--panel-2)" }}>
          <div className="value">{formatNumber(summary.folders)}</div>
          <div className="label">Pastas encontradas</div>
        </div>
        <div className="card stat" style={{ background: "var(--panel-2)" }}>
          <div className="value">{formatDecimal(summary.area_ha, " ha")}</div>
          <div className="label">Área estimada</div>
        </div>
      </div>

      <div className="grid cols-2">
        <div>
          <div className="kv"><span>Arquivos varridos</span>
            <span>{formatNumber(summary.total_files)}</span></div>
          <div className="kv"><span>Imagens com GPS</span>
            <span>{formatNumber(summary.images_with_gps)}</span></div>
          <div className="kv"><span>Imagens sem GPS</span>
            <span>{formatNumber(summary.images_without_gps)}</span></div>
          <div className="kv"><span>Inválidas</span>
            <span>{formatNumber(summary.invalid_images)}</span></div>
          <div className="kv"><span>Duplicadas</span>
            <span>{formatNumber(summary.duplicate_images)}</span></div>
        </div>
        <div>
          <div className="kv"><span>Câmera detectada</span><span>{camera}</span></div>
          <div className="kv"><span>Resolução</span><span>{resolution}</span></div>
          <div className="kv"><span>Altitude média</span>
            <span>{formatDecimal(summary.mean_relative_altitude_m, " m")}</span></div>
          <div className="kv"><span>GSD estimado</span>
            <span>{formatDecimal(summary.gsd_cm, " cm/px")}</span></div>
          <div className="kv"><span>Sistema de coordenadas</span>
            <span>{summary.epsg ? `EPSG:${summary.epsg} (${summary.utm_zone})` : "—"}</span></div>
        </div>
      </div>

      {summary.bands?.length ? (
        <p className="muted" style={{ marginTop: 12, fontSize: 13 }}>
          Bandas: {summary.bands.map((b) => `${b.band} (${b.count})`).join(", ")}
        </p>
      ) : null}

      {summary.folder_names?.length ? (
        <p className="muted" style={{ marginTop: 10, fontSize: 12 }}>
          Subpastas no dataset: {summary.folder_names.join(", ")}
        </p>
      ) : null}

      {!!summary.invalid_images && (
        <div className="alert warn" style={{ marginTop: 14 }}>
          {formatNumber(summary.invalid_images)} imagens foram ignoradas devido a problemas
          nos arquivos. O processamento continua com as demais.
          {summary.invalid_samples?.length ? (
            <ul style={{ margin: "8px 0 0 16px" }}>
              {summary.invalid_samples.slice(0, 5).map((item) => (
                <li key={item.file}>{item.file} — {item.reason}</li>
              ))}
            </ul>
          ) : null}
        </div>
      )}

      {!!summary.duplicate_images && (
        <div className="alert info" style={{ marginTop: 10 }}>
          {formatNumber(summary.duplicate_images)} imagens duplicadas em pastas diferentes
          entraram no dataset uma única vez.
        </div>
      )}
    </div>
  );
}
