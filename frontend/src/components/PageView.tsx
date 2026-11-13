import { useEffect, useMemo, useRef, useState } from 'react';
import { pageImageUrl } from '../api';
import { humanize } from '../lib/guess';
import type { OcrArticle, PageInfo } from '../types';

type Box = [number, number, number, number];
type Mode = 'detect' | 'clean' | 'original';
type Verdict = 'removed' | 'review' | 'kept';

const CLASS_COLORS: Record<string, string> = {
  title: '#7c3aed', 'plain text': '#2563eb', abandon: '#78716c', figure: '#059669', figure_caption: '#14b8a6',
  table: '#0891b2', table_caption: '#06b6d4', table_footnote: '#0e7490', isolate_formula: '#db2777', formula_caption: '#f472b6',
};
const RED = '#e5484d';
const OCR_COLOR: Record<Verdict, string> = { removed: RED, review: '#f5a524', kept: '#30a46c' };
const OVERLAP = 0.5;          // share of a layout block that must sit inside an ad box for the block to count as part of the ad

const area = (b: number[]) => Math.max(0, b[2] - b[0]) * Math.max(0, b[3] - b[1]);
const inter = (a: number[], b: number[]) => area([Math.max(a[0], b[0]), Math.max(a[1], b[1]), Math.min(a[2], b[2]), Math.min(a[3], b[3])]);
const union = (a: Box, b: number[]): Box => [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[2], b[2]), Math.max(a[3], b[3])];
const verdictOf = (a: OcrArticle): Verdict => (a.ad_label === 'ad' || !a.kept ? 'removed' : a.ad_label === 'uncertain' ? 'review' : 'kept');

interface Region { key: string; adId: number | null; box: Box; source: 'model' | 'manual'; conf: number | null; blocks: number[] }
interface ABlock { id: number; cls: string; conf: number; box: Box; modelAd: boolean; adOf: number | null; isAd: boolean }

function Thumb({ img, box, page, tick }: { img: HTMLImageElement | null; box: Box; page: PageInfo; tick: number }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const c = ref.current;
    if (!c || !img || !img.naturalWidth) return;
    const S = 96, k = img.naturalWidth / page.width;
    const [x0, y0, x1, y1] = box, w = x1 - x0, h = y1 - y0, f = Math.min(S / (w * k), S / (h * k));
    c.width = S; c.height = S;
    c.getContext('2d')?.drawImage(img, x0 * k, y0 * k, w * k, h * k, (S - w * k * f) / 2, (S - h * k * f) / 2, w * k * f, h * k * f);
  }, [img, box, page, tick]);
  return <canvas ref={ref} width={96} height={96} style={{ width: 48, height: 48, border: '1px solid var(--border)', borderRadius: 4 }} aria-hidden="true" />;
}

/** The newspaper page with the ads that are being ignored. Red = ad regions (model) and the layout blocks inside them; you can
 *  adjust the thresholds, click a block to add or remove it from an ad, dismiss a whole ad, and preview the page without ads.
 *  Changes here are a preview - the pipeline used the confidence shown in the note (use "Mark as ad" in the OCR tab to act on one item). */
export default function PageView({ runId, pages, items }: { runId: string; pages: PageInfo[]; items: OcrArticle[] }) {
  const papers = useMemo(() => [...new Set(pages.map((p) => p.paper_id))], [pages]);
  const nameOf = (id: string) => items.find((a) => a.paper_id === id)?.paper ?? id;
  const [paper, setPaper] = useState(papers[0] ?? '');
  const paperPages = useMemo(() => pages.filter((p) => p.paper_id === paper), [pages, paper]);
  const [pageNo, setPageNo] = useState<number>(paperPages[0]?.page_no ?? 1);
  const page = paperPages.find((p) => p.page_no === pageNo) ?? paperPages[0];
  const pageKey = page ? `${page.paper_id}:${page.page_no}` : '';

  const [mode, setMode] = useState<Mode>('detect');
  const [adConf, setAdConf] = useState<number | null>(null);          // null = what the pipeline used
  const [layConf, setLayConf] = useState(0.25);
  const [showBlocks, setShowBlocks] = useState(true);
  const [showOcr, setShowOcr] = useState(false);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [blockOv, setBlockOv] = useState<Record<string, Record<number, 'ad' | 'none'>>>({});
  const [dismissed, setDismissed] = useState<Record<string, number[]>>({});
  const [focus, setFocus] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const [tick, setTick] = useState(0);

  const effConf = adConf ?? page?.ad_conf ?? 0.25;
  const ov = blockOv[pageKey] ?? {};
  const dis = dismissed[pageKey] ?? [];

  const { blocks, regions } = useMemo(() => {
    if (!page) return { blocks: [] as ABlock[], regions: [] as Region[] };
    const adBoxes = page.regions.filter((r) => r.label === 'ad' && r.conf >= effConf && !dis.includes(r.id));
    const bl: ABlock[] = (page.blocks ?? []).filter((b) => b.conf >= layConf || ov[b.id]).map((b) => {
      let best: number | null = null, bestFrac = 0;
      for (const a of adBoxes) {
        const f = inter(b.bbox, a.bbox) / (area(b.bbox) || 1);
        if (f > bestFrac) { bestFrac = f; best = a.id; }
      }
      const modelAd = bestFrac >= OVERLAP;
      const o = ov[b.id];
      return { id: b.id, cls: b.cls, conf: b.conf, box: b.bbox, modelAd, adOf: modelAd ? best : null, isAd: o === 'ad' ? true : o === 'none' ? false : modelAd };
    });
    const rs: Region[] = adBoxes.map((a) => ({ key: `m${a.id}`, adId: a.id, box: [...a.bbox] as Box, source: 'model', conf: a.conf, blocks: [] }));
    const byAd = new Map(rs.map((r) => [r.adId, r]));
    for (const b of bl) {
      if (!b.isAd) continue;
      const r = b.adOf !== null ? byAd.get(b.adOf) : undefined;
      if (r) { r.box = union(r.box, b.box); r.blocks.push(b.id); }
      else rs.push({ key: `b${b.id}`, adId: null, box: [...b.box] as Box, source: 'manual', conf: null, blocks: [b.id] });
    }
    rs.sort((a, b) => a.box[1] - b.box[1] || a.box[0] - b.box[0]);
    return { blocks: bl, regions: rs };
  }, [page, effConf, layConf, ov, dis]);

  const here = useMemo(() => items.filter((a) => a.paper_id === paper && a.page_no === page?.page_no), [items, paper, page]);
  const insideAds = useMemo(() => {
    const m: Record<string, number> = {};
    for (const r of regions) {
      m[r.key] = here.filter((a) => a.bboxes.some((b) => {
        const cx = (b[0] + b[2]) / 2, cy = (b[1] + b[3]) / 2;
        return cx >= r.box[0] && cx <= r.box[2] && cy >= r.box[1] && cy <= r.box[3];
      })).length;
    }
    return m;
  }, [regions, here]);

  if (!page) return <p className="empty">No page layout was saved for this run.</p>;
  const idx = paperPages.indexOf(page);
  const adArea = Math.min(100, (100 * regions.reduce((s, r) => s + area(r.box), 0)) / (page.width * page.height));
  const counts: Record<string, number> = {};
  blocks.forEach((b) => { counts[b.cls] = (counts[b.cls] ?? 0) + 1; });
  const go = (p: number) => { setPageNo(p); setFocus(null); setPicked(null); };
  const sel = here.find((a) => a.article_id === picked) ?? null;
  const fs = page.width / 55;

  const toggleBlock = (b: ABlock) => setBlockOv((m) => {
    const cur = { ...(m[pageKey] ?? {}) };
    if (b.isAd === b.modelAd) cur[b.id] = b.isAd ? 'none' : 'ad'; else delete cur[b.id];
    return { ...m, [pageKey]: cur };
  });
  const dismiss = (r: Region) => {
    if (r.adId !== null) setDismissed((m) => ({ ...m, [pageKey]: [...(m[pageKey] ?? []), r.adId as number] }));
    setBlockOv((m) => {
      const cur = { ...(m[pageKey] ?? {}) };
      r.blocks.forEach((id) => { if (cur[id] === 'ad') delete cur[id]; });
      return { ...m, [pageKey]: cur };
    });
    if (focus === r.key) setFocus(null);
  };
  const rect = (b: number[]) => ({ x: b[0], y: b[1], width: b[2] - b[0], height: b[3] - b[1] });

  return (
    <div className="stack">
      <div className="filters">
        <label>Paper{' '}
          <select value={paper} onChange={(e) => { setPaper(e.target.value); go(pages.find((p) => p.paper_id === e.target.value)?.page_no ?? 1); }}>
            {papers.map((p) => <option key={p} value={p}>{nameOf(p)}</option>)}
          </select>
        </label>{' '}
        <button type="button" disabled={idx <= 0} onClick={() => go(paperPages[idx - 1].page_no)}>Previous</button>{' '}
        <label>Page{' '}
          <select value={page.page_no} onChange={(e) => go(Number(e.target.value))}>
            {paperPages.map((p) => <option key={p.page_no} value={p.page_no}>{p.page_no}</option>)}
          </select>
        </label>{' '}
        <button type="button" disabled={idx >= paperPages.length - 1} onClick={() => go(paperPages[idx + 1].page_no)}>Next</button>{' '}
        <span role="group" aria-label="View mode">
          {(['detect', 'clean', 'original'] as Mode[]).map((m) => (
            <button key={m} type="button" className={mode === m ? 'chip active' : 'chip'} aria-pressed={mode === m} onClick={() => setMode(m)}>
              {m === 'detect' ? 'Detect' : m === 'clean' ? 'Without ads' : 'Original'}
            </button>
          ))}
        </span>
      </div>
      <p className="muted small">
        {mode === 'detect' ? 'Red = ads that are ignored. Click a layout block to add it to or remove it from an ad; click a red region to dismiss the whole ad.'
          : mode === 'clean' ? 'Preview of the page with the ads blanked out - what is left is what is kept.' : 'The page as uploaded.'}
        {!page.yolo && ' The ad detector was not active for this run, so only the text rules were used.'}
      </p>

      <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0, 3fr) minmax(240px, 1fr)', gap: '1rem', alignItems: 'start' }}>
        <div style={{ position: 'relative', lineHeight: 0 }}>
          <img ref={imgRef} src={pageImageUrl(runId, page.paper_id, page.page_no)} alt={`${nameOf(paper)} page ${page.page_no}`}
               onLoad={() => setTick((t) => t + 1)} style={{ width: '100%', height: 'auto', display: 'block' }} />
          <svg viewBox={`0 0 ${page.width} ${page.height}`} preserveAspectRatio="none" aria-label="Page overlay"
               style={{ position: 'absolute', inset: 0, width: '100%', height: '100%' }}>
            {mode === 'clean' && regions.map((r) => <rect key={r.key} {...rect([r.box[0] - 2, r.box[1] - 2, r.box[2] + 2, r.box[3] + 2])} fill="#ffffff" />)}
            {mode === 'detect' && (
              <>
                {regions.map((r) => (
                  <rect key={`h${r.key}`} {...rect(r.box)} fill="rgba(229,72,77,.10)" stroke="none" style={{ cursor: 'pointer' }} onClick={() => dismiss(r)}>
                    <title>Click to dismiss this whole ad</title>
                  </rect>
                ))}
                {showBlocks && [...blocks].filter((b) => b.isAd || !hidden.has(b.cls)).sort((x, y) => area(y.box) - area(x.box)).map((b) => {
                  const c = CLASS_COLORS[b.cls] ?? '#888';
                  return (
                    <rect key={`b${b.id}`} {...rect(b.box)} fill={b.isAd ? 'rgba(229,72,77,.30)' : `${c}14`} stroke={b.isAd ? RED : c}
                          strokeWidth={b.isAd ? 1.4 : 0.7} style={{ cursor: 'pointer' }} onClick={() => toggleBlock(b)}>
                      <title>{`${b.cls} - ${Math.round(b.conf * 100)}%${b.isAd ? ' - part of an ad' : ''} - click to ${b.isAd ? 'remove from ads' : 'mark as ad'}`}</title>
                    </rect>
                  );
                })}
                {showOcr && here.map((a) => a.bboxes.map((b, i) => (
                  <rect key={`o${a.article_id}-${i}`} {...rect(b)} fill={picked === a.article_id ? `${OCR_COLOR[verdictOf(a)]}66` : 'none'} stroke={OCR_COLOR[verdictOf(a)]}
                        strokeWidth={picked === a.article_id ? 2.2 : 1} strokeDasharray="3 2" style={{ cursor: 'pointer' }} onClick={() => setPicked(a.article_id)}>
                    <title>{`${a.headline || '(no headline)'} - ${verdictOf(a)}`}</title>
                  </rect>
                )))}
                {regions.map((r, i) => {
                  const label = `AD ${i + 1} - ${r.source === 'manual' ? 'manual' : `${Math.round((r.conf ?? 0) * 100)}%`}`;
                  const y = Math.max(0, r.box[1] - fs * 1.5);
                  return (
                    <g key={`l${r.key}`} pointerEvents="none">
                      <rect {...rect(r.box)} fill="none" stroke={RED} strokeWidth={focus === r.key ? 3 : 1.8} strokeDasharray={r.source === 'manual' ? '4 3' : undefined} />
                      <rect x={r.box[0]} y={y} width={label.length * fs * 0.6 + fs} height={fs * 1.5} fill={RED} />
                      <text x={r.box[0] + fs * 0.5} y={y + fs * 1.1} fill="#fff" fontSize={fs}>{label}</text>
                    </g>
                  );
                })}
              </>
            )}
          </svg>
        </div>

        <aside className="stack" aria-label="Ad controls">
          <section className="card" aria-label="Page summary">
            <div><strong>{regions.length}</strong> ad{regions.length === 1 ? '' : 's'} ignored on this page</div>
            <div><strong>{adArea.toFixed(1)}%</strong> of the page is ads</div>
            <div className="muted small">{blocks.length} layout blocks - {here.filter((a) => verdictOf(a) === 'removed').length} OCR items dropped</div>
          </section>
          <label className="small">Ad confidence: <strong>{effConf.toFixed(2)}</strong>
            <input type="range" min={0.05} max={0.95} step={0.05} value={effConf} aria-label="Ad confidence" onChange={(e) => setAdConf(Number(e.target.value))} style={{ width: '100%' }} />
          </label>
          <label className="small">Layout block confidence: <strong>{layConf.toFixed(2)}</strong>
            <input type="range" min={0.1} max={0.95} step={0.05} value={layConf} aria-label="Layout confidence" onChange={(e) => setLayConf(Number(e.target.value))} style={{ width: '100%' }} />
          </label>
          <p className="muted small">The pipeline ignored ads at {(page.ad_conf ?? 0.25).toFixed(2)} or higher; moving the sliders is a preview only.</p>
          <label className="small"><input type="checkbox" checked={showBlocks} onChange={(e) => setShowBlocks(e.target.checked)} /> Show layout blocks</label>
          <label className="small"><input type="checkbox" checked={showOcr} onChange={(e) => setShowOcr(e.target.checked)} /> Show OCR articles (green kept, red dropped)</label>
          {showBlocks && Object.keys(counts).length > 0 && (
            <div role="group" aria-label="Block types" className="small">
              {Object.keys(CLASS_COLORS).filter((c) => counts[c]).map((c) => (
                <button key={c} type="button" className={hidden.has(c) ? 'chip' : 'chip active'} aria-pressed={!hidden.has(c)} title={hidden.has(c) ? `Show ${c}` : `Hide ${c}`}
                        onClick={() => setHidden((h) => { const n = new Set(h); if (n.has(c)) n.delete(c); else n.add(c); return n; })}>
                  <span style={{ color: CLASS_COLORS[c] }}>■</span> {c.replace('_', ' ')} {counts[c]}
                </button>
              ))}
            </div>
          )}
        </aside>
      </div>

      <section aria-label="Ignored parts">
        <h3>Ignored parts of this page {regions.length > 0 && <span className="muted small">({regions.length} found)</span>}</h3>
        {regions.length === 0 ? <p className="muted small">No ads found on this page at this confidence.</p> : (
          <ul className="stack" style={{ listStyle: 'none', padding: 0 }}>
            {regions.map((r, i) => (
              <li key={r.key} className="card" style={{ display: 'flex', gap: '.75rem', alignItems: 'center', cursor: 'pointer', outline: focus === r.key ? `2px solid ${RED}` : undefined }}
                  onClick={() => { setFocus(focus === r.key ? null : r.key); setMode('detect'); }}>
                <Thumb img={imgRef.current} box={r.box} page={page} tick={tick} />
                <div style={{ flex: 1 }}>
                  <strong>Ad {i + 1}</strong>{' '}
                  <span className="muted small">
                    {r.source === 'manual' ? 'added by you' : `${Math.round((r.conf ?? 0) * 100)}% confidence`} - {r.blocks.length} block{r.blocks.length === 1 ? '' : 's'} -{' '}
                    {Math.round(r.box[2] - r.box[0])}x{Math.round(r.box[3] - r.box[1])} pt - {insideAds[r.key] ?? 0} OCR item{insideAds[r.key] === 1 ? '' : 's'} inside
                  </span>
                </div>
                <button type="button" aria-label={`Remove ad ${i + 1}`} title="Not an ad" onClick={(e) => { e.stopPropagation(); dismiss(r); }}>x</button>
              </li>
            ))}
          </ul>
        )}
      </section>

      {sel && (
        <section className="card" aria-label="Selected block">
          <strong>{sel.headline || <em className="muted">(no headline)</em>}</strong>{' '}
          <span className={`chip${verdictOf(sel) === 'kept' ? '' : ' warn'}`}>
            {verdictOf(sel) === 'removed' ? 'Removed' : verdictOf(sel) === 'review' ? 'Kept - needs review' : 'Kept'}
          </span>
          <p className="muted small">
            {sel.ad_reason && sel.ad_reason !== 'ok' ? `Why: ${sel.ad_reason.split(',').map(humanize).join(', ')}. ` : ''}
            {sel.removed_reason ? `Cleaning: ${humanize(sel.removed_reason)}. ` : ''}
            OCR {sel.ocr_confidence == null ? '-' : `${Math.round(sel.ocr_confidence * 100)}%`}
          </p>
          <p className="small excerpt">{sel.text.slice(0, 400) || <em className="muted">(no text read)</em>}</p>
        </section>
      )}
    </div>
  );
}
