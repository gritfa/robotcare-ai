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

export interface KnowledgeHealth {
  status: 'normal' | 'knowledge_degraded' | 'external_model_unavailable'
  ready: boolean
  embedding_configured: boolean
  models: KnowledgeModelHealth[]
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
}

export interface KnowledgeAnswer {
  status: 'answered' | 'refused'
  answer: string | null
  citations: AnswerCitation[]
  refusal_reason: 'knowledge_gap' | 'model_refused' | 'citation_invalid' | 'unsafe_answer' | null
  record_id: number
}
