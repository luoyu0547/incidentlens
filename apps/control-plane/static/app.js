/**
 * IncidentLens Dashboard — live investigation updates via EventSource.
 *
 * Connects to SSE endpoint and updates DOM with investigation state.
 * Does NOT show thinking process — only observable outputs.
 */

const API_BASE = window.location.origin;

// Empty state text for evaluation comparison (尚无实际运行结果)
const EVAL_EMPTY_STATE = '尚无实际运行结果';

let eventSource = null;
let incidentId = null;
let selectedCase = null; // currently selected case in editor

// ---- HTML escaping to prevent XSS ----
function escapeHtml(str) {
    if (typeof str !== 'string') return str;
    return str
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// ---- JSON fetch helper ----
async function apiJson(path, options = {}) {
    const response = await fetch(`${API_BASE}${path}`, {
        ...options,
        headers: {
            'Content-Type': 'application/json',
            ...(options.headers || {}),
        },
    });
    const body = response.status === 204 ? null : await response.json();
    if (!response.ok) {
        const error = new Error(body?.detail || `HTTP ${response.status}`);
        error.status = response.status;
        error.body = body;
        throw error;
    }
    return body;
}

// ---- DOM references ----
const startForm = document.getElementById('start-form');
const alertStatus = document.getElementById('alert-status');
const timeline = document.getElementById('timeline');
const hypothesesEl = document.getElementById('hypotheses');
const toolSummary = document.getElementById('tool-summary');
const evidenceEl = document.getElementById('evidence');
const casesEl = document.getElementById('cases');
const reportEl = document.getElementById('report');
const confirmBtn = document.getElementById('confirm-btn');
const rejectBtn = document.getElementById('reject-btn');
const confirmStatus = document.getElementById('confirm-status');

// ---- Governance DOM references ----
const reviewQueueList = document.getElementById('review-queue-list');
const reviewQueueStatus = document.getElementById('review-queue-status');
const caseEditForm = document.getElementById('case-edit-form');
const caseIdInput = document.getElementById('case-id');
const caseRevisionInput = document.getElementById('case-revision');
const editorStatus = document.getElementById('editor-status');
const reviewActorInput = document.getElementById('review-actor');
const reviewReasonInput = document.getElementById('review-reason');
const searchForm = document.getElementById('search-form');
const searchResults = document.getElementById('search-results');
const searchStatus = document.getElementById('search-status');
const historyCaseIdInput = document.getElementById('history-case-id');
const loadHistoryBtn = document.getElementById('load-history-btn');
const historyList = document.getElementById('history-list');
const historyStatus = document.getElementById('history-status');
const exportInvestigationBtn = document.getElementById('export-investigation-btn');
const exportStatus = document.getElementById('export-status');
const evalScenarioSelect = document.getElementById('eval-scenario');
const loadEvalBtn = document.getElementById('load-eval-btn');
const evalTableBody = document.getElementById('eval-table-body');
const evalEmptyState = document.getElementById('eval-empty-state');
const evalStatus = document.getElementById('eval-status');

// ---- Utility ----
function setStatus(el, msg, type) {
    el.textContent = msg;
    el.className = `status-box ${type || ''}`;
}

// ---- Start investigation ----
startForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const formData = new FormData(startForm);
    const body = {
        service: formData.get('service'),
        error_rate: parseFloat(formData.get('error_rate')) || null,
        symptom: formData.get('symptom') || null,
    };

    alertStatus.textContent = 'Starting investigation...';
    alertStatus.className = 'status-box';

    try {
        const resp = await fetch(`${API_BASE}/api/investigations/start`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        incidentId = data.incident_id;
        alertStatus.textContent = `Investigation started: ${incidentId}`;
        alertStatus.className = 'status-box success';

        // Connect to SSE
        connectSSE(incidentId);

        // Run rounds automatically
        runRounds(incidentId, data.max_rounds || 8);
    } catch (err) {
        alertStatus.textContent = `Error: ${err.message}`;
        alertStatus.className = 'status-box error';
    }
});

// ---- SSE connection ----
function connectSSE(incId) {
    if (eventSource) {
        eventSource.close();
    }
    // Clear previous data
    timeline.innerHTML = '';
    hypothesesEl.innerHTML = '';
    toolSummary.innerHTML = '';
    evidenceEl.innerHTML = '';
    casesEl.innerHTML = '';
    reportEl.innerHTML = '';
    confirmBtn.disabled = true;
    rejectBtn.disabled = true;

    eventSource = new EventSource(`${API_BASE}/api/investigations/${incId}/events`);

    eventSource.addEventListener('state_changed', (e) => {
        const data = JSON.parse(e.data);
        addTimelineEntry('state_changed', data);
        updateStatus(data);
    });

    eventSource.addEventListener('tool_called', (e) => {
        const data = JSON.parse(e.data);
        addTimelineEntry('tool_called', data);
        addToolEntry(data);
    });

    eventSource.addEventListener('evidence_recorded', (e) => {
        const data = JSON.parse(e.data);
        addTimelineEntry('evidence_recorded', data);
        addEvidenceEntry(data);
    });

    eventSource.addEventListener('report_ready', (e) => {
        const data = JSON.parse(e.data);
        addTimelineEntry('report_ready', data);
        renderReport(data);
        confirmBtn.disabled = false;
        rejectBtn.disabled = false;
    });

    eventSource.onerror = () => {
        // Connection closed or errored — this is normal when investigation ends
    };
}

// ---- Run investigation rounds ----
async function runRounds(incId, maxRounds) {
    for (let round = 0; round < maxRounds; round++) {
        try {
            const resp = await fetch(`${API_BASE}/api/investigations/${incId}/round`, {
                method: 'POST',
            });
            const data = await resp.json();

            if (data.status === 'report_ready' || data.status === 'needs_more_evidence') {
                if (data.status === 'report_ready' && data.report) {
                    renderReport(data.report);
                }
                break;
            }

            // Small delay between rounds for readability
            await new Promise(r => setTimeout(r, 500));
        } catch (err) {
            console.error('Round error:', err);
            break;
        }
    }
}

// ---- UI update functions ----
function addTimelineEntry(eventType, data) {
    const entry = document.createElement('div');
    entry.className = `timeline-entry ${eventType}`;
    entry.innerHTML = `
        <div class="event-type">${escapeHtml(eventType.replace('_', ' '))}</div>
        <div class="event-data">${escapeHtml(formatEventData(eventType, data))}</div>
    `;
    timeline.appendChild(entry);
    timeline.scrollTop = timeline.scrollHeight;
}

function formatEventData(eventType, data) {
    switch (eventType) {
        case 'state_changed':
            return `Status: ${data.status || ''} | Round: ${data.round || ''} | Phase: ${data.phase || ''}`;
        case 'tool_called':
            return `Tool: ${data.tool || ''} | Args: ${JSON.stringify(data.args || {})}`;
        case 'evidence_recorded':
            return `Source: ${data.source_tool || ''} | Content: ${JSON.stringify(data.content || {}).substring(0, 100)}`;
        case 'report_ready':
            return `Root Cause: ${data.root_cause || 'Identified'}`;
        default:
            return JSON.stringify(data);
    }
}

function updateStatus(data) {
    if (data.status) {
        const statusEl = document.createElement('div');
        statusEl.className = `status-badge ${escapeHtml(data.status)}`;
        statusEl.textContent = data.status.replace('_', ' ');
        alertStatus.textContent = '';
        alertStatus.appendChild(statusEl);
    }
}

function addToolEntry(data) {
    const entry = document.createElement('div');
    entry.className = 'tool-entry';
    entry.innerHTML = `<span class="tool-name">${escapeHtml(data.tool || 'unknown')}</span> — ${escapeHtml(JSON.stringify(data.args || {}))}`;
    toolSummary.appendChild(entry);
}

function addEvidenceEntry(data) {
    const entry = document.createElement('div');
    entry.className = 'evidence-entry';
    entry.innerHTML = `<span class="evidence-source">${escapeHtml(data.source_tool || 'unknown')}</span>: ${escapeHtml(JSON.stringify(data.content || {}).substring(0, 150))}`;
    evidenceEl.appendChild(entry);
}

function renderReport(data) {
    reportEl.innerHTML = `
        <div class="root-cause">${escapeHtml(data.root_cause || 'No root cause identified')}</div>
        <div class="findings">
            <h3>Findings</h3>
            ${(data.findings || []).map(f => `
                <div class="finding">
                    <strong>${escapeHtml(f.source_tool || 'Tool')}</strong>: ${escapeHtml(JSON.stringify(f.content || {}).substring(0, 200))}
                </div>
            `).join('')}
        </div>
    `;
}

// ---- Confirmation ----
confirmBtn.addEventListener('click', async () => {
    if (!incidentId) return;
    confirmStatus.textContent = 'Confirming...';
    try {
        const resp = await fetch(`${API_BASE}/api/cases`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                status: 'human_verified',
                service: document.getElementById('service').value,
                symptom: document.getElementById('symptom').value || 'investigated',
            }),
        });
        const data = await resp.json();
        confirmStatus.textContent = `Case saved (ID: ${data.case_id})`;
        confirmStatus.className = 'status-box success';
        confirmBtn.disabled = true;
        rejectBtn.disabled = true;
    } catch (err) {
        confirmStatus.textContent = `Error: ${err.message}`;
        confirmStatus.className = 'status-box error';
    }
});

rejectBtn.addEventListener('click', () => {
    confirmStatus.textContent = 'Findings rejected. Investigation may continue.';
    confirmStatus.className = 'status-box error';
    confirmBtn.disabled = true;
    rejectBtn.disabled = true;
});

// ============================================================
// GOVERNANCE FUNCTIONS
// ============================================================

// ---- Load Review Queue ----
async function loadReviewQueue() {
    setStatus(reviewQueueStatus, 'Loading review queue...', '');
    try {
        const data = await apiJson('/api/cases?status=agent_generated&limit=50');
        const draftData = await apiJson('/api/cases?status=draft&limit=50');
        const allCases = [...(data.cases || []), ...(draftData.cases || [])];
        reviewQueueList.innerHTML = '';
        if (allCases.length === 0) {
            reviewQueueList.innerHTML = '<p class="empty-hint">No cases pending review.</p>';
            setStatus(reviewQueueStatus, '', '');
            return;
        }
        allCases.forEach(c => {
            const card = document.createElement('div');
            card.className = 'queue-card';
            card.dataset.caseId = c.id;
            card.innerHTML = `
                <div class="card-header">
                    <span class="card-id">#${escapeHtml(String(c.id))}</span>
                    <span class="card-status status-badge ${escapeHtml(c.status)}">${escapeHtml(c.status)}</span>
                </div>
                <div class="card-symptom">${escapeHtml(c.symptom)}</div>
                <div class="card-services">${escapeHtml((c.affected_services || []).join(', '))}</div>
                <div class="card-actions">
                    <button class="btn-select-case" data-case-id="${escapeHtml(String(c.id))}">Edit</button>
                </div>
            `;
            card.querySelector('.btn-select-case').addEventListener('click', () => selectCaseForEdit(c));
            reviewQueueList.appendChild(card);
        });
        setStatus(reviewQueueStatus, `Loaded ${allCases.length} case(s).`, 'success');
    } catch (err) {
        setStatus(reviewQueueStatus, `Error: ${escapeHtml(err.message)}`, 'error');
    }
}

// ---- Select Case for Editing ----
function selectCaseForEdit(c) {
    selectedCase = c;
    caseIdInput.value = c.id;
    caseRevisionInput.value = c.revision;
    document.getElementById('edit-symptom').value = c.symptom || '';
    document.getElementById('edit-services').value = (c.affected_services || []).join(', ');
    document.getElementById('edit-category').value = c.root_cause_category || '';
    document.getElementById('edit-cause').value = c.root_cause_description || '';
    document.getElementById('edit-resolution').value = c.resolution || '';
    document.getElementById('edit-remediation').value = (c.remediation_advice || []).join('\n');
    document.getElementById('edit-environment').value = c.environment || '';
    document.getElementById('edit-version-exact').value = c.service_version_exact || '';
    document.getElementById('edit-version-min').value = c.service_version_min || '';
    document.getElementById('edit-version-max').value = c.service_version_max || '';
    historyCaseIdInput.value = c.id;
    setStatus(editorStatus, `Loaded case #${c.id} (revision ${c.revision}).`, 'success');
}

// ---- Collect Editor Fields ----
function collectEditorFields() {
    return {
        symptom: document.getElementById('edit-symptom').value.trim(),
        affected_services: document.getElementById('edit-services').value.split(',').map(s => s.trim()).filter(Boolean),
        root_cause_category: document.getElementById('edit-category').value.trim(),
        root_cause_description: document.getElementById('edit-cause').value.trim(),
        resolution: document.getElementById('edit-resolution').value.trim(),
        remediation_advice: document.getElementById('edit-remediation').value.split('\n').map(s => s.trim()).filter(Boolean),
        environment: document.getElementById('edit-environment').value.trim(),
        service_version_exact: document.getElementById('edit-version-exact').value.trim(),
        service_version_min: document.getElementById('edit-version-min').value.trim(),
        service_version_max: document.getElementById('edit-version-max').value.trim(),
    };
}

// ---- Save (PATCH) Case ----
caseEditForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!selectedCase) {
        setStatus(editorStatus, 'No case selected.', 'error');
        return;
    }
    setStatus(editorStatus, 'Saving...', '');
    try {
        const body = {
            ...collectEditorFields(),
            expected_version: selectedCase.revision,
            actor: reviewActorInput.value || 'local-user',
            reason: reviewReasonInput.value || '',
        };
        const updated = await apiJson(`/api/cases/${selectedCase.id}`, {
            method: 'PATCH',
            body: JSON.stringify(body),
        });
        selectedCase = updated;
        caseRevisionInput.value = updated.revision;
        setStatus(editorStatus, `Saved. Revision ${updated.revision}.`, 'success');
        loadReviewQueue();
    } catch (err) {
        if (err.status === 409) {
            setStatus(editorStatus, '案例已被其他操作更新，请重新加载', 'error');
            // Reload latest from server
            try {
                const latest = await apiJson(`/api/cases/${selectedCase.id}`);
                selectCaseForEdit(latest);
            } catch (_) { /* ignore reload error */ }
        } else {
            setStatus(editorStatus, `Error: ${escapeHtml(err.message)}`, 'error');
        }
    }
});

// ---- Confirm Case ----
document.getElementById('confirm-case-btn').addEventListener('click', async () => {
    if (!selectedCase) {
        setStatus(editorStatus, 'No case selected.', 'error');
        return;
    }
    setStatus(editorStatus, 'Confirming...', '');
    try {
        const body = {
            expected_version: selectedCase.revision,
            actor: reviewActorInput.value || 'local-user',
            reason: reviewReasonInput.value || '',
        };
        const updated = await apiJson(`/api/cases/${selectedCase.id}/confirm`, {
            method: 'POST',
            body: JSON.stringify(body),
        });
        selectedCase = updated;
        setStatus(editorStatus, `Case confirmed (ID: ${updated.id}).`, 'success');
        loadReviewQueue();
    } catch (err) {
        if (err.status === 409) {
            setStatus(editorStatus, '案例已被其他操作更新，请重新加载', 'error');
        } else {
            setStatus(editorStatus, `Error: ${escapeHtml(err.message)}`, 'error');
        }
    }
});

// ---- Reject Case ----
document.getElementById('reject-case-btn').addEventListener('click', async () => {
    if (!selectedCase) {
        setStatus(editorStatus, 'No case selected.', 'error');
        return;
    }
    setStatus(editorStatus, 'Rejecting...', '');
    try {
        const body = {
            expected_version: selectedCase.revision,
            actor: reviewActorInput.value || 'local-user',
            reason: reviewReasonInput.value || '',
        };
        const updated = await apiJson(`/api/cases/${selectedCase.id}/reject`, {
            method: 'POST',
            body: JSON.stringify(body),
        });
        selectedCase = updated;
        setStatus(editorStatus, `Case rejected (ID: ${updated.id}).`, 'success');
        loadReviewQueue();
    } catch (err) {
        if (err.status === 409) {
            setStatus(editorStatus, '案例已被其他操作更新，请重新加载', 'error');
        } else {
            setStatus(editorStatus, `Error: ${escapeHtml(err.message)}`, 'error');
        }
    }
});

// ---- Deprecate Case ----
document.getElementById('deprecate-case-btn').addEventListener('click', async () => {
    if (!selectedCase) {
        setStatus(editorStatus, 'No case selected.', 'error');
        return;
    }
    setStatus(editorStatus, 'Deprecating...', '');
    try {
        const body = {
            expected_version: selectedCase.revision,
            actor: reviewActorInput.value || 'local-user',
            reason: reviewReasonInput.value || '',
        };
        const updated = await apiJson(`/api/cases/${selectedCase.id}/deprecate`, {
            method: 'POST',
            body: JSON.stringify(body),
        });
        selectedCase = updated;
        setStatus(editorStatus, `Case deprecated (ID: ${updated.id}).`, 'success');
        loadReviewQueue();
    } catch (err) {
        if (err.status === 409) {
            setStatus(editorStatus, '案例已被其他操作更新，请重新加载', 'error');
        } else {
            setStatus(editorStatus, `Error: ${escapeHtml(err.message)}`, 'error');
        }
    }
});

// ---- Case Search ----
searchForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const q = document.getElementById('search-query').value.trim();
    if (!q) {
        setStatus(searchStatus, 'Query is required.', 'error');
        return;
    }
    setStatus(searchStatus, 'Searching...', '');
    searchResults.innerHTML = '';
    try {
        const params = new URLSearchParams({ q });
        const service = document.getElementById('search-service').value.trim();
        const category = document.getElementById('search-category').value.trim();
        const environment = document.getElementById('search-environment').value.trim();
        const version = document.getElementById('search-version').value.trim();
        if (service) params.set('service', service);
        if (category) params.set('root_cause_category', category);
        if (environment) params.set('environment', environment);
        if (version) params.set('service_version', version);

        const data = await apiJson(`/api/cases/search?${params.toString()}`);
        const hits = data.results || [];
        if (hits.length === 0) {
            searchResults.innerHTML = '<p class="empty-hint">No results found.</p>';
            setStatus(searchStatus, '', '');
            return;
        }
        hits.forEach(hit => {
            const card = document.createElement('div');
            card.className = 'search-hit-card';
            card.innerHTML = `
                <div class="hit-header">
                    <span class="hit-id">#${escapeHtml(String(hit.case_id || hit.id || ''))}</span>
                    <span class="hit-mode">${escapeHtml(hit.retrieval_mode || 'unknown')}</span>
                    <span class="hit-score">Lexical: ${escapeHtml(String(hit.lexical_score ?? '-'))}</span>
                    <span class="hit-score">Semantic: ${escapeHtml(String(hit.semantic_score ?? '-'))}</span>
                </div>
                <div class="hit-symptom">${escapeHtml(hit.symptom || '')}</div>
                <div class="hit-reason">${escapeHtml(hit.similarity_reason || '')}</div>
            `;
            searchResults.appendChild(card);
        });
        setStatus(searchStatus, `Found ${hits.length} result(s).`, 'success');
    } catch (err) {
        setStatus(searchStatus, `Error: ${escapeHtml(err.message)}`, 'error');
    }
});

// ---- Feedback ----
document.querySelectorAll('.feedback-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
        const caseId = document.getElementById('feedback-case-id').value;
        if (!caseId) {
            setStatus(document.getElementById('feedback-status'), 'Case ID is required.', 'error');
            return;
        }
        const incidentIdVal = document.getElementById('feedback-incident-id').value.trim();
        const comment = document.getElementById('feedback-comment').value.trim();
        const rating = btn.dataset.rating;
        // Deterministic idempotency key from session
        const uiKey = `${caseId}:${incidentIdVal}:${rating}:${crypto.randomUUID()}`;

        setStatus(document.getElementById('feedback-status'), 'Submitting feedback...', '');
        try {
            await apiJson(`/api/cases/${caseId}/feedback`, {
                method: 'POST',
                body: JSON.stringify({
                    idempotency_key: uiKey,
                    rating: rating,
                    incident_id: incidentIdVal || null,
                    actor: 'local-user',
                    comment: comment,
                }),
            });
            setStatus(document.getElementById('feedback-status'), `Feedback recorded (${rating}).`, 'success');
        } catch (err) {
            setStatus(document.getElementById('feedback-status'), `Error: ${escapeHtml(err.message)}`, 'error');
        }
    });
});

// ---- Case History ----
loadHistoryBtn.addEventListener('click', async () => {
    const caseId = historyCaseIdInput.value;
    if (!caseId) {
        setStatus(historyStatus, 'Case ID is required.', 'error');
        return;
    }
    setStatus(historyStatus, 'Loading history...', '');
    historyList.innerHTML = '';
    try {
        const data = await apiJson(`/api/cases/${caseId}/history`);
        const reviews = data.reviews || [];
        if (reviews.length === 0) {
            historyList.innerHTML = '<p class="empty-hint">No history entries.</p>';
            setStatus(historyStatus, '', '');
            return;
        }
        reviews.forEach(r => {
            const entry = document.createElement('div');
            entry.className = 'history-entry';
            entry.innerHTML = `
                <span class="history-action">${escapeHtml(r.action)}</span>
                <span class="history-actor">${escapeHtml(r.actor)}</span>
                <span class="history-transition">${escapeHtml(r.previous_status || '')} &rarr; ${escapeHtml(r.new_status || '')}</span>
                <span class="history-reason">${escapeHtml(r.reason || '')}</span>
                <span class="history-time">${escapeHtml(r.created_at || '')}</span>
            `;
            historyList.appendChild(entry);
        });
        setStatus(historyStatus, `Loaded ${reviews.length} action(s).`, 'success');
    } catch (err) {
        setStatus(historyStatus, `Error: ${escapeHtml(err.message)}`, 'error');
    }
});

// ---- Export Investigation ----
exportInvestigationBtn.addEventListener('click', async () => {
    if (!incidentId) {
        setStatus(exportStatus, 'No active investigation.', 'error');
        return;
    }
    setStatus(exportStatus, 'Downloading...', '');
    try {
        const a = document.createElement('a');
        a.href = `${API_BASE}/api/investigations/${incidentId}/export`;
        a.download = `investigation-${incidentId}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setStatus(exportStatus, 'Export started.', 'success');
    } catch (err) {
        setStatus(exportStatus, `Error: ${escapeHtml(err.message)}`, 'error');
    }
});

// ---- Evaluation Comparison ----
async function loadEvaluations() {
    setStatus(evalStatus, 'Loading evaluations...', '');
    evalTableBody.innerHTML = '';
    try {
        const scenario = evalScenarioSelect.value || 'all';
        const data = await apiJson(`/api/evaluations/comparison?scenario=${encodeURIComponent(scenario)}`);
        const runs = data.runs || [];
        if (runs.length === 0) {
            evalEmptyState.style.display = 'block';
            evalTableBody.style.display = 'none';
            setStatus(evalStatus, '', '');
            return;
        }
        evalEmptyState.style.display = 'none';
        evalTableBody.style.display = '';
        runs.forEach(run => {
            const m = run.metrics || {};
            const row = document.createElement('tr');
            row.innerHTML = `
                <td>${escapeHtml(run.strategy || '')}</td>
                <td>${escapeHtml(run.scenario || '')}</td>
                <td>${escapeHtml(String(m.accuracy ?? '-'))}</td>
                <td>${escapeHtml(String(m.precision ?? '-'))}</td>
                <td>${escapeHtml(String(m.recall ?? '-'))}</td>
                <td>${escapeHtml(String(m.f1 ?? '-'))}</td>
                <td>${escapeHtml(String(m.latency_ms ?? '-'))}</td>
                <td>${escapeHtml(String(m.cost_usd ?? '-'))}</td>
                <td>${escapeHtml(String(run.total_runs ?? run.run_count ?? '-'))}</td>
            `;
            evalTableBody.appendChild(row);
        });
        setStatus(evalStatus, `Loaded ${runs.length} strategy run(s).`, 'success');
    } catch (err) {
        evalEmptyState.style.display = 'block';
        evalTableBody.style.display = 'none';
        setStatus(evalStatus, `Error: ${escapeHtml(err.message)}`, 'error');
    }
}

loadEvalBtn.addEventListener('click', loadEvaluations);

// ---- Initial Load ----
document.addEventListener('DOMContentLoaded', () => {
    loadReviewQueue();
});
