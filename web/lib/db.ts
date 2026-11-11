import { Pool } from 'pg';
import crypto from 'node:crypto';
import type { Area } from './areas';

export type { Area } from './areas';
export { AREA_LABEL, AREAS } from './areas';

export type Status = 'new' | 'applied' | 'ignored' | 'retired';

export interface Job {
  id: string;
  employer: string;
  title: string;
  location: string | null;
  url: string;
  status: Status;
  /** Free-text note the human wrote when dismissing a job. */
  statusReason: string | null;
  posted: string | null;
  firstSeen: string;
  lastSeen: string;
  statusChangedAt: string | null;
  /** Present when the Pi resolved a route from one of the two cluster pins. */
  hasMap: boolean;
  driveMin: number | null;
  driveMi: number | null;
  /** Which cluster this posting belongs to: the nearer of the two pins. */
  area: Area | null;
  /** Encoded polyline (precision 5) of the drive; the browser decodes it to draw the map. */
  mapGeometry: string | null;
  mapOrigin: string | null;
}

/** Longest dismissal note we keep; anything past this is truncated. */
export const MAX_REASON = 400;

/** A route is ~1–8 KB of polyline; anything this big is not a route. */
export const MAX_GEOMETRY = 20_000;

export function rowToJob(r: Record<string, unknown>): Job {
  return {
    id: String(r.id),
    employer: String(r.employer),
    title: String(r.title),
    location: (r.location as string) ?? null,
    url: (r.url as string) ?? '',
    status: r.status as Status,
    statusReason: (r.status_reason as string | null) ?? null,
    posted: (r.posted as string) ?? null,
    firstSeen: new Date(r.first_seen as string).toISOString(),
    lastSeen: new Date(r.last_seen as string).toISOString(),
    statusChangedAt: r.status_changed_at ? new Date(r.status_changed_at as string).toISOString() : null,
    hasMap: Boolean(r.map_geometry),
    driveMin: r.drive_min === null || r.drive_min === undefined ? null : Number(r.drive_min),
    driveMi: r.drive_mi === null || r.drive_mi === undefined ? null : Number(r.drive_mi),
    area: (r.map_cluster as Area | null) ?? null,
    mapGeometry: (r.map_geometry as string | null) ?? null,
    mapOrigin: (r.map_origin as string | null) ?? null,
  };
}

/**
 * Columns every caller selects; keeps the two read paths in sync. The route-card fields come
 * from scalar subqueries so both queries keep their plain `FROM jobs`.
 */
export const JOB_COLUMNS = `id, employer, title, location, url, status, status_reason,
              posted::text AS posted, first_seen, last_seen, status_changed_at,
              (SELECT m.drive_min  FROM job_maps m WHERE m.id = jobs.id) AS drive_min,
              (SELECT m.drive_mi   FROM job_maps m WHERE m.id = jobs.id) AS drive_mi,
              (SELECT m.cluster    FROM job_maps m WHERE m.id = jobs.id) AS map_cluster,
              (SELECT m.geometry   FROM job_maps m WHERE m.id = jobs.id) AS map_geometry,
              (SELECT m.origin_label FROM job_maps m WHERE m.id = jobs.id) AS map_origin`;

const CONN = process.env.POSTGRES_URL || process.env.DATABASE_URL || '';

declare global {
  // eslint-disable-next-line no-var
  var __jobPool: Pool | undefined;
  // eslint-disable-next-line no-var
  var __jobSchema: Promise<void> | undefined;
}

export function hasDb(): boolean {
  return Boolean(CONN);
}

export function pool(): Pool {
  if (!CONN) throw new Error('POSTGRES_URL is not configured');
  if (!global.__jobPool) {
    global.__jobPool = new Pool({ connectionString: CONN, max: 3, idleTimeoutMillis: 10_000 });
  }
  return global.__jobPool;
}

export async function ensureSchema(): Promise<void> {
  if (!global.__jobSchema) {
    global.__jobSchema = (async () => {
      await pool().query(`
        CREATE TABLE IF NOT EXISTS jobs (
          id                TEXT PRIMARY KEY,
          job_key           TEXT,
          employer          TEXT NOT NULL,
          title             TEXT NOT NULL,
          location          TEXT,
          url               TEXT,
          status            TEXT NOT NULL DEFAULT 'new',
          status_reason     TEXT,
          posted            DATE,
          first_seen        TIMESTAMPTZ NOT NULL DEFAULT now(),
          last_seen         TIMESTAMPTZ NOT NULL DEFAULT now(),
          status_changed_at TIMESTAMPTZ
        );
      `);
      await pool().query(`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS job_key TEXT;`);
      await pool().query(`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS posted DATE;`);
      await pool().query(`ALTER TABLE jobs ADD COLUMN IF NOT EXISTS status_reason TEXT;`);
      await pool().query(`CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status);`);
      await pool().query(`CREATE INDEX IF NOT EXISTS jobs_last_seen_idx ON jobs (last_seen DESC);`);
      await pool().query(`
        CREATE TABLE IF NOT EXISTS meta (
          key   TEXT PRIMARY KEY,
          value TEXT
        );
      `);
      // Route cards: the drive from the pinned origin, as an encoded polyline the browser
      // draws over OpenStreetMap tiles. No image is stored, so a card costs ~2 KB not ~200 KB.
      await pool().query(`
        CREATE TABLE IF NOT EXISTS job_maps (
          id           TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
          geometry     TEXT,
          drive_min    INTEGER,
          drive_mi     NUMERIC(6,1),
          cluster      TEXT,
          origin_label TEXT,
          generated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
      `);
      // Which of the two clusters the posting belongs to (added when the board split in two).
      await pool().query(`ALTER TABLE job_maps ADD COLUMN IF NOT EXISTS cluster TEXT;`);
      // The first cut stored a rendered PNG instead; that shape is not compatible and the rows
      // carried nothing worth keeping, so swap the column out rather than leave a dead one.
      const legacy = await pool().query(
        `SELECT 1 FROM information_schema.columns
          WHERE table_name = 'job_maps' AND column_name = 'png'`);
      if (legacy.rowCount) {
        await pool().query(`ALTER TABLE job_maps ADD COLUMN IF NOT EXISTS geometry TEXT;`);
        await pool().query(`ALTER TABLE job_maps DROP COLUMN IF EXISTS png;`);
      }
    })();
  }
  return global.__jobSchema;
}

/** Stable id derived from the Pi's employer + requisition id. */
export function jobId(employer: string, jobKey: string): string {
  return crypto.createHash('sha1').update(`${employer}|${jobKey}`).digest('hex').slice(0, 16);
}

export async function setMeta(key: string, value: string): Promise<void> {
  await pool().query(
    `INSERT INTO meta (key, value) VALUES ($1, $2)
     ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value`,
    [key, value],
  );
}
