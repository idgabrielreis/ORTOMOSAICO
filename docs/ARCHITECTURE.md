# Arquitetura — Ortomosaico (gerenciador de voo + motor fotogramétrico)

Documento de decisão técnica. Responde aos 10 pontos pedidos na especificação e
descreve o que a primeira versão implementa de fato.

## 1. Visão geral

O aplicativo **não** reimplementa um motor de fotogrametria. Ele é um
**gerenciador de voos, datasets e processamentos** construído sobre um motor
open-source consolidado. O diferencial do produto está na camada de dados:

```
Usuário  ->  Projeto (voo)  ->  Descoberta recursiva de imagens  ->  Dataset único
         ->  Motor fotogramétrico  ->  GeoTIFF georreferenciado  ->  Mapa / Export
```

Regra fundamental do domínio, aplicada em todo o código:

> **Pasta raiz selecionada = 1 voo = 1 dataset = 1 ortomosaico.**
> Subpastas são apenas organização física dos cartões de memória / partes do voo.

Nada no modelo de dados permite que uma subpasta vire um projeto. A entidade
`Project` tem N `Image`, e `Image` guarda `relative_path` apenas como metadado
informativo (para relatório e diagnóstico), nunca como chave de agrupamento.

## 2. Arquitetura recomendada

| Camada | Tecnologia | Justificativa técnica |
|---|---|---|
| Frontend | Next.js (App Router) + TypeScript + MapLibre GL | Next dá roteamento, streaming de UI e build único; MapLibre é WebGL, licença BSD, suporta raster XYZ e vetor no mesmo canvas, e aguenta ortomosaicos grandes por tiles (Leaflet é DOM/canvas e sofre com pan em raster pesado). |
| API | FastAPI + Pydantic v2 + Uvicorn | Tipagem estática ponta a ponta, OpenAPI automática para o cliente TS, e async nativo para streaming de progresso (SSE) e upload em chunks. |
| Persistência | SQLAlchemy 2.0; SQLite por padrão, PostgreSQL/PostGIS quando `DATABASE_URL` aponta para Postgres | SQLite deixa o protótipo rodar sem infraestrutura. PostGIS entra quando houver consulta espacial real (interseção de voos, footprints, mapas de talhão). O código usa GeoJSON em coluna JSON, portanto a migração para `geometry` é aditiva. |
| Qualidade | Presets alta / média / baixa em um único lugar (`processing/quality.py`) | A escolha do usuário é uma só e vale para todos os motores: controla o detalhe usado na detecção de características, quantos pares são comparados e o GSD da saída em relação ao nativo. |
| Fila | Abstração `TaskQueue` com dois back-ends: executor local (thread + processo filho) e Celery/Redis | Processamento fotogramétrico dura horas: precisa sair do processo da API. Celery/Redis é o padrão do ecossistema Python e permite escalar workers em máquinas com GPU. O executor local existe para o desenvolvedor rodar tudo com um comando. |
| Motor | OpenDroneMap (ODM), consumido via NodeODM (REST) ou `docker run` | Ver seção 3. |
| Raster | GDAL / rasterio / rio-tiler / pyproj / shapely | GDAL é a referência para GeoTIFF, COG, reprojeção e overviews. rio-tiler serve tiles XYZ direto do COG sem pré-gerar pirâmide em disco. |
| Metadados | Pillow + parser XMP próprio, com `exiftool` opcional | EXIF cobre GPS, câmera e tempo. Ângulos de gimbal, RTK e dados multiespectrais da DJI só existem no bloco XMP `drone-dji:` — daí o parser dedicado. |
| Armazenamento | Filesystem local em `STORAGE_ROOT`, acessado por uma interface `Storage` | Um único ponto de troca para S3/GCS depois. O motor precisa de caminho POSIX, então nuvem exige staging local de qualquer forma. |

Rejeitamos deliberadamente: banco em memória, processamento no navegador
(WASM), e qualquer biblioteca de "image stitching" de fotos panorâmicas —
elas assumem centro óptico único e não produzem produto georreferenciado.

## 3. Escolha do motor fotogramétrico

Avaliação das opções pedidas:

| Ferramenta | Avaliação |
|---|---|
| **OpenDroneMap / WebODM** | Pipeline completo e fechado: SfM (OpenSfM), MVS, DSM/DTM, ortorretificação, blending com seamline, saída em GeoTIFF/COG. Aceita GCP, RTK, e câmeras multiespectrais DJI (inclusive Mavic 3M, com alinhamento de bandas e correção por painel de refletância). Tem imagem Docker oficial e API REST (NodeODM). **Escolhido como motor principal.** |
| COLMAP | SfM/MVS excelente e mais preciso em cena livre, mas não entrega ortomosaico georreferenciado; precisaríamos escrever ortorretificação, DSM e blending. Fica como opção futura para o passo de SfM. |
| OpenSfM | É justamente o SfM interno do ODM. Usar direto significa reconstruir o resto do pipeline. |
| MicMac | Muito capaz e rigoroso, porém linha de comando complexa, documentação em francês/acadêmica, difícil de operacionalizar. |
| AliceVision / Meshroom | Forte em MVS e malha 3D, foco em VFX; ortomosaico geodésico não é o caso de uso central. |

**Decisão: OpenDroneMap.** Motivo curto: é o único que entrega o produto final
exigido (ortomosaico georreferenciado) de ponta a ponta, com licença permissiva
(AGPL para o WebODM, BSD/variadas para o ODM core — o app conversa por REST,
sem linkagem), suporte a multiespectral DJI e caminho claro para GCP e GPU
(`--feature-type sift` com CUDA nas imagens `opendronemap/odm:gpu`).

O motor é acessado por um **adaptador** (`processing/engines/odm_engine.py`)
atrás da interface `PhotogrammetryEngine`. Trocar ODM por COLMAP+GDAL depois
não toca a API nem o frontend.

### Motor `sfm` (COLMAP via pycolmap)

Fotogrametria real sem Docker, em CPU: SIFT, pares candidatos escolhidos pela
posição das fotos, SfM incremental com bundle adjustment, alinhamento do modelo
às coordenadas das câmeras por similaridade, superfície a partir da nuvem
esparsa e ortorretificação por projeção inversa sobre essa superfície.

Duas decisões importantes:

- **A intrínseca vem do EXIF e fica fixa.** Em voo nadir sobre terreno pouco
  acidentado, focal e profundidade da cena são quase indistinguíveis: deixar a
  autocalibração livre produzia um modelo coerente com escala errada (altura de
  voo de 720 m em vez de 120 m). Com a focal fixa, o resíduo das posições das
  câmeras caiu de 8,6 m para 0,15 m no voo de teste.
- **A saída é escrita em blocos.** O ortomosaico sai no GSD nativo na qualidade
  alta, o que passa facilmente de um gigapixel; cada bloco de 2048 px é
  projetado, composto e gravado isoladamente, com um cache pequeno de fotos
  decodificadas. O pico de memória depende do bloco, não do tamanho do voo.

Ele não faz reconstrução densa (MVS), então a superfície é mais grosseira que a
do ODM. Para o produto de máxima precisão o motor `odm` continua sendo a
escolha.

### Motor `direct` (projeção direta pelos metadados)

Existe um segundo motor real, não um mock: ele projeta cada imagem no plano do
terreno usando GPS, altitude relativa, distância focal, tamanho do sensor e os
ângulos de gimbal do XMP, e compõe o mosaico com feathering e correção de
exposição. É fotogrametria simplificada (assume terreno plano, sem bundle
adjustment), gera GeoTIFF georreferenciado de verdade e roda em segundos.
Serve para: pré-visualização do voo, validação do dataset, ambientes sem Docker,
e testes automatizados. A interface deixa explícito qual motor gerou o produto,
e a precisão esperada de cada um.

## 4. Como o processamento de múltiplas pastas é implementado

`ingest/discovery.py`, função `discover_images(root)`:

1. Caminhada com `os.scandir` recursivo, iterativo (pilha explícita, sem
   recursão de função) — não estoura pilha em árvores profundas.
2. `follow_symlinks=False` e um conjunto de `(st_dev, st_ino)` já visitados:
   symlink circular não trava a varredura.
3. Filtro de extensão (`.jpg .jpeg .tif .tiff .png`) e de diretórios de ruído
   (`__MACOSX`, `.Trash`, `.thumbs`, `.cache`, saídas do próprio app).
4. Validação barata primeiro: tamanho > 0, assinatura de arquivo (magic bytes),
   e `Image.verify()` do Pillow. Arquivo corrompido vira `InvalidImage` com
   motivo, não exceção que derruba o job.
5. Deduplicação em dois níveis: `(tamanho, hash dos primeiros e últimos 64 KiB)`
   como filtro barato, e SHA-256 completo só nos candidatos colididos. Mesma
   foto copiada em duas pastas entra uma vez só.
6. Extração de metadados EXIF/XMP em pool de processos.
7. Ordenação por timestamp e, na ausência dele, por caminho natural.
8. Tudo isso resulta em **um** registro `Dataset` com N imagens. O caminho
   relativo é guardado apenas para exibição (`CAMERA_01/DJI_0001.JPG`).

A varredura é feita em streaming e emite progresso a cada 200 arquivos, então a
tela mostra "1.842 imagens encontradas em 9 pastas" enquanto ainda varre.

Duas formas de entrada, mesma função de descoberta:

- **Pasta no servidor / NAS**: usuário informa o caminho, o backend varre.
  É o modo para 3.000+ imagens.
- **Upload pelo navegador**: `<input webkitdirectory>` preserva
  `webkitRelativePath`; o frontend envia em lotes paralelos limitados e o
  backend reconstrói a árvore em `STORAGE_ROOT/projects/<id>/images/`.

## 5. Como evitar estouro de memória com milhares de imagens

- Nenhuma imagem full-size é carregada no navegador. O front consome apenas
  thumbnails de 256 px e tiles XYZ de 256×256.
- O backend nunca faz `list(all_images)` com pixels: a descoberta trabalha com
  metadados (algumas centenas de bytes por imagem), e leitura de pixel só
  acontece dentro do motor.
- Extração de EXIF em `ProcessPoolExecutor` com `chunksize`, resultados
  gravados no banco em lotes de 500 — o pico de RAM é O(lote), não O(dataset).
- Thumbnails geradas com `Image.draft()` (decodificação JPEG em escala
  reduzida, ~8× menos memória e tempo).
- Ortomosaico escrito como **COG** com overviews; a API serve tiles via
  rio-tiler, lendo apenas as janelas necessárias do arquivo.
- Export e download por streaming (`StreamingResponse`), nunca `read()` inteiro.
- O motor ODM roda em processo separado, com limite de memória configurável;
  se ele morrer por OOM o job falha isoladamente e a API continua de pé.

## 6. Como o backend controla o processamento

- `Job` no banco com estado (`queued`, `running`, `succeeded`, `failed`,
  `canceled`), etapa atual (1..8), percentual, contadores, tempo decorrido e
  estimativa.
- Um `TaskQueue` publica o job; o worker executa `run_pipeline(job_id)`.
- O adaptador do motor traduz a saída do ODM (log de estágios) em progresso
  normalizado nas 8 etapas da UI, por mapa de padrões de linha.
- Progresso chega ao navegador por **SSE** (`/api/jobs/{id}/events`), com
  polling de fallback. Métricas de CPU/RAM vêm de `psutil` e GPU de
  `nvidia-smi` quando existir.
- Cancelamento: flag no banco + `SIGTERM` no processo filho / `revoke` no
  Celery. Logs completos em `storage/projects/<id>/logs/`, e as últimas linhas
  ficam disponíveis na API.
- Recuperação de erro: imagens inválidas são registradas com motivo e o job
  segue com as válidas, desde que reste um mínimo configurável (padrão: 60% e
  ao menos 5 imagens). O relatório final lista o que foi ignorado.

## 7. Como o ortomosaico é georreferenciado

- Cada imagem contribui com posição (lat/lon/alt), ângulos e parâmetros de
  câmera. O ODM resolve as poses por SfM + bundle adjustment e ortorretifica
  sobre o DSM, escrevendo GeoTIFF com CRS e transform corretos (UTM local por
  padrão, para manter unidades em metros).
- O app lê o CRS do produto com rasterio, reprojeta para EPSG:4326/3857 quando
  necessário e guarda os limites em GeoJSON para o mapa.
- O usuário pode fixar o CRS de saída (EPSG, zona UTM, hemisfério). Sem
  escolha manual, a zona UTM é derivada do centroide do voo.
- No motor `direct`, o georreferenciamento vem do modelo de câmera pinhole
  projetado no plano do terreno: GSD = altura_relativa × tamanho_do_pixel /
  distância_focal; os cantos da imagem viram coordenadas geográficas e o
  GeoTIFF sai com transform afim em UTM.
- Exportação preserva CRS, e gera World File e KML/KMZ quando pedido.

## 8. O que é implementado aqui e o que vem de biblioteca

Implementado neste repositório:

- Descoberta recursiva, validação, deduplicação e ordenação (o diferencial).
- Parser de EXIF e de XMP DJI, incluindo `drone-dji:` (yaw/pitch/roll do
  gimbal, altitude relativa, flag RTK).
- Modelo de dados de projeto/dataset/imagem/job, e todo o controle de execução.
- Adaptadores de motor, tradução de progresso, cancelamento e logs.
- Motor `direct` (projeção, mosaico, blending, escrita de GeoTIFF).
- Estimativa de área do voo (envoltória convexa em UTM), GSD e resumo.
- Tiles XYZ, exportação (GeoTIFF/COG, PNG, KMZ, World File, relatório).
- Frontend inteiro.

Bibliotecas / ferramentas externas:

- **OpenDroneMap**: SfM, bundle adjustment, nuvem de pontos, DSM/DTM,
  ortorretificação e blending do produto de precisão.
- GDAL/rasterio/rio-tiler: I/O raster, reprojeção, COG, tiles.
- pyproj/shapely: CRS, envoltória e áreas.
- OpenCV/NumPy: features e composição no motor `direct` e nas thumbnails.
- Pillow: decodificação e EXIF.

## 9. Estrutura de pastas

```
ORTOMOSAICO/
├── backend/
│   ├── app/
│   │   ├── main.py                # FastAPI, CORS, rotas, startup
│   │   ├── config.py              # Settings (env)
│   │   ├── db.py  models.py  schemas.py
│   │   ├── api/                   # projects, datasets, jobs, tiles, exports, system
│   │   ├── ingest/                # discovery, validate, metadata(EXIF/XMP), summary
│   │   ├── processing/            # pipeline, queue, engines/{base,odm,direct}
│   │   ├── geo/                   # crs, footprint, mosaic, export, report
│   │   └── utils/                 # hashing, images, sysinfo, logging
│   ├── tests/
│   └── requirements.txt
├── frontend/                      # Next.js + TS + MapLibre
│   ├── app/                       # dashboard, /projects/new, /projects/[id]
│   ├── components/                # wizard, dataset summary, progresso, mapa
│   └── lib/api.ts
├── docker-compose.yml             # api, worker, redis, postgis, nodeodm, web
├── docs/ARCHITECTURE.md
└── storage/                       # projetos, imagens, produtos, logs (gitignored)
```

## 10. Fluxo completo dos dados

```
1. POST /api/projects                 -> cria projeto (voo)
2. POST /api/projects/{id}/scan       -> caminho no servidor
   ou  POST /api/projects/{id}/upload -> lotes com caminho relativo preservado
3. discovery: varre subpastas, valida, deduplica
4. metadata: EXIF + XMP em pool de processos, gravação em lotes
5. summary: contagens, GPS, altitude média, câmera, área (ha), GSD
   -> tela "Dataset encontrado" + botão "Processar voo completo"
6. POST /api/projects/{id}/jobs       -> enfileira pipeline (motor + opções)
7. worker: prepara diretório do dataset (uma pasta plana, todas as imagens)
8. motor: SfM -> bundle adjustment -> nuvem -> DSM -> ortorretificação -> mosaico
9. pós: converte para COG, calcula bounds/CRS, gera thumbnail e relatório
10. GET /api/projects/{id}/tiles/{z}/{x}/{y}.png -> mapa MapLibre
11. GET /api/projects/{id}/exports/geotiff|png|kmz|worldfile|report
```

## Limites conhecidos da primeira versão

- O motor de precisão exige Docker (imagem `opendronemap/odm`) ou um NodeODM
  acessível. Sem isso, apenas o motor `direct` fica disponível, e a UI diz isso
  claramente.
- GCP, RTK/PPK e índices de vegetação têm modelo de dados e pontos de extensão
  previstos, mas não estão implementados.
- Autenticação multiusuário não faz parte do MVP.
