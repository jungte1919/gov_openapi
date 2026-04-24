/**
 * Generic Sigungu choropleth renderer.
 * Required payload:
 * {
 *   byCode: { "<sigungu_code>": number, ... },
 *   itemName: "고용률",
 *   prdLabel: "2025",
 *   footnote: "optional"
 * }
 *
 * Usage:
 *   window.renderSigunguMap(containerElement, payload)
 */
(function () {
  const TOPO_URL =
    "https://raw.githubusercontent.com/southkorea/southkorea-maps/master/kostat/2018/json/skorea-municipalities-2018-topo-simple.json";

  function colorScale(v, vmin, vmax) {
    if (v == null || Number.isNaN(v)) return "#e5e7eb";
    const t =
      vmax <= vmin ? 0.5 : Math.max(0, Math.min(1, (v - vmin) / (vmax - vmin)));
    const lo = [239, 246, 255];
    const hi = [12, 74, 110];
    const r = Math.round(lo[0] + (hi[0] - lo[0]) * t);
    const g = Math.round(lo[1] + (hi[1] - lo[1]) * t);
    const b = Math.round(lo[2] + (hi[2] - lo[2]) * t);
    return "rgb(" + r + "," + g + "," + b + ")";
  }

  function renderSigunguMap(container, payload) {
    if (!container) return;
    if (
      typeof L === "undefined" ||
      typeof topojson === "undefined" ||
      typeof topojson.feature !== "function"
    ) {
      container.innerHTML =
        '<p class="chart-missing">Leaflet/TopoJSON 라이브러리가 필요합니다.</p>';
      return;
    }
    if (!payload || !payload.byCode) {
      container.innerHTML = '<p class="chart-missing">지도 데이터가 없습니다.</p>';
      return;
    }

    const vals = Object.values(payload.byCode).filter(
      (x) => typeof x === "number" && !Number.isNaN(x)
    );
    const vmin = vals.length ? Math.min.apply(null, vals) : 0;
    const vmax = vals.length ? Math.max.apply(null, vals) : 100;

    fetch(TOPO_URL)
      .then((r) => {
        if (!r.ok) throw new Error("topojson " + r.status);
        return r.json();
      })
      .then((topo) => {
        const geo = topojson.feature(
          topo,
          topo.objects["skorea_municipalities_2018_geo"]
        );
        container.innerHTML = "";
        const mapDiv = document.createElement("div");
        mapDiv.style.height = "520px";
        container.appendChild(mapDiv);

        const map = L.map(mapDiv, {
          zoomControl: true,
          attributionControl: true,
          scrollWheelZoom: true,
        });
        const layer = L.geoJSON(geo, {
          style: (feat) => {
            const code = feat.properties && feat.properties.code;
            const v = code != null ? payload.byCode[String(code)] : null;
            return {
              fillColor: colorScale(v, vmin, vmax),
              color: "#94a3b8",
              weight: 0.4,
              fillOpacity: v != null ? 0.88 : 0.35,
            };
          },
          onEachFeature: (feat, lyr) => {
            const code = feat.properties && feat.properties.code;
            const name = (feat.properties && feat.properties.name) || "";
            const v = code != null ? payload.byCode[String(code)] : null;
            lyr.bindTooltip(
              "<strong>" +
                (name || code || "") +
                "</strong><br/>" +
                (payload.itemName || "값") +
                ": " +
                (v != null ? Number(v).toFixed(2) : "—"),
              { sticky: true, direction: "auto" }
            );
          },
        });
        layer.addTo(map);
        map.fitBounds(layer.getBounds(), { padding: [12, 12] });
      })
      .catch(() => {
        container.innerHTML =
          '<p class="chart-missing">행정구역 경계 데이터를 불러오지 못했습니다.</p>';
      });
  }

  window.renderSigunguMap = renderSigunguMap;
})();

