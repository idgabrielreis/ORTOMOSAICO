# Ortomosaico

Aplicativo web para processar imagens aéreas de drone e gerar ortomosaicos
georreferenciados, com uma regra de domínio no centro do produto:

> **Uma pasta raiz = um voo = um dataset = um ortomosaico.**
> Subpastas (`CAMERA_01`, `PARTE_03`, cartões de memória) são apenas
> organização física. O usuário aponta para a pasta do voo e o sistema encontra
> e processa todas as imagens abaixo dela como uma única missão.

Isso elimina o problema de plataformas que exigem processar uma pasta por vez e
acabam produzindo vários mosaicos separados do mesmo voo.

## O que já funciona

- Criação de projeto (voo) e ingestão por pasta do servidor ou upload da árvore
  de pastas pelo navegador.
- Descoberta recursiva com validação, deduplicação e classificação de arquivos
  corrompidos, sem interromper a varredura.
- Leitura de EXIF e do bloco XMP da DJI: GPS, altitude relativa, ângulos de
  gimbal, RTK, banda espectral, câmera, focal e sensor.
- Tela “Dataset encontrado” com contagens, altitude média, área em hectares,
  GSD, câmera e sistema de coordenadas antes de processar.
- Processamento do voo inteiro em 8 etapas, com progresso ao vivo, tempo
  decorrido e estimado, uso de CPU/RAM/GPU, avisos, logs e cancelamento.
- Dois motores fotogramétricos atrás da mesma interface:
  - **odm** — OpenDroneMap (Docker ou NodeODM): SfM, bundle adjustment, nuvem de
    pontos, DSM/DTM, ortorretificação e blending;
  - **direct** — georreferenciamento direto por GPS, geometria de câmera e
    gimbal; sem SfM, com terreno plano, para pré-visualização rápida e para
    ambientes sem Docker. O produto é um GeoTIFF real, não uma simulação.
- Mapa estilo GIS (MapLibre) com ortomosaico em tiles XYZ, posição das câmeras,
  footprints, limite do projeto, alternância de camadas, leitura de coordenadas
  e medição de distância e área.
- Exportação em GeoTIFF, PNG, KMZ, World File e relatório JSON.

A arquitetura, as decisões técnicas e o fluxo completo dos dados estão em
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Como rodar

### Desenvolvimento (sem Docker)

```bash
# backend
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 8000

# frontend, em outro terminal
cd frontend
npm install
npm run dev            # http://localhost:3000
```

Sem Docker, apenas o motor `direct` fica disponível — a interface mostra isso
explicitamente na tela de dataset.

### Ambiente completo

```bash
docker compose up -d   # api, worker, redis, PostGIS, NodeODM e web
```

Monte o disco das imagens em `./data` e os produtos sairão em `./storage`.

### Dados de exemplo

```bash
cd backend
.venv/bin/python tools/generate_sample_flight.py --out /tmp/VOO_FAZENDA_X --rows 6 --cols 8
```

Gera um voo sintético em quatro subpastas, com EXIF e XMP da DJI, mais arquivos
corrompidos e duplicados para exercitar a recuperação de erros. Aponte o
projeto para `/tmp/VOO_FAZENDA_X` e processe.

### Testes

```bash
cd backend && .venv/bin/python -m pytest
```

## Configuração

Todas as variáveis usam o prefixo `ORTO_`:

| Variável | Padrão | Função |
|---|---|---|
| `ORTO_STORAGE_ROOT` | `./storage` | Projetos, produtos, logs e miniaturas |
| `ORTO_DATABASE_URL` | SQLite em `STORAGE_ROOT` | Use `postgresql+psycopg://…` para PostGIS |
| `ORTO_QUEUE_BACKEND` | `local` | `celery` para worker separado |
| `ORTO_NODEODM_URL` | vazio | Nó NodeODM; sem ele o ODM roda por `docker run` |
| `ORTO_ODM_DOCKER_IMAGE` | `opendronemap/odm:latest` | Imagem do motor |
| `ORTO_ODM_USE_GPU` | `false` | Usa a imagem com CUDA |
| `ORTO_SCAN_ALLOWED_ROOTS` | vazio | Restringe onde a varredura pode ler (`:` separa) |
| `ORTO_MIN_VALID_RATIO` | `0.6` | Fração mínima de imagens válidas para processar |

## Próximos passos previstos na arquitetura

GCP, RTK/PPK, índices de vegetação (NDVI, NDRE, GNDVI) para as bandas do Mavic
3M, autenticação multiusuário e armazenamento em nuvem. O modelo de dados e as
interfaces já reservam espaço para todos eles.
