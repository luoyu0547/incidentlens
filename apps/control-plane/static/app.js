/**
 * IncidentLens Dashboard — live investigation updates via EventSource.
 *
 * Connects to SSE endpoint and updates DOM with investigation state.
 * Does NOT show thinking process — only observable outputs.
 */

const API_BASE = window.location.origin;

let eventSource = null;
let incidentId = null;

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
        <div class="event-type">${eventType.replace('_', ' ')}</div>
        <div class="event-data">${formatEventData(eventType, data)}</div>
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
        statusEl.className = `status-badge ${data.status}`;
        statusEl.textContent = data.status.replace('_', ' ');
    }
}

function addToolEntry(data) {
    const entry = document.createElement('div');
    entry.className = 'tool-entry';
    entry.innerHTML = `<span class="tool-name">${data.tool || 'unknown'}</span> — ${JSON.stringify(data.args || {})}`;
    toolSummary.appendChild(entry);
}

function addEvidenceEntry(data) {
    const entry = document.createElement('div');
    entry.className = 'evidence-entry';
    entry.innerHTML = `<span class="evidence-source">${data.source_tool || 'unknown'}</span>: ${JSON.stringify(data.content || {}).substring(0, 150)}`;
    evidenceEl.appendChild(entry);
}

function renderReport(data) {
    reportEl.innerHTML = `
        <div class="root-cause">${data.root_cause || 'No root cause identified'}</div>
        <div class="findings">
            <h3>Findings</h3>
            ${(data.findings || []).map(f => `
                <div class="finding">
                    <strong>${f.source_tool || 'Tool'}</strong>: ${JSON.stringify(f.content || {}).substring(0, 200)}
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
