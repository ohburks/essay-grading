// Data model for the platform frontend — ported from TGFWA src/types.ts.
// Shapes mirror the API's camelCase serialisation (backend/app/api/sessions.py).

export type Speaker = 'student' | 'assistant';

export interface TraceTurn {
  turnId: number;
  speaker: Speaker;
  text: string;
  timestamp?: string;
}

export interface Trace {
  traceId: string;
  assignmentId: string;
  turns: TraceTurn[];
}

export type Referenceability = 'strong' | 'weak';

export interface RubricCriterion {
  criterionId: string;
  standard: string;
  dimension: string;
  statement: string;
  scale: '0-5';
  anchors: Record<string, string>;
  referenceability: Referenceability;
  source: string;
  teacherGuidance?: string;
}

export interface Rubric {
  rubricId: string;
  version: string;
  genre: string;
  criteria: RubricCriterion[];
  assignmentGuidance?: string;
}

export type Channel = 'trace' | 'product';

export interface EvidenceItem {
  turnId?: number;
  quote: string;
  reasoning: string;
}

export type ConfidenceLevel = 'low' | 'med' | 'high';

export interface TeacherOverride {
  score: number;
  rationale: string;
  ts: string;
}

export interface ScoreRecord {
  criterionId: string;
  channel: Channel;
  passes: (number | 'no-evidence')[];
  median: number | null;
  spread: number | null;
  noEvidence: boolean;
  confidence: ConfidenceLevel;
  evidence: EvidenceItem[];
  anchorMatched?: string | null;
  styleApplied: string | null;
  rubricVersion: string;
  gradedAt: string;
  teacherOverride: TeacherOverride | null;
  needsReview: boolean;
  reviewReasons: string[];
  // present on cross-session queue rows
  assessmentId?: string;
  assessmentName?: string;
  username?: string;
}

export type RelianceMode = 'passive' | 'active' | 'constructive';
export type RelianceLabel = 'reflective' | 'cautious' | 'thoughtless' | 'collaborative' | 'undetermined';

export interface SegmentCoding {
  segmentTurns: number[];
  helpSeeking: RelianceMode;
  responseUse: RelianceMode;
  evidence: string;
  verification: boolean;
}

export interface LayerBResult {
  segments: SegmentCoding[];
  grid: Record<RelianceMode, Record<RelianceMode, number>>;
  dominantHelpSeeking: RelianceMode;
  dominantResponseUse: RelianceMode;
  interpretiveLabel: RelianceLabel;
  verificationRate: number;
}

export interface DimensionDivergence {
  dimension: string;
  standard: string;
  traceScore: number | null;
  productScore: number | null;
  divergence: number | null;
  criterionIds: string[];
}

export interface DivergenceInterpretation {
  headline: string;
  detail: string;
  tone: 'flag' | 'target' | 'valid' | 'neutral';
}

export type AssessmentMode = 'essay_trace';
export type AssessmentStatus = 'draft' | 'in_progress' | 'grading' | 'graded' | 'error';

export interface AssessmentSummary {
  id: string;
  username: string;
  mode: AssessmentMode;
  status: AssessmentStatus;
  name: string;
  description: string;
  contentId: string;
  contentVersion: string;
  isExemplar: boolean;
  gradedLive: boolean;
  createdAt: string;
  completedAt: string;
}

export interface AssessmentDetail extends AssessmentSummary {
  artifacts: {
    essay?: string;
    trace?: Trace;
    [key: string]: unknown;
  };
  scores?: ScoreRecord[];
  layerB?: LayerBResult | null;
  divergence?: DimensionDivergence[];
  interpretation?: DivergenceInterpretation;
  evaluations?: unknown[];
}

export interface ContentItem<T = Record<string, unknown>> {
  contentId: string;
  version: string;
  createdBy: string;
  createdAt: string;
  payload: T;
}

export interface GradingProgress {
  done: number;
  total: number;
  label: string;
}

export type CitationStatus = 'pending' | 'fetched' | 'indexed' | 'unverifiable' | 'error';
export type ResolutionMethod = 'direct_url' | 'doi' | 'crossref_search' | 'unresolved' | '';

export interface CitationEntry {
  ordinal: number;
  sourceKey: string;
  rawCitation: string;
  resolvedUrl: string;
  resolutionMethod: ResolutionMethod;
  status: CitationStatus;
  error: string;
  chunkCount: number;
}

export type FactCheckVerdict =
  | 'supported'
  | 'contradicted'
  | 'not_addressed'
  | 'unverifiable_source'
  | 'unchecked';

export interface FactCheckStatement {
  statementIndex: number;
  statementText: string;
  citationMarker: string;
  sourceKey: string;
  matchConfidence: 'high' | 'low' | '';
  verdict: FactCheckVerdict;
  evidenceQuote: string;
  reasoning: string;
  checkedAt: string;
}

export interface FactCheckResult {
  citations: CitationEntry[];
  statements: FactCheckStatement[];
}

export function median(nums: number[]): number {
  const s = [...nums].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

export function effectiveScore(r: ScoreRecord): number | null {
  if (r.teacherOverride) return r.teacherOverride.score;
  return r.noEvidence ? null : r.median;
}

export function downloadJSON(filename: string, data: unknown): void {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}
