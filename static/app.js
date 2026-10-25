// Crossbill page logic: load invoice -> review (AG Grid) -> decide (server rules) -> approve -> pay.
// The browser never computes tax; every change re-calls /api/decide.
(() => {
  "use strict";

  const { dryRun, categories, journalHeader } = window.CROSSBILL;
  const $ = (id) => document.getElementById(id);

  const state = {
    aiInvoice: null,   // invoice exactly as the AI read it (never edited)
    invoice: null,     // reviewed copy: categories may be changed by the human
    source: "",        // text shown under "What the AI read"
    decision: null,
    payout: null,      // {payout_batch_id, batch_status, dry_run}
    busy: false,
  };
  let decideSeq = 0;
  let grid = null;
  let pollTimer = null;
  let pollSeq = 0;

  // after approval the payout status is checked automatically until it settles
  const POLL_INTERVAL_MS = 5000;
  const POLL_LIMIT_MS = 3 * 60 * 1000;
  const FINAL_STATUSES = new Set(["SUCCESS", "DENIED", "CANCELED", "FAILED", "RETURNED", "BLOCKED", "REVERSED", "REFUNDED"]);

  // ---------- formatting ----------
  const intFmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
  const fxFmt = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const jpy = (n) => (n == null ? "" : `JPY ${intFmt.format(n)}`);
  const money = (cur, n) => (cur === "JPY" ? jpy(n) : `${cur} ${fxFmt.format(n)}`);
  const pct = (rate) => `${parseFloat((rate * 100).toFixed(4))}%`;
  const label = (cat) => (cat ? cat.replaceAll("_", " ") : "");
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function basisText(line, country) {
    if (line.withholding_basis === "domestic") return `Domestic ${pct(line.withholding_rate)}`;
    if (line.withholding_basis === "treaty") return `Japan-${country} treaty ${pct(line.withholding_rate)} (form on file)`;
    return "No withholding";
  }

  // ---------- API ----------
  async function api(path, options = {}) {
    const res = await fetch(path, options);
    const isJson = (res.headers.get("content-type") || "").includes("application/json");
    const body = isJson ? await res.json() : await res.blob();
    if (!res.ok) {
      const err = new Error((isJson && body.error) || `Request failed (${res.status})`);
      err.status = res.status;
      err.body = isJson ? body : null;
      throw err;
    }
    return body;
  }
  const postJson = (path, data) =>
    api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });

  function showStatus(text, kind = "info") {
    const el = $("status");
    el.textContent = text;
    el.className = "mt-2 text-sm " + (kind === "error" ? "text-black font-semibold border-l-4 border-black pl-2"
      : kind === "ai" ? "ai-text" : "text-gray-600");
    el.classList.toggle("hidden", !text);
  }

  // ---------- progress ----------
  function renderSteps() {
    const reviewed = $("confirm").checked;
    let current = 1;
    const done = new Set();
    if (state.invoice && !state.busy) {
      done.add(1);
      if (state.decision) done.add(3);
      current = 2;
      if (reviewed) { done.add(2); current = 4; }
      if (state.payout) {
        done.add(4);
        current = 5;
        // a successful payout means approval and payment are both complete
        if (state.payout.batch_status === "SUCCESS" || state.payout.item_status === "SUCCESS") {
          done.add(4);
          done.add(5);
        }
      }
    }
    document.querySelectorAll("#steps .step").forEach((li) => {
      const n = Number(li.dataset.step);
      li.classList.toggle("current", n === current && !done.has(n));
      li.classList.toggle("done", done.has(n));
      li.setAttribute("aria-current", n === current ? "step" : "false");
    });
  }

  // ---------- samples ----------
  async function loadSampleList() {
    const box = $("sample-list");
    try {
      const samples = await api("/api/samples");
      box.innerHTML = '<span class="text-sm font-semibold mr-1">Samples</span>';
      for (const s of samples) {
        const group = document.createElement("span");
        group.className = "inline-flex items-center gap-1 mr-2";
        const amount = s.amount != null ? ` · ${money(s.currency, s.amount)}` : "";
        group.innerHTML =
          `<button type="button" class="btn btn-plain" data-load="${esc(s.id)}" ${s.has_json ? "" : "disabled"}
             title="Uses the pre-extracted JSON (no AI call)">Load sample ${esc(s.title)}${esc(amount)}</button>
           <button type="button" class="btn btn-ai" data-live="${esc(s.id)}"
             title="Send this PDF to Claude now">Run live AI extraction</button>`;
        box.appendChild(group);
      }
    } catch (e) {
      box.innerHTML = `<span class="text-sm">Could not load samples: ${esc(e.message)}</span>`;
    }
  }

  async function loadSample(id) {
    showStatus("Loading sample...");
    try {
      const data = await api(`/api/samples/${encodeURIComponent(id)}`);
      setInvoice(data.invoice, data.pdf_url, "Pre-extracted by Claude earlier (sample JSON, no live AI call).");
      showStatus("");
    } catch (e) {
      showStatus(e.message, "error");
    }
  }

  async function extractLive(path, pdfUrl, formData) {
    state.busy = true;
    renderSteps();
    setBusy(true);
    showStatus("Reading the invoice with Claude...", "ai");
    try {
      const inv = await api(path, { method: "POST", body: formData });
      setInvoice(inv, pdfUrl, "Read live by Claude just now.");
      showStatus("");
    } catch (e) {
      showStatus(e.message, "error");
    } finally {
      state.busy = false;
      setBusy(false);
      renderSteps();
    }
  }

  function setBusy(on) {
    document.querySelectorAll("#sample-list button, #upload-form button").forEach((b) => { b.disabled = on; });
  }

  // ---------- invoice ----------
  function setInvoice(inv, pdfUrl, source) {
    state.aiInvoice = structuredClone(inv);
    state.invoice = structuredClone(inv);
    state.source = source;
    state.decision = null;
    state.payout = null;
    stopPolling();
    $("confirm").checked = false;
    showApproveError("");
    renderPayout();

    // "Pay in": JPY or the invoice currency
    const sel = document.querySelector('[name="pay_currency"]');
    const prev = sel.value;
    sel.innerHTML = '<option value="JPY">JPY</option>' +
      (inv.currency && inv.currency !== "JPY" ? `<option value="${esc(inv.currency)}">${esc(inv.currency)} (as invoiced)</option>` : "");
    sel.value = [...sel.options].some((o) => o.value === prev) ? prev : "JPY";

    $("empty").classList.add("hidden");
    $("workspace").classList.remove("hidden");
    if ($("pdf").getAttribute("src") !== pdfUrl) $("pdf").src = pdfUrl;
    renderAiRead();
    initGrid();
    decide();
  }

  function renderAiRead() {
    const inv = state.aiInvoice;
    $("ai-source").textContent = state.source;
    const fields = [
      ["Vendor", inv.vendor_name],
      ["Country", inv.vendor_country],
      ["Vendor type", (inv.vendor_is_individual ? "Individual (sole proprietor)" : "Company") + " (AI's best guess)"],
      ["Invoice number", inv.invoice_number],
      ["Issue date", inv.issue_date],
      ["Due date", inv.due_date],
      ["Total", inv.total != null ? money(inv.currency, inv.total) : ""],
      ["Tax shown", inv.tax_shown != null ? money(inv.currency, inv.tax_shown) : ""],
      ["Payment email", inv.payment_email],
      ["Work location", inv.work_location],
      ["Notes", inv.notes_summary],
    ];
    $("ai-fields").innerHTML = fields
      .filter(([, v]) => v !== undefined && v !== null && v !== "")
      .map(([k, v]) => `<dt class="text-gray-500">${esc(k)}</dt><dd class="min-w-0 break-words">${esc(v)}</dd>`)
      .join("");
    $("ai-lines").innerHTML = inv.line_items.map((li) => `
      <li class="border-l-2 pl-3" style="border-color: var(--ai)">
        <div class="font-medium">${esc(li.description)}</div>
        <div class="text-xs text-gray-600 tabular-nums">${esc(money(inv.currency, li.amount))}
          · AI category: <span class="ai-text font-medium">${esc(label(li.category))}</span></div>
        <blockquote class="text-xs text-gray-700 mt-0.5">${esc(li.category_reason)}</blockquote>
      </li>`).join("");
  }

  // ---------- settings ----------
  function readSettings() {
    const f = $("settings-form");
    return {
      payment_date: f.payment_date.value,
      pay_currency: f.pay_currency.value,
      ttm: Number(f.ttm.value),
      ttb: Number(f.ttb.value),
      pay_rate: null,
      treaty_form: f.treaty_form.checked,
    };
  }

  function settingsValid() {
    const s = readSettings();
    return Boolean(s.payment_date) && s.ttm > 0 && s.ttb > 0;
  }

  // ---------- decision ----------
  async function decide() {
    if (!state.invoice) return;
    if (!settingsValid()) { showStatus("Enter a payment date and positive TTM / TTB rates.", "error"); return; }
    const seq = ++decideSeq;
    try {
      const d = await postJson("/api/decide", { invoice: state.invoice, settings: readSettings() });
      if (seq !== decideSeq) return; // a newer request is in flight
      state.decision = d;
      showStatus("");
      renderDecision();
    } catch (e) {
      if (seq === decideSeq) showStatus(e.message, "error");
    }
  }

  function overrides() {
    return state.aiInvoice.line_items
      .map((ai, i) => ({ description: ai.description, from: ai.category, to: state.invoice.line_items[i].category }))
      .filter((o) => o.from !== o.to);
  }

  function renderDecision() {
    const d = state.decision;
    $("pay-amount").textContent = money(d.pay_currency, d.pay_amount);
    $("total-jpy").textContent = jpy(d.total_jpy);
    $("wht-jpy").textContent = jpy(d.withholding_jpy);
    $("fx-used").textContent = d.pay_currency === "JPY"
      ? `${fxFmt.format(d.fx_rate_payment)} JPY/${d.currency} (TTM)`
      : `TTB ${fxFmt.format(d.fx_rate_ttb)} for withholding`;
    $("wht-basis").innerHTML = d.lines.map((l) =>
      `<li><span class="text-gray-500">Withholding on</span> ${esc(l.description)}: <strong>${esc(basisText(l, d.vendor_country))}</strong></li>`
    ).join("");

    // grid
    grid.setGridOption("rowData", d.lines.map((l, i) => ({
      idx: i,
      description: l.description,
      amount_fx: l.amount_fx,
      amount_jpy: l.amount_jpy,
      category: state.invoice.line_items[i].category,
      ai_category: state.aiInvoice.line_items[i].category,
      consumption_tax: l.consumption_tax,
      withholding_jpy: l.withholding_jpy,
      account: l.account,
      ai_reason: l.ai_reason,
    })));
    const ov = overrides();
    $("overrides").classList.toggle("hidden", !ov.length);
    $("overrides").textContent = ov.length
      ? "Your corrections (sent with the approval): " + ov.map((o) => `${o.description}: ${label(o.from)} → ${label(o.to)}`).join("; ")
      : "";

    // flags
    $("flags").innerHTML = d.review_flags.length
      ? d.review_flags.map((f) => `<li>${esc(f)}</li>`).join("")
      : '<li class="list-none -ml-5 text-gray-600">Nothing to flag.</li>';

    // journal
    $("journal-head").innerHTML = journalHeader.map((h) => `<th>${esc(h)}</th>`).join("");
    $("journal-body").innerHTML = d.journal_rows.map((r) => "<tr>" + r.map((c, j) =>
      `<td class="${j === 2 || j === 5 ? "num" : ""}">${esc(typeof c === "number" ? intFmt.format(c) : c)}</td>`
    ).join("") + "</tr>").join("");

    $("receiver-note").textContent =
      `Vendor payment email on the invoice: ${d.payment_email}. In the sandbox the payout goes to the demo test account instead.`;
    renderApprove();
    renderSteps();
  }

  // ---------- grid ----------
  // Native <select> editor: opens with one click (AG Grid Community's select needs a second click).
  class CategoryEditor {
    init(p) {
      this.select = document.createElement("select");
      this.select.className = "w-full h-full text-xs bg-white";
      this.select.setAttribute("aria-label", "Category");
      for (const c of categories) this.select.add(new Option(label(c), c, false, c === p.value));
      this.select.addEventListener("change", () => p.stopEditing());
    }
    getGui() { return this.select; }
    afterGuiAttached() {
      this.select.focus();
      try { this.select.showPicker(); } catch { /* older browsers: focused select, arrow keys work */ }
    }
    getValue() { return this.select.value; }
  }

  function initGrid() {
    if (grid) return;
    const num = { type: "rightAligned", cellClass: "cell-num" };
    grid = agGrid.createGrid($("grid"), {
      domLayout: "autoHeight",
      getRowId: (p) => String(p.data.idx),
      defaultColDef: { resizable: true, sortable: false, wrapText: true, autoHeight: true, suppressMovable: true },
      singleClickEdit: true,
      stopEditingWhenCellsLoseFocus: true,
      columnDefs: [
        { field: "description", headerName: "Description", pinned: "left", width: 170 },
        { field: "amount_fx", headerName: "Amount (FX)", width: 100, ...num,
          valueFormatter: (p) => money(state.invoice.currency, p.value) },
        { field: "amount_jpy", headerName: "Amount (JPY)", width: 105, ...num, valueFormatter: (p) => intFmt.format(p.value) },
        { field: "category", headerName: "Category (edit)", width: 190, editable: () => !state.payout,
          cellEditor: CategoryEditor,
          valueFormatter: (p) => label(p.value),
          cellClass: "cell-category",
          equals: () => false, // always re-render so the "edited by you" marker follows the AI category
          cellRenderer: (p) => {
            const edited = p.data.category !== p.data.ai_category;
            return `<span>${esc(label(p.value))}</span>` +
              (edited ? `<span class="edited-marker" title="AI said: ${esc(label(p.data.ai_category))}">edited by you</span>` : "");
          } },
        { field: "consumption_tax", headerName: "Consumption tax", width: 170 },
        { field: "withholding_jpy", headerName: "Withholding (JPY)", width: 105, ...num, valueFormatter: (p) => intFmt.format(p.value) },
        { field: "account", headerName: "Account", width: 140 },
        { field: "ai_reason", headerName: "AI reason", width: 280, cellClass: "text-gray-700" },
      ],
      onCellValueChanged: (p) => {
        if (p.colDef.field !== "category" || p.newValue === p.oldValue) return;
        state.invoice.line_items[p.data.idx].category = p.newValue;
        decide();
      },
    });
  }

  // ---------- journal CSV ----------
  async function downloadCsv() {
    if (!state.decision) return;
    try {
      const blob = await postJson("/api/journal.csv", state.decision);
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `journal_${state.decision.invoice_number}.csv`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    } catch (e) {
      showStatus(e.message, "error");
    }
  }

  // ---------- approve and pay ----------
  const approverName = () => $("approver").value.trim();

  function renderApprove() {
    const ok = Boolean(state.decision) && $("confirm").checked && approverName() !== "" && !state.payout;
    $("approve").disabled = !ok;
    $("approve-hint").textContent = state.payout ? "Approved." : ok ? "" : "Tick the checkbox and enter the approver name.";
  }

  function showApproveError(text) {
    $("approve-error").textContent = text;
    $("approve-error").classList.toggle("hidden", !text);
  }

  async function approve() {
    $("approve").disabled = true;
    showApproveError("");
    showStatus(dryRun ? "Recording the approval (simulated payout)..." : "Sending the payout to PayPal...");
    try {
      const res = await postJson("/api/approve", {
        invoice: state.invoice,
        settings: readSettings(),
        decision: state.decision,
        approver: approverName(),
        overrides: overrides(),
      });
      state.payout = res;
      renderPayout();
      startPolling();
      showStatus(res.dry_run ? "Approved. Simulated payout recorded in the audit log." : "Approved. Payout sent to PayPal.");
    } catch (e) {
      showStatus("");
      const pp = e.body && e.body.paypal_error;
      showApproveError(pp ? `PayPal error ${pp.name}: ${pp.message} Nothing was paid.` : `Approval failed: ${e.message}`);
    }
    renderApprove();
    renderSteps();
  }

  function renderPayout() {
    const p = state.payout;
    // after approval the page shows exactly what was paid: no more edits to this invoice
    for (const el of $("settings-form").elements) if (!el.dataset.sync) el.disabled = Boolean(p);
    $("confirm").disabled = Boolean(p);
    $("payout").classList.toggle("hidden", !p);
    if (!p) return;
    $("batch-id").textContent = p.payout_batch_id;
    $("batch-status").textContent = p.batch_status + (p.dry_run ? " (simulated)" : "");
  }

  function payoutSettled() {
    const p = state.payout;
    return Boolean(p) && (FINAL_STATUSES.has(p.batch_status) || FINAL_STATUSES.has(p.item_status));
  }

  async function refreshStatus() {
    if (!state.payout) return;
    const batchId = state.payout.payout_batch_id;
    try {
      const res = await api(`/api/payout/${encodeURIComponent(batchId)}`);
      if (!state.payout || state.payout.payout_batch_id !== batchId) return; // another invoice was loaded meanwhile
      state.payout = { ...state.payout, batch_status: res.batch_status, item_status: res.item_status };
      renderPayout();
      renderSteps();
    } catch (e) {
      showStatus(e.message, "error");
    }
  }

  function stopPolling() {
    pollSeq += 1; // any check still in flight sees a newer seq and stops
    clearTimeout(pollTimer);
    pollTimer = null;
  }

  // check every 5 seconds for up to 3 minutes; stop as soon as the status is final
  function startPolling() {
    stopPolling();
    if (!state.payout || payoutSettled()) return;
    const seq = pollSeq;
    const deadline = Date.now() + POLL_LIMIT_MS;
    const tick = async () => {
      await refreshStatus();
      if (seq !== pollSeq) return;
      if (payoutSettled()) { pollTimer = null; return; }
      if (Date.now() + POLL_INTERVAL_MS > deadline) {
        pollTimer = null;
        showStatus("The payout is still pending after 3 minutes. Use Refresh status to check again.");
        return;
      }
      pollTimer = setTimeout(tick, POLL_INTERVAL_MS);
    };
    pollTimer = setTimeout(tick, POLL_INTERVAL_MS);
  }

  // ---------- wiring ----------
  function wire() {
    const today = new Date();
    const iso = new Date(today.getTime() - today.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
    $("settings-form").payment_date.value = iso;

    $("sample-list").addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      if (btn.dataset.load) loadSample(btn.dataset.load);
      if (btn.dataset.live) {
        const id = btn.dataset.live;
        extractLive(`/api/samples/${encodeURIComponent(id)}/extract`, `/api/samples/${encodeURIComponent(id)}/pdf`);
      }
    });

    $("upload-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const file = $("upload-file").files[0];
      if (!file) { showStatus("Choose a PDF file first.", "error"); return; }
      const fd = new FormData();
      fd.append("file", file);
      extractLive("/api/extract", URL.createObjectURL(file), fd);
    });

    let timer = null;
    $("settings-form").addEventListener("input", (e) => {
      if (e.target.dataset.sync) return; // approver name does not change the decision
      clearTimeout(timer);
      timer = setTimeout(decide, 250);
    });
    $("settings-form").addEventListener("submit", (e) => e.preventDefault());

    // the approver name appears in Settings and in the Approve card; keep both in sync
    document.querySelectorAll('[data-sync="approver"]').forEach((input) => input.addEventListener("input", () => {
      document.querySelectorAll('[data-sync="approver"]').forEach((other) => { if (other !== input) other.value = input.value; });
      renderApprove();
    }));
    $("confirm").addEventListener("change", () => { renderApprove(); renderSteps(); });
    $("approve").addEventListener("click", approve);
    $("refresh-status").addEventListener("click", refreshStatus);
    $("download-csv").addEventListener("click", downloadCsv);
  }

  wire();
  renderSteps();
  loadSampleList();
  // deep link for demos: /?sample=invoice_B_logo_copyright
  const linked = new URLSearchParams(location.search).get("sample");
  if (linked) loadSample(linked);
})();
