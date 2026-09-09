"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";

/** Navegador de pastas do servidor: escolhe a raiz do voo. */
export function FolderPicker({
  value,
  onChange,
}: {
  value: string;
  onChange: (path: string) => void;
}) {
  const [current, setCurrent] = useState<string>(value);
  const [parent, setParent] = useState<string>("");
  const [entries, setEntries] = useState<{ name: string; path: string }[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = (path?: string) => {
    api
      .browse(path)
      .then((data) => {
        setCurrent(data.path);
        setParent(data.parent);
        setEntries(data.entries);
        setError(null);
        onChange(data.path);
      })
      .catch((e) => setError(e.message));
  };

  useEffect(() => {
    load(value || undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div>
      <div className="field">
        <label>Pasta raiz do voo (no servidor)</label>
        <div className="row">
          <input
            value={current}
            onChange={(event) => setCurrent(event.target.value)}
            placeholder="/dados/VOO_FAZENDA_X"
          />
          <button type="button" className="btn" onClick={() => load(current)}>Abrir</button>
        </div>
      </div>
      {error && <div className="alert err" style={{ marginBottom: 10 }}>{error}</div>}
      <div className="picker-list">
        <button type="button" onClick={() => load(parent)} className="muted">.. (subir um nível)</button>
        {entries.map((entry) => (
          <button key={entry.path} type="button" onClick={() => load(entry.path)}>
            {entry.name}
          </button>
        ))}
        {!entries.length && <button type="button" className="muted" disabled>
          Sem subpastas aqui</button>}
      </div>
      <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
        Selecione a pasta do voo, não cada subpasta. Tudo abaixo dela vira um dataset só.
      </p>
    </div>
  );
}
