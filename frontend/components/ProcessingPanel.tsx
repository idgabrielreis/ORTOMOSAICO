"use client";

import { useEffect, useState } from "react";

import { api, subscribeJob, type Job, type JobEvent } from "@/lib/api";
import { STATUS_LABEL, formatDuration, formatNumber } from "@/lib/format";
import { STAGES } from "@/lib/stages";

export function ProcessingPanel({ job, onFinish }: { job: Job; onFinish: () => void }) {
  const [event, setEvent] = useState<JobEvent | null>(null);
  const [log, setLog] = useState("");

  useEffect(() => {
    const unsubscribe = subscribeJob(job.id, (incoming) => {
      setEvent(incoming);
      if (["succeeded", "failed", "canceled"].includes(incoming.status)) onFinish();
    });
    const timer = setInterval(() => {
      api.jobLog(job.id).then(setLog).catch(() => undefined);
    }, 3000);
    api.jobLog(job.id).then(setLog).catch(() => undefined);
    return () => {
      unsubscribe();
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.id]);

  const current = event ?? {
    ...job,
    elapsed_seconds: null,
    eta_seconds: null,
    resources: { cpu_percent: null, memory_percent: null, memory_used_gb: null,
                 memory_total_gb: null, gpus: [] },
  } as JobEvent;
  const resources = current.resources;
  const running = ["queued", "running"].includes(current.status);

  return (
    <div className="grid" style={{ gridTemplateColumns: "1.4fr 1fr", alignItems: "start" }}>
      <div className="card">
        <div className="row" style={{ marginBottom: 12 }}>
          <h2 style={{ margin: 0 }}>{current.stage_label || "Preparando"}</h2>
          <span className="spacer" />
          <span className={`badge ${current.status === "succeeded" ? "ok"
            : current.status === "failed" ? "err" : "run"}`}>
            {STATUS_LABEL[current.status]}
          </span>
        </div>

        <div className="progress-track" style={{ marginBottom: 8 }}>
          <div className="progress-fill" style={{ width: `${current.progress}%` }} />
        </div>
        <div className="row muted" style={{ fontSize: 12, marginBottom: 16 }}>
          <span>{current.progress.toFixed(1)}%</span>
          <span className="spacer" />
          <span>
            {formatNumber(current.images_done)} / {formatNumber(current.images_total)} imagens
          </span>
        </div>

        <div className="steps">
          {STAGES.map(([number, label]) => (
            <div
              key={number}
              className={`step ${current.stage > number ? "done"
                : current.stage === number ? "active" : ""}`}
            >
              <div className="dot">{current.stage > number ? "✓" : number}</div>
              <span>{label}</span>
            </div>
          ))}
        </div>

        {current.error && <div className="alert err" style={{ marginTop: 14 }}>{current.error}</div>}
        {current.warnings?.map((warning) => (
          <div className="alert warn" style={{ marginTop: 10 }} key={warning}>{warning}</div>
        ))}

        {running && (
          <button
            className="btn danger"
            style={{ marginTop: 14 }}
            onClick={() => api.cancelJob(job.id)}
          >
            Cancelar processamento
          </button>
        )}
      </div>

      <div className="card">
        <h3>Execução</h3>
        <div className="kv"><span>Motor</span><span>{job.engine}</span></div>
        <div className="kv"><span>Tempo decorrido</span>
          <span>{formatDuration(current.elapsed_seconds)}</span></div>
        <div className="kv"><span>Tempo restante (estimado)</span>
          <span>{formatDuration(current.eta_seconds)}</span></div>
        <div className="kv"><span>CPU</span>
          <span>{resources.cpu_percent === null ? "—" : `${resources.cpu_percent.toFixed(0)}%`}</span>
        </div>
        <div className="kv"><span>RAM</span>
          <span>
            {resources.memory_used_gb === null
              ? "—"
              : `${resources.memory_used_gb} / ${resources.memory_total_gb} GB`}
          </span>
        </div>
        <div className="kv"><span>GPU</span>
          <span>
            {resources.gpus?.length
              ? `${resources.gpus[0].name} · ${resources.gpus[0].utilization_percent}%`
              : "não disponível"}
          </span>
        </div>

        <h3 style={{ marginTop: 16 }}>Log</h3>
        <div className="log">{log || "sem registros ainda"}</div>
      </div>
    </div>
  );
}
