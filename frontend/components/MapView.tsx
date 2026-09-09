"use client";

import maplibregl, { type GeoJSONSource, type Map as MapLibreMap } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect, useRef, useState } from "react";

import { api, type DatasetSummary } from "@/lib/api";

type MeasureMode = "off" | "distance" | "area";

const EARTH_RADIUS_M = 6_371_008.8;

/** Distância geodésica entre dois pontos (haversine). */
function haversine(a: [number, number], b: [number, number]): number {
  const toRad = (value: number) => (value * Math.PI) / 180;
  const dLat = toRad(b[1] - a[1]);
  const dLon = toRad(b[0] - a[0]);
  const lat1 = toRad(a[1]);
  const lat2 = toRad(b[1]);
  const h =
    Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_RADIUS_M * Math.asin(Math.sqrt(h));
}

/** Área de um polígono geodésico pequeno, projetado localmente em metros. */
function polygonArea(points: [number, number][]): number {
  if (points.length < 3) return 0;
  const lat0 = (points.reduce((sum, p) => sum + p[1], 0) / points.length) * (Math.PI / 180);
  const mx = EARTH_RADIUS_M * Math.cos(lat0) * (Math.PI / 180);
  const my = EARTH_RADIUS_M * (Math.PI / 180);
  const xy = points.map(([lon, lat]) => [lon * mx, lat * my] as [number, number]);
  let total = 0;
  for (let i = 0; i < xy.length; i += 1) {
    const [x1, y1] = xy[i];
    const [x2, y2] = xy[(i + 1) % xy.length];
    total += x1 * y2 - x2 * y1;
  }
  return Math.abs(total) / 2;
}

const BASE_STYLE: maplibregl.StyleSpecification = {
  version: 8,
  sources: {
    osm: {
      type: "raster",
      tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
      tileSize: 256,
      attribution: "© OpenStreetMap",
    },
  },
  layers: [
    { id: "background", type: "background", paint: { "background-color": "#0d1117" } },
    { id: "osm", type: "raster", source: "osm", paint: { "raster-opacity": 0.55 } },
  ],
};

export function MapView({
  projectId,
  summary,
  hasOrthomosaic,
}: {
  projectId: string;
  summary: DatasetSummary;
  hasOrthomosaic: boolean;
}) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<MapLibreMap | null>(null);
  const [ready, setReady] = useState(false);
  const [cursor, setCursor] = useState<{ lon: number; lat: number } | null>(null);
  const [layers, setLayers] = useState({
    ortho: true, cameras: true, footprints: false, boundary: true, basemap: true,
  });
  const [measureMode, setMeasureMode] = useState<MeasureMode>("off");
  const [measurePoints, setMeasurePoints] = useState<[number, number][]>([]);

  useEffect(() => {
    if (!container.current || map.current) return;
    const instance = new maplibregl.Map({
      container: container.current,
      style: BASE_STYLE,
      center: summary.center ?? [-47.83, -21.17],
      zoom: summary.center ? 15 : 4,
      attributionControl: false,
    });
    instance.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
    instance.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");
    instance.on("mousemove", (event) =>
      setCursor({ lon: event.lngLat.lng, lat: event.lngLat.lat }),
    );
    instance.on("load", () => setReady(true));
    map.current = instance;
    return () => {
      instance.remove();
      map.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Camadas de dados: ortomosaico (tiles), posições das câmeras, footprints e limite.
  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready) return;

    let canceled = false;

    async function addLayers() {
      if (hasOrthomosaic && !instance!.getSource("ortho")) {
        try {
          const tileJson = await api.tileJson(projectId);
          if (canceled) return;
          instance!.addSource("ortho", {
            type: "raster",
            tiles: [tileJson.tiles[0]],
            tileSize: 256,
            bounds: tileJson.bounds,
            minzoom: Math.max(0, tileJson.minzoom - 2),
            maxzoom: tileJson.maxzoom,
          });
          instance!.addLayer({ id: "ortho", type: "raster", source: "ortho" });
          instance!.fitBounds(tileJson.bounds, { padding: 40, duration: 800 });
        } catch {
          /* ainda sem ortomosaico */
        }
      }

      if (!instance!.getSource("images")) {
        const response = await fetch(`/api/projects/${projectId}/images.geojson`);
        if (!response.ok || canceled) return;
        const data = await response.json();
        instance!.addSource("images", { type: "geojson", data });
        instance!.addLayer({
          id: "footprints",
          type: "line",
          source: "images",
          filter: ["==", ["get", "kind"], "footprint"],
          paint: { "line-color": "#38bdf8", "line-width": 0.6, "line-opacity": 0.55 },
        });
        instance!.addLayer({
          id: "cameras",
          type: "circle",
          source: "images",
          filter: ["==", ["get", "kind"], "camera"],
          paint: {
            "circle-radius": 3.4,
            "circle-color": "#4ade80",
            "circle-stroke-color": "#04140a",
            "circle-stroke-width": 1,
          },
        });
        instance!.on("click", "cameras", (event) => {
          const feature = event.features?.[0];
          if (!feature) return;
          const properties = feature.properties as Record<string, string>;
          new maplibregl.Popup({ closeButton: true })
            .setLngLat(event.lngLat)
            .setHTML(
              `<div style="color:#0d1117;font-size:12px">
                 <strong>${properties.file}</strong><br/>
                 pasta: ${properties.folder}<br/>
                 altitude: ${Number(properties.altitude ?? 0).toFixed(1)} m<br/>
                 câmera: ${properties.camera ?? "—"}
               </div>`,
            )
            .addTo(instance!);
        });
      }

      if (summary.hull_wgs84?.length && !instance!.getSource("boundary")) {
        const ring = [...summary.hull_wgs84, summary.hull_wgs84[0]];
        instance!.addSource("boundary", {
          type: "geojson",
          data: {
            type: "Feature",
            properties: {},
            geometry: { type: "LineString", coordinates: ring },
          },
        });
        instance!.addLayer({
          id: "boundary",
          type: "line",
          source: "boundary",
          paint: { "line-color": "#fbbf24", "line-width": 1.6, "line-dasharray": [3, 2] },
        });
        if (!hasOrthomosaic && summary.bounds_wgs84) {
          const [w, s, e, n] = summary.bounds_wgs84;
          instance!.fitBounds([[w, s], [e, n]], { padding: 50, duration: 600 });
        }
      }
    }

    addLayers();
    return () => {
      canceled = true;
    };
  }, [ready, projectId, hasOrthomosaic, summary]);

  // Visibilidade das camadas.
  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready) return;
    const visibility = (visible: boolean) => (visible ? "visible" : "none");
    for (const [id, visible] of [
      ["ortho", layers.ortho],
      ["cameras", layers.cameras],
      ["footprints", layers.footprints],
      ["boundary", layers.boundary],
      ["osm", layers.basemap],
    ] as [string, boolean][]) {
      if (instance.getLayer(id)) {
        instance.setLayoutProperty(id, "visibility", visibility(visible));
      }
    }
  }, [layers, ready]);

  // Medição de distância e área.
  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready) return;

    const onClick = (event: maplibregl.MapMouseEvent) => {
      if (measureMode === "off") return;
      setMeasurePoints((points) => [...points, [event.lngLat.lng, event.lngLat.lat]]);
    };
    instance.on("click", onClick);
    return () => {
      instance.off("click", onClick);
    };
  }, [measureMode, ready]);

  useEffect(() => {
    const instance = map.current;
    if (!instance || !ready) return;
    const data: GeoJSON.FeatureCollection = {
      type: "FeatureCollection",
      features: measurePoints.length
        ? [
            {
              type: "Feature",
              properties: {},
              geometry:
                measureMode === "area" && measurePoints.length > 2
                  ? { type: "Polygon", coordinates: [[...measurePoints, measurePoints[0]]] }
                  : { type: "LineString", coordinates: measurePoints },
            },
          ]
        : [],
    };
    const source = instance.getSource("measure") as GeoJSONSource | undefined;
    if (source) {
      source.setData(data);
      return;
    }
    instance.addSource("measure", { type: "geojson", data });
    instance.addLayer({
      id: "measure-fill",
      type: "fill",
      source: "measure",
      filter: ["==", ["geometry-type"], "Polygon"],
      paint: { "fill-color": "#f87171", "fill-opacity": 0.18 },
    });
    instance.addLayer({
      id: "measure-line",
      type: "line",
      source: "measure",
      paint: { "line-color": "#f87171", "line-width": 2 },
    });
  }, [measurePoints, measureMode, ready]);

  const distance = measurePoints.reduce(
    (total, point, index) =>
      index === 0 ? 0 : total + haversine(measurePoints[index - 1], point),
    0,
  );
  const area = measureMode === "area" ? polygonArea(measurePoints) : 0;

  return (
    <div className="map-wrap">
      <div ref={container} style={{ position: "absolute", inset: 0 }} />

      <div className="map-overlay tr">
        <h3 style={{ marginBottom: 6 }}>Camadas</h3>
        {[
          ["ortho", "Ortomosaico"],
          ["cameras", "Posição das câmeras"],
          ["footprints", "Footprint das imagens"],
          ["boundary", "Limite do projeto"],
          ["basemap", "Mapa base"],
        ].map(([key, label]) => (
          <div className="layer-toggle" key={key}>
            <input
              id={`layer-${key}`}
              type="checkbox"
              checked={layers[key as keyof typeof layers]}
              onChange={(event) =>
                setLayers((current) => ({ ...current, [key]: event.target.checked }))
              }
            />
            <label htmlFor={`layer-${key}`} style={{ margin: 0 }}>{label}</label>
          </div>
        ))}
      </div>

      <div className="map-overlay tl">
        <div className="row">
          <button
            className={`btn ${measureMode === "distance" ? "primary" : ""}`}
            onClick={() => {
              setMeasureMode(measureMode === "distance" ? "off" : "distance");
              setMeasurePoints([]);
            }}
          >
            Distância
          </button>
          <button
            className={`btn ${measureMode === "area" ? "primary" : ""}`}
            onClick={() => {
              setMeasureMode(measureMode === "area" ? "off" : "area");
              setMeasurePoints([]);
            }}
          >
            Área
          </button>
          {measurePoints.length > 0 && (
            <button className="btn" onClick={() => setMeasurePoints([])}>Limpar</button>
          )}
        </div>
        {measureMode !== "off" && (
          <p className="muted" style={{ margin: "8px 0 0", fontSize: 12 }}>
            {measureMode === "distance"
              ? `${(distance / 1000).toFixed(3)} km (${distance.toFixed(1)} m)`
              : `${(area / 10000).toFixed(2)} ha (${area.toFixed(0)} m²)`}
            <br />
            Clique no mapa para adicionar pontos.
          </p>
        )}
      </div>

      <div className="map-overlay bl">
        {cursor
          ? `${cursor.lat.toFixed(6)}, ${cursor.lon.toFixed(6)}`
          : "mova o cursor sobre o mapa"}
        {summary.epsg ? ` · EPSG:${summary.epsg}` : ""}
      </div>
    </div>
  );
}
