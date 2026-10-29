// Waveform display and seek control. Peaks come from the server, which measures
// them from the real WAV samples; nothing here is decorative or random.
window.Waveforms = (() => {
  const cache = new Map();
  const PLAYED = "#ffc04d";
  const REST = "rgba(190, 200, 220, 0.34)";
  const BANDS = {
    candidate: "rgba(98, 201, 255, 0.2)",
    confirmed_announcement: "rgba(82, 214, 164, 0.26)",
    uncertain: "rgba(98, 201, 255, 0.2)",
    not_announcement: "rgba(190, 200, 220, 0.08)",
  };

  // One request per recording version; failures are not cached, so a retry can succeed.
  function fetchPeaks(url, key) {
    if (!cache.has(key)) {
      const request = fetch(url, { cache: "no-store" }).then(async (response) => {
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !Array.isArray(data.peaks)) throw new Error(data.error || `HTTP ${response.status}`);
        return data;
      });
      request.catch(() => cache.delete(key));
      cache.set(key, request);
    }
    return cache.get(key);
  }

  function formatTime(seconds) {
    const whole = Math.max(0, Math.floor(seconds || 0));
    return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
  }

  class Waveform {
    constructor({ label, height = 44, onSeek = () => {} }) {
      this.height = height;
      this.onSeek = onSeek;
      this.peaks = null;
      this.duration = 0;
      this.position = 0;
      this.intervals = [];

      this.el = document.createElement("div");
      this.el.className = "wave";
      this.el.tabIndex = 0;
      this.el.setAttribute("role", "slider");
      this.el.setAttribute("aria-label", label);
      this.el.setAttribute("aria-valuemin", "0");
      this.canvas = document.createElement("canvas");
      this.canvas.style.height = `${height}px`;
      this.status = document.createElement("span");
      this.status.className = "wave-status";
      this.el.append(this.canvas, this.status);
      this.setState("loading", "Measuring waveform…");

      let dragging = false;
      this.el.addEventListener("pointerdown", (event) => {
        if (!this.duration) return;
        dragging = true;
        this.el.setPointerCapture(event.pointerId);
        this.seekToX(event.clientX);
      });
      this.el.addEventListener("pointermove", (event) => dragging && this.seekToX(event.clientX));
      const stop = () => (dragging = false);
      this.el.addEventListener("pointerup", stop);
      this.el.addEventListener("pointercancel", stop);
      this.el.addEventListener("keydown", (event) => {
        if (!this.duration) return;
        const steps = { ArrowLeft: -5, ArrowRight: 5, ArrowDown: -5, ArrowUp: 5, PageDown: -15, PageUp: 15 };
        let target = null;
        if (event.key in steps) target = this.position + steps[event.key];
        else if (event.key === "Home") target = 0;
        else if (event.key === "End") target = this.duration;
        if (target === null) return;
        event.preventDefault();
        this.onSeek(Math.min(this.duration, Math.max(0, target)));
      });
      new ResizeObserver(() => this.draw()).observe(this.el);
    }

    async load(url, key) {
      this.setState("loading", "Measuring waveform…");
      try {
        const data = await fetchPeaks(url, key);
        this.peaks = data.peaks;
        this.duration = this.duration || data.duration_seconds;
        this.setState("ready", "");
      } catch (error) {
        this.peaks = null;
        this.setState("error", "Waveform unavailable; audio still plays.");
      }
      this.updateAria();
      this.draw();
      return this.peaks !== null;
    }

    setState(state, text) {
      this.el.dataset.state = state;
      this.status.textContent = text;
    }

    setDuration(seconds) {
      if (Number.isFinite(seconds) && seconds > 0) {
        this.duration = seconds;
        this.el.setAttribute("aria-valuemax", seconds.toFixed(1));
        this.updateAria();
      }
    }

    setPosition(seconds) {
      this.position = seconds || 0;
      this.updateAria();
      this.draw();
    }

    setIntervals(intervals) {
      this.intervals = intervals;
      this.draw();
    }

    updateAria() {
      this.el.setAttribute("aria-valuemax", (this.duration || 0).toFixed(1));
      this.el.setAttribute("aria-valuenow", this.position.toFixed(1));
      this.el.setAttribute("aria-valuetext", `${formatTime(this.position)} of ${formatTime(this.duration)}`);
    }

    seekToX(clientX) {
      const rect = this.el.getBoundingClientRect();
      const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
      this.onSeek(ratio * this.duration);
    }

    draw() {
      const scale = window.devicePixelRatio || 1;
      const width = Math.round(this.el.clientWidth * scale);
      const height = Math.round(this.height * scale);
      if (!width) return;
      if (this.canvas.width !== width || this.canvas.height !== height) {
        this.canvas.width = width;
        this.canvas.height = height;
      }
      const context = this.canvas.getContext("2d");
      context.clearRect(0, 0, width, height);
      if (!this.duration) return;

      for (const interval of this.intervals) {
        const x0 = (interval.start_seconds / this.duration) * width;
        const x1 = (interval.end_seconds / this.duration) * width;
        context.fillStyle = BANDS[interval.kind] || BANDS.candidate;
        context.fillRect(x0, 0, Math.max(2 * scale, x1 - x0), height);
      }

      if (!this.peaks) return;
      const bar = 2 * scale;
      const gap = 1 * scale;
      const count = Math.max(1, Math.floor(width / (bar + gap)));
      const played = (this.position / this.duration) * width;
      for (let i = 0; i < count; i++) {
        // Each bar shows the largest measured peak in its slice of the recording.
        const from = Math.floor((i / count) * this.peaks.length);
        const to = Math.max(from + 1, Math.floor(((i + 1) / count) * this.peaks.length));
        let peak = 0;
        for (let j = from; j < to; j++) peak = Math.max(peak, this.peaks[j]);
        const barHeight = Math.max(scale, peak * height * 0.92);
        const x = i * (bar + gap);
        context.fillStyle = x < played ? PLAYED : REST;
        context.fillRect(x, (height - barHeight) / 2, bar, barHeight);
      }
    }
  }

  return { Waveform, fetchPeaks, formatTime };
})();
