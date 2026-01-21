// Update this to your deployed API URL:
const API_BASE = "https://ai-phishing-threat-detection-production.up.railway.app";

let autoMode = false;
let lastItemId = null;

Office.onReady(() => {
  const scanBtn = document.getElementById("scanBtn");
  const reportBtn = document.getElementById("reportBtn");
  const quarantineBtn = document.getElementById("quarantineBtn");
  const autoToggle = document.getElementById("autoToggle");

  if (scanBtn) scanBtn.addEventListener("click", scanCurrentEmail);
  if (reportBtn) reportBtn.addEventListener("click", reportCurrentEmail);
  if (quarantineBtn) quarantineBtn.addEventListener("click", quarantineCurrentEmail);

  if (autoToggle) {
    autoToggle.addEventListener("change", (e) => {
      autoMode = e.target.checked;
      setStatus(autoMode ? "Auto mode enabled." : "Auto mode disabled.");

      if (autoMode) {
        hookItemChanged();
        scanCurrentEmail();
      }
    });
  }

  setStatus("Ready.");
});

function setStatus(msg) {
  document.getElementById("status").textContent = msg;
}

function setVerdictUI(verdictText) {
  const el = document.getElementById("verdict");
  el.textContent = verdictText ?? "—";
  el.classList.remove("green", "orange", "red", "neutral");
  el.classList.add(colorClassForVerdict(verdictText));
}

function setScoreUI(id, score) {
  const el = document.getElementById(id);
  if (score === null || score === undefined || Number.isNaN(score)) {
    el.textContent = "—";
    el.classList.remove("green", "orange", "red");
    el.classList.add("neutral");
    return;
  }
  const v = Math.max(0, Math.min(100, Number(score)));
  el.textContent = `${v.toFixed(0)}%`;
  el.classList.remove("green", "orange", "red", "neutral");
  el.classList.add(colorClassForScore(v));
}

function setReasonsUI(reasons, indicators) {
  const ul = document.getElementById("reasons");
  ul.innerHTML = "";
  const allItems = [];

  if (indicators && indicators.length > 0) {
    indicators.forEach(ind => allItems.push({ text: ind, type: "indicator" }));
  }
  if (reasons && reasons.length > 0) {
    reasons.forEach(r => {
      if (r && r.trim()) allItems.push({ text: r, type: "reason" });
    });
  }

  if (allItems.length === 0) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = "No analysis details available.";
    ul.appendChild(li);
    return;
  }

  allItems.forEach(item => {
    const li = document.createElement("li");
    li.textContent = item.text;
    if (item.type === "indicator") li.style.color = "#ff9800";
    ul.appendChild(li);
  });
}

// Align colors with backend confidence (red >= 0.7, orange >= 0.4)
function colorClassForScore(score) {
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

function setQuarantineVisibility(verdict) {
  const section = document.getElementById("quarantineSection");
  const btn = document.getElementById("quarantineBtn");
  const v = (verdict || "").toUpperCase();

  if (v === "SUSPICIOUS" || v === "PHISHING") {
    section.classList.remove("hidden");
    btn.disabled = false;
    btn.classList.remove("success");
    btn.innerHTML = '<span class="btn-icon">🛡️</span> Move to Quarantine';
  } else {
    section.classList.add("hidden");
  }
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
    const attachments = await getAttachmentsMetadata(item);
    setStatus("Sending to analyzer...");

    const res = await fetch(`${API_BASE}/check`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ eml, attachments })
    });

    if (!res.ok) throw new Error(`Analyze failed (${res.status})`);

    const data = await res.json();

    setVerdictUI(data.verdict);
    setScoreUI("aiScore", data.ai_score);
    setScoreUI("sublimeScore", data.sublime_score);
    setReasonsUI(data.reasons || [], data.indicators || []);
    setQuarantineVisibility(data.verdict);

    disableReport(false);
    setStatus("Done.");
  } catch (err) {
    console.error(err);
    setStatus(`Error: ${err.message}`);
    setVerdictUI("—");
    setScoreUI("aiScore", null);
    setScoreUI("sublimeScore", null);
    setReasonsUI([], []);
    setQuarantineVisibility(null);
    disableReport(true);
  }
}

async function reportCurrentEmail() {
  const item = Office.context.mailbox.item;
  if (!item) return;

  setStatus("Reporting...");
  disableReport(true);

  try {
    const eml = await getEmlFromItem(item);
    const attachments = await getAttachmentsMetadata(item);

    const res = await fetch(`${API_BASE}/report`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ eml, attachments })
    });

    if (!res.ok) throw new Error(`Report failed (${res.status})`);

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

// ---- EML extraction with full hardening ----
async function getEmlFromItem(item) {
  try {
    const pseudo = await buildPseudoEml(item);
    const base64 = btoa(unescape(encodeURIComponent(pseudo)));
    return `__BASE64_EML__:${base64}`;
  } catch (err) {
    console.error("Failed to build EML:", err);
    throw new Error("Could not extract email content");
  }
}

async function buildPseudoEml(item) {
  let headers = "";
  try {
    if (typeof item.getAllInternetHeadersAsync === "function") {
      headers = await getAsyncProm(item, item.getAllInternetHeadersAsync, {});
    }
  } catch {}

  let bodyText = "";
  try {
    bodyText = await getAsyncProm(item, item.body.getAsync, { coercionType: Office.CoercionType.Text });
  } catch {}

  let bodyHtml = "";
  try {
    bodyHtml = await getAsyncProm(item, item.body.getAsync, { coercionType: Office.CoercionType.Html });
  } catch {}

  const subject = item.subject || "";
  const from = item.from?.emailAddress || item.from?.displayName || "";
  const to = (item.to || []).map(x => x.emailAddress || x.displayName).join(", ");
  const boundary = "----=_NextPart_" + Date.now().toString(36);

  // Build multipart MIME if we have HTML, otherwise plain text
  let eml = `From: ${from}
To: ${to}
Subject: ${subject}
${headers ? headers.trim() : ""}
MIME-Version: 1.0`;

  if (bodyHtml) {
    eml += `
Content-Type: multipart/alternative; boundary="${boundary}"

--${boundary}
Content-Type: text/plain; charset="utf-8"

${bodyText}

--${boundary}
Content-Type: text/html; charset="utf-8"

${bodyHtml}

--${boundary}--`;
  } else {
    eml += `
Content-Type: text/plain; charset="utf-8"

${bodyText}`;
  }

  // Add attachment placeholders for metadata
  const attachments = await getAttachmentsMetadata(item);
  if (attachments.length > 0) {
    eml += `\n\nAttachments:\n${attachments.map(a => `${a.name} (${a.size} bytes, ${a.contentType})`).join("\n")}`;
  }

  return eml;
}

// ---- Attachment metadata ----
async function getAttachmentsMetadata(item) {
  const atts = item.attachments || [];
  return atts.map(att => ({
    name: att.name || "unknown",
    size: att.size || 0,
    contentType: att.contentType || "application/octet-stream",
    isInline: att.isInline || false
  }));
}

// Helper to promisify Office.js async calls
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

// ---- Quarantine functions unchanged ----
async function quarantineCurrentEmail() {
  const item = Office.context.mailbox.item;
  if (!item) {
    setStatus("No email item found.");
    return;
  }

  const btn = document.getElementById("quarantineBtn");
  btn.disabled = true;
  btn.innerHTML = '<span class="btn-icon">⏳</span> Moving...';
  setStatus("Moving to quarantine...");

  try {
    const itemId = item.itemId;
    if (!itemId) {
      throw new Error("Cannot get email ID");
    }

    await moveToQuarantineEWS(itemId);

    btn.classList.add("success");
    btn.innerHTML = '<span class="btn-icon">✓</span> Quarantined';
    setStatus("Email moved to Quarantine folder.");

    try {
      const eml = await getEmlFromItem(item);
      await fetch(`${API_BASE}/report`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ eml, quarantined: true })
      });
    } catch (reportErr) {
      console.warn("Auto-report after quarantine failed:", reportErr);
    }

  } catch (err) {
    console.error("Quarantine error:", err);
    btn.disabled = false;
    btn.innerHTML = '<span class="btn-icon">🛡️</span> Move to Quarantine';
    setStatus(`Quarantine failed: ${err.message}`);
  }
}

function moveToQuarantineEWS(itemId) {
  return new Promise((resolve, reject) => {
    const ewsId = convertToEwsId(itemId);

    const soapRequest = `<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:xsd="http://www.w3.org/2001/XMLSchema"
               xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"
               xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"
               xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages">
  <soap:Header>
    <t:RequestServerVersion Version="Exchange2013"/>
  </soap:Header>
  <soap:Body>
    <m:MoveItem>
      <m:ToFolderId>
        <t:DistinguishedFolderId Id="junkemail"/>
      </m:ToFolderId>
      <m:ItemIds>
        <t:ItemId Id="${ewsId}"/>
      </m:ItemIds>
    </m:MoveItem>
  </soap:Body>
</soap:Envelope>`;

    Office.context.mailbox.makeEwsRequestAsync(soapRequest, (result) => {
      if (result.status === Office.AsyncResultStatus.Failed) {
        tryAlternativeMove(itemId).then(resolve).catch(reject);
        return;
      }

      const response = result.value;
      if (response.includes("ResponseClass=\"Success\"")) {
        resolve();
      } else if (response.includes("ErrorMoveCopyFailed") || response.includes("ErrorItemNotFound")) {
        reject(new Error("Email may have already been moved or deleted"));
      } else {
        tryAlternativeMove(itemId).then(resolve).catch(reject);
      }
    });
  });
}

function convertToEwsId(itemId) {
  if (itemId.startsWith("AAM")) return itemId;
  return itemId;
}

async function tryAlternativeMove(itemId) {
  return new Promise((resolve, reject) => {
    const item = Office.context.mailbox.item;
    if (Office.context.mailbox.restUrl) {
      moveViaRest(itemId)
        .then(resolve)
        .catch(() => reject(new Error("Could not move email. Please manually move to Junk folder.")));
    } else {
      reject(new Error("Move not supported. Please manually move to Junk folder."));
    }
  });
}

async function moveViaRest(itemId) {
  const restUrl = Office.context.mailbox.restUrl;
  const accessToken = await getAccessToken();

  const response = await fetch(`${restUrl}/v2.0/me/messages/${itemId}/move`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${accessToken}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({ DestinationId: "JunkEmail" })
  });

  if (!response.ok) {
    throw new Error(`REST move failed: ${response.status}`);
  }
}

function getAccessToken() {
  return new Promise((resolve, reject) => {
    Office.context.mailbox.getCallbackTokenAsync({ isRest: true }, (result) => {
      if (result.status === Office.AsyncResultStatus.Succeeded) {
        resolve(result.value);
      } else {
        reject(new Error("Could not get access token"));
      }
    });
  });
}
