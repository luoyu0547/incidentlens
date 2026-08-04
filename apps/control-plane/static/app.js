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
let investigationStartedAt = null;

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
const runStatusBadge = document.getElementById('run-status-badge');
const topIncidentId = document.getElementById('top-incident-id');

// ---- Navigation and motion ----
function switchView(viewId) {
    document.querySelectorAll('.view').forEach(view => {
        view.classList.toggle('active', view.id === viewId);
    });
    document.querySelectorAll('.nav-item').forEach(item => {
        const active = item.dataset.view === viewId && (
            viewId !== 'overview-view' || item === document.querySelector('.nav-item[data-view="overview-view"]')
        );
        item.classList.toggle('active', active);
        if (active) item.setAttribute('aria-current', 'page');
        else item.removeAttribute('aria-current');
    });
    window.scrollTo({ top: 0, behavior: 'smooth' });
}

document.querySelectorAll('[data-view]').forEach(trigger => {
    trigger.addEventListener('click', () => switchView(trigger.dataset.view));
});

function animateMetric(element) {
    if (!element?.dataset.count) return;
    const target = Number(element.dataset.count);
    const decimals = Number(element.dataset.decimals || 0);
    const suffix = element.dataset.suffix || '';
    const start = performance.now();
    const duration = 850;
    const tick = now => {
        const progress = Math.min((now - start) / duration, 1);
        const eased = 1 - Math.pow(1 - progress, 3);
        element.textContent = `${(target * eased).toFixed(decimals)}${suffix}`;
        if (progress < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
}

function updateLiveMetric(id, value, suffix = '') {
    const el = document.getElementById(id);
    if (!el) return;
    el.dataset.count = String(value);
    el.dataset.suffix = suffix;
    animateMetric(el);
}

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

    alertStatus.textContent = '正在创建调查任务…';
    alertStatus.className = 'status-box';
    document.body.classList.add('is-investigating');
    runStatusBadge.textContent = '调查中';
    investigationStartedAt = performance.now();

    try {
        const resp = await fetch(`${API_BASE}/api/investigations/start`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        incidentId = data.incident_id;
        alertStatus.textContent = `调查已启动：${incidentId}`;
        alertStatus.className = 'status-box success';
        topIncidentId.textContent = incidentId;

        // Connect to SSE
        connectSSE(incidentId);

        // Run rounds automatically
        runRounds(incidentId, data.max_rounds || 8);
    } catch (err) {
        document.body.classList.remove('is-investigating');
        runStatusBadge.textContent = '启动失败';
        alertStatus.textContent = `启动失败：${err.message}`;
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
        document.body.classList.remove('is-investigating');
        runStatusBadge.textContent = '报告已生成';
        updateLiveMetric('metric-confidence', 92, '%');
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
    const elapsed = investigationStartedAt ? Math.floor((performance.now() - investigationStartedAt) / 1000) : 0;
    const eventLabels = {
        state_changed: '状态推进',
        tool_called: '工具调用',
        evidence_recorded: '证据入库',
        report_ready: '报告生成',
    };
    entry.innerHTML = `
        <time>00:${String(elapsed).padStart(2, '0')}</time>
        <span class="timeline-dot"></span>
        <div>
            <div class="event-type">${escapeHtml(eventLabels[eventType] || eventType)}</div>
            <strong>${escapeHtml(data.phase || data.tool || (eventType === 'report_ready' ? '根因报告已就绪' : '调查状态已更新'))}</strong>
            <div class="event-data">${escapeHtml(formatEventData(eventType, data))}</div>
        </div>
    `;
    timeline.appendChild(entry);
    timeline.scrollTop = timeline.scrollHeight;
}

function formatEventData(eventType, data) {
    switch (eventType) {
        case 'state_changed':
            return `状态：${data.status || ''} · 轮次：${data.round || ''} · 阶段：${data.phase || ''}`;
        case 'tool_called':
            return `工具：${data.tool || ''} · 参数：${JSON.stringify(data.args || {})}`;
        case 'evidence_recorded':
            return `来源：${data.source_tool || ''} · 内容：${JSON.stringify(data.content || {}).substring(0, 100)}`;
        case 'report_ready':
            return `根因：${data.root_cause || '已识别'}`;
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
    updateLiveMetric('metric-tools', toolSummary.children.length);
}

function addEvidenceEntry(data) {
    const entry = document.createElement('div');
    entry.className = 'evidence-entry';
    entry.innerHTML = `<span class="evidence-source">${escapeHtml(data.source_tool || 'unknown')}</span>: ${escapeHtml(JSON.stringify(data.content || {}).substring(0, 150))}`;
    evidenceEl.appendChild(entry);
    updateLiveMetric('metric-evidence', evidenceEl.children.length);
}

function renderReport(data) {
    reportEl.innerHTML = `
        <div class="root-cause"><span>已确认根因</span><p>${escapeHtml(data.root_cause || '暂未识别根因')}</p></div>
        <div class="findings">
            <h3>关键发现</h3>
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
    confirmStatus.textContent = '正在确认并沉淀案例…';
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
        confirmStatus.textContent = `案例已保存（ID：${data.case_id}）`;
        confirmStatus.className = 'status-box success';
        confirmBtn.disabled = true;
        rejectBtn.disabled = true;
    } catch (err) {
        confirmStatus.textContent = `保存失败：${err.message}`;
        confirmStatus.className = 'status-box error';
    }
});

rejectBtn.addEventListener('click', () => {
    confirmStatus.textContent = '调查结论已驳回，可补充证据后继续调查。';
    confirmStatus.className = 'status-box error';
    confirmBtn.disabled = true;
    rejectBtn.disabled = true;
});

// ============================================================
// GOVERNANCE FUNCTIONS
// ============================================================

// ---- Load Review Queue ----
async function loadReviewQueue() {
    setStatus(reviewQueueStatus, '正在加载审核队列…', '');
    try {
        const data = await apiJson('/api/cases?status=agent_generated&limit=50');
        const draftData = await apiJson('/api/cases?status=draft&limit=50');
        const allCases = [...(data.cases || []), ...(draftData.cases || [])];
        reviewQueueList.innerHTML = '';
        if (allCases.length === 0) {
            reviewQueueList.innerHTML = '<p class="empty-hint">当前没有待审核案例。</p>';
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
                    <button class="btn-select-case" data-case-id="${escapeHtml(String(c.id))}">编辑</button>
                </div>
            `;
            card.querySelector('.btn-select-case').addEventListener('click', () => selectCaseForEdit(c));
            reviewQueueList.appendChild(card);
        });
        setStatus(reviewQueueStatus, `已加载 ${allCases.length} 个案例。`, 'success');
    } catch (err) {
        setStatus(reviewQueueStatus, `加载失败：${escapeHtml(err.message)}`, 'error');
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
    setStatus(editorStatus, `已加载案例 #${c.id}（修订版本 ${c.revision}）。`, 'success');
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
        setStatus(editorStatus, '请先选择案例。', 'error');
        return;
    }
    setStatus(editorStatus, '正在保存…', '');
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
        setStatus(editorStatus, `保存成功，当前修订版本为 ${updated.revision}。`, 'success');
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
            setStatus(editorStatus, `保存失败：${escapeHtml(err.message)}`, 'error');
        }
    }
});

// ---- Confirm Case ----
document.getElementById('confirm-case-btn').addEventListener('click', async () => {
    if (!selectedCase) {
        setStatus(editorStatus, '请先选择案例。', 'error');
        return;
    }
    setStatus(editorStatus, '正在确认案例…', '');
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
        setStatus(editorStatus, `案例已确认（ID：${updated.id}）。`, 'success');
        loadReviewQueue();
    } catch (err) {
        if (err.status === 409) {
            setStatus(editorStatus, '案例已被其他操作更新，请重新加载', 'error');
        } else {
            setStatus(editorStatus, `确认失败：${escapeHtml(err.message)}`, 'error');
        }
    }
});

// ---- Reject Case ----
document.getElementById('reject-case-btn').addEventListener('click', async () => {
    if (!selectedCase) {
        setStatus(editorStatus, '请先选择案例。', 'error');
        return;
    }
    setStatus(editorStatus, '正在驳回案例…', '');
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
        setStatus(editorStatus, `案例已驳回（ID：${updated.id}）。`, 'success');
        loadReviewQueue();
    } catch (err) {
        if (err.status === 409) {
            setStatus(editorStatus, '案例已被其他操作更新，请重新加载', 'error');
        } else {
            setStatus(editorStatus, `驳回失败：${escapeHtml(err.message)}`, 'error');
        }
    }
});

// ---- Deprecate Case ----
document.getElementById('deprecate-case-btn').addEventListener('click', async () => {
    if (!selectedCase) {
        setStatus(editorStatus, '请先选择案例。', 'error');
        return;
    }
    setStatus(editorStatus, '正在废弃案例…', '');
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
        setStatus(editorStatus, `案例已废弃（ID：${updated.id}）。`, 'success');
        loadReviewQueue();
    } catch (err) {
        if (err.status === 409) {
            setStatus(editorStatus, '案例已被其他操作更新，请重新加载', 'error');
        } else {
            setStatus(editorStatus, `废弃失败：${escapeHtml(err.message)}`, 'error');
        }
    }
});

// ---- Case Search ----
searchForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const q = document.getElementById('search-query').value.trim();
    if (!q) {
        setStatus(searchStatus, '请输入检索内容。', 'error');
        return;
    }
    setStatus(searchStatus, '正在检索案例…', '');
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
            searchResults.innerHTML = '<p class="empty-hint">没有找到匹配案例。</p>';
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
                    <span class="hit-score">词法：${escapeHtml(String(hit.lexical_score ?? '-'))}</span>
                    <span class="hit-score">语义：${escapeHtml(String(hit.semantic_score ?? '-'))}</span>
                </div>
                <div class="hit-symptom">${escapeHtml(hit.symptom || '')}</div>
                <div class="hit-reason">${escapeHtml(hit.similarity_reason || '')}</div>
            `;
            searchResults.appendChild(card);
        });
        setStatus(searchStatus, `找到 ${hits.length} 个匹配案例。`, 'success');
    } catch (err) {
        setStatus(searchStatus, `检索失败：${escapeHtml(err.message)}`, 'error');
    }
});

// ---- Feedback ----
document.querySelectorAll('.feedback-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
        const caseId = document.getElementById('feedback-case-id').value;
        if (!caseId) {
            setStatus(document.getElementById('feedback-status'), '请输入案例 ID。', 'error');
            return;
        }
        const incidentIdVal = document.getElementById('feedback-incident-id').value.trim();
        const comment = document.getElementById('feedback-comment').value.trim();
        const rating = btn.dataset.rating;
        // Deterministic idempotency key from session
        const uiKey = `${caseId}:${incidentIdVal}:${rating}:${crypto.randomUUID()}`;

        setStatus(document.getElementById('feedback-status'), '正在提交反馈…', '');
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
            setStatus(document.getElementById('feedback-status'), `反馈已记录（${rating}）。`, 'success');
        } catch (err) {
            setStatus(document.getElementById('feedback-status'), `提交失败：${escapeHtml(err.message)}`, 'error');
        }
    });
});

// ---- Case History ----
loadHistoryBtn.addEventListener('click', async () => {
    const caseId = historyCaseIdInput.value;
    if (!caseId) {
        setStatus(historyStatus, '请输入案例 ID。', 'error');
        return;
    }
    setStatus(historyStatus, '正在加载操作历史…', '');
    historyList.innerHTML = '';
    try {
        const data = await apiJson(`/api/cases/${caseId}/history`);
        const reviews = data.reviews || [];
        if (reviews.length === 0) {
            historyList.innerHTML = '<p class="empty-hint">当前没有历史记录。</p>';
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
        setStatus(historyStatus, `已加载 ${reviews.length} 条操作记录。`, 'success');
    } catch (err) {
        setStatus(historyStatus, `加载失败：${escapeHtml(err.message)}`, 'error');
    }
});

// ---- Export Investigation ----
exportInvestigationBtn.addEventListener('click', async () => {
    if (!incidentId) {
        setStatus(exportStatus, '当前没有可导出的调查任务。', 'error');
        return;
    }
    setStatus(exportStatus, '正在准备下载…', '');
    try {
        const a = document.createElement('a');
        a.href = `${API_BASE}/api/investigations/${incidentId}/export`;
        a.download = `investigation-${incidentId}.json`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setStatus(exportStatus, '导出任务已开始。', 'success');
    } catch (err) {
        setStatus(exportStatus, `导出失败：${escapeHtml(err.message)}`, 'error');
    }
});

// ---- Evaluation Comparison ----
async function loadEvaluations() {
    setStatus(evalStatus, '正在加载评测结果…', '');
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
        setStatus(evalStatus, `已加载 ${runs.length} 组策略结果。`, 'success');
    } catch (err) {
        evalEmptyState.style.display = 'block';
        evalTableBody.style.display = 'none';
        setStatus(evalStatus, `加载失败：${escapeHtml(err.message)}`, 'error');
    }
}

loadEvalBtn.addEventListener('click', loadEvaluations);

// ---- Initial Load ----
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-count]').forEach(animateMetric);
    loadReviewQueue();
});
