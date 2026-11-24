import {api} from '../api/client.js';
import {empty,errorSlot} from '../components/common/layout.js';
import {report} from '../components/common/report.js';
import {table} from '../components/common/table.js';
import {decisions,states} from '../constants/enums.js';
import {bindForm} from '../hooks/forms.js';
import {state} from '../stores/session.js';
import {fieldInput} from '../types/fields.js';
import {esc,formData,time} from '../utils/html.js';

const MAX_TEAMS = 3;

export async function analysis() {
  const profile = state.profile;
  const [datasets,runs] = await Promise.all([api('/datasets'),api('/runs')]);
  const orderDatasets = datasets.filter(row => (row.kind || 'orders') === 'orders');
  const scenarioDatasets = datasets.filter(row => row.kind === 'scenarios');
  const activeOrder = orderDatasets.find(row => row.id === state.datasetId) || orderDatasets[0];
  if (activeOrder && state.datasetId == null) state.datasetId = activeOrder.id;
  const workDate = await pendingWorkDate(activeOrder?.id);
  const form = state.account.role === 'analyst' && orderDatasets.length ? `
    <form id="queue" class="inline-form team-queue">
      <label>工单数据版本<select name="dataset_id">${orderDatasets.map(row => `<option value="${row.id}" ${row.id === activeOrder?.id ? 'selected' : ''}>${esc(row.name)} · #${row.id}</option>`).join('')}</select></label>
      <label>作业日<input id="work-date" name="work_date" type="date" value="${esc(workDate)}"></label>
      <label>逐情景工时版本<select id="scenario-dataset" name="scenario_dataset_id"><option value="">不绑定情景工时</option>${scenarioDatasets.map(row => `<option value="${row.id}" ${row.id === state.scenarioDatasetId ? 'selected' : ''}>${esc(row.name)} · #${row.id}</option>`).join('')}</select></label>
      ${profile.options.map(field => `<label>${esc(field.label)}${fieldInput(field,esc)}</label>`).join('')}
      <button type="submit">提交计算</button>
      ${errorSlot}
    </form>
    <section class="scenario-upload-card">
      <h3>补传逐情景工时</h3>
      <p class="hint">选定作业日后上传 CSV：每个情景须恰好覆盖该作业日全部待完工工单。支持 <code>scenario_id,order_id,duration_min</code> 长表，或以工单号为首列、各情景为列的宽表；工时按分钟计。两份数据分别保存为不可变新版本，补正后各自生成新版。</p>
      ${scenarioUploadForm()}
      <p class="hint">已有 ${scenarioDatasets.length} 个情景工时版本，可在上方“逐情景工时版本”中选择。</p>
    </section>` : empty(datasets.length ? '分析员可提交计算。' : '先导入数据版本。', '#1', '查看数据台账');
  return `<section><h2>计算条件</h2><p>${esc(profile.method)}</p>${form}</section><section><h2>分析记录</h2>${table(runs.map(run => ({任务:{link:`#3/${run.id}`,text:`#${run.id}`},数据版本:run.dataset_id,情景版本:run.scenario_dataset_id ?? '—',状态:states[run.state],提交时间:time(run.created),复核:decisions[run.decision] ?? '待复核'})))}</section>`;
}

export function scenarioUploadForm(extraClass = '') {
  if (state.account?.role !== 'analyst') return '<p class="hint">分析员可补传逐情景工时。</p>';
  return `<form id="scenario-upload" class="inline-form scenario-upload ${extraClass}">
    <label class="file-label">补传逐情景工时（CSV）<input name="file" type="file" accept=".csv,text/csv" required></label>
    <button type="submit">上传并生成新版本</button>
    ${errorSlot}
  </form>`;
}

async function pendingWorkDate(datasetId) {
  if (state.workDate) return state.workDate;
  if (!datasetId) return '';
  try {
    const detail = await api(`/datasets/${datasetId}`);
    const days = [...new Set(detail.records
      .filter(row => row.status === '待完工' || row.status === 'pending')
      .map(row => String(row.planned_start || '').slice(0, 10))
      .filter(Boolean))];
    return days.length === 1 ? days[0] : (days[0] || '');
  } catch {
    return '';
  }
}

export async function currentRun() {
  const runs = await api('/runs');
  const chosen = runs.find(item => item.id === state.runId) || runs[0];
  return {runs,run:chosen ? await api(`/runs/${chosen.id}`) : null};
}

export function chooseRun(runs, selected) {
  return `<label>分析任务<select id="pick-run">${runs.map(row => `<option value="${row.id}" ${selected === row.id ? 'selected' : ''}>#${row.id} · ${states[row.state]} · ${time(row.created)}</option>`).join('')}</select></label>`;
}

function pctText(value) {
  return `${Number(value).toFixed(2).replace(/\.?0+$/, '')}%`;
}

export function teamPanel(plan) {
  if (!plan) return '';
  const failed = plan.feasible === false;
  const assignments = plan.assignments || [];
  const candidates = plan.candidates || plan.team_counts || [];
  const scenarios = plan.scenarios || [];
  const assignmentTable = table(assignments.map(item => ({
    工单号:item.order_id || item.工单号,
    河段:item.reach || item.河段 || '—',
    原定开工:item.planned_start || item.原定开工 || '—',
    分派:`班组 ${item.team ?? item.team_id ?? item.班组号}`,
  })));
  const candidateTable = candidates.length ? table(candidates.map(item => ({
    可用班组数:item.allowed_teams ?? item.team_count,
    实际使用班组:item.team_count,
    覆盖情景:`${item.covered_scenarios}/${item.scenario_count}`,
    覆盖率:pctText(item.coverage_pct),
    是否达标:item.meets_target ? '达到目标' : '未达到',
  }))) : '';
  const scenarioTable = scenarios.length ? table(scenarios.map(item => ({
    情景:item.scenario_id,
    覆盖判定:item.covered ? '计入覆盖' : '整组不计（同班组工单重叠）',
    重叠工单:(item.conflicts || []).map(pair => `${pair[0]} 与 ${pair[1]}`).join('；') || '—',
  }))) : '';
  const headline = failed
    ? `<p class="notice team-headline" role="alert"><strong>三个班组仍不达标</strong>：即使动用全部 ${plan.max_teams || MAX_TEAMS} 个班组，最高可达覆盖率仍为 <strong><span data-max-coverage="${esc(plan.highest_coverage_pct)}">${pctText(plan.highest_coverage_pct)}</span></strong>（最多覆盖 ${plan.candidates?.[plan.candidates.length - 1]?.covered_scenarios ?? plan.covered_scenarios}/${plan.scenario_count} 个情景），覆盖目标 ${pctText(plan.target_pct)}。现展示最高可达方案，并<strong>阻止发布</strong>。</p>`
    : `<p class="notice ok team-headline">按原定开工时间计算，最少 <strong data-team-count="${esc(plan.team_count)}">${plan.team_count} 个班组</strong> 即可达到覆盖目标；同一份分派用于全部 ${plan.scenario_count} 个情景，覆盖率 <strong data-coverage="${esc(plan.coverage_pct)}">${pctText(plan.coverage_pct)}</strong>（${plan.covered_scenarios}/${plan.scenario_count} 个情景）。</p>`;
  return `<section class="team-plan panel" data-team-plan data-feasible="${plan.feasible}" data-coverage="${esc(plan.coverage_pct)}" data-max-coverage="${esc(plan.highest_coverage_pct)}">
    <h2>班组分派与覆盖率</h2>
    ${headline}
    <h3>同一份分派（两个河段共用三个班组）</h3>
    <div data-assignment>${assignmentTable}</div>
    ${candidateTable ? '<h3>各班型覆盖</h3>' + candidateTable : ''}
    ${scenarioTable ? '<h3>逐情景重叠判定</h3>' + scenarioTable : ''}
  </section>`;
}

export async function results() {
  const {runs,run} = await currentRun();
  if (!run) return empty('提交一条分析任务后可在这里查看结果。','#2','配置分析');
  const result = run.result;
  const resultView = result ? `${result.team_plan ? teamPanel(result.team_plan) : ''}${report(result)}` : `<p ${run.error ? 'class="error"' : 'data-poll'}>${esc(run.error || states[run.state])}</p>`;
  return `<section>${chooseRun(runs,run.id)}<p class="hint">数据版本 #${run.dataset_id}${run.scenario_dataset_id ? ` · 情景工时版本 #${run.scenario_dataset_id}` : ''}${run.work_date ? ` · 作业日 ${esc(run.work_date)}` : ''} · 计算状态 ${states[run.state]}</p>${resultView}${run.result ? `<a class="button" href="#4/${run.id}">进入业务复核</a>` : '<a href="#2">回到分析条件</a>'}</section>`;
}

export async function submitScenarioUpload(form) {
  const file = form.elements.file.files[0];
  if (!file) throw new Error('请选择逐情景工时 CSV。');
  const imported = await api('/scenario-datasets', {name:file.name, csv:await file.text()});
  state.scenarioDatasetId = imported.id;
  state.status = imported.created ? `已保存逐情景工时 ${imported.rows} 行（新版本 #${imported.id}）。` : `情景工时与已有版本一致，已选中 #${imported.id}。`;
}

export function bindAnalysis(root, section, refresh) {
  bindForm(root,'#queue',async form => {
    const values = formData(form);
    const dataset_id = Number(values.dataset_id);
    const scenario_dataset_id = values.scenario_dataset_id ? Number(values.scenario_dataset_id) : null;
    const work_date = values.work_date || null;
    const options = {...values};
    delete options.dataset_id;
    delete options.scenario_dataset_id;
    delete options.work_date;
    state.workDate = work_date;
    const run = await api('/runs',{dataset_id,scenario_dataset_id,work_date,options});
    location.hash = `3/${run.id}`;
  });
  bindForm(root,'#scenario-upload',form => submitScenarioUpload(form),refresh);
  root.querySelector('#pick-run')?.addEventListener('change',event => {location.hash = `${section}/${event.target.value}`;});
}
