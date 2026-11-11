'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Card,
  CardContent,
  Chip,
  Container,
  Dialog,
  DialogContent,
  Divider,
  Link,
  MenuItem,
  Paper,
  Stack,
  Switch,
  TextField,
  Typography,
} from '@mui/material';
import type { Job, Status } from '@/lib/db';
import { AREA_LABEL, type Area } from '@/lib/areas';
import { employerColor, type EmployerColor } from './employerColor';
import RouteMap from './RouteMap';

interface ApiPayload {
  active: Job[];
  archive: Job[];
  /** Sites the scanner has seen but does not scrape yet; empty when nothing new turned up. */
  meta: {
    lastIngest: string | null;
    scannerVersion: string | null;
    mapOrigin: string | null;
    /** Rows that fell outside the 50 minute boundary and are hidden. */
    retired: number;
    /** Whether the employer hunt keeps running, and what it has added. */
    db: boolean;
  };
  error?: string;
}

const REFRESH_MS = 5 * 60 * 1000;

/** Mirrors MAX_REASON in lib/db.ts — that module pulls in `pg`, so it can't be imported here. */
const REASON_MAX = 400;

/** Deterministic UTC formatting so SSR and client produce identical markup. */
function fmtUTC(iso: string | null): string {
  if (!iso) return 'never';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return 'unknown';
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getUTCMonth() + 1)}/${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())} UTC`;
}

function timeAgo(iso: string | null): string {
  if (!iso) return 'never';
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return 'unknown';
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (s < 60) return 'just now';
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} hr ago`;
  const d = Math.floor(h / 24);
  return d === 1 ? 'yesterday' : `${d} days ago`;
}

function isStale(job: Job): boolean {
  const seen = new Date(job.lastSeen).getTime();
  return Date.now() - seen > 7 * 24 * 60 * 60 * 1000;
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** Posted dates are plain YYYY-MM-DD; format from UTC parts so SSR and client agree. */
function fmtPosted(iso: string | null): string {
  if (!iso) return '';
  const d = new Date(`${iso}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return '';
  const label = `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`;
  return d.getUTCFullYear() === new Date().getUTCFullYear() ? label : `${label}, ${d.getUTCFullYear()}`;
}

/** Known posting dates rank above unknown ones, which fall back to found-order. */
function postedRank(job: Job): number {
  if (!job.posted) return -Infinity;
  const t = new Date(`${job.posted}T00:00:00Z`).getTime();
  return Number.isNaN(t) ? -Infinity : t;
}

/** "Philadelphia, PA · Posted Nov 3 · Found 1 hr ago" */
function metaLine(job: Job, rel: (iso: string | null) => string, marked = false): string {
  const posted = job.posted ? fmtPosted(job.posted) : '';
  return [
    job.location,
    posted && `Posted ${posted}`,
    `Found ${rel(job.firstSeen)}`,
    marked && job.statusChangedAt && `Marked ${rel(job.statusChangedAt)}`,
  ]
    .filter(Boolean)
    .join(' · ');
}

/** "42 min" / "1 hr 15 min" */
function fmtDrive(min: number | null): string {
  if (min === null || !Number.isFinite(min)) return '';
  const m = Math.round(min);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  const rest = m % 60;
  return rest ? `${h} hr ${rest} min` : `${h} hr`;
}

/** Green / amber / red by commute length — the "can I actually do this daily" question. */
function driveColor(min: number | null): 'success' | 'warning' | 'error' | 'default' {
  if (min === null || !Number.isFinite(min)) return 'default';
  if (min < 30) return 'success';
  if (min <= 60) return 'warning';
  return 'error';
}

/**
 * The route card for one job: the drive from the pinned origin, drawn in the browser from the
 * polyline the Pi resolved at ingest. No map API is called when the board is viewed.
 */
function RouteCard({ job }: { job: Job }) {
  const [open, setOpen] = useState(false);
  if (!job.hasMap || !job.mapGeometry) return null;

  const drive = fmtDrive(job.driveMin);
  const miles = job.driveMi === null ? '' : `${Math.round(job.driveMi)} mi`;
  // The cluster name, not the pin: "from York", never "from Country Club of York".
  const from = job.mapOrigin ?? (job.area ? AREA_LABEL[job.area] : null);
  const fromLabel = from ? `from ${from}` : 'from the nearest hub';
  const destination = job.location ?? job.title;
  const googleMaps = `https://www.google.com/maps/dir/?api=1&origin=${encodeURIComponent(
    from ? `${from}, PA` : '',
  )}&destination=${encodeURIComponent(destination)}&travelmode=driving`;

  return (
    <Stack spacing={1} sx={{ mt: 1.5 }}>
      <RouteMap
        geometry={job.mapGeometry}
        originLabel={from}
        destination={destination}
        height={140}
      />
      <Stack
        direction="row"
        spacing={1}
        useFlexGap
        sx={{ flexWrap: 'wrap', alignItems: 'center' }}
      >
        <Chip
          size="small"
          color={driveColor(job.driveMin)}
          variant="outlined"
          label={[drive, miles].filter(Boolean).join(' · ')}
        />
        <Typography variant="caption" color="text.secondary">
          {fromLabel}
        </Typography>
        <Box sx={{ flex: 1 }} />
        <Link
          href={googleMaps}
          target="_blank"
          rel="noopener noreferrer"
          variant="caption"
          underline="hover"
        >
          Directions ↗
        </Link>
        <Button size="small" onClick={() => setOpen(true)} sx={{ minWidth: 0 }}>
          Open map
        </Button>
      </Stack>

      <Dialog open={open} onClose={() => setOpen(false)} maxWidth="md" fullWidth>
        <DialogContent>
          <Typography variant="subtitle1" component="h2" gutterBottom>
            {job.title}
          </Typography>
          <Typography variant="body2" color="text.secondary" gutterBottom>
            {[drive, miles, fromLabel].filter(Boolean).join(' · ')}
          </Typography>
          <RouteMap
            geometry={job.mapGeometry}
            originLabel={from}
            destination={destination}
            height={420}
            interactive
          />
          <Stack direction="row" spacing={1} sx={{ mt: 1.5, alignItems: 'center' }}>
            <Link
              href={job.url}
              target="_blank"
              rel="noopener noreferrer"
              variant="caption"
              underline="hover"
            >
              View posting ↗
            </Link>
            <Box sx={{ flex: 1 }} />
            <Button size="small" href={googleMaps} target="_blank" rel="noopener noreferrer">
              Directions
            </Button>
            <Button size="small" onClick={() => setOpen(false)}>
              Close
            </Button>
          </Stack>
        </DialogContent>
      </Dialog>
    </Stack>
  );
}

/** Colored pill so each employer is recognizable at a glance. */
function EmployerChip({ employer, color }: { employer: string; color: EmployerColor }) {
  return (
    <Chip
      size="small"
      variant="outlined"
      label={employer}
      sx={{ bgcolor: color.bg, color: color.fg, borderColor: color.border }}
    />
  );
}

/**
 * Which cluster the posting belongs to. Deliberately quiet - the employer chip already carries
 * colour - but it is also the label on the map, so it sits right next to it.
 */
function AreaChip({ area }: { area: Area }) {
  return <Chip size="small" variant="outlined" label={AREA_LABEL[area]} />;
}

/** Note box: opened by "Not for me" on a live job and by "Add/Edit note" in the archive. */
function ReasonComposer({
  id,
  value,
  onChange,
  onCancel,
  onSubmit,
  busy,
  submitLabel,
  suggestion,
}: {
  id: string;
  value: string;
  onChange: (v: string) => void;
  onCancel: () => void;
  onSubmit: () => void;
  busy: boolean;
  submitLabel: string;
  /** Most recent dismissal reason, offered as a one-click fill instead of retyping it. */
  suggestion?: string | null;
}) {
  const empty = !value.trim();
  const reuse = suggestion && empty ? suggestion : null;
  return (
    <Stack spacing={1} sx={{ mt: 2 }}>
      <TextField
        id={`reason-${id}`}
        label="Why not?"
        placeholder="Optional — e.g. RN license required, not population health, commute too far"
        autoFocus
        fullWidth
        multiline
        minRows={2}
        size="small"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        slotProps={{ htmlInput: { maxLength: REASON_MAX } }}
        onKeyDown={(e) => {
          if (e.key === 'Escape') {
            e.preventDefault();
            onCancel();
          } else if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            onSubmit();
          }
        }}
      />
      <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
        <Typography variant="caption" color="text.secondary">
          {empty ? 'Blank means an earlier dismissal already covers it' : `${value.trim().length}/${REASON_MAX}`}
        </Typography>
        {reuse && (
          <Button size="small" onClick={() => onChange(reuse)} sx={{ minHeight: 0, py: 0 }}>
            Reuse “{reuse.length > 32 ? `${reuse.slice(0, 32)}…` : reuse}”
          </Button>
        )}
        <Box sx={{ flex: 1 }} />
        <Button onClick={onCancel}>Cancel</Button>
        <Button variant="contained" disabled={busy} onClick={onSubmit}>
          {submitLabel}
        </Button>
      </Stack>
    </Stack>
  );
}

export default function JobBoard({ initial }: { initial: ApiPayload }) {
  const [data, setData] = useState<ApiPayload>(initial);
  const [query, setQuery] = useState('');
  const [employer, setEmployer] = useState('all');
  const [area, setArea] = useState<'all' | Area>('all');
  const [sort, setSort] = useState<'posted' | 'found' | 'employer' | 'title'>('posted');
  const [showArchive, setShowArchive] = useState(false);
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [refreshing, setRefreshing] = useState(false);
  const [mounted, setMounted] = useState(false);
  const [dismissing, setDismissing] = useState<string | null>(null);
  const [reasonDraft, setReasonDraft] = useState('');
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  // Relative times must not be rendered during SSR, or the server and client
  // markup disagree (React hydration error #418).
  useEffect(() => setMounted(true), []);
  const rel = (iso: string | null) => (mounted ? timeAgo(iso) : fmtUTC(iso));

  const refresh = useCallback(async (showSpinner = false) => {
    if (showSpinner) setRefreshing(true);
    try {
      const res = await fetch('/api/jobs', { cache: 'no-store' });
      const json = (await res.json()) as ApiPayload;
      setData(json);
    } catch {
      /* keep previous data on transient failure */
    } finally {
      if (showSpinner) setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    timer.current = setInterval(() => void refresh(), REFRESH_MS);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [refresh]);

  const setStatus = useCallback(
    async (id: string, status: Status, reason?: string) => {
      const note = status === 'ignored' ? (reason ?? '').trim() : '';
      setBusy((b) => ({ ...b, [id]: true }));
      // optimistic
      setData((prev) => {
        const all = [...prev.active, ...prev.archive];
        const target = all.find((j) => j.id === id);
        if (!target) return prev;
        const moved: Job = {
          ...target,
          status,
          statusReason: note || null,
          statusChangedAt: status === 'new' ? null : new Date().toISOString(),
        };
        const rest = all.filter((j) => j.id !== id);
        const active = status === 'new' ? [moved, ...rest.filter((j) => j.status === 'new')] : rest.filter((j) => j.status === 'new');
        const archive =
          status === 'new' ? rest.filter((j) => j.status !== 'new') : [moved, ...rest.filter((j) => j.status !== 'new')];
        return { ...prev, active, archive };
      });
      try {
        await fetch(`/api/jobs/${id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(note ? { status, reason: note } : { status }),
        });
      } catch {
        /* fall through to reconcile */
      } finally {
        setBusy((b) => {
          const next = { ...b };
          delete next[id];
          return next;
        });
        void refresh();
      }
    },
    [refresh],
  );

  const openDismiss = useCallback((id: string, initialText = '') => {
    setDismissing((cur) => (cur === id ? null : id));
    setReasonDraft(initialText);
  }, []);

  const closeDismiss = useCallback(() => {
    setDismissing(null);
    setReasonDraft('');
  }, []);

  const confirmDismiss = useCallback(
    async (id: string) => {
      const note = reasonDraft;
      closeDismiss();
      await setStatus(id, 'ignored', note);
    },
    [closeDismiss, reasonDraft, setStatus],
  );

  const employers = useMemo(() => {
    const s = new Set<string>();
    for (const j of [...data.active, ...data.archive]) s.add(j.employer);
    return [...s].sort();
  }, [data]);

  // Colour slots follow first-sighting order, so adding a new employer never recolours
  // the ones already on the board.
  const colorFor = useMemo(() => {
    const firstSeen = new Map<string, number>();
    for (const j of [...data.active, ...data.archive]) {
      const t = new Date(j.firstSeen).getTime();
      const cur = firstSeen.get(j.employer);
      if (cur === undefined || t < cur) firstSeen.set(j.employer, t);
    }
    const order = [...firstSeen.entries()]
      .sort((a, b) => a[1] - b[1] || a[0].localeCompare(b[0]))
      .map(([name]) => name);
    const index = new Map(order.map((name, i) => [name, i]));
    return (employer: string) => employerColor(index.get(employer) ?? 0);
  }, [data]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    let rows = data.active.filter((j) => {
      if (employer !== 'all' && j.employer !== employer) return false;
      if (area !== 'all' && j.area !== area) return false;
      if (!q) return true;
      return (
        j.title.toLowerCase().includes(q) ||
        j.employer.toLowerCase().includes(q) ||
        (j.location ?? '').toLowerCase().includes(q)
      );
    });
    rows = [...rows].sort((a, b) => {
      if (sort === 'employer') return a.employer.localeCompare(b.employer) || a.title.localeCompare(b.title);
      if (sort === 'title') return a.title.localeCompare(b.title);
      if (sort === 'found') return new Date(b.firstSeen).getTime() - new Date(a.firstSeen).getTime();
      const pa = postedRank(a);
      const pb = postedRank(b);
      if (pa !== pb) return pb - pa;
      return new Date(b.firstSeen).getTime() - new Date(a.firstSeen).getTime();
    });
    return rows;
  }, [data.active, employer, area, query, sort]);

  const counts = {
    active: data.active.length,
    applied: data.archive.filter((j) => j.status === 'applied').length,
    ignored: data.archive.filter((j) => j.status === 'ignored').length,
  };

  // The newest dismissal reason, offered as a one-click fill: a batch of ten bad rows usually
  // shares one reason, and retyping it ten times is the wrong tax.
  const lastReason = useMemo(() => {
    const withReason = data.archive.filter((j) => j.status === 'ignored' && j.statusReason);
    const newest = withReason.sort(
      (a, b) => new Date(b.statusChangedAt ?? 0).getTime() - new Date(a.statusChangedAt ?? 0).getTime(),
    )[0];
    return newest?.statusReason ?? null;
  }, [data.archive]);

  const stamps = [
    { label: 'To review', value: counts.active },
    { label: 'Applied', value: counts.applied },
    { label: 'Not applicable', value: counts.ignored },
  ];

  return (
    <Container component="main" maxWidth="md" sx={{ py: 6 }}>
      <Stack spacing={3}>
        <Stack spacing={0.5}>
          <Typography variant="h5" component="h1" sx={{ fontWeight: 500 }}>
            Population Health Jobs
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Within a 50 minute drive of York or West Chester
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Scanned once a day — last checked {rel(data.meta.lastIngest)}
            {data.meta.scannerVersion && ` · scanner ${data.meta.scannerVersion}`}
            {data.meta.retired > 0 && ` · ${data.meta.retired} dropped as out of range`}
          </Typography>
        </Stack>

        {!data.meta.db && (
          <Alert severity="warning">
            No database connected yet. Add <code>POSTGRES_URL</code> in Vercel → Settings →
            Environment Variables, then redeploy.
          </Alert>
        )}
        {data.error && <Alert severity="error">Database error: {data.error}</Alert>}

        <Stack direction="row" spacing={2}>
          {stamps.map((s) => (
            <Paper key={s.label} variant="outlined" sx={{ flex: 1, p: 2 }}>
              <Typography variant="h6" component="p">
                {s.value}
              </Typography>
              <Typography variant="body2" color="text.secondary">
                {s.label}
              </Typography>
            </Paper>
          ))}
        </Stack>

        <Stack direction="row" spacing={2} useFlexGap sx={{ flexWrap: 'wrap' }}>
          <TextField
            size="small"
            placeholder="Search title, employer, or city…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            sx={{ flex: '1 1 220px' }}
            slotProps={{ htmlInput: { 'aria-label': 'Search jobs' } }}
          />
          <TextField
            select
            size="small"
            label="Employer"
            value={employer}
            onChange={(e) => setEmployer(e.target.value)}
            sx={{ minWidth: 180 }}
          >
            <MenuItem value="all">All employers</MenuItem>
            {employers.map((e) => (
              <MenuItem key={e} value={e}>
                <Box
                  component="span"
                  sx={{
                    width: 8,
                    height: 8,
                    borderRadius: '50%',
                    bgcolor: colorFor(e).dot,
                    mr: 1,
                    flexShrink: 0,
                  }}
                />
                {e}
              </MenuItem>
            ))}
          </TextField>
          <TextField
            select
            size="small"
            label="Area"
            value={area}
            onChange={(e) => setArea(e.target.value as 'all' | Area)}
            sx={{ minWidth: 150 }}
          >
            <MenuItem value="all">Both areas</MenuItem>
            <MenuItem value="york">{AREA_LABEL.york}</MenuItem>
            <MenuItem value="west_chester">{AREA_LABEL.west_chester}</MenuItem>
          </TextField>
          <TextField
            select
            size="small"
            label="Sort by"
            value={sort}
            onChange={(e) => setSort(e.target.value as typeof sort)}
            sx={{ minWidth: 170 }}
          >
            <MenuItem value="posted">Newest posted</MenuItem>
            <MenuItem value="found">Recently found</MenuItem>
            <MenuItem value="employer">By employer</MenuItem>
            <MenuItem value="title">By title</MenuItem>
          </TextField>
          <Button variant="outlined" onClick={() => void refresh(true)} disabled={refreshing}>
            {refreshing ? 'Refreshing…' : 'Refresh'}
          </Button>
        </Stack>

        {filtered.length === 0 ? (
          <Paper variant="outlined" sx={{ py: 6, px: 3, textAlign: 'center' }}>
            <Typography variant="subtitle1" component="p" gutterBottom>
              {data.active.length === 0 ? 'No jobs waiting for review' : 'Nothing matches that filter'}
            </Typography>
            <Typography variant="body2" color="text.secondary">
              {data.active.length === 0
                ? 'New matches land after the 8:00 AM scan. Anything you applied to or dismissed lives in the archive below.'
                : 'Try clearing the search box or switching the employer filter.'}
            </Typography>
          </Paper>
        ) : (
          <Stack spacing={2}>
            {filtered.map((job) => (
              <Card key={job.id} variant="outlined">
                <CardContent>
                  <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} sx={{ justifyContent: 'space-between' }}>
                    <Box sx={{ minWidth: 0 }}>
                      <Typography variant="subtitle1" component="h2" sx={{ fontWeight: 500 }}>
                        {job.url ? (
                          <Link href={job.url} target="_blank" rel="noopener noreferrer" underline="hover" color="inherit">
                            {job.title}
                          </Link>
                        ) : (
                          job.title
                        )}
                      </Typography>
                      <Stack direction="row" spacing={1} useFlexGap sx={{ mt: 1, flexWrap: 'wrap', alignItems: 'center' }}>
                        <EmployerChip employer={job.employer} color={colorFor(job.employer)} />
                        {job.area && <AreaChip area={job.area} />}
                        <Typography variant="body2" color="text.secondary">
                          {metaLine(job, rel)}
                        </Typography>
                        {mounted && isStale(job) && (
                          <Chip
                            size="small"
                            variant="outlined"
                            color="warning"
                            label="May be closed"
                            title="Not returned by the last few scans"
                          />
                        )}
                      </Stack>
                      <RouteCard job={job} />
                    </Box>
                    <Stack direction="row" spacing={1} sx={{ flexShrink: 0, alignItems: 'flex-start' }}>
                      <Button
                        variant="contained"
                        size="small"
                        component="a"
                        href={job.url || undefined}
                        target="_blank"
                        rel="noopener noreferrer"
                        disabled={!job.url}
                      >
                        Apply
                      </Button>
                      <Button
                        variant="outlined"
                        size="small"
                        color="success"
                        disabled={!!busy[job.id]}
                        onClick={() => void setStatus(job.id, 'applied')}
                      >
                        Applied
                      </Button>
                      <Button size="small" disabled={!!busy[job.id]} onClick={() => openDismiss(job.id)}>
                        Not for me
                      </Button>
                    </Stack>
                  </Stack>
                  {dismissing === job.id && (
                    <ReasonComposer
                      id={job.id}
                      value={reasonDraft}
                      onChange={setReasonDraft}
                      onCancel={closeDismiss}
                      onSubmit={() => void confirmDismiss(job.id)}
                      busy={!!busy[job.id]}
                      submitLabel="Dismiss"
                      suggestion={lastReason}
                    />
                  )}
                </CardContent>
              </Card>
            ))}
          </Stack>
        )}

        {data.archive.length > 0 && (
          <Box>
            <Divider sx={{ mb: 2 }} />
            <Stack direction="row" spacing={1} sx={{ alignItems: 'center', justifyContent: 'space-between' }}>
              <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
                <Typography variant="h6">Archive</Typography>
                <Chip size="small" label={data.archive.length} />
              </Stack>
              <Button onClick={() => setShowArchive((v) => !v)}>{showArchive ? 'Hide' : 'Show'}</Button>
            </Stack>
            <Typography variant="caption" color="text.secondary">
              Not-applicable rows are a queue, not history: once the scanner is fixed for their
              reason they get deleted from the database.
            </Typography>
            {showArchive && (
              <Stack spacing={2} sx={{ mt: 2 }}>
                {data.archive.map((job) => (
                  <Card key={job.id} variant="outlined">
                    <CardContent>
                      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2} sx={{ justifyContent: 'space-between' }}>
                        <Box sx={{ minWidth: 0 }}>
                          <Typography variant="subtitle1" component="h2" sx={{ fontWeight: 500 }}>
                            {job.url ? (
                              <Link href={job.url} target="_blank" rel="noopener noreferrer" underline="hover" color="inherit">
                                {job.title}
                              </Link>
                            ) : (
                              job.title
                            )}
                          </Typography>
                          <Stack direction="row" spacing={1} useFlexGap sx={{ mt: 1, flexWrap: 'wrap', alignItems: 'center' }}>
                            <EmployerChip employer={job.employer} color={colorFor(job.employer)} />
                            {job.area && <AreaChip area={job.area} />}
                            <Chip
                              size="small"
                              variant="outlined"
                              color={job.status === 'applied' ? 'success' : 'default'}
                              label={job.status === 'applied' ? 'Applied' : 'Not applicable'}
                            />
                            <Typography variant="body2" color="text.secondary">
                              {metaLine(job, rel, true)}
                            </Typography>
                          </Stack>
                          {job.statusReason && (
                            <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
                              “{job.statusReason}”
                            </Typography>
                          )}
                          <RouteCard job={job} />
                        </Box>
                        <Stack direction="row" spacing={1} sx={{ flexShrink: 0, alignItems: 'flex-start' }}>
                          <Button size="small" disabled={!!busy[job.id]} onClick={() => void setStatus(job.id, 'new')}>
                            Restore
                          </Button>
                          {job.status === 'ignored' && (
                            <Button size="small" onClick={() => openDismiss(job.id, job.statusReason ?? '')}>
                              {job.statusReason ? 'Edit note' : 'Add note'}
                            </Button>
                          )}
                        </Stack>
                      </Stack>
                      {dismissing === job.id && (
                        <ReasonComposer
                          id={job.id}
                          value={reasonDraft}
                          onChange={setReasonDraft}
                          onCancel={closeDismiss}
                          onSubmit={() => void confirmDismiss(job.id)}
                          busy={!!busy[job.id]}
                          submitLabel="Save note"
                        />
                      )}
                    </CardContent>
                  </Card>
                ))}
              </Stack>
            )}
          </Box>
        )}

        <Typography variant="caption" component="p" color="text.secondary">
          Built from the job_watch.py scan. Matches are population-health, care-coordination and
          community-health roles within a 50 minute drive of York or West Chester, screened for
          licensure and seniority. Applied jobs stay in the archive. A Not-for-me row is a fix
          request: the reason is what the scanner gets corrected for, and once it is, that row is
          deleted from the database rather than kept.
        </Typography>
      </Stack>
    </Container>
  );
}
