"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { DatasetSummaryCard } from "@/components/DatasetSummary";
import { FolderPicker } from "@/components/FolderPicker";
import {
  api,
  subscribeJob,
  uploadFolder,
  type DatasetSummary,
  type Engine,
  type Quality,
} from "@/lib/api";
import { formatNumber } from "@/lib/format";

type Step = "projeto" | "pastas" | "varredura" | "revisao";

export default function NewProjectPage() {
  const router = useRouter();
  const [step, setStep] = useState<Step>("projeto");
  const [name, setName] = useState("");
  const [quality, setQuality] = useState("alta");
  const [qualities, setQualities] = useState<Quality[]>([]);
  const [engines, setEngines] = useState<Engine[]>([]);
  const [mode, setMode] = useState<"server" | "upload">("server");
  const [folders, setFolders] = useState<string[]>([]);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [scanProgress, setScanProgress] = useState({ label: "", percent: 0 });
  const [uploadProgress, setUploadProgress] = useState<{ sent: number; total: number } | null>(null);
  const [summary, setSummary] = useState<DatasetSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api
      .systemInfo()
      .then((info) => {
        setQualities(info.qualities);
        setEngines(info.engines);
      })
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    if (fileInput.current) {
      fileInput.current.setAttribute("webkitdirectory", "");
      fileInput.current.setAttribute("directory", "");
    }
  }, [mode, step]);

  async function ensureProject(): Promise<string> {
    if (projectId) return projectId;
    const project = await api.createProject({ name, quality });
    setProjectId(project.id);
    return project.id;
  }

  function watchScan(jobId: string, id: string) {
    setStep("varredura");
    const unsubscribe = subscribeJob(jobId, (event) => {
      setScanProgress({ label: event.stage_label, percent: event.progress });
      if (event.status === "succeeded") {
        unsubscribe();
        api.summary(id).then((data) => {
          setSummary(data);
          setStep("revisao");
        });
      }
      if (event.status === "failed") {
        unsubscribe();
        setError(event.error ?? "falha ao ler as pastas");
        setStep("pastas");
      }
    });
  }

  async function scanFolders() {
    setError(null);
    setBusy(true);
    try {
      const id = await ensureProject();
      const scan = await api.scan(id, folders);
      watchScan(scan.job_id, id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function startUpload(files: FileList | null) {
    if (!files?.length) return;
    setError(null);
    setBusy(true);
    try {
      const id = await ensureProject();
      const images = Array.from(files).filter((file) => /\.(jpe?g|tiff?|png)$/i.test(file.name));
      setUploadProgress({ sent: 0, total: images.length });
      const result = await uploadFolder(id, images, (sent, total) =>
        setUploadProgress({ sent, total }),
      );
      watchScan(result.job_id, id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function process() {
    if (!projectId) return;
    setBusy(true);
    try {
      await api.startProcessing(projectId, { engine: "auto", quality });
      router.push(`/project?id=${projectId}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  const engineEmUso = engines.find((item) => item.available);

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Novo processamento</h1>
          <p className="sub">Fotos do drone, um processamento, um ortomosaico</p>
        </div>
      </div>

      {error && <div className="alert err" style={{ marginBottom: 16 }}>{error}</div>}

      {step === "projeto" && (
        <div className="card" style={{ maxWidth: 720 }}>
          <h2>Projeto</h2>
          <div className="field">
            <label>Nome do projeto</label>
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Voo 20-08"
              autoFocus
            />
          </div>

          <h3 style={{ marginTop: 18 }}>Qualidade do processamento</h3>
          <div className="grid cols-3" style={{ marginBottom: 16 }}>
            {qualities.map((option) => (
              <button
                key={option.value}
                type="button"
                className="card"
                onClick={() => setQuality(option.value)}
                style={{
                  textAlign: "left",
                  cursor: "pointer",
                  borderColor: quality === option.value ? "var(--accent)" : "var(--border)",
                  background: quality === option.value ? "var(--panel-2)" : "var(--panel)",
                  color: "var(--text)",
                }}
              >
                <div style={{ fontWeight: 650, marginBottom: 4 }}>{option.label}</div>
                <div className="muted" style={{ fontSize: 12 }}>{option.summary}</div>
              </button>
            ))}
          </div>

          <button
            className="btn primary"
            disabled={!name.trim()}
            onClick={() => setStep("pastas")}
          >
            Continuar
          </button>
        </div>
      )}

      {step === "pastas" && (
        <div className="card" style={{ maxWidth: 820 }}>
          <h2>Pastas de fotos</h2>
          <p className="sub" style={{ marginBottom: 14 }}>
            Escolha uma ou várias pastas. Subpastas são percorridas automaticamente e tudo
            vira um único processamento.
          </p>

          <div className="row" style={{ marginBottom: 16 }}>
            <button
              className={`btn ${mode === "server" ? "primary" : ""}`}
              onClick={() => setMode("server")}
            >
              Pastas no servidor
            </button>
            <button
              className={`btn ${mode === "upload" ? "primary" : ""}`}
              onClick={() => setMode("upload")}
            >
              Enviar pasta do computador
            </button>
          </div>

          {mode === "server" ? (
            <>
              <FolderPicker selected={folders} onChange={setFolders} />
              <button
                className="btn primary"
                style={{ marginTop: 16 }}
                disabled={busy || !folders.length}
                onClick={scanFolders}
              >
                Ler {folders.length > 1 ? `as ${folders.length} pastas` : "a pasta"}
              </button>
            </>
          ) : (
            <>
              <div className="field">
                <label>Selecione a pasta com as fotos</label>
                <input ref={fileInput} type="file" multiple
                       onChange={(event) => startUpload(event.target.files)} />
              </div>
              {uploadProgress && (
                <>
                  <div className="progress-track">
                    <div
                      className="progress-fill"
                      style={{ width: `${(uploadProgress.sent / uploadProgress.total) * 100}%` }}
                    />
                  </div>
                  <p className="muted" style={{ marginTop: 8 }}>
                    Enviando {formatNumber(uploadProgress.sent)} de{" "}
                    {formatNumber(uploadProgress.total)} fotos
                  </p>
                </>
              )}
              <p className="muted" style={{ fontSize: 12 }}>
                Para milhares de fotos, use as pastas no servidor: o envio pelo navegador é
                limitado pela rede.
              </p>
            </>
          )}
        </div>
      )}

      {step === "varredura" && (
        <div className="card" style={{ maxWidth: 720 }}>
          <h2>Lendo as fotos</h2>
          <div className="progress-track">
            <div className="progress-fill" style={{ width: `${scanProgress.percent}%` }} />
          </div>
          <p className="muted" style={{ marginTop: 10 }}>{scanProgress.label}</p>
        </div>
      )}

      {step === "revisao" && summary && (
        <div className="grid" style={{ gridTemplateColumns: "1.6fr 1fr", alignItems: "start" }}>
          <DatasetSummaryCard summary={summary} />
          <div className="card">
            <h2>Pronto para processar</h2>
            <div className="kv"><span>Fotos</span>
              <span>{formatNumber(summary.valid_images)}</span></div>
            <div className="kv"><span>Pastas</span>
              <span>{formatNumber(summary.folders)}</span></div>
            <div className="kv"><span>Qualidade</span>
              <span>{qualities.find((q) => q.value === quality)?.label ?? quality}</span></div>
            <div className="kv"><span>Motor</span>
              <span>{engineEmUso ? engineEmUso.name : "indisponível"}</span></div>

            <button
              className="btn primary"
              style={{ width: "100%", justifyContent: "center", marginTop: 16 }}
              disabled={busy || !summary.valid_images}
              onClick={process}
            >
              Processar todas as fotos
            </button>
            <p className="muted" style={{ fontSize: 12, marginTop: 10 }}>
              Um único ortomosaico será gerado com todas as fotos encontradas.
            </p>
          </div>
        </div>
      )}
    </>
  );
}
