import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import type { CitationEntry, FactCheckResult, FactCheckStatement, GradingProgress } from '../types';

const STATUS_COLOR: Record<string, string> = {
  indexed: 'var(--status-good)',
  fetched: 'var(--status-warning)',
  pending: 'var(--ink-muted)',
  unverifiable: 'var(--status-serious)',
  error: 'var(--status-critical)',
};

const VERDICT_COLOR: Record<string, string> = {
  supported: 'var(--status-good)',
  contradicted: 'var(--status-critical)',
  not_addressed: 'var(--status-warning)',
  unverifiable_source: 'var(--ink-muted)',
  unchecked: 'var(--ink-muted)',
};

const VERDICT_LABEL: Record<string, string> = {
  supported: 'supported',
  contradicted: 'contradicted',
  not_addressed: 'not addressed',
  unverifiable_source: 'source unverifiable',
  unchecked: 'unchecked',
};

/** Citation resolution + fact-check view: runs its own job/SSE lifecycle
 * (unlike LayerBPanel, which is purely presentational) because fact-checking
 * is a separately triggerable job from grading. Mirrors SessionDetail's
 * grade()/EventSource pattern. */
export function FactCheckPanel({ assessmentId, active }: { assessmentId: string; active: boolean }) {
  const qc = useQueryClient();
  const [progress, setProgress] = useState<GradingProgress | null>(null);
  const [error, setError] = useState('');
  const esRef = useRef<EventSource | null>(null);

  const { data, refetch } = useQuery({
    queryKey: ['fact-check', assessmentId],
    queryFn: () => api.get<FactCheckResult>(`/api/assessments/${assessmentId}/fact-check`),
    enabled: active,
  });

  useEffect(() => () => esRef.current?.close(), []);

  async function runFactCheck(force: boolean) {
    setError('');
    try {
      const { jobId, total } = await api.post<{ jobId: string; total: number }>(
        `/api/assessments/${assessmentId}/fact-check${force ? '?force=true' : ''}`,
      );
      setProgress({ done: 0, total, label: 'starting…' });
      const es = new EventSource(`/api/jobs/${jobId}/events`);
      esRef.current = es;
      es.onmessage = (ev) => {
        const evData = JSON.parse(ev.data) as { type: string; done?: number; total?: number; label?: string; error?: string };
        if (evData.type === 'progress') {
          setProgress({ done: evData.done ?? 0, total: evData.total ?? total, label: evData.label ?? '' });
          void refetch();
        } else if (evData.type === 'done') {
          es.close();
          setProgress(null);
          void refetch();
          void qc.invalidateQueries({ queryKey: ['fact-check', assessmentId] });
        } else if (evData.type === 'error') {
          es.close();
          setProgress(null);
          setError(evData.error ?? 'Fact-check failed.');
          void refetch();
        }
      };
      es.onerror = () => {
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

  const citations = data?.citations ?? [];
  const statements = data?.statements ?? [];

  return (
    <div className="space-y-4">
      <div className="card p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="panel-title">Fact-check against cited sources</h2>
            <p className="mt-1 text-xs" style={{ color: 'var(--ink-secondary)' }}>
              Extracts the works-cited list, downloads and indexes each source, then checks
              in-text-cited statements against the actual source text.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <button
              className="rounded-sm px-3 py-1.5 text-xs font-medium text-white disabled:opacity-40"
              style={{ background: 'var(--accent)' }}
              disabled={progress !== null}
              onClick={() => void runFactCheck(false)}
            >
              {citations.length > 0 ? 'Re-check' : 'Run fact-check'}
            </button>
            {citations.length > 0 && (
              <button
                className="rounded-sm border px-3 py-1.5 text-xs font-medium disabled:opacity-40"
                style={{ borderColor: 'var(--gridline)', color: 'var(--ink-secondary)' }}
                disabled={progress !== null}
                onClick={() => void runFactCheck(true)}
                title="Reset any unverifiable/errored sources and retry fetching them"
              >
                Force re-fetch
              </button>
            )}
          </div>
        </div>
        {progress && (
          <div className="mt-3 text-xs" style={{ color: 'var(--ink-muted)' }}>
            {progress.label} ({progress.done}/{progress.total})
          </div>
        )}
        {error && <div role="alert" className="mt-3 text-xs" style={{ color: 'var(--status-critical)' }}>{error}</div>}
      </div>

      {citations.length === 0 && !progress ? (
        <div className="card p-8 text-center text-sm" style={{ color: 'var(--ink-muted)' }}>
          No fact-check has been run for this assessment yet.
        </div>
      ) : (
        <>
          <div className="card p-4">
            <h3 className="panel-title">Works-cited sources</h3>
            <div className="mt-2 space-y-2">
              {citations.map((c) => <CitationRow key={c.ordinal} citation={c} />)}
            </div>
          </div>

          <div className="card p-4">
            <h3 className="panel-title">Checked statements</h3>
            <div className="mt-2 space-y-2">
              {statements.length === 0 ? (
                <div className="text-xs" style={{ color: 'var(--ink-muted)' }}>
                  No in-text-cited statements were found in this essay.
                </div>
              ) : (
                statements.map((s) => <StatementRow key={s.statementIndex} statement={s} />)
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function CitationRow({ citation }: { citation: CitationEntry }) {
  return (
    <div className="rounded border p-2 text-xs" style={{ borderColor: 'var(--gridline)', background: 'var(--surface-1)' }}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="flex-1">{citation.rawCitation}</span>
        <span className="flex items-center gap-2 whitespace-nowrap">
          {citation.resolutionMethod && (
            <span style={{ color: 'var(--ink-muted)' }}>{citation.resolutionMethod.replace('_', ' ')}</span>
          )}
          <span className="flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-full" style={{ background: STATUS_COLOR[citation.status] ?? 'var(--ink-muted)' }} />
            {citation.status}
          </span>
          {citation.chunkCount > 0 && <span style={{ color: 'var(--ink-muted)' }}>{citation.chunkCount} chunks</span>}
        </span>
      </div>
      {citation.resolvedUrl && (
        <div className="mt-1">
          <a href={citation.resolvedUrl} target="_blank" rel="noreferrer" className="underline" style={{ color: 'var(--accent)' }}>
            {citation.resolvedUrl}
          </a>
        </div>
      )}
      {citation.error && (
        <div className="mt-1" style={{ color: 'var(--status-serious-text)' }}>{citation.error}</div>
      )}
    </div>
  );
}

function StatementRow({ statement }: { statement: FactCheckStatement }) {
  const [expanded, setExpanded] = useState(false);
  const long = statement.statementText.length > 160;
  const shown = expanded || !long ? statement.statementText : `${statement.statementText.slice(0, 160)}…`;

  return (
    <div className="rounded border p-2 text-xs" style={{ borderColor: 'var(--gridline)', background: 'var(--surface-1)' }}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex-1">
          <span className="italic">“{shown}”</span>{' '}
          {long && (
            <button className="underline" style={{ color: 'var(--ink-muted)' }} onClick={() => setExpanded((v) => !v)}>
              {expanded ? 'show less' : 'show more'}
            </button>
          )}
          <div className="mt-0.5" style={{ color: 'var(--ink-muted)' }}>{statement.citationMarker}</div>
        </div>
        <span className="flex items-center gap-1 whitespace-nowrap">
          <span className="inline-block h-2 w-2 rounded-full" style={{ background: VERDICT_COLOR[statement.verdict] ?? 'var(--ink-muted)' }} />
          {VERDICT_LABEL[statement.verdict] ?? statement.verdict}
        </span>
      </div>
      {statement.evidenceQuote && (
        <div className="mt-1 rounded p-2" style={{ background: 'var(--page)' }}>
          “{statement.evidenceQuote}”
        </div>
      )}
      {statement.reasoning && (
        <div className="mt-1" style={{ color: 'var(--ink-secondary)' }}>{statement.reasoning}</div>
      )}
    </div>
  );
}
