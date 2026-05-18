import axios from 'axios';
import { computed, reactive, ref } from 'vue';

export function useDashboardDeviceMetrics({ tab }) {
        const DASHBOARD_DEVICE_POLL_INTERVAL = 2500;
        const DASHBOARD_DEVICE_HISTORY_LIMIT = 72;
        const DASHBOARD_DEVICE_HISTORY_WINDOW_MS = 28000;
        const DASHBOARD_DEVICE_POLL_GRACE_MS = 8000;
        const dashboardDeviceMetrics = reactive({
            cpu: { percent: 0 },
            memory: { percent: 0, used_gb: 0, total_gb: 0 },
            network: { up_bytes_per_sec: 0, down_bytes_per_sec: 0, up_human: '0 B/s', down_human: '0 B/s' },
            disk: { read_bytes_per_sec: 0, write_bytes_per_sec: 0, read_human: '0 B/s', write_human: '0 B/s' },
            timestamp: null,
        });
        const dashboardDeviceMetricHistory = reactive({
            cpuPercent: [],
            memoryPercent: [],
            uploadBytes: [],
            downloadBytes: [],
            diskReadBytes: [],
            diskWriteBytes: [],
        });

        const dashboardDeviceMetricsPulse = ref(false);

        const dashboardDeviceMetricsLoaded = ref(false);

        let dashboardDeviceMetricsPolling = null;

        let dashboardDeviceMetricsAnimationFrame = null;

        let dashboardDeviceMetricsRequestInFlight = false;

        const pushDashboardMetricSample = (queue, value, sampledAt = Date.now()) => {
            const nextValue = Number(value);
            queue.push({
                t: sampledAt,
                value: Number.isFinite(nextValue) ? nextValue : 0,
            });
            const cutoff = sampledAt - DASHBOARD_DEVICE_HISTORY_WINDOW_MS - DASHBOARD_DEVICE_POLL_GRACE_MS;
            while (queue.length > 0 && Number(queue[0]?.t || 0) < cutoff) {
                queue.shift();
            }
            while (queue.length > DASHBOARD_DEVICE_HISTORY_LIMIT) {
                queue.shift();
            }
        };

        const resetDashboardDeviceMetricHistory = () => {
            Object.values(dashboardDeviceMetricHistory).forEach((queue) => queue.splice(0, queue.length));
        };

        const recordDashboardDeviceMetricHistory = () => {
            const now = Date.now();
            pushDashboardMetricSample(dashboardDeviceMetricHistory.cpuPercent, dashboardDeviceMetrics.cpu.percent, now);
            pushDashboardMetricSample(dashboardDeviceMetricHistory.memoryPercent, dashboardDeviceMetrics.memory.percent, now);
            pushDashboardMetricSample(dashboardDeviceMetricHistory.uploadBytes, dashboardDeviceMetrics.network.up_bytes_per_sec, now);
            pushDashboardMetricSample(dashboardDeviceMetricHistory.downloadBytes, dashboardDeviceMetrics.network.down_bytes_per_sec, now);
            pushDashboardMetricSample(dashboardDeviceMetricHistory.diskReadBytes, dashboardDeviceMetrics.disk.read_bytes_per_sec, now);
            pushDashboardMetricSample(dashboardDeviceMetricHistory.diskWriteBytes, dashboardDeviceMetrics.disk.write_bytes_per_sec, now);
        };

        const resetDashboardDeviceMetrics = () => {
            Object.assign(dashboardDeviceMetrics, {
                cpu: { percent: 0 },
                memory: { percent: 0, used_gb: 0, total_gb: 0 },
                network: { up_bytes_per_sec: 0, down_bytes_per_sec: 0, up_human: '0 B/s', down_human: '0 B/s' },
                disk: { read_bytes_per_sec: 0, write_bytes_per_sec: 0, read_human: '0 B/s', write_human: '0 B/s' },
                timestamp: null,
            });
            resetDashboardDeviceMetricHistory();
            dashboardMetricCanvasState.clear();
        };

        const fetchDashboardDeviceMetrics = async () => {
            if (dashboardDeviceMetricsRequestInFlight || tab.value !== 'dashboard' || document.hidden) return;
            dashboardDeviceMetricsRequestInFlight = true;
            try {
                const res = await axios.get('/api/dashboard_device_metrics');
                Object.assign(dashboardDeviceMetrics, {
                    cpu: { percent: 0 },
                    memory: { percent: 0, used_gb: 0, total_gb: 0 },
                    network: { up_bytes_per_sec: 0, down_bytes_per_sec: 0, up_human: '0 B/s', down_human: '0 B/s' },
                    disk: { read_bytes_per_sec: 0, write_bytes_per_sec: 0, read_human: '0 B/s', write_human: '0 B/s' },
                    timestamp: null,
                }, res.data || {});
                recordDashboardDeviceMetricHistory();
                startDashboardDeviceMetricsAnimation();
                dashboardDeviceMetricsLoaded.value = true;
                dashboardDeviceMetricsPulse.value = false;
                requestAnimationFrame(() => {
                    dashboardDeviceMetricsPulse.value = true;
                });
                setTimeout(() => {
                    dashboardDeviceMetricsPulse.value = false;
                }, 520);
            } catch (e) {
                console.log('Dashboard device metrics failed', e);
                dashboardDeviceMetricsLoaded.value = false;
            } finally {
                dashboardDeviceMetricsRequestInFlight = false;
            }
        };


        const dashboardMetricPaletteMap = {
            cpu: { lineStart: '#4caeb5', lineEnd: '#5f8fdd', fillStart: '#4caeb5', fillEnd: '#5f8fdd' },
            memory: { lineStart: '#6d9de0', lineEnd: '#7f8fe6', fillStart: '#6d9de0', fillEnd: '#7f8fe6' },
            upload: { lineStart: '#56c2b1', lineEnd: '#5caecb', fillStart: '#56c2b1', fillEnd: '#5caecb' },
            download: { lineStart: '#5c9fe0', lineEnd: '#4f83dc', fillStart: '#5c9fe0', fillEnd: '#4f83dc' },
            'disk-read': { lineStart: '#74acd8', lineEnd: '#6d96d8', fillStart: '#74acd8', fillEnd: '#6d96d8' },
            'disk-write': { lineStart: '#4fb3ca', lineEnd: '#5a8fd2', fillStart: '#4fb3ca', fillEnd: '#5a8fd2' },
        };
        const dashboardMetricCanvasState = new Map();

        const hexToRgba = (hex, alpha = 1) => {
            const normalized = String(hex || '').replace('#', '');
            if (normalized.length !== 6) return `rgba(95, 143, 221, ${alpha})`;
            const value = Number.parseInt(normalized, 16);
            const r = (value >> 16) & 255;
            const g = (value >> 8) & 255;
            const b = value & 255;
            return `rgba(${r}, ${g}, ${b}, ${alpha})`;
        };

        const getDashboardMetricCanvasConfig = (key) => {
            const map = {
                cpu: { key: 'cpu', history: dashboardDeviceMetricHistory.cpuPercent, mode: 'percent', tone: 'cpu' },
                memory: { key: 'memory', history: dashboardDeviceMetricHistory.memoryPercent, mode: 'percent', tone: 'memory' },
                upload: { key: 'upload', history: dashboardDeviceMetricHistory.uploadBytes, mode: 'throughput', tone: 'upload' },
                download: { key: 'download', history: dashboardDeviceMetricHistory.downloadBytes, mode: 'throughput', tone: 'download' },
                'disk-read': { key: 'disk-read', history: dashboardDeviceMetricHistory.diskReadBytes, mode: 'throughput', tone: 'disk-read' },
                'disk-write': { key: 'disk-write', history: dashboardDeviceMetricHistory.diskWriteBytes, mode: 'throughput', tone: 'disk-write' },
            };
            return map[key] || map.cpu;
        };

        const normalizeCanvasSamples = (history, now) => {
            const samples = Array.isArray(history)
                ? history
                    .map((sample) => {
                        if (sample && typeof sample === 'object') {
                            const value = Number(sample.value);
                            const t = Number(sample.t);
                            return {
                                t: Number.isFinite(t) ? t : now,
                                value: Number.isFinite(value) ? value : 0,
                            };
                        }
                        const value = Number(sample);
                        return { t: now, value: Number.isFinite(value) ? value : 0 };
                    })
                    .filter((sample) => Number.isFinite(sample.t) && Number.isFinite(sample.value))
                    .sort((a, b) => a.t - b.t)
                : [];
            return samples;
        };

        const getCanvasMetricScale = (samples, mode) => {
            const values = samples.map((sample) => sample.value);
            const rawMin = Math.min(...values);
            const rawMax = Math.max(...values, mode === 'throughput' ? 1 : 0);
            if (mode === 'percent') {
                const rawRange = Math.max(rawMax - rawMin, 0);
                const visibleRange = Math.min(100, Math.max(rawRange * 1.35, 14));
                const center = (rawMin + rawMax) / 2;
                let min = Math.max(0, center - visibleRange / 2);
                let max = Math.min(100, center + visibleRange / 2);
                if (max - min < visibleRange) {
                    if (min <= 0) max = Math.min(100, min + visibleRange);
                    if (max >= 100) min = Math.max(0, max - visibleRange);
                }
                return { min, max: max > min ? max : min + 1 };
            }
            const max = Math.max(rawMax * 1.18, 1);
            return { min: 0, max };
        };

        const getDashboardMetricVisualState = (config, targetValue, targetScale) => {
            const stateKey = config.key || config.tone || 'cpu';
            let state = dashboardMetricCanvasState.get(stateKey);
            if (!state) {
                state = {
                    displayValue: targetValue,
                    scaleMin: targetScale.min,
                    scaleMax: targetScale.max,
                };
                dashboardMetricCanvasState.set(stateKey, state);
            }
            state.displayValue += (targetValue - state.displayValue) * 0.12;
            const minEase = targetScale.min < state.scaleMin ? 0.12 : 0.035;
            const maxEase = targetScale.max > state.scaleMax ? 0.18 : 0.04;
            state.scaleMin += (targetScale.min - state.scaleMin) * minEase;
            state.scaleMax += (targetScale.max - state.scaleMax) * maxEase;
            if (state.scaleMax - state.scaleMin < 1) {
                state.scaleMax = state.scaleMin + 1;
            }
            return state;
        };

        const traceCanvasMetricCurve = (ctx, points) => {
            if (!points.length) return;
            ctx.moveTo(points[0].x, points[0].y);
            if (points.length === 1) return;
            if (points.length === 2) {
                ctx.lineTo(points[1].x, points[1].y);
                return;
            }
            for (let index = 1; index < points.length - 1; index += 1) {
                const currentPoint = points[index];
                const nextPoint = points[index + 1];
                const midX = (currentPoint.x + nextPoint.x) / 2;
                const midY = (currentPoint.y + nextPoint.y) / 2;
                ctx.quadraticCurveTo(currentPoint.x, currentPoint.y, midX, midY);
            }
            const lastPoint = points[points.length - 1];
            ctx.lineTo(lastPoint.x, lastPoint.y);
        };

        const drawDashboardMetricCanvas = (canvas, config, now) => {
            const rect = canvas.getBoundingClientRect();
            if (rect.width <= 0 || rect.height <= 0) return false;
            const dpr = Math.min(window.devicePixelRatio || 1, 2);
            const targetWidth = Math.max(1, Math.round(rect.width * dpr));
            const targetHeight = Math.max(1, Math.round(rect.height * dpr));
            if (canvas.width !== targetWidth || canvas.height !== targetHeight) {
                canvas.width = targetWidth;
                canvas.height = targetHeight;
            }

            const ctx = canvas.getContext('2d');
            if (!ctx) return false;
            const width = rect.width;
            const height = rect.height;
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, width, height);

            const samples = normalizeCanvasSamples(config.history, now);
            if (samples.length < 2) return true;
            const windowStart = now - DASHBOARD_DEVICE_HISTORY_WINDOW_MS;
            const beforeWindow = [...samples].reverse().find((sample) => sample.t < windowStart);
            const hasWindowBoundarySample = !!beforeWindow;
            const rawVisibleSamples = samples.filter((sample) => sample.t >= windowStart && sample.t <= now + DASHBOARD_DEVICE_POLL_INTERVAL);
            if (beforeWindow) {
                rawVisibleSamples.unshift({ t: windowStart, value: beforeWindow.value });
            }
            if (rawVisibleSamples.length < 2) return true;

            const latestSample = samples[samples.length - 1];
            const targetScale = getCanvasMetricScale(rawVisibleSamples, config.mode);
            const visualState = getDashboardMetricVisualState(config, latestSample.value, targetScale);
            const scale = { min: visualState.scaleMin, max: visualState.scaleMax };
            const visibleSamples = rawVisibleSamples
                .filter((sample) => sample !== latestSample && sample.t < latestSample.t)
                .concat({
                    t: Math.max(windowStart, Math.min(latestSample.t, now)),
                    value: visualState.displayValue,
                });
            const lastVisibleSample = visibleSamples[visibleSamples.length - 1];
            if (!lastVisibleSample || now - lastVisibleSample.t > 12) {
                visibleSamples.push({ t: now, value: visualState.displayValue });
            }

            const topPadding = 4;
            const bottomPadding = 6;
            const chartHeight = Math.max(1, height - topPadding - bottomPadding);
            const yForValue = (value) => {
                const ratio = Math.max(0, Math.min(1, (value - scale.min) / (scale.max - scale.min || 1)));
                return topPadding + (1 - ratio) * chartHeight;
            };
            const xForTime = (t) => ((t - windowStart) / DASHBOARD_DEVICE_HISTORY_WINDOW_MS) * width;
            let points = visibleSamples.map((sample) => ({
                x: xForTime(sample.t),
                y: yForValue(sample.value),
            })).filter((point) => point.x >= -width * 0.08 && point.x <= width * 1.08);

            if (points.length < 2) return true;
            if (hasWindowBoundarySample && points[0].x > 0) {
                points.unshift({ x: 0, y: points[0].y });
            }
            const latestPoint = points[points.length - 1];
            if (now - latestSample.t <= DASHBOARD_DEVICE_POLL_INTERVAL * 1.5 && latestPoint.x < width) {
                points.push({ x: width, y: latestPoint.y });
            }

            const palette = dashboardMetricPaletteMap[config.tone] || dashboardMetricPaletteMap.cpu;
            const lineGradient = ctx.createLinearGradient(0, 0, width, 0);
            lineGradient.addColorStop(0, palette.lineStart);
            lineGradient.addColorStop(1, palette.lineEnd);
            const fillGradient = ctx.createLinearGradient(0, topPadding, 0, height);
            fillGradient.addColorStop(0, hexToRgba(palette.fillStart, 0.24));
            fillGradient.addColorStop(0.72, hexToRgba(palette.fillEnd, 0.07));
            fillGradient.addColorStop(1, hexToRgba(palette.fillEnd, 0));

            ctx.save();
            ctx.beginPath();
            traceCanvasMetricCurve(ctx, points);
            const fillStartX = Math.max(0, points[0].x);
            const fillEndX = Math.min(width, points[points.length - 1].x);
            ctx.lineTo(fillEndX, height + 2);
            ctx.lineTo(fillStartX, height + 2);
            ctx.closePath();
            ctx.fillStyle = fillGradient;
            ctx.fill();

            ctx.beginPath();
            traceCanvasMetricCurve(ctx, points);
            ctx.lineWidth = 1.65;
            ctx.lineCap = 'round';
            ctx.lineJoin = 'round';
            ctx.strokeStyle = lineGradient;
            ctx.shadowColor = hexToRgba(palette.lineEnd, 0.28);
            ctx.shadowBlur = 7;
            ctx.stroke();

            ctx.restore();
            return true;
        };

        const drawDashboardDeviceMetricCanvases = () => {
            if (tab.value !== 'dashboard' || document.hidden) return;
            const canvases = document.querySelectorAll('.metric-sub-card-sparkline-canvas');
            if (!canvases.length) return;
            const now = Date.now();
            canvases.forEach((canvas) => {
                drawDashboardMetricCanvas(canvas, getDashboardMetricCanvasConfig(canvas.dataset.metricKey), now);
            });
        };

        const stopDashboardDeviceMetricsPolling = () => {
            if (dashboardDeviceMetricsPolling) {
                clearInterval(dashboardDeviceMetricsPolling);
                dashboardDeviceMetricsPolling = null;
            }
            if (dashboardDeviceMetricsAnimationFrame) {
                cancelAnimationFrame(dashboardDeviceMetricsAnimationFrame);
                dashboardDeviceMetricsAnimationFrame = null;
            }
        };

        const startDashboardDeviceMetricsAnimation = () => {
            if (dashboardDeviceMetricsAnimationFrame) return;
            dashboardDeviceMetricsAnimationFrame = requestAnimationFrame(() => {
                dashboardDeviceMetricsAnimationFrame = null;
                drawDashboardDeviceMetricCanvases();
            });
        };

        const startDashboardDeviceMetricsPolling = () => {
            stopDashboardDeviceMetricsPolling();
            if (tab.value !== 'dashboard' || document.hidden) return;
            startDashboardDeviceMetricsAnimation();
            fetchDashboardDeviceMetrics();
            dashboardDeviceMetricsPolling = setInterval(() => {
                if (tab.value !== 'dashboard' || document.hidden) return;
                fetchDashboardDeviceMetrics();
            }, DASHBOARD_DEVICE_POLL_INTERVAL);
        };


        const getDeviceMetricState = (percent) => {
            const value = Number(percent || 0);
            if (value >= 95) return 'danger';
            if (value >= 80) return 'warning';
            return 'normal';
        };

        const formatDevicePercent = (value) => `${Math.round(Number(value || 0))}%`;
        const formatDeviceMemory = (used, total) => `${Number(used || 0).toFixed(1)} / ${Number(total || 0).toFixed(1)} GB`;

        const splitMetricDisplay = (valueText, options = {}) => {
            const fallback = options.fallback || '--';
            const raw = String(valueText || fallback).trim();
            if (!raw || raw === fallback) {
                return { main: fallback, unit: '', split: false };
            }
            if (raw.endsWith('%')) {
                return {
                    main: raw.slice(0, -1) || '0',
                    unit: '%',
                    split: true,
                };
            }
            const matched = raw.match(/^([\d.]+)\s+(.+)$/);
            if (matched) {
                return {
                    main: matched[1],
                    unit: matched[2],
                    split: true,
                };
            }
            return { main: raw, unit: '', split: false };
        };


        const dashboardDeviceMetricCards = computed(() => {
            const cpuValueText = formatDevicePercent(dashboardDeviceMetrics.cpu.percent);
            const memoryValueText = formatDevicePercent(dashboardDeviceMetrics.memory.percent);
            const uploadValueText = dashboardDeviceMetrics.network.up_human || '--';
            const downloadValueText = dashboardDeviceMetrics.network.down_human || '--';
            const diskReadValueText = dashboardDeviceMetrics.disk.read_human || '--';
            const diskWriteValueText = dashboardDeviceMetrics.disk.write_human || '--';
            return [
                {
                    key: 'cpu',
                    label: 'CPU',
                    icon: 'fa-microchip',
                    tone: 'cpu',
                    state: getDeviceMetricState(dashboardDeviceMetrics.cpu.percent),
                    valueText: cpuValueText,
                    valueDisplay: splitMetricDisplay(cpuValueText),
                    subText: '',
                },
                {
                    key: 'memory',
                    label: '内存',
                    icon: 'fa-memory',
                    tone: 'memory',
                    state: getDeviceMetricState(dashboardDeviceMetrics.memory.percent),
                    valueText: memoryValueText,
                    valueDisplay: splitMetricDisplay(memoryValueText),
                    subText: formatDeviceMemory(dashboardDeviceMetrics.memory.used_gb, dashboardDeviceMetrics.memory.total_gb),
                },
                {
                    key: 'upload',
                    label: '上传',
                    icon: 'fa-arrow-up',
                    tone: 'upload',
                    state: 'normal',
                    valueText: uploadValueText,
                    valueDisplay: splitMetricDisplay(uploadValueText),
                    subText: '',
                },
                {
                    key: 'download',
                    label: '下载',
                    icon: 'fa-arrow-down',
                    tone: 'download',
                    state: 'normal',
                    valueText: downloadValueText,
                    valueDisplay: splitMetricDisplay(downloadValueText),
                    subText: '',
                },
                {
                    key: 'disk-read',
                    label: '读取',
                    icon: 'fa-hard-drive',
                    tone: 'disk-read',
                    state: 'normal',
                    valueText: diskReadValueText,
                    valueDisplay: splitMetricDisplay(diskReadValueText),
                    subText: '',
                },
                {
                    key: 'disk-write',
                    label: '写入',
                    icon: 'fa-pen-to-square',
                    tone: 'disk-write',
                    state: 'normal',
                    valueText: diskWriteValueText,
                    valueDisplay: splitMetricDisplay(diskWriteValueText),
                    subText: '',
                },
            ];
        });


    return {
        dashboardDeviceMetrics,
        dashboardDeviceMetricsLoaded,
        dashboardDeviceMetricsPulse,
        dashboardDeviceMetricCards,
        startDashboardDeviceMetricsPolling,
        stopDashboardDeviceMetricsPolling,
        getDeviceMetricState,
        formatDevicePercent,
        formatDeviceMemory,
    };
}
