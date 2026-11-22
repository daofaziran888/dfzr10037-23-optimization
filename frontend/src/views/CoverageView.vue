<template>
  <section class="page" data-module="index_coverage">
    <header class="page-head">
      <div>
        <h2>版本化索引覆盖段</h2>
        <p class="page-desc">
          按版本化覆盖段交付构建结果，支持构建代仲裁、墓碑保护、暂缓核对、幂等发布与补构建清单。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记覆盖段
        </button>
        <button class="btn" data-testid="refresh-all" type="button" @click="loadAll">
          刷新核对
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">覆盖段总数</span>
        <strong class="stat-value">{{ segments.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已发布</span>
        <strong class="stat-value">{{ countByStage('已发布') }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">仅就绪</span>
        <strong class="stat-value">{{ countByStage('已就绪') }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">暂缓核对</span>
        <strong class="stat-value">{{ countByStage('暂缓核对') }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已发布实体</span>
        <strong class="stat-value">{{ coverage.published_count }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">仅就绪实体</span>
        <strong class="stat-value">{{ coverage.ready_only_count }}</strong>
      </article>
    </div>

    <p v-if="message" class="form-tip" :class="{ error: hasError }">{{ message }}</p>

    <form v-if="showCreate" class="filter-bar create-form" @submit.prevent="submitCreate">
      <label class="filter-item">
        <span>覆盖范围</span>
        <input v-model="createForm.scope" data-testid="segment-scope" placeholder="如：机房A" />
      </label>
      <label class="filter-item">
        <span>覆盖段编号（可空）</span>
        <input v-model="createForm.segmentCode" data-testid="segment-code" placeholder="如：SEG-A" />
      </label>
      <button class="btn primary" type="submit">提交登记</button>
      <button class="btn ghost" type="button" @click="showCreate = false">取消</button>
    </form>

    <div class="filter-bar">
      <label class="filter-item">
        <span>检索实体（区分已发布/仅就绪）</span>
        <input v-model="searchId" data-testid="search-entity" placeholder="实体标识" @keyup.enter="doSearch" />
      </label>
      <button class="btn" data-testid="search-btn" type="button" @click="doSearch">检索</button>
      <label class="filter-item">
        <span>按图代生成补构建清单</span>
        <input v-model.number="planGeneration" data-testid="plan-generation" type="number" placeholder="如图代 200" />
      </label>
      <button class="btn" data-testid="plan-btn" type="button" @click="doPlan">生成补构建清单</button>
    </div>

    <div v-if="searchResult" class="result-panel">
      <h3>检索结论：{{ searchResult.query }}（归一标识：{{ searchResult.normalized_entity_id }}）</h3>
      <p>
        可检索：<b>{{ searchResult.retrievable ? '是' : '否' }}</b>；
        仅就绪：<b>{{ searchResult.partial_ready ? '是' : '否' }}</b>；{{ searchResult.message }}
      </p>
      <p v-if="searchResult.held_segments?.length">暂缓中的段：{{ searchResult.held_segments.join('、') }}</p>
    </div>

    <div v-if="planResult" class="result-panel">
      <h3>
        图代 {{ planResult.graph_generation }} 补构建清单（{{ planResult.supplement_count }} 项，
        快照实体 {{ planResult.snapshot_entity_count }}，已覆盖 {{ planResult.covered_entity_count }}）
      </h3>
      <p v-if="planResult.stale_covered?.length">
        过期覆盖（当前图已不存在）：{{ planResult.stale_covered.join('、') }}
      </p>
      <ul>
        <li v-for="task in planResult.tasks" :key="task.task_code">
          {{ task.task_code }} · {{ task.entity_id }} · {{ task.reason }}
        </li>
      </ul>
    </div>

    <table class="data-table">
      <thead>
        <tr>
          <th>覆盖段</th>
          <th>范围/版本</th>
          <th>阶段</th>
          <th>实体数</th>
          <th>墓碑/冲突</th>
          <th>交付代</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in segments" :key="row.segment_id" :data-status="row.stage">
          <td>{{ row.segment_code }}</td>
          <td>{{ row.scope }} · v{{ row.version }}</td>
          <td><span class="stage-badge" :class="stageClass(row.stage)">{{ row.stage }}</span></td>
          <td>{{ row.entity_count }}</td>
          <td>{{ row.tombstone_count }} / {{ row.conflict_count }}</td>
          <td>{{ deliveryGenerations(row) }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDeliver(row)">交付构建</button>
            <button class="link" type="button" @click="doReconcile(row)">核对</button>
            <button v-if="row.stage !== '暂缓核对' && row.stage !== '已发布'" class="link" type="button" @click="doHold(row)">
              暂缓
            </button>
            <button v-if="row.stage === '暂缓核对'" class="link" type="button" @click="doRelease(row)">
              恢复
            </button>
            <button v-if="row.stage !== '已发布' && row.stage !== '暂缓核对'" class="link" type="button" @click="doPublish(row)">
              发布
            </button>
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="deliverTarget" class="modal-mask" @click.self="deliverTarget = null">
      <form class="modal-card" @submit.prevent="submitDeliver">
        <h3>向 {{ deliverTarget.segment_code }} 交付构建结果</h3>
        <label class="filter-item">
          <span>构建编号</span>
          <input v-model="deliverForm.build_id" required placeholder="如：BUILD-A-G100" />
        </label>
        <label class="filter-item">
          <span>构建代</span>
          <input v-model.number="deliverForm.build_generation" required type="number" />
        </label>
        <label class="filter-item">
          <span>新增实体（逗号分隔，兼容历史标识）</span>
          <input v-model="deliverForm.addedText" placeholder="DEV-OLD-001,DEV-002" />
        </label>
        <label class="filter-item">
          <span>移除/退役实体（逗号分隔）</span>
          <input v-model="deliverForm.removedText" placeholder="DEV-002" />
        </label>
        <label class="filter-item">
          <span>回执实体数（须与实际覆盖一致）</span>
          <input v-model.number="deliverForm.receipt_entity_count" required type="number" />
        </label>
        <label class="filter-item">
          <span>回执图代</span>
          <input v-model.number="deliverForm.receipt_snapshot_generation" required type="number" />
        </label>
        <div class="row-actions">
          <button class="btn primary" type="submit">提交交付</button>
          <button class="btn ghost" type="button" @click="deliverTarget = null">取消</button>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import {
  coverageApi,
  type CoverageRead,
  type CoverageSegment,
  type DeliveryResult,
  type SearchResult,
  type SupplementPlan,
} from '@/api/index-coverage'

const segments = ref<CoverageSegment[]>([])
const coverage = ref<CoverageRead>({
  published: [],
  ready_only: [],
  published_count: 0,
  ready_only_count: 0,
  held_segments: [],
  read_at: '',
})
const message = ref('')
const hasError = ref(false)
const showCreate = ref(false)
const createForm = reactive({ scope: '', segmentCode: '' })
const searchId = ref('')
const searchResult = ref<SearchResult | null>(null)
const planGeneration = ref<number | null>(null)
const planResult = ref<SupplementPlan | null>(null)

const deliverTarget = ref<CoverageSegment | null>(null)
const deliverForm = reactive({
  build_id: '',
  build_generation: 100,
  addedText: '',
  removedText: '',
  receipt_entity_count: 0,
  receipt_snapshot_generation: 100,
})

function notify(text: string, error = false) {
  message.value = text
  hasError.value = error
}

function splitIds(text: string): string[] {
  return text
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
}

function countByStage(stage: string): number {
  return segments.value.filter((item) => item.stage === stage).length
}

function stageClass(stage: string): string {
  if (stage === '已发布') return 'stage-published'
  if (stage === '暂缓核对') return 'stage-held'
  if (stage === '已就绪') return 'stage-ready'
  return 'stage-registered'
}

function deliveryGenerations(row: CoverageSegment): string {
  const generations = (row.deliveries ?? [])
    .map((item: any) => item.build_generation)
    .filter((value, index, array) => array.indexOf(value) === index)
    .sort((a, b) => a - b)
  return generations.length ? `G${generations.join('/G')}` : '—'
}

async function loadAll() {
  try {
    const [segmentData, coverageData] = await Promise.all([
      coverageApi.listSegments(),
      coverageApi.readCoverage(),
    ])
    segments.value = segmentData.items
    coverage.value = coverageData
    notify('覆盖段与读取视图已刷新')
  } catch (error) {
    notify(error instanceof Error ? error.message : '刷新失败', true)
  }
}

function openCreate() {
  showCreate.value = true
}

async function submitCreate() {
  try {
    await coverageApi.registerSegment(createForm.scope, createForm.segmentCode)
    showCreate.value = false
    createForm.scope = ''
    createForm.segmentCode = ''
    await loadAll()
    notify('覆盖段已登记（版本 v1）')
  } catch (error) {
    notify(error instanceof Error ? error.message : '登记失败', true)
  }
}

function openDeliver(row: CoverageSegment) {
  deliverTarget.value = row
  Object.assign(deliverForm, {
    build_id: '',
    build_generation: 100,
    addedText: '',
    removedText: '',
    receipt_entity_count: 0,
    receipt_snapshot_generation: 100,
  })
}

async function submitDeliver() {
  if (!deliverTarget.value) return
  try {
    const result: DeliveryResult = await coverageApi.deliver(
      deliverTarget.value.segment_id,
      {
        build_id: deliverForm.build_id,
        build_generation: deliverForm.build_generation,
        added: splitIds(deliverForm.addedText),
        removed: splitIds(deliverForm.removedText),
        receipt_entity_count: deliverForm.receipt_entity_count,
        receipt_snapshot_generation: deliverForm.receipt_snapshot_generation,
        graph_snapshot_generation: deliverForm.receipt_snapshot_generation,
      },
    )
    deliverTarget.value = null
    await loadAll()
    if (result.result === '已拒绝') {
      notify(`交付被拒收：${JSON.stringify(result.detail)}`, true)
    } else if (result.result === '重复回执') {
      notify(`重复回执已幂等处理（第 ${result.detail.redeliveries ?? 1} 次重新送达）`)
    } else {
      notify(`交付已接收，应用新增 ${(result.detail.applied_added as string[])?.length ?? 0} 个、移除 ${(result.detail.applied_removed as string[])?.length ?? 0} 个实体`)
    }
  } catch (error) {
    notify(error instanceof Error ? error.message : '交付失败', true)
  }
}

async function doReconcile(row: CoverageSegment) {
  try {
    const result = await coverageApi.reconcile(row.segment_id)
    notify(
      result.blocked
        ? `${row.segment_code} 核对卡住：${result.message}`
        : `${row.segment_code} 核对${result.reconciled ? '一致' : '存在差异'}：覆盖 ${result.covered_entity_count} 个实体`,
      Boolean(result.blocked) || !result.reconciled,
    )
  } catch (error) {
    notify(error instanceof Error ? error.message : '核对失败', true)
  }
}

async function doHold(row: CoverageSegment) {
  try {
    await coverageApi.hold(row.segment_id, '人工暂缓，其他段继续交付')
    await loadAll()
    notify(`${row.segment_code} 已暂缓核对，其他段不受影响`)
  } catch (error) {
    notify(error instanceof Error ? error.message : '暂缓失败', true)
  }
}

async function doRelease(row: CoverageSegment) {
  try {
    await coverageApi.release(row.segment_id)
    await loadAll()
    notify(`${row.segment_code} 已恢复核对`)
  } catch (error) {
    notify(error instanceof Error ? error.message : '恢复失败', true)
  }
}

async function doPublish(row: CoverageSegment) {
  try {
    const result = await coverageApi.publish(row.segment_id)
    await loadAll()
    notify(
      result.deduplicated
        ? `${row.segment_code} 已发布过，本次并发发布只记一次`
        : `${row.segment_code} 发布成功：${result.graph_build_todo}`,
    )
  } catch (error) {
    notify(error instanceof Error ? error.message : '发布失败', true)
  }
}

async function doSearch() {
  if (!searchId.value.trim()) return
  try {
    searchResult.value = await coverageApi.search(searchId.value.trim())
  } catch (error) {
    notify(error instanceof Error ? error.message : '检索失败', true)
  }
}

async function doPlan() {
  if (!planGeneration.value) return
  try {
    planResult.value = await coverageApi.supplementPlan(planGeneration.value)
  } catch (error) {
    notify(error instanceof Error ? error.message : '补构建清单生成失败', true)
  }
}

onMounted(loadAll)
</script>

<style scoped>
.stage-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
}
.stage-published {
  background: #dcfce7;
  color: #166534;
}
.stage-ready {
  background: #dbeafe;
  color: #1e40af;
}
.stage-held {
  background: #fef3c7;
  color: #92400e;
}
.stage-registered {
  background: #f1f5f9;
  color: #475569;
}
.form-tip {
  background: #ecfdf5;
  border: 1px solid #a7f3d0;
  padding: 8px 12px;
  border-radius: 6px;
  font-size: 13px;
}
.form-tip.error {
  background: #fef2f2;
  border-color: #fecaca;
  color: #991b1b;
}
.create-form,
.result-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}
.result-panel h3 {
  margin: 0 0 8px;
  font-size: 14px;
}
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 20;
}
.modal-card {
  background: #fff;
  border-radius: 10px;
  padding: 18px;
  width: 460px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
</style>
