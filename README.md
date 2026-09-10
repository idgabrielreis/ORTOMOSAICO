# Ortomosaico

Aplicativo web que transforma as fotografias de um voo de drone em um
ortomosaico georreferenciado, com uma regra no centro do produto:

> **As pastas escolhidas = um processamento = um ortomosaico.**
> Uma pasta ou dez, com quantas subpastas houver: todas as fotos entram no
> mesmo processamento fotogramétrico e saem como um único ortomosaico.

Isso elimina o problema de plataformas que exigem processar uma pasta por vez e
acabam produzindo vários mosaicos separados do mesmo voo.

O fluxo é:

```
criar projeto -> nome -> qualidade -> selecionar pastas -> processar
   -> ortomosaico -> visualizar -> exportar GeoTIFF RGB
```

O ortomosaico é calculado a partir das fotografias (detecção de
características, correspondências, SfM, bundle adjustment, superfície e
ortorretificação). Não é uma colagem de imagens no mapa.

## O que já funciona

- Projeto com nome e qualidade (alta, média, baixa) escolhidas na criação.
- Seleção de uma ou várias pastas, no servidor ou por upload da árvore de
  pastas pelo navegador; subpastas são percorridas automaticamente.
- Descoberta recursiva com validação, deduplicação e classificação de arquivos
  corrompidos, sem interromper a varredura.
- Leitura de EXIF/XMP: câmera, focal, sensor, posição, altitude e horário.
- Resumo antes de processar: fotos encontradas, válidas, pastas lidas,
  resolução, câmera, GSD estimado e sistema de coordenadas.
- Processamento em 8 etapas com progresso ao vivo, tempo decorrido e estimado,
  uso de CPU/RAM/GPU, avisos, logs e cancelamento.
- Três motores atrás da mesma interface:
  - **odm** — OpenDroneMap (Docker ou NodeODM): SfM, bundle adjustment, nuvem
    densa, DSM e ortorretificação. É o motor de maior precisão.
  - **sfm** — COLMAP via pycolmap, em CPU e sem Docker: SIFT, pares escolhidos
    pela posição, SfM incremental, bundle adjustment, superfície a partir da
    nuvem esparsa e ortorretificação por projeção inversa.
  - **direct** — projeção direta pelos metadados, sem SfM, para prévia rápida.
- Ortomosaico gravado em blocos, sem teto de resolução: na qualidade alta a
  saída fica no GSD nativo das fotos, e um mosaico de gigapixels não precisa
  caber na memória.
- Mapa (MapLibre) com ortomosaico em tiles XYZ, posição das câmeras, limite do
  projeto, camadas alternáveis, coordenadas e medição de distância e área.
- Exportação do ortomosaico RGB em GeoTIFF (`Orto_<projeto>.rgb.tif`), além de
  PNG e relatório JSON.

Fora do escopo desta etapa, e adiado de propósito: MDE/MDS como produto de
primeira classe, GCP, RTK/PPK, KML de planejamento e índices de vegetação.

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

Sem Docker o motor `odm` fica indisponível e o `sfm` assume: a interface mostra
qual motor está em uso e por quê.

### Ambiente completo

```bash
docker compose up -d   # api, worker, redis, PostGIS, NodeODM e web
```

Depois abra <http://localhost:3000>.

As fotos ficam disponíveis para o app pelo volume `./data`, montado somente
para leitura, e os produtos saem em `./storage`. Para apontar para outro disco,
troque a linha do volume nos serviços `api` e `worker` do `docker-compose.yml`:

```yaml
    volumes:
      - ./storage:/storage
      - "I:/FALTA PROCESSAR:/data:ro"    # Windows
      - /mnt/dados/voos:/data:ro          # Linux
```

Dentro do app, as pastas aparecem sob `/data`.

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
