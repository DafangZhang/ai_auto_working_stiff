const API_BASE = '';
const POLL_INTERVAL = 2000;

const PHASE_META = {
    pending: { label: '待设计', tone: 'neutral', order: 0 },
    designed: { label: '待审核', tone: 'purple', order: 1 },
    design_reviewed: { label: '待开发', tone: 'blue', order: 2 },
    coding: { label: '修复中', tone: 'yellow', order: 2 },
    coding_complete: { label: '待验证', tone: 'blue', order: 3 },
    done: { label: '已完成', tone: 'green', order: 4 }
};

const PHASE_LABELS = Object.fromEntries(Object.entries(PHASE_META).map(([key, value]) => [key, value.label]));

const STEP_FLOW = [
    { id: 'select_project', label: '项目', description: '选择项目根目录' },
    { id: 'upload_prd', label: 'PRD', description: '上传需求文档' },
    { id: 'awaiting_answers', label: '问题', description: '确认边界' },
    { id: 'awaiting_prd_confirmation', label: '确认', description: '保存 PRD' },
    { id: 'awaiting_start', label: 'Stories', description: '确认工作清单' },
    { id: 'running', label: '执行', description: '监控 Agent' }
];

const STEP_LABELS = {
    select_project: '选择项目',
    upload_prd: '上传 PRD',
    analyzing_project: '分析项目',
    awaiting_answers: '确认问题',
    reviewing_prd: '评审 PRD',
    awaiting_prd_confirmation: '确认 PRD',
    generating_stories: '生成 Story',
    awaiting_start: '等待开始',
    running: '运行中',
    completed: '已完成',
    error: '错误'
};

const STEP_TO_FLOW_ID = {
    analyzing_project: 'upload_prd',
    reviewing_prd: 'awaiting_answers',
    generating_stories: 'awaiting_prd_confirmation',
    completed: 'running',
    error: 'running'
};

const AGENTS = [
    { id: 'designer', name: 'Designer', role: '设计方案', phase: 'pending' },
    { id: 'reviewer', name: 'Reviewer', role: '审核设计', phase: 'designed' },
    { id: 'coder', name: 'Coder', role: '实现代码', phase: 'design_reviewed' },
    { id: 'validator', name: 'Validator', role: '验证结果', phase: 'coding_complete' }
];

const ARTIFACTS = [
    { key: 'story', label: '需求', present: () => true },
    { key: 'hasDesignV1', label: 'D1' },
    { key: 'hasDesignV2', label: 'D2' },
    { key: 'hasImplementation', label: '实现' },
    { key: 'hasTestReport', label: '测试' }
];

let currentSession = null;
let runtimeStories = [];
let actionError = '';
let pendingPrdFileName = '';
let pendingAction = '';

async function init() {
    await loadAllData();
    setInterval(loadAllData, POLL_INTERVAL);
    window.onclick = (event) => {
        if (event.target === document.getElementById('story-modal')) closeModal();
    };
}

async function requestJson(url, options = {}) {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || '请求失败');
    return data;
}

function nextPaint() {
    return new Promise((resolve) => requestAnimationFrame(() => resolve()));
}

async function loadAllData() {
    try {
        if (currentSession && !shouldAutoRefresh(currentSession.step)) {
            updateLastUpdateTime();
            return;
        }
        currentSession = await requestJson(`${API_BASE}/api/session`);
        if (['running', 'completed'].includes(currentSession.step)) {
            runtimeStories = await requestJson(`${API_BASE}/api/stories`);
        } else {
            runtimeStories = [];
        }
        renderShell(currentSession);
        renderMain(currentSession);
        renderLogs(currentSession.currentAgentLog || {});
        updateLastUpdateTime();
    } catch (error) {
        renderError(error.message);
    }
}

function shouldAutoRefresh(step) {
    return ['analyzing_project', 'reviewing_prd', 'generating_stories', 'running', 'completed', 'error'].includes(step);
}

function renderShell(session) {
    const project = session.projectRoot || '等待选择项目';
    document.getElementById('project-subtitle').textContent = project;
    const step = STEP_LABELS[session.step] || session.step || '未知状态';
    const status = session.runStatus || {};
    const progress = typeof status.progress === 'number' && ['running', 'completed'].includes(session.step)
        ? ` · ${status.progress}%`
        : '';
    document.getElementById('overall-status').textContent = `${step}${progress}`;
    const restartButton = document.getElementById('restart-session-button');
    if (restartButton) {
        const canRestart = Boolean(session.projectRoot);
        restartButton.disabled = !canRestart;
        restartButton.title = canRestart ? '清空当前项目的 .ai-worker 状态并从第一步重新开始' : '当前没有活动会话';
    }
}

function renderMain(session) {
    if (['running', 'completed'].includes(session.step)) {
        renderRunView(session);
        return;
    }

    const renderers = {
        select_project: renderProjectStep,
        upload_prd: renderUploadStep,
        analyzing_project: () => renderBusy('正在分析项目和 PRD'),
        awaiting_answers: renderQuestionsStep,
        reviewing_prd: () => renderBusy('正在整理可执行 PRD'),
        awaiting_prd_confirmation: renderPrdConfirmStep,
        generating_stories: () => renderBusy('正在拆分 Stories'),
        awaiting_start: renderStoriesPreviewStep,
        error: renderSessionError
    };
    (renderers[session.step] || renderProjectStep)(session);
}

function renderProjectStep(session) {
    setMain(renderWorkbenchCard({
        session,
        kicker: 'Step 1 / Project',
        title: '选择要开发的项目根目录',
        description: 'AI Worker 会在目标项目内创建或复用 .ai-worker 工件目录，用于保存会话、Stories、日志和执行状态。',
        body: `
            ${renderActionError()}
            <form class="stack" onsubmit="submitProject(event)">
                <label class="field-block">
                    <span>项目绝对路径</span>
                    <input id="project-root-input" type="text" value="${escapeAttr(session.projectRoot || '')}" placeholder="/Users/name/workspace/project" autocomplete="off" aria-describedby="project-root-help">
                    <small id="project-root-help">必须是本机存在的绝对路径。不会修改项目源码，直到后续开始执行 Agent。</small>
                </label>
                <div class="notice-grid">
                    <div class="notice-item"><strong>.ai-worker</strong><span>保存 PRD、Stories、运行状态和日志。</span></div>
                    <div class="notice-item"><strong>只读扫描</strong><span>下一步会先分析目录和关键文件。</span></div>
                </div>
                <div class="actions left">
                    <button type="submit" class="primary-button" ${pendingAction === 'project' ? 'disabled' : ''}>${pendingAction === 'project' ? '校验中...' : '校验项目并继续'}</button>
                </div>
            </form>
        `
    }));
}

async function submitProject(event) {
    event.preventDefault();
    try {
        actionError = '';
        const projectRoot = document.getElementById('project-root-input').value.trim();
        if (!projectRoot) throw new Error('请输入项目绝对路径。');
        if (!projectRoot.startsWith('/')) throw new Error('项目根目录必须是绝对路径。');
        pendingAction = 'project';
        renderMain(currentSession || { step: 'select_project', projectRoot });
        await nextPaint();
        await requestJson('/api/session/project', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ projectRoot })
        });
        pendingAction = '';
        currentSession = null;
        await loadAllData();
    } catch (error) {
        pendingAction = '';
        actionError = error.message;
        renderMain(currentSession || { step: 'select_project' });
    }
}

function renderUploadStep(session) {
    setMain(renderWorkbenchCard({
        session,
        kicker: 'Step 2 / PRD',
        title: '上传 Markdown PRD',
        description: '上传后会自动扫描项目结构、Git 状态和关键上下文，再进入问题确认或 PRD 评审。',
        body: `
            ${renderActionError()}
            <form class="stack" onsubmit="uploadPrd(event)">
                <label class="upload-zone" for="prd-file-input">
                    <span class="upload-title">选择 Markdown PRD 文件</span>
                    <span class="upload-meta" id="selected-prd-name">${escapeHtml(pendingPrdFileName || '支持 .md / .markdown，单次上传一个文件')}</span>
                    <input id="prd-file-input" type="file" accept=".md,.markdown" onchange="handlePrdFileChange(event)">
                </label>
                <div class="helper-strip compact">
                    <span>上传完成后会自动进入项目分析。</span>
                    <span>右侧日志会显示后台任务的实时输出。</span>
                </div>
                <div class="actions left">
                    <button type="submit" class="primary-button" ${pendingAction === 'upload_prd' ? 'disabled' : ''}>${pendingAction === 'upload_prd' ? '上传中...' : '上传并分析'}</button>
                </div>
            </form>
            ${session.rawPrd ? renderDocumentPreview('已上传 PRD 预览', session.rawPrd) : ''}
        `
    }));
}

function handlePrdFileChange(event) {
    const file = event.target.files && event.target.files[0];
    pendingPrdFileName = file ? `${file.name} · ${formatBytes(file.size)}` : '';
    const target = document.getElementById('selected-prd-name');
    if (target) target.textContent = pendingPrdFileName || '支持 .md / .markdown，单次上传一个文件';
}

async function uploadPrd(event) {
    event.preventDefault();
    try {
        actionError = '';
        pendingAction = 'upload_prd';
        const input = document.getElementById('prd-file-input');
        if (!input.files.length) throw new Error('请选择 PRD 文件。');
        const formData = new FormData();
        formData.append('prd', input.files[0]);
        currentSession = {
            ...(currentSession || {}),
            step: 'analyzing_project',
            activity: {
                kind: 'analyze_project',
                status: 'running',
                message: '正在上传 PRD 并启动项目分析...',
                detail: '上传完成后会自动扫描项目结构与 Git 状态。',
                progress: 5
            }
        };
        renderShell(currentSession);
        renderMain(currentSession);
        await nextPaint();
        await requestJson('/api/session/prd', { method: 'POST', body: formData });
        pendingPrdFileName = '';
        await requestJson('/api/session/analyze', { method: 'POST' });
        pendingAction = '';
        await loadAllData();
    } catch (error) {
        pendingAction = '';
        actionError = error.message;
        renderMain(currentSession || { step: 'upload_prd' });
    }
}

function renderQuestionsStep(session) {
    const questions = session.questions || [];
    const answered = Object.values(session.answers || {}).filter(value => String(value || '').trim()).length;
    setMain(renderWorkbenchCard({
        session,
        kicker: 'Step 3 / Questions',
        title: '补充项目和需求边界',
        description: '这些问题来自项目扫描和 PRD 检查。回答会写入 Reviewed PRD，供后续 Story 拆分和 Agent 执行使用。',
        aside: renderMetricStrip([
            { label: '待确认', value: questions.length },
            { label: '已填写', value: answered },
            { label: '必填', value: questions.filter(q => q.required !== false).length }
        ]),
        body: `
            ${renderActionError()}
            <form class="question-form" onsubmit="submitAnswers(event)">
                ${questions.map((q, index) => renderQuestion(q, session.answers || {}, index)).join('') || '<p class="empty-state">暂无待确认问题。</p>'}
                <div class="actions left">
                    <button type="submit" class="primary-button" ${pendingAction === 'answers' ? 'disabled' : ''}>${pendingAction === 'answers' ? '提交中...' : '提交答案并继续评审'}</button>
                </div>
            </form>
        `
    }));
}

function renderQuestion(question, answers, index) {
    const required = question.required !== false;
    return `
        <label class="question-block">
            <span class="question-head">
                <strong>${index + 1}. ${escapeHtml(question.title)}</strong>
                ${required ? '<em>必填</em>' : '<em>可选</em>'}
            </span>
            <span class="question-text">${escapeHtml(question.question)}</span>
            <textarea data-question-id="${escapeAttr(question.id)}" data-required="${required ? 'true' : 'false'}" placeholder="${escapeAttr(question.placeholder || '')}">${escapeHtml(answers[question.id] || '')}</textarea>
            <small>${escapeHtml(question.whyItMatters || '')}</small>
        </label>
    `;
}

async function submitAnswers(event) {
    event.preventDefault();
    try {
        actionError = '';
        pendingAction = 'answers';
        const answers = {};
        const missing = [];
        document.querySelectorAll('[data-question-id]').forEach((el) => {
            const value = el.value.trim();
            answers[el.dataset.questionId] = value;
            if (el.dataset.required === 'true' && !value) {
                missing.push(el.dataset.questionId);
            }
        });
        if (missing.length) throw new Error(`还有 ${missing.length} 个必填问题未填写。`);
        await requestJson('/api/session/answers', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ answers })
        });
        pendingAction = '';
        currentSession = null;
        await loadAllData();
    } catch (error) {
        pendingAction = '';
        actionError = error.message;
        renderMain(currentSession || { step: 'awaiting_answers', questions: [] });
    }
}

function renderPrdConfirmStep(session) {
    const prd = session.reviewedPrd || '';
    setMain(renderWorkbenchCard({
        session,
        kicker: 'Step 4 / Reviewed PRD',
        title: '确认可执行 PRD',
        description: '可以在这里做最后编辑。确认后会保存当前内容，并启动 story-splitter 生成 Story 工作清单。',
        aside: renderMetricStrip([
            { label: '字符', value: prd.length },
            { label: '行数', value: prd ? prd.split('\n').length : 0 },
            { label: '下一步', value: 'Story' }
        ]),
        body: `
            ${renderActionError()}
            <div class="editor-toolbar">
                <div>
                    <strong>Reviewed PRD</strong>
                    <span>保存后将作为 Story 拆分的唯一需求输入。</span>
                </div>
                <button type="button" class="primary-button" ${pendingAction === 'generate_stories' ? 'disabled' : ''} onclick="confirmPrd()">${pendingAction === 'generate_stories' ? '正在启动拆分...' : '保存 PRD 并生成 Story'}</button>
            </div>
            <textarea id="reviewed-prd-editor" class="prd-editor">${escapeHtml(prd)}</textarea>
            <div class="helper-strip compact">
                <span>Story 生成通常需要 10-60 秒。</span>
                <span>生成过程中不会改变后端拆分协议，只展示过程反馈。</span>
            </div>
        `
    }));
}

async function confirmPrd() {
    try {
        actionError = '';
        pendingAction = 'generate_stories';
        const markdown = document.getElementById('reviewed-prd-editor').value;
        if (!markdown.trim()) throw new Error('Reviewed PRD 不能为空。');
        currentSession = {
            ...(currentSession || {}),
            step: 'generating_stories',
            activity: {
                kind: 'generate_stories',
                status: 'running',
                message: '正在保存 PRD 并启动 Story 拆分...',
                detail: '页面会自动刷新。你可以留在当前页等待，也可以查看右侧日志区域。',
                progress: 5
            }
        };
        renderShell(currentSession);
        renderMain(currentSession);
        await nextPaint();
        await requestJson('/api/session/prd/confirm', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ markdown })
        });
        await requestJson('/api/session/stories/generate', { method: 'POST' });
        pendingAction = '';
        await loadAllData();
    } catch (error) {
        pendingAction = '';
        actionError = error.message;
        renderMain(currentSession || { step: 'awaiting_prd_confirmation' });
    }
}

function renderStoriesPreviewStep(session) {
    const stories = sortStoriesById(session.storiesPreview || []);
    const summary = summarizeStoryPreview(stories);
    const completedMessage = session.activity?.kind === 'generate_stories' && session.activity?.status === 'completed'
        ? `<div class="inline-success">${escapeHtml(session.activity.message || 'Story 已生成完成')}</div>`
        : '';
    setMain(renderWorkbenchCard({
        session,
        kicker: 'Step 5 / Stories',
        title: '确认 Story 工作清单',
        description: '确认拆分结果、依赖和验收标准后即可开始执行。执行阶段会按依赖和 Story 顺序推进。',
        aside: renderMetricStrip([
            { label: 'Stories', value: stories.length },
            { label: '依赖', value: summary.dependencyCount },
            { label: '验收项', value: summary.criteriaCount }
        ]),
        body: `
            ${renderActionError()}
            ${completedMessage}
            <div class="list-toolbar">
                <div>
                    <strong>${stories.length} 个 Story 等待开始</strong>
                    <span>优先级越小越靠前；依赖会在执行时自动检查。</span>
                </div>
                <button type="button" class="primary-button" ${pendingAction === 'start_worker' || !stories.length ? 'disabled' : ''} onclick="startWorker()">${pendingAction === 'start_worker' ? '启动中...' : '开始工作'}</button>
            </div>
            <div class="story-preview-list">
                ${stories.map(renderStoryPreviewCard).join('') || '<p class="empty-state">暂无 Stories。</p>'}
            </div>
        `
    }));
}

function renderStoryPreviewCard(story) {
    const criteria = (story.acceptanceCriteria || []).slice(0, 3);
    const tasks = (story.implementationTasks || []).slice(0, 2);
    return `
        <article class="story-card">
            <div class="story-card-head">
                <span class="story-id">${escapeHtml(story.storyId)}</span>
                <span class="priority-badge">P${escapeHtml(story.priority || '-')}</span>
            </div>
            <h3>${escapeHtml(story.title)}</h3>
            <p>${escapeHtml(story.description || '')}</p>
            ${tasks.length ? `<div class="compact-list"><strong>实现任务</strong>${tasks.map(item => `<span>${escapeHtml(item)}</span>`).join('')}</div>` : ''}
            <div class="compact-list"><strong>验收标准</strong>${criteria.map(item => `<span>${escapeHtml(item)}</span>`).join('') || '<span>待补充</span>'}</div>
            <div class="dependencies">依赖: ${escapeHtml((story.dependencies || []).join(', ') || '无')}</div>
        </article>
    `;
}

async function startWorker() {
    try {
        actionError = '';
        pendingAction = 'start_worker';
        renderMain(currentSession || { step: 'awaiting_start', storiesPreview: [] });
        await nextPaint();
        await requestJson('/api/session/start', { method: 'POST' });
        pendingAction = '';
        currentSession = null;
        await loadAllData();
    } catch (error) {
        pendingAction = '';
        actionError = error.message;
        renderMain(currentSession || { step: 'awaiting_start', storiesPreview: [] });
    }
}

function renderRunView(session) {
    const status = session.runStatus || {};
    const workerState = status.workerState || {};
    const previewStories = session.storiesPreview || [];
    const stories = mergeRuntimeStories(previewStories, runtimeStories);
    const currentStoryId = workerState.currentStoryId || '';
    const currentPhase = workerState.currentPhase || '';
    const currentPhaseMeta = phaseMeta(currentPhase);
    setMain(`
        <div class="run-layout">
            ${renderStepRail(session)}
            <section class="overview">
                <div class="progress-card">
                    <h2>总进度</h2>
                    <div class="progress-ring" style="--progress:${Number(status.progress || 0)}%">
                        <span>${Number(status.progress || 0)}%</span>
                    </div>
                </div>
                <div class="stats-card">
                    ${renderMetricStrip([
                        { label: '总 Story', value: status.total || stories.length || 0 },
                        { label: '已完成', value: status.done || 0, tone: 'green' },
                        { label: '进行中', value: status.inProgress || 0, tone: 'yellow' },
                        { label: '待处理', value: status.pending || 0, tone: 'muted' }
                    ])}
                </div>
            </section>
            <section class="current-story-panel">
                <div>
                    <span class="phase-badge phase-${escapeAttr(currentPhaseMeta.tone)}">${escapeHtml(currentPhaseMeta.label)}</span>
                    <h2>${escapeHtml(currentStoryId || (session.step === 'completed' ? '所有 Story 已完成' : '等待任务'))}</h2>
                    <p class="muted">${escapeHtml(workerState.currentAgent ? `${workerState.currentAgent.toUpperCase()} 正在处理` : '当前没有运行中的 Agent')}</p>
                </div>
                <div class="progress-pill">${status.done || 0}/${status.total || stories.length || 0}</div>
            </section>
            <section class="agent-stage">
                <div class="panel-header">
                    <div>
                        <h2>Agent 工作状态</h2>
                        <p class="muted">按 Designer、Reviewer、Coder、Validator 顺序推进当前 Story。</p>
                    </div>
                </div>
                <div class="agent-flow">
                    ${AGENTS.map(agent => renderAgent(agent, workerState)).join('')}
                </div>
            </section>
            <section class="stories-section">
                <div class="panel-header">
                    <div>
                        <h2>Stories</h2>
                        <p class="muted">点击任意 Story 查看需求、设计、实现和验证产物。</p>
                    </div>
                </div>
                <div class="stories-list" id="stories-list-runtime">
                    ${stories.map(story => renderRuntimeStory(story, currentStoryId)).join('') || '<p class="empty-state">暂无 Stories。</p>'}
                </div>
            </section>
        </div>
    `);
}

function renderAgent(agent, workerState) {
    const active = workerState.currentAgent === agent.id;
    const currentIndex = AGENTS.findIndex(item => item.id === workerState.currentAgent);
    const index = AGENTS.findIndex(item => item.id === agent.id);
    const done = currentIndex >= 0 && index < currentIndex;
    const state = active ? 'active' : done ? 'done' : 'idle';
    const phase = phaseMeta(agent.phase);
    return `
        <article class="agent-card ${state}">
            <span class="agent-dot"></span>
            <div>
                <h3>${agent.name}</h3>
                <p>${agent.role}</p>
            </div>
            <small>${active ? escapeHtml(workerState.currentStoryId || 'active') : escapeHtml(phase.label)}</small>
        </article>
    `;
}

function renderRuntimeStory(story, currentStoryId) {
    const id = story.directory || story.id || story.storyId;
    const phase = story.status?.phase || story.phase || 'pending';
    const meta = phaseMeta(phase);
    const active = currentStoryId && [story.id, story.storyId, story.directory].includes(currentStoryId);
    return `
        <button type="button" class="story-row ${active ? 'active' : ''}" onclick="showStoryDetail('${escapeAttr(id)}')">
            <span class="phase-badge phase-${escapeAttr(meta.tone)}">${escapeHtml(meta.label)}</span>
            <span class="story-id">${escapeHtml(story.storyId || story.id || id)}</span>
            <strong>${escapeHtml(story.title || story.status?.title || story.id || id)}</strong>
            <small>${escapeHtml((story.dependencies || story.status?.dependencies || []).join(', ') || '无依赖')}</small>
            <span class="artifact-dots" aria-label="产物状态">${renderArtifactDots(story)}</span>
        </button>
    `;
}

async function showStoryDetail(storyId) {
    try {
        const story = await requestJson(`/api/story/${storyId}`);
        renderStoryModal(story);
        document.getElementById('story-modal').style.display = 'block';
    } catch (error) {
        alert(error.message);
    }
}

function renderStoryModal(story) {
    const tabs = [];
    const contents = [];
    const status = story.status || {};
    const phase = status.phase || 'pending';
    const meta = phaseMeta(phase);
    const title = status.title || story.id;
    addTab(tabs, contents, 'story', '需求', story.story, true);
    if (story.designV1) addTab(tabs, contents, 'design1', '设计 V1', story.designV1);
    if (story.designV2) addTab(tabs, contents, 'design2', '设计 V2', story.designV2);
    if (story.implementation) addTab(tabs, contents, 'impl', '实现', story.implementation);
    if (story.testReport) addTab(tabs, contents, 'test', '测试报告', story.testReport);
    addTab(tabs, contents, 'status', '状态详情', JSON.stringify(status, null, 2));
    document.getElementById('modal-body').innerHTML = `
        <div class="modal-titlebar">
            <div>
                <span class="phase-badge phase-${escapeAttr(meta.tone)}">${escapeHtml(meta.label)}</span>
                <h2>${escapeHtml(story.id)}${title && title !== story.id ? `: ${escapeHtml(title)}` : ''}</h2>
            </div>
            <div class="artifact-dots large">${renderArtifactDots({
                hasDesignV1: Boolean(story.designV1),
                hasDesignV2: Boolean(story.designV2),
                hasImplementation: Boolean(story.implementation),
                hasTestReport: Boolean(story.testReport)
            })}</div>
        </div>
        <div class="tabs">${tabs.join('')}</div>
        ${contents.join('')}
    `;
}

function addTab(tabs, contents, id, label, text, active = false) {
    tabs.push(`<button type="button" class="tab ${active ? 'active' : ''}" onclick="switchTab(this, 'tab-${id}')">${escapeHtml(label)}</button>`);
    contents.push(`<div id="tab-${id}" class="tab-content ${active ? 'active' : ''}"><pre>${escapeHtml(text || '')}</pre></div>`);
}

function switchTab(tabEl, contentId) {
    document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));
    tabEl.classList.add('active');
    document.getElementById(contentId).classList.add('active');
}

function renderLogs(log) {
    const meta = document.getElementById('log-meta');
    if (log.agent) {
        meta.textContent = `${log.agent.toUpperCase()} · ${log.storyId || ''} · ${PHASE_LABELS[log.phase] || log.phase || ''}`;
    } else if (currentSession?.activity?.status === 'running') {
        meta.textContent = currentSession.activity.message || '后台任务进行中';
    } else {
        meta.textContent = '暂无运行中的 Agent';
    }
    const container = document.getElementById('logs-content');
    const lines = log.lines || [];
    if (lines.length) {
        container.innerHTML = lines.map(line => `<div class="log-line">${highlightLogLine(line)}</div>`).join('');
    } else if (currentSession?.activity?.status === 'running') {
        container.innerHTML = `
            <div class="log-line log-info">[INFO] ${escapeHtml(currentSession.activity.message || '后台任务运行中')}</div>
            ${currentSession.activity.detail ? `<div class="log-line">${escapeHtml(currentSession.activity.detail)}</div>` : ''}
            <div class="log-line">[INFO] 当前步骤会自动轮询刷新，无需重复点击。</div>
        `;
    } else {
        container.innerHTML = '<p class="empty-state compact">暂无日志。</p>';
    }
    container.scrollTop = container.scrollHeight;
}

function highlightLogLine(line) {
    const escaped = escapeHtml(line);
    if (escaped.includes('[ERROR]')) return `<span class="log-error">${escaped}</span>`;
    if (escaped.includes('[WARNING]')) return `<span class="log-warning">${escaped}</span>`;
    if (escaped.includes('[INFO]')) return `<span class="log-info">${escaped}</span>`;
    return escaped;
}

function renderBusy(message) {
    const activity = (currentSession && currentSession.activity) || {};
    const progress = typeof activity.progress === 'number' ? activity.progress : 12;
    const detail = activity.detail || '状态会自动刷新';
    const stages = renderBusyStages(activity.kind, progress);
    setMain(renderWorkbenchCard({
        session: currentSession || { step: 'generating_stories' },
        kicker: STEP_LABELS[currentSession?.step] || '处理中',
        title: activity.message || message,
        description: detail,
        body: `
            <div class="busy-card">
                <div class="busy-head">
                    <div class="loader" aria-hidden="true"></div>
                    <div>
                        <h2>${escapeHtml(activity.message || message)}</h2>
                        <p class="muted">${escapeHtml(detail)}</p>
                    </div>
                </div>
                <div class="progress-track" aria-hidden="true">
                    <span class="progress-fill" style="width:${Math.max(8, progress)}%"></span>
                </div>
                <div class="progress-meta">
                    <span>${escapeHtml(STEP_LABELS[currentSession?.step] || '处理中')}</span>
                    <span>${progress}%</span>
                </div>
                <div class="busy-stages">${stages}</div>
                <div class="busy-note">长任务已转为后台执行。请关注右侧日志，页面会自动刷新到下一步。</div>
            </div>
        `
    }));
}

function renderBusyStages(kind, progress) {
    const stageMap = {
        analyze_project: [
            { label: '扫描目录与关键文件', threshold: 15 },
            { label: '检查 Git 与项目约束', threshold: 45 },
            { label: '生成问题与 PRD 上下文', threshold: 75 }
        ],
        generate_stories: [
            { label: '准备 shared 上下文', threshold: 12 },
            { label: '调用 story-splitter', threshold: 46 },
            { label: '整理 Story 预览结果', threshold: 78 }
        ]
    };
    const stages = stageMap[kind] || [
        { label: '准备任务', threshold: 20 },
        { label: '后台处理中', threshold: 55 },
        { label: '整理结果', threshold: 85 }
    ];
    return stages.map((stage) => {
        const state = progress >= stage.threshold ? 'done' : 'pending';
        return `<div class="busy-stage ${state}"><span></span><strong>${escapeHtml(stage.label)}</strong></div>`;
    }).join('');
}

function renderSessionError(session) {
    renderError(session.lastError || '会话进入错误状态', true, session);
}

function renderError(message, canRecover = false, session = currentSession) {
    setMain(renderWorkbenchCard({
        session: session || { step: 'error' },
        kicker: 'Error',
        title: '处理失败',
        description: '当前步骤没有完成。可以根据右侧日志定位原因，然后重新加载或返回上一步。',
        body: `
            <div class="inline-error">${escapeHtml(message)}</div>
            <div class="actions left">
                ${canRecover ? '<button type="button" class="ghost-button" onclick="recoverSession()">返回上一步</button>' : ''}
                <button type="button" class="ghost-button" onclick="loadAllData()">重新加载</button>
            </div>
        `
    }));
}

async function recoverSession() {
    try {
        actionError = '';
        await requestJson('/api/session/recover', { method: 'POST' });
        currentSession = null;
        await loadAllData();
    } catch (error) {
        renderError(error.message);
    }
}

async function restartSession() {
    if (!currentSession || !currentSession.projectRoot) return;
    const confirmed = window.confirm('这会停止当前执行，并清空该项目 .ai-worker 下的会话、Stories、日志和状态。确定重新开始吗？');
    if (!confirmed) return;

    try {
        actionError = '';
        pendingPrdFileName = '';
        await requestJson('/api/session/restart', { method: 'POST' });
        currentSession = null;
        runtimeStories = [];
        closeModal();
        await loadAllData();
    } catch (error) {
        renderError(error.message);
    }
}

function renderWorkbenchCard({ session, kicker, title, description, aside = '', body }) {
    return `
        <div class="workbench-card">
            ${renderStepRail(session)}
            <div class="workbench-head">
                <div>
                    <div class="step-kicker">${escapeHtml(kicker)}</div>
                    <h2>${escapeHtml(title)}</h2>
                    <p>${escapeHtml(description || '')}</p>
                </div>
                ${aside ? `<div class="head-aside">${aside}</div>` : ''}
            </div>
            ${body}
        </div>
    `;
}

function renderStepRail(session = {}) {
    const activeId = STEP_TO_FLOW_ID[session.step] || session.step || 'select_project';
    const activeIndex = Math.max(0, STEP_FLOW.findIndex(step => step.id === activeId));
    return `
        <nav class="step-rail" aria-label="工作流步骤">
            ${STEP_FLOW.map((step, index) => {
                const state = index < activeIndex ? 'done' : index === activeIndex ? 'active' : 'idle';
                return `
                    <div class="step-node ${state}">
                        <span>${index + 1}</span>
                        <div><strong>${escapeHtml(step.label)}</strong><small>${escapeHtml(step.description)}</small></div>
                    </div>
                `;
            }).join('')}
        </nav>
    `;
}

function renderMetricStrip(metrics) {
    return `<div class="metric-strip">${metrics.map(metric => `
        <div class="metric ${metric.tone ? `metric-${escapeAttr(metric.tone)}` : ''}">
            <span>${escapeHtml(metric.label)}</span>
            <strong>${escapeHtml(metric.value)}</strong>
        </div>
    `).join('')}</div>`;
}

function renderDocumentPreview(title, text) {
    return `
        <section class="document-preview">
            <div class="section-title">
                <h3>${escapeHtml(title)}</h3>
                <span>${text.length} chars</span>
            </div>
            <pre class="preview">${escapeHtml(text.slice(0, 1800))}${text.length > 1800 ? '\n...' : ''}</pre>
        </section>
    `;
}

function renderActionError() {
    if (!actionError) return '';
    return `<div class="inline-error" role="alert">${escapeHtml(actionError)}</div>`;
}

function renderArtifactDots(story) {
    return ARTIFACTS.map(item => {
        const present = item.present ? item.present(story) : Boolean(story[item.key]);
        return `<span class="artifact-dot ${present ? 'present' : ''}" title="${escapeAttr(item.label)}">${escapeHtml(item.label)}</span>`;
    }).join('');
}

function summarizeStoryPreview(stories) {
    return stories.reduce((summary, story) => {
        summary.dependencyCount += (story.dependencies || []).length;
        summary.criteriaCount += (story.acceptanceCriteria || []).length;
        return summary;
    }, { dependencyCount: 0, criteriaCount: 0 });
}

function mergeRuntimeStories(previews, details) {
    const byId = new Map();
    previews.forEach(story => {
        const id = story.directory || story.storyId;
        byId.set(id, { ...story, id });
    });
    details.forEach(detail => {
        const id = detail.id;
        const existing = byId.get(id) || byId.get(detail.status?.storyId) || {};
        byId.set(id, {
            ...existing,
            ...detail,
            id,
            storyId: existing.storyId || detail.status?.storyId || id,
            title: existing.title || detail.status?.title || id,
            dependencies: existing.dependencies || detail.status?.dependencies || []
        });
    });
    return sortStoriesById(Array.from(byId.values()));
}

function sortStoriesById(stories) {
    return [...stories].sort(compareStoriesById);
}

function compareStoriesById(a, b) {
    const storyA = storyIdentity(a);
    const storyB = storyIdentity(b);
    const numberA = extractStoryNumber(storyA);
    const numberB = extractStoryNumber(storyB);
    if (numberA != null && numberB != null && numberA !== numberB) return numberA - numberB;
    if (numberA != null && numberB == null) return -1;
    if (numberA == null && numberB != null) return 1;
    return storyA.localeCompare(storyB);
}

function storyIdentity(story) {
    return String(story.storyId || story.status?.storyId || story.id || story.directory || '');
}

function extractStoryNumber(value) {
    const match = String(value).match(/US-(\d+)/i);
    return match ? Number(match[1]) : null;
}

function phaseMeta(phase) {
    return PHASE_META[phase] || { label: phase || '未知', tone: 'neutral', order: 0 };
}

function formatBytes(size) {
    if (!Number.isFinite(size)) return '';
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function setMain(html) {
    document.getElementById('main-panel').innerHTML = html;
}

function closeModal() {
    const modal = document.getElementById('story-modal');
    if (modal) modal.style.display = 'none';
}

function updateLastUpdateTime() {
    document.getElementById('last-update').textContent = `最后更新: ${new Date().toLocaleTimeString()}`;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
}

function escapeAttr(text) {
    return escapeHtml(text).replace(/"/g, '&quot;');
}

init();
