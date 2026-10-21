"""Adaptador SQLite: transações, chave de organização e auditoria."""
import sqlite3
import os
import json
import uuid
import re
import hashlib
import random
import secrets
import hmac
from pathlib import Path
from .database_paths import validate_database_path
from .environment_policy import require_operational_office
from datetime import date, datetime, timedelta, timezone
from .models import Snapshot, Result
from .normalizer import canonical, timestamp
from .detector import classify


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
 org TEXT NOT NULL, number TEXT NOT NULL, court TEXT NOT NULL,
 interval INTEGER NOT NULL, created_at TEXT NOT NULL, last_checked TEXT,
 next_due TEXT NOT NULL, last_error TEXT, metadata TEXT,
 PRIMARY KEY(org, number));
CREATE TABLE IF NOT EXISTS scopes (
 org TEXT NOT NULL, number TEXT NOT NULL, scope TEXT NOT NULL,
 PRIMARY KEY(org, number, scope),
 FOREIGN KEY(org, number) REFERENCES watches(org, number));
CREATE TABLE IF NOT EXISTS runs (
 id INTEGER PRIMARY KEY, org TEXT NOT NULL, number TEXT NOT NULL,
 started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
 error TEXT, raw TEXT, result TEXT,
 FOREIGN KEY(org, number) REFERENCES watches(org, number));
CREATE TABLE IF NOT EXISTS movements (
 org TEXT NOT NULL, number TEXT NOT NULL, fingerprint TEXT NOT NULL,
 scope TEXT NOT NULL, anchor TEXT NOT NULL, when_at TEXT NOT NULL,
 code TEXT NOT NULL, name TEXT NOT NULL, complements TEXT NOT NULL,
 raw TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, kind TEXT NOT NULL,
 PRIMARY KEY(org, number, fingerprint),
 FOREIGN KEY(org, number) REFERENCES watches(org, number));
CREATE INDEX IF NOT EXISTS movements_anchor ON movements(org, number, anchor);
CREATE TABLE IF NOT EXISTS events (
 id INTEGER PRIMARY KEY, org TEXT NOT NULL, number TEXT NOT NULL,
 fingerprint TEXT NOT NULL, kind TEXT NOT NULL, detected_at TEXT NOT NULL,
 run_id INTEGER NOT NULL, replaces TEXT,
 UNIQUE(org, number, fingerprint, kind),
 FOREIGN KEY(run_id) REFERENCES runs(id),
 FOREIGN KEY(org, number, fingerprint) REFERENCES movements(org, number, fingerprint));
CREATE TABLE IF NOT EXISTS communications (
 org TEXT NOT NULL, number TEXT NOT NULL, source_id TEXT NOT NULL,
 payload TEXT NOT NULL, collected_at TEXT NOT NULL,
 PRIMARY KEY(org, number, source_id),
 FOREIGN KEY(org, number) REFERENCES watches(org, number));
CREATE TABLE IF NOT EXISTS communication_inbox (
 org TEXT NOT NULL, number TEXT NOT NULL, source_id TEXT NOT NULL,
 payload TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
 read_at TEXT, discovery_kind TEXT NOT NULL DEFAULT 'process_sync',
 found_by_process INTEGER NOT NULL DEFAULT 0 CHECK(found_by_process IN (0,1)),
 found_by_oab INTEGER NOT NULL DEFAULT 0 CHECK(found_by_oab IN (0,1)),
 PRIMARY KEY(org,number,source_id));
CREATE INDEX IF NOT EXISTS communication_inbox_date
 ON communication_inbox(org,first_seen_at,read_at);
CREATE TABLE IF NOT EXISTS communication_oab_links (
 org TEXT NOT NULL, number TEXT NOT NULL, source_id TEXT NOT NULL,
 oab TEXT NOT NULL, uf TEXT NOT NULL, monitor_id INTEGER,
 linked_at TEXT NOT NULL,
 PRIMARY KEY(org,number,source_id,oab,uf),
 FOREIGN KEY(org,number,source_id) REFERENCES communication_inbox(org,number,source_id) ON DELETE CASCADE,
 FOREIGN KEY(monitor_id) REFERENCES oab_monitors(id) ON DELETE SET NULL);
CREATE INDEX IF NOT EXISTS communication_oab_filter
 ON communication_oab_links(org,oab,uf,number,source_id);
CREATE TABLE IF NOT EXISTS source_checks (
 id INTEGER PRIMARY KEY, org TEXT NOT NULL, number TEXT NOT NULL,
 source TEXT NOT NULL, checked_at TEXT NOT NULL, status TEXT NOT NULL,
 period_start TEXT, period_end TEXT, count INTEGER, error TEXT,
 FOREIGN KEY(org, number) REFERENCES watches(org, number));
CREATE TABLE IF NOT EXISTS access_log (
 id INTEGER PRIMARY KEY, at TEXT NOT NULL, action TEXT NOT NULL,
 number TEXT, actor TEXT NOT NULL, purpose TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS raw_records (
 org TEXT NOT NULL, number TEXT NOT NULL, source TEXT NOT NULL,
 source_id TEXT NOT NULL, payload TEXT NOT NULL, collected_at TEXT NOT NULL,
 PRIMARY KEY(org,number,source,source_id));
CREATE TABLE IF NOT EXISTS portfolios (
 org TEXT NOT NULL, name TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(org,name));
CREATE TABLE IF NOT EXISTS portfolio_processes (
 org TEXT NOT NULL, name TEXT NOT NULL, number TEXT NOT NULL,
 PRIMARY KEY(org,name,number),
 FOREIGN KEY(org,name) REFERENCES portfolios(org,name),
 FOREIGN KEY(org,number) REFERENCES watches(org,number));
CREATE TABLE IF NOT EXISTS tpu_catalog_versions (
 version TEXT PRIMARY KEY, reference_date TEXT NOT NULL, source TEXT NOT NULL,
 imported_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tpu_catalog_items (
 version TEXT NOT NULL, code TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY(version,code), FOREIGN KEY(version) REFERENCES tpu_catalog_versions(version));
CREATE TABLE IF NOT EXISTS tpu_enrichments (
 org TEXT NOT NULL, number TEXT NOT NULL, fingerprint TEXT NOT NULL,
 version TEXT NOT NULL, payload TEXT NOT NULL, processed_at TEXT NOT NULL,
 PRIMARY KEY(org,number,fingerprint,version),
 FOREIGN KEY(org,number,fingerprint) REFERENCES movements(org,number,fingerprint),
 FOREIGN KEY(version) REFERENCES tpu_catalog_versions(version));
CREATE TABLE IF NOT EXISTS event_relations (
 org TEXT NOT NULL, number TEXT NOT NULL, left_id TEXT NOT NULL,
 right_id TEXT NOT NULL, status TEXT NOT NULL, evidence TEXT NOT NULL,
 reviewed_by TEXT, updated_at TEXT NOT NULL,
 PRIMARY KEY(org,number,left_id,right_id),
 FOREIGN KEY(org,number) REFERENCES watches(org,number));
CREATE TABLE IF NOT EXISTS discovery_runs (
 id INTEGER PRIMARY KEY, org TEXT NOT NULL, source TEXT NOT NULL,
 criterion TEXT NOT NULL, oab TEXT NOT NULL, uf TEXT NOT NULL,
 period_start TEXT NOT NULL, period_end TEXT NOT NULL,
 checked_at TEXT NOT NULL, status TEXT NOT NULL, pages INTEGER,
 reported INTEGER, matched INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS oab_monitors (
 id INTEGER PRIMARY KEY, org TEXT NOT NULL, oab TEXT NOT NULL, uf TEXT NOT NULL,
 lawyer_name TEXT, enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 interval_hours INTEGER NOT NULL DEFAULT 24 CHECK(interval_hours IN (6,12,24,48)),
 created_at TEXT NOT NULL, last_attempt_at TEXT, last_success_at TEXT,
 next_run_at TEXT, status TEXT NOT NULL DEFAULT 'active', last_error TEXT,
 last_result TEXT, cursor_end TEXT, baseline_completed INTEGER NOT NULL DEFAULT 0 CHECK(baseline_completed IN (0,1)),
 last_job_id INTEGER, UNIQUE(org,oab,uf));
CREATE INDEX IF NOT EXISTS oab_monitors_due ON oab_monitors(enabled,next_run_at);
CREATE TABLE IF NOT EXISTS oab_monitor_jobs (
 id INTEGER PRIMARY KEY, monitor_id INTEGER NOT NULL, org TEXT NOT NULL,
 window_start TEXT NOT NULL, window_end TEXT NOT NULL,
 run_kind TEXT NOT NULL CHECK(run_kind IN ('baseline','incremental','manual')),
 status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled')),
 attempts INTEGER NOT NULL DEFAULT 0, scheduled_at TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
 result TEXT, error TEXT,
 FOREIGN KEY(monitor_id) REFERENCES oab_monitors(id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS oab_monitor_jobs_ready ON oab_monitor_jobs(status,scheduled_at,id);
CREATE UNIQUE INDEX IF NOT EXISTS oab_monitor_jobs_active_dedupe ON oab_monitor_jobs(monitor_id)
 WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS discovered_processes (
 org TEXT NOT NULL, number TEXT NOT NULL, source TEXT NOT NULL,
 source_id TEXT NOT NULL, oab TEXT NOT NULL, uf TEXT NOT NULL,
 available_at TEXT, collected_at TEXT NOT NULL,
 PRIMARY KEY(org,source,source_id,oab,uf));
CREATE TABLE IF NOT EXISTS collection_jobs (
 id INTEGER PRIMARY KEY, org TEXT NOT NULL, number TEXT NOT NULL,
 group_id TEXT NOT NULL, source TEXT NOT NULL, collection_type TEXT NOT NULL,
 stage_order INTEGER NOT NULL, priority INTEGER NOT NULL DEFAULT 100,
 status TEXT NOT NULL CHECK(status IN ('queued','running','completed','partial','failed','awaiting_access')),
 attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 5,
 scheduled_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 started_at TEXT, completed_at TEXT, lease_until TEXT, lease_token TEXT,
 execution_id TEXT, source_execution_id TEXT, lease_owner TEXT, params TEXT NOT NULL DEFAULT '{}',
 error TEXT, coverage TEXT, result TEXT,
 FOREIGN KEY(org,number) REFERENCES watches(org,number) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS collection_jobs_ready ON collection_jobs(status,scheduled_at,priority,id);
CREATE INDEX IF NOT EXISTS collection_jobs_group ON collection_jobs(org,number,group_id,stage_order);
CREATE UNIQUE INDEX IF NOT EXISTS collection_jobs_active_dedupe
 ON collection_jobs(org,number,source,collection_type,group_id)
 WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS source_rate_limits (
 source TEXT PRIMARY KEY, next_allowed_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS human_verifications (
 id TEXT PRIMARY KEY, org TEXT NOT NULL, number TEXT NOT NULL, job_id INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('queued','aguardando_verificacao_humana','accepted','expired','cancelled','failed')),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 completed_at TEXT, worker_id TEXT, error_code TEXT,
 FOREIGN KEY(org,number) REFERENCES watches(org,number) ON DELETE CASCADE,
 FOREIGN KEY(job_id) REFERENCES collection_jobs(id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS human_verifications_job ON human_verifications(job_id,created_at);
CREATE TABLE IF NOT EXISTS logical_event_states (
 org TEXT NOT NULL, number TEXT NOT NULL, logical_id TEXT NOT NULL,
 anchor_key TEXT NOT NULL, first_seen_at TEXT, baseline INTEGER NOT NULL DEFAULT 0,
 eligible INTEGER NOT NULL DEFAULT 0, read_at TEXT, read_by TEXT,
 merged_into TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(org,number,logical_id),
 FOREIGN KEY(org,number) REFERENCES watches(org,number) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS logical_event_unread ON logical_event_states(org,number,eligible,read_at);
CREATE TABLE IF NOT EXISTS logical_event_members (
 org TEXT NOT NULL, number TEXT NOT NULL, source_event_key TEXT NOT NULL,
 logical_id TEXT NOT NULL, source TEXT NOT NULL, first_seen_at TEXT,
 PRIMARY KEY(org,number,source_event_key),
 FOREIGN KEY(org,number,logical_id) REFERENCES logical_event_states(org,number,logical_id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS logical_event_members_state ON logical_event_members(org,number,logical_id);
CREATE TABLE IF NOT EXISTS i2_state_meta (
 key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS monitor_schedules (
 org TEXT NOT NULL, number TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
 interval_hours INTEGER NOT NULL DEFAULT 24 CHECK(interval_hours IN (6,12,24,48)),
 last_scheduled_at TEXT, last_execution_at TEXT, next_run_at TEXT,
 status TEXT NOT NULL DEFAULT 'paused', last_error TEXT, last_group_id TEXT,
 updated_at TEXT NOT NULL,
 PRIMARY KEY(org,number),
 FOREIGN KEY(org,number) REFERENCES watches(org,number) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS monitor_schedules_due ON monitor_schedules(enabled,next_run_at);
CREATE TABLE IF NOT EXISTS offices (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 email TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY, email TEXT NOT NULL COLLATE NOCASE UNIQUE,
 password_hash TEXT NOT NULL, password_salt TEXT NOT NULL,
 password_iterations INTEGER NOT NULL, created_at TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), full_name TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS office_memberships (
 office_id TEXT NOT NULL, user_id INTEGER NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('office_admin','lawyer')),
 created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 display_name TEXT NOT NULL DEFAULT '', PRIMARY KEY(office_id,user_id),
 FOREIGN KEY(office_id) REFERENCES offices(id),
 FOREIGN KEY(user_id) REFERENCES users(id));
CREATE INDEX IF NOT EXISTS office_memberships_user ON office_memberships(user_id,office_id);
CREATE TABLE IF NOT EXISTS auth_sessions (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, token_hash TEXT NOT NULL UNIQUE,
 active_office_id TEXT NOT NULL, created_at TEXT NOT NULL,
 last_seen_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT,
 FOREIGN KEY(active_office_id,user_id) REFERENCES office_memberships(office_id,user_id));
CREATE INDEX IF NOT EXISTS auth_sessions_expiry ON auth_sessions(expires_at,revoked_at);
CREATE TABLE IF NOT EXISTS service_credentials (
 id TEXT PRIMARY KEY, org TEXT NOT NULL, name TEXT NOT NULL,
 token_hash TEXT NOT NULL UNIQUE, permissions TEXT NOT NULL,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT,
 last_used_at TEXT, rotated_from_id TEXT, created_by TEXT,
 FOREIGN KEY(org) REFERENCES offices(id),
 FOREIGN KEY(rotated_from_id) REFERENCES service_credentials(id));
CREATE INDEX IF NOT EXISTS service_credentials_scope ON service_credentials(org,revoked_at,expires_at);
CREATE TABLE IF NOT EXISTS password_recovery_tokens (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, token_hash TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL, expires_at TEXT NOT NULL, consumed_at TEXT,
 FOREIGN KEY(user_id) REFERENCES users(id));
CREATE INDEX IF NOT EXISTS password_recovery_expiry ON password_recovery_tokens(expires_at,consumed_at);
CREATE TABLE IF NOT EXISTS platform_admins (
 id INTEGER PRIMARY KEY, email TEXT NOT NULL COLLATE NOCASE UNIQUE,
 password_hash TEXT NOT NULL, password_salt TEXT NOT NULL,
 password_iterations INTEGER NOT NULL, created_at TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), last_login_at TEXT,
 is_owner INTEGER NOT NULL DEFAULT 0 CHECK(is_owner IN (0,1)));
CREATE TABLE IF NOT EXISTS platform_admin_sessions (
 id TEXT PRIMARY KEY, admin_id INTEGER NOT NULL, token_hash TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
 expires_at TEXT NOT NULL, revoked_at TEXT,
 FOREIGN KEY(admin_id) REFERENCES platform_admins(id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS platform_admin_sessions_expiry
 ON platform_admin_sessions(expires_at,revoked_at);
CREATE TABLE IF NOT EXISTS office_calendar_items (
 id TEXT PRIMARY KEY, org TEXT NOT NULL, number TEXT,
 item_type TEXT NOT NULL CHECK(item_type IN ('hearing','meeting','task','deadline')),
 title TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT,
 status TEXT NOT NULL CHECK(status IN ('open','confirmed','discarded','scheduled','completed','cancelled')),
 responsible TEXT, notes TEXT NOT NULL DEFAULT '', origin TEXT NOT NULL DEFAULT 'manual',
 source_id TEXT, created_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 FOREIGN KEY(org,number) REFERENCES watches(org,number) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS office_calendar_scope ON office_calendar_items(org,item_type,status,starts_at);
"""


class SQLiteRepository:
    def __init__(self, path: str, *, environment=None, primary_path=None):
        self.environment = (environment or os.getenv("PROCESSWATCH_ENV", "development")).strip().lower()
        path = validate_database_path(
            path,
            environment=self.environment,
            primary_path=primary_path,
        )
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18):
            raise ValueError("Versão do banco não suportada.")
        has_user_tables = bool(self.db.execute("""SELECT 1 FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%' LIMIT 1""").fetchone())
        if version < 18 and path != ":memory:" and Path(path).is_file() and has_user_tables:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
            backup_path = Path(f"{path}.pre-v18-{stamp}.backup.sqlite3")
            backup = sqlite3.connect(str(backup_path), timeout=30)
            try:
                self.db.backup(backup)
                if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup anterior à migração v18 não passou na verificação de integridade.")
            except Exception:
                backup.close()
                backup_path.unlink(missing_ok=True)
                raise
            else:
                backup.close()
        try:
            self.db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
        except Exception:
            if self.db.in_transaction:
                self.db.rollback()
            raise
        with self.db:
            office_columns = {row[1] for row in self.db.execute("PRAGMA table_info(offices)")}
            for column in ("email", "phone", "address"):
                if column not in office_columns:
                    self.db.execute(f"ALTER TABLE offices ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            user_columns = {row[1] for row in self.db.execute("PRAGMA table_info(users)")}
            if "full_name" not in user_columns:
                self.db.execute("ALTER TABLE users ADD COLUMN full_name TEXT NOT NULL DEFAULT ''")
            membership_columns = {row[1] for row in self.db.execute("PRAGMA table_info(office_memberships)")}
            if "active" not in membership_columns:
                self.db.execute("ALTER TABLE office_memberships ADD COLUMN active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))")
            if "display_name" not in membership_columns:
                self.db.execute("ALTER TABLE office_memberships ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
        with self.db:
            self.db.execute("""INSERT OR IGNORE INTO monitor_schedules
                (org,number,enabled,interval_hours,status,updated_at)
                SELECT org,number,0,24,'paused',? FROM watches""", (now(),))
            stamp = now()
            tenant_tables = ("watches", "portfolios", "collection_jobs", "discovered_processes",
                             "discovery_runs", "source_checks", "raw_records", "communications",
                             "movements", "runs", "events", "monitor_schedules")
            orgs = set()
            for table in tenant_tables:
                orgs.update(row[0] for row in self.db.execute(
                    f"SELECT DISTINCT org FROM {table} WHERE org IS NOT NULL").fetchall())
            for office_id in orgs:
                if self.environment == "production" and office_id == "local":
                    continue
                display_name = "Escritório local" if office_id == "local" else office_id
                self.db.execute("INSERT OR IGNORE INTO offices(id,name,created_at) VALUES(?,?,?)",
                                (office_id, display_name, stamp))
            if self.environment != "production":
                self.db.execute("INSERT OR IGNORE INTO offices(id,name,created_at) VALUES('local','Escritório local',?)",
                                (stamp,))
                self.db.execute("UPDATE offices SET name='Escritório local' WHERE id='local' AND name='local'")
        with self.db:
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(access_log)")}
            if "actor" not in columns:
                self.db.execute("ALTER TABLE access_log ADD COLUMN actor TEXT NOT NULL DEFAULT 'local-authenticated-user'")
            if "org" not in columns:
                self.db.execute("ALTER TABLE access_log ADD COLUMN org TEXT")
        self._ensure_i2_baseline()
        with self.db:
            # Additively index historical process-scoped DJEN rows in the shared inbox.
            # The composite identity matches the existing communications primary key.
            self.db.execute("""INSERT OR IGNORE INTO communication_inbox
                (org,number,source_id,payload,first_seen_at,last_seen_at,discovery_kind,found_by_process)
                SELECT org,number,source_id,payload,collected_at,collected_at,'process_sync',1
                FROM communications""")
            platform_admin_columns = {row[1] for row in self.db.execute("PRAGMA table_info(platform_admins)")}
            if "is_owner" not in platform_admin_columns:
                self.db.execute("ALTER TABLE platform_admins ADD COLUMN is_owner INTEGER NOT NULL DEFAULT 0 CHECK(is_owner IN (0,1))")
            if not self.db.execute("SELECT 1 FROM platform_admins WHERE is_owner=1 LIMIT 1").fetchone():
                first_admin = self.db.execute("SELECT id FROM platform_admins ORDER BY id LIMIT 1").fetchone()
                if first_admin:
                    self.db.execute("UPDATE platform_admins SET is_owner=1 WHERE id=?", (first_admin["id"],))
            self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS platform_admin_single_owner "
                            "ON platform_admins(is_owner) WHERE is_owner=1")
            self.db.execute("PRAGMA user_version=18")

    def close(self):
        self.db.close()

    def list_offices(self):
        return [dict(row) for row in self.db.execute(
            "SELECT id,name,email,phone,address,created_at,active FROM offices ORDER BY name,id")]

    def office(self, office_id):
        row = self.db.execute(
            "SELECT id,name,email,phone,address,created_at,active FROM offices WHERE id=?",
            (office_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _validated_office_profile(name, email, phone, address):
        name, email, phone, address = (str(value or "").strip() for value in
                                      (name, email, phone, address))
        if not name or len(name) > 160:
            raise ValueError("Informe um nome de escritório de até 160 caracteres.")
        if email and (len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)):
            raise ValueError("E-mail do escritório inválido.")
        if len(phone) > 40 or (phone and not re.fullmatch(r"[+()\d\s.\-]{7,40}", phone)):
            raise ValueError("Telefone do escritório inválido.")
        if len(address) > 500 or any(ord(char) < 32 and char not in "\n\t" for char in address):
            raise ValueError("Endereço do escritório inválido ou muito longo.")
        return name, email, phone, address

    def update_office(self, office_id, *, name, email, phone, address):
        """Update all office profile fields for the authenticated office API."""
        name, email, phone, address = self._validated_office_profile(name, email, phone, address)
        with self.db:
            changed = self.db.execute(
                "UPDATE offices SET name=?,email=?,phone=?,address=? WHERE id=? AND active=1",
                (name, email, phone, address, office_id)).rowcount
        if not changed:
            raise LookupError("Escritório ativo não encontrado.")
        return self.office(office_id)

    def patch_platform_office(self, office_id, changes):
        """Partially update an existing office profile without changing its lifecycle state."""
        allowed = {"name", "email", "phone", "address"}
        if not changes or set(changes) - allowed:
            raise ValueError("Informe ao menos um campo cadastral válido.")
        if any(value is None for value in changes.values()):
            raise ValueError("Campos cadastrais não podem ser nulos; use texto vazio para limpar campos opcionais.")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT name,email,phone,address FROM offices WHERE id=?", (office_id,)).fetchone()
            if not row:
                raise LookupError("Escritório não encontrado.")
            values = {key: row[key] for key in allowed}
            values.update(changes)
            values = self._validated_office_profile(**values)
            self.db.execute("UPDATE offices SET name=?,email=?,phone=?,address=? WHERE id=?",
                            (*values, office_id))
        return self.office(office_id)

    def set_office_active(self, office_id, active):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            office = self.office(office_id)
            if not office:
                raise LookupError("Escritório não encontrado.")
            if not active and self._office_has_running_jobs(office_id):
                raise ValueError("Aguarde a conclusão dos jobs em andamento antes de desativar o escritório.")
            self.db.execute("UPDATE offices SET active=? WHERE id=?", (int(active), office_id))
        return self.office(office_id)

    def _office_has_running_jobs(self, office_id):
        return any(self.db.execute(f"SELECT 1 FROM {table} WHERE org=? AND status='running' LIMIT 1",
                                   (office_id,)).fetchone()
                   for table in ("collection_jobs", "oab_monitor_jobs"))

    def delete_office(self, office_id):
        """Remove an office and all its scoped records atomically; never infer cascades."""
        scoped = ("human_verifications", "collection_jobs", "communication_oab_links",
                  "communication_inbox", "oab_monitor_jobs", "oab_monitors",
                  "logical_event_members", "logical_event_states", "events", "tpu_enrichments",
                  "event_relations", "office_calendar_items", "monitor_schedules",
                  "portfolio_processes", "communications", "movements", "runs",
                  "source_checks", "scopes", "portfolios", "watches", "raw_records",
                  "discovered_processes", "discovery_runs", "service_credentials")
        # Audit rows intentionally outlive their office so destructive removal
        # cannot erase the evidence of the deletion itself or earlier actions.
        known = set(scoped) | {"offices", "office_memberships", "access_log"}
        tables = [row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        unexpected = [table for table in tables if table not in known and
                      "org" in {row[1] for row in self.db.execute(f"PRAGMA table_info('{table}')")}]
        if unexpected:
            raise RuntimeError("Há novas tabelas organizacionais sem política de exclusão: " + ", ".join(unexpected))
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if not self.office(office_id):
                raise LookupError("Escritório não encontrado.")
            if self._office_has_running_jobs(office_id):
                raise ValueError("Aguarde a conclusão dos jobs em andamento antes de excluir o escritório.")
            member_ids = [row[0] for row in self.db.execute(
                "SELECT user_id FROM office_memberships WHERE office_id=?", (office_id,))]
            self.db.execute("DELETE FROM auth_sessions WHERE active_office_id=?", (office_id,))
            self.db.execute("UPDATE service_credentials SET rotated_from_id=NULL WHERE org=?", (office_id,))
            for table in scoped:
                self.db.execute(f"DELETE FROM {table} WHERE org=?", (office_id,))
            self.db.execute("DELETE FROM office_memberships WHERE office_id=?", (office_id,))
            for user_id in member_ids:
                if not self.db.execute("SELECT 1 FROM office_memberships WHERE user_id=?", (user_id,)).fetchone():
                    self.db.execute("DELETE FROM password_recovery_tokens WHERE user_id=?", (user_id,))
                    self.db.execute("DELETE FROM users WHERE id=?", (user_id,))
            self.db.execute("DELETE FROM offices WHERE id=?", (office_id,))
            if self.db.execute("PRAGMA foreign_key_check").fetchone():
                raise RuntimeError("Exclusão interrompida por falha de integridade referencial.")
        return True

    @staticmethod
    def _password_record(password, *, salt=None, iterations=310_000):
        import base64
        salt = salt or secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return (base64.urlsafe_b64encode(digest).decode("ascii"),
                base64.urlsafe_b64encode(salt).decode("ascii"), iterations)

    def create_office(self, office_id, name):
        office_id = str(office_id).strip().lower()
        require_operational_office(self.environment, office_id)
        name = str(name).strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", office_id):
            raise ValueError("Identificador do escritório inválido.")
        if not name or len(name) > 160:
            raise ValueError("Informe um nome de escritório de até 160 caracteres.")
        with self.db:
            self.db.execute("INSERT INTO offices(id,name,created_at) VALUES(?,?,?)",
                            (office_id, name, now()))
        return {"id": office_id, "name": name}

    def bootstrap_platform_admin(self, email, password):
        """Create the first global administrator through a local operator action only."""
        email = str(email or "").strip().casefold()
        email_parts = email.split("@")
        if (len(email) > 254 or len(email_parts) != 2 or not email_parts[0] or
                not email_parts[1] or "." not in email_parts[1] or
                any(char.isspace() for char in email)):
            raise ValueError("E-mail inválido.")
        if len(password or "") < 12 or len(password) > 1024:
            raise ValueError("A senha deve ter de 12 a 1024 caracteres.")
        password_hash, salt, iterations = self._password_record(password)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.db.execute("SELECT 1 FROM platform_admins LIMIT 1").fetchone():
                raise ValueError("A administração da plataforma já foi inicializada.")
            cursor = self.db.execute("""INSERT INTO platform_admins
                (email,password_hash,password_salt,password_iterations,created_at,is_owner)
                VALUES(?,?,?,?,?,1)""", (email, password_hash, salt, iterations, now()))
            self.db.execute("INSERT INTO access_log(at,action,number,actor,purpose,org) VALUES(?,?,?,?,?,NULL)",
                (now(), "platform_admin_bootstrap", None, f"platform_admin:{email}",
                 "criação local do primeiro administrador global"))
            return {"id": cursor.lastrowid, "email": email}

    def authenticate_platform_admin(self, email, password):
        row = self.db.execute("SELECT * FROM platform_admins WHERE email=? COLLATE NOCASE AND active=1",
                              (str(email or "").strip(),)).fetchone()
        if not row:
            self._password_record(password or "")
            return None
        if not self._check_password(password, row):
            return None
        with self.db:
            self.db.execute("UPDATE platform_admins SET last_login_at=? WHERE id=?", (now(), row["id"]))
        return {"id": row["id"], "email": row["email"], "is_owner": bool(row["is_owner"])}

    @staticmethod
    def _validated_platform_admin_email(email):
        email = str(email or "").strip().casefold()
        parts = email.split("@")
        if (len(email) > 254 or len(parts) != 2 or not parts[0] or not parts[1] or
                "." not in parts[1] or any(char.isspace() for char in email)):
            raise ValueError("E-mail inválido.")
        return email

    def create_platform_admin(self, email, password):
        """Create a delegated global administrator; never grants ownership."""
        email = self._validated_platform_admin_email(email)
        if len(password or "") < 12 or len(password) > 1024:
            raise ValueError("A senha deve ter de 12 a 1024 caracteres.")
        password_hash, salt, iterations = self._password_record(password)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            cursor = self.db.execute("""INSERT INTO platform_admins
                (email,password_hash,password_salt,password_iterations,created_at,active,is_owner)
                VALUES(?,?,?,?,?,1,0)""", (email, password_hash, salt, iterations, now()))
            return self.platform_admin(cursor.lastrowid)

    def list_platform_admins(self):
        return [dict(row) for row in self.db.execute("""SELECT a.id,a.email,a.active,a.is_owner,
            a.created_at,a.last_login_at,
            (SELECT count(*) FROM platform_admin_sessions s WHERE s.admin_id=a.id
                AND s.revoked_at IS NULL AND s.expires_at>?) AS active_sessions
            FROM platform_admins a ORDER BY a.is_owner DESC,a.id""", (now(),)).fetchall()]

    def platform_admin(self, admin_id):
        row = self.db.execute("""SELECT a.id,a.email,a.active,a.is_owner,a.created_at,a.last_login_at,
            (SELECT count(*) FROM platform_admin_sessions s WHERE s.admin_id=a.id
                AND s.revoked_at IS NULL AND s.expires_at>?) AS active_sessions
            FROM platform_admins a WHERE a.id=?""", (now(), admin_id)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["active"] = bool(result["active"])
        result["is_owner"] = bool(result["is_owner"])
        return result

    def update_platform_admin_email(self, admin_id, email):
        email = self._validated_platform_admin_email(email)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT id FROM platform_admins WHERE id=?", (admin_id,)).fetchone()
            if not row:
                raise LookupError("Administrador global não encontrado.")
            changed = self.db.execute("UPDATE platform_admins SET email=? WHERE id=? AND email<>?",
                                      (email, admin_id, email)).rowcount
            if changed:
                self.db.execute("UPDATE platform_admin_sessions SET revoked_at=? "
                                "WHERE admin_id=? AND revoked_at IS NULL", (now(), admin_id))
        return self.platform_admin(admin_id)

    def set_platform_admin_active(self, admin_id, active):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT active,is_owner FROM platform_admins WHERE id=?", (admin_id,)).fetchone()
            if not row:
                raise LookupError("Administrador global não encontrado.")
            if row["is_owner"] and not active:
                raise ValueError("A identidade proprietária não pode ser desativada.")
            self.db.execute("UPDATE platform_admins SET active=? WHERE id=?", (int(bool(active)), admin_id))
            if row["active"] and not active:
                self.db.execute("UPDATE platform_admin_sessions SET revoked_at=? "
                                "WHERE admin_id=? AND revoked_at IS NULL", (now(), admin_id))
        return self.platform_admin(admin_id)

    def revoke_platform_admin_sessions(self, admin_id, *, except_session_id=None):
        with self.db:
            if except_session_id:
                changed = self.db.execute("""UPDATE platform_admin_sessions SET revoked_at=?
                    WHERE admin_id=? AND id<>? AND revoked_at IS NULL""",
                    (now(), admin_id, except_session_id)).rowcount
            else:
                changed = self.db.execute("""UPDATE platform_admin_sessions SET revoked_at=?
                    WHERE admin_id=? AND revoked_at IS NULL""", (now(), admin_id)).rowcount
        return changed

    def change_platform_admin_password(self, admin_id, current_password, new_password):
        if len(new_password or "") < 12 or len(new_password) > 1024:
            raise ValueError("A nova senha deve ter de 12 a 1024 caracteres.")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT * FROM platform_admins WHERE id=? AND active=1",
                                  (admin_id,)).fetchone()
            if not row or not self._check_password(current_password, row):
                raise PermissionError("Senha atual inválida.")
            password_hash, salt, iterations = self._password_record(new_password)
            stamp = now()
            self.db.execute("UPDATE platform_admins SET password_hash=?,password_salt=?,"
                            "password_iterations=? WHERE id=?",
                            (password_hash, salt, iterations, admin_id))
            self.db.execute("UPDATE platform_admin_sessions SET revoked_at=? "
                            "WHERE admin_id=? AND revoked_at IS NULL", (stamp, admin_id))
        return True

    def reset_delegated_platform_admin_password(self, admin_id, new_password):
        if len(new_password or "") < 12 or len(new_password) > 1024:
            raise ValueError("A nova senha deve ter de 12 a 1024 caracteres.")
        password_hash, salt, iterations = self._password_record(new_password)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT is_owner FROM platform_admins WHERE id=?", (admin_id,)).fetchone()
            if not row:
                raise LookupError("Administrador global não encontrado.")
            if row["is_owner"]:
                raise ValueError("A senha do proprietário só pode ser recuperada por procedimento local.")
            stamp = now()
            self.db.execute("UPDATE platform_admins SET password_hash=?,password_salt=?,"
                            "password_iterations=? WHERE id=?",
                            (password_hash, salt, iterations, admin_id))
            self.db.execute("UPDATE platform_admin_sessions SET revoked_at=? "
                            "WHERE admin_id=? AND revoked_at IS NULL", (stamp, admin_id))
        return self.platform_admin(admin_id)

    def recover_platform_owner_password_locally(self, owner_email, new_password, *, actor="local_operator"):
        """Restore the bootstrap owner's access from an explicitly authorized local operation."""
        if len(new_password or "") < 12 or len(new_password) > 1024:
            raise ValueError("A nova senha deve ter de 12 a 1024 caracteres.")
        email = self._validated_platform_admin_email(owner_email)
        password_hash, salt, iterations = self._password_record(new_password)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            owner = self.db.execute("SELECT id,email FROM platform_admins WHERE is_owner=1 AND email=?",
                                    (email,)).fetchone()
            if not owner:
                raise LookupError("A identidade proprietária não corresponde ao e-mail informado.")
            stamp = now()
            self.db.execute("UPDATE platform_admins SET password_hash=?,password_salt=?,"
                            "password_iterations=?,active=1 WHERE id=?",
                            (password_hash, salt, iterations, owner["id"]))
            self.db.execute("UPDATE platform_admin_sessions SET revoked_at=? "
                            "WHERE admin_id=? AND revoked_at IS NULL", (stamp, owner["id"]))
            self._record_access_in_transaction(
                "platform_owner_password_recovery", None,
                f"recuperação local do proprietário admin_id={owner['id']}; conta ativada e sessões revogadas",
                actor=str(actor)[:160], org=None)
        return {"id": owner["id"], "email": owner["email"], "active": True, "is_owner": True}

    def create_platform_admin_session(self, admin_id, raw_token, expires_at):
        session_id = uuid.uuid4().hex
        stamp = now()
        token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        with self.db:
            self.db.execute("""INSERT INTO platform_admin_sessions
                (id,admin_id,token_hash,created_at,last_seen_at,expires_at)
                VALUES(?,?,?,?,?,?)""", (session_id, admin_id, token_hash, stamp, stamp, expires_at))
        return session_id

    def platform_admin_session(self, raw_token, *, idle_minutes=60):
        if not raw_token or len(raw_token) > 256:
            return None
        token_hash = hashlib.sha256(raw_token.encode("ascii", errors="ignore")).hexdigest()
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=idle_minutes)).isoformat()
        row = self.db.execute("""SELECT s.id session_id,s.admin_id,s.expires_at,a.email,a.is_owner
            FROM platform_admin_sessions s JOIN platform_admins a ON a.id=s.admin_id
            WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>? AND s.last_seen_at>? AND a.active=1""",
            (token_hash, now(), cutoff)).fetchone()
        if not row:
            with self.db:
                self.db.execute("""UPDATE platform_admin_sessions SET revoked_at=? WHERE token_hash=?
                    AND revoked_at IS NULL AND (expires_at<=? OR last_seen_at<=?)""",
                    (now(), token_hash, now(), cutoff))
            return None
        with self.db:
            self.db.execute("UPDATE platform_admin_sessions SET last_seen_at=? WHERE id=?",
                             (now(), row["session_id"]))
        return dict(row)

    def revoke_platform_admin_session(self, session_id):
        with self.db:
            return self.db.execute("UPDATE platform_admin_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                                   (now(), session_id)).rowcount > 0

    def provision_office(self, office_id, name, administrator_email, password, *,
                         full_name="", office_email="", phone="", address=""):
        """Atomically provision one office and its first office administrator."""
        office_id = str(office_id or "").strip().lower()
        name = str(name or "").strip()
        administrator_email = str(administrator_email or "").strip().casefold()
        full_name = str(full_name or "").strip()
        office_email = str(office_email or "").strip()
        phone = str(phone or "").strip()
        address = str(address or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", office_id):
            raise ValueError("Identificador do escritório inválido.")
        if not name or len(name) > 160:
            raise ValueError("Informe um nome de escritório de até 160 caracteres.")
        if len(full_name) > 160:
            raise ValueError("Nome deve ter até 160 caracteres.")
        email_parts = administrator_email.split("@")
        if (len(administrator_email) > 254 or len(email_parts) != 2 or not email_parts[0] or
                not email_parts[1] or "." not in email_parts[1] or
                any(char.isspace() for char in administrator_email)):
            raise ValueError("E-mail inválido.")
        if len(password or "") < 12 or len(password) > 1024:
            raise ValueError("A senha deve ter de 12 a 1024 caracteres.")
        if office_email and (len(office_email) > 254 or
                             not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", office_email)):
            raise ValueError("E-mail do escritório inválido.")
        if len(phone) > 40 or (phone and not re.fullmatch(r"[+()\d\s.\-]{7,40}", phone)):
            raise ValueError("Telefone do escritório inválido.")
        if len(address) > 500 or any(ord(char) < 32 and char not in "\n\t" for char in address):
            raise ValueError("Endereço do escritório inválido ou muito longo.")
        password_hash, salt, iterations = self._password_record(password)
        stamp = now()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("INSERT INTO offices(id,name,created_at,email,phone,address) VALUES(?,?,?,?,?,?)",
                            (office_id, name, stamp, office_email, phone, address))
            user_cursor = self.db.execute("""INSERT INTO users(email,password_hash,password_salt,
                password_iterations,created_at,full_name) VALUES(?,?,?,?,?,?)""",
                (administrator_email, password_hash, salt, iterations, stamp, full_name))
            user_id = user_cursor.lastrowid
            self.db.execute("""INSERT INTO office_memberships
                (office_id,user_id,role,created_at,display_name) VALUES(?,?,'office_admin',?,?)""",
                (office_id, user_id, stamp, full_name))
            return {"office": self.office(office_id),
                    "administrator": {"id": user_id, "email": administrator_email,
                                      "office_id": office_id, "role": "office_admin",
                                      "full_name": full_name}}

    def platform_office_stats(self, office_id):
        if not self.office(office_id):
            return None
        def count(sql, args=()):
            return self.db.execute(sql, args).fetchone()[0]
        return {
            "users": count("SELECT count(*) FROM office_memberships WHERE office_id=? AND active=1", (office_id,)),
            "processes": count("SELECT count(*) FROM watches WHERE org=?", (office_id,)),
            "movements": count("SELECT count(*) FROM movements WHERE org=?", (office_id,)),
            "communications": count("SELECT count(*) FROM communication_inbox WHERE org=?", (office_id,)),
            "jobs_pending": count("SELECT count(*) FROM collection_jobs WHERE org=? AND status='queued'", (office_id,)),
            "jobs_running": count("SELECT count(*) FROM collection_jobs WHERE org=? AND status='running'", (office_id,)),
            "jobs_failed": count("SELECT count(*) FROM collection_jobs WHERE org=? AND status='failed'", (office_id,)),
        }

    def _create_user_in_transaction(self, email, password, office_id, role="office_admin", full_name="",
                                   *, allow_inactive_office=False):
        require_operational_office(self.environment, office_id)
        email = str(email).strip().casefold()
        full_name = str(full_name or "").strip()
        if len(full_name) > 160:
            raise ValueError("Nome deve ter até 160 caracteres.")
        if not re.fullmatch(r"[^\s@]{1,64}@[^\s@]{1,190}", email):
            raise ValueError("E-mail inválido.")
        if len(password) < 12 or len(password) > 1024:
            raise ValueError("A senha deve ter de 12 a 1024 caracteres.")
        if role not in ("office_admin", "lawyer"):
            raise ValueError("Papel inválido.")
        password_hash, salt, iterations = self._password_record(password)
        stamp = now()
        office = self.db.execute("SELECT active FROM offices WHERE id=?", (office_id,)).fetchone()
        if not office or (not office["active"] and not allow_inactive_office):
            raise ValueError("Escritório não encontrado ou inativo.")
        cursor = self.db.execute("""INSERT INTO users(email,password_hash,password_salt,
            password_iterations,created_at,full_name) VALUES(?,?,?,?,?,?)""",
            (email, password_hash, salt, iterations, stamp, full_name))
        user_id = cursor.lastrowid
        self.db.execute("""INSERT INTO office_memberships
            (office_id,user_id,role,created_at,display_name) VALUES(?,?,?,?,?)""",
            (office_id, user_id, role, stamp, full_name))
        return {"id": user_id, "email": email, "office_id": office_id, "role": role,
                "full_name": full_name}

    def create_user(self, email, password, office_id, role="office_admin", full_name=""):
        require_operational_office(self.environment, office_id)
        with self.db:
            return self._create_user_in_transaction(email, password, office_id, role, full_name)

    def add_user_to_office(self, user_id, office_id, role="lawyer"):
        require_operational_office(self.environment, office_id)
        if role not in ("office_admin", "lawyer"):
            raise ValueError("Papel inválido.")
        with self.db:
            self.db.execute("INSERT INTO office_memberships(office_id,user_id,role,created_at) VALUES(?,?,?,?)",
                            (office_id, user_id, role, now()))

    def authenticate_user(self, email, password):
        import base64
        row = self.db.execute("""SELECT * FROM users WHERE email=? COLLATE NOCASE AND active=1""",
                              (str(email).strip(),)).fetchone()
        if not row:
            # Keep the failure path close to the cost of a real password check.
            self._password_record(password or "")
            return None
        if not self._check_password(password, row):
            return None
        offices = self.user_offices(row["id"], include_inactive_admin=True)
        return {"id": row["id"], "email": row["email"], "full_name": row["full_name"], "offices": offices}

    def user_offices(self, user_id, *, include_inactive_admin=False):
        return [dict(row) for row in self.db.execute("""SELECT o.id,o.name,o.active,m.role,m.display_name
            FROM office_memberships m JOIN offices o ON o.id=m.office_id
            WHERE m.user_id=? AND m.active=1 AND (o.active=1 OR (?=1 AND m.role='office_admin'))
            ORDER BY o.active DESC,o.name,o.id""", (user_id, int(include_inactive_admin)))]

    def create_auth_session(self, user_id, raw_token, office_id, expires_at):
        stamp = now()
        session_id = uuid.uuid4().hex
        token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        with self.db:
            self.db.execute("""INSERT INTO auth_sessions
                (id,user_id,token_hash,active_office_id,created_at,last_seen_at,expires_at)
                VALUES(?,?,?,?,?,?,?)""",
                (session_id, user_id, token_hash, office_id, stamp, stamp, expires_at))
        return session_id

    @staticmethod
    def _service_credential_metadata(row):
        data = dict(row)
        data["permissions"] = json.loads(data.pop("permissions"))
        data["active"] = data["revoked_at"] is None and data["expires_at"] > now()
        data.pop("token_hash", None)
        return data

    def create_service_credential(self, org, name, permissions, expires_at, *, created_by=None,
                                  rotated_from_id=None):
        require_operational_office(self.environment, org)
        name = str(name or "").strip()
        permissions = sorted(set(permissions))
        if not name or len(name) > 100:
            raise ValueError("O nome do serviço deve ter de 1 a 100 caracteres.")
        if not permissions:
            raise ValueError("Informe ao menos uma permissão.")
        raw_token = "pwsvc_" + secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        credential_id = uuid.uuid4().hex
        stamp = now()
        with self.db:
            self.db.execute("""INSERT INTO service_credentials
                (id,org,name,token_hash,permissions,created_at,expires_at,rotated_from_id,created_by)
                VALUES(?,?,?,?,?,?,?,?,?)""", (credential_id, org, name, token_hash, json.dumps(permissions), stamp,
                 expires_at, rotated_from_id, created_by))
            row = self.db.execute("SELECT * FROM service_credentials WHERE id=?", (credential_id,)).fetchone()
        return self._service_credential_metadata(row), raw_token

    def service_credentials(self, org):
        rows = self.db.execute("""SELECT * FROM service_credentials WHERE org=?
            ORDER BY created_at DESC,id""", (org,)).fetchall()
        return [self._service_credential_metadata(row) for row in rows]

    def service_credential(self, org, credential_id):
        row = self.db.execute("SELECT * FROM service_credentials WHERE org=? AND id=?",
                              (org, credential_id)).fetchone()
        return self._service_credential_metadata(row) if row else None

    def authenticate_service_credential(self, raw_token):
        if not raw_token or len(raw_token) > 256 or not raw_token.startswith("pwsvc_"):
            return None
        token_hash = hashlib.sha256(raw_token.encode("ascii", errors="ignore")).hexdigest()
        stamp = now()
        with self.db:
            row = self.db.execute("""SELECT c.* FROM service_credentials c JOIN offices o ON o.id=c.org
                WHERE c.token_hash=? AND c.revoked_at IS NULL AND c.expires_at>? AND o.active=1""",
                (token_hash, stamp)).fetchone()
            if not row:
                return None
            self.db.execute("UPDATE service_credentials SET last_used_at=? WHERE id=?", (stamp, row["id"]))
            updated = self.db.execute("SELECT * FROM service_credentials WHERE id=?", (row["id"],)).fetchone()
            return self._service_credential_metadata(updated)

    def revoke_service_credential(self, org, credential_id):
        with self.db:
            changed = self.db.execute("""UPDATE service_credentials SET revoked_at=?
                WHERE org=? AND id=? AND revoked_at IS NULL AND expires_at>?""",
                (now(), org, credential_id, now())).rowcount
        return bool(changed)

    def rotate_service_credential(self, org, credential_id, expires_at, *, created_by=None):
        stamp = now()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("""SELECT * FROM service_credentials WHERE org=? AND id=?
                AND revoked_at IS NULL AND expires_at>?""", (org, credential_id, stamp)).fetchone()
            if not row:
                return None
            self.db.execute("UPDATE service_credentials SET revoked_at=? WHERE id=?", (stamp, credential_id))
            return self.create_service_credential(org, row["name"], json.loads(row["permissions"]),
                expires_at, created_by=created_by, rotated_from_id=credential_id)

    def auth_session(self, raw_token, *, idle_minutes=60, allow_inactive=False):
        if not raw_token or len(raw_token) > 256:
            return None
        token_hash = hashlib.sha256(raw_token.encode("ascii", errors="ignore")).hexdigest()
        idle_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=idle_minutes)).isoformat()
        row = self.db.execute("""SELECT s.id session_id,s.user_id,s.active_office_id,s.expires_at,
            u.email,u.full_name,o.name office_name,m.role,m.display_name FROM auth_sessions s
            JOIN users u ON u.id=s.user_id
            JOIN offices o ON o.id=s.active_office_id
            JOIN office_memberships m ON m.user_id=s.user_id AND m.office_id=s.active_office_id
            WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>? AND s.last_seen_at>?
            AND u.active=1 AND (o.active=1 OR (?=1 AND m.role='office_admin')) AND m.active=1""",
            (token_hash, now(), idle_cutoff, int(allow_inactive))).fetchone()
        if not row:
            with self.db:
                self.db.execute("""UPDATE auth_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL
                    AND (expires_at<=? OR last_seen_at<=?)""", (now(), token_hash, now(), idle_cutoff))
            return None
        with self.db:
            self.db.execute("UPDATE auth_sessions SET last_seen_at=? WHERE id=?", (now(), row["session_id"]))
        data = dict(row)
        data["offices"] = self.user_offices(data["user_id"])
        return data

    def set_session_office(self, session_id, user_id, office_id):
        with self.db:
            changed = self.db.execute("""UPDATE auth_sessions SET active_office_id=?,last_seen_at=?
                WHERE id=? AND user_id=? AND revoked_at IS NULL AND expires_at>? AND EXISTS
                (SELECT 1 FROM office_memberships WHERE user_id=? AND office_id=? AND active=1)""",
                (office_id, now(), session_id, user_id, now(), user_id, office_id)).rowcount
        return bool(changed)

    def revoke_auth_session(self, session_id):
        with self.db:
            return self.db.execute("UPDATE auth_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                                   (now(), session_id)).rowcount > 0

    def update_membership_display_name(self, office_id, user_id, display_name):
        display_name = str(display_name or "").strip()
        if not display_name or len(display_name) > 160:
            raise ValueError("Nome deve ter de 1 a 160 caracteres.")
        with self.db:
            changed = self.db.execute("UPDATE office_memberships SET display_name=? WHERE office_id=? AND user_id=? AND active=1",
                                      (display_name, office_id, user_id)).rowcount
        return bool(changed)

    def office_users(self, office_id):
        return [dict(row) for row in self.db.execute("""SELECT u.id,u.email,u.active AS account_active,
            m.active,m.role,COALESCE(NULLIF(trim(m.display_name),''),u.full_name,'') AS display_name,m.created_at,
            (SELECT count(*) FROM auth_sessions s WHERE s.user_id=u.id AND s.active_office_id=m.office_id
                AND s.revoked_at IS NULL AND s.expires_at>?) AS active_sessions
            FROM office_memberships m JOIN users u ON u.id=m.user_id
            WHERE m.office_id=? ORDER BY m.display_name COLLATE NOCASE,u.email COLLATE NOCASE""",
            (now(), office_id))]

    def find_user(self, email):
        email = str(email or "").strip().casefold()
        if not re.fullmatch(r"[^\s@]{1,64}@[\w.-]{1,190}", email):
            raise ValueError("E-mail inválido.")
        row = self.db.execute("SELECT id,email,full_name,active FROM users WHERE email=? COLLATE NOCASE", (email,)).fetchone()
        return dict(row) if row else None

    def _ensure_office_membership_in_transaction(self, office_id, email, role, *, password=None, full_name="",
                                                  allow_inactive_office=False, allow_inactive_account=False):
        user = self.find_user(email)
        if role not in ("office_admin", "lawyer"):
            raise ValueError("Papel inválido.")
        if user:
            if password is not None:
                raise ValueError("Omita a senha ao vincular uma conta existente.")
            if not user["active"] and not allow_inactive_account:
                raise ValueError("A conta está desativada; não é possível vinculá-la ao escritório.")
            office = self.db.execute("SELECT active FROM offices WHERE id=?", (office_id,)).fetchone()
            if not office or (not office["active"] and not allow_inactive_office):
                raise ValueError("Escritório não encontrado ou inativo.")
            exists = self.db.execute("SELECT active,role,display_name FROM office_memberships WHERE office_id=? AND user_id=?",
                                     (office_id, user["id"])).fetchone()
            if exists and exists["active"]:
                raise ValueError("Usuário já está vinculado a este escritório.")
            display_name = (full_name or exists["display_name"]) if exists else (full_name or user["full_name"])
            if exists:
                changed = (not exists["active"] or exists["role"] != role or
                           (full_name and exists["display_name"] != full_name))
                self.db.execute("UPDATE office_memberships SET active=1,role=?,display_name=? WHERE office_id=? AND user_id=?",
                                (role, display_name, office_id, user["id"]))
                if changed:
                    self.db.execute("UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND active_office_id=? AND revoked_at IS NULL",
                                    (now(), user["id"], office_id))
            else:
                self.db.execute("""INSERT INTO office_memberships
                    (office_id,user_id,role,created_at,display_name) VALUES(?,?,?,?,?)""",
                    (office_id, user["id"], role, now(), display_name))
            return {"id": user["id"], "email": user["email"], "role": role, "created": False}
        if not password:
            raise ValueError("Para criar uma conta nova, informe uma senha temporária segura.")
        created = self._create_user_in_transaction(email, password, office_id, role, full_name,
                                                   allow_inactive_office=allow_inactive_office)
        return {**created, "created": True}

    def ensure_office_membership(self, office_id, email, role, *, password=None, full_name=""):
        require_operational_office(self.environment, office_id)
        with self.db:
            return self._ensure_office_membership_in_transaction(
                office_id, email, role, password=password, full_name=full_name)

    def ensure_platform_office_membership(self, office_id, email, role, *, password=None, full_name=""):
        """Create or link an office user without changing the office's active state."""
        require_operational_office(self.environment, office_id)
        with self.db:
            return self._ensure_office_membership_in_transaction(
                office_id, email, role, password=password, full_name=full_name,
                allow_inactive_office=True, allow_inactive_account=True)

    def office_user(self, office_id, user_id):
        return next((item for item in self.office_users(office_id) if item["id"] == user_id), None)

    def platform_office_user(self, office_id, user_id):
        account = self.db.execute("SELECT id,email,full_name,active,created_at FROM users WHERE id=?",
                                  (user_id,)).fetchone()
        membership = self.db.execute("""SELECT m.office_id,m.user_id,m.role,m.active,m.display_name,m.created_at,
            o.name AS office_name,o.active AS office_active FROM office_memberships m
            JOIN offices o ON o.id=m.office_id WHERE m.office_id=? AND m.user_id=?""",
            (office_id, user_id)).fetchone()
        if not account or not membership:
            return None
        memberships = [dict(row) for row in self.db.execute("""SELECT m.office_id,o.name AS office_name,
            o.active AS office_active,m.role,m.active,m.display_name,m.created_at
            FROM office_memberships m JOIN offices o ON o.id=m.office_id
            WHERE m.user_id=? ORDER BY o.name,o.id""", (user_id,)).fetchall()]
        return {"account": dict(account), "membership": dict(membership), "memberships": memberships}

    def update_office_membership(self, office_id, user_id, *, role=None, active=None,
                                 display_name=None, account_active=None):
        if role is not None and role not in ("office_admin", "lawyer"):
            raise ValueError("Papel inválido.")
        if display_name is not None:
            display_name = str(display_name).strip()
            if not display_name or len(display_name) > 160:
                raise ValueError("Nome deve ter de 1 a 160 caracteres.")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("""SELECT m.role,m.active,m.display_name,u.email,u.active AS account_active FROM office_memberships m
                JOIN users u ON u.id=m.user_id WHERE m.office_id=? AND m.user_id=?""",
                                  (office_id, user_id)).fetchone()
            if not row:
                raise LookupError("Vínculo não encontrado neste escritório.")
            role = row["role"] if role is None else role
            active = bool(row["active"]) if active is None else active
            display_name = row["display_name"] if display_name is None else display_name
            account_active = bool(row["account_active"]) if account_active is None else bool(account_active)
            if row["role"] == "office_admin" and row["active"] and (role != "office_admin" or not active):
                admins = self.db.execute("""SELECT count(*) FROM office_memberships m JOIN users u ON u.id=m.user_id
                    WHERE m.office_id=? AND m.role='office_admin' AND m.active=1 AND u.active=1""",
                    (office_id,)).fetchone()[0]
                if admins <= 1:
                    raise ValueError("O escritório precisa manter ao menos um administrador ativo.")
            if row["account_active"] and not account_active:
                admin_offices = self.db.execute("""SELECT office_id FROM office_memberships
                    WHERE user_id=? AND role='office_admin' AND active=1""", (user_id,)).fetchall()
                for admin_office in admin_offices:
                    remaining = self.db.execute("""SELECT count(*) FROM office_memberships m
                        JOIN users u ON u.id=m.user_id WHERE m.office_id=? AND m.user_id<>?
                        AND m.role='office_admin' AND m.active=1 AND u.active=1""",
                        (admin_office["office_id"], user_id)).fetchone()[0]
                    if remaining == 0:
                        raise ValueError("A conta é o último administrador ativo de um escritório e não pode ser desativada.")
            self.db.execute("UPDATE office_memberships SET role=?,active=?,display_name=? WHERE office_id=? AND user_id=?",
                            (role, int(bool(active)), display_name, office_id, user_id))
            self.db.execute("UPDATE users SET active=? WHERE id=?", (int(account_active), user_id))
            if not active or row["role"] != role:
                self.db.execute("UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND active_office_id=? AND revoked_at IS NULL",
                                (now(), user_id, office_id))
            if not active or (row["role"] == "office_admin" and role != "office_admin"):
                self.db.execute("""UPDATE service_credentials SET revoked_at=? WHERE org=? AND created_by=?
                    AND revoked_at IS NULL""", (now(), office_id, row["email"]))
            if row["account_active"] and not account_active:
                self.db.execute("UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                                (now(), user_id))
                self.db.execute("UPDATE service_credentials SET revoked_at=? WHERE created_by=? AND revoked_at IS NULL",
                                (now(), row["email"]))
        return True

    def remove_office_membership(self, office_id, user_id):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("""SELECT m.role,m.active,u.email FROM office_memberships m
                JOIN users u ON u.id=m.user_id WHERE m.office_id=? AND m.user_id=?""",
                                  (office_id, user_id)).fetchone()
            if not row:
                return False
            if row["role"] == "office_admin" and row["active"]:
                admins = self.db.execute("""SELECT count(*) FROM office_memberships m JOIN users u ON u.id=m.user_id
                    WHERE m.office_id=? AND m.role='office_admin' AND m.active=1 AND u.active=1""",
                    (office_id,)).fetchone()[0]
                if admins <= 1:
                    raise ValueError("O escritório precisa manter ao menos um administrador ativo.")
            self.db.execute("DELETE FROM auth_sessions WHERE user_id=? AND active_office_id=?", (user_id, office_id))
            self.db.execute("""UPDATE service_credentials SET revoked_at=? WHERE org=? AND created_by=?
                AND revoked_at IS NULL""", (now(), office_id, row["email"]))
            self.db.execute("DELETE FROM office_memberships WHERE office_id=? AND user_id=?", (office_id, user_id))
            if not self.db.execute("SELECT 1 FROM office_memberships WHERE user_id=?", (user_id,)).fetchone():
                self.db.execute("DELETE FROM password_recovery_tokens WHERE user_id=?", (user_id,))
                self.db.execute("DELETE FROM users WHERE id=?", (user_id,))
        return True

    def delete_own_account(self, user_id, password):
        if not self.verify_user_password(user_id, password):
            raise PermissionError("Senha atual inválida.")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            email = self.db.execute("SELECT email FROM users WHERE id=?", (user_id,)).fetchone()[0]
            memberships = self.db.execute("SELECT office_id,role,active FROM office_memberships WHERE user_id=?",
                                          (user_id,)).fetchall()
            for membership in memberships:
                if membership["role"] == "office_admin" and membership["active"]:
                    count = self.db.execute("""SELECT count(*) FROM office_memberships m JOIN users u ON u.id=m.user_id
                        WHERE m.office_id=? AND m.role='office_admin'
                        AND m.active=1 AND u.active=1""", (membership["office_id"],)).fetchone()[0]
                    if count <= 1:
                        raise ValueError("O escritório precisa manter ao menos um administrador ativo.")
            self.db.execute("UPDATE service_credentials SET revoked_at=? WHERE created_by=? AND revoked_at IS NULL",
                            (now(), email))
            self.db.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
            self.db.execute("DELETE FROM office_memberships WHERE user_id=?", (user_id,))
            self.db.execute("DELETE FROM password_recovery_tokens WHERE user_id=?", (user_id,))
            self.db.execute("DELETE FROM users WHERE id=?", (user_id,))
        return True

    @staticmethod
    def _check_password(password, row):
        import base64
        try:
            expected = base64.urlsafe_b64decode(row["password_hash"])
            salt = base64.urlsafe_b64decode(row["password_salt"])
            actual = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt,
                                         row["password_iterations"])
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError):
            return False

    def verify_user_password(self, user_id, password):
        row = self.db.execute("SELECT password_hash,password_salt,password_iterations FROM users WHERE id=? AND active=1",
                              (user_id,)).fetchone()
        return bool(row and self._check_password(password, row))

    def change_user_password(self, user_id, new_password, *, current_password=None, recovery_token=None):
        if len(new_password or "") < 12 or len(new_password) > 1024:
            raise ValueError("A nova senha deve ter de 12 a 1024 caracteres.")
        if current_password is not None and not self.verify_user_password(user_id, current_password):
            raise PermissionError("Senha atual inválida.")
        token_hash = hashlib.sha256(recovery_token.encode("utf-8")).hexdigest() if recovery_token is not None else None
        password_hash, salt, iterations = self._password_record(new_password)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if token_hash is not None:
                token = self.db.execute("""SELECT id FROM password_recovery_tokens WHERE user_id=? AND token_hash=?
                    AND consumed_at IS NULL AND expires_at>?""", (user_id, token_hash, now())).fetchone()
                if not token:
                    raise PermissionError("Token inválido ou expirado.")
                self.db.execute("UPDATE password_recovery_tokens SET consumed_at=? WHERE id=?", (now(), token["id"]))
            self.db.execute("UPDATE users SET password_hash=?,password_salt=?,password_iterations=? WHERE id=?",
                            (password_hash, salt, iterations, user_id))
            self.db.execute("UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                            (now(), user_id))
            self.db.execute("UPDATE password_recovery_tokens SET consumed_at=? WHERE user_id=? AND consumed_at IS NULL",
                            (now(), user_id))

    def _create_password_recovery_token_in_transaction(self, user_id, ttl_minutes):
        raw = secrets.token_urlsafe(32)
        token_id = uuid.uuid4().hex
        stamp = datetime.now(timezone.utc)
        expires_at = stamp + timedelta(minutes=ttl_minutes)
        self.db.execute("UPDATE password_recovery_tokens SET consumed_at=? WHERE user_id=? AND consumed_at IS NULL",
                        (stamp.isoformat(), user_id))
        self.db.execute("""INSERT INTO password_recovery_tokens
            (id,user_id,token_hash,created_at,expires_at) VALUES(?,?,?,?,?)""",
            (token_id, user_id, hashlib.sha256(raw.encode()).hexdigest(), stamp.isoformat(),
             expires_at.isoformat()))
        return raw, expires_at.isoformat()

    def create_password_recovery_token(self, user_id, *, ttl_minutes=30):
        with self.db:
            raw, _ = self._create_password_recovery_token_in_transaction(user_id, ttl_minutes)
        return raw

    def issue_office_password_recovery_token(self, user_id, office_id, *, actor, ttl_minutes=30):
        """Issue a one-use recovery token only for an active account in the admin's office."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            target = self.db.execute("""SELECT u.id FROM users u
                JOIN office_memberships m ON m.user_id=u.id
                JOIN offices o ON o.id=m.office_id
                WHERE u.id=? AND u.active=1 AND m.office_id=? AND m.active=1 AND o.active=1""",
                (user_id, office_id)).fetchone()
            if not target:
                raise LookupError("Conta ativa não encontrada neste escritório.")
            token, expires_at = self._create_password_recovery_token_in_transaction(user_id, ttl_minutes)
            self._record_access_in_transaction(
                "password_recovery_token_issued", None,
                f"token de uso único emitido para user_id={user_id}; validade de {ttl_minutes} minutos",
                actor=str(actor)[:160], org=office_id)
        return {"token": token, "expires_at": expires_at}

    def _movement_timeline(self, org, number):
        from .consolidation import consolidate
        snapshot = self.latest_court_snapshot(org, number, "PJe/TRF1")
        snapshots = [dict(snapshot["data"], collected_at=snapshot["collected_at"])] if snapshot else []
        if len(number) == 20 and number[13:16] == "404":
            assisted = self.latest_court_snapshot(org, number, "TRF4 — importação assistida")
            if assisted:
                snapshots.append(dict(assisted["data"], collected_at=assisted["collected_at"]))
        return [item for item in consolidate(number, self.timeline(org, number),
                                             self.communications(org, number), self, org,
                                             court_snapshots=snapshots)
                if item.get("tipo") == "movimentacao"]

    def _member_first_seen(self, org, number, source_key):
        source, _, original_id = source_key.partition(":")
        if source == "datajud":
            row = self.db.execute("""SELECT first_seen FROM movements
                WHERE org=? AND number=? AND fingerprint=?""", (org, number, original_id)).fetchone()
            return row[0] if row else None
        if source == "pje":
            rows = self.db.execute("""SELECT collected_at,payload FROM raw_records
                WHERE org=? AND number=? AND source='PJe/TRF1' ORDER BY collected_at""", (org, number))
            for row in rows:
                try:
                    payload = json.loads(row["payload"])
                except (ValueError, TypeError):
                    continue
                if any(str(item.get("source_id") or "unknown") == original_id
                       for item in payload.get("movements", []) if isinstance(item, dict)):
                    return row["collected_at"]
        if source == "trf4-assist":
            rows = self.db.execute("""SELECT collected_at,payload FROM raw_records
                WHERE org=? AND number=? AND source='TRF4 — importação assistida' ORDER BY collected_at""",
                (org, number))
            for row in rows:
                try:
                    payload = json.loads(row["payload"])
                except (ValueError, TypeError):
                    continue
                if any(str(item.get("source_id") or "") == original_id
                       for item in payload.get("movements", []) if isinstance(item, dict)):
                    return row["collected_at"]
        return None

    @staticmethod
    def _event_source_keys(event):
        keys = event.get("source_event_ids") or [event.get("id")]
        return sorted({str(key) for key in keys if key and str(key).startswith(("datajud:", "pje:", "trf4-assist:"))})

    def _sync_logical_event_states(self, org, number, events, *, baseline=False,
                                   new_source_keys=(), baseline_source_keys=(), split_baseline=True):
        """Attach current G.3 groups to stable source-member identities, without changing G.3."""
        activation = self.db.execute("SELECT value FROM i2_state_meta WHERE key='activated_at'").fetchone()
        activated_at = activation[0] if activation else now()
        new_keys = set(new_source_keys)
        historical_keys = set(baseline_source_keys)
        assigned = set()
        for event in events:
            keys = self._event_source_keys(event)
            if not keys:
                continue
            placeholders = ",".join("?" for _ in keys)
            found = self.db.execute(f"""SELECT DISTINCT s.* FROM logical_event_members m
                JOIN logical_event_states s USING(org,number,logical_id)
                WHERE m.org=? AND m.number=? AND m.source_event_key IN ({placeholders})
                  AND s.merged_into IS NULL""", (org, number, *keys)).fetchall()
            states = {row["logical_id"]: dict(row) for row in found}
            if states:
                logical_id = min(states, key=lambda key: (states[key]["created_at"], key))
                state = states[logical_id]
                if len(states) > 1:
                    eligible_states = [item for item in states.values() if item["eligible"]]
                    all_seen = all(item["read_at"] is not None or not item["eligible"]
                                   for item in states.values())
                    read_at = max((item["read_at"] for item in states.values() if item["read_at"]), default=None) if all_seen else None
                    self.db.execute("""UPDATE logical_event_states SET eligible=?,read_at=?,read_by=?,
                        updated_at=? WHERE org=? AND number=? AND logical_id=?""",
                        (int(bool(eligible_states)), read_at, next((x["read_by"] for x in states.values() if x["read_by"]), None),
                         now(), org, number, logical_id))
                    for old_id in states:
                        if old_id == logical_id:
                            continue
                        self.db.execute("""UPDATE logical_event_members SET logical_id=?
                            WHERE org=? AND number=? AND logical_id=?""", (logical_id, org, number, old_id))
                        self.db.execute("""UPDATE logical_event_states SET merged_into=?,updated_at=?
                            WHERE org=? AND number=? AND logical_id=?""", (logical_id, now(), org, number, old_id))
            else:
                logical_id = "logical:" + hashlib.sha256((org + "\n" + number + "\n" + keys[0]).encode()).hexdigest()
                observed = [self._member_first_seen(org, number, key) for key in keys]
                known_times = [value for value in observed if value]
                first_seen_at = min(known_times) if known_times else None
                is_new = bool(not baseline and not historical_keys.intersection(keys) and
                              (new_keys.intersection(keys) or (first_seen_at and first_seen_at >= activated_at)))
                stamp = now()
                self.db.execute("""INSERT OR IGNORE INTO logical_event_states
                    (org,number,logical_id,anchor_key,first_seen_at,baseline,eligible,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (org, number, logical_id, keys[0], first_seen_at, int(baseline or not is_new),
                     int(is_new), stamp, stamp))
            if logical_id in assigned or (states and state["anchor_key"] not in keys):
                # A former G.3 group split. Keep its read state with the first
                # deterministic child only; a sibling becomes a non-alerting
                # baseline identity instead of inheriting a read decision.
                split_key = keys[0]
                logical_id = "logical:" + hashlib.sha256((org + "\n" + number + "\n" + split_key + "\nsplit").encode()).hexdigest()
                observed = [self._member_first_seen(org, number, key) for key in keys]
                known_times = [value for value in observed if value]
                stamp = now()
                self.db.execute("""INSERT OR IGNORE INTO logical_event_states
                    (org,number,logical_id,anchor_key,first_seen_at,baseline,eligible,created_at,updated_at)
                    VALUES(?,?,?,?,?,1,0,?,?)""",
                    (org, number, logical_id, split_key, min(known_times) if known_times else None, stamp, stamp))
            assigned.add(logical_id)
            state = self.db.execute("SELECT logical_id FROM logical_event_states WHERE org=? AND number=? AND logical_id=?",
                                    (org, number, logical_id)).fetchone()
            for key in keys:
                source = key.split(":", 1)[0]
                first_seen = self._member_first_seen(org, number, key)
                self.db.execute("""INSERT INTO logical_event_members
                    (org,number,source_event_key,logical_id,source,first_seen_at) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(org,number,source_event_key) DO UPDATE SET logical_id=excluded.logical_id,
                    source=excluded.source,first_seen_at=COALESCE(logical_event_members.first_seen_at,excluded.first_seen_at)""",
                    (org, number, key, logical_id, source, first_seen))
            if state:
                self.db.execute("""UPDATE logical_event_states SET first_seen_at=COALESCE(first_seen_at,?),
                    updated_at=? WHERE org=? AND number=? AND logical_id=?""",
                    (min([self._member_first_seen(org, number, key) for key in keys if self._member_first_seen(org, number, key)] or [None])
                     if any(self._member_first_seen(org, number, key) for key in keys) else None,
                     now(), org, number, logical_id))

    def _ensure_i2_baseline(self):
        marker = self.db.execute("SELECT value FROM i2_state_meta WHERE key='baseline_complete'").fetchone()
        if marker:
            return
        activated_at = now()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("INSERT OR IGNORE INTO i2_state_meta(key,value) VALUES('activated_at',?)", (activated_at,))
            for row in self.db.execute("SELECT org,number FROM watches").fetchall():
                self._sync_logical_event_states(row["org"], row["number"],
                                                self._movement_timeline(row["org"], row["number"]), baseline=True)
            self.db.execute("INSERT OR REPLACE INTO i2_state_meta(key,value) VALUES('baseline_complete',?)", (now(),))

    def sync_read_state(self, org, number, *, baseline=False, new_source_keys=()):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._sync_logical_event_states(org, number, self._movement_timeline(org, number),
                                            baseline=baseline, new_source_keys=new_source_keys)

    def decorate_read_state(self, org, number, events):
        result = []
        for original in events:
            event = dict(original)
            keys = self._event_source_keys(event)
            rows = []
            if keys:
                placeholders = ",".join("?" for _ in keys)
                rows = self.db.execute(f"""SELECT s.* FROM logical_event_members m
                    JOIN logical_event_states s USING(org,number,logical_id)
                    WHERE m.org=? AND m.number=? AND m.source_event_key IN ({placeholders})
                      AND s.merged_into IS NULL ORDER BY s.created_at,s.logical_id""",
                    (org, number, *keys)).fetchall()
            state = dict(rows[0]) if rows else None
            event.update({"logical_event_id": state["logical_id"] if state else None,
                          "first_seen_at": state["first_seen_at"] if state else None,
                          "read_at": state["read_at"] if state else None,
                          "read_by": state["read_by"] if state else None,
                          "is_unread": bool(state and state["eligible"] and not state["read_at"]),
                          "read_status": ("unread" if state and state["eligible"] and not state["read_at"]
                                          else "read" if state and state["read_at"]
                                          else "historical" if state and state["baseline"]
                                          else "unknown")})
            result.append(event)
        return result

    def current_movement_events(self, org, number):
        self.get(org, number)
        return self.decorate_read_state(org, number, self._movement_timeline(org, number))

    def unread_summary(self, org, number):
        events = self.current_movement_events(org, number)
        unread = [event for event in events if event["is_unread"]]
        return {"unread_count": len(unread), "total_count": len(events)}

    def mark_logical_event_read(self, org, number, logical_id, actor="local-authenticated-user"):
        stamp = now()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            changed = self.db.execute("""UPDATE logical_event_states SET read_at=?,read_by=?,updated_at=?
                WHERE org=? AND number=? AND logical_id=? AND read_at IS NULL AND merged_into IS NULL""",
                (stamp, actor, stamp, org, number, logical_id)).rowcount
        return bool(changed)

    def mark_process_unread_read(self, org, number, actor="local-authenticated-user"):
        stamp = now()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            return self.db.execute("""UPDATE logical_event_states SET read_at=?,read_by=?,updated_at=?
                WHERE org=? AND number=? AND eligible=1 AND read_at IS NULL AND merged_into IS NULL""",
                (stamp, actor, stamp, org, number)).rowcount

    def enqueue_collection_group(self, org, number, collection_type, *, priority=100,
                                 include_datajud=True, include_djen=True,
                                 start=None, end=None, group_id=None, max_attempts=5,
                                 additional_sources=(), job_parameters=None):
        """Persist a source-ordered collection group; active jobs are deduplicated."""
        require_operational_office(self.environment, org)
        self.get(org, number)
        group_id = group_id or uuid.uuid4().hex
        stamp = now()
        payload = {"start": start, "end": end}
        if job_parameters:
            payload.update(job_parameters)
        params = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        jobs = []
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            active_group = self.db.execute("""SELECT group_id FROM collection_jobs
                WHERE org=? AND number=? AND collection_type=? AND params=?
                  AND status IN ('queued','running') ORDER BY id DESC LIMIT 1""",
                (org, number, collection_type, params)).fetchone()
            if active_group:
                return {"group_id": active_group[0],
                        "jobs": self.collection_jobs(org, number, group_id=active_group[0])}
            sequence = []
            unique_sources = list(dict.fromkeys(additional_sources))
            if "TRF4-EPROC-DIRECT" in unique_sources:
                sequence.append(("TRF4-EPROC-DIRECT", 5))
            if include_datajud:
                sequence.append(("DataJud", 10))
            for source in unique_sources:
                if source not in ("TRF1-PJE-1G", "TRF1-PJE-JE", "TRF4-EPROC-DIRECT"):
                    raise ValueError("Fonte judicial adicional não registrada.")
                if source != "TRF4-EPROC-DIRECT":
                    sequence.append((source, 15))
            if include_djen:
                sequence.append(("DJEN", 20))
            sequence.append(("Consolidation", 30))
            for source, order in sequence:
                self.db.execute("""INSERT OR IGNORE INTO collection_jobs
                    (org,number,group_id,source,collection_type,stage_order,priority,status,
                     attempts,max_attempts,scheduled_at,created_at,updated_at,params)
                    VALUES(?,?,?,?,?,?,?,'queued',0,?,?,?, ?,?)""",
                    (org, number, group_id, source, collection_type, order, priority,
                     max_attempts, stamp, stamp, stamp, params))
                row = self.db.execute("""SELECT id,group_id,source,status FROM collection_jobs
                    WHERE org=? AND number=? AND source=? AND collection_type=? AND group_id=?
                      AND status IN ('queued','running')""",
                    (org, number, source, collection_type, group_id)).fetchone()
                if row:
                    jobs.append(dict(row))
        return {"group_id": group_id, "jobs": jobs}

    def monitor_schedule(self, org, number):
        self.get(org, number)
        row = self.db.execute("SELECT * FROM monitor_schedules WHERE org=? AND number=?",
                              (org, number)).fetchone()
        if row:
            result = dict(row)
        else:
            result = {"org": org, "number": number, "enabled": 0, "interval_hours": 24,
                      "last_scheduled_at": None, "last_execution_at": None, "next_run_at": None,
                      "status": "paused", "last_error": None, "last_group_id": None,
                      "updated_at": None}
        result["enabled"] = bool(result["enabled"])
        return result

    def configure_monitor_schedule(self, org, number, *, enabled, interval_hours):
        if not isinstance(enabled, bool):
            raise ValueError("enabled deve ser verdadeiro ou falso.")
        if interval_hours not in (6, 12, 24, 48):
            raise ValueError("A frequência deve ser 6, 12, 24 ou 48 horas.")
        self.get(org, number)
        stamp_dt = datetime.now(timezone.utc)
        stamp = stamp_dt.isoformat()
        next_run = (stamp_dt + timedelta(hours=interval_hours)).isoformat() if enabled else None
        status = "active" if enabled else "paused"
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("""INSERT INTO monitor_schedules
                (org,number,enabled,interval_hours,next_run_at,status,updated_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(org,number) DO UPDATE SET
                enabled=excluded.enabled,interval_hours=excluded.interval_hours,
                next_run_at=excluded.next_run_at,status=excluded.status,
                last_error=NULL,updated_at=excluded.updated_at""",
                (org, number, int(enabled), interval_hours, next_run, status, stamp))
        return self.monitor_schedule(org, number)

    def enqueue_due_monitor_schedules(self, org="local", *, now_at=None, limit=10,
                                      max_attempts=5):
        if org is not None:
            require_operational_office(self.environment, org)
        """Atomically enqueue at most one catch-up cycle per due process.

        Missed intervals are coalesced into one run; next_run_at is based on now,
        so a long shutdown never creates a burst of stale requests.
        """
        stamp_dt = now_at or datetime.now(timezone.utc)
        if stamp_dt.tzinfo is None:
            stamp_dt = stamp_dt.replace(tzinfo=timezone.utc)
        stamp = stamp_dt.astimezone(timezone.utc).isoformat()
        results = []
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            sql = """SELECT s.*,w.metadata FROM monitor_schedules s
                JOIN watches w USING(org,number) JOIN offices o ON o.id=s.org AND o.active=1
                WHERE s.enabled=1 AND s.next_run_at IS NOT NULL AND s.next_run_at<=?"""
            params = [stamp]
            if org is not None:
                sql += " AND s.org=?"
                params.append(org)
            sql += " ORDER BY s.next_run_at,s.org,s.number LIMIT ?"
            params.append(max(1, limit))
            rows = self.db.execute(sql, params).fetchall()
            for row in rows:
                row_org = row["org"]
                interval = int(row["interval_hours"])
                next_run = (stamp_dt.astimezone(timezone.utc) + timedelta(hours=interval)).isoformat()
                active = self.db.execute("""SELECT group_id FROM collection_jobs
                    WHERE org=? AND number=? AND status IN ('queued','running')
                    ORDER BY id DESC LIMIT 1""", (row_org, row["number"])).fetchone()
                if active:
                    self.db.execute("""UPDATE monitor_schedules SET last_scheduled_at=?,
                        next_run_at=?,status='coalesced',last_group_id=?,last_error=NULL,updated_at=?
                        WHERE org=? AND number=?""",
                        (stamp, next_run, active["group_id"], stamp, row_org, row["number"]))
                    results.append({"number": row["number"], "group_id": active["group_id"],
                                   "status": "coalesced", "jobs_created": 0})
                    continue
                group_id = "schedule-" + uuid.uuid4().hex
                params = json.dumps({"start": None, "end": None, "scheduled": True}, ensure_ascii=False)
                from .routing import route_process
                metadata = json.loads(row["metadata"] or "[]")
                routing = route_process(row["number"], metadata)
                sequence = ([ ("DataJud", 10) ] if routing.get("datajud_alias") else [])
                sequence.extend((("DJEN", 20), ("Consolidation", 30)))
                for source, order in sequence:
                    self.db.execute("""INSERT INTO collection_jobs
                        (org,number,group_id,source,collection_type,stage_order,priority,status,
                         attempts,max_attempts,scheduled_at,created_at,updated_at,params)
                        VALUES(?,?,?,?, 'scheduled_refresh', ?,100,'queued',0,?,?,?,?,?)""",
                        (row_org, row["number"], group_id, source, order, max_attempts,
                         stamp, stamp, stamp, params))
                self.db.execute("""UPDATE monitor_schedules SET last_scheduled_at=?,
                    next_run_at=?,status='queued',last_group_id=?,last_error=NULL,updated_at=?
                    WHERE org=? AND number=?""",
                    (stamp, next_run, group_id, stamp, row_org, row["number"]))
                results.append({"number": row["number"], "group_id": group_id,
                                "status": "queued", "jobs_created": len(sequence)})
        return results

    def scheduled_job_group(self, org, number):
        self.get(org, number)
        row = self.db.execute("""SELECT last_group_id FROM monitor_schedules
            WHERE org=? AND number=?""", (org, number)).fetchone()
        if not row or not row[0]:
            return []
        return self.collection_jobs(org, number, group_id=row[0])

    def _update_monitor_schedule_for_job(self, job, *, started=False):
        if job.get("collection_type") != "scheduled_refresh":
            return
        stamp = now()
        if started:
            self.db.execute("""UPDATE monitor_schedules SET status='running',last_execution_at=?,
                updated_at=? WHERE org=? AND number=? AND last_group_id=? AND enabled=1""",
                (stamp, stamp, job["org"], job["number"], job["group_id"]))
            return
        schedule = self.db.execute("""SELECT enabled FROM monitor_schedules
            WHERE org=? AND number=? AND last_group_id=?""",
            (job["org"], job["number"], job["group_id"])).fetchone()
        if not schedule or not schedule["enabled"]:
            return
        rows = self.db.execute("""SELECT status,error FROM collection_jobs
            WHERE org=? AND number=? AND group_id=?""",
            (job["org"], job["number"], job["group_id"])).fetchall()
        states = {row["status"] for row in rows}
        if "running" in states:
            status = "running"
        elif "queued" in states:
            status = "queued"
        elif "awaiting_access" in states:
            status = "awaiting_access"
        elif "failed" in states:
            status = "failed"
        elif "partial" in states:
            status = "partial"
        else:
            status = "completed"
        error = next((row["error"] for row in rows if row["error"]), None)
        self.db.execute("""UPDATE monitor_schedules SET status=?,last_error=?,updated_at=?
            WHERE org=? AND number=? AND last_group_id=?""",
            (status, error, stamp, job["org"], job["number"], job["group_id"]))

    def enqueue_group_source(self, org, number, group_id, source, *, priority=50, max_attempts=5):
        """Add a routed connector after DataJud has supplied the system and degree."""
        if source not in ("TRF1-PJE-1G", "TRF1-PJE-JE"):
            raise ValueError("Fonte judicial adicional não registrada.")
        self.get(org, number)
        stamp = now()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            group = self.db.execute("""SELECT collection_type,params,priority,max_attempts
                FROM collection_jobs WHERE org=? AND number=? AND group_id=? ORDER BY id LIMIT 1""",
                (org, number, group_id)).fetchone()
            if not group:
                return None
            existing = self.db.execute("""SELECT id FROM collection_jobs
                WHERE org=? AND number=? AND group_id=? AND source=? LIMIT 1""",
                (org, number, group_id, source)).fetchone()
            if existing:
                return dict(self.db.execute("SELECT id,group_id,source,status FROM collection_jobs WHERE id=?",
                                            (existing[0],)).fetchone())
            cursor = self.db.execute("""INSERT INTO collection_jobs
                (org,number,group_id,source,collection_type,stage_order,priority,status,attempts,
                 max_attempts,scheduled_at,created_at,updated_at,params)
                VALUES(?,?,?,?,?,15,?,'queued',0,?,?,?,?,?)""",
                (org, number, group_id, source, group[0], priority, max_attempts,
                 stamp, stamp, stamp, group[1]))
            return dict(self.db.execute("SELECT id,group_id,source,status FROM collection_jobs WHERE id=?",
                                        (cursor.lastrowid,)).fetchone())

    def collection_jobs(self, org, number, *, group_id=None):
        self.get(org, number)
        sql = "SELECT * FROM collection_jobs WHERE org=? AND number=?"
        params = [org, number]
        if group_id:
            sql += " AND group_id=?"
            params.append(group_id)
        sql += " ORDER BY created_at DESC,group_id,stage_order,id"
        return [dict(row) for row in self.db.execute(sql, params)]

    def latest_collection_group(self, org, number):
        self.get(org, number)
        row = self.db.execute("""SELECT group_id FROM collection_jobs WHERE org=? AND number=?
            ORDER BY id DESC LIMIT 1""", (org, number)).fetchone()
        return self.collection_jobs(org, number, group_id=row[0]) if row else []

    def active_collection_group(self, org, number):
        self.get(org, number)
        row = self.db.execute("""SELECT group_id FROM collection_jobs WHERE org=? AND number=?
            AND status IN ('queued','running') ORDER BY id DESC LIMIT 1""", (org, number)).fetchone()
        return self.collection_jobs(org, number, group_id=row[0]) if row else []

    def recover_expired_jobs(self, org=None):
        """Recover expired leases without relying on process-local locks."""
        stamp = datetime.now(timezone.utc)
        sql = "SELECT * FROM collection_jobs WHERE status='running' AND lease_until<=?"
        params = [stamp.isoformat()]
        if org is not None:
            require_operational_office(self.environment, org)
            sql += " AND org=?"
            params.append(org)
        elif self.environment == "production":
            sql += " AND org<>'local'"
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            rows = [dict(row) for row in self.db.execute(sql, params)]
            for row in rows:
                verification = self.db.execute("""SELECT id FROM human_verifications
                    WHERE job_id=? AND status IN ('queued','aguardando_verificacao_humana')
                    ORDER BY created_at DESC LIMIT 1""", (row["id"],)).fetchone()
                if verification:
                    self.db.execute("""UPDATE human_verifications SET status='expired',updated_at=?,
                        completed_at=?,error_code='worker_interrupted' WHERE id=?""",
                        (stamp.isoformat(), stamp.isoformat(), verification[0]))
                    error = json.dumps({"code": "human_session_expired", "message":
                        "A sessão assistida terminou com a interrupção do worker; solicite nova retomada.",
                        "retryable": False}, ensure_ascii=False)
                    self.db.execute("""UPDATE collection_jobs SET status='awaiting_access',updated_at=?,
                        lease_until=NULL,lease_token=NULL,lease_owner=NULL,error=?,completed_at=?
                        WHERE id=? AND status='running'""",
                        (stamp.isoformat(), error, stamp.isoformat(), row["id"]))
                    self._update_monitor_schedule_for_job(row)
                    continue
                terminal = row["attempts"] >= row["max_attempts"]
                status = "failed" if terminal else "queued"
                error = json.dumps({"code": "lease_expired", "message": "Worker interrompido; lease expirou.",
                                    "retryable": not terminal}, ensure_ascii=False)
                self.db.execute("""UPDATE collection_jobs SET status=?,scheduled_at=?,updated_at=?,
                    lease_until=NULL,lease_token=NULL,lease_owner=NULL,error=?,completed_at=? WHERE id=? AND status='running'""",
                    (status, stamp.isoformat(), stamp.isoformat(), error,
                     stamp.isoformat() if terminal else None, row["id"]))
                self._update_monitor_schedule_for_job(row)
        return len(rows)

    def create_human_verification(self, org, number, job_id, *, ttl_seconds=180):
        """Persist an attended-browser request without storing cookies or browser state."""
        verification_id = uuid.uuid4().hex
        stamp = datetime.now(timezone.utc)
        expiry = stamp + timedelta(seconds=max(30, ttl_seconds))
        with self.db:
            self.db.execute("""INSERT INTO human_verifications
                (id,org,number,job_id,status,created_at,updated_at,expires_at)
                VALUES(?,?,?,?,'queued',?,?,?)""",
                (verification_id, org, number, job_id, stamp.isoformat(), stamp.isoformat(), expiry.isoformat()))
            row = self.db.execute("SELECT params FROM collection_jobs WHERE id=? AND org=? AND number=?",
                                  (job_id, org, number)).fetchone()
            if not row:
                raise ValueError("Job de coleta não encontrado.")
            params = json.loads(row[0] or "{}")
            params.update({"human_assist": True, "human_verification_id": verification_id,
                           "human_verification_ttl": max(30, ttl_seconds)})
            self.db.execute("UPDATE collection_jobs SET status='queued',scheduled_at=?,updated_at=?,"
                            "completed_at=NULL,lease_until=NULL,lease_token=NULL,lease_owner=NULL,error=NULL,params=? WHERE id=?",
                            (stamp.isoformat(), stamp.isoformat(), json.dumps(params), job_id))
        return verification_id

    def human_verification(self, verification_id):
        row = self.db.execute("SELECT * FROM human_verifications WHERE id=?", (verification_id,)).fetchone()
        return dict(row) if row else None

    def latest_human_verification(self, org, number):
        row = self.db.execute("""SELECT * FROM human_verifications WHERE org=? AND number=?
            ORDER BY created_at DESC LIMIT 1""", (org, number)).fetchone()
        return dict(row) if row else None

    def expire_human_verifications(self, org, number=None):
        stamp = datetime.now(timezone.utc)
        where = "status IN ('queued','aguardando_verificacao_humana') AND expires_at<=?"
        params = [stamp.isoformat()]
        if org is not None:
            require_operational_office(self.environment, org)
            where = "org=? AND " + where
            params.insert(0, org)
        elif self.environment == "production":
            where = "org<>'local' AND " + where
        if number is not None:
            where += " AND number=?"
            params.append(number)
        with self.db:
            rows = list(self.db.execute("""SELECT id,job_id FROM human_verifications
                WHERE """ + where, params))
            for row in rows:
                self.db.execute("""UPDATE human_verifications SET status='expired',updated_at=?,
                    completed_at=?,error_code='verification_timeout' WHERE id=?""",
                    (stamp.isoformat(), stamp.isoformat(), row["id"]))
                self.db.execute("""UPDATE collection_jobs SET status='awaiting_access',updated_at=?,completed_at=?,
                    lease_until=NULL,lease_token=NULL,lease_owner=NULL,error=?
                    WHERE id=? AND status IN ('queued','running')""",
                    (stamp.isoformat(), stamp.isoformat(), json.dumps({"code":"human_verification_expired",
                     "message":"A solicitação de retomada expirou; solicite uma nova.","retryable":False}), row["job_id"]))
        return len(rows)

    def update_human_verification(self, verification_id, status, *, worker_id=None, error_code=None):
        allowed = {"queued", "aguardando_verificacao_humana", "accepted", "expired", "cancelled", "failed"}
        if status not in allowed:
            raise ValueError("Estado de verificação humana inválido.")
        stamp = datetime.now(timezone.utc)
        terminal = status in {"accepted", "expired", "cancelled", "failed"}
        with self.db:
            return self.db.execute("""UPDATE human_verifications SET status=?,updated_at=?,
                completed_at=CASE WHEN ? THEN ? ELSE completed_at END,
                worker_id=COALESCE(?,worker_id),error_code=? WHERE id=?
                AND status IN ('queued','aguardando_verificacao_humana')""",
                (status, stamp.isoformat(), int(terminal), stamp.isoformat(), worker_id, error_code,
                 verification_id)).rowcount > 0

    def cancel_human_verification(self, verification_id):
        row = self.human_verification(verification_id)
        if not row or row["status"] not in ("queued", "aguardando_verificacao_humana"):
            return False
        stamp = now()
        with self.db:
            self.db.execute("UPDATE human_verifications SET status='cancelled',updated_at=?,completed_at=? WHERE id=?",
                            (stamp, stamp, verification_id))
            self.db.execute("""UPDATE collection_jobs SET status='awaiting_access',updated_at=?,completed_at=?,
                lease_until=NULL,lease_token=NULL,lease_owner=NULL,error=? WHERE id=? AND status IN ('queued','running')""",
                (stamp, stamp, json.dumps({"code":"human_verification_cancelled","message":"Retomada cancelada pelo usuário.","retryable":False}),row["job_id"]))
        return True

    def claim_collection_job(self, org, worker_id, *, lease_seconds=1800):
        """Atomically acquire the next eligible stage with a renewable lease token."""
        if org is not None:
            require_operational_office(self.environment, org)
        self.expire_human_verifications(org)
        self.recover_expired_jobs()
        stamp = datetime.now(timezone.utc)
        lease_until = (stamp + timedelta(seconds=lease_seconds)).isoformat()
        token = uuid.uuid4().hex
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            sql = """SELECT j.* FROM collection_jobs j
                JOIN offices o ON o.id=j.org AND o.active=1
                WHERE j.status='queued' AND j.scheduled_at<=?
                  AND NOT EXISTS (SELECT 1 FROM collection_jobs global_running
                    WHERE global_running.status='running')
                  AND NOT EXISTS (SELECT 1 FROM collection_jobs running
                    WHERE running.org=j.org AND running.number=j.number AND running.status='running')
                  AND (j.source NOT IN ('TRF1-PJE-1G','TRF1-PJE-JE') OR NOT EXISTS
                    (SELECT 1 FROM collection_jobs court_running
                     WHERE court_running.source IN ('TRF1-PJE-1G','TRF1-PJE-JE')
                     AND court_running.status='running'))
                  AND NOT EXISTS (SELECT 1 FROM collection_jobs earlier
                    WHERE earlier.org=j.org AND earlier.number=j.number AND earlier.group_id=j.group_id
                      AND earlier.stage_order<j.stage_order
                      AND earlier.status IN ('queued','running'))"""
            params = [stamp.isoformat()]
            if org is not None:
                sql += " AND j.org=?"
                params.append(org)
            if self.environment == "production":
                sql += " AND j.org<>'local'"
            sql += " ORDER BY j.priority,j.scheduled_at,j.id LIMIT 1"
            row = self.db.execute(sql, params).fetchone()
            if not row:
                return None
            execution_id = uuid.uuid4().hex
            changed = self.db.execute("""UPDATE collection_jobs SET status='running',attempts=attempts+1,
                started_at=?,updated_at=?,lease_until=?,lease_token=?,execution_id=?,lease_owner=?
                WHERE id=? AND status='queued'""",
                (stamp.isoformat(), stamp.isoformat(), lease_until, token, execution_id, worker_id, row["id"])).rowcount
            if not changed:
                return None
            self._update_monitor_schedule_for_job(dict(row), started=True)
            claimed = self.db.execute("SELECT * FROM collection_jobs WHERE id=?", (row["id"],)).fetchone()
            return dict(claimed)

    def finish_collection_job(self, job_id, lease_token, *, status, result=None, coverage=None,
                              source_execution_id=None, error=None, params=None):
        if status not in ("completed", "partial", "failed", "awaiting_access"):
            raise ValueError("Estado terminal de job inválido.")
        stamp = now()
        with self.db:
            changed = self.db.execute("""UPDATE collection_jobs SET status=?,updated_at=?,completed_at=?,
                lease_until=NULL,lease_token=NULL,lease_owner=NULL,result=?,coverage=?,source_execution_id=?,error=?,params=COALESCE(?,params)
                WHERE id=? AND status='running' AND lease_token=?""",
                (status, stamp, stamp, json.dumps(result, ensure_ascii=False) if result is not None else None,
                 json.dumps(coverage, ensure_ascii=False) if coverage is not None else None,
                 source_execution_id,
                 json.dumps(error, ensure_ascii=False) if error is not None else None,
                 json.dumps(params, ensure_ascii=False, separators=(",", ":")) if params is not None else None,
                 job_id, lease_token)).rowcount
            if changed:
                job = self.db.execute("SELECT * FROM collection_jobs WHERE id=?", (job_id,)).fetchone()
                self._update_monitor_schedule_for_job(dict(job))
        return bool(changed)

    def source_rate_wait(self, source, minimum_interval):
        """Persistent per-database request slot; returns seconds to wait (0 reserves now)."""
        stamp = datetime.now(timezone.utc)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT next_allowed_at FROM source_rate_limits WHERE source=?",
                                  (source,)).fetchone()
            next_at = datetime.fromisoformat(row[0].replace("Z", "+00:00")) if row else None
            if next_at and next_at > stamp:
                return (next_at - stamp).total_seconds()
            allowed = (stamp + timedelta(seconds=max(0, minimum_interval))).isoformat()
            self.db.execute("""INSERT INTO source_rate_limits(source,next_allowed_at,updated_at)
                VALUES(?,?,?) ON CONFLICT(source) DO UPDATE SET
                next_allowed_at=excluded.next_allowed_at,updated_at=excluded.updated_at""",
                (source, allowed, stamp.isoformat()))
            return 0

    def retry_collection_job(self, job, *, code, message, retryable, result=None, coverage=None,
                             retry_base_seconds=5, retry_max_seconds=900):
        stamp = datetime.now(timezone.utc)
        attempts = job["attempts"]
        terminal = not retryable or attempts >= job["max_attempts"]
        status = "failed" if terminal else "queued"
        delay = min(retry_max_seconds,
                    retry_base_seconds * (2 ** max(attempts - 1, 0)) * random.uniform(0.8, 1.2))
        scheduled = stamp if terminal else stamp + timedelta(seconds=delay)
        error = {"code": code, "message": message[:500], "retryable": bool(retryable)}
        with self.db:
            changed = self.db.execute("""UPDATE collection_jobs SET status=?,scheduled_at=?,updated_at=?,
                completed_at=?,lease_until=NULL,lease_token=NULL,lease_owner=NULL,error=?,result=?,coverage=?
                WHERE id=? AND status='running' AND lease_token=?""",
                (status, scheduled.isoformat(), stamp.isoformat(), stamp.isoformat() if terminal else None,
                 json.dumps(error, ensure_ascii=False),
                 json.dumps(result, ensure_ascii=False) if result is not None else None,
                 json.dumps(coverage, ensure_ascii=False) if coverage is not None else None,
                 job["id"], job["lease_token"])).rowcount
            if changed:
                self._update_monitor_schedule_for_job(job)
        return bool(changed), status, scheduled.isoformat()

    def retry_failed_collection_group(self, job_id):
        """Replay a terminal failed group as new rows, retaining the original history."""
        stamp = now()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            job = self.db.execute("SELECT * FROM collection_jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                raise LookupError("Job de coleta não encontrado.")
            if job["status"] != "failed":
                raise ValueError("Somente jobs com falha terminal podem ser reenfileirados.")

            group_rows = [dict(row) for row in self.db.execute(
                "SELECT * FROM collection_jobs WHERE org=? AND number=? AND group_id=? ORDER BY stage_order,id",
                (job["org"], job["number"], job["group_id"]))]
            active_rows = [dict(row) for row in self.db.execute(
                "SELECT * FROM collection_jobs WHERE org=? AND number=? AND status IN ('queued','running')",
                (job["org"], job["number"]))]
            retry_marker = str(job["group_id"])
            for active in active_rows:
                try:
                    params = json.loads(active.get("params") or "{}")
                except (TypeError, ValueError):
                    params = {}
                if isinstance(params, dict) and params.get("_admin_retry_of_group") == retry_marker:
                    retry_group = active["group_id"]
                    rows = [dict(row) for row in self.db.execute(
                        "SELECT id,org,number,source,collection_type,status,attempts,scheduled_at "
                        "FROM collection_jobs WHERE org=? AND number=? AND group_id=? ORDER BY stage_order,id",
                        (job["org"], job["number"], retry_group))]
                    return {"status": "already_queued", "group_id": retry_group, "jobs": rows}
            if active_rows:
                raise ValueError("Já existe outra coleta ativa para este processo.")
            if not group_rows:
                raise LookupError("Grupo original do job não encontrado.")
            selected_rows = [dict(job)]
            consolidation = next((row for row in group_rows
                                  if row["source"] == "Consolidation" and row["id"] != job["id"]), None)
            if consolidation:
                selected_rows.append(consolidation)
            for row in selected_rows:
                try:
                    original_params = json.loads(row.get("params") or "{}")
                except (TypeError, ValueError):
                    original_params = {}
                if isinstance(original_params, dict) and original_params.get("human_assist"):
                    raise ValueError("Jobs de verificação humana devem ser retomados pelo fluxo assistido.")
            if any(row["status"] in ("queued", "running") for row in group_rows):
                raise ValueError("O grupo ainda contém etapas em andamento.")

            new_group_id = "admin-retry-" + uuid.uuid4().hex
            created = []
            for old in selected_rows:
                try:
                    params = json.loads(old.get("params") or "{}")
                except (TypeError, ValueError):
                    params = {}
                if not isinstance(params, dict):
                    params = {}
                params["_admin_retry_of_group"] = retry_marker
                cursor = self.db.execute("""INSERT INTO collection_jobs
                    (org,number,group_id,source,collection_type,stage_order,priority,status,
                     attempts,max_attempts,scheduled_at,created_at,updated_at,params)
                    VALUES(?,?,?,?,?,?,?,'queued',0,?,?,?,?,?)""",
                    (old["org"], old["number"], new_group_id, old["source"],
                     old["collection_type"], old["stage_order"], old["priority"],
                     old["max_attempts"], stamp, stamp, stamp,
                     json.dumps(params, ensure_ascii=False, separators=(",", ":"))))
                created.append({"id": cursor.lastrowid, "org": old["org"], "number": old["number"],
                                "source": old["source"], "collection_type": old["collection_type"],
                                "status": "queued", "attempts": 0, "scheduled_at": stamp})
            if job["collection_type"] == "scheduled_refresh":
                self.db.execute("""UPDATE monitor_schedules SET last_group_id=?,status='queued',
                    last_error=NULL,updated_at=? WHERE org=? AND number=? AND last_group_id=?""",
                    (new_group_id, stamp, job["org"], job["number"], job["group_id"]))
        return {"status": "scheduled", "group_id": new_group_id, "jobs": created}

    def list_platform_jobs(self, *, office_id=None, number=None, collection_type=None,
                           status=None, source=None, created_after=None, created_before=None,
                           job_kind=None, limit=50, offset=0):
        """Paginated global view over process collection and OAB monitor queues."""
        clauses, args = [], []
        for column, value in (("org", office_id), ("number", number),
                              ("collection_type", collection_type), ("status", status),
                              ("source", source)):
            if value is not None:
                clauses.append(f"{column}=?")
                args.append(value)
        if created_after is not None:
            clauses.append("created_at>=?")
            args.append(created_after)
        if created_before is not None:
            clauses.append("created_at<=?")
            args.append(created_before)
        if job_kind is not None:
            clauses.append("job_kind=?")
            args.append(job_kind)
        if self.environment == "production":
            clauses.append("org<>'local'")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        union = """SELECT id,org,number,source,collection_type,status,attempts,max_attempts,
                scheduled_at,created_at,updated_at,started_at,completed_at,error,coverage,result,
                'collection' AS job_kind,NULL AS monitor_id,NULL AS oab,NULL AS uf
            FROM collection_jobs
            UNION ALL
            SELECT j.id,j.org,NULL AS number,'DJEN' AS source,'oab_monitor' AS collection_type,
                j.status,j.attempts,NULL AS max_attempts,j.scheduled_at,j.created_at,j.updated_at,
                j.started_at,j.completed_at,j.error,NULL AS coverage,j.result,'oab' AS job_kind,
                j.monitor_id,m.oab,m.uf
            FROM oab_monitor_jobs j JOIN oab_monitors m ON m.id=j.monitor_id"""
        db = self.db
        total = db.execute(f"SELECT count(*) FROM ({union}) jobs{where}", args).fetchone()[0]
        rows = db.execute(f"SELECT * FROM ({union}) jobs{where} "
                          "ORDER BY created_at DESC,job_kind,id DESC LIMIT ? OFFSET ?",
                          (*args, limit, offset)).fetchall()
        return {"items": [dict(row) for row in rows], "total": total,
                "limit": limit, "offset": offset}

    def _upsert_inbox_communication(self, org, number, source_id, payload, *,
                                    found_by_process=False, found_by_oab=False,
                                    discovery_kind="process_sync", oab=None, uf=None,
                                    monitor_id=None):
        stamp = now()
        existing = self.db.execute("SELECT payload FROM communication_inbox WHERE org=? AND number=? AND source_id=?",
                                   (org, number, str(source_id))).fetchone()
        merged_payload = dict(payload or {})
        if existing:
            try:
                previous = json.loads(existing["payload"])
            except (TypeError, ValueError):
                previous = {}
            if isinstance(previous, dict):
                merged_payload = previous | {key: value for key, value in merged_payload.items()
                                              if value not in (None, "", [], {})}
                if isinstance(previous.get("original"), dict) and isinstance(merged_payload.get("original"), dict):
                    merged_payload["original"] = previous["original"] | {
                        key: value for key, value in merged_payload["original"].items()
                        if value not in (None, "", [], {})}
        encoded = json.dumps(merged_payload, ensure_ascii=False)
        self.db.execute("""INSERT INTO communication_inbox
            (org,number,source_id,payload,first_seen_at,last_seen_at,discovery_kind,
             found_by_process,found_by_oab)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(org,number,source_id) DO UPDATE SET
              payload=excluded.payload,last_seen_at=excluded.last_seen_at,
              found_by_process=max(communication_inbox.found_by_process,excluded.found_by_process),
              found_by_oab=max(communication_inbox.found_by_oab,excluded.found_by_oab)""",
            (org, number, str(source_id), encoded, stamp, stamp, discovery_kind,
             int(found_by_process), int(found_by_oab)))
        if oab and uf:
            if monitor_id is not None and not self.db.execute(
                    "SELECT 1 FROM oab_monitors WHERE org=? AND id=?", (org, monitor_id)).fetchone():
                monitor_id = None
            self.db.execute("""INSERT INTO communication_oab_links
                (org,number,source_id,oab,uf,monitor_id,linked_at) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(org,number,source_id,oab,uf) DO UPDATE SET
                  monitor_id=COALESCE(excluded.monitor_id,communication_oab_links.monitor_id)""",
                (org, number, str(source_id), oab, uf, monitor_id, stamp))

    def save_oab_discovery(self, org, oab, uf, result, *, monitor_id=None,
                           discovery_kind="manual_oab_discovery"):
        """Commit complete discovery atomically; found cases are not monitoring watches."""
        with self.db:
            cursor = self.db.execute("""INSERT INTO discovery_runs
                (org,source,criterion,oab,uf,period_start,period_end,checked_at,status,pages,reported,matched)
                VALUES(?,'DJEN','OAB/UF',?,?,?,?,?,'success',?,?,?)""",
                (org, oab, uf, result.period_start, result.period_end, now(),
                 result.pages, result.reported, len(result.items)))
            for item in result.items:
                self.db.execute("""INSERT INTO discovered_processes
                    (org,number,source,source_id,oab,uf,available_at,collected_at)
                    VALUES(?,?,'DJEN',?,?,?,?,?) ON CONFLICT(org,source,source_id,oab,uf)
                    DO UPDATE SET collected_at=excluded.collected_at""",
                    (org, item.number, item.source_id, oab, uf, item.available_at, now()))
                self.db.execute("""INSERT INTO raw_records
                    (org,number,source,source_id,payload,collected_at)
                    VALUES(?,?,'DJEN',?,?,?) ON CONFLICT(org,number,source,source_id)
                    DO UPDATE SET payload=excluded.payload,collected_at=excluded.collected_at""",
                    (org, item.number, item.source_id,
                     json.dumps(item.original, ensure_ascii=False), now()))
                self._upsert_inbox_communication(
                    org, item.number, item.source_id, item.model_dump(),
                    found_by_oab=True, discovery_kind=discovery_kind,
                    oab=oab, uf=uf, monitor_id=monitor_id)
        return cursor.lastrowid

    def create_oab_monitor(self, org, oab, uf, *, lawyer_name=None,
                           enabled=True, interval_hours=24, now_at=None):
        require_operational_office(self.environment, org)
        if interval_hours not in (6, 12, 24, 48):
            raise ValueError("A frequência deve ser 6, 12, 24 ou 48 horas.")
        if not isinstance(enabled, bool):
            raise ValueError("enabled deve ser verdadeiro ou falso.")
        stamp_dt = now_at or datetime.now(timezone.utc)
        stamp = stamp_dt.astimezone(timezone.utc).isoformat()
        next_run = stamp if enabled else None
        try:
            with self.db:
                cursor = self.db.execute("""INSERT INTO oab_monitors
                    (org,oab,uf,lawyer_name,enabled,interval_hours,created_at,next_run_at,status)
                    VALUES(?,?,?,?,?,?,?,?,?)""",
                    (org, oab, uf, lawyer_name.strip() if lawyer_name and lawyer_name.strip() else None,
                     int(enabled), interval_hours, stamp, next_run, "active" if enabled else "paused"))
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint failed: oab_monitors.org, oab_monitors.oab, oab_monitors.uf" in str(exc):
                raise ValueError("Esta OAB/UF já está cadastrada neste escritório.") from None
            raise
        return self.oab_monitor(org, cursor.lastrowid)

    def list_oab_monitors(self, org):
        rows = self.db.execute("SELECT * FROM oab_monitors WHERE org=? ORDER BY created_at DESC,id DESC", (org,))
        return [dict(row) | {"enabled": bool(row["enabled"]),
                             "baseline_completed": bool(row["baseline_completed"])} for row in rows]

    def oab_monitor(self, org, monitor_id):
        row = self.db.execute("SELECT * FROM oab_monitors WHERE org=? AND id=?", (org, monitor_id)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        result["baseline_completed"] = bool(result["baseline_completed"])
        result["recent_jobs"] = [dict(item) for item in self.db.execute(
            "SELECT id,window_start,window_end,run_kind,status,attempts,scheduled_at,started_at,completed_at,result,error "
            "FROM oab_monitor_jobs WHERE monitor_id=? ORDER BY id DESC LIMIT 10", (monitor_id,))]
        return result

    def update_oab_monitor(self, org, monitor_id, *, enabled=None, interval_hours=None, lawyer_name=None):
        monitor = self.oab_monitor(org, monitor_id)
        if not monitor:
            return None
        if enabled is not None and not isinstance(enabled, bool):
            raise ValueError("enabled deve ser verdadeiro ou falso.")
        if interval_hours is not None and interval_hours not in (6, 12, 24, 48):
            raise ValueError("A frequência deve ser 6, 12, 24 ou 48 horas.")
        active = monitor["enabled"] if enabled is None else enabled
        interval = monitor["interval_hours"] if interval_hours is None else interval_hours
        stamp_dt = datetime.now(timezone.utc)
        stamp = stamp_dt.isoformat()
        next_run = stamp if active and (enabled is True or not monitor["next_run_at"]) else monitor["next_run_at"]
        if not active:
            next_run = None
        with self.db:
            self.db.execute("""UPDATE oab_monitors SET enabled=?,interval_hours=?,lawyer_name=?,
                next_run_at=?,status=?,last_error=CASE WHEN ? THEN last_error ELSE NULL END
                WHERE org=? AND id=?""",
                (int(active), interval,
                 (lawyer_name.strip() or None) if lawyer_name is not None else monitor["lawyer_name"],
                 next_run, "active" if active else "paused", int(active), org, monitor_id))
            if not active:
                self.db.execute("""UPDATE oab_monitor_jobs SET status='cancelled',updated_at=?,completed_at=?
                    WHERE monitor_id=? AND status='queued' AND run_kind<>'manual'""",
                    (stamp, stamp, monitor_id))
        return self.oab_monitor(org, monitor_id)

    def delete_oab_monitor(self, org, monitor_id):
        with self.db:
            return self.db.execute("DELETE FROM oab_monitors WHERE org=? AND id=?", (org, monitor_id)).rowcount > 0

    @staticmethod
    def _oab_window(monitor, today):
        cursor = monitor.get("cursor_end")
        if not cursor:
            finish = today
            begin = finish - timedelta(days=30)
            kind = "baseline" if not monitor.get("baseline_completed") else "incremental"
        else:
            begin = date.fromisoformat(str(cursor)[:10]) - timedelta(days=2)
            if (today - begin).days > 30:
                finish = begin + timedelta(days=30)
            else:
                finish = today
            kind = "incremental"
        return begin, finish, kind

    def enqueue_oab_monitor_job(self, org, monitor_id, *, manual=False, now_at=None):
        require_operational_office(self.environment, org)
        stamp_dt = now_at or datetime.now(timezone.utc)
        stamp = stamp_dt.astimezone(timezone.utc).isoformat()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            monitor = self.oab_monitor(org, monitor_id)
            if not monitor:
                raise ValueError("Monitoramento OAB não encontrado neste escritório.")
            if not manual and not monitor["enabled"]:
                raise ValueError("Ative o monitoramento OAB antes de agendar a consulta.")
            active = self.db.execute("SELECT * FROM oab_monitor_jobs WHERE monitor_id=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1",
                                     (monitor_id,)).fetchone()
            if active:
                return dict(active)
            begin, finish, kind = self._oab_window(monitor, stamp_dt.date())
            if manual:
                kind = "manual"
            cursor = self.db.execute("""INSERT INTO oab_monitor_jobs
                (monitor_id,org,window_start,window_end,run_kind,status,scheduled_at,created_at,updated_at)
                VALUES(?,?,?,?,?,'queued',?,?,?)""",
                (monitor_id, org, begin.isoformat(), finish.isoformat(), kind, stamp, stamp, stamp))
            job_id = cursor.lastrowid
            self.db.execute("UPDATE oab_monitors SET last_job_id=?,status='queued' WHERE org=? AND id=?",
                            (job_id, org, monitor_id))
        return dict(self.db.execute("SELECT * FROM oab_monitor_jobs WHERE id=?", (job_id,)).fetchone())

    def enqueue_due_oab_monitor_jobs(self, org=None, *, now_at=None, limit=10):
        if org is not None:
            require_operational_office(self.environment, org)
        stamp_dt = now_at or datetime.now(timezone.utc)
        stamp_dt = stamp_dt.astimezone(timezone.utc)
        stamp = stamp_dt.isoformat()
        results = []
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            sql = """SELECT m.* FROM oab_monitors m JOIN offices o ON o.id=m.org AND o.active=1
                WHERE m.enabled=1 AND m.next_run_at IS NOT NULL AND m.next_run_at<=?"""
            params = [stamp]
            if org is not None:
                sql += " AND m.org=?"
                params.append(org)
            if self.environment == "production":
                sql += " AND m.org<>'local'"
            sql += " ORDER BY m.next_run_at,m.id LIMIT ?"
            params.append(max(1, limit))
            for monitor_row in self.db.execute(sql, params).fetchall():
                monitor = dict(monitor_row)
                active = self.db.execute("SELECT id FROM oab_monitor_jobs WHERE monitor_id=? AND status IN ('queued','running')",
                                         (monitor["id"],)).fetchone()
                if active:
                    continue
                begin, finish, kind = self._oab_window(monitor, stamp_dt.date())
                cursor = self.db.execute("""INSERT INTO oab_monitor_jobs
                    (monitor_id,org,window_start,window_end,run_kind,status,scheduled_at,created_at,updated_at)
                    VALUES(?,?,?,?,?,'queued',?,?,?)""",
                    (monitor["id"], monitor["org"], begin.isoformat(), finish.isoformat(), kind,
                     stamp, stamp, stamp))
                job_id = cursor.lastrowid
                next_run = (stamp_dt + timedelta(hours=monitor["interval_hours"])).isoformat()
                self.db.execute("""UPDATE oab_monitors SET status='queued',last_job_id=?,next_run_at=?
                    WHERE id=?""", (job_id, next_run, monitor["id"]))
                results.append({"monitor_id": monitor["id"], "job_id": job_id,
                                "window_start": begin.isoformat(), "window_end": finish.isoformat(),
                                "run_kind": kind})
        return results

    def requeue_stale_oab_monitor_jobs(self, org=None, *, stale_after_seconds=7200, now_at=None):
        """Recover a worker-interrupted OAB job without advancing its cursor."""
        stamp_dt = now_at or datetime.now(timezone.utc)
        stamp_dt = stamp_dt.astimezone(timezone.utc)
        stamp = stamp_dt.isoformat()
        cutoff = (stamp_dt - timedelta(seconds=max(60, stale_after_seconds))).isoformat()
        where = "status='running' AND started_at IS NOT NULL AND started_at<=?"
        params = [cutoff]
        if org is not None:
            require_operational_office(self.environment, org)
            where += " AND org=?"
            params.append(org)
        elif self.environment == "production":
            where += " AND org<>'local'"
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            job_ids = [row[0] for row in self.db.execute(
                f"SELECT id FROM oab_monitor_jobs WHERE {where}", params).fetchall()]
            if not job_ids:
                return 0
            marks = ",".join("?" for _ in job_ids)
            self.db.execute(f"UPDATE oab_monitor_jobs SET status='queued',started_at=NULL,updated_at=? WHERE id IN ({marks})",
                            [stamp, *job_ids])
            self.db.execute(f"UPDATE oab_monitors SET status='queued' WHERE last_job_id IN ({marks}) AND enabled=1",
                            job_ids)
            return len(job_ids)

    def claim_oab_monitor_job(self, org=None):
        if org is not None:
            require_operational_office(self.environment, org)
        stamp = datetime.now(timezone.utc).isoformat()
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            sql = """SELECT j.* FROM oab_monitor_jobs j JOIN offices o ON o.id=j.org AND o.active=1
                WHERE j.status='queued' AND j.scheduled_at<=?"""
            params = [stamp]
            if org is not None:
                sql += " AND j.org=?"
                params.append(org)
            if self.environment == "production":
                sql += " AND j.org<>'local'"
            row = self.db.execute(sql + " ORDER BY j.scheduled_at,j.id LIMIT 1", params).fetchone()
            if not row:
                return None
            changed = self.db.execute("""UPDATE oab_monitor_jobs SET status='running',attempts=attempts+1,
                started_at=?,updated_at=? WHERE id=? AND status='queued'""",
                (stamp, stamp, row["id"])).rowcount
            if not changed:
                return None
            self.db.execute("UPDATE oab_monitors SET status='running',last_attempt_at=? WHERE id=?",
                            (stamp, row["monitor_id"]))
            claimed = dict(self.db.execute("SELECT * FROM oab_monitor_jobs WHERE id=?", (row["id"],)).fetchone())
            claimed["monitor"] = dict(self.db.execute("SELECT * FROM oab_monitors WHERE id=?", (row["monitor_id"],)).fetchone())
            return claimed

    def finish_oab_monitor_job(self, job, *, success, result=None, error=None, retry_seconds=900):
        stamp_dt = datetime.now(timezone.utc)
        stamp = stamp_dt.isoformat()
        status = "completed" if success else "failed"
        encoded_result = json.dumps(result, ensure_ascii=False) if result is not None else None
        encoded_error = json.dumps({"message": str(error)[:500]}, ensure_ascii=False) if error else None
        with self.db:
            changed = self.db.execute("""UPDATE oab_monitor_jobs SET status=?,updated_at=?,completed_at=?,
                result=?,error=? WHERE id=? AND status='running'""",
                (status, stamp, stamp, encoded_result, encoded_error, job["id"])).rowcount
            if not changed:
                return False
            monitor = self.db.execute("SELECT * FROM oab_monitors WHERE id=?", (job["monitor_id"],)).fetchone()
            if not monitor:
                return True
            if success:
                cursor_end = max(str(monitor["cursor_end"] or ""), job["window_end"])
                next_run = (stamp_dt + timedelta(hours=monitor["interval_hours"])).isoformat() if monitor["enabled"] else None
                self.db.execute("""UPDATE oab_monitors SET last_success_at=?,cursor_end=?,
                    baseline_completed=1,last_result=?,last_error=NULL,status=?,next_run_at=? WHERE id=?""",
                    (stamp, cursor_end, encoded_result, "active" if monitor["enabled"] else "paused",
                     next_run, job["monitor_id"]))
            else:
                next_run = (stamp_dt + timedelta(seconds=max(60, retry_seconds))).isoformat() if monitor["enabled"] else None
                self.db.execute("""UPDATE oab_monitors SET last_error=?,status=?,next_run_at=? WHERE id=?""",
                    (str(error)[:500], "failed" if monitor["enabled"] else "paused", next_run, job["monitor_id"]))
        return True

    def oab_monitor_communications(self, org, monitor_id, *, start=None, end=None, page=1, size=20):
        monitor = self.oab_monitor(org, monitor_id)
        if not monitor:
            return None
        where = "l.org=? AND l.oab=? AND l.uf=?"
        params = [org, monitor["oab"], monitor["uf"]]
        if start:
            where += " AND date(coalesce(json_extract(i.payload,'$.published_at'),json_extract(i.payload,'$.available_at'),i.first_seen_at))>=date(?)"
            params.append(start)
        if end:
            where += " AND date(coalesce(json_extract(i.payload,'$.published_at'),json_extract(i.payload,'$.available_at'),i.first_seen_at))<=date(?)"
            params.append(end)
        total = self.db.execute(f"SELECT count(*) FROM communication_inbox i JOIN communication_oab_links l USING(org,number,source_id) WHERE {where}", params).fetchone()[0]
        rows = self.db.execute(f"""SELECT i.*,l.oab,l.uf,w.court,
            CASE WHEN w.number IS NULL THEN 0 ELSE 1 END AS process_monitored
            FROM communication_inbox i JOIN communication_oab_links l USING(org,number,source_id)
            LEFT JOIN watches w ON w.org=i.org AND w.number=i.number
            WHERE {where} GROUP BY i.org,i.number,i.source_id
            ORDER BY coalesce(json_extract(i.payload,'$.published_at'),json_extract(i.payload,'$.available_at'),i.first_seen_at) DESC
            LIMIT ? OFFSET ?""", (*params, size, (page-1)*size)).fetchall()
        return {"monitor": monitor, "total": total, "page": page, "size": size,
                "items": [dict(row) for row in rows]}

    def inbox_communications(self, org, *, oab=None, uf=None, number=None, q=None, start=None, end=None,
                             read=None, monitored=None, kind=None, page=1, size=20):
        # The CTE also surfaces legacy communication rows written before the
        # shared inbox migration, without mutating data during a read request.
        inbox_cte = """WITH inbox_rows AS (
            SELECT org,number,source_id,payload,first_seen_at,last_seen_at,read_at,discovery_kind,
                   found_by_process,found_by_oab FROM communication_inbox
            UNION ALL
            SELECT c.org,c.number,c.source_id,c.payload,c.collected_at,c.collected_at,NULL,'process_sync',1,0
            FROM communications c WHERE NOT EXISTS (
                SELECT 1 FROM communication_inbox i WHERE i.org=c.org AND i.number=c.number AND i.source_id=c.source_id)) """
        where = ["i.org=?"]
        params = [org]
        if number:
            where.append("i.number=?"); params.append(number)
        if q:
            where.append("(i.number LIKE ? OR lower(i.payload) LIKE lower(?))")
            params.extend(["%" + q + "%", "%" + q + "%"])
        if start:
            where.append("date(coalesce(json_extract(i.payload,'$.published_at'),json_extract(i.payload,'$.available_at'),i.first_seen_at))>=date(?)"); params.append(start)
        if end:
            where.append("date(coalesce(json_extract(i.payload,'$.published_at'),json_extract(i.payload,'$.available_at'),i.first_seen_at))<=date(?)"); params.append(end)
        if read is not None:
            where.append("(i.read_at IS NOT NULL)=?"); params.append(int(read))
        if monitored is not None:
            where.append("EXISTS(SELECT 1 FROM watches w WHERE w.org=i.org AND w.number=i.number)=?"); params.append(int(monitored))
        if oab:
            where.append("EXISTS(SELECT 1 FROM communication_oab_links l WHERE l.org=i.org AND l.number=i.number AND l.source_id=i.source_id AND l.oab=? AND (? IS NULL OR l.uf=?))")
            params.extend([oab, uf, uf])
        elif uf:
            where.append("EXISTS(SELECT 1 FROM communication_oab_links l WHERE l.org=i.org AND l.number=i.number AND l.source_id=i.source_id AND l.uf=?)"); params.append(uf)
        if kind == "intimation":
            where.append("lower(coalesce(json_extract(i.payload,'$.tipo'),'')) LIKE '%intima%'")
        elif kind == "other":
            where.append("lower(coalesce(json_extract(i.payload,'$.tipo'),'')) NOT LIKE '%intima%'")
        clause = " AND ".join(where)
        total = self.db.execute(inbox_cte + f"SELECT count(*) FROM inbox_rows i WHERE {clause}", params).fetchone()[0]
        rows = self.db.execute(inbox_cte + f"""SELECT i.*,w.court,
            EXISTS(SELECT 1 FROM watches w2 WHERE w2.org=i.org AND w2.number=i.number) AS process_monitored,
            (SELECT json_group_array(json_object('oab',l.oab,'uf',l.uf))
             FROM communication_oab_links l WHERE l.org=i.org AND l.number=i.number AND l.source_id=i.source_id) AS oab_refs
            FROM inbox_rows i LEFT JOIN watches w ON w.org=i.org AND w.number=i.number
            WHERE {clause} ORDER BY coalesce(json_extract(i.payload,'$.published_at'),json_extract(i.payload,'$.available_at'),i.first_seen_at) DESC,
              i.number,i.source_id LIMIT ? OFFSET ?""", (*params, size, (page-1)*size)).fetchall()
        counts = self.db.execute(inbox_cte + """SELECT count(*) AS total,
            sum(CASE WHEN read_at IS NULL THEN 1 ELSE 0 END) AS unread,
            sum(CASE WHEN discovery_kind='new_publication' THEN 1 ELSE 0 END) AS new_count
            FROM inbox_rows WHERE org=?""", (org,)).fetchone()
        items = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            item["oab_refs"] = json.loads(item["oab_refs"] or "[]")
            item["is_read"] = bool(item["read_at"])
            item["is_new"] = item["discovery_kind"] == "new_publication"
            items.append(item)
        return {"items": items, "total": total, "page": page, "size": size,
                "unread_count": counts["unread"] or 0, "new_count": counts["new_count"] or 0,
                "source": "SQLite local"}

    def communication_detail(self, org, number, source_id):
        row = self.db.execute("SELECT * FROM communication_inbox WHERE org=? AND number=? AND source_id=?",
                              (org, number, source_id)).fetchone()
        if not row:
            return None
        result = dict(row); result["payload"] = json.loads(result["payload"])
        result["oab_refs"] = [dict(item) for item in self.db.execute(
            "SELECT oab,uf,linked_at FROM communication_oab_links WHERE org=? AND number=? AND source_id=? ORDER BY oab,uf",
            (org,number,source_id))]
        result["process_monitored"] = self.db.execute(
            "SELECT 1 FROM watches WHERE org=? AND number=?", (org,number)).fetchone() is not None
        result["is_read"] = bool(result["read_at"])
        return result

    def set_communication_read(self, org, number, source_id, *, read):
        stamp = now()
        with self.db:
            changed = self.db.execute("""UPDATE communication_inbox SET read_at=?
                WHERE org=? AND number=? AND source_id=?""",
                (stamp if read else None, org, number, source_id)).rowcount
        return bool(changed)

    def record_oab_discovery_failure(self, org, oab, uf, start, end, error):
        with self.db:
            self.db.execute("""INSERT INTO discovery_runs
                (org,source,criterion,oab,uf,period_start,period_end,checked_at,status,error)
                VALUES(?,'DJEN','OAB/UF',?,?,?,?,?,'error',?)""",
                (org, oab, uf, start, end, now(), str(error)[:300]))

    def discovered_by_oab(self, org, oab, uf):
        return [dict(row) for row in self.db.execute("""SELECT d.number, count(*) AS publications,
            CASE WHEN w.number IS NULL THEN 0 ELSE 1 END AS monitored
            FROM discovered_processes d LEFT JOIN watches w ON w.org=d.org AND w.number=d.number
            WHERE d.org=? AND d.oab=? AND d.uf=? GROUP BY d.number ORDER BY d.number""",
            (org, oab, uf))]

    def oab_discovery_page(self, org, oab, uf, start, end, page=1, size=20):
        from .providers.djen import parse_communication

        filters = (org, oab, uf, start, end)
        condition = "d.org=? AND d.oab=? AND d.uf=? AND substr(d.available_at,1,10)>=? AND substr(d.available_at,1,10)<=?"
        total = self.db.execute(f"SELECT count(DISTINCT d.number) FROM discovered_processes d WHERE {condition}", filters).fetchone()[0]
        publication_total = self.db.execute(f"SELECT count(*) FROM discovered_processes d WHERE {condition}", filters).fetchone()[0]
        rows = self.db.execute(f"""SELECT d.number,MAX(d.available_at) AS last_available,
            CASE WHEN w.number IS NULL THEN 0 ELSE 1 END AS monitored
            FROM discovered_processes d LEFT JOIN watches w ON w.org=d.org AND w.number=d.number
            WHERE {condition} GROUP BY d.number
            ORDER BY last_available DESC,d.number LIMIT ? OFFSET ?""",
            (*filters, size, (page - 1) * size)).fetchall()
        items = []
        for row in rows:
            communication_rows = self.db.execute(f"""SELECT d.source_id,d.available_at,r.payload
                FROM discovered_processes d JOIN raw_records r
                  ON r.org=d.org AND r.number=d.number AND r.source=d.source AND r.source_id=d.source_id
                WHERE {condition} AND d.number=?
                ORDER BY d.available_at DESC,d.source_id""", (*filters, row["number"])).fetchall()
            communications = [parse_communication(json.loads(item["payload"]), row["number"])
                               for item in communication_rows]
            if not communications:
                continue
            serialized = [item.model_dump(exclude={"original"}) for item in communications]
            items.append({"number": row["number"], "source_id": communications[0].source_id,
                          "available_at": row["last_available"], "monitored": bool(row["monitored"]),
                          "communication": serialized[0], "communications": serialized})
        return {"total": total, "publication_total": publication_total,
                "page": page, "size": size, "items": items}

    def was_discovered_by_oab(self, org, oab, uf, number, start, end):
        return self.db.execute("""SELECT 1 FROM discovered_processes WHERE
            org=? AND oab=? AND uf=? AND number=? AND substr(available_at,1,10)>=?
            AND substr(available_at,1,10)<=? LIMIT 1""",
            (org, oab, uf, number, start, end)).fetchone() is not None

    def latest_oab_discovery_run(self, org, oab, uf, start, end):
        row = self.db.execute("""SELECT id,checked_at,status,pages,reported,matched,error
            FROM discovery_runs WHERE org=? AND oab=? AND uf=? AND period_start=? AND period_end=?
            ORDER BY id DESC LIMIT 1""", (org, oab, uf, start, end)).fetchone()
        return dict(row) if row else None

    def register(self, org, number, court, interval, limit):
        require_operational_office(self.environment, org)
        if not org.strip() or interval <= 0 or limit <= 0:
            raise ValueError("Organização, intervalo e limite inválidos.")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            exists = self.db.execute("SELECT 1 FROM watches WHERE org=? AND number=?", (org, number)).fetchone()
            if not exists:
                count = self.db.execute("SELECT count(*) FROM watches WHERE org=?", (org,)).fetchone()[0]
                if count >= limit:
                    raise ValueError("Limite local de processos desta organização atingido.")
                self.db.execute("INSERT INTO watches(org,number,court,interval,created_at,next_due) VALUES(?,?,?,?,?,?)",
                                (org, number, court, interval, now(), now()))
                self.db.execute("""INSERT OR IGNORE INTO monitor_schedules
                    (org,number,enabled,interval_hours,status,updated_at)
                    VALUES(?,?,0,24,'paused',?)""", (org, number, now()))
            else:
                self.db.execute("UPDATE watches SET interval=? WHERE org=? AND number=?", (interval, org, number))

    def get(self, org, number):
        row = self.db.execute("SELECT * FROM watches WHERE org=? AND number=?", (org, number)).fetchone()
        if not row:
            raise ValueError("Processo não cadastrado nesta organização.")
        return dict(row)

    def list_watches(self, org, due_only=False):
        sql = "SELECT * FROM watches WHERE org=?"
        params = [org]
        if due_only:
            sql += " AND next_due<=?"
            params.append(now())
        return [dict(r) for r in self.db.execute(sql + " ORDER BY number", params)]

    def start_run(self, org, number):
        self.get(org, number)
        with self.db:
            cur = self.db.execute("INSERT INTO runs(org,number,started_at,status) VALUES(?,?,?,'running')", (org, number, now()))
            return cur.lastrowid

    def apply(self, org, number, run, snapshot: Snapshot):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            watch = self.get(org, number)
            run_row = self.db.execute("SELECT * FROM runs WHERE id=? AND org=? AND number=?", (run, org, number)).fetchone()
            if not run_row or run_row["status"] != "running":
                raise ValueError("Execução não está aberta para este processo.")
            latest = self.db.execute("SELECT max(id) FROM runs WHERE org=? AND number=? AND status='success'", (org, number)).fetchone()[0]
            if latest is not None and latest > run:
                raise ValueError("Execução ultrapassada por consulta mais recente; histórico preservado.")
            previous = {meta["_scope"]: meta for meta in json.loads(watch["metadata"] or "[]")}
            for meta in snapshot.metadata:
                before = previous.get(meta["_scope"], {}).get("dataHoraUltimaAtualizacao")
                after = meta.get("dataHoraUltimaAtualizacao")
                if before and after:
                    first, second = datetime.fromisoformat(timestamp(before)), datetime.fromisoformat(timestamp(after))
                    if bool(first.tzinfo) == bool(second.tzinfo) and second < first:
                        raise ValueError("Fonte retornou versão anterior do documento; histórico preservado.")
            old = [dict(r) for r in self.db.execute("SELECT * FROM movements WHERE org=? AND number=? AND active=1", (org, number))]
            scopes = {r[0] for r in self.db.execute("SELECT scope FROM scopes WHERE org=? AND number=?", (org, number))}
            decisions = classify(snapshot.movements, old, scopes)
            counts = {"historical": 0, "new": 0, "corrected": 0, "review": 0}
            stamp = now()
            for movement, kind, replaces in decisions:
                if replaces:
                    self.db.execute("UPDATE movements SET active=0 WHERE org=? AND number=? AND fingerprint=?", (org, number, replaces))
                self.db.execute("""INSERT INTO movements VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?)
                    ON CONFLICT(org,number,fingerprint) DO UPDATE SET active=1,last_seen=excluded.last_seen""",
                    (org, number, movement.fingerprint, movement.scope, movement.anchor,
                     movement.when, movement.code, movement.name, canonical(movement.complements),
                     canonical(movement.raw), stamp, stamp, kind))
                if kind != "historical":
                    self.db.execute("INSERT OR IGNORE INTO events(org,number,fingerprint,kind,detected_at,run_id,replaces) VALUES(?,?,?,?,?,?,?)",
                                    (org, number, movement.fingerprint, kind, stamp, run, replaces))
                counts[kind] += 1
            for movement in snapshot.movements:
                self.db.execute("UPDATE movements SET last_seen=? WHERE org=? AND number=? AND fingerprint=?", (stamp, org, number, movement.fingerprint))
            for scope in snapshot.scopes:
                self.db.execute("INSERT OR IGNORE INTO scopes VALUES(?,?,?)", (org, number, scope))
            result = Result(counts["historical"], counts["new"], counts["corrected"], counts["review"])
            due = (datetime.now(timezone.utc) + timedelta(seconds=watch["interval"])).isoformat()
            self.db.execute("UPDATE watches SET metadata=?,last_checked=?,next_due=?,last_error=NULL WHERE org=? AND number=?",
                            (canonical(snapshot.metadata), stamp, due, org, number))
            self.db.execute("UPDATE runs SET status='success',finished_at=?,raw=?,result=? WHERE id=?",
                            (stamp, canonical(snapshot.raw), canonical(result.__dict__), run))
            new_keys = {"datajud:" + movement.fingerprint for movement, kind, _ in decisions
                        if kind in ("new", "review")}
            historical_keys = {"datajud:" + movement.fingerprint for movement, kind, _ in decisions
                               if kind in ("historical", "corrected")}
            for movement, kind, replaces in decisions:
                if kind != "corrected" or not replaces:
                    continue
                old_key = "datajud:" + replaces
                new_key = "datajud:" + movement.fingerprint
                prior = self.db.execute("""SELECT m.logical_id,m.first_seen_at,s.anchor_key
                    FROM logical_event_members m JOIN logical_event_states s
                    USING(org,number,logical_id) WHERE m.org=? AND m.number=? AND m.source_event_key=?""",
                    (org, number, old_key)).fetchone()
                if prior:
                    self.db.execute("""INSERT OR REPLACE INTO logical_event_members
                        (org,number,source_event_key,logical_id,source,first_seen_at)
                        VALUES(?,?,?,?, 'datajud',?)""",
                        (org, number, new_key, prior["logical_id"], prior["first_seen_at"]))
                    if prior["anchor_key"] == old_key:
                        self.db.execute("""UPDATE logical_event_states SET anchor_key=?,updated_at=?
                            WHERE org=? AND number=? AND logical_id=?""",
                            (new_key, stamp, org, number, prior["logical_id"]))
            self._sync_logical_event_states(org, number, self._movement_timeline(org, number),
                                            new_source_keys=new_keys,
                                            baseline_source_keys=historical_keys)
            return result

    def fail(self, org, number, run, error, raw=None):
        watch = self.get(org, number)
        due = (datetime.now(timezone.utc) + timedelta(seconds=watch["interval"])).isoformat()
        with self.db:
            self.db.execute("UPDATE runs SET status='error',finished_at=?,error=?,raw=? WHERE id=? AND org=? AND number=? AND status='running'",
                            (now(), error, canonical(raw) if raw is not None else None, run, org, number))
            self.db.execute("UPDATE watches SET last_checked=?,next_due=?,last_error=? WHERE org=? AND number=?", (now(), due, error, org, number))

    def timeline(self, org, number, revisions=False):
        self.get(org, number)
        sql = "SELECT * FROM movements WHERE org=? AND number=?"
        if not revisions:
            sql += " AND active=1"
        return [dict(r) for r in self.db.execute(sql + " ORDER BY when_at,code,fingerprint", (org, number))]

    def save_communications(self, org, number, items, *, status, period_start=None, period_end=None, error=None):
        self.get(org, number)
        stamp = now()
        with self.db:
            for item in items:
                self.db.execute("""INSERT INTO communications(org,number,source_id,payload,collected_at)
                    VALUES(?,?,?,?,?) ON CONFLICT(org,number,source_id) DO UPDATE SET
                    payload=excluded.payload,collected_at=excluded.collected_at""",
                    (org, number, item["source_id"], json.dumps(item, ensure_ascii=False), stamp))
                self._upsert_inbox_communication(org, number, item["source_id"], item,
                    found_by_process=True, discovery_kind="process_sync")
                if item.get("original") is not None:
                    self.db.execute("""INSERT INTO raw_records(org,number,source,source_id,payload,collected_at)
                        VALUES(?,?,?,?,?,?) ON CONFLICT(org,number,source,source_id) DO UPDATE SET
                        payload=excluded.payload,collected_at=excluded.collected_at""",
                        (org, number, "DJEN", item["source_id"], json.dumps(item["original"], ensure_ascii=False), stamp))
            self.db.execute("""INSERT INTO source_checks(org,number,source,checked_at,status,period_start,period_end,count,error)
                VALUES(?,?,?,?,?,?,?,?,?)""", (org, number, "DJEN", stamp, status, period_start, period_end, len(items), error))

    def attach_oab_communications(self, org, oab, uf, number, start, end):
        """Reuse already collected OAB evidence without claiming process-query coverage."""
        from .providers.djen import parse_communication

        self.get(org, number)
        rows = self.db.execute("""SELECT d.source_id,r.payload FROM discovered_processes d
            JOIN raw_records r ON r.org=d.org AND r.number=d.number
              AND r.source=d.source AND r.source_id=d.source_id
            WHERE d.org=? AND d.number=? AND d.source='DJEN' AND d.oab=? AND d.uf=?
              AND substr(d.available_at,1,10)>=? AND substr(d.available_at,1,10)<=?
            ORDER BY d.available_at,d.source_id""",
            (org, number, oab, uf, start, end)).fetchall()
        added = 0
        stamp = now()
        with self.db:
            for row in rows:
                original = json.loads(row["payload"])
                parsed = parse_communication(original, number).model_dump()
                cursor = self.db.execute("""INSERT OR IGNORE INTO communications
                    (org,number,source_id,payload,collected_at) VALUES(?,?,?,?,?)""",
                    (org, number, str(row["source_id"]),
                     json.dumps(parsed, ensure_ascii=False), stamp))
                added += cursor.rowcount
                self._upsert_inbox_communication(org, number, row["source_id"], parsed,
                    found_by_oab=True, discovery_kind="oab_attached", oab=oab, uf=uf)
        return added

    def communications(self, org, number):
        self.get(org, number)
        rows = self.db.execute("""SELECT payload,collected_at FROM communications
            WHERE org=? AND number=? ORDER BY collected_at,source_id""", (org, number)).fetchall()
        existing = {}
        for row in rows:
            item = json.loads(row["payload"])
            existing[str(item.get("source_id") or "")] = dict(item, collected_at=row["collected_at"])
        inbox = self.db.execute("""SELECT payload,last_seen_at FROM communication_inbox
            WHERE org=? AND number=? ORDER BY first_seen_at,source_id""", (org, number)).fetchall()
        for row in inbox:
            item = json.loads(row["payload"])
            item.setdefault("collected_at", row["last_seen_at"])
            existing[str(item.get("source_id") or "")] = item
        return list(existing.values())

    def portfolio_communications(self, org):
        """List local DJEN communications, including CNJs outside the process portfolio."""
        rows = self.db.execute("""SELECT i.number,i.payload,i.last_seen_at AS collected_at,w.court,
            i.read_at,i.discovery_kind,i.found_by_process,i.found_by_oab,
            EXISTS(SELECT 1 FROM watches w2 WHERE w2.org=i.org AND w2.number=i.number) AS process_monitored
            FROM communication_inbox i LEFT JOIN watches w ON w.org=i.org AND w.number=i.number
            WHERE i.org=? ORDER BY i.last_seen_at DESC,i.number,i.source_id""", (org,)).fetchall()
        return [dict(row) for row in rows]

    def participants(self, org, number):
        """Return process-scoped identity groups while retaining all publication evidence.

        Exact normalized name plus the same participant category is a probable
        identity match inside one case. Explicit source identity IDs and distinct
        OAB/UF registrations take precedence and keep conflicting identities apart.
        """
        from .participants import normalized_name

        groups = {}
        for communication in self.communications(org, number):
            for item in communication.get("participants", []):
                name = item.get("original_name") or ""
                name_key = item.get("normalized_name") or normalized_name(name)
                oab = re.sub(r"\W", "", str(item.get("oab") or "")).upper()
                uf = str(item.get("uf") or "").strip().upper()
                source_identity_id = str(item.get("source_identity_id") or "").strip()
                document_type = str(item.get("document_type") or
                    ("cpf" if item.get("cpf") else "cnpj" if item.get("cnpj") else "")).casefold()
                document_number = re.sub(r"\D", "", str(item.get("document_number") or
                    item.get("cpf") or item.get("cnpj") or ""))
                if source_identity_id:
                    key = ("source-id", str(item.get("source") or "DJEN_PUBLICACAO"), source_identity_id)
                elif document_number and document_type in {"cpf", "cnpj"}:
                    key = ("document", document_type, document_number)
                elif oab and uf:
                    # Require the structured OAB/UF and exact normalized name. A name
                    # mismatch under the same registration is retained as a conflict.
                    key = ("oab-name", oab, uf, name_key)
                else:
                    kind_key = " ".join(str(item.get("kind") or "participante").casefold().split())
                    role = item.get("role_original") or item.get("role") or item.get("papel")
                    role_mapping = None
                    if role:
                        from .participants import normalize_procedural_function
                        role_mapping = normalize_procedural_function(
                            item.get("source") or "DJEN_PUBLICACAO", role)
                    # Explicitly different legal roles are distinct process
                    # relations even when names coincide. Non-legal delivery
                    # labels and unconfirmed DJEN codes do not split repeated
                    # communication mentions.
                    role_key = (role_mapping.get("procedural_function")
                                if role_mapping and role_mapping.get("procedural_function")
                                else "")
                    source_pole_code = str(item.get("polo_code_original") or item.get("polo") or "").strip().upper()
                    if not role_key and source_pole_code:
                        # Unknown source codes are not interpreted, but distinct
                        # original values remain separate identity candidates.
                        role_key = "unmapped-pole:" + source_pole_code
                    if name_key:
                        # One process-scoped, exact full-name/category identity
                        # accumulates all communication mentions. Conflicting
                        # document/OAB identifiers above remain separate; role
                        # and pole evidence is aggregated, not treated as a new
                        # person. This is a probable identity, not proof against
                        # a same-name homonym.
                        key = ("case-name-kind", str(item.get("source") or "DJEN_PUBLICACAO"),
                               kind_key, role_key, name_key)
                    else:
                        key = ("source-mention", str(item.get("source") or "DJEN_PUBLICACAO"),
                               str(communication.get("source_id") or item.get("evidence_id") or "unknown"),
                               str(item.get("participant_id") or ""))
                group = groups.get(key)
                if group is None:
                    group = {**item, "participant_id": hashlib.sha256("|".join(key).encode()).hexdigest(),
                             "number": number, "original_name": name, "normalized_name": name_key,
                             "kind": item.get("kind") or "participante", "kinds": [], "roles": [],
                             "procedural_functions": [], "sources": [],
                             "identity_status": "confirmed_source_id" if source_identity_id else
                                 "confirmed_document" if document_number else
                                 "confirmed_oab_uf_name" if oab and uf else "probable_case_name_kind",
                             "identity_confidence": "identificador estável da fonte e nome coincidem" if source_identity_id else
                                 "documento oficial coincide no mesmo processo" if document_number else
                                 "OAB/UF e nome normalizado coincidem" if oab and uf else
                                 "nome normalizado e categoria coincidem no mesmo processo; identidade provável",
                             "mention_count": 0, "evidence_ids": [], "original_names": [],
                             "polo_evidence": [], "communication_roles": [], "relationships": [],
                             "document_type": document_type or None, "document_number": document_number or None,
                             "cpf": item.get("cpf"), "cnpj": item.get("cnpj"),
                             "identity_confirmed": bool(source_identity_id or (oab and uf) or document_number)}
                    groups[key] = group
                group["mention_count"] += 1
                for field, value in (("kinds", item.get("kind")), ("roles", item.get("role_original") or item.get("role")),
                                     ("procedural_functions", item.get("procedural_function")),
                                     ("evidence_ids", item.get("evidence_id")),
                                     ("original_names", item.get("original_name")),
                                     ("sources", item.get("source") or "DJEN_PUBLICACAO"),
                                     ("communication_roles", item.get("communication_role"))):
                    if value and value not in group[field]:
                        group[field].append(value)
                for field in ("cpf", "cnpj", "document_number", "document_type", "oab", "uf"):
                    if not group.get(field) and item.get(field):
                        group[field] = item[field]
                if item.get("polo_code_original") or item.get("polo") is not None:
                    evidence = {"source": item.get("source") or "DJEN_PUBLICACAO",
                                "field": item.get("polo_source_field"),
                                "code": item.get("polo_code_original") or item.get("polo"),
                                "evidence_id": item.get("evidence_id")}
                    if evidence not in group["polo_evidence"]:
                        group["polo_evidence"].append(evidence)
                group["relationships"].extend(rel for rel in item.get("relationships", [])
                                              if rel not in group["relationships"])
                if item.get("polo_code_original") or item.get("polo") is not None:
                    evidence = {"source": item.get("source") or "DJEN_PUBLICACAO",
                                "field": item.get("polo_source_field"),
                                "code": item.get("polo_code_original") or item.get("polo"),
                                "evidence_id": item.get("evidence_id")}
                    if evidence not in group["polo_evidence"]:
                        group["polo_evidence"].append(evidence)
        result = list(groups.values())
        oab_groups = {}
        for person in result:
            person["roles"].sort(key=str.casefold)
            person["procedural_functions"] = sorted(set(person["procedural_functions"]), key=str.casefold)
            person["kinds"] = sorted(set(person["kinds"]), key=str.casefold)
            person["sources"] = sorted(set(person["sources"]), key=str.casefold)
            person["evidence_ids"] = sorted(set(person["evidence_ids"]))
            person["original_names"] = sorted(set(person["original_names"]), key=str.casefold)
            if person.get("oab") and person.get("uf"):
                reg = (re.sub(r"\W", "", str(person["oab"])).upper(), str(person["uf"]).upper())
                oab_groups.setdefault(reg, []).append(person)
        for colliding in oab_groups.values():
            if len(colliding) > 1:
                for person in colliding:
                    person["identity_status"] = "identifier_name_conflict"
                    person["identity_conflict_candidates"] = [p["participant_id"] for p in colliding
                                                               if p is not person]
        return sorted(result, key=lambda item: (item["kind"], item["normalized_name"], item["participant_id"]))

    def reprocess_djen(self, org, number):
        from .providers.djen import parse_communication
        self.get(org, number)
        rows = list(self.db.execute("""SELECT source_id,payload FROM raw_records
            WHERE org=? AND number=? AND source='DJEN'""", (org, number)))
        with self.db:
            for row in rows:
                parsed = parse_communication(json.loads(row["payload"]), number).model_dump()
                self.db.execute("""UPDATE communications SET payload=?
                    WHERE org=? AND number=? AND source_id=?""",
                    (json.dumps(parsed, ensure_ascii=False), org, number, row["source_id"]))
        return len(rows)

    def source_coverage(self, org, number, source):
        self.get(org, number)
        return [dict(row) for row in self.db.execute("""SELECT checked_at,status,period_start,period_end,count,error
            FROM source_checks WHERE org=? AND number=? AND source=? ORDER BY id""", (org, number, source))]

    def save_court_snapshot(self, org, number, source, payload, *, status, coverage=None, error=None,
                            baseline_if_first=False):
        """Version normalized public court data in raw_records; retain collection history."""
        self.get(org, number)
        previous = self.db.execute("SELECT 1 FROM raw_records WHERE org=? AND number=? AND source=? LIMIT 1",
                                    (org, number, source)).fetchone()
        serialized = canonical(payload)
        source_id = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        stamp = now()
        with self.db:
            if not self.db.in_transaction:
                self.db.execute("BEGIN IMMEDIATE")
            known_keys = {row[0] for row in self.db.execute(
                "SELECT source_event_key FROM logical_event_members WHERE org=? AND number=?",
                (org, number))}
            self.db.execute("""INSERT OR IGNORE INTO raw_records
                (org,number,source,source_id,payload,collected_at) VALUES(?,?,?,?,?,?)""",
                (org, number, source, source_id, serialized, stamp))
            self.db.execute("""INSERT INTO source_checks
                (org,number,source,checked_at,status,period_start,period_end,count,error)
                VALUES(?,?,?,?,?,NULL,NULL,?,?)""",
                (org, number, source, stamp, status,
                 len(payload.get("movements", [])) + len(payload.get("participants", [])),
                 canonical(error) if error else None))
            events = self._movement_timeline(org, number)
            current_keys = {key for event in events for key in self._event_source_keys(event)}
            self._sync_logical_event_states(
                org, number, events,
                baseline=bool(baseline_if_first and not previous),
                new_source_keys=current_keys - known_keys,
            )
        return source_id

    def record_court_attempt(self, org, number, source, *, status, coverage=None, error=None):
        """Record a failed/empty collection attempt without replacing process data."""
        self.get(org, number)
        stamp = now()
        count = 0
        if coverage:
            count = int(coverage.get("movement_count") or 0) + int(coverage.get("participants") or 0)
        with self.db:
            self.db.execute("""INSERT INTO source_checks
                (org,number,source,checked_at,status,period_start,period_end,count,error)
                VALUES(?,?,?,?,?,NULL,NULL,?,?)""",
                (org, number, source, stamp, status, count,
                 canonical(error) if error else None))

    def list_calendar_items(self, org, *, item_type=None, status=None, start=None, end=None, number=None):
        """Return only entries belonging to the active office."""
        clauses = ["org=?"]
        params = [org]
        if item_type:
            clauses.append("item_type=?")
            params.append(item_type)
        if status:
            clauses.append("status=?")
            params.append(status)
        if start:
            clauses.append("starts_at>=?")
            params.append(start)
        if end:
            clauses.append("starts_at<=?")
            params.append(end)
        if number:
            clauses.append("number=?")
            params.append(number)
        rows = self.db.execute("SELECT * FROM office_calendar_items WHERE " + " AND ".join(clauses) +
                               " ORDER BY starts_at,id", params).fetchall()
        return [dict(row) for row in rows]

    def create_calendar_item(self, org, *, item_type, title, starts_at, ends_at=None,
                             number=None, status=None, responsible=None, notes="",
                             origin="manual", source_id=None, created_by=None):
        if number:
            self.get(org, number)
        item_id = uuid.uuid4().hex
        stamp = now()
        default_status = "open" if item_type == "deadline" else "scheduled"
        with self.db:
            self.db.execute("""INSERT INTO office_calendar_items
                (id,org,number,item_type,title,starts_at,ends_at,status,responsible,notes,origin,source_id,created_by,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item_id, org, number, item_type, title.strip(), starts_at, ends_at,
                 status or default_status, responsible.strip() if responsible else None,
                 notes.strip(), origin, source_id, created_by, stamp, stamp))
        return self.calendar_item(org, item_id)

    def calendar_item(self, org, item_id):
        row = self.db.execute("SELECT * FROM office_calendar_items WHERE org=? AND id=?", (org, item_id)).fetchone()
        return dict(row) if row else None

    def update_calendar_item(self, org, item_id, *, title=None, starts_at=None, ends_at=None,
                             status=None, responsible=None, notes=None, number=None):
        current = self.calendar_item(org, item_id)
        if current is None:
            return None
        if number:
            self.get(org, number)
        values = {"title": title.strip() if title is not None else current["title"],
                  "starts_at": starts_at if starts_at is not None else current["starts_at"],
                  "ends_at": ends_at if ends_at is not None else current["ends_at"],
                  "status": status if status is not None else current["status"],
                  "responsible": responsible.strip() if responsible is not None and responsible.strip() else current["responsible"],
                  "notes": notes.strip() if notes is not None else current["notes"],
                  "number": number if number is not None else current["number"]}
        if values["number"]:
            self.get(org, values["number"])
        with self.db:
            self.db.execute("""UPDATE office_calendar_items SET title=?,starts_at=?,ends_at=?,status=?,
                responsible=?,notes=?,number=?,updated_at=? WHERE org=? AND id=?""",
                (values["title"], values["starts_at"], values["ends_at"], values["status"],
                 values["responsible"], values["notes"], values["number"], now(), org, item_id))
        return self.calendar_item(org, item_id)

    def latest_court_snapshot(self, org, number, source="PJe/TRF1"):
        snapshots = self.court_snapshots(org, number, source)
        return snapshots[0] if snapshots else None

    def court_snapshots(self, org, number, source="PJe/TRF1"):
        """Return substantive versioned snapshots newest first, preserving history."""
        self.get(org, number)
        rows = self.db.execute("""SELECT payload,source_id,collected_at FROM raw_records
            WHERE org=? AND number=? AND source=? ORDER BY collected_at DESC,source_id DESC""",
            (org, number, source))
        snapshots = []
        for row in rows:
            data = json.loads(row["payload"])
            # Older versions persisted empty error-shaped payloads. Skip those while
            # retaining legitimate partial snapshots that contain process evidence.
            substantive = any(data.get(key) for key in (
                "class", "class_code", "subjects", "distribution_date", "jurisdiction",
                "court_organ", "situation", "case_value", "movements", "participants",
                "documents", "hearings"))
            substantive = substantive or any(key in data and data.get(key) is not None
                for key in ("judicial_secrecy", "legal_aid"))
            substantive = substantive or bool(data.get("case") or data.get("sources"))
            if substantive:
                snapshots.append({"data": data, "source": source, "source_id": row["source_id"],
                                  "collected_at": row["collected_at"]})
        return snapshots

    def add_to_portfolio(self, org, name, number):
        self.get(org, number)
        if not name or len(name) > 100:
            raise ValueError("Nome da carteira inválido.")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO portfolios VALUES(?,?,?)", (org, name, now()))
            self.db.execute("INSERT OR IGNORE INTO portfolio_processes VALUES(?,?,?)", (org, name, number))

    def portfolio_numbers(self, org, name):
        return [row[0] for row in self.db.execute(
            "SELECT number FROM portfolio_processes WHERE org=? AND name=? ORDER BY number", (org, name))]

    def latest_source_check(self, org, number, source):
        self.get(org, number)
        row = self.db.execute("""SELECT * FROM source_checks WHERE org=? AND number=? AND source=?
            ORDER BY id DESC LIMIT 1""", (org, number, source)).fetchone()
        return dict(row) if row else None

    def record_access(self, action, number, purpose, *, actor="local-authenticated-user", org=None):
        with self.db:
            self._record_access_in_transaction(action, number, purpose, actor=actor, org=org)

    def _record_access_in_transaction(self, action, number, purpose, *, actor, org):
        self.db.execute("INSERT INTO access_log(at,action,number,actor,purpose,org) VALUES(?,?,?,?,?,?)",
                        (now(), action, number, actor, purpose, org))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        self.db.execute("DELETE FROM access_log WHERE at<?", (cutoff,))

    def save_tpu_catalog(self, version, reference_date, source, items):
        if not version or not source or not items:
            raise ValueError("Catálogo TPU sem versão, fonte ou itens.")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO tpu_catalog_versions VALUES(?,?,?,?)",
                            (version, reference_date, source, now()))
            for item in items:
                self.db.execute("""INSERT INTO tpu_catalog_items(version,code,payload) VALUES(?,?,?)
                    ON CONFLICT(version,code) DO UPDATE SET payload=excluded.payload""",
                    (version, item["code"], json.dumps(item, ensure_ascii=False)))

    def tpu_versions(self):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM tpu_catalog_versions ORDER BY reference_date DESC, imported_at DESC")]

    def tpu_item(self, code, version=None):
        if version is None:
            versions = self.tpu_versions()
            if not versions:
                return None
            version = versions[0]["version"]
        row = self.db.execute("SELECT payload FROM tpu_catalog_items WHERE version=? AND code=?",
                              (version, str(code))).fetchone()
        return json.loads(row[0]) if row else None

    def tpu_hierarchy(self, code, version=None):
        item = self.tpu_item(code, version)
        if item is None:
            return []
        return [{"code": parent, "name": (self.tpu_item(parent, version) or {}).get("name")}
                for parent in item.get("parents", [])] + [{"code": item["code"], "name": item["name"]}]

    def reprocess_tpu(self, org, number, version=None):
        from .tpu import enrich_movement
        self.get(org, number)
        versions = self.tpu_versions()
        if not versions:
            raise ValueError("Importe uma versão oficial da TPU antes de reprocessar.")
        version = version or versions[0]["version"]
        rows = self.timeline(org, number, revisions=True)
        count = 0
        with self.db:
            for row in rows:
                item = self.tpu_item(row["code"], version)
                if item is None:
                    continue
                payload = enrich_movement(row, item)
                self.db.execute("""INSERT INTO tpu_enrichments(org,number,fingerprint,version,payload,processed_at)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(org,number,fingerprint,version)
                    DO UPDATE SET payload=excluded.payload,processed_at=excluded.processed_at""",
                    (org, number, row["fingerprint"], version, json.dumps(payload, ensure_ascii=False), now()))
                count += 1
        return count

    def movement_enrichment(self, org, number, fingerprint, version=None):
        from .tpu import enrich_movement
        if version is None:
            versions = self.tpu_versions()
            if not versions:
                return None
            version = versions[0]["version"]
        row = self.db.execute("""SELECT payload FROM tpu_enrichments
            WHERE org=? AND number=? AND fingerprint=? AND version=?""",
            (org, number, fingerprint, version)).fetchone()
        if row:
            return json.loads(row[0])
        movement = self.db.execute("""SELECT * FROM movements
            WHERE org=? AND number=? AND fingerprint=?""", (org, number, fingerprint)).fetchone()
        if movement is None:
            return None
        item = self.tpu_item(movement["code"], version)
        return enrich_movement(dict(movement), item) if item else None

    def movement_enrichments(self, org, number, movements, version=None):
        """Resolve TPU enrichment for a timeline in a bounded number of SQL reads."""
        from .tpu import enrich_movement

        if not movements:
            return {}
        if version is None:
            versions = self.tpu_versions()
            if not versions:
                return {}
            version = versions[0]["version"]
        fingerprints = [str(row["fingerprint"]) for row in movements]
        placeholders = ",".join("?" for _ in fingerprints)
        existing = {row["fingerprint"]: json.loads(row["payload"]) for row in self.db.execute(
            f"SELECT fingerprint,payload FROM tpu_enrichments WHERE org=? AND number=? AND version=? "
            f"AND fingerprint IN ({placeholders})", (org, number, version, *fingerprints))}
        missing = [row for row in movements if row["fingerprint"] not in existing]
        codes = sorted({str(row["code"]) for row in missing})
        catalog = {}
        if codes:
            code_marks = ",".join("?" for _ in codes)
            catalog = {row["code"]: json.loads(row["payload"]) for row in self.db.execute(
                f"SELECT code,payload FROM tpu_catalog_items WHERE version=? AND code IN ({code_marks})",
                (version, *codes))}
        for row in missing:
            item = catalog.get(str(row["code"]))
            if item:
                existing[row["fingerprint"]] = enrich_movement(row, item)
        return existing

    def propose_relation(self, org, number, left_id, right_id, evidence):
        self.get(org, number)
        if left_id == right_id or not left_id or not right_id or not evidence:
            raise ValueError("Relação sem eventos distintos ou evidência.")
        with self.db:
            self.db.execute("""INSERT OR IGNORE INTO event_relations
                (org,number,left_id,right_id,status,evidence,updated_at)
                VALUES(?,?,?,?,'possible',?,?)""",
                (org, number, left_id, right_id, evidence, now()))

    def review_relation(self, org, number, left_id, right_id, status, reviewer):
        if status not in ("confirmed", "rejected") or not reviewer:
            raise ValueError("Revisão de relação inválida.")
        with self.db:
            changed = self.db.execute("""UPDATE event_relations SET status=?,reviewed_by=?,updated_at=?
                WHERE org=? AND number=? AND left_id=? AND right_id=?""",
                (status, reviewer, now(), org, number, left_id, right_id)).rowcount
        if not changed:
            raise ValueError("Relação não encontrada.")

    def event_relations(self, org, number):
        self.get(org, number)
        return [dict(row) for row in self.db.execute("""SELECT left_id,right_id,status,evidence,reviewed_by,updated_at
            FROM event_relations WHERE org=? AND number=? ORDER BY left_id,right_id""", (org, number))]

    def delete_watch(self, org, number):
        self.get(org, number)
        with self.db:
            self.db.execute("DELETE FROM collection_jobs WHERE org=? AND number=?", (org, number))
            self.db.execute("DELETE FROM event_relations WHERE org=? AND number=?", (org, number))
            self.db.execute("DELETE FROM tpu_enrichments WHERE org=? AND number=?", (org, number))
            self.db.execute("DELETE FROM portfolio_processes WHERE org=? AND number=?", (org, number))
            self.db.execute("DELETE FROM raw_records WHERE org=? AND number=?", (org, number))
            self.db.execute("DELETE FROM source_checks WHERE org=? AND number=?", (org, number))
            self.db.execute("DELETE FROM communications WHERE org=? AND number=?", (org, number))
            self.db.execute("DELETE FROM events WHERE org=? AND number=?", (org, number))
            self.db.execute("DELETE FROM movements WHERE org=? AND number=?", (org, number))
            self.db.execute("DELETE FROM runs WHERE org=? AND number=?", (org, number))
            self.db.execute("DELETE FROM scopes WHERE org=? AND number=?", (org, number))
            self.db.execute("DELETE FROM watches WHERE org=? AND number=?", (org, number))

    def runs(self, org, number):
        self.get(org, number)
        return [dict(r) for r in self.db.execute("SELECT * FROM runs WHERE org=? AND number=? ORDER BY id", (org, number))]

    def events(self, org, number, after=0):
        self.get(org, number)
        sql = """SELECT e.*, m.name AS movement_name, m.code AS movement_code,
                 m.when_at AS movement_when, m.complements AS movement_complements
                 FROM events e JOIN movements m
                 ON m.org=e.org AND m.number=e.number AND m.fingerprint=e.fingerprint
                 WHERE e.org=? AND e.number=? AND e.id>? ORDER BY e.id"""
        return [dict(r) for r in self.db.execute(sql, (org, number, after))]
