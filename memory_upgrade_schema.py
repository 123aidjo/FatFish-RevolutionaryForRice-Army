"""Additive v6 migration; original memory and evidence tables remain authoritative."""
SCHEMA = (
    '''CREATE TABLE memory_vectors (
        node_id INTEGER PRIMARY KEY REFERENCES memory_nodes(id) ON DELETE CASCADE,
        generation TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL DEFAULT '',
        dimension INTEGER, vector BLOB, state TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, available REAL NOT NULL DEFAULT 0,
        lease TEXT, error TEXT, updated_at REAL NOT NULL DEFAULT 0)''',
    '''CREATE TABLE memory_lifecycle (
        node_id INTEGER PRIMARY KEY REFERENCES memory_nodes(id) ON DELETE CASCADE,
        tier TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active', expires REAL,
        recalls INTEGER NOT NULL DEFAULT 0, last_recall REAL, revision REAL NOT NULL DEFAULT 0,
        epistemic TEXT NOT NULL DEFAULT 'self_report')''',
    '''CREATE TABLE memory_tasks (
        id INTEGER PRIMARY KEY, node_id INTEGER NOT NULL REFERENCES memory_nodes(id) ON DELETE CASCADE,
        kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        available REAL NOT NULL DEFAULT 0, lease TEXT, error TEXT, version REAL,
        UNIQUE(node_id,kind))''',
    "CREATE INDEX memory_tasks_ready ON memory_tasks(state,available)",
    "INSERT INTO memory_vectors(node_id) SELECT id FROM memory_nodes",
    "INSERT INTO memory_lifecycle(node_id,status,epistemic) SELECT id,CASE WHEN state='active' THEN 'active' ELSE 'inactive' END,CASE WHEN origin='inference' THEN 'inference' WHEN origin='system' THEN 'observation' ELSE 'self_report' END FROM memory_nodes",
    '''CREATE TRIGGER memory_vector_insert AFTER INSERT ON memory_nodes BEGIN
        INSERT INTO memory_vectors(node_id) VALUES(new.id);
        INSERT INTO memory_lifecycle(node_id,status,epistemic) VALUES(new.id,
            CASE WHEN new.state='active' THEN 'active' ELSE 'inactive' END,
            CASE WHEN new.origin='inference' THEN 'inference' WHEN new.origin='system' THEN 'observation' ELSE 'self_report' END);
    END''',
    '''CREATE TRIGGER memory_vector_edit AFTER UPDATE OF value,topic ON memory_nodes
        WHEN old.value<>new.value OR old.topic<>new.topic BEGIN
        UPDATE memory_vectors SET state='pending',vector=NULL,attempts=0,available=0,lease=NULL WHERE node_id=new.id;
    END''',
    '''CREATE TRIGGER memory_lifecycle_state AFTER UPDATE OF state ON memory_nodes
        WHEN old.state<>new.state BEGIN
        UPDATE memory_lifecycle SET status=CASE WHEN new.state='active' THEN 'active' ELSE 'inactive' END,
            revision=new.updated_at WHERE node_id=new.id;
    END''',
)
