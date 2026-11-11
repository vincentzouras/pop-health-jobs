import JobBoard from './JobBoard';
import { ensureSchema, hasDb, JOB_COLUMNS, pool, rowToJob } from '@/lib/db';

export const dynamic = 'force-dynamic';

const EMPTY_META = { lastIngest: null, scannerVersion: null, mapOrigin: null, retired: 0 };

async function load() {
  if (!hasDb()) {
    return {
      active: [],
      archive: [],
      meta: { ...EMPTY_META, db: false },
    };
  }
  try {
    await ensureSchema();
    const { rows } = await pool().query(
      `SELECT ${JOB_COLUMNS}
         FROM jobs
        ORDER BY posted DESC NULLS LAST, first_seen DESC`,
    );
    const jobs = rows.map(rowToJob);
    const meta = await pool().query(
      `SELECT key, value FROM meta WHERE key IN ('last_ingest','scanner_version')`,
    );
    const origin = await pool().query(
      `SELECT origin_label FROM job_maps WHERE origin_label IS NOT NULL LIMIT 1`,
    );
    const metaMap = Object.fromEntries(meta.rows.map((r) => [String(r.key), r.value as string]));
    return {
      active: jobs.filter((j) => j.status === 'new'),
      archive: jobs.filter((j) => j.status === 'applied' || j.status === 'ignored'),
      meta: {
        lastIngest: metaMap.last_ingest ?? null,
        scannerVersion: metaMap.scanner_version ?? null,
        mapOrigin: (origin.rows[0]?.origin_label as string) ?? null,
        retired: jobs.filter((j) => j.status === 'retired').length,
        db: true,
      },
    };
  } catch (err) {
    return {
      active: [],
      archive: [],
      meta: { ...EMPTY_META, db: true },
      error: String(err).slice(0, 300),
    };
  }
}

export default async function Page() {
  const initial = await load();
  return <JobBoard initial={initial} />;
}
