-- Schema from 9d8a3fc; no user data or credentials.
CREATE TABLE tasks (
                    task_id TEXT PRIMARY KEY,
                    file_name TEXT NOT NULL,
                    file_path TEXT,
                    status TEXT DEFAULT 'pending',
                    priority INTEGER DEFAULT 0,
                    backend TEXT DEFAULT 'pipeline',
                    options TEXT,
                    result_path TEXT,
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    worker_id TEXT,
                    retry_count INTEGER DEFAULT 0
                , parent_task_id TEXT, is_parent INTEGER DEFAULT 0, child_count INTEGER DEFAULT 0, child_completed INTEGER DEFAULT 0, user_id TEXT, data TEXT, api_key_id TEXT, merge_claimed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE task_file_access (
        task_id TEXT NOT NULL, kind TEXT NOT NULL, raw_path TEXT NOT NULL,
        canonical_path TEXT, PRIMARY KEY(task_id, kind));
CREATE TABLE task_webhooks (
        task_id TEXT PRIMARY KEY, config TEXT NOT NULL);
CREATE TABLE webhook_deliveries (
        id TEXT PRIMARY KEY, task_id TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'pending', next_at REAL NOT NULL DEFAULT 0,
        lease_token TEXT, config TEXT NOT NULL, payload TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, last_error TEXT,
        retry_count INTEGER NOT NULL, task_status TEXT NOT NULL,
        UNIQUE(task_id, retry_count, task_status));
CREATE TABLE users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT,
                    full_name TEXT,
                    role TEXT DEFAULT 'user',
                    is_active BOOLEAN DEFAULT 1,
                    is_sso BOOLEAN DEFAULT 0,
                    sso_provider TEXT,
                    sso_subject TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_login TIMESTAMP
                , token_epoch INTEGER DEFAULT 0);
CREATE TABLE api_keys (
                    key_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    api_key_hash TEXT NOT NULL,
                    name TEXT NOT NULL,
                    prefix TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP,
                    last_used TIMESTAMP, scopes TEXT, webhook_enabled INTEGER DEFAULT 0, webhook_url TEXT, webhook_secret TEXT, webhook_auth_type TEXT, webhook_auth_token TEXT, webhook_auth_username TEXT, webhook_auth_password TEXT, webhook_auth_header_name TEXT, webhook_auth_header_value TEXT,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                );
CREATE TABLE revoked_tokens (
                    jti TEXT PRIMARY KEY,
                    user_id TEXT,
                    expires_at TIMESTAMP
                );
CREATE INDEX idx_status ON tasks(status);
CREATE INDEX idx_priority ON tasks(priority DESC);
CREATE INDEX idx_created_at ON tasks(created_at);
CREATE INDEX idx_worker_id ON tasks(worker_id);
CREATE INDEX idx_parent_task ON tasks(parent_task_id);
CREATE INDEX idx_is_parent ON tasks(is_parent);
CREATE INDEX idx_file_access_path ON task_file_access(kind,canonical_path);
CREATE INDEX idx_file_access_dirty ON task_file_access(canonical_path) WHERE canonical_path IS NULL;
CREATE INDEX idx_webhook_due ON webhook_deliveries(state, next_at);
CREATE INDEX idx_username ON users(username);
CREATE INDEX idx_email ON users(email);
CREATE INDEX idx_sso_subject ON users(sso_subject);
CREATE INDEX idx_api_key_prefix ON api_keys(prefix);
CREATE INDEX idx_api_key_user ON api_keys(user_id);
CREATE TRIGGER file_access_upload_insert AFTER INSERT ON tasks
                BEGIN
                    DELETE FROM task_file_access WHERE task_id=NEW.task_id AND kind='upload';
                    INSERT INTO task_file_access(task_id,kind,raw_path)
                    SELECT NEW.task_id,'upload',NEW.file_path
                    WHERE NEW.file_path IS NOT NULL AND NEW.file_path != 'CLEARED';
                END;
CREATE TRIGGER file_access_upload_update AFTER UPDATE OF file_path ON tasks
                BEGIN
                    DELETE FROM task_file_access WHERE task_id=NEW.task_id AND kind='upload';
                    INSERT INTO task_file_access(task_id,kind,raw_path)
                    SELECT NEW.task_id,'upload',NEW.file_path
                    WHERE NEW.file_path IS NOT NULL AND NEW.file_path != 'CLEARED';
                END;
CREATE TRIGGER file_access_output_insert AFTER INSERT ON tasks
                BEGIN
                    DELETE FROM task_file_access WHERE task_id=NEW.task_id AND kind='output';
                    INSERT INTO task_file_access(task_id,kind,raw_path)
                    SELECT NEW.task_id,'output',NEW.result_path
                    WHERE NEW.result_path IS NOT NULL AND NEW.result_path != 'CLEARED';
                END;
CREATE TRIGGER file_access_output_update AFTER UPDATE OF result_path ON tasks
                BEGIN
                    DELETE FROM task_file_access WHERE task_id=NEW.task_id AND kind='output';
                    INSERT INTO task_file_access(task_id,kind,raw_path)
                    SELECT NEW.task_id,'output',NEW.result_path
                    WHERE NEW.result_path IS NOT NULL AND NEW.result_path != 'CLEARED';
                END;
CREATE TRIGGER file_access_delete AFTER DELETE ON tasks BEGIN
        DELETE FROM task_file_access WHERE task_id=OLD.task_id; END;
CREATE TRIGGER task_webhook_terminal
        AFTER UPDATE OF status ON tasks
        WHEN NEW.parent_task_id IS NULL AND NEW.status IN ('completed','failed','cancelled')
             AND OLD.status != NEW.status
        BEGIN
          INSERT OR IGNORE INTO webhook_deliveries
          (id, task_id, config, payload, retry_count, task_status)
          SELECT lower(hex(randomblob(16))), NEW.task_id, w.config,
            json_object('task_id',NEW.task_id,'file_name',NEW.file_name,'status',NEW.status,
              'retry_count',NEW.retry_count,'completed_at',NEW.completed_at),
            NEW.retry_count, NEW.status
          FROM task_webhooks w WHERE w.task_id=NEW.task_id;
        END;
