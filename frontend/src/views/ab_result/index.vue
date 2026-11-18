<template>
  <section class="page" data-module="ab_result">
    <header class="page-head">
      <div><h2>AB结果分析</h2><p class="page-desc">AB对比结果表的每一行差异都由同一批成员的实验组均值减对照组均值得出，列表、详情抽屉与下游读取保持同一版本。</p></div>
      <div class="page-actions">
        <button class="btn" data-testid="recalculate" type="button" @click="recalculate">重算结果表</button>
        <button class="btn" data-testid="verify" type="button" @click="verify">核对结果表</button>
      </div>
    </header>

    <div class="analysis-summary">
      <article v-for="m in summaryMetrics" :key="m.label" class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">{{ m.label }}</span>
        <strong style="font-size:22px;display:block">{{ m.value }}</strong>
        <span :class="m.trend >= 0 ? 'trend-up' : 'trend-down'" style="font-size:12px">{{ m.trend >= 0 ? '↑' : '↓' }}{{ Math.abs(m.trend) }}%</span>
      </article>
    </div>

    <form class="analysis-form" @submit.prevent="runAnalysis">
      <label v-for="field in paramFields" :key="field"><span>{{ field }}</span>
        <input v-model="params[field]" :placeholder="`请输入${field}`" />
      </label>
      <button class="btn primary" type="submit" data-testid="run-analysis">运行分析</button>
    </form>

    <div v-if="chartData.length" class="analysis-chart">
      <div v-for="(bar, i) in chartData" :key="i" class="analysis-bar" :style="{ height: barHeight(bar.value) + 'px' }" :title="`${bar.label}：${bar.value}`"><span>{{ bar.label }}</span></div>
    </div>

    <table class="data-table" v-if="results.length">
      <thead>
        <tr>
          <th v-for="c in resultColumns" :key="c">{{ c }}</th>
          <th>详情</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in results" :key="row.id" :data-testid="`compare-row-${row.id}`" :data-version="row.结果版本">
          <td v-for="c in resultColumns" :key="c">{{ formatCell(row, c) }}</td>
          <td><button class="link" type="button" :data-testid="`detail-${row.id}`" @click="openDetail(row)">查看详情</button></td>
        </tr>
      </tbody>
    </table>
    <p v-if="!results.length && !loading" style="text-align:center;color:var(--muted)">暂无对比结果，可先运行分析</p>

    <footer class="page-foot">
      <span>结果版本：{{ tableVersion }} · 共 {{ results.length }} 行 · AB结果分析 {{ release }}</span>
      <span v-if="message" :class="messageIsError ? 'error-text' : ''">{{ message }}</span>
    </footer>

    <div v-if="detail" class="modal-backdrop" data-testid="detail-drawer">
      <aside class="modal detail-drawer">
        <h3>AB对比结果详情</h3>
        <dl class="detail-grid">
          <template v-for="field in detailFields" :key="field">
            <dt>{{ field }}</dt>
            <dd :data-testid="`detail-${field}`">{{ formatCell(detail, field) }}</dd>
          </template>
        </dl>
        <p class="detail-note">详情与列表来自同一结果版本 {{ detail.结果版本 }}；差异 = 实验组均值 − 对照组均值。</p>
        <div class="page-actions">
          <button class="btn" type="button" data-testid="close-detail" @click="detail = null">关闭</button>
        </div>
      </aside>
    </div>
  </section>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { request } from '@/api/client'

const ENDPOINT = '/api/ab_result'
const RELEASE = 'V028'
const summaryMetrics = ref<{ label: string; value: number; trend: number }[]>([])
const results = ref<any[]>([])
const chartData = ref<{ label: string; value: number }[]>([])
const tableVersion = ref(0)
const release = ref(RELEASE)
const loading = ref(false)
const message = ref('')
const messageIsError = ref(false)
const detail = ref<any>(null)
const params = ref<Record<string, string>>({})
const paramFields = ['报告编号']
const resultColumns = ['报告编号', '实验编号', '分析指标', '指标版本', '对照组均值', '实验组均值', '差异', '核对状态']
const detailFields = [...resultColumns, '效果方向', '置信水平', '对照组成员版本', '实验组成员版本', '成员快照', '结果版本', '生成时间', '核对说明']
const NUMERIC_FIELDS = new Set(['对照组均值', '实验组均值', '差异'])

function formatCell(row: any, field: string) {
  const value = row?.[field]
  if (value === undefined || value === null || value === '') return '—'
  if (NUMERIC_FIELDS.has(field) && typeof value === 'number') return value.toFixed(6)
  return String(value)
}

function barHeight(v: number) {
  const max = Math.max(...chartData.value.map(d => d.value), 0.000001)
  return Math.max(10, (Math.abs(v) / max) * 140)
}

async function fetchSummary() {
  try {
    const res = await request(`${ENDPOINT}/summary`)
    if (!res.ok) return
    const data = await res.json()
    summaryMetrics.value = data.metrics || []
    release.value = data.release || RELEASE
  } catch (e) { /* 汇总失败不影响结果表读取 */ }
}

async function loadResults() {
  loading.value = true
  message.value = ''
  messageIsError.value = false
  try {
    const res = await request(ENDPOINT)
    if (!res.ok) throw new Error('AB对比结果表读取失败')
    const data = await res.json()
    results.value = data.items || []
    tableVersion.value = data.version ?? 0
    release.value = data.release || RELEASE
    // KPI、图表和结果表都来自同一次读取，避免导航页与抽屉各显示一套口径。
    summaryMetrics.value = [
      { label: '对比结果行', value: results.value.length, trend: results.value.length },
      { label: '已核对行', value: results.value.filter((row: any) => row['核对状态'] === '已核对').length,
        trend: results.value.filter((row: any) => row['核对状态'] === '已核对').length },
      { label: '结果版本', value: tableVersion.value, trend: tableVersion.value },
    ]
    chartData.value = results.value.slice(0, 6).map((row: any) => ({
      label: String(row['报告编号'] ?? ''),
      value: Number(row['差异'] ?? 0),
    }))
    void fetchSummary()
  } catch (e) {
    results.value = []
    message.value = e instanceof Error ? e.message : 'AB对比结果表读取失败'
    messageIsError.value = true
  } finally {
    loading.value = false
  }
}

async function runAnalysis() {
  loading.value = true
  message.value = ''
  messageIsError.value = false
  try {
    const res = await request(`${ENDPOINT}/run`, {
      method: 'POST', body: JSON.stringify({ values: params.value }),
    })
    const data = await res.json()
    results.value = data.items || []
    chartData.value = (data.items || []).slice(0, 6).map((row: any) => ({
      label: String(row['报告编号'] ?? row['分组'] ?? ''),
      value: Number(row['差异'] ?? 0),
    }))
    // 结果表版本随运行一起刷新，避免页面停留在旧行。
    const table = await request(ENDPOINT)
    if (table.ok) {
      const payload = await table.json()
      tableVersion.value = payload.version ?? tableVersion.value
    }
    await fetchSummary()
  } catch (e) {
    message.value = e instanceof Error ? e.message : '运行分析失败'
    messageIsError.value = true
  } finally {
    loading.value = false
  }
}

async function recalculate() {
  loading.value = true
  message.value = ''
  messageIsError.value = false
  try {
    const res = await request(`${ENDPOINT}/recalculate`, { method: 'POST', body: JSON.stringify({ values: {} }) })
    const data = await res.json()
    if (!data.ok) throw new Error(data.message || '重算失败')
    message.value = data.message || 'AB对比结果表已重算'
    await loadResults()
  } catch (e) {
    message.value = e instanceof Error ? e.message : '重算失败'
    messageIsError.value = true
    await loadResults()
  } finally {
    loading.value = false
  }
}

async function verify() {
  message.value = ''
  messageIsError.value = false
  try {
    const res = await request(`${ENDPOINT}/verify`, { method: 'POST', body: JSON.stringify({ values: {} }) })
    const data = await res.json()
    messageIsError.value = !data.ok
    message.value = data.ok
      ? `核对通过：${data.checked} 行差异均等于实验组均值减对照组均值（版本 ${data.version}）`
      : (data.message || '核对未通过')
    await loadResults()
  } catch (e) {
    message.value = e instanceof Error ? e.message : '核对失败'
    messageIsError.value = true
  }
}

async function openDetail(row: any) {
  // 抽屉只认当前列表的版本；版本已变化时重新取同一报告的明细。
  try {
    const res = await request(`${ENDPOINT}/${row.id}`)
    if (!res.ok) throw new Error('详情读取失败')
    const data = await res.json()
    detail.value = data['AB对比结果'] ? { ...row, ...data['AB对比结果'] } : { ...row, ...data }
  } catch (e) {
    detail.value = { ...row }
    message.value = e instanceof Error ? e.message : '详情读取失败'
    messageIsError.value = true
  }
}

onMounted(loadResults)
</script>
