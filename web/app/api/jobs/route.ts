import { NextResponse } from 'next/server';
import { ensureSchema, hasDb, JOB_COLUMNS, pool, rowToJob } from '@/lib/db';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const EMPTY_META = { lastIngest: null, scannerVersion: null, mapOrigin: null, retired: 0 };

export async function GET() {
  if (!hasDb()) {
    return NextResponse.json(
      { active: [], archive: [], meta: { ...EMPTY_META, db: false } },
      { status: 200 },
    );
  }
  try {
    await ensureSchema();
    const { rows } = await pool().query(
      `SELECT ${JOB_COLUMNS}
         FROM jobs
        ORDER BY posted DESC NULLS LAST, first_seen DESC`,
    );
    const jobs = rows.map(rowToJob);
    const active = jobs.filter((j) => j.status === 'new');
    const archive = jobs.filter((j) => j.status === 'applied' || j.status === 'ignored');
    // Rows that dropped outside the 50 minute boundary are neither: counted, not listed.
    const retired = jobs.filter((j) => j.status === 'retired').length;

    const meta = await pool().query(
      `SELECT key, value FROM meta WHERE key IN ('last_ingest','scanner_version')`,
    );
    const origin = await pool().query(
      `SELECT origin_label FROM job_maps WHERE origin_label IS NOT NULL LIMIT 1`,
    );
    const metaMap = Object.fromEntries(meta.rows.map((r) => [String(r.key), r.value as string]));
    return NextResponse.json({
      active,
      archive,
      meta: {
        lastIngest: metaMap.last_ingest ?? null,
        scannerVersion: metaMap.scanner_version ?? null,
        mapOrigin: (origin.rows[0]?.origin_label as string) ?? null,
        retired,
        db: true,
      },
    });
  } catch (err) {
    return NextResponse.json(
      {
        active: [], archive: [],
        meta: { ...EMPTY_META, db: true },
        error: String(err).slice(0, 300),
      },
      { status: 200 },
    );
  }
}
