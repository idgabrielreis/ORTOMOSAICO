"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { api, type DashboardStats } from "@/lib/api";
import { STATUS_LABEL, formatDate, formatDecimal, formatNumber } from "@/lib/format";

export default function DashboardPage() {
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = () => api.stats().then(setStats).catch((e) => setError(e.message));
    load();
    const timer = setInterval(load, 5000);
    return () => clearInterval(timer);
  }, []);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Dashboard</h1>
          <p className="sub">Voos processados e em andamento</p>
        </div>
        <Link href="/projects/new" className="btn primary">Novo processamento</Link>
      </div>

      {error && <div className="alert err">{error}</div>}

      <div className="grid cols-4" style={{ marginBottom: 18 }}>
        <div className="card stat">
          <div className="value">{formatNumber(stats?.projects_total)}</div>
          <div className="label">Projetos</div>
        </div>
        <div className="card stat">
          <div className="value">{formatNumber(stats?.projects_processing)}</div>
          <div className="label">Em processamento</div>
        </div>
        <div className="card stat">
          <div className="value">{formatNumber(stats?.images_total)}</div>
          <div className="label">Imagens processadas</div>
        </div>
        <div className="card stat">
          <div className="value">{formatDecimal(stats?.area_ha_total, " ha")}</div>
          <div className="label">Área processada</div>
        </div>
      </div>

      {stats?.active_jobs?.length ? (
        <div className="card" style={{ marginBottom: 18 }}>
          <h3>Em execução</h3>
          {stats.active_jobs.map((job) => (
            <Link key={job.id} href={`/projects/${job.project_id}`}>
              <div style={{ margin: "10px 0" }}>
                <div className="row" style={{ marginBottom: 6 }}>
                  <span>{job.stage_label || "Preparando"}</span>
                  <span className="spacer" />
                  <span className="muted">{job.progress.toFixed(0)}%</span>
                </div>
                <div className="progress-track">
                  <div className="progress-fill" style={{ width: `${job.progress}%` }} />
                </div>
              </div>
            </Link>
          ))}
        </div>
      ) : null}

      <div className="card">
        <h3>Projetos recentes</h3>
        <table>
          <thead>
            <tr>
              <th>Projeto</th>
              <th>Status</th>
              <th>Imagens</th>
              <th>Área</th>
              <th>Atualizado</th>
            </tr>
          </thead>
          <tbody>
            {stats?.recent_projects?.length ? (
              stats.recent_projects.map((project) => (
                <tr key={project.id}>
                  <td><Link href={`/projects/${project.id}`}>{project.name}</Link></td>
                  <td>
                    <span className={`badge ${project.status === "completed" ? "ok"
                      : project.status === "processing" ? "run"
                      : project.status === "failed" ? "err" : ""}`}>
                      {STATUS_LABEL[project.status] ?? project.status}
                    </span>
                  </td>
                  <td>{formatNumber(project.images)}</td>
                  <td>{formatDecimal(project.area_ha, " ha")}</td>
                  <td className="muted">{formatDate(project.updated_at)}</td>
                </tr>
              ))
            ) : (
              <tr><td colSpan={5} className="muted">
                Nenhum projeto ainda. Crie um novo processamento para começar.
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}
