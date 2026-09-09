const numberFormat = new Intl.NumberFormat("pt-BR");
const decimalFormat = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 1 });

export const formatNumber = (value: number | null | undefined) =>
  value === null || value === undefined ? "—" : numberFormat.format(value);

export const formatDecimal = (value: number | null | undefined, suffix = "") =>
  value === null || value === undefined ? "—" : `${decimalFormat.format(value)}${suffix}`;

export function formatDuration(seconds: number | null | undefined): string {
  if (!seconds && seconds !== 0) return "—";
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = total % 60;
  if (hours) return `${hours}h ${String(minutes).padStart(2, "0")}min`;
  if (minutes) return `${minutes}min ${String(rest).padStart(2, "0")}s`;
  return `${rest}s`;
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  return new Date(value).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" });
}

export const STATUS_LABEL: Record<string, string> = {
  created: "Criado",
  scanning: "Varrendo pastas",
  ready: "Pronto para processar",
  processing: "Processando",
  completed: "Concluído",
  failed: "Falhou",
  queued: "Na fila",
  running: "Em execução",
  succeeded: "Concluído",
  canceled: "Cancelado",
};
