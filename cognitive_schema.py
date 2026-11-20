"""M2 仅增表，消息与发送账本继续归 M1 管理。"""

SCHEMA = (
    """CREATE TABLE memory_jobs (
        id INTEGER PRIMARY KEY, scope TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('live','simulation')),
        platform TEXT NOT NULL, channel_kind TEXT NOT NULL, channel_id TEXT NOT NULL, message_id TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','done','failed','suppressed')),
        attempts INTEGER NOT NULL DEFAULT 0, available REAL NOT NULL, lease TEXT, error TEXT,
        UNIQUE(platform,channel_kind,channel_id,message_id), UNIQUE(scope,id),
        FOREIGN KEY(platform,channel_kind,channel_id,message_id)
          REFERENCES events(platform,channel_kind,channel_id,message_id) ON DELETE CASCADE
    )""",
    "CREATE INDEX memory_jobs_ready ON memory_jobs(mode,state,available,id)",
    """CREATE TABLE memory_nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, subject TEXT NOT NULL, slot TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('person','event','fact','preference','promise','autobiography','reflection')),
        topic TEXT NOT NULL, value TEXT NOT NULL, state TEXT NOT NULL
          CHECK(state IN ('active','superseded','conflict','inactive')),
        origin TEXT NOT NULL CHECK(origin IN ('message','system','inference')),
        importance INTEGER NOT NULL CHECK(importance BETWEEN 1 AND 5),
        confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        UNIQUE(scope,id), UNIQUE(scope,slot,value)
    )""",
    "CREATE INDEX memory_scope_search ON memory_nodes(scope,subject,state,kind,updated_at DESC)",
    "CREATE INDEX memory_slot ON memory_nodes(scope,slot,state)",
    """CREATE TABLE memory_edges (
        scope TEXT NOT NULL, origin_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
        relation TEXT NOT NULL CHECK(relation IN ('participated_in','related_to','supersedes','supports','contradicts','belongs_to')),
        at REAL NOT NULL, confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
        CHECK(origin_id<>target_id), PRIMARY KEY(scope,origin_id,target_id,relation),
        FOREIGN KEY(scope,origin_id) REFERENCES memory_nodes(scope,id) ON DELETE CASCADE,
        FOREIGN KEY(scope,target_id) REFERENCES memory_nodes(scope,id) ON DELETE CASCADE
    )""",
    "CREATE INDEX memory_edge_targets ON memory_edges(scope,target_id,relation)",
    """CREATE TABLE memory_sources (
        scope TEXT NOT NULL, node_id INTEGER NOT NULL, source_key TEXT NOT NULL, job_id INTEGER,
        at REAL NOT NULL, PRIMARY KEY(scope,node_id,source_key),
        FOREIGN KEY(scope,node_id) REFERENCES memory_nodes(scope,id) ON DELETE CASCADE,
        FOREIGN KEY(scope,job_id) REFERENCES memory_jobs(scope,id) ON DELETE CASCADE
    )""",
    """CREATE TABLE memory_revisions (
        id INTEGER PRIMARY KEY, scope TEXT NOT NULL, node_id INTEGER NOT NULL, at REAL NOT NULL,
        action TEXT NOT NULL, previous_state TEXT, evidence_node INTEGER,
        FOREIGN KEY(scope,node_id) REFERENCES memory_nodes(scope,id) ON DELETE CASCADE
    )""",
    """CREATE TABLE memory_goals (
        scope TEXT NOT NULL, node_id INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('proposed','active','completed','cancelled','expired')),
        due_at REAL, condition TEXT NOT NULL, may_execute INTEGER NOT NULL DEFAULT 0 CHECK(may_execute=0),
        updated_at REAL NOT NULL, PRIMARY KEY(scope,node_id),
        FOREIGN KEY(scope,node_id) REFERENCES memory_nodes(scope,id) ON DELETE CASCADE
    )""",
    """CREATE TABLE memory_self_state (
        scope TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, updated_at REAL NOT NULL,
        PRIMARY KEY(scope,key)
    )""",
    """CREATE TABLE memory_suppression (
        scope TEXT NOT NULL, fingerprint TEXT NOT NULL, PRIMARY KEY(scope,fingerprint)
    )""",
)
