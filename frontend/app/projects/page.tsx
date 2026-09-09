"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { api, type Project } from "@/lib/api";
import { STATUS_LABEL, formatDate, formatDecimal, formatNumber } from "@/lib/format";

export default function ProjectsPage() {
  const [projects, setProjects] = useState<Project[]>([]);

  useEffect(() => {
    api.projects().then(setProjects).catch(() => setProjects([]));
  }, []);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Projetos</h1>
          <p className="sub">Cada projeto é um voo completo</p>
        </div>
        <Link href="/projects/new" className="btn primary">Novo processamento</Link>
      </div>

      <div className="card">
        <table>
          <thead>
            <tr>
              <th>Nome</th>
              <th>Status</th>
              <th>Pastas</th>
              <th>Imagens</th>
              <th>Área</th>
              <th>Criado</th>
            </tr>
          </thead>
          <tbody>
            {projects.map((project) => (
              <tr key={project.id}>
                <td>
                  <Link href={`/projects/${project.id}`}>{project.name}</Link>
                  <div className="muted" style={{ fontSize: 12 }}>{project.source_path}</div>
                </td>
                <td><span className="badge">{STATUS_LABEL[project.status]}</span></td>
                <td>{formatNumber(project.summary?.folders)}</td>
                <td>{formatNumber(project.summary?.valid_images)}</td>
                <td>{formatDecimal(project.summary?.area_ha, " ha")}</td>
                <td className="muted">{formatDate(project.created_at)}</td>
              </tr>
            ))}
            {!projects.length && (
              <tr><td colSpan={6} className="muted">Nenhum projeto cadastrado.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}
