"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { DatasetSummaryCard } from "@/components/DatasetSummary";
import { FolderPicker } from "@/components/FolderPicker";
import { api, subscribeJob, uploadFolder, type DatasetSummary, type Engine } from "@/lib/api";
import { formatNumber } from "@/lib/format";

type Step = "identificacao" | "origem" | "varredura" | "revisao";

export default function NewProjectPage() {
  const router = useRouter();
  const [step, setStep] = useState<Step>("identificacao");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [epsg, setEpsg] = useState<string>("");
  const [gsd, setGsd] = useState<string>("");
  const [mode, setMode] = useState<"server" | "upload">("server");
  const [serverPath, setServerPath] = useState("");
  const [projectId, setProjectId] = useState<string | null>(null);
  const [scanProgress, setScanProgress] = useState<{ label: string; percent: number }>({
    label: "", percent: 0,
  });
  const [uploadProgress, setUploadProgress] = useState<{ sent: number; total: number } | null>(null);
  const [summary, setSummary] = useState<DatasetSummary | null>(null);
  const [engines, setEngines] = useState<Engine[]>([]);
  const [engine, setEngine] = useState("auto");
  const [quality, setQuality] = useState("medium");
  const [fastOrtho, setFastOrtho] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    api.systemInfo().then((info) => setEngines(info.engines)).catch(() => undefined);
  }, []);

  useEffect(() => {
    // O input de pasta só existe com atributos não padronizados no TSX.
    if (fileInput.current) {
      fileInput.current.setAttribute("webkitdirectory", "");
      fileInput.current.setAttribute("directory", "");
    }
  }, [mode]);

  async function ensureProject(): Promise<string> {
    if (projectId) return projectId;
    const project = await api.createProject({
      name,
      description,
      output_epsg: epsg ? Number(epsg) : null,
      target_gsd_cm: gsd ? Number(gsd) : null,
    });
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
        setError(event.error ?? "falha na varredura");
        setStep("origem");
      }
    });
  }

  async function startServerScan() {
    setError(null);
    setBusy(true);
    try {
      const id = await ensureProject();
      const scan = await api.scan(id, serverPath);
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
      const images = Array.from(files).filter((file) =>
        /\.(jpe?g|tiff?|png)$/i.test(file.name),
      );
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

  async function startProcessing() {
    if (!projectId) return;
    setBusy(true);
    try {
      await api.startProcessing(projectId, {
        engine,
        quality,
        fast_orthophoto: fastOrtho,
        multispectral: (summary?.bands?.length ?? 0) > 1,
      });
      router.push(`/projects/${projectId}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Novo processamento</h1>
          <p className="sub">Um voo, quantas subpastas forem necessárias, um ortomosaico</p>
        </div>
      </div>

      {error && <div className="alert err" style={{ marginBottom: 16 }}>{error}</div>}

      {step === "identificacao" && (
        <div className="card" style={{ maxWidth: 640 }}>
          <h2>Identificação do voo</h2>
          <div className="field">
            <label>Nome do projeto</label>
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Fazenda Santa Rita — voo 09/09/2026"
            />
          </div>
          <div className="field">
            <label>Descrição (opcional)</label>
            <input
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="Talhões 1 a 4, Mavic 3M, 120 m"
            />
          </div>
          <div className="grid cols-2">
            <div className="field">
              <label>EPSG de saída (vazio = UTM do voo)</label>
              <input value={epsg} onChange={(e) => setEpsg(e.target.value)} placeholder="31983" />
            </div>
            <div className="field">
              <label>GSD alvo em cm/px (opcional)</label>
              <input value={gsd} onChange={(e) => setGsd(e.target.value)} placeholder="5" />
            </div>
          </div>
          <button
            className="btn primary"
            disabled={!name.trim()}
            onClick={() => setStep("origem")}
          >
            Continuar
          </button>
        </div>
      )}

      {step === "origem" && (
        <div className="card" style={{ maxWidth: 720 }}>
          <h2>Imagens do voo</h2>
          <div className="row" style={{ marginBottom: 16 }}>
            <button
              className={`btn ${mode === "server" ? "primary" : ""}`}
              onClick={() => setMode("server")}
            >
              Pasta no servidor
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
              <FolderPicker value={serverPath} onChange={setServerPath} />
              <button
                className="btn primary"
                style={{ marginTop: 14 }}
                disabled={busy || !serverPath}
                onClick={startServerScan}
              >
                Varrer pasta do voo
              </button>
            </>
          ) : (
            <>
              <div className="field">
                <label>Selecione a pasta raiz do voo (subpastas incluídas)</label>
                <input
                  ref={fileInput}
                  type="file"
                  multiple
                  onChange={(event) => startUpload(event.target.files)}
                />
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
                    {formatNumber(uploadProgress.total)} imagens
                  </p>
                </>
              )}
              <p className="muted" style={{ fontSize: 12 }}>
                Para voos com milhares de imagens, prefira a pasta no servidor: o envio pelo
                navegador é limitado pela rede.
              </p>
            </>
          )}
        </div>
      )}

      {step === "varredura" && (
        <div className="card" style={{ maxWidth: 720 }}>
          <h2>Procurando imagens</h2>
          <p className="sub" style={{ marginBottom: 14 }}>
            Percorrendo todas as subpastas e lendo os metadados EXIF/XMP.
          </p>
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
            <h2>Processamento</h2>
            <div className="field">
              <label>Motor fotogramétrico</label>
              <select value={engine} onChange={(event) => setEngine(event.target.value)}>
                <option value="auto">Automático (usa o de maior precisão disponível)</option>
                {engines.map((item) => (
                  <option key={item.name} value={item.name} disabled={!item.available}>
                    {item.name} — {item.precision}
                    {item.available ? "" : ` (indisponível: ${item.reason})`}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>Qualidade</label>
              <select value={quality} onChange={(event) => setQuality(event.target.value)}>
                <option value="lowest">Mais rápida</option>
                <option value="low">Baixa</option>
                <option value="medium">Média</option>
                <option value="high">Alta</option>
                <option value="ultra">Máxima</option>
              </select>
            </div>
            <div className="layer-toggle">
              <input
                id="fast"
                type="checkbox"
                checked={fastOrtho}
                onChange={(event) => setFastOrtho(event.target.checked)}
              />
              <label htmlFor="fast" style={{ margin: 0 }}>
                Ortomosaico rápido (superfície 2.5D)
              </label>
            </div>
            <button
              className="btn primary"
              style={{ width: "100%", justifyContent: "center", marginTop: 14 }}
              disabled={busy || !summary.valid_images}
              onClick={startProcessing}
            >
              Processar voo completo
            </button>
            <p className="muted" style={{ fontSize: 12, marginTop: 10 }}>
              {formatNumber(summary.valid_images)} imagens de {formatNumber(summary.folders)}{" "}
              pastas serão processadas como uma única missão.
            </p>
          </div>
        </div>
      )}
    </>
  );
}
