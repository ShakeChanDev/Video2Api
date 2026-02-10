<template>
  <div class="settings-page">
    <el-row :gutter="12">
      <el-col :span="24">
        <el-card class="glass-card" v-loading="loading">
          <template #header>
            <div class="card-title">系统设置</div>
          </template>

          <el-tabs v-model="activeTab" class="settings-tabs">
            <el-tab-pane label="连接" name="ixbrowser">
              <el-form :model="systemForm" label-width="200px">
                <el-form-item label="ixBrowser API Base">
                  <el-input v-model="systemForm.ixbrowser.api_base" />
                </el-form-item>
                <el-form-item label="请求超时（ms）">
                  <el-input-number v-model="systemForm.ixbrowser.request_timeout_ms" :min="1000" :max="120000" />
                </el-form-item>
                <el-form-item label="忙重试次数">
                  <el-input-number v-model="systemForm.ixbrowser.busy_retry_max" :min="0" :max="20" />
                </el-form-item>
                <el-form-item label="忙重试间隔（秒）">
                  <el-input-number v-model="systemForm.ixbrowser.busy_retry_delay_seconds" :min="0.1" :max="30" :step="0.1" />
                </el-form-item>
                <el-form-item label="分组窗口缓存 TTL（秒）">
                  <el-input-number v-model="systemForm.ixbrowser.group_windows_cache_ttl_sec" :min="5" :max="3600" />
                </el-form-item>
                <el-form-item label="实时配额缓存 TTL（秒）">
                  <el-input-number v-model="systemForm.ixbrowser.realtime_quota_cache_ttl_sec" :min="1" :max="600" />
                </el-form-item>
              </el-form>
            </el-tab-pane>

            <el-tab-pane label="任务" name="sora">
              <el-form :model="systemForm" label-width="200px">
                <el-form-item label="最大并发">
                  <el-input-number v-model="systemForm.sora.job_max_concurrency" :min="1" :max="10" />
                </el-form-item>
                <el-form-item label="任务轮询间隔（秒）">
                  <el-input-number v-model="systemForm.sora.generate_poll_interval_sec" :min="3" :max="60" />
                </el-form-item>
                <el-form-item label="任务最大等待（分钟）">
                  <el-input-number v-model="systemForm.sora.generate_max_minutes" :min="1" :max="120" />
                </el-form-item>
                <el-form-item label="Draft 等待超时（分钟）">
                  <el-input-number v-model="systemForm.sora.draft_wait_timeout_minutes" :min="1" :max="120" />
                </el-form-item>
                <el-form-item label="Draft 手动轮询（分钟）">
                  <el-input-number v-model="systemForm.sora.draft_manual_poll_interval_minutes" :min="1" :max="60" />
                </el-form-item>
                <el-form-item label="heavy load 换号总尝试次数">
                  <div class="field-row">
                    <el-input-number v-model="systemForm.sora.heavy_load_retry_max_attempts" :min="1" :max="10" />
                    <div class="inline-tip">heavy load 换号总尝试次数（含首次）。设为 1 表示不自动换号重试。</div>
                  </div>
                </el-form-item>
                <el-form-item label="阻塞资源类型">
                  <el-select
                    v-model="systemForm.sora.blocked_resource_types"
                    multiple
                    filterable
                    allow-create
                    default-first-option
                    style="width: 100%"
                  >
                    <el-option label="image" value="image" />
                    <el-option label="media" value="media" />
                    <el-option label="font" value="font" />
                  </el-select>
                </el-form-item>
                <el-form-item label="默认分组">
                  <el-input v-model="systemForm.sora.default_group_title" />
                </el-form-item>
                <el-form-item label="默认时长">
                  <el-select v-model="systemForm.sora.default_duration" style="width: 100%">
                    <el-option label="10s" value="10s" />
                    <el-option label="15s" value="15s" />
                    <el-option label="25s" value="25s" />
                  </el-select>
                </el-form-item>
                <el-form-item label="默认比例">
                  <el-select v-model="systemForm.sora.default_aspect_ratio" style="width: 100%">
                    <el-option label="landscape" value="landscape" />
                    <el-option label="portrait" value="portrait" />
                  </el-select>
                </el-form-item>

                <el-divider content-position="left">账号权重调度与自动恢复</el-divider>

                <el-form-item label="启用权重调度">
                  <div class="field-row">
                    <el-switch v-model="systemForm.sora.account_dispatch.enabled" />
                    <div class="inline-tip">关闭后仍可手动指定窗口，但不会自动选号。</div>
                  </div>
                </el-form-item>

                <el-form-item label="启用自动扫描">
                  <div class="field-row">
                    <el-switch v-model="systemForm.sora.account_dispatch.auto_scan_enabled" />
                    <div class="inline-tip">开启后会周期扫描账号配额，用于自动恢复 24h 次数。</div>
                  </div>
                </el-form-item>

                <el-form-item label="扫描间隔（分钟）">
                  <el-input-number v-model="systemForm.sora.account_dispatch.auto_scan_interval_minutes" :min="1" :max="360" />
                </el-form-item>

                <el-form-item label="扫描分组">
                  <el-input v-model="systemForm.sora.account_dispatch.auto_scan_group_title" />
                </el-form-item>

                <el-form-item label="评分窗口（小时）">
                  <el-input-number v-model="systemForm.sora.account_dispatch.lookback_hours" :min="1" :max="720" />
                </el-form-item>

                <el-form-item label="惩罚半衰期（小时）">
                  <el-input-number v-model="systemForm.sora.account_dispatch.decay_half_life_hours" :min="1" :max="720" />
                </el-form-item>

                <el-form-item label="数量权重">
                  <el-input-number v-model="systemForm.sora.account_dispatch.quantity_weight" :min="0" :max="1" :step="0.05" />
                </el-form-item>

                <el-form-item label="质量权重">
                  <el-input-number v-model="systemForm.sora.account_dispatch.quality_weight" :min="0" :max="1" :step="0.05" />
                </el-form-item>

                <el-form-item label="配额封顶">
                  <el-input-number v-model="systemForm.sora.account_dispatch.quota_cap" :min="1" :max="1000" />
                </el-form-item>

                <el-form-item label="最低剩余次数">
                  <el-input-number v-model="systemForm.sora.account_dispatch.min_quota_remaining" :min="0" :max="1000" />
                </el-form-item>

                <el-form-item label="未知配额分">
                  <el-input-number v-model="systemForm.sora.account_dispatch.unknown_quota_score" :min="0" :max="100" :step="1" />
                </el-form-item>

                <el-form-item label="默认质量分">
                  <el-input-number v-model="systemForm.sora.account_dispatch.default_quality_score" :min="0" :max="100" :step="1" />
                </el-form-item>

                <el-form-item label="活跃任务惩罚">
                  <el-input-number v-model="systemForm.sora.account_dispatch.active_job_penalty" :min="0" :max="100" :step="0.5" />
                </el-form-item>

                <el-form-item label="Plus 加分">
                  <el-input-number v-model="systemForm.sora.account_dispatch.plus_bonus" :min="0" :max="100" :step="0.5" />
                </el-form-item>

                <el-form-item label="忽略规则">
                  <div class="rule-panel">
                    <el-table
                      :data="systemForm.sora.account_dispatch.quality_ignore_rules"
                      size="small"
                      class="rule-table"
                      style="width: 100%"
                    >
                      <el-table-column label="phase" width="110">
                        <template #default="{ row }">
                          <el-input v-model="row.phase" placeholder="可选" />
                        </template>
                      </el-table-column>
                      <el-table-column label="message contains" min-width="220">
                        <template #default="{ row }">
                          <el-input v-model="row.message_contains" placeholder="例如：ixBrowser" />
                        </template>
                      </el-table-column>
                      <el-table-column label="操作" width="90" align="center">
                        <template #default="{ $index }">
                          <el-button size="small" type="danger" @click="removeIgnoreRule($index)">删除</el-button>
                        </template>
                      </el-table-column>
                    </el-table>
                    <div class="rule-actions">
                      <el-button size="small" class="btn-soft" @click="addIgnoreRule">新增忽略规则</el-button>
                    </div>
                  </div>
                </el-form-item>

                <el-form-item label="惩罚规则">
                  <div class="rule-panel">
                    <el-table
                      :data="systemForm.sora.account_dispatch.quality_error_rules"
                      size="small"
                      class="rule-table"
                      style="width: 100%"
                    >
                      <el-table-column label="phase" width="110">
                        <template #default="{ row }">
                          <el-input v-model="row.phase" placeholder="可选" />
                        </template>
                      </el-table-column>
                      <el-table-column label="message contains" min-width="220">
                        <template #default="{ row }">
                          <el-input v-model="row.message_contains" placeholder="例如：heavy load" />
                        </template>
                      </el-table-column>
                      <el-table-column label="penalty" width="110" align="center">
                        <template #default="{ row }">
                          <el-input-number v-model="row.penalty" :min="0" :max="100" :step="1" />
                        </template>
                      </el-table-column>
                      <el-table-column label="cooldown(m)" width="140" align="center">
                        <template #default="{ row }">
                          <el-input-number v-model="row.cooldown_minutes" :min="0" :max="10080" :step="1" />
                        </template>
                      </el-table-column>
                      <el-table-column label="block" width="90" align="center">
                        <template #default="{ row }">
                          <el-switch v-model="row.block_during_cooldown" />
                        </template>
                      </el-table-column>
                      <el-table-column label="操作" width="90" align="center">
                        <template #default="{ $index }">
                          <el-button size="small" type="danger" @click="removeErrorRule($index)">删除</el-button>
                        </template>
                      </el-table-column>
                    </el-table>
                    <div class="rule-actions">
                      <el-button size="small" class="btn-soft" @click="addErrorRule">新增惩罚规则</el-button>
                    </div>
                  </div>
                </el-form-item>

                <el-form-item label="默认惩罚">
                  <div class="default-rule-grid">
                    <div class="field-row">
                      <span class="rule-label">Penalty</span>
                      <el-input-number
                        v-model="systemForm.sora.account_dispatch.default_error_rule.penalty"
                        :min="0"
                        :max="100"
                        :step="1"
                      />
                    </div>
                    <div class="field-row">
                      <span class="rule-label">Cooldown(m)</span>
                      <el-input-number
                        v-model="systemForm.sora.account_dispatch.default_error_rule.cooldown_minutes"
                        :min="0"
                        :max="10080"
                        :step="1"
                      />
                    </div>
                    <div class="field-row">
                      <span class="rule-label">Block</span>
                      <el-switch v-model="systemForm.sora.account_dispatch.default_error_rule.block_during_cooldown" />
                    </div>
                  </div>
                </el-form-item>
              </el-form>
            </el-tab-pane>

            <el-tab-pane label="去水印" name="watermark">
              <el-form :model="watermarkForm" label-width="200px">
                <el-form-item label="启用去水印">
                  <div class="field-row">
                    <el-switch v-model="watermarkForm.enabled" />
                    <div class="inline-tip">关闭后将无法完成任务。</div>
                  </div>
                </el-form-item>
                <el-form-item label="解析方式">
                  <el-select v-model="watermarkForm.parse_method" style="width: 100%">
                    <el-option label="自定义解析" value="custom" />
                    <el-option label="第三方解析" value="third_party" />
                  </el-select>
                </el-form-item>
                <template v-if="watermarkForm.parse_method === 'custom'">
                  <el-form-item label="解析服务器地址">
                    <el-input v-model="watermarkForm.custom_parse_url" placeholder="例如：http://127.0.0.1:5001" />
                  </el-form-item>
                  <el-form-item label="访问密钥">
                    <el-input
                      v-model="watermarkForm.custom_parse_token"
                      type="password"
                      show-password
                      placeholder="可选"
                    />
                  </el-form-item>
                  <el-form-item label="解析路径">
                    <el-input v-model="watermarkForm.custom_parse_path" placeholder="/get-sora-link" />
                  </el-form-item>
                </template>
                <el-form-item label="最大重试次数">
                  <el-input-number v-model="watermarkForm.retry_max" :min="0" :max="10" />
                </el-form-item>
              </el-form>
            </el-tab-pane>

            <el-tab-pane label="扫描" name="scan">
              <el-form :model="systemForm" label-width="200px">
                <el-form-item label="历史保留条数">
                  <el-input-number v-model="systemForm.scan.history_limit" :min="1" :max="50" />
                </el-form-item>
                <el-form-item label="默认分组">
                  <el-input v-model="systemForm.scan.default_group_title" />
                </el-form-item>
              </el-form>
            </el-tab-pane>

            <el-tab-pane label="日志" name="logging">
              <el-form :model="systemForm" label-width="200px">
                <el-form-item label="日志等级">
                  <div class="field-row">
                    <el-select v-model="systemForm.logging.log_level" class="flex-1">
                      <el-option label="DEBUG" value="DEBUG" />
                      <el-option label="INFO" value="INFO" />
                      <el-option label="WARN" value="WARN" />
                      <el-option label="ERROR" value="ERROR" />
                    </el-select>
                    <el-tag v-if="isRequiresRestart('logging.log_level')" type="warning" size="small">需重启</el-tag>
                  </div>
                </el-form-item>
                <el-form-item label="日志文件">
                  <div class="field-row">
                    <el-input v-model="systemForm.logging.log_file" class="flex-1" />
                    <el-tag v-if="isRequiresRestart('logging.log_file')" type="warning" size="small">需重启</el-tag>
                  </div>
                </el-form-item>
                <el-form-item label="日志最大大小（Bytes）">
                  <div class="field-row">
                    <el-input-number v-model="systemForm.logging.log_max_bytes" :min="1048576" :max="104857600" class="flex-1" />
                    <el-tag v-if="isRequiresRestart('logging.log_max_bytes')" type="warning" size="small">需重启</el-tag>
                  </div>
                </el-form-item>
                <el-form-item label="日志备份数量">
                  <div class="field-row">
                    <el-input-number v-model="systemForm.logging.log_backup_count" :min="1" :max="100" class="flex-1" />
                    <el-tag v-if="isRequiresRestart('logging.log_backup_count')" type="warning" size="small">需重启</el-tag>
                  </div>
                </el-form-item>
                <el-form-item label="事件日志保留天数">
                  <el-input-number v-model="systemForm.logging.event_log_retention_days" :min="0" :max="3650" />
                </el-form-item>
                <el-form-item label="事件日志清理间隔（秒）">
                  <el-input-number v-model="systemForm.logging.event_log_cleanup_interval_sec" :min="60" :max="86400" />
                </el-form-item>
                <el-form-item label="事件日志最大大小（MB）">
                  <el-input-number v-model="systemForm.logging.event_log_max_mb" :min="1" :max="10240" />
                </el-form-item>
                <el-form-item label="API 日志采集模式">
                  <el-select v-model="systemForm.logging.api_log_capture_mode" style="width: 100%">
                    <el-option label="全部采集 (all)" value="all" />
                    <el-option label="失败或慢请求 (failed_slow)" value="failed_slow" />
                    <el-option label="仅失败 (failed_only)" value="failed_only" />
                  </el-select>
                </el-form-item>
                <el-form-item label="慢请求阈值（ms）">
                  <el-input-number v-model="systemForm.logging.api_slow_threshold_ms" :min="100" :max="120000" />
                </el-form-item>
                <el-form-item label="日志脱敏模式">
                  <el-select v-model="systemForm.logging.log_mask_mode" style="width: 100%">
                    <el-option label="基础脱敏 (basic)" value="basic" />
                    <el-option label="关闭脱敏 (off)" value="off" />
                  </el-select>
                </el-form-item>
                <el-form-item label="系统日志入库等级">
                  <el-select v-model="systemForm.logging.system_logger_ingest_level" style="width: 100%">
                    <el-option label="DEBUG" value="DEBUG" />
                    <el-option label="INFO" value="INFO" />
                    <el-option label="WARN" value="WARN" />
                    <el-option label="ERROR" value="ERROR" />
                  </el-select>
                </el-form-item>
                <el-form-item label="审计日志保留天数">
                  <el-input-number v-model="systemForm.logging.audit_log_retention_days" :min="0" :max="365" />
                </el-form-item>
                <el-form-item label="审计清理间隔（秒）">
                  <el-input-number v-model="systemForm.logging.audit_log_cleanup_interval_sec" :min="60" :max="86400" />
                </el-form-item>
              </el-form>
            </el-tab-pane>

            <el-tab-pane label="安全" name="security">
              <el-form :model="systemForm" label-width="200px">
                <el-form-item label="JWT Secret">
                  <div class="field-row">
                    <el-input
                      v-model="systemForm.auth.secret_key"
                      type="password"
                      show-password
                      placeholder="留空不修改"
                      class="flex-1"
                    />
                    <el-tag v-if="isRequiresRestart('auth.secret_key')" type="warning" size="small">需重启</el-tag>
                  </div>
                </el-form-item>
                <el-form-item label="JWT Algorithm">
                  <div class="field-row">
                    <el-input v-model="systemForm.auth.algorithm" class="flex-1" />
                    <el-tag v-if="isRequiresRestart('auth.algorithm')" type="warning" size="small">需重启</el-tag>
                  </div>
                </el-form-item>
                <el-form-item label="对外视频接口 Token">
                  <el-input v-model="systemForm.video_api.bearer_token" placeholder="留空则 /v1/videos 返回 503（关闭状态）" />
                  <div class="inline-tip">用于 /v1/videos Bearer 鉴权，保存后立即生效。</div>
                </el-form-item>
                <el-form-item label="Token 过期（分钟）">
                  <el-input-number v-model="systemForm.auth.access_token_expire_minutes" :min="5" :max="10080" />
                </el-form-item>
              </el-form>
            </el-tab-pane>

            <el-tab-pane label="服务" name="server">
              <el-form :model="systemForm" label-width="200px">
                <el-form-item label="应用名称">
                  <div class="field-row">
                    <el-input v-model="systemForm.server.app_name" class="flex-1" />
                    <el-tag v-if="isRequiresRestart('server.app_name')" type="warning" size="small">需重启</el-tag>
                  </div>
                </el-form-item>
                <el-form-item label="调试模式">
                  <div class="field-row">
                    <el-switch v-model="systemForm.server.debug" />
                    <el-tag v-if="isRequiresRestart('server.debug')" type="warning" size="small">需重启</el-tag>
                  </div>
                </el-form-item>
                <el-form-item label="监听地址">
                  <div class="field-row">
                    <el-input v-model="systemForm.server.host" class="flex-1" />
                    <el-tag v-if="isRequiresRestart('server.host')" type="warning" size="small">需重启</el-tag>
                  </div>
                </el-form-item>
                <el-form-item label="端口">
                  <div class="field-row">
                    <el-input-number v-model="systemForm.server.port" :min="1" :max="65535" class="flex-1" />
                    <el-tag v-if="isRequiresRestart('server.port')" type="warning" size="small">需重启</el-tag>
                  </div>
                </el-form-item>
              </el-form>
            </el-tab-pane>

            <el-tab-pane label="定时" name="scheduler">
              <el-form :model="schedulerForm" label-width="140px">
                <el-form-item label="启用定时">
                  <el-switch v-model="schedulerForm.enabled" />
                </el-form-item>
                <el-form-item label="执行时刻">
                  <el-input v-model="schedulerForm.times" placeholder="例如：09:00,13:30,21:10" />
                  <div class="inline-tip">24 小时制，多个时刻用英文逗号分隔。</div>
                </el-form-item>
                <el-form-item label="时区">
                  <el-input v-model="schedulerForm.timezone" />
                </el-form-item>
              </el-form>
            </el-tab-pane>
          </el-tabs>
        </el-card>
      </el-col>
    </el-row>

    <el-card class="glass-card save-card">
      <div class="save-row">
        <div class="save-left">
          <div class="save-desc">配置修改后立即生效（标记需重启的除外）</div>
          <div class="save-meta" v-if="systemUpdatedAt">最近保存：{{ systemUpdatedAt }}</div>
        </div>
        <div class="save-actions">
          <el-button @click="loadAll">重置</el-button>
          <el-button type="primary" :loading="saving" @click="saveAll">保存设置</el-button>
        </div>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import {
  getScanSchedulerConfig,
  getSystemSettings,
  updateScanSchedulerConfig,
  updateSystemSettings,
  getWatermarkFreeConfig,
  updateWatermarkFreeConfig
} from '../api'

const loading = ref(false)
const saving = ref(false)
const activeTab = ref('ixbrowser')

const defaultSystemForm = {
  ixbrowser: {
    api_base: 'http://127.0.0.1:53200',
    request_timeout_ms: 10000,
    busy_retry_max: 6,
    busy_retry_delay_seconds: 1.2,
    group_windows_cache_ttl_sec: 120,
    realtime_quota_cache_ttl_sec: 30
  },
  sora: {
    job_max_concurrency: 2,
    generate_poll_interval_sec: 6,
    generate_max_minutes: 30,
    draft_wait_timeout_minutes: 20,
    draft_manual_poll_interval_minutes: 5,
    heavy_load_retry_max_attempts: 4,
    blocked_resource_types: ['image', 'media', 'font'],
    default_group_title: 'Sora',
    default_duration: '10s',
    default_aspect_ratio: 'landscape',
    account_dispatch: {
      enabled: true,
      auto_scan_enabled: true,
      auto_scan_interval_minutes: 10,
      auto_scan_group_title: 'Sora',
      lookback_hours: 72,
      decay_half_life_hours: 24,
      quantity_weight: 0.45,
      quality_weight: 0.55,
      quota_cap: 30,
      min_quota_remaining: 2,
      unknown_quota_score: 40,
      default_quality_score: 70,
      active_job_penalty: 8,
      plus_bonus: 5,
      quality_ignore_rules: [
        { phase: null, message_contains: 'ixBrowser' },
        { phase: null, message_contains: '调用 ixBrowser' },
        { phase: 'publish', message_contains: '未找到发布按钮' },
        { phase: 'publish', message_contains: '发布未返回链' },
        { phase: 'publish', message_contains: '发布未返回链接' },
        { phase: 'submit', message_contains: '未找到提示词输入框' }
      ],
      quality_error_rules: [
        { phase: null, message_contains: 'heavy load', penalty: 8, cooldown_minutes: 15, block_during_cooldown: true },
        {
          phase: null,
          message_contains: 'execution context was destroyed',
          penalty: 14,
          cooldown_minutes: 45,
          block_during_cooldown: false
        }
      ],
      default_error_rule: { penalty: 10, cooldown_minutes: 30, block_during_cooldown: false }
    }
  },
  scan: {
    history_limit: 10,
    default_group_title: 'Sora'
  },
  logging: {
    log_level: 'INFO',
    log_file: 'logs/app.log',
    log_max_bytes: 10485760,
    log_backup_count: 5,
    event_log_retention_days: 30,
    event_log_cleanup_interval_sec: 3600,
    event_log_max_mb: 100,
    api_log_capture_mode: 'all',
    api_slow_threshold_ms: 2000,
    log_mask_mode: 'basic',
    system_logger_ingest_level: 'DEBUG',
    audit_log_retention_days: 3,
    audit_log_cleanup_interval_sec: 3600
  },
  auth: {
    secret_key: null,
    algorithm: 'HS256',
    access_token_expire_minutes: 10080
  },
  video_api: {
    bearer_token: ''
  },
  server: {
    app_name: 'Video2Api',
    debug: true,
    host: '0.0.0.0',
    port: 8001
  }
}

const defaultSchedulerForm = {
  enabled: false,
  times: '09:00,13:30,21:00',
  timezone: 'Asia/Shanghai'
}

const defaultWatermarkForm = {
  enabled: true,
  parse_method: 'custom',
  custom_parse_url: '',
  custom_parse_token: '',
  custom_parse_path: '/get-sora-link',
  retry_max: 2
}

const systemForm = ref({ ...defaultSystemForm })
const schedulerForm = ref({ ...defaultSchedulerForm })
const watermarkForm = ref({ ...defaultWatermarkForm })
const systemDefaults = ref({ ...defaultSystemForm })
const schedulerDefaults = ref({ ...defaultSchedulerForm })
const watermarkDefaults = ref({ ...defaultWatermarkForm })
const requiresRestart = ref([])
const systemUpdatedAt = ref('')

const mergeSettings = (base, override) => {
  const result = Array.isArray(base) ? [...base] : { ...base }
  Object.keys(override || {}).forEach((key) => {
    const baseValue = result[key]
    const overrideValue = override[key]
    if (
      baseValue &&
      overrideValue &&
      typeof baseValue === 'object' &&
      !Array.isArray(baseValue) &&
      typeof overrideValue === 'object' &&
      !Array.isArray(overrideValue)
    ) {
      result[key] = mergeSettings(baseValue, overrideValue)
    } else {
      result[key] = overrideValue
    }
  })
  return result
}

const isRequiresRestart = (path) => requiresRestart.value.includes(path)

const loadAll = async () => {
  loading.value = true
  try {
    const [systemEnvelope, schedulerEnvelope, watermarkConfig] = await Promise.all([
      getSystemSettings(),
      getScanSchedulerConfig(),
      getWatermarkFreeConfig()
    ])
    const systemData = systemEnvelope?.data || {}
    const systemDefault = systemEnvelope?.defaults || defaultSystemForm
    systemDefaults.value = mergeSettings(defaultSystemForm, systemDefault)
    systemForm.value = mergeSettings(systemDefaults.value, systemData)
    requiresRestart.value = systemEnvelope?.requires_restart || []
    systemUpdatedAt.value = systemEnvelope?.updated_at || ''

    const schedulerData = schedulerEnvelope?.data || {}
    const schedulerDefault = schedulerEnvelope?.defaults || defaultSchedulerForm
    schedulerDefaults.value = mergeSettings(defaultSchedulerForm, schedulerDefault)
    schedulerForm.value = mergeSettings(schedulerDefaults.value, schedulerData)

    const watermarkData = watermarkConfig || {}
    watermarkDefaults.value = mergeSettings(defaultWatermarkForm, watermarkData)
    watermarkForm.value = mergeSettings(defaultWatermarkForm, watermarkData)
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '读取系统设置失败')
  } finally {
    loading.value = false
  }
}

const validateTimes = (value) => {
  const times = String(value || '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean)
  if (times.length === 0) {
    return false
  }
  const pattern = /^([01]\d|2[0-3]):[0-5]\d$/
  return times.every((item) => pattern.test(item))
}

const normalizePayload = (payload) => {
  const data = JSON.parse(JSON.stringify(payload))
  if (data?.auth) {
    if (data.auth.secret_key !== null && typeof data.auth.secret_key === 'string') {
      if (!data.auth.secret_key.trim()) {
        data.auth.secret_key = null
      }
    }
  }
  if (data?.video_api && typeof data.video_api.bearer_token === 'string') {
    data.video_api.bearer_token = data.video_api.bearer_token.trim()
  }
  return data
}

const addIgnoreRule = () => {
  const cfg = systemForm.value?.sora?.account_dispatch
  if (!cfg) return
  if (!Array.isArray(cfg.quality_ignore_rules)) {
    cfg.quality_ignore_rules = []
  }
  cfg.quality_ignore_rules.push({ phase: null, message_contains: '' })
}

const removeIgnoreRule = (index) => {
  const cfg = systemForm.value?.sora?.account_dispatch
  if (!cfg) return
  if (!Array.isArray(cfg.quality_ignore_rules)) return
  cfg.quality_ignore_rules.splice(index, 1)
}

const addErrorRule = () => {
  const cfg = systemForm.value?.sora?.account_dispatch
  if (!cfg) return
  if (!Array.isArray(cfg.quality_error_rules)) {
    cfg.quality_error_rules = []
  }
  cfg.quality_error_rules.push({
    phase: null,
    message_contains: '',
    penalty: 10,
    cooldown_minutes: 30,
    block_during_cooldown: false
  })
}

const removeErrorRule = (index) => {
  const cfg = systemForm.value?.sora?.account_dispatch
  if (!cfg) return
  if (!Array.isArray(cfg.quality_error_rules)) return
  cfg.quality_error_rules.splice(index, 1)
}

const saveAll = async () => {
  if (!validateTimes(schedulerForm.value.times)) {
    ElMessage.warning('执行时刻格式不正确，请输入 HH:mm 并用逗号分隔')
    activeTab.value = 'scheduler'
    return
  }
  if (watermarkForm.value.enabled && watermarkForm.value.parse_method === 'custom') {
    const url = String(watermarkForm.value.custom_parse_url || '').trim()
    if (!url) {
      ElMessage.warning('请填写去水印解析服务器地址')
      activeTab.value = 'watermark'
      return
    }
  }
  saving.value = true
  try {
    const payload = normalizePayload(systemForm.value)
    const watermarkPayload = {
      ...watermarkForm.value,
      custom_parse_path: (watermarkForm.value.custom_parse_path || '').trim() || '/get-sora-link'
    }
    const [systemEnvelope, schedulerEnvelope, watermarkConfig] = await Promise.all([
      updateSystemSettings(payload),
      updateScanSchedulerConfig(schedulerForm.value),
      updateWatermarkFreeConfig(watermarkPayload)
    ])
    requiresRestart.value = systemEnvelope?.requires_restart || []
    systemUpdatedAt.value = systemEnvelope?.updated_at || ''
    const systemData = systemEnvelope?.data || {}
    const systemDefault = systemEnvelope?.defaults || defaultSystemForm
    systemDefaults.value = mergeSettings(defaultSystemForm, systemDefault)
    systemForm.value = mergeSettings(systemDefaults.value, systemData)

    const schedulerData = schedulerEnvelope?.data || {}
    const schedulerDefault = schedulerEnvelope?.defaults || defaultSchedulerForm
    schedulerDefaults.value = mergeSettings(defaultSchedulerForm, schedulerDefault)
    schedulerForm.value = mergeSettings(schedulerDefaults.value, schedulerData)

    if (watermarkConfig) {
      watermarkDefaults.value = mergeSettings(defaultWatermarkForm, watermarkConfig)
      watermarkForm.value = mergeSettings(defaultWatermarkForm, watermarkConfig)
    }

    ElMessage.success('系统设置已保存并生效')
  } catch (error) {
    ElMessage.error(error?.response?.data?.detail || '保存失败')
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  await loadAll()
})
</script>

<style scoped>
.settings-page {
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--page-gap);
  background: transparent;
}

.card-title {
  font-weight: 700;
  color: var(--ink);
}

.settings-tabs :deep(.el-tabs__content) {
  padding-top: 4px;
}

.inline-tip {
  margin-top: 6px;
  font-size: 12px;
  color: #475569;
}

.save-card {
  margin-top: 0;
}

.save-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.save-left {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.save-desc {
  color: #0f172a;
  font-weight: 600;
}

.save-meta {
  color: #64748b;
  font-size: 12px;
}

.save-actions {
  display: flex;
  gap: 10px;
}

.field-row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
}

.flex-1 {
  flex: 1;
}

.rule-panel {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.rule-actions {
  display: flex;
  justify-content: flex-end;
}

.rule-table :deep(.el-input__wrapper) {
  box-shadow: none;
}

.default-rule-grid {
  width: 100%;
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}

.rule-label {
  font-size: 12px;
  color: #475569;
  min-width: 78px;
}

@media (max-width: 920px) {
  .default-rule-grid {
    grid-template-columns: 1fr;
  }
}
</style>
