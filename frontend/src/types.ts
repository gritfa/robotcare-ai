export type EntityId = string | number
export interface User { id: EntityId; email: string; name?: string; full_name?: string; role?: 'user' | 'admin' }
export interface AuthResult { access_token: string; token_type?: string; user?: User }
export interface RobotModel { id: EntityId; code: string; name: string; brand?: string; description?: string; image_url?: string; enabled?: boolean }
export interface Device { id: EntityId; nickname: string; robot_model_id: EntityId; robot_model?: RobotModel; serial_number?: string; purchase_date?: string; created_at?: string }
export interface DiagnosticOption {
  stable_key: string
  version: number
  issue_category_code: string
  issue_category_name: string
  title: string
  status?: 'draft' | 'published' | 'retired'
}
export type DiagnosticStatus = 'collecting' | 'in_progress' | 'resolved' | 'unresolved' | 'report_ready' | 'cancelled'
export interface DiagnosticStep { id: EntityId; position: number; title: string; instruction: string; source_label: string; safety_note?: string; options?: string[] }
export interface StepExecution { id?: EntityId; outcome: 'resolved'|'not_resolved'; step: DiagnosticStep; created_at?: string }
export interface Attachment {
  id: EntityId
  session_id: EntityId
  original_filename: string
  content_type: string
  size_bytes: number
  created_at: string
}
export interface Diagnostic {
  id: EntityId; device_id: EntityId; device?: Device; issue_description: string; error_code?: string;
  issue_category_code?: string; status: DiagnosticStatus; resolved?: boolean; current_position?: number;
  report_available?: boolean; executions?: StepExecution[]; attachments?: Attachment[]; created_at?: string; updated_at?: string
}
export interface ServiceReport { id: EntityId; session_id: EntityId; report_number: string; content: string; created_at: string }
export interface ReportPdf { filename: string; size_bytes: number; download_url: string }

export interface KnowledgeSearchResult {
  score: number
  content: string
  document_title: string
  source_url: string
  page_number: number
}

export interface KnowledgeModelStatus {
  robot_model_id: number
  model_code: string
  document_count: number
  chunk_count: number
  vector_count: number
}

export interface KnowledgeModelHealth extends KnowledgeModelStatus {
  document_sha256s: string[]
  ready: boolean
}

export interface KnowledgeProbe {
  embedding_service: boolean
  retrieval_end_to_end: boolean
  generation_service: boolean
  errors: string[]
}

export interface KnowledgeHealth {
  status: 'normal' | 'knowledge_degraded' | 'external_model_unavailable'
  ready: boolean
  embedding_configured: boolean
  models: KnowledgeModelHealth[]
  probe?: KnowledgeProbe | null
}

export interface AdminGenerationStats {
  answered_count: number
  refused_count: number
  refusal_by_reason: Record<string, number>
}

export interface AdminOverview {
  user_count: number
  active_model_count: number
  published_flow_count: number
  knowledge_document_count: number
  knowledge_chunk_count: number
  safety_block_count: number
  unresolved_diagnostic_count: number
  service_report_count: number
  generation_stats: AdminGenerationStats
  content_gap_count: number
}

export type ContentGapStatus = 'open' | 'investigating' | 'resolved' | 'wont_fix'
export type GapReplayStatus = 'passed' | 'failed' | 'error'

export interface AdminContentGap {
  query_normalized: string
  count: number
  robot_model_id: EntityId
  model_code: string
  last_seen_at: string
  status: ContentGapStatus
  linked_document_id: EntityId | null
  linked_document_title: string | null
  replay_status: GapReplayStatus | null
  replay_citation_count: number | null
  replay_answer_excerpt: string | null
  replay_checked_at: string | null
  resolved_at: string | null
  note: string | null
}

export interface AdminContentGapResolution {
  status: ContentGapStatus
  linked_document_id: EntityId | null
  linked_document_title: string | null
  replay_status: GapReplayStatus | null
  replay_citation_count: number | null
  replay_answer_excerpt: string | null
  replay_checked_at: string | null
  resolved_at: string | null
  note: string | null
}

export interface AdminContentGapReplay {
  replay_status: GapReplayStatus
  citation_count: number
  answer_excerpt: string | null
  detail: string
  gap_status: ContentGapStatus
  checked_at: string | null
}

export interface AdminKnowledgeDocument {
  id: EntityId
  robot_model_id: EntityId
  model_code: string
  title: string
  source_url: string
  sha256: string
  page_count: number
  chunk_count: number
  vector_count: number
  status: 'active' | 'disabled'
  version: number
  embedding_model: string | null
  file_size: number | null
  has_archived_file: boolean
  created_at: string
  updated_at: string
}

export interface AdminKnowledgeDocumentVersion {
  version: number
  sha256: string
  title: string
  page_count: number
  chunk_count: number
  change_kind: 'upload' | 'reindex' | 'rollback' | 'release'
  note: string | null
  has_archived_file: boolean
  embedding_model: string | null
  created_at: string
}

export interface AdminKnowledgeDocumentDetail extends AdminKnowledgeDocument {
  versions: AdminKnowledgeDocumentVersion[]
}

export interface AdminKnowledgeChunk {
  chunk_index: number
  page_number: number
  content: string
  has_embedding: boolean
}

export interface AdminKnowledgeChunkPage {
  document_id: EntityId
  total: number
  offset: number
  limit: number
  items: AdminKnowledgeChunk[]
}

export interface AdminKnowledgeDiffPreview {
  status: 'new' | 'identical' | 'changed'
  model_code: string
  source_url: string
  incoming_title: string
  incoming_sha256: string
  incoming_page_count: number
  incoming_chunk_count: number
  document_id: EntityId | null
  current_version: number | null
  current_title: string | null
  current_sha256: string | null
  current_page_count: number | null
  current_chunk_count: number | null
  current_updated_at: string | null
  page_delta: number | null
  chunk_delta: number | null
  pages_comparable: boolean
  pages_incomparable_reason: string | null
  changed_pages: number[]
  added_pages: number[]
  removed_pages: number[]
}

export interface AdminKnowledgeUploadResult {
  document_id: EntityId
  created: boolean
  changed: boolean
  chunk_count: number
  sha256: string
}

export interface AdminModel {
  id: EntityId
  code: string
  name: string
  brand: string
  active: boolean
}

export interface AdminSafetyBlock {
  id: EntityId
  model_code: string
  category: string
  risk_level: string
  created_at: string
}

export interface AdminSafetyBlockDetail extends AdminSafetyBlock {
  user_id: EntityId
  device_id: EntityId
  reason: string
  advice: string
}

export interface AdminUnresolvedReport {
  report: {
    id: EntityId
    report_number: string
    created_at: string
  }
  diagnostic: {
    id: EntityId
    status: string
    created_at: string
  }
  model: {
    id: EntityId
    code: string
    name: string
  }
  user: {
    email_masked: string
  }
}

export interface AdminDiagnosticDetail {
  id: EntityId
  user_id: EntityId
  device_id: EntityId
  flow_id: EntityId
  status: string
  issue_description: string
  error_code?: string | null
  created_at: string
}

export interface AdminServiceReportDetail {
  id: EntityId
  session_id: EntityId
  report_number: string
  content: string
  created_at: string
}

export interface AdminAuditLog {
  id: EntityId
  actor_user_id: EntityId | null
  action: string
  resource_type: string
  resource_id: string | null
  details_json: Record<string, unknown>
  created_at: string
}

export interface AnswerCitation {
  index: number
  source_url: string
  page_number: number
  score: number
  document_sha256: string
  /** 检索到的原文；证据抽屉要展示它，没有原文的引用无法核验 */
  snippet?: string
  document_title?: string
}

export interface KnowledgeAnswer {
  status: 'answered' | 'refused'
  answer: string | null
  citations: AnswerCitation[]
  refusal_reason: 'knowledge_gap' | 'model_refused' | 'citation_invalid' | 'unsafe_answer' | null
  record_id: number
}

export interface Conversation {
  id: number
  robot_model_id: number
  robot_model_code: string
  title: string
  resolved?: boolean | null
  updated_at: string
}

export type MessageIntent = 'smalltalk' | 'capability' | 'action' | 'followup' | 'knowledge'

export interface QuickAction {
  code: string
  label: string
  diagnostic_id?: number
}

export type FeedbackReason =
  | 'off_topic'
  | 'unclear_steps'
  | 'wrong_citation'
  | 'wrong_model'
  | 'still_unresolved'

export interface MessageFeedback {
  message_id: number
  helpful: boolean
  reason: FeedbackReason | null
}
export type MessageActionCode = 'start_diagnostic' | 'generate_report' | 'upload_image'

export interface ChatMessage {
  id: EntityId
  role: 'user' | 'assistant'
  content: string
  citations: AnswerCitation[]
  refusal_reason: KnowledgeAnswer['refusal_reason']
  // 后端路由层结论；路由层上线前的历史消息为 null，按普通回答渲染
  intent?: MessageIntent | null
  action_code?: MessageActionCode | null
  quick_actions?: QuickAction[]
  created_at: string
}

export interface ConversationDetail {
  id: number
  robot_model_id: number
  robot_model_code: string
  title: string
  messages: ChatMessage[]
}

export interface ChatMessagePair {
  user_message: ChatMessage
  assistant_message: ChatMessage
}
