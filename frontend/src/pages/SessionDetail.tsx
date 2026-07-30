import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import { api } from '../api/client';
import { Dashboard } from '../components/Dashboard';
import { FactCheckPanel } from '../components/FactCheckPanel';
import { LayerBPanel } from '../components/LayerBPanel';
import type { AssessmentDetail, ContentItem, GradingProgress, Rubric } from '../types';

type Tab = 'dashboard' | 'layerb' | 'factcheck';

export default function SessionDetail() {
  const { id = '' } = useParams();
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>('dashboard');
  const [progress, setProgress] = useState<GradingProgress | null>(null);
  const [error, setError] = useState('');
  const esRef = useRef<EventSource | null>(null);

  const { data: assessment, refetch } = useQuery({
    queryKey: ['assessment', id],
    queryFn: () => api.get<AssessmentDetail>(`/api/assessments/${id}`),
  });

  const rubricId = assessment?.contentId || 'mccr-w11-12-arg';
  const { data: rubricItem } = useQuery({
    queryKey: ['rubric', rubricId],
    queryFn: () => api.get<ContentItem<Rubric>>(`/api/content/rubrics/${rubricId}`),
    enabled: assessment?.mode === 'essay_trace',
  });

  useEffect(() => () => esRef.current?.close(), []);

  async function grade() {
    setError('');
    try {
      const { jobId, total } = await api.post<{ jobId: string; total: number }>(
        `/api/assessments/${id}/grade`,
      );
      setProgress({ done: 0, total, label: 'starting…' });
      const es = new EventSource(`/api/jobs/${jobId}/events`);
      esRef.current = es;
      es.onmessage = (ev) => {
        const data = JSON.parse(ev.data) as { type: string; done?: number; total?: number; label?: string; error?: string };
        if (data.type === 'progress') {
          setProgress({ done: data.done ?? 0, total: data.total ?? total, label: data.label ?? '' });
          void refetch(); // records persist as they complete — progressive UI
        } else if (data.type === 'done') {
          es.close();
          setProgress(null);
          void refetch();
          void qc.invalidateQueries({ queryKey: ['assessments'] });
        } else if (data.type === 'error') {
          es.close();
          setProgress(null);
          setError(data.error ?? 'Grading failed.');
          void refetch();
        }
      };
      es.onerror = () => {
        // SSE dropped — fall back to polling the job row.
        es.close();
        const poll = setInterval(() => {
          void api.get<{ status: string; done: number; total: number; label: string; error: string }>(`/api/jobs/${jobId}`).then((job) => {
            if (job.status === 'running') {
              setProgress({ done: job.done, total: job.total, label: job.label });
            } else {
              clearInterval(poll);
              setProgress(null);
              if (job.status === 'error') setError(job.error);
              void refetch();
            }
          });
        }, 1000);
      };
    } catch (e) {
      setProgress(null);
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  if (!assessment) {
    return <div className="card p-8 text-center text-sm" style={{ color: 'var(--ink-muted)' }}>Loading…</div>;
  }

  return (
    <div>
      <header className="mb-5 flex flex-wrap items-end justify-between gap-2 border-b pb-4" style={{ borderColor: 'var(--gridline)' }}>
        <div>
          <div className="kicker">{assessment.name}</div>
          <h1 className="font-display mt-0.5 text-[1.7rem] leading-tight" style={{ fontWeight: 560 }}>
            {tab === 'dashboard' ? 'Scores & divergence'
              : tab === 'layerb' ? 'How the student worked with AI'
              : 'Fact-check against cited sources'}
          </h1>
        </div>
        <div className="flex items-center gap-2">
          <span className="font-data rounded-sm border px-2 py-1 text-[10px] uppercase tracking-wide" style={{ borderColor: 'var(--gridline)', color: 'var(--ink-muted)' }}>
            {assessment.gradedLive ? 'graded live' : assessment.isExemplar ? 'bundled demo scores' : 'ungraded'} · rubric v{assessment.contentVersion || rubricItem?.version}
          </span>
          <button
            className="rounded-sm px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
            style={{ background: 'var(--accent)' }}
            disabled={progress !== null}
            onClick={() => void grade()}
            title="Run the full grading pipeline with the configured LLM"
          >
            {(assessment.scores?.length ?? 0) > 0 ? 'Re-grade' : 'Grade'} live
          </button>
        </div>
      </header>

      <nav className="mb-4 flex gap-1 text-xs" aria-label="Session views">
        {(['dashboard', 'layerb', 'factcheck'] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className="rounded-sm border px-2.5 py-1.5"
            style={tab === t ? { borderColor: 'var(--accent)', color: 'var(--accent)', fontWeight: 600 } : { borderColor: 'var(--gridline)', color: 'var(--ink-secondary)' }}
            aria-current={tab === t ? 'page' : undefined}
          >
            {t === 'dashboard' ? 'Scores & Divergence' : t === 'layerb' ? 'AI Reliance' : 'Fact-Check'}
          </button>
        ))}
      </nav>

      {error && (
        <div role="alert" className="card mb-4 border-l-2 p-3 text-sm" style={{ borderLeftColor: 'var(--status-critical)' }}>
          <b>Error:</b> {error}
        </div>
      )}
      {progress && (
        <div className="card mb-4 p-3 text-sm" aria-live="polite">
          <div className="mb-1.5 flex justify-between">
            <span>Grading — {progress.label}</span>
            <span className="font-data text-xs">{progress.done}/{progress.total}</span>
          </div>
          <div className="h-1 w-full" style={{ background: 'var(--div-mid)' }} role="progressbar"
            aria-valuenow={progress.done} aria-valuemin={0} aria-valuemax={progress.total} aria-label="Grading progress">
            <div className="h-1 transition-all" style={{ width: `${(progress.done / Math.max(1, progress.total)) * 100}%`, background: 'var(--accent)' }} />
          </div>
        </div>
      )}

      {tab === 'dashboard' && rubricItem && (
        <Dashboard assessment={assessment} rubric={rubricItem.payload} onChanged={() => void refetch()} />
      )}
      {tab === 'layerb' && <LayerBPanel layerB={assessment.layerB} />}
      {tab === 'factcheck' && <FactCheckPanel assessmentId={id} active={tab === 'factcheck'} />}
    </div>
  );
}
