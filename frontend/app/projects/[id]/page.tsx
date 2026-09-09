"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { DatasetSummaryCard } from "@/components/DatasetSummary";
import { ExportPanel } from "@/components/ExportPanel";
import { MapView } from "@/components/MapView";
import { ProcessingPanel } from "@/components/ProcessingPanel";
import { api, type Engine, type Job, type Project } from "@/lib/api";
import { STATUS_LABEL, formatDate, formatNumber } from "@/lib/format";

type Tab = "dataset" | "processamento" | "mapa" | "exportacao";

export default function ProjectPage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;
  const [project, setProject] = useState<Project | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [engines, setEngines] = useState<Engine[]>([]);
  const [tab, setTab] = useState<Tab>("dataset");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [loaded, jobList] = await Promise.all([
        api.project(projectId),
        api.jobs(projectId),
      ]);
      setProject(loaded);
      setJobs(jobList);
      return { loaded, jobList };
    } catch (e) {
      setError((e as Error).message);
      return null;
    }
  }, [projectId]);

  useEffect(() => {
    load().then((result) => {
      if (!result) return;
      const active = result.jobList.find((job) =>
        ["queued", "running"].includes(job.status) && job.kind === "orthomosaic",
      );
      if (active) setTab("processamento");
      else if (result.loaded.status === "completed") setTab("mapa");
    });
    api.systemInfo().then((info) => setEngines(info.engines)).catch(() => undefined);
  }, [load]);

  const activeJob = jobs.find(
    (job) => job.kind === "orthomosaic" && ["queued", "running"].includes(job.status),
  );
  const lastJob = jobs.find((job) => job.kind === "orthomosaic");
  const completed = project?.status === "completed";

  async function process() {
    setBusy(true);
    setError(null);
    try {
      await api.startProcessing(projectId, {
        engine: "auto",
        quality: "medium",
        fast_orthophoto: true,
        multispectral: (project?.summary.bands?.length ?? 0) > 1,
      });
      await load();
      setTab("processamento");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (!project) {
    return <p className="muted">{error ?? "Carregando projeto…"}</p>;
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{project.name}</h1>
          <p className="sub">
            {project.source_path} · {formatNumber(project.summary.valid_images)} imagens em{" "}
            {formatNumber(project.summary.folders)} pastas · criado em{" "}
            {formatDate(project.created_at)}
          </p>
        </div>
        <div className="row">
          <span className={`badge ${completed ? "ok" : activeJob ? "run" : ""}`}>
            {STATUS_LABEL[project.status]}
          </span>
          {!activeJob && (
            <button className="btn primary" disabled={busy} onClick={process}>
              {completed ? "Reprocessar voo" : "Processar voo completo"}
            </button>
          )}
        </div>
      </div>

      {error && <div className="alert err" style={{ marginBottom: 14 }}>{error}</div>}

      <div className="tabs">
        {([
          ["dataset", "Dataset"],
          ["processamento", "Processamento"],
          ["mapa", "Mapa"],
          ["exportacao", "Exportação"],
        ] as [Tab, string][]).map(([key, label]) => (
          <button
            key={key}
            className={tab === key ? "active" : ""}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "dataset" && (
        <div className="grid" style={{ gridTemplateColumns: "1.6fr 1fr", alignItems: "start" }}>
          <DatasetSummaryCard summary={project.summary} />
          <div className="card">
            <h3>Motores disponíveis</h3>
            {engines.map((engine) => (
              <div className="kv" key={engine.name}>
                <span>
                  <strong style={{ color: "var(--text)" }}>{engine.name}</strong>
                  <br />
                  <span style={{ fontSize: 12 }}>{engine.description}</span>
                </span>
                <span className={`badge ${engine.available ? "ok" : "warn"}`}>
                  {engine.available ? engine.precision : "indisponível"}
                </span>
              </div>
            ))}
            <h3 style={{ marginTop: 16 }}>Histórico</h3>
            {jobs.length ? (
              jobs.slice(0, 6).map((job) => (
                <div className="kv" key={job.id}>
                  <span>{job.kind === "scan" ? "Varredura" : `Ortomosaico (${job.engine})`}</span>
                  <span className="muted">{STATUS_LABEL[job.status]}</span>
                </div>
              ))
            ) : (
              <p className="muted">Nenhum job executado.</p>
            )}
          </div>
        </div>
      )}

      {tab === "processamento" && (
        activeJob || lastJob ? (
          <ProcessingPanel job={(activeJob ?? lastJob)!} onFinish={load} />
        ) : (
          <div className="card">
            <p className="muted">
              Nenhum processamento iniciado. Use “Processar voo completo” para gerar o
              ortomosaico de todas as imagens do voo.
            </p>
          </div>
        )
      )}

      {tab === "mapa" && (
        <MapView
          projectId={projectId}
          summary={project.summary}
          hasOrthomosaic={completed}
        />
      )}

      {tab === "exportacao" && (
        completed ? (
          <ExportPanel projectId={projectId} />
        ) : (
          <div className="card">
            <p className="muted">A exportação fica disponível quando o ortomosaico é gerado.</p>
          </div>
        )
      )}
    </>
  );
}
