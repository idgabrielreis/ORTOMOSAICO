"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";

/** Navegador de pastas do servidor com seleção múltipla.

Selecionar várias pastas não cria vários processamentos: todas entram na mesma
lista e formam um único ortomosaico. */
export function FolderPicker({
  selected,
  onChange,
}: {
  selected: string[];
  onChange: (paths: string[]) => void;
}) {
  const [current, setCurrent] = useState<string>("");
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
      })
      .catch((e) => setError(e.message));
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const add = (path: string) => {
    if (!selected.includes(path)) onChange([...selected, path]);
  };

  return (
    <div>
      <div className="field">
        <label>Pasta atual</label>
        <div className="row">
          <input
            value={current}
            onChange={(event) => setCurrent(event.target.value)}
            placeholder="/dados/voo_20-08"
          />
          <button type="button" className="btn" onClick={() => load(current)}>Abrir</button>
          <button type="button" className="btn primary" onClick={() => add(current)}>
            Adicionar esta pasta
          </button>
        </div>
      </div>

      {error && <div className="alert err" style={{ marginBottom: 10 }}>{error}</div>}

      <div className="picker-list">
        <button type="button" onClick={() => load(parent)} className="muted">
          .. (subir um nível)
        </button>
        {entries.map((entry) => (
          <button key={entry.path} type="button" onClick={() => load(entry.path)}>
            <span>{entry.name}</span>
            <span
              className="badge"
              style={{ float: "right" }}
              onClick={(event) => {
                event.stopPropagation();
                add(entry.path);
              }}
            >
              adicionar
            </span>
          </button>
        ))}
        {!entries.length && (
          <button type="button" className="muted" disabled>Sem subpastas aqui</button>
        )}
      </div>

      <div style={{ marginTop: 14 }}>
        <h3>Pastas selecionadas ({selected.length})</h3>
        {selected.length ? (
          selected.map((path) => (
            <div className="kv" key={path}>
              <span style={{ color: "var(--text)", wordBreak: "break-all" }}>{path}</span>
              <button
                type="button"
                className="btn danger"
                onClick={() => onChange(selected.filter((item) => item !== path))}
              >
                remover
              </button>
            </div>
          ))
        ) : (
          <p className="muted">
            Nenhuma pasta ainda. Adicione uma ou várias — todas as fotos entram no mesmo
            ortomosaico.
          </p>
        )}
      </div>
    </div>
  );
}
