// Update this if your Flask server differs:
const API_BASE = "http://127.0.0.1:5000";

let autoMode = false;
let lastItemId = null;

Office.onReady(() => {
  document.getElementById("scanBtn").addEventListener("click", scanCurrentEmail);
  document.getElementById("reportBtn").addEventListener("click", reportCurrentEmail);

  const autoToggle = document.getElementById("autoToggle");
  autoToggle.addEventListener("change", (e) => {
    autoMode = e.target.checked;
    setStatus(autoMode ? "Auto mode enabled." : "Auto mode disabled.");

    if (autoMode) {
      // Scan immediately and also whenever selection changes
      hookItemChanged();
      scanCurrentEmail();
    }
  });

  setStatus("Ready.");
});

function setStatus(msg) {
  const el = document.getElementById("status");
  el.textContent = msg;
}

function setVerdictUI(verdictText) {
  const el = document.getElementById("verdict");
  el.textContent = verdictText ?? "—";

  el.classList.remove("green", "orange", "red", "neutral");
  el.classList.add(colorClassForVerdict(verdictText));
}

function setScoreUI(id, score0to100) {
  const el = document.getElementById(id);
  if (score0to100 === null || score0to100 === undefined || Number.isNaN(score0to100)) {
    el.textContent = "—";
    el.classList.remove("green", "orange", "red");
    el.classList.add("neutral");
    return;
  }

  const v = Math.max(0, Math.min(100, Number(score0to100)));
  el.textContent = `${v.toFixed(0)}%`;
  el.classList.remove("green", "orange", "red", "neutral");
  el.classList.add(colorClassForScore(v));
}

function setReasonsUI(reasons, indicators) {
  const ul = document.getElementById("reasons");
  ul.innerHTML = "";

  const allItems = [];

  // Add indicators first (these are concrete detections)
  if (indicators && indicators.length > 0) {
    for (const ind of indicators) {
      allItems.push({ text: ind, type: "indicator" });
    }
  }

  // Add AI reasoning
  if (reasons && reasons.length > 0) {
    for (const r of reasons) {
      if (r && r.trim()) {
        allItems.push({ text: r, type: "reason" });
      }
    }
  }

  if (allItems.length === 0) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = "No analysis details available.";
    ul.appendChild(li);
    return;
  }

  for (const item of allItems) {
    const li = document.createElement("li");
    li.textContent = item.text;
    if (item.type === "indicator") {
      li.style.color = "#ff9800"; // Orange for indicators
    }
    ul.appendChild(li);
  }
}

function colorClassForScore(score) {
  // You can tweak thresholds:
  if (score >= 70) return "red";
  if (score >= 40) return "orange";
  return "green";
}

function colorClassForVerdict(v) {
  const s = (v || "").toLowerCase();
  if (s.includes("phish")) return "red";
  if (s.includes("susp")) return "orange";
  if (s.includes("safe") || s.includes("legit")) return "green";
  return "neutral";
}

function hookItemChanged() {
  const mailbox = Office.context.mailbox;
  if (!mailbox || !mailbox.addHandlerAsync) return;

  mailbox.addHandlerAsync(Office.EventType.ItemChanged, () => {
    if (!autoMode) return;
    const item = Office.context.mailbox.item;
    const itemId = item?.itemId || null;
    if (itemId && itemId === lastItemId) return;
    lastItemId = itemId;
    scanCurrentEmail();
  });
}

async function scanCurrentEmail() {
  const item = Office.context.mailbox.item;
  if (!item) {
    setStatus("No email item found.");
    return;
  }

  disableReport(true);
  setStatus("Extracting EML...");

  try {
    const eml = await getEmlFromItem(item);
    console.log(eml)

    setStatus("Sending to local analyzer...");
    const res = await fetch(`${API_BASE}/check`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ eml })
    });

    if (!res.ok) {
      const txt = await res.text();
      throw new Error(`Analyze failed (${res.status}): ${txt}`);
    }

    const data = await res.json();

    // expected fields from backend
    setVerdictUI(data.verdict);
    setScoreUI("aiScore", data.ai_score);
    setScoreUI("sublimeScore", data.sublime_score);
    setReasonsUI(data.reasons || [], data.indicators || []);

    disableReport(false);
    setStatus("Done.");
  } catch (err) {
    console.error(err);
    setStatus(`Error: ${err.message}`);
    setVerdictUI("—");
    setScoreUI("aiScore", null);
    setScoreUI("sublimeScore", null);
    setReasonsUI([], []);
    disableReport(true);
  }
}

async function reportCurrentEmail() {
  const item = Office.context.mailbox.item;
  if (!item) return;

  setStatus("Reporting to Teams...");
  disableReport(true);

  try {
    const eml = await getEmlFromItem(item);

    const res = await fetch(`${API_BASE}/report`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ eml })
    });

    if (!res.ok) {
      const txt = await res.text();
      throw new Error(`Report failed (${res.status}): ${txt}`);
    }

    setStatus("Reported successfully.");
  } catch (err) {
    console.error(err);
    setStatus(`Report error: ${err.message}`);
  } finally {
    disableReport(false);
  }
}

function disableReport(disabled) {
  document.getElementById("reportBtn").disabled = !!disabled;
}

// ---- EML extraction ----
// Preferred: getAsFileAsync (produces .eml in read mode when supported)
// Fallback: build RFC822-like content using headers + body
function getEmlFromItem(item) {
  return new Promise((resolve, reject) => {
    if (typeof item.getAsFileAsync === "function") {
      item.getAsFileAsync((result) => {
        if (result.status !== Office.AsyncResultStatus.Succeeded) {
          // fallback instead of failing
          buildPseudoEml(item).then(resolve).catch(reject);
          return;
        }
        const file = result.value;
        file.getSliceAsync(0, (sliceResult) => {
          try {
            if (sliceResult.status !== Office.AsyncResultStatus.Succeeded) {
              file.closeAsync();
              buildPseudoEml(item).then(resolve).catch(reject);
              return;
            }
            const slice = sliceResult.value;
            const base64 = slice.data; // base64 encoded
            file.closeAsync();
            // Backend accepts raw RFC822 text, so decode base64 in backend OR here.
            // We'll send base64-wrapped EML to backend to decode safely:
            resolve(`__BASE64_EML__:${base64}`);
          } catch (e) {
            try { file.closeAsync(); } catch {}
            buildPseudoEml(item).then(resolve).catch(reject);
          }
        });
      });
    } else {
      buildPseudoEml(item).then(resolve).catch(reject);
    }
  });
}

function getAsyncProm(item, method, opts) {
  return new Promise((resolve, reject) => {
    method.call(item, opts, (res) => {
      if (res.status !== Office.AsyncResultStatus.Succeeded) {
        reject(new Error(res.error?.message || "Office.js call failed"));
      } else {
        resolve(res.value);
      }
    });
  });
}

async function buildPseudoEml(item) {
  // Collect headers + body; this is not perfect EML but works for analysis services.
  let headers = "";
  try {
    if (typeof item.getAllInternetHeadersAsync === "function") {
      headers = await getAsyncProm(item, item.getAllInternetHeadersAsync, {});
    }
  } catch {}

  let bodyText = "";
  try {
    // Use text body; switch to HTML if you prefer coercionType: Html
    bodyText = await getAsyncProm(item, item.body.getAsync, { coercionType: Office.CoercionType.Text });
  } catch {}

  const subject = item.subject || "";
  const from = item.from?.emailAddress || item.from?.displayName || "";
  const to = (item.to || []).map(x => x.emailAddress || x.displayName).join(", ");

  const pseudo =
`From: ${from}
To: ${to}
Subject: ${subject}
${headers ? headers.trim() : ""}
MIME-Version: 1.0
Content-Type: text/plain; charset="utf-8"

${bodyText}`;

  return pseudo;
}
