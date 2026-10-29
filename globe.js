// Interactive globe, ported from the course project's Globe.tsx to plain
// JavaScript on globe.gl (the library react-globe.gl wraps).
// City markers use sourced city-level coordinates (cities/cities.json);
// exact markers appear only for recordings listed in audio/locations.json.
window.SonicGlobe = (() => {
  const container = document.querySelector("#globe");
  const fallback = document.querySelector("#globe-fallback");
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const AMBER = "#ffc04d";
  const EXACT = "#fff3cf";
  const COOL = "#62c9ff";

  let globe = null;
  let cityMarkers = [];
  let exactMarkers = [];
  let highlighted = new Set();
  let mode = "landing";
  let onSelect = () => {};

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
  }

  function webglAvailable() {
    try {
      const canvas = document.createElement("canvas");
      return Boolean(canvas.getContext("webgl2") || canvas.getContext("webgl"));
    } catch {
      return false;
    }
  }

  function showFallback(message) {
    container.hidden = true;
    fallback.textContent = message;
    fallback.hidden = false;
  }

  const isHighlighted = (marker) => highlighted.has(marker.city);

  function render() {
    if (!globe) return;
    globe
      .pointsData([...cityMarkers, ...exactMarkers])
      .ringsData(cityMarkers);
  }

  // On the landing screen the globe sits beside the title instead of under it.
  function applyLayout() {
    if (!globe) return;
    const width = container.clientWidth;
    const height = container.clientHeight;
    globe.width(width).height(height);
    if (mode === "landing" && width > 720) {
      globe.globeOffset([width * 0.17, 0]);
    } else if (mode === "landing") {
      globe.globeOffset([0, -height * 0.18]);
    } else {
      globe.globeOffset([0, 0]);
    }
  }

  function init() {
    if (typeof window.Globe !== "function") {
      showFallback("The 3D globe could not load. Chat and audio still work.");
      return;
    }
    if (!webglAvailable()) {
      showFallback("The 3D globe needs WebGL, which is unavailable in this browser. Chat and audio still work.");
      return;
    }

    globe = new window.Globe(container, { animateIn: !reduceMotion })
      .backgroundColor("rgba(0,0,0,0)")
      .showAtmosphere(true)
      .atmosphereColor("#7fb3ff")
      .atmosphereAltitude(0.15)
      .polygonCapColor(() => "rgba(140, 150, 172, 0.13)")
      .polygonSideColor(() => "rgba(0, 0, 0, 0)")
      .polygonStrokeColor(() => "rgba(170, 186, 212, 0.26)")
      .polygonAltitude(0.004)
      .polygonsTransitionDuration(0)
      .pointLat("lat")
      .pointLng("lng")
      .pointColor((marker) => (isHighlighted(marker) ? COOL : marker.kind === "city" ? AMBER : EXACT))
      .pointAltitude((marker) => (marker.kind === "city" ? 0.02 : 0.04))
      .pointRadius((marker) => (marker.kind === "city" ? 0.75 : 0.32))
      .pointsTransitionDuration(0)
      .pointLabel((marker) => {
        const count = marker.recordings.length;
        const kind = marker.kind === "city" ? "City-level marker" : "Exact recording spot";
        return `<strong>${escapeHtml(marker.title)}</strong><br>${count} recording${count === 1 ? "" : "s"} · ${kind}`;
      })
      .onPointClick((marker) => {
        globe.controls().autoRotate = false;
        focus(marker.city);
        onSelect(marker);
      })
      .ringLat("lat")
      .ringLng("lng")
      .ringColor((marker) => {
        const [r, g, b] = isHighlighted(marker) ? [98, 201, 255] : [255, 192, 77];
        return (t) => `rgba(${r}, ${g}, ${b}, ${0.7 * (1 - t)})`;
      })
      .ringMaxRadius(2.6)
      .ringPropagationSpeed(1.1)
      .ringRepeatPeriod(1800);

    // No licensed Earth texture is bundled, so the sphere is near-black and the
    // continents come from public-domain Natural Earth outlines.
    const material = globe.globeMaterial();
    material.color.set("#0a0c11");
    if (material.emissive) material.emissive.set("#020306");

    // The camera fits the globe to the height, so tall phone screens start further out.
    const portrait = container.clientWidth / Math.max(container.clientHeight, 1) < 0.8;
    globe.pointOfView({ lat: 30, lng: 20, altitude: portrait ? 3.6 : 2.0 }, 0);
    const controls = globe.controls();
    controls.autoRotate = !reduceMotion;
    controls.autoRotateSpeed = 0.4;
    controls.minDistance = 140;
    controls.maxDistance = 600;
    // Stop auto-rotating once the listener starts exploring.
    controls.addEventListener("start", () => {
      controls.autoRotate = false;
    });

    new ResizeObserver(applyLayout).observe(container);
    applyLayout();

    document.addEventListener("visibilitychange", () => {
      if (document.hidden) globe.pauseAnimation();
      else globe.resumeAnimation();
    });

    fetch("/vendor/ne_110m_land.geojson")
      .then((response) => (response.ok ? response.json() : Promise.reject(response.status)))
      .then((land) => globe.polygonsData(land.features))
      .catch(() => {
        // The atmosphere and markers still show the globe without outlines.
      });

    render();
  }

  function markerFor(cityName) {
    return cityMarkers.find((marker) => marker.city === cityName)
      || exactMarkers.find((marker) => marker.city === cityName);
  }

  function focus(cityName, ms = 900) {
    const marker = markerFor(cityName);
    if (!globe || !marker) return;
    globe.controls().autoRotate = false;
    globe.pointOfView({ lat: marker.lat, lng: marker.lng, altitude: 1.7 }, reduceMotion ? 0 : ms);
  }

  // Build the WebGL scene after the first frame so chat and status load first.
  requestAnimationFrame(() => setTimeout(init, 0));

  return {
    setData({ cities, recordings }) {
      cityMarkers = cities
        .filter((city) => city.center)
        .map((city) => ({
          kind: "city",
          city: city.name,
          title: city.name,
          lat: city.center.lat,
          lng: city.center.lng,
          recordings: recordings.filter((item) => item.location === city.name),
        }));

      const byPoint = new Map();
      for (const item of recordings) {
        if (!item.coordinates) continue;
        const { lat, lng, place } = item.coordinates;
        const key = `${lat},${lng}`;
        if (!byPoint.has(key)) {
          byPoint.set(key, { kind: "exact", city: item.location, title: place || item.location, lat, lng, recordings: [] });
        }
        byPoint.get(key).recordings.push(item);
      }
      exactMarkers = [...byPoint.values()];
      render();
      return { cities: cityMarkers.length, exact: exactMarkers.length };
    },
    highlightCities(names) {
      highlighted = new Set(names);
      render();
    },
    setMode(nextMode) {
      mode = nextMode;
      applyLayout();
    },
    focus,
    onSelect(callback) {
      onSelect = callback;
    },
    // Screen position of a city marker, for automated interface checks.
    screenCoords(cityName) {
      const marker = markerFor(cityName);
      return globe && marker ? globe.getScreenCoords(marker.lat, marker.lng, 0.02) : null;
    },
    get available() {
      return Boolean(globe);
    },
  };
})();
