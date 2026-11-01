/* منطق التطبيق: يدعم الآن متابعة تقدم yt-dlp ونقل الملف إلى المتصفح. */
const $ = id => document.getElementById(id);
const fmt = s => s !== null && s !== undefined && Number.isFinite(Number(s))
  ? `${Math.floor(Number(s) / 60)}:${String(Math.floor(Number(s) % 60)).padStart(2, "0")}`
  : "—";

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const formatBytes = value => {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = bytes / 1024;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit++;
  }
  return `${size.toFixed(size >= 100 ? 0 : 1)} ${units[unit]}`;
};
const formatSpeed = value => {
  const speed = Number(value);
  return Number.isFinite(speed) && speed > 0 ? `${formatBytes(speed)}/ث` : "—";
};
const makeJobId = () => {
  const bytes = new Uint8Array(16);
  if (window.crypto && window.crypto.getRandomValues) {
    window.crypto.getRandomValues(bytes);
    return Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
  }
  // Local fallback for older browsers. Modern browsers use crypto above.
  return `${Date.now().toString(16)}${Math.random().toString(16).slice(2).padEnd(20, "0")}`.slice(0, 32);
};

const setProgress = ({ percentage, message, speed, downloaded, total, eta, indeterminate = false }) => {
  const panel = $("progress-panel");
  panel.hidden = false;

  const hasPercentage = percentage !== null && percentage !== undefined && percentage !== "" && Number.isFinite(Number(percentage));
  const pct = hasPercentage ? Math.max(0, Math.min(100, Number(percentage))) : null;
  $("progress-status").textContent = message || "جارٍ التنزيل...";
  $("progress-percent").textContent = pct === null ? "—" : `${Math.round(pct)}%`;
  $("progress-fill").style.width = pct === null ? "34%" : `${pct}%`;
  $("progress-track").classList.toggle("is-indeterminate", indeterminate || pct === null);
  $("progress-track").setAttribute("aria-valuenow", pct === null ? "0" : String(Math.round(pct)));

  const received = downloaded === null || downloaded === undefined ? NaN : Number(downloaded);
  const expected = total === null || total === undefined ? NaN : Number(total);
  let sizeText = "الحجم: —";
  if (Number.isFinite(received) && received >= 0) {
    sizeText = Number.isFinite(expected) && expected > 0
      ? `الحجم: ${formatBytes(received)} / ${formatBytes(expected)}`
      : `الحجم: ${formatBytes(received)}`;
  }

  $("progress-speed").textContent = `السرعة: ${formatSpeed(speed)}`;
  $("progress-size").textContent = sizeText;
  $("progress-eta").textContent = `المتبقي: ${eta === null || eta === undefined ? "—" : fmt(eta)}`;
};

const setButtonLabel = (button, text) => {
  const label = button.querySelector("span");
  if (label) label.textContent = text;
  else button.textContent = text;
};

const readDownloadResponse = async response => {
  const headerLength = Number(response.headers.get("Content-Length"));
  const total = Number.isFinite(headerLength) && headerLength > 0 ? headerLength : null;

  if (!response.body || !response.body.getReader) {
    const blob = await response.blob();
    setProgress({
      percentage: 100,
      message: "اكتمل نقل الملف إلى جهازك.",
      downloaded: blob.size,
      total: blob.size,
      speed: null,
      eta: 0
    });
    return blob;
  }

  const reader = response.body.getReader();
  const chunks = [];
  let received = 0;
  let previousReceived = 0;
  let previousTime = performance.now();
  let currentSpeed = null;

  while (true) {
    const result = await reader.read();
    if (result.done) break;
    if (!result.value) continue;

    chunks.push(result.value);
    received += result.value.byteLength;

    const now = performance.now();
    const elapsed = (now - previousTime) / 1000;
    if (elapsed >= 0.35) {
      currentSpeed = (received - previousReceived) / elapsed;
      previousReceived = received;
      previousTime = now;
    }

    const percentage = total ? 95 + (Math.min(received / total, 1) * 5) : null;
    const remaining = total && currentSpeed > 0 ? (total - received) / currentSpeed : null;
    setProgress({
      percentage,
      message: "جارٍ نقل الملف إلى جهازك...",
      speed: currentSpeed,
      downloaded: received,
      total,
      eta: remaining,
      indeterminate: !total
    });
  }

  const contentType = response.headers.get("Content-Type") || "application/octet-stream";
  return new Blob(chunks, { type: contentType });
};

const getDownloadName = response => {
  const disposition = response.headers.get("Content-Disposition") || "";
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (encoded) {
    try { return decodeURIComponent(encoded[1].trim().replace(/^"|"$/g, "")); }
    catch (_) { /* Fall through to regular filename. */ }
  }
  const regular = disposition.match(/filename="?([^";]+)"?/i);
  return regular ? regular[1].trim() : "video.mp4";
};

$("check").onclick = async () => {
  const url = $("url").value.trim();
  $("err").textContent = "";
  $("card").style.display = "none";
  $("progress-panel").hidden = true;
  if (!url) {
    $("err").textContent = "الصق رابط الفيديو أولاً.";
    return;
  }

  const checkButton = $("check");
  checkButton.disabled = true;
  setButtonLabel(checkButton, "جارٍ الفحص…");

  try {
    const response = await fetch("/api/info", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "تعذّر فحص الرابط.");

    $("thumb").src = data.thumbnail || "";
    $("title").textContent = data.title || "فيديو بدون عنوان";
    $("meta").textContent = [data.uploader, fmt(data.duration)].filter(Boolean).join(" • ");
    $("quality").innerHTML = (data.heights || [])
      .map(height => `<option value="v:${height}">فيديو ${height}p</option>`)
      .join("") + `<option value="a:0">صوت فقط (m4a)</option>`;
    $("card").style.display = "block";
  } catch (error) {
    $("err").textContent = error.message || "حدث خطأ، حاول مرة أخرى.";
  } finally {
    checkButton.disabled = false;
    setButtonLabel(checkButton, "فحص الرابط");
  }
};

$("dl").onclick = async () => {
  const [kindCode, height] = $("quality").value.split(":");
  const params = new URLSearchParams({
    url: $("url").value.trim(),
    kind: kindCode === "a" ? "audio" : "video",
    height,
    job_id: makeJobId()
  });
  const jobId = params.get("job_id");
  const downloadButton = $("dl");

  $("err").textContent = "";
  downloadButton.disabled = true;
  setButtonLabel(downloadButton, "جارٍ التنزيل...");
  setProgress({ percentage: 0, message: "جارٍ بدء التنزيل...", downloaded: 0, total: null });

  let polling = true;
  const pollTask = (async () => {
    while (polling) {
      try {
        const response = await fetch(`/api/progress/${jobId}`, { cache: "no-store" });
        if (response.ok) {
          const data = await response.json();
          setProgress({
            percentage: data.percentage,
            message: data.message,
            speed: data.speed,
            downloaded: data.downloaded_bytes,
            total: data.total_bytes,
            eta: data.eta,
            indeterminate: data.percentage === null || data.percentage === undefined
          });
          if (data.status === "failed" || data.status === "ready") break;
        }
      } catch (_) {
        // A transient polling error should not cancel the actual download.
      }
      await sleep(500);
    }
  })();

  try {
    // The original GET /api/download endpoint and its parameters remain intact;
    // job_id is only an optional addition for live progress reporting.
    const response = await fetch("/api/download?" + params.toString());
    polling = false;
    await pollTask;

    if (!response.ok) {
      let errorMessage = "فشل التنزيل.";
      try {
        const data = await response.json();
        errorMessage = data.error || errorMessage;
      } catch (_) { /* Keep fallback message. */ }
      throw new Error(errorMessage);
    }

    const blob = await readDownloadResponse(response);
    const objectUrl = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = getDownloadName(response);
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);

    setProgress({
      percentage: 100,
      message: "اكتمل التنزيل بنجاح.",
      downloaded: blob.size,
      total: blob.size,
      speed: null,
      eta: 0
    });
  } catch (error) {
    $("err").textContent = error.message || "فشل التنزيل.";
    setProgress({
      percentage: null,
      message: "تعذّر إكمال التنزيل.",
      downloaded: null,
      total: null,
      speed: null,
      eta: null,
      indeterminate: true
    });
  } finally {
    polling = false;
    await pollTask;
    downloadButton.disabled = false;
    setButtonLabel(downloadButton, "نزّل الفيديو");
  }
};
